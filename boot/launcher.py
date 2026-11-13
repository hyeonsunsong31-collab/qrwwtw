"""
boot/launcher.py -- what START_JARVIS.bat runs: start JARVIS and prove that it appeared.

Double-clicking START used to fire the logon task and hope. When anything was wrong -- the
overlay runtime missing, an old copy still holding the port, a child crashing at start-up
-- nothing appeared and nothing said why. This launcher takes responsibility instead:

  1. checks the installation and repairs the overlay runtime if it is incomplete,
  2. finds what is already running; if JARVIS is healthy it just asks it to show itself,
  3. otherwise stops stale JARVIS processes and starts a fresh background service,
  4. waits until the service runs, the HUD window is on screen, the brain answers and the
     two are connected -- and says so; or says exactly which step failed and what to do,
     and saves a full report to data/logs/diagnostics.txt.

    python boot/launcher.py                  start (or show) JARVIS and verify it
    python boot/launcher.py --restart        stop everything first, then start fresh
    python boot/launcher.py --stop           stop JARVIS (auto-start at sign-in is kept)
    python boot/launcher.py --status         report what is running, change nothing
    python boot/launcher.py --wait           only wait for JARVIS to come up (installer)
    python boot/launcher.py --safe-graphics  restart with software rendering (and keep it)
    python boot/launcher.py --normal-graphics   back to normal rendering

Exit codes: 0 = JARVIS is online (or stopped, for --stop); 1 = it did not come up (the
reason was printed); 3 = the installation is incomplete -- INSTALL_JARVIS.bat repairs it.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib.util
import platform
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from boot import common, overlay_runtime

IS_WIN = common.IS_WIN
REQUIRED_FILES = ["agent/server.py", "boot/jarvis_service.py", "overlay/main.js", "overlay/preload.js",
                  "overlay/renderer/index.html", "config/jarvis.config.json", "package-lock.json"]
# import name -> what breaks without it (the backend cannot start, or the overlay cannot connect)
REQUIRED_PACKAGES = {"numpy": "neural core", "fastapi": "command bus", "uvicorn": "command bus",
                     "websockets": "overlay connection", "httpx": "language engine client",
                     "psutil": "process and system control"}
EXIT_OK, EXIT_FAILED, EXIT_NEEDS_INSTALL = 0, 1, 3

T_SUPERVISOR = 25        # seconds for the background service to report in
T_OVERLAY = 75           # ... for the HUD window to be on screen
T_BACKEND = 240          # ... for the brain to answer (first start builds the neural mesh)
T_CONNECT = 45           # ... for the HUD to connect to the brain once both are up


# ------------------------------------------------------------------ console
def say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except Exception:
        pass


def ok(text: str) -> None:
    say("   [ok]  " + text)


def note(text: str) -> None:
    say("   [..]  " + text)


def bad(text: str) -> None:
    say("   [!!]  " + text)


def wrap(text: str, indent: str = "     ", width: int = 74) -> List[str]:
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if line and len(line) + 1 + len(word) > width:
                out.append(indent + line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(indent + line if line else "")
    return out


# ------------------------------------------------------------------ preflight
def path_notes() -> List[str]:
    notes = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(var)
        if base and common._norm(ROOT).startswith(common._norm(base) + "/"):
            notes.append("This folder is inside OneDrive. Syncing thousands of program files slows JARVIS down "
                         "and can lock them; a plain folder such as C:\\JARVIS works better.")
            break
    try:
        ROOT.encode("ascii")
    except UnicodeEncodeError:
        notes.append("The folder path contains non-English characters. The display works, but some speech "
                     "models cannot be loaded from such a path; a folder such as C:\\JARVIS avoids that.")
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if tmp and common._norm(ROOT).startswith(common._norm(tmp) + "/"):
        notes.append("This folder is inside the temporary folder -- JARVIS seems to be running from inside the "
                     "ZIP. Extract the whole ZIP (right-click > Extract All) and start it from there.")
    return notes


def missing_files() -> List[str]:
    return [f for f in REQUIRED_FILES if not os.path.isfile(os.path.join(ROOT, *f.split("/")))]


def missing_packages() -> List[str]:
    out = []
    for name in REQUIRED_PACKAGES:
        try:
            found = importlib.util.find_spec(name) is not None
        except Exception:
            found = False
        if not found:
            out.append(name)
    return out


def autostart_registered() -> Optional[bool]:
    if not IS_WIN:
        return None
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "JARVIS"], capture_output=True, timeout=15,
                           stdin=subprocess.DEVNULL, creationflags=common.CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return None


# ------------------------------------------------------------------ state
def supervisor_state(started_after: Optional[float] = None) -> Tuple[bool, dict]:
    """(alive, status) for a 1.3+ supervisor running from THIS folder."""
    sup = common.read_json(common.status_file()) or {}
    try:
        alive = (bool(sup) and common.same_path(str(sup.get("root", "")), ROOT)
                 and sup.get("state") == "running" and common.pid_alive(sup.get("pid"))
                 and time.time() - float(sup.get("updated") or 0) < 30
                 and (started_after is None or float(sup.get("started") or 0) >= started_after - 2))
    except (TypeError, ValueError):
        alive = False
    return alive, sup


def overlay_state(sup: dict) -> Tuple[bool, dict]:
    """(on screen, state) -- the HUD window of the overlay process this supervisor started."""
    child = (sup.get("children") or {}).get("overlay") or {}
    ov = common.read_json(common.overlay_state_file()) or {}
    shown = bool(child.get("alive") and ov and ov.get("pid") == child.get("pid")
                 and ov.get("visible") and ov.get("loaded"))
    return shown, ov


def inspect() -> dict:
    sup_alive, sup = supervisor_state()
    shown, ov = overlay_state(sup) if sup_alive else (False, {})
    bstate, bstatus = common.backend_status()
    connected = bstate == "ok" and int((bstatus or {}).get("overlay_clients") or 0) >= 1
    return {"supervisor": sup_alive, "sup": sup, "overlay": shown, "ov": ov, "backend": bstate,
            "status": bstatus or {}, "connected": connected,
            "healthy": sup_alive and shown and connected}


def port_owner(port: int) -> str:
    ps = common._psutil()
    if ps is None:
        return ""
    try:
        for c in ps.net_connections(kind="tcp"):
            if c.status == ps.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                try:
                    return f"{ps.Process(c.pid).name()} (process id {c.pid})"
                except Exception:
                    return f"process id {c.pid}"
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ stop / start
def stop_everything(quiet: bool = False) -> Tuple[int, List[dict]]:
    """Stop every JARVIS process (any folder). Returns (number stopped, the ones that refused)."""
    if IS_WIN:
        try:   # a task instance ended this way is not restarted by Task Scheduler's retry rule
            subprocess.run(["schtasks", "/end", "/tn", "JARVIS"], capture_output=True, timeout=15,
                           stdin=subprocess.DEVNULL, creationflags=common.CREATE_NO_WINDOW)
        except Exception:
            pass
    procs = common.find_jarvis_processes()
    if not procs:
        return 0, []
    stopped, failed = common.stop_processes(procs)
    main_procs = [p for p in stopped if p.get("main", True)]
    if not quiet:
        kinds: Dict[str, int] = {}
        for p in main_procs:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        if kinds:
            ok("stopped the previous JARVIS (" + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + ")")
    time.sleep(0.5)
    return len(stopped), failed


def start_supervisor() -> subprocess.Popen:
    """Start the background service detached from this console, so closing the window
    (or this launcher exiting) never takes JARVIS down with it."""
    cmd = [common.python_exe(gui=True), os.path.join(ROOT, "boot", "jarvis_service.py"), "--from-launcher"]
    out = open(os.path.join(common.logs_dir(), "supervisor.out"), "ab", buffering=0)
    kwargs = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, close_fds=True)
    try:
        if IS_WIN:
            flags = common.DETACHED_PROCESS | common.CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(cmd, creationflags=flags | common.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:        # the console's job object does not allow breaking away
                return subprocess.Popen(cmd, creationflags=flags, **kwargs)
        return subprocess.Popen(cmd, start_new_session=True, **kwargs)
    finally:
        out.close()


# ------------------------------------------------------------------ wait + verify
def wait_ready(proc: Optional[subprocess.Popen] = None, started_after: Optional[float] = None,
               scale: float = 1.0) -> Tuple[bool, dict]:
    """Watch JARVIS come up, printing each stage as it is reached.

    Returns (True, ctx) when the HUD is on screen and connected to the brain, otherwise
    (False, ctx) with ctx["reason"] naming the stage that failed."""
    t_start = time.time()
    done: Dict[str, float] = {}
    ctx: dict = {"reason": "", "detail": "", "stages": done}
    sup_gone_since = 0.0
    last_note = t_start
    both_since = 0.0
    foreign_since = 0.0

    def fail(reason: str, detail: str = "") -> Tuple[bool, dict]:
        ctx["reason"], ctx["detail"] = reason, detail
        return False, ctx

    while True:
        now = time.time()
        waited = now - t_start
        sup_alive, sup = supervisor_state(started_after)
        ctx["sup"] = sup

        if sup_alive:
            sup_gone_since = 0.0
            if "supervisor" not in done:
                done["supervisor"] = waited
                ok(f"background service is running (process {sup.get('pid')})")
        else:
            exited = proc is not None and proc.poll() is not None
            if exited or "supervisor" in done:
                sup_gone_since = sup_gone_since or now
                if now - sup_gone_since > 6:
                    return fail("supervisor_exited", f"exit code {proc.poll() if proc is not None else '?'}")
            elif waited > T_SUPERVISOR * scale:
                return fail("supervisor_silent")

        problems = {p.get("id"): p.get("text", "") for p in (sup.get("problems") or [])} if sup_alive else {}
        ctx["problems"] = problems
        for key in ("stale_processes", "backend_crashloop", "overlay_crashloop"):
            if key in problems:
                return fail(key, problems[key])
        if "could not be installed" in problems.get("overlay_runtime", ""):
            return fail("overlay_runtime", problems["overlay_runtime"])

        shown, ov = overlay_state(sup) if sup_alive else (False, {})
        ctx["ov"] = ov
        if shown and "overlay" not in done:
            done["overlay"] = waited
            b = ov.get("bounds") or {}
            where = f" ({b.get('width')}x{b.get('height')} at {b.get('x')},{b.get('y')})" if b else ""
            ok("the HUD window is on screen" + where
               + (" [software rendering]" if ov.get("safeGraphics") else ""))

        bstate, bstatus = common.backend_status()
        ctx["backend"], ctx["status"] = bstate, bstatus or {}
        if bstate == "ok" and "backend" not in done:
            done["backend"] = waited
            n = int((bstatus or {}).get("neurons_active") or 0)
            ok(f"the brain is online (version {(bstatus or {}).get('version', '?')}, {n:,} active neurons)")
        if bstate == "foreign":
            foreign_since = foreign_since or now
            if now - foreign_since > 20:
                return fail("port_foreign", port_owner(common.backend_port()))
        else:
            foreign_since = 0.0

        connected = bstate == "ok" and int((bstatus or {}).get("overlay_clients") or 0) >= 1
        if shown and connected:
            done["connected"] = waited
            ok("the HUD is connected to the brain")
            return True, ctx

        if sup_alive and "overlay" not in done and waited - done.get("supervisor", 0) > T_OVERLAY * scale:
            return fail("overlay_not_visible")
        if "backend" not in done and waited > T_BACKEND * scale:
            return fail("backend_not_ready")
        if "overlay" in done and "backend" in done:
            both_since = both_since or now
            if now - both_since > T_CONNECT * scale:
                return fail("not_connected")

        if now - last_note >= 12:
            last_note = now
            waiting_for = ("the background service" if "supervisor" not in done else
                           "the HUD window" if "overlay" not in done else
                           "the brain (first start can take a minute)" if "backend" not in done else
                           "the HUD to connect")
            if "overlay runtime is being installed" in problems.get("overlay_runtime", ""):
                waiting_for = "the on-screen display to finish downloading"
            note(f"still waiting for {waiting_for} ... ({int(waited)} s)")
        time.sleep(0.5)


# ------------------------------------------------------------------ diagnosis
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))(?::\s*(.*))?$")
_BIND = re.compile(r"error while attempting to bind|Errno 10048|Errno 98\b|WinError 1001[03]|WinError 10048|"
                   r"[Aa]ddress already in use")


def final_exception(lines: List[str]) -> Tuple[str, str]:
    """(type, message) of the last Python exception in a log, e.g. the line
    "ModuleNotFoundError: No module named 'fastapi'" that ends a traceback."""
    for ln in reversed(lines):
        m = _EXC_LINE.match(ln.strip())
        if m:
            return m.group(1).split(".")[-1], (m.group(2) or "").strip()
    return "", ""


def backend_sign(lines: List[str]) -> Tuple[str, str]:
    """Classify why the backend died: (kind, detail), kind '' when unknown."""
    if _BIND.search("\n".join(lines[-25:])):
        return "port_in_use", ""
    name, msg = final_exception(lines)
    if name == "ModuleNotFoundError":
        m = re.search(r"'([^']+)'", msg)
        return "missing_module", (m.group(1) if m else msg)
    if name == "ImportError" and "DLL load failed" in msg:
        return "dll", msg
    if name == "JSONDecodeError":
        return "config", msg
    if name == "PermissionError" or "WinError 5]" in msg:
        return "permission", msg
    return "", ""


def last_error_line(lines: List[str]) -> str:
    for ln in reversed(lines):
        s = ln.strip()
        if s and not s.startswith("=====") and not s.startswith("[jarvis]"):
            return s[:300]
    return ""


def log_path(name: str) -> str:
    return os.path.join(common.logs_dir(), name)


def diagnose(ctx: dict) -> Tuple[str, str]:
    """(what went wrong, what to do) in plain words, from the failed stage and the logs."""
    reason, detail = ctx.get("reason", ""), ctx.get("detail", "")
    backend_log = common.tail(log_path("backend.log"), 60)
    overlay_log = common.tail(log_path("overlay-main.log"), 40) + common.tail(log_path("overlay.log"), 20)
    port = common.backend_port()

    if reason == "needs_install":
        return detail, "Double-click INSTALL_JARVIS.bat. It repairs what is missing and keeps what is already installed."
    if reason == "overlay_runtime":
        return ("The on-screen display (Electron) is not installed and could not be downloaded: " + detail,
                "Check the internet connection (a firewall or proxy may block github.com or registry.npmjs.org), "
                "then double-click START_JARVIS.bat again.")
    if reason in ("stale_processes", "cannot_stop"):
        return ("An older JARVIS is still running and could not be stopped" + (f" ({detail})" if detail else "") + ". "
                "It was most likely started with administrator rights.",
                "Sign out of Windows and sign in again (or restart the PC), then double-click START_JARVIS.bat.")
    if reason == "supervisor_exited" and detail.endswith(" 3"):
        return ("Another JARVIS background service is already running and holds the lock, but it does not belong "
                "to this folder or could not be identified.",
                "Sign out of Windows and sign in again (or restart the PC), then double-click START_JARVIS.bat.")
    if reason in ("supervisor_exited", "supervisor_silent"):
        last = last_error_line(common.tail(log_path("supervisor.log"), 30) + common.tail(log_path("supervisor.out"), 30))
        return ("The background service did not stay running" + (f" ({detail})" if detail else "")
                + (f". Last message: {last}" if last else "."),
                "Double-click INSTALL_JARVIS.bat to repair the installation. If it still fails, send the report below.")
    if reason == "port_foreign":
        who = f" It is used by {detail}." if detail else ""
        return (f"Port {port}, which JARVIS's brain uses, is taken by another program or another copy of JARVIS."
                + who,
                "Close that program (or restart the PC), then double-click START_JARVIS.bat again.")
    if reason in ("backend_crashloop", "backend_not_ready"):
        kind, value = backend_sign(backend_log)
        if kind == "missing_module":
            return (f"The brain cannot start because the Python package '{value}' is missing.",
                    "Double-click INSTALL_JARVIS.bat; it installs the missing packages.")
        if kind == "dll":
            return ("The brain cannot start because a system library is missing: " + value,
                    "Install the 'Microsoft Visual C++ Redistributable (x64)' and run INSTALL_JARVIS.bat again.")
        if kind == "port_in_use":
            owner = port_owner(port)
            return (f"The brain cannot open port {port} because another program is using it"
                    + (f": {owner}." if owner else "."),
                    "Close that program (or restart the PC), then double-click START_JARVIS.bat again.")
        if kind == "permission":
            return ("The brain is not allowed to write into this folder: " + value,
                    "Move the JARVIS folder to a normal location such as C:\\JARVIS (not Program Files, not a "
                    "protected or synced folder), run INSTALL_JARVIS.bat there, then START_JARVIS.bat.")
        if kind == "config":
            return ("A settings file is damaged (config\\jarvis.config.json or permissions.json): " + value,
                    "Restore the file from the original ZIP, then double-click START_JARVIS.bat again.")
        last = last_error_line(backend_log)
        if reason == "backend_not_ready" and not last:
            return (f"The brain is still starting after {T_BACKEND} seconds.",
                    "Wait a little and double-click START_JARVIS.bat again; if it keeps happening, send the report below.")
        return ("The brain keeps stopping right after it starts" + (f". Last message: {last}" if last else "."),
                "Double-click INSTALL_JARVIS.bat to repair the installation. If it still fails, send the report below.")
    if reason in ("overlay_crashloop", "overlay_not_visible"):
        last = last_error_line(overlay_log)
        child = ((ctx.get("sup") or {}).get("children") or {}).get("overlay") or {}
        if reason == "overlay_not_visible" and child.get("alive"):
            what = "The on-screen display started, but its window never finished loading"
        elif reason == "overlay_not_visible":
            what = "The on-screen display did not start"
        else:
            what = "The on-screen display keeps closing right after it starts"
        return (what + (f". Last message: {last}" if last else "."),
                "Double-click START_JARVIS_SAFE_GRAPHICS.bat, which starts it with software rendering "
                "(this helps with graphics-driver problems). If that fails too, send the report below.")
    if reason == "not_connected":
        return ("The HUD is on screen and the brain is running, but they did not connect to each other.",
                "Double-click START_JARVIS.bat once more. If it repeats, a security program may be blocking "
                f"local connections to 127.0.0.1:{port}; send the report below.")
    return ("JARVIS did not come up" + (f" ({reason})" if reason else "") + ".",
            "Double-click START_JARVIS.bat again; if it repeats, send the report below.")


def build_report(ctx: dict, what: str, todo: str) -> str:
    L: List[str] = []
    add = L.append
    add("JARVIS start-up report")
    add("=" * 70)
    add(f"time            : {common.now_iso()}")
    add(f"JARVIS version  : {common.version()}")
    add(f"folder          : {ROOT}")
    add(f"system          : {platform.platform()}")
    add(f"python          : {sys.version.split()[0]} ({sys.executable})")
    add(f"administrator   : {'yes' if common.is_admin() else 'no'}")
    add(f"auto-start task : {autostart_registered()}")
    add("")
    add("RESULT")
    add(f"  failed stage  : {ctx.get('reason') or '-'}  {ctx.get('detail') or ''}".rstrip())
    add(f"  what          : {what}")
    add(f"  to do         : {todo}")
    add(f"  stages reached: {ctx.get('stages') or {}}")
    add("")
    add("INSTALLATION")
    mf, mp = missing_files(), missing_packages()
    add(f"  missing files    : {mf or 'none'}")
    add(f"  missing packages : {mp or 'none'}")
    add(f"  overlay runtime  : {overlay_runtime.problems() or 'ready'}")
    add(f"  electron binary  : {common.electron_exe()}")
    add(f"  safe graphics    : {os.path.exists(common.safe_graphics_flag())}")
    for n in path_notes():
        add(f"  note: {n}")
    add("")
    add("RUNNING")
    add(f"  supervisor.json : {common.read_json(common.status_file())}")
    add(f"  overlay.json    : {common.read_json(common.overlay_state_file())}")
    bstate, bstatus = common.backend_status()
    add(f"  backend         : {bstate} {bstatus or ''}")
    add(f"  port {common.backend_port()} owner : {port_owner(common.backend_port()) or '-'}")
    for p in common.find_jarvis_processes():
        add(f"  process: {p['kind']:10s} pid {p['pid']:<7} {p.get('root') or ''}")
    for name, lines in (("supervisor.log", 40), ("supervisor.out", 15), ("backend.log", 50),
                        ("overlay-main.log", 50), ("overlay.log", 25)):
        add("")
        add(f"--- last lines of data/logs/{name} ---")
        got = common.tail(log_path(name), lines)
        L.extend(got or ["(empty or missing)"])
    add("")
    add(f"--- last lines of data/install.log ---")
    L.extend(common.tail(os.path.join(common.data_dir(), "install.log"), 25) or ["(empty or missing)"])
    return "\n".join(L) + "\n"


def report_failure(ctx: dict, open_report: bool) -> int:
    what, todo = diagnose(ctx)
    path = log_path("diagnostics.txt")
    try:
        with open(path, "w", encoding="utf-8", errors="backslashreplace") as fh:
            fh.write(build_report(ctx, what, todo))
        saved = True
    except OSError:
        saved = False
    say()
    say("  JARVIS did not start correctly.")
    say()
    say("  What went wrong:")
    for ln in wrap(what):
        say(ln)
    say()
    say("  What to do:")
    for ln in wrap(todo):
        say(ln)
    if saved:
        say()
        say("  A full report was saved here (send this file if you need help):")
        say("     " + path)
        if open_report and IS_WIN:
            try:
                os.startfile(path)            # opens in Notepad
            except Exception:
                pass
    say()
    return EXIT_NEEDS_INSTALL if ctx.get("reason") == "needs_install" else EXIT_FAILED


def report_success(status: dict, already: bool) -> int:
    token = common.read_token()
    if token:
        common.http_json(common.backend_url("/announce"), timeout=4, token=token, method="POST")
    say()
    say("  JARVIS is online." if not already else "  JARVIS is already running - I asked it to show itself.")
    say()
    say("     * The HUD is at the top-left of your main screen; the round WAKE button")
    say("       is in the bottom-right corner, just above the taskbar.")
    say('     * Say "wake up Jarvis", click WAKE, or press Ctrl+Alt+J.')
    say("     * JARVIS keeps running in the background. To quit it, right-click the")
    say("       JARVIS icon next to the clock and choose Quit JARVIS.")
    notes = []
    if status.get("llm_online") is False:
        notes.append("The language model (Ollama) is not running yet, so replies are basic. "
                     "INSTALL_JARVIS.bat installs and starts it.")
    vs = status.get("voice_state")
    if vs == "loading":
        notes.append("The voice is still loading; the wake phrase will work in a moment.")
    elif vs in ("unavailable", "failed"):
        notes.append("Voice is not available (speech packages or a microphone are missing); typing works. "
                     "INSTALL_JARVIS.bat repairs the speech packages.")
    if autostart_registered() is False:
        notes.append("JARVIS is not set to start at sign-in yet; run INSTALL_JARVIS.bat once to enable that.")
    notes += path_notes()
    if notes:
        say()
        for n in notes:
            for i, ln in enumerate(wrap(n, indent="       ", width=70)):
                say(("     - " + ln.strip()) if i == 0 else ln)
    say()
    return EXIT_OK


# ------------------------------------------------------------------ commands
def cmd_status() -> int:
    st = inspect()
    say(f"  background service : {'running' if st['supervisor'] else 'not running'}")
    say(f"  HUD window         : {'on screen' if st['overlay'] else 'not on screen'}")
    say(f"  brain              : {st['backend']}")
    say(f"  HUD <-> brain      : {'connected' if st['connected'] else 'not connected'}")
    for p in common.find_jarvis_processes():
        if p.get("main", True):
            say(f"  process            : {p['kind']} (pid {p['pid']}) {p.get('root') or ''}")
    return EXIT_OK if st["healthy"] else EXIT_FAILED


def cmd_stop() -> int:
    n, failed = stop_everything()
    if failed:
        bad("could not stop: " + ", ".join(f"{p['kind']} (pid {p['pid']}): {p['error']}" for p in failed))
        return EXIT_FAILED
    say("  JARVIS is stopped." if n else "  JARVIS was not running.")
    return EXIT_OK


def preflight(ctx: dict) -> bool:
    """Step 1. Returns False (with ctx filled in) when JARVIS cannot be started as it is."""
    mf = missing_files()
    if mf:
        ctx.update(reason="needs_install",
                   detail="This folder is not a complete copy of JARVIS (missing: " + ", ".join(mf[:4]) + "). "
                          "Extract the whole ZIP again.")
        return False
    mp = missing_packages()
    if mp:
        ctx.update(reason="needs_install",
                   detail="JARVIS's Python packages are not installed (missing: " + ", ".join(mp) + ").")
        return False
    ok("program files and Python packages are in place")
    left = overlay_runtime.problems()
    if left:
        note("the on-screen display is not installed yet - downloading it now (one time, about 120 MB)")
        left = overlay_runtime.ensure(log=lambda m: say("         " + m))
        if left:
            ctx.update(reason="overlay_runtime", detail=left[0])
            return False
    ok("the on-screen display (Electron, three.js, MediaPipe) is installed")
    return True


def cmd_start(restart: bool, open_report: bool) -> int:
    ctx: dict = {"reason": "", "detail": "", "stages": {}}
    say("  [1/3] Checking the installation")
    if not preflight(ctx):
        return report_failure(ctx, open_report)

    say("  [2/3] Looking for a running JARVIS")
    st = inspect()
    if st["healthy"] and not restart:
        ok("JARVIS is running and its HUD is connected")
        return report_success(st["status"], already=True)
    if st["supervisor"] and not restart:
        note("JARVIS is running but not fully up - waiting for it")
        good, wctx = wait_ready(scale=0.5)
        if good:
            return report_success(wctx.get("status") or {}, already=True)
        note("it did not come up by itself - restarting it")
    n, failed = stop_everything()
    if failed:
        ctx.update(reason="cannot_stop",
                   detail=", ".join(f"{p['kind']} pid {p['pid']}: {p['error']}" for p in failed))
        return report_failure(ctx, open_report)
    if not n:
        ok("nothing was running")

    say("  [3/3] Starting JARVIS")
    t0 = time.time()
    try:
        proc = start_supervisor()
    except Exception as e:
        ctx.update(reason="supervisor_exited", detail=f"could not be started: {e!r}")
        return report_failure(ctx, open_report)
    good, wctx = wait_ready(proc, started_after=t0)
    if good:
        return report_success(wctx.get("status") or {}, already=False)
    return report_failure(wctx, open_report)


def cmd_wait(open_report: bool) -> int:
    good, ctx = wait_ready()
    if good:
        return report_success(ctx.get("status") or {}, already=False)
    return report_failure(ctx, open_report)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    common.ensure_stdio()
    if "--help" in args or "-h" in args:
        say(__doc__)
        return EXIT_OK
    say()
    say(f"  J.A.R.V.I.S  {common.version()}")
    say(f"  {ROOT}")
    say()
    if "--status" in args:
        return cmd_status()
    if "--stop" in args:
        return cmd_stop()
    if "--wait" in args:
        return cmd_wait(open_report="--no-open" not in args)
    restart = "--restart" in args
    flag = common.safe_graphics_flag()
    if "--safe-graphics" in args:
        try:
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            with open(flag, "w", encoding="utf-8") as fh:
                fh.write("software rendering requested with START_JARVIS_SAFE_GRAPHICS.bat\n")
        except OSError:
            pass
        note("software rendering is ON for the on-screen display (START_JARVIS.bat keeps it;")
        note("run 'START_JARVIS.bat --normal-graphics' to switch back)")
        restart = True
    elif "--normal-graphics" in args:
        try:
            os.remove(flag)
        except OSError:
            pass
        restart = True
    return cmd_start(restart, open_report="--no-open" not in args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
