"""
Application control (requirement 20: run any installed app).

Launching resolves the spoken name in this order:
  1. built-in Windows tools and settings URIs (always present: notepad, calc, settings...),
  2. an index of every Start Menu shortcut (all users + current user) and every Store/UWP
     app reported by `Get-StartApps`, matched fuzzily (exact > prefix > substring > close
     spelling), so "open spotify", "open VS code" or "메모장 열어줘" find the real app,
  3. otherwise a polite "couldn't find it" (no error pop-ups).
Closing terminates matching processes gracefully (psutil), falling back to a kill only if
they don't exit.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import time
from typing import List, Optional, Tuple

from .base import ok, fail, powershell, IS_WINDOWS, IS_MAC

# Spoken name -> built-in executable / URI that exists on every Windows install.
SYSTEM_TOOLS = {
    "notepad": "notepad", "메모장": "notepad",
    "calculator": "calc", "calc": "calc", "계산기": "calc",
    "paint": "mspaint", "그림판": "mspaint",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "탐색기": "explorer", "파일 탐색기": "explorer",
    "cmd": "cmd", "command prompt": "cmd", "명령 프롬프트": "cmd",
    "powershell": "powershell", "task manager": "taskmgr", "작업 관리자": "taskmgr",
    "control panel": "control", "제어판": "control",
    "settings": "ms-settings:", "설정": "ms-settings:",
    "camera": "microsoft.windows.camera:", "카메라": "microsoft.windows.camera:",
    "terminal": "wt",
}
# Spoken name -> how the app appears in the Start Menu (fuzzy-matched afterwards).
SYNONYMS = {
    "chrome": "google chrome", "크롬": "google chrome",
    "edge": "microsoft edge", "엣지": "microsoft edge",
    "word": "word", "워드": "word", "excel": "excel", "엑셀": "excel",
    "powerpoint": "powerpoint", "파워포인트": "powerpoint", "outlook": "outlook",
    "vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
    "teams": "microsoft teams", "spotify": "spotify", "discord": "discord", "steam": "steam",
    "kakaotalk": "kakaotalk", "카카오톡": "카카오톡", "카톡": "카카오톡",
}
# Spoken name -> process image name, for closing.
PROCESS_NAMES = {
    "chrome": "chrome", "크롬": "chrome", "edge": "msedge", "엣지": "msedge",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
    "notepad": "notepad", "메모장": "notepad", "calculator": "calculatorapp", "계산기": "calculatorapp",
    "spotify": "spotify", "vscode": "code", "vs code": "code", "discord": "discord",
    "teams": "ms-teams", "카카오톡": "kakaotalk", "kakaotalk": "kakaotalk",
}

_INDEX = {"t": 0.0, "items": []}   # items: (display name, kind 'lnk'|'aumid', target)
_INDEX_TTL = 3600


def _start_menu_dirs() -> List[str]:
    dirs = []
    for env, sub in (("ProgramData", r"Microsoft\Windows\Start Menu\Programs"),
                     ("APPDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base and os.path.isdir(os.path.join(base, sub)):
            dirs.append(os.path.join(base, sub))
    return dirs


def build_index(force: bool = False) -> List[Tuple[str, str, str]]:
    if not IS_WINDOWS:
        return []
    if not force and _INDEX["items"] and time.time() - _INDEX["t"] < _INDEX_TTL:
        return _INDEX["items"]
    items, seen = [], set()
    for d in _start_menu_dirs():
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                name = f[:-4]
                low = name.lower()
                if "uninstall" in low or "제거" in low or low in seen:
                    continue
                seen.add(low)
                items.append((name, "lnk", os.path.join(root, f)))
    try:  # Store / UWP apps (Calculator, Spotify from the Store, etc.)
        out = powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress", timeout=25)
        data = json.loads(out) if out else []
        if isinstance(data, dict):
            data = [data]
        for app in data:
            name, aumid = app.get("Name"), app.get("AppID")
            if name and aumid and name.lower() not in seen:
                seen.add(name.lower())
                items.append((name, "aumid", aumid))
    except Exception:
        pass
    _INDEX.update(t=time.time(), items=items)
    return items


def best_match(query: str, items: List[Tuple[str, str, str]]) -> Optional[Tuple[str, str, str]]:
    q = (query or "").strip().lower()
    if not q or not items:
        return None
    exact = [it for it in items if it[0].lower() == q]
    if exact:
        return exact[0]
    prefix = [it for it in items if it[0].lower().startswith(q)]
    if prefix:
        return min(prefix, key=lambda it: len(it[0]))
    contains = [it for it in items if q in it[0].lower()]
    if contains:
        return min(contains, key=lambda it: len(it[0]))
    names = [it[0].lower() for it in items]
    close = difflib.get_close_matches(q, names, n=1, cutoff=0.72)
    return items[names.index(close[0])] if close else None


def launch_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application, sir?")
    key = name.lower()
    if IS_WINDOWS:
        try:
            tool = SYSTEM_TOOLS.get(key)
            if tool:
                if tool.endswith(":"):
                    os.startfile(tool)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([tool], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                     if tool in ("cmd", "powershell") else 0)
                return ok(f"Opening {name}, sir.", {"app": name, "target": tool})
            match = best_match(SYNONYMS.get(key, key), build_index())
            if match:
                disp, kind, target = match
                if kind == "lnk":
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
                return ok(f"Opening {disp}, sir.", {"app": disp, "kind": kind})
            return fail(f"I couldn't find an app called {name} on this PC, sir.",
                        error=f"no Start Menu or Store app matched '{name}'")
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))

    # non-Windows (development / testing)
    if IS_MAC:
        try:
            subprocess.run(["open", "-a", name], capture_output=True, timeout=10)
            return ok(f"Opening {name}, sir.", {"app": name})
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))
    exe = shutil.which(key) or shutil.which(key.replace(" ", "-"))
    if not exe:
        return fail(f"I can't find {name} on this system, sir.", error=f"executable not found: {name}")
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok(f"Opening {name}, sir.", {"app": name, "target": exe})
    except Exception as e:
        return fail(f"I couldn't open {name}, sir.", str(e))


def close_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application should I close, sir?")
    try:
        import psutil  # type: ignore
    except Exception:
        return fail("Closing apps needs the psutil library, sir.", "psutil missing")
    want = PROCESS_NAMES.get(name.lower(), name.lower().replace(" ", ""))
    procs = []
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info.get("name") or "").lower()
        base = pname[:-4] if pname.endswith(".exe") else pname
        if base == want or (len(want) >= 4 and want in base):
            if p.pid != os.getpid():
                procs.append(p)
    if not procs:
        return fail(f"{name} doesn't appear to be running, sir.")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=4)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    return ok(f"Closed {name}, sir.", {"app": name, "processes": len(procs)})
