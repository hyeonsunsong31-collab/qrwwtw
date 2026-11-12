"""
boot/common.py -- plumbing shared by the supervisor (jarvis_service.py) and the START
launcher (launcher.py). Standard library only; psutil is used when it is installed.

Everything that decides "is JARVIS installed / running / visible?" lives here so the two
programs can never disagree:

  * paths and versions            ROOT, data_dir(), logs_dir(), version()
  * never-crash console output    ensure_stdio()  (pythonw.exe has no stdout/stderr at all)
  * small files                   read_json / write_json_atomic / tail / rotate
  * the backend                   backend_port(), http_json(), read_token()
  * the overlay runtime           electron_exe(), overlay_runtime_problems()
  * one supervisor at a time      SingleInstance (named mutex on Windows, flock elsewhere)
  * JARVIS's own processes        find_jarvis_processes() / stop_processes()
  * a visible last resort         message_box()
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WIN = sys.platform.startswith("win")
DEFAULT_PORT = 8765
LEGACY_LOCK_PORT = 8799          # what JARVIS 1.0-1.2 supervisors used as their lock
QUIT_EXIT_CODE = 42              # the overlay exits with this when the user chose "Quit JARVIS"

# Windows process-creation flags (0 elsewhere, so call sites stay portable)
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
DETACHED_PROCESS = 0x00000008 if IS_WIN else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WIN else 0
CREATE_BREAKAWAY_FROM_JOB = 0x01000000 if IS_WIN else 0


# --------------------------------------------------------------------------- paths
def data_dir() -> str:
    return os.environ.get("JARVIS_DATA") or os.path.join(ROOT, "data")


def logs_dir() -> str:
    d = os.path.join(data_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def config_path() -> str:
    return os.environ.get("JARVIS_CONFIG") or os.path.join(ROOT, "config", "jarvis.config.json")


def status_file() -> str:
    return os.path.join(data_dir(), "supervisor.json")


def overlay_state_file() -> str:
    return os.path.join(data_dir(), "overlay.json")


def safe_graphics_flag() -> str:
    return os.path.join(data_dir(), "overlay.safe-graphics")


def version() -> str:
    pkg = read_json(os.path.join(ROOT, "package.json"), {}) or {}
    return str(pkg.get("version") or "unknown")


# --------------------------------------------------------------------------- stdio
def ensure_stdio(log_name: Optional[str] = None) -> None:
    """Make sys.stdout / sys.stderr usable no matter how the process was started.

    Under pythonw.exe (how the logon task runs the supervisor) both are None, so any
    library that touches them -- uvicorn calls sys.stdout.isatty() while configuring its
    logging -- dies with AttributeError before a single line is logged. Point missing
    streams at a log file (or the null device) instead.
    """
    target = None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        if target is None:
            try:
                path = os.path.join(logs_dir(), log_name) if log_name else os.devnull
                target = open(path, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
            except OSError:
                target = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, target)
    for name in ("stdout", "stderr"):        # never raise on a character the console can't show
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- files
def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str, obj) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def tail(path: str, lines: int = 30, max_bytes: int = 64 * 1024) -> List[str]:
    """Last `lines` lines of a text file ([] if it does not exist)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return [ln for ln in text.split("\n") if ln.strip()][-lines:]


