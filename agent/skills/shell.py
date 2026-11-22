"""
Shell commands (v1.4.3): JARVIS runs PowerShell commands for you -- when you ask by voice or
text ("run the command Get-Process", "셸에서 ipconfig 실행해 줘"), or when its language model
decides a command would do the job.

Permissions -- JARVIS Settings > Permissions, "allow shell commands", or config/permissions.json:
  run_shell           ask (default) | allow | deny    PowerShell as you
  run_shell_elevated  deny (default) | ask | allow    PowerShell as administrator; Windows' own
                                                      UAC prompt still appears for every command
Safety that no setting turns off:
  * a command that can destroy data or the system (format, diskpart, recursive deletes,
    bcdedit, vssadmin delete, registry deletes, download-and-run, disabling Defender, ...)
    is always shown to you for confirmation first,
  * every command and its complete output are recorded (the audit log and data/results),
  * commands time out (60 seconds by default) and what is shown on screen is capped.
The language model may ask for commands, but it can never change these permissions.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional, Tuple

from .base import ok, fail

IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
SHOW_LINES, SHOW_CHARS = 40, 4000
PRELUDE = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
           "$ProgressPreference='SilentlyContinue'; ")

# (pattern, why) -- matched case-insensitively against the whole command
DANGEROUS = [
    (r"\bformat(\.com)?\s+[a-z]:", "formats a drive"),
    (r"\bformat-volume\b|\bclear-disk\b|\binitialize-disk\b|\bremove-partition\b", "erases a disk or partition"),
    (r"\bdiskpart\b", "edits disks and partitions"),
    (r"\bremove-item\b[^|;]*-r(ecurse)?\b|\brm\b[^|;]*\s-[a-z]*r|\brmdir\b[^|;]*/s|\brd\b\s[^|;]*/s|"
     r"\bdel\b[^|;]*/s|\berase\b[^|;]*/s", "deletes files recursively"),
    (r"\b(remove-item|del|erase|rm|rmdir|rd)\b[^|;]*(\\windows|\\system32|\\program files|[a-z]:\\\s*$|[a-z]:\\\*)",
     "deletes system files or a whole drive"),
    (r"\bclear-content\b|\bclear-recyclebin\b", "erases file contents"),
    (r"\bcipher\b[^|;]*/w", "wipes free disk space"),
    (r"\bvssadmin\b[^|;]*delete|\bwbadmin\b[^|;]*delete|\bwmic\b[^|;]*shadowcopy[^|;]*delete", "deletes backups"),
    (r"\bbcdedit\b|\bbootrec\b|\bbcdboot\b", "changes how Windows starts"),
    (r"\breg(\.exe)?\s+delete\b|\bremove-itemproperty\b[^|;]*hklm|\bremove-item\b[^|;]*hk(lm|cu):",
     "deletes registry settings"),
    (r"\bset-executionpolicy\b", "changes the script security policy"),
    (r"\b(stop|restart)-computer\b|\bshutdown(\.exe)?\b", "shuts down or restarts the PC"),
    (r"\btakeown\b|\bicacls\b[^|;]*/(reset|grant|remove)", "changes file ownership or access rights"),
    (r"\bnet\s+user\b[^|;]*/(delete|add)|\b(remove|new)-localuser\b|\bnet\s+localgroup\b[^|;]*/add",
     "changes user accounts"),
    (r"\bset-mppreference\b[^|;]*-disable|\badd-mppreference\b[^|;]*exclusion", "weakens Windows Defender"),
    (r"\bnetsh\b[^|;]*(firewall|advfirewall)[^|;]*(off|disable|delete)", "turns off the firewall"),
    (r"(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^|;]*\|\s*(iex|invoke-expression)\b|"
     r"\biex\b\s*\(\s*(iwr|irm|new-object|invoke-webrequest|invoke-restmethod)", "downloads and runs code"),
    (r"-encodedcommand\b|-enc\s+[a-z0-9+/=]{16,}", "runs hidden (encoded) code"),
    (r"start-process\b[^|;]*-verb\s+runas", "asks for administrator rights"),
    (r"\buninstall-package\b|\bwmic\b[^|;]*\bcall\s+uninstall\b|\bmsiexec\b[^|;]*/x", "uninstalls software"),
    (r"\bdisable-netadapter\b|\bremove-netadapter\b", "disconnects the network"),
    (r"\bschtasks\b[^|;]*/(create|delete)|\b(register|unregister)-scheduledtask\b", "changes scheduled tasks"),
]
_DANGEROUS = [(re.compile(p, re.IGNORECASE), why) for p, why in DANGEROUS]


def guard(command: str) -> Tuple[bool, str]:
    """(dangerous, why): commands that are always confirmed, whatever the permission says."""
    c = (command or "").strip()
    for pat, why in _DANGEROUS:
        if pat.search(c):
            return True, why
    return False, ""


def preflight(args: dict, text: str = "") -> dict:
    """What the permission prompt shows for a shell command, and whether it must ask."""
    command = str((args or {}).get("command") or "").strip()
    elevated = bool((args or {}).get("_elevated"))
    danger, why = guard(command)
    label = "PowerShell as administrator" if elevated else "PowerShell"
    detail = f"{label}: {command}" + (f"\n(always confirmed: it {why})" if danger else "")
    return {"detail": detail, "force_ask": danger, "keywords": False,
            "risk": 0.95 if danger else (0.8 if elevated else 0.6)}


def _powershell() -> Optional[str]:
    for name in ("powershell", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                     "powershell.exe")
    return p if os.path.exists(p) else None


def _encode(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _timeout(value) -> int:
    try:
        return max(1, min(MAX_TIMEOUT, int(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _save(command: str, output: str, code, elevated: bool) -> Optional[str]:
    try:
        fd, path = tempfile.mkstemp(prefix="jarvis_shell_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  {'administrator ' if elevated else ''}command:\n"
                     f"{command}\n# exit code: {code}\n\n{output}")
        return path
    except OSError:
        return None


def _shown(output: str) -> str:
    lines = output.rstrip().splitlines()
    cut = lines[:SHOW_LINES]
    text = "\n".join(cut)[:SHOW_CHARS]
    if len(lines) > SHOW_LINES or len(text) < len("\n".join(cut)):
        text += f"\n... ({len(lines)} lines in all; the full output is saved with my records)"
    return text


def _result(command: str, output: str, code, elevated: bool, seconds: float) -> dict:
    output = output.replace("\r\n", "\n").strip()
    saved = _save(command, output, code, elevated)
    lines = [ln for ln in output.splitlines() if ln.strip()]
    shown = _shown(output) if output else "(no output)"
    data = {"command": command, "exit_code": code, "elevated": elevated, "seconds": round(seconds, 2),
            "lines": len(lines), "show": f"$ {command}\n{shown}",
            "generated_files": [saved] if saved else [], "temp_files": [saved] if saved else []}
    if code == 0:
        if not lines:
            speak, ko = "Done, sir. The command finished without output.", "완료했습니다, sir. 출력 없이 끝났습니다."
        elif len(lines) == 1 and len(lines[0]) <= 140:
            speak, ko = f"Done, sir: {lines[0].strip()}", f"완료했습니다, sir: {lines[0].strip()}"
        else:
            speak = f"Done, sir. It printed {len(lines)} lines; they're on screen."
            ko = f"완료했습니다, sir. 출력 {len(lines)}줄을 화면에 표시했습니다."
        data["speak_ko"] = ko
        return ok(speak, data)
    first = (lines[-1] if lines else "no error text").strip()[:200]
    data["speak_ko"] = f"명령이 실패했습니다, sir (종료 코드 {code}): {first}"
    r = fail(f"The command failed, sir (exit code {code}): {first}", f"exit code {code}")
    r["data"] = data
    return r


def run_shell(command: str = "", timeout=DEFAULT_TIMEOUT, **_) -> dict:
    """Run a PowerShell command as the signed-in user (another shell on non-Windows systems)."""
    command = (command or "").strip()
    if not command:
        return fail("Which command shall I run, sir?", "no command")
    t = _timeout(timeout)
    t0 = time.time()
    try:
        if IS_WINDOWS:
            ps = _powershell()
            if not ps:
                return fail("I couldn't find PowerShell on this PC, sir.", "powershell missing")
            cmd = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-EncodedCommand", _encode(PRELUDE + command)]
        else:
            cmd = [os.environ.get("SHELL") or "/bin/sh", "-c", command]
        r = subprocess.run(cmd, capture_output=True, timeout=t, cwd=os.path.expanduser("~"),
                           stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace")
        res = _result(command, partial + f"\n(stopped after {t} seconds)", "timeout", False, time.time() - t0)
        res["speak"] = f"The command was still running after {t} second{'s' if t != 1 else ''}, sir, so I stopped it."
        res["data"]["speak_ko"] = f"명령이 {t}초가 지나도 끝나지 않아 중단했습니다, sir."
        return res
    except OSError as e:
        return fail(f"I couldn't run that command, sir: {e}", str(e))
    output = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    return _result(command, output, r.returncode, False, time.time() - t0)


def run_shell_elevated(command: str = "", timeout=DEFAULT_TIMEOUT, **_) -> dict:
    """Run a PowerShell command as administrator. Windows shows its UAC prompt every time."""
    command = (command or "").strip()
    if not command:
        return fail("Which command shall I run as administrator, sir?", "no command")
    if not IS_WINDOWS:
        return fail("Running commands as administrator is only available on Windows, sir.", "not windows")
    ps = _powershell()
    if not ps:
        return fail("I couldn't find PowerShell on this PC, sir.", "powershell missing")
    t = _timeout(timeout)
    fd, out_path = tempfile.mkstemp(prefix="jarvis_admin_", suffix=".txt")
    os.close(fd)
    inner = (PRELUDE + "& { " + command + " } *>&1 | Out-File -FilePath '" + out_path.replace("'", "''") +
             "' -Encoding utf8; exit $LASTEXITCODE")
    outer = ("$p = Start-Process -FilePath '" + ps.replace("'", "''") + "' -Verb RunAs -WindowStyle Hidden -Wait "
             "-PassThru -ArgumentList '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand','"
             + _encode(inner) + "'; exit $p.ExitCode")
    t0 = time.time()
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", _encode(PRELUDE + outer)],
                           capture_output=True, timeout=t + 120, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)          # + time to answer the UAC prompt
    except subprocess.TimeoutExpired:
        return fail("The administrator command did not finish in time, sir.", "timeout")
    except OSError as e:
        return fail(f"I couldn't run that command as administrator, sir: {e}", str(e))
    try:
        with open(out_path, encoding="utf-8-sig", errors="replace") as fh:
            output = fh.read()
    except OSError:
        output = ""
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    err = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0 and ("canceled by the user" in err.lower() or "취소" in err):
        res = fail("You declined the administrator prompt, sir, so nothing was run.", "UAC declined")
        res["data"] = {"speak_ko": "관리자 권한 요청을 거절하셔서 아무것도 실행하지 않았습니다, sir."}
        return res
    return _result(command, output + ("\n" + err if err.strip() and not output.strip() else ""),
                   r.returncode, True, time.time() - t0)


# ------------------------------------------------------------ permission changes
_APPLY = None          # set by the server: apply_permission(category, level) -> dict


def set_permission_writer(fn) -> None:
    global _APPLY
    _APPLY = fn


def set_permission(level: str = "allow", scope: str = "normal", **_) -> dict:
    """'allow shell commands' / 'always ask before shell commands' / 'block shell commands'
    (and the same for administrator commands). Only from the user, never from the model."""
    level = level if level in ("allow", "ask", "deny") else "ask"
    category = "run_shell_elevated" if scope == "admin" else "run_shell"
    if _APPLY is None:
        return fail("I can't change my permissions from here, sir.", "no writer")
    res = _APPLY(category, level)
    if res.get("rejected"):
        return fail(f"I couldn't change that permission, sir: {res['rejected']}", "rejected")
    what = "administrator commands (Windows still asks with its own prompt each time)" if scope == "admin" \
        else "shell commands"
    what_ko = "관리자 권한 명령(Windows가 매번 자체 확인 창을 띄웁니다)" if scope == "admin" else "셸 명령"
    speak = {"allow": f"Very good, sir. I may now run {what} without asking first - except anything that could "
                      f"destroy data, which I'll always show you.",
             "ask": f"Very good, sir. I'll ask you before running {what}.",
             "deny": f"Very good, sir. I won't run {what}."}[level]
    ko = {"allow": f"알겠습니다, sir. 이제 {what_ko}을 묻지 않고 실행합니다. 단, 데이터를 망가뜨릴 수 있는 명령은 항상 먼저 보여 드립니다.",
          "ask": f"알겠습니다, sir. {what_ko}을 실행하기 전에 여쭙겠습니다.",
          "deny": f"알겠습니다, sir. {what_ko}은 실행하지 않겠습니다."}[level]
    return ok(speak, {"category": category, "level": level, "speak_ko": ko})