def rotate(path: str, max_bytes: int = 2 * 1024 * 1024) -> None:
    """Keep one previous generation once a log grows past max_bytes."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


# --------------------------------------------------------------------------- backend
_port_cache: Optional[int] = None


def backend_port() -> int:
    global _port_cache
    if _port_cache is None:
        cfg = read_json(config_path(), {}) or {}
        try:
            _port_cache = int((cfg.get("server") or {}).get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            _port_cache = DEFAULT_PORT
    return _port_cache


def backend_url(path: str = "") -> str:
    return f"http://127.0.0.1:{backend_port()}{path}"


def read_token() -> str:
    try:
        with open(os.path.join(data_dir(), "session.token"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback: never via a proxy


def http_json(url: str, timeout: float = 4.0, token: str = "", method: str = "GET") -> Tuple[int, Optional[dict]]:
    """(status, json) for a loopback request; status 0 = nothing is listening / no answer."""
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if token:
        req.add_header("x-jarvis-token", token)
    try:
        with _opener.open(req, timeout=timeout) as r:
            body = r.read(1024 * 1024)
            try:
                return r.status, json.loads(body.decode("utf-8"))
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def backend_health() -> Optional[dict]:
    status, body = http_json(backend_url("/health"), timeout=3.0)
    return body if status == 200 and isinstance(body, dict) else None


def backend_status() -> Tuple[str, Optional[dict]]:
    """('ok', status) when the backend of THIS folder answers; otherwise why not.

    'down'     nothing answers on the port
    'foreign'  something answers but rejects this folder's session token -- another copy of
               JARVIS (or a different program) owns the port
    'no-token' it answers but this folder has no session token yet
    """
    if backend_health() is None:
        return "down", None
    token = read_token()
    if not token:
        return "no-token", None
    status, body = http_json(backend_url("/status"), timeout=4.0, token=token)
    if status == 200 and isinstance(body, dict):
        return "ok", body
    if status in (401, 403):
        return "foreign", None
    return "down", None


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


# --------------------------------------------------------------------------- executables
def python_exe(gui: bool = False) -> str:
    """The virtual environment's interpreter (pythonw.exe = no console window)."""
    names = (["pythonw.exe", "python.exe"] if gui else ["python.exe"]) if IS_WIN else ["python"]
    for n in names:
        for folder in ("Scripts", "bin"):
            p = os.path.join(ROOT, ".venv", folder, n)
            if os.path.exists(p):
                return p
    if IS_WIN and gui:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return sys.executable


def electron_dir() -> str:
    return os.path.join(ROOT, "node_modules", "electron")


def electron_exe() -> Optional[str]:
    """Full path of the Electron binary, or None if the overlay runtime is not installed.

    The binary is started directly -- not through node_modules/.bin/electron.cmd or npx,
    which need Node.js on PATH, hide every error, and (npx) wait forever on a prompt.
    """
    base = electron_dir()
    names = []
    try:
        with open(os.path.join(base, "path.txt"), encoding="utf-8") as fh:
            names.append(fh.read().strip())
    except OSError:
        pass
    names += ["electron.exe" if IS_WIN else "electron", "Electron.app/Contents/MacOS/Electron"]
    for n in names:
        if n:
            p = os.path.join(base, "dist", *n.split("/"))
            if os.path.isfile(p):
                return p
    return None


def overlay_runtime_problems() -> List[str]:
    """What the holographic overlay still needs; an empty list means it can start."""
    problems = []
    if electron_exe() is None:
        problems.append("the Electron runtime (node_modules/electron/dist) is missing")
    if not os.path.isfile(os.path.join(ROOT, "node_modules", "three", "build", "three.module.js")):
        problems.append("three.js (node_modules/three) is missing")
    if not os.path.isfile(os.path.join(ROOT, "node_modules", "@mediapipe", "tasks-vision", "vision_bundle.mjs")):
        problems.append("MediaPipe hand tracking (node_modules/@mediapipe/tasks-vision) is missing")
    return problems


# --------------------------------------------------------------------------- single instance
class SingleInstance:
    """One JARVIS supervisor per logon session.

    Windows: a named mutex -- it cannot collide with other software and is released by the
    kernel the moment the process dies. (JARVIS 1.0-1.2 locked a TCP port instead; on PCs
    where that port was reserved or taken, the supervisor decided it was "already running"
    and quit without a trace.) Elsewhere: an flock on data/supervisor.lock.
    """

    def __init__(self, name: str = "JARVIS.Supervisor"):
        self.name = name
        self._handle = None
        self._fh = None
        self._legacy = None

    def acquire(self) -> bool:
        ok = self._acquire_windows() if IS_WIN else self._acquire_posix()
        if ok:
            self._hold_legacy_port()
        return ok

    def _acquire_windows(self) -> bool:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.set_last_error(0)
            handle = k32.CreateMutexW(None, False, "Local\\" + self.name)
            err = ctypes.get_last_error()
            if not handle:
                return True               # could not even create it: do not block start-up
            if err == 183:                # ERROR_ALREADY_EXISTS -> another supervisor owns it
                k32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except Exception:
            return True

    def _acquire_posix(self) -> bool:
        try:
            import fcntl
            os.makedirs(data_dir(), exist_ok=True)
            fh = open(os.path.join(data_dir(), "supervisor.lock"), "a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            self._fh = fh
            return True
        except Exception:
            return True

    def _hold_legacy_port(self) -> None:
        """Also hold the old lock port so a 1.0-1.2 supervisor started later (a stale logon
        task, a second copy of the folder) sees JARVIS as running and exits. Best effort."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", LEGACY_LOCK_PORT))
            s.listen(1)
            self._legacy = s
        except OSError:
            self._legacy = None

    @property
    def legacy_port_held(self) -> bool:
        return self._legacy is not None


# --------------------------------------------------------------------------- processes
def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    ps = _psutil()
    if ps is not None:
        try:
            if not ps.pid_exists(pid):
                return False
        except Exception:
            return False
        if not IS_WIN:
            try:
                return ps.Process(pid).status() != ps.STATUS_ZOMBIE
            except Exception:
                return False
        return True
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259          # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _norm(path: str) -> str:
    p = os.path.normpath(path).replace("\\", "/")
    return p.lower() if IS_WIN else p


def is_jarvis_root(path: str) -> bool:
    return bool(path) and os.path.isfile(os.path.join(path, "boot", "jarvis_service.py")) \
        and os.path.isfile(os.path.join(path, "overlay", "main.js"))


def same_path(a: str, b: str) -> bool:
    try:
        return _norm(os.path.realpath(a)) == _norm(os.path.realpath(b))
    except OSError:
        return _norm(a) == _norm(b)


def classify_process(name: str, exe: str, cmdline: List[str], cwd: str) -> Optional[Dict[str, str]]:
    """Decide whether a process is one of JARVIS's own, from facts only JARVIS processes have.

    Returns {"kind": supervisor|backend|overlay|music, "root": folder} or None. Nothing is
    ever matched by name alone: a supervisor must be running <root>/boot/jarvis_service.py,
    a backend must be `-m agent.server` inside a JARVIS folder, an overlay must be the
    Electron binary that lives inside a JARVIS folder, and a music player must carry the
    title JARVIS gives its own player.
    """
    lname = (name or "").lower()
    args = [a for a in (cmdline or []) if a]
    if lname.startswith("python"):
        for a in args[1:]:
            if _norm(a).endswith("boot/jarvis_service.py"):
                full = a if os.path.isabs(a) else os.path.join(cwd or "", a)
                root = os.path.dirname(os.path.dirname(os.path.abspath(full)))
                if is_jarvis_root(root):
                    return {"kind": "supervisor", "root": root}
        for i, a in enumerate(args[:-1]):
            if a == "-m" and args[i + 1] == "agent.server" and is_jarvis_root(cwd or ""):
                return {"kind": "backend", "root": cwd}
        return None
    if lname.startswith("electron"):
        raw = os.path.normpath(exe or (args[0] if args else "")).replace("\\", "/")
        m = re.search(r"/node_modules/electron/dist/", raw, re.IGNORECASE if IS_WIN else 0)
        if m:
            root = os.path.normpath(raw[: m.start()])
            if is_jarvis_root(root):
                return {"kind": "overlay", "root": root}
        return None
    if lname.startswith(("ffplay", "mpv")) and any("JARVIS-music" in a for a in args):
        return {"kind": "music", "root": ""}
    return None


def my_lineage() -> List[int]:
    """This process and its ancestors (the venv's python.exe is a stub that runs the real
    interpreter as a child, so "me" is always at least two processes on Windows)."""
    pids = [os.getpid()]
    ps = _psutil()
    if ps is None:
        try:
            pids.append(os.getppid())
        except Exception:
            pass
        return pids
    try:
        for parent in ps.Process().parents():
            pids.append(parent.pid)
    except Exception:
        pass
    return pids


def find_jarvis_processes(exclude: Optional[List[int]] = None) -> List[dict]:
    """Every running JARVIS process on this PC (any folder), except this process's lineage."""
    ps = _psutil()
    if ps is None:
        return []
    skip = set(exclude if exclude is not None else my_lineage())
    found = []
    for proc in ps.process_iter(["pid", "name"]):
        pid = proc.info.get("pid")
        name = proc.info.get("name") or ""
        if pid in skip or not name.lower().startswith(("python", "electron", "ffplay", "mpv")):
            continue
        exe = cwd = ""
        cmdline: List[str] = []
        denied = False
        try:
            exe = proc.exe() or ""
        except Exception:
            denied = True
        try:
            cmdline = proc.cmdline() or []
        except Exception:
            denied = True
        try:
            cwd = proc.cwd() or ""
        except Exception:
            pass
        info = classify_process(name, exe, cmdline, cwd)
        if info is None:
            continue
        info.update({"pid": pid, "name": name, "exe": exe, "cmdline": cmdline,
                     "main": "--type=" not in " ".join(cmdline), "denied": denied})
        found.append(info)
    return found


_STOP_ORDER = {"supervisor": 0, "backend": 1, "overlay": 1, "music": 2}


def stop_processes(procs: List[dict], timeout: float = 6.0) -> Tuple[List[dict], List[dict]]:
    """Stop the given JARVIS processes (supervisors first, so nothing gets restarted behind
    our back). Returns (stopped, failed); failed entries carry an "error" explanation."""
    ps = _psutil()
    stopped: List[dict] = []
    failed: List[dict] = []
    if ps is None:
        return stopped, [dict(p, error="psutil is not installed") for p in procs]
    for rank in sorted(set(_STOP_ORDER.values())):
        batch = []
        for info in procs:
            if _STOP_ORDER.get(info["kind"], 1) != rank:
                continue
            try:
                p = ps.Process(info["pid"])
                p.terminate()
                batch.append((p, info))
            except ps.NoSuchProcess:
                stopped.append(info)
            except ps.AccessDenied:
                failed.append(dict(info, error="access denied (it was started as administrator)"))
            except Exception as e:
                failed.append(dict(info, error=repr(e)))
        if not batch:
            continue
        _gone, alive = ps.wait_procs([p for p, _ in batch], timeout=timeout)
        alive_pids = {p.pid for p in alive}
        for p, info in batch:
            if p.pid not in alive_pids:
                stopped.append(info)
                continue
            try:
                p.kill()
                p.wait(timeout=3)
                stopped.append(info)
            except ps.NoSuchProcess:
                stopped.append(info)
            except Exception as e:
                failed.append(dict(info, error=repr(e)))
    return stopped, failed


# --------------------------------------------------------------------------- last resort
def message_box(title: str, text: str) -> None:
    """A visible warning from a windowless process (Windows only; returns immediately)."""
    if not IS_WIN:
        return

    def _show():
        try:
            import ctypes
            # MB_OK | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x0 | 0x30 | 0x10000 | 0x40000)
        except Exception:
            pass

    import threading
    threading.Thread(target=_show, daemon=True).start()


def is_admin() -> bool:
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def wait_until(predicate, timeout: float, interval: float = 0.5):
    """Poll predicate() until it returns something truthy; that value, or None on timeout."""
    end = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() >= end:
            return None
        time.sleep(interval)
