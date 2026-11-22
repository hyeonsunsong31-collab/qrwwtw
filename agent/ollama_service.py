"""
agent/ollama_service.py -- finding, starting and repairing the local language engine (v1.4.3).

WHY: "cannot connect to the language engine" had several causes that all looked the same:

  * A system proxy (set by a VPN, security software, a company network, or a tool such as
    Fiddler). The HTTP client honoured it -- on Windows it reads the proxy from the Internet
    Options in the registry -- and sent JARVIS's requests for 127.0.0.1 to the proxy, so a
    perfectly healthy Ollama looked unreachable. Every request to a local engine now bypasses
    proxies (make_client()).
  * Ollama installed but not running (its tray app was closed or did not start at sign-in),
    or installed where JARVIS did not look. JARVIS now finds it (PATH, the usual folders, the
    uninstall entries in the registry), starts it (its tray app, or `ollama serve` with a log
    in data/logs/ollama.log), checks that it really answers, and says what failed if not.
  * Port 11434 blocked by Windows (a range reserved by Hyper-V / WSL / Docker) or held by
    another program. JARVIS runs Ollama on the next free port and remembers it.
  * Ollama's own OLLAMA_HOST setting pointing somewhere else: honoured.

Model problems (not downloaded, too big for the memory) are handled in llm_ollama.py.
Everything here blocks; the server calls it off the event loop.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Callable, List, Optional
from urllib.parse import urlsplit

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None

IS_WIN = sys.platform.startswith("win")
DEFAULT_PORT = 11434
DEFAULT_HOST = f"http://127.0.0.1:{DEFAULT_PORT}"
ALT_PORTS = list(range(11435, 11450))
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WIN else 0
DETACHED_PROCESS = 0x00000008 if IS_WIN else 0
CREATE_BREAKAWAY_FROM_JOB = 0x01000000 if IS_WIN else 0
INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"

# what keeps the language engine from answering, with the fix (English / Korean)
PROBLEMS = {
    "not_installed": dict(
        action="install",
        en="My language engine (Ollama) isn't installed on this PC, sir.",
        fix="Say or type 'install the language engine' and I'll install it (about 1 GB, a few minutes), "
            "or run INSTALL_JARVIS.bat.",
        ko="이 PC에 언어 엔진(Ollama)이 설치되어 있지 않습니다, sir.",
        fix_ko="'언어 엔진 설치해 줘'라고 말씀하시거나 입력하시면 설치하겠습니다(약 1GB, 몇 분). "
               "INSTALL_JARVIS.bat을 다시 실행하셔도 됩니다."),
    "start_failed": dict(
        action="check",
        en="My language engine (Ollama) is installed but won't start, sir ({detail}).",
        fix="I'll keep trying. Restarting the PC usually clears it; data\\logs\\ollama.log has the details.",
        ko="언어 엔진(Ollama)이 설치되어 있지만 시작되지 않습니다, sir ({detail}).",
        fix_ko="계속 다시 시도하겠습니다. 보통 PC를 다시 시작하면 해결되며, 자세한 내용은 data\\logs\\ollama.log에 있습니다."),
    "remote_down": dict(
        action="check",
        en="I can't reach the language engine at {detail}, sir.",
        fix="Check that the computer at that address is on and running Ollama, or set llm.host back to "
            "http://127.0.0.1:11434 in config\\jarvis.config.json.",
        ko="{detail}의 언어 엔진에 연결할 수 없습니다, sir.",
        fix_ko="그 주소의 컴퓨터가 켜져 있고 Ollama가 실행 중인지 확인하시거나, config\\jarvis.config.json의 "
               "llm.host를 http://127.0.0.1:11434로 되돌려 주세요."),
    "installing": dict(
        action=None,
        en="I'm installing my language engine (Ollama), sir ({detail}).",
        fix="This takes a few minutes; I'll tell you when it's ready.",
        ko="언어 엔진(Ollama)을 설치하는 중입니다, sir ({detail}).",
        fix_ko="몇 분 걸립니다. 준비되면 알려 드리겠습니다."),
    "install_failed": dict(
        action="install",
        en="I couldn't install my language engine, sir ({detail}).",
        fix="Check the internet connection and say 'install the language engine' again, or install Ollama "
            "from ollama.com.",
        ko="언어 엔진을 설치하지 못했습니다, sir ({detail}).",
        fix_ko="인터넷 연결을 확인하고 다시 '언어 엔진 설치해 줘'라고 말씀하시거나, ollama.com에서 Ollama를 설치해 주세요."),
}


def problem(code: str, detail: str = "") -> dict:
    p = PROBLEMS[code]
    fmt = lambda s: s.replace("{detail}", detail or "unknown reason")  # noqa: E731
    return {"code": code, "detail": detail or None, "action": p["action"], "message": fmt(p["en"]),
            "fix": fmt(p["fix"]), "message_ko": fmt(p["ko"]), "fix_ko": fmt(p["fix_ko"]), "since": time.time()}


# --------------------------------------------------------------------------- addresses
def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def port_of(url: str) -> int:
    try:
        return urlsplit(url).port or (443 if url.startswith("https") else DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def is_loopback(url: str) -> bool:
    return host_of(url) in LOOPBACK


def make_client(base_url: str, timeout: float = 10.0):
    """An HTTP client for the language engine. For an engine on this PC it never uses a
    proxy: with a system proxy set (VPN, security software, company network) requests for
    127.0.0.1 would otherwise go to the proxy and fail."""
    if httpx is None:
        raise RuntimeError("the httpx package is not installed")
    return httpx.Client(timeout=timeout, trust_env=not is_loopback(base_url))


def system_proxy() -> Optional[str]:
    """The proxy Windows (or the environment) would apply, if any -- for the report."""
    try:
        p = urllib.request.getproxies()
    except Exception:
        return None
    return p.get("http") or p.get("https") or p.get("all") or None


def parse_ollama_host(value: str) -> Optional[str]:
    """OLLAMA_HOST as Ollama reads it ("0.0.0.0", "127.0.0.1:11435", ":11435",
    "http://host:port") -> the URL JARVIS should connect to."""
    v = (value or "").strip().strip('"')
    if not v:
        return None
    scheme = "http"
    if "://" in v:
        scheme, v = v.split("://", 1)
    v = v.split("/", 1)[0]
    host, port = v, ""
    if v.startswith("["):                                   # [::1]:11434
        host, _, rest = v[1:].partition("]")
        port = rest.lstrip(":")
    elif v.count(":") == 1:
        host, port = v.split(":")
    if host in ("", "0.0.0.0", "::", "*", "[::]"):
        host = "127.0.0.1"                                  # "listen everywhere": connect locally
    if ":" in host:
        host = f"[{host}]"
    p = int(port) if port.isdigit() else (443 if scheme == "https" else DEFAULT_PORT)
    return f"{scheme}://{host}:{p}"


def env_ollama_host() -> Optional[str]:
    """OLLAMA_HOST from this process, or (Windows) from the user's / machine's saved
    environment -- set after JARVIS started, it is not in os.environ yet."""
    v = os.environ.get("OLLAMA_HOST")
    if not v and IS_WIN:
        try:
            import winreg  # type: ignore
            for hive, path in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                               (winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(hive, path) as k:
                        v = winreg.QueryValueEx(k, "OLLAMA_HOST")[0]
                    if v:
                        break
                except OSError:
                    continue
        except Exception:
            v = None
    return parse_ollama_host(v) if v else None


# --------------------------------------------------------------------------- finding Ollama
def _registry_install_dirs() -> List[str]:
    out: List[str] = []
    if not IS_WIN:
        return out
    try:
        import winreg  # type: ignore
    except Exception:
        return out
    roots = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                            name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if name.lower().startswith("ollama"):
                                loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                                if loc:
                                    out.append(loc)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def find_ollama() -> Optional[str]:
    """Path of the ollama executable, wherever it was installed."""
    exe = "ollama.exe" if IS_WIN else "ollama"
    found = shutil.which("ollama")
    if found:
        return found
    dirs = []
    for var, sub in (("LOCALAPPDATA", r"Programs\Ollama"), ("ProgramFiles", "Ollama"),
                     ("ProgramW6432", "Ollama"), ("ProgramFiles(x86)", "Ollama")):
        base = os.environ.get(var)
        if base:
            dirs.append(os.path.join(base, sub))
    dirs += _registry_install_dirs()
    if not IS_WIN:
        dirs += ["/usr/local/bin", "/usr/bin", os.path.expanduser("~/.local/bin")]
    for d in dirs:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return None


def find_tray_app(exe: Optional[str]) -> Optional[str]:
    """Ollama's Windows tray app ('ollama app.exe'), which runs and updates the server."""
    if not exe or not IS_WIN:
        return None
    p = os.path.join(os.path.dirname(exe), "ollama app.exe")
    return p if os.path.isfile(p) else None


def ollama_processes() -> List[dict]:
    out = []
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            if name.startswith("ollama"):
                out.append({"pid": p.info["pid"], "name": p.info.get("name"),
                            "cmdline": " ".join(p.info.get("cmdline") or [])[:200]})
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- ports
def port_state(port: int) -> str:
    """'free', 'in_use' (another program listens) or 'forbidden' (Windows reserved the port,
    e.g. for Hyper-V / WSL / Docker: binding fails with 'access denied')."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return "free"
    except OSError as e:
        code = getattr(e, "winerror", None) or e.errno
        return "forbidden" if code in (10013, 13) else "in_use"
    finally:
        s.close()


def port_owner(port: int) -> str:
    try:
        import psutil  # type: ignore
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                try:
                    return f"{psutil.Process(c.pid).name()} (pid {c.pid})"
                except Exception:
                    return f"pid {c.pid}"
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- probing
def probe(base_url: str, timeout: float = 4.0) -> dict:
    """Does Ollama answer at base_url? {'ok', 'version', 'error', 'status'}"""
    out = {"url": base_url, "ok": False, "version": None, "error": None, "status": None}
    if httpx is None:
        out["error"] = "the httpx package is not installed"
        return out
    try:
        with make_client(base_url, timeout) as c:
            r = c.get(base_url + "/api/version")
            out["status"] = r.status_code
            if r.status_code == 200:
                try:
                    v = r.json().get("version")
                except ValueError:
                    v = None
                if v:
                    out.update(ok=True, version=str(v))
                    return out
            r2 = c.get(base_url + "/api/tags")           # older Ollama has no /api/version
            try:
                if r2.status_code == 200 and "models" in r2.json():
                    out["ok"] = True
                    return out
            except ValueError:
                pass
            r3 = c.get(base_url + "/")                   # ... and says "Ollama is running" at /
            if r3.status_code == 200 and "ollama" in r3.text.lower():
                out["ok"] = True
                return out
            out["error"] = f"another program answers on this port (HTTP {r.status_code})"
    except Exception as e:
        name = type(e).__name__
        if "Connect" in name:
            out["error"] = "nothing is listening"
        elif "Timeout" in name:
            out["error"] = "no answer (timed out)"
        else:
            out["error"] = f"{name}: {e}"[:200]
    return out


# --------------------------------------------------------------------------- the service
class OllamaService:
    """Keeps the language engine reachable: finds Ollama, starts it, moves it off a blocked
    port, and explains what is wrong when none of that works."""
    START_WAIT_S = 30.0
    TRAY_WAIT_S = 20.0

    def __init__(self, base_url: str = DEFAULT_HOST, data_dir: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.configured = (base_url or DEFAULT_HOST).rstrip("/")
        self.data_dir = data_dir or os.environ.get("JARVIS_DATA") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        self.log = log or (lambda m: print("[jarvis] language engine: " + m, flush=True))
        self.exe: Optional[str] = None
        self.version: Optional[str] = None
        self.problem: Optional[dict] = None
        self.note = ""                       # e.g. "port 11434 is reserved by Windows; using 11435"
        self.started_by_jarvis = False
        self.attempts = 0                    # failed start attempts in a row (backs off retries)
        self.last_attempt = 0.0
        self.last_probe: dict = {}
        self.proxy = system_proxy()
        self.base = self._initial_base()
        self._logged: set = set()

    # ----------------------------------------------------------- helpers #
    def _say(self, msg: str):
        if msg not in self._logged:
            self._logged.add(msg)
            self.log(msg)

    def _port_file(self) -> str:
        return os.path.join(self.data_dir, "llm_port.json")

    def _saved_port(self) -> Optional[int]:
        try:
            with open(self._port_file(), encoding="utf-8") as fh:
                p = int(json.load(fh).get("port"))
            return p if 1024 < p < 65536 else None
        except (OSError, ValueError, TypeError):
            return None

    def _save_port(self, port: Optional[int]):
        try:
            if port is None or port == DEFAULT_PORT:
                if os.path.exists(self._port_file()):
                    os.remove(self._port_file())
                return
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._port_file(), "w", encoding="utf-8") as fh:
                json.dump({"port": port, "why": self.note, "at": time.time()}, fh)
        except OSError:
            pass

    def _initial_base(self) -> str:
        if self.configured != DEFAULT_HOST:
            return self.configured                       # the user pointed JARVIS somewhere on purpose
        env = env_ollama_host()
        if env:
            return env
        saved = self._saved_port()
        return f"http://127.0.0.1:{saved}" if saved else DEFAULT_HOST

    def candidates(self) -> List[str]:
        """Where Ollama might be answering right now, best guess first."""
        out = [self.base]
        for url in (env_ollama_host(), DEFAULT_HOST,
                    f"http://127.0.0.1:{self._saved_port()}" if self._saved_port() else None):
            if url and url not in out and (self.configured == DEFAULT_HOST or url == self.configured):
                out.append(url)
        return out

    def describe(self) -> dict:
        return {"url": self.base, "exe": self.exe, "version": self.version, "note": self.note or None,
                "proxy": self.proxy, "proxy_bypassed": bool(self.proxy and is_loopback(self.base)),
                "started_by_jarvis": self.started_by_jarvis, "problem": self.problem,
                "processes": ollama_processes()[:4]}

    # ------------------------------------------------------------- check #
    def find_running(self) -> bool:
        """Probe the likely addresses; adopt the one that answers."""
        for url in self.candidates():
            p = probe(url)
            self.last_probe = p
            if p["ok"]:
                if url != self.base:
                    self.log(f"Ollama answers at {url}")
                    self.base = url
                self.version = p["version"]
                self.problem = None
                return True
        return False

    # ------------------------------------------------------------- start #
    def ensure_running(self) -> bool:
        """Make Ollama answer. Returns True when it does (blocking; off the event loop)."""
        self.last_attempt = time.time()
        ok = self._ensure_running()
        self.attempts = 0 if ok else self.attempts + 1
        return ok

    def _ensure_running(self) -> bool:
        if self.find_running():
            return True
        if not is_loopback(self.base):
            self.problem = problem("remote_down", f"{self.base} ({self.last_probe.get('error')})")
            return False
        if self.problem is not None and self.problem["code"] == "installing":
            return False
        self.exe = find_ollama()
        if not self.exe:
            self.problem = problem("not_installed")
            self._say("Ollama is not installed (looked in PATH, the usual folders and the registry)")
            return False
        # the default port is free again (the program that held it is gone): go back to it
        if self.configured == DEFAULT_HOST and port_of(self.base) != DEFAULT_PORT and not env_ollama_host() \
                and port_state(DEFAULT_PORT) == "free":
            self.base, self.note = DEFAULT_HOST, ""
            self._save_port(None)
        # at sign-in Ollama's own tray app may be starting it right now: give it a moment
        if any("serve" in p["cmdline"] or "app" in (p["name"] or "").lower() for p in ollama_processes()):
            if self._wait(self.base, 15.0) or self.find_running():
                return True
        port = port_of(self.base)
        state = port_state(port)
        moved = False
        if state != "free":
            owner = port_owner(port)
            why = ("reserved by Windows (Hyper-V / WSL / Docker port range)" if state == "forbidden"
                   else f"in use by {owner or 'another program'}")
            alt = next((p for p in ALT_PORTS if port_state(p) == "free"), None)
            if alt is None:
                self.problem = problem("start_failed", f"port {port} is {why} and no other port is free")
                return False
            self.note = f"port {port} is {why}; the language engine runs on port {alt} instead"
            self.log(self.note)
            self.base, port, moved = f"http://127.0.0.1:{alt}", alt, True
        tray = find_tray_app(self.exe)
        if tray and not moved and port == DEFAULT_PORT and not env_ollama_host():
            self._start([tray], use_log=False)
            if self._wait(self.base, self.TRAY_WAIT_S):
                self.started_by_jarvis = True
                self.log("started Ollama (its tray app)")
                return True
        proc = self._start([self.exe, "serve"], port=port)
        if proc is not None and self._wait(self.base, self.START_WAIT_S, proc):
            self.started_by_jarvis = True
            self._save_port(port)
            self.log(f"started Ollama ('ollama serve' on port {port})")
            return True
        detail = self._log_tail() or (f"exit code {proc.poll()}" if proc is not None and proc.poll() is not None
                                      else "it did not answer within 30 seconds")
        if not moved and ("bind" in detail.lower() or "address" in detail.lower() or "forbidden" in detail.lower()):
            alt = next((p for p in ALT_PORTS if port_state(p) == "free"), None)      # bind failed after all
            if alt is not None:
                self.note = f"port {port} could not be used ({detail[:80]}); the language engine runs on port {alt}"
                self.base = f"http://127.0.0.1:{alt}"
                proc = self._start([self.exe, "serve"], port=alt)
                if proc is not None and self._wait(self.base, self.START_WAIT_S, proc):
                    self.started_by_jarvis = True
                    self._save_port(alt)
                    self.log(self.note)
                    return True
                detail = self._log_tail() or detail
        self.problem = problem("start_failed", detail[:200])
        self._say(f"Ollama would not start: {detail[:200]}")
        return False

    def _start(self, cmd: List[str], port: Optional[int] = None, use_log: bool = True):
        env = dict(os.environ)
        if port is not None:
            env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
        out = subprocess.DEVNULL
        if use_log:
            try:
                logs = os.path.join(self.data_dir, "logs")
                os.makedirs(logs, exist_ok=True)
                out = open(os.path.join(logs, "ollama.log"), "a", encoding="utf-8", errors="replace")
                out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} JARVIS starts: {' '.join(cmd)} "
                          f"(OLLAMA_HOST={env.get('OLLAMA_HOST', '')}) =====\n")
                out.flush()
            except OSError:
                out = subprocess.DEVNULL
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        proc = None
        try:
            for extra in ((CREATE_BREAKAWAY_FROM_JOB, 0) if IS_WIN else (0,)):   # outlive a JARVIS restart
                try:
                    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=subprocess.STDOUT if out is not subprocess.DEVNULL
                                            else subprocess.DEVNULL,
                                            cwd=os.path.dirname(cmd[0]) or None, creationflags=flags | extra)
                    break
                except OSError as e:
                    if extra == 0:
                        self.problem = problem("start_failed", f"{type(e).__name__}: {e}"[:200])
        finally:
            if out is not subprocess.DEVNULL:
                out.close()                              # the child has its own handle
        return proc

    def _wait(self, url: str, seconds: float, proc=None) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            p = probe(url, timeout=2.0)
            if p["ok"]:
                self.version = p["version"]
                self.problem = None
                return True
            if proc is not None and proc.poll() is not None:
                return False                             # it exited: the log says why
            time.sleep(0.5)
        return False

    def _log_tail(self) -> str:
        """The last meaningful line Ollama wrote (bind errors, missing libraries...)."""
        try:
            with open(os.path.join(self.data_dir, "logs", "ollama.log"), encoding="utf-8", errors="replace") as fh:
                lines = fh.read()[-6000:].splitlines()
        except OSError:
            return ""
        for ln in reversed(lines):
            low = ln.lower()
            if ln.startswith("====="):
                break
            if any(k in low for k in ("error", "panic", "bind", "denied", "failed", "forbidden", "unable")):
                return ln.strip()[-220:]
        return ""

    # ----------------------------------------------------------- install #
    def install(self, progress: Optional[Callable[[str], None]] = None) -> bool:
        """Download and install Ollama for this Windows user (no administrator rights needed),
        then start it. Blocking: minutes."""
        say = progress or (lambda m: None)
        if not IS_WIN:
            self.problem = problem("install_failed", "automatic installation is only available on Windows")
            return False
        self.problem = problem("installing", "downloading")
        dl_dir = os.path.join(os.path.dirname(self.data_dir), "tools", "downloads")
        os.makedirs(dl_dir, exist_ok=True)
        exe = os.path.join(dl_dir, "OllamaSetup.exe")
        try:
            say("downloading Ollama")
            req = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "JARVIS-installer"})
            with urllib.request.urlopen(req, timeout=60) as r, open(exe + ".part", "wb") as fh:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, 0.0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total and time.time() - last > 1.0:
                        last = time.time()
                        pct = int(100 * done / total)
                        self.problem = problem("installing", f"downloading, {pct}%")
                        say(f"downloading Ollama {pct}%")
            os.replace(exe + ".part", exe)
            self.problem = problem("installing", "running the installer")
            say("running the Ollama installer")
            r = subprocess.run([exe, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],   # per user, no admin
                               timeout=900, creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0:
                raise RuntimeError(f"the installer exited with code {r.returncode}")
        except Exception as e:
            self.problem = problem("install_failed", f"{type(e).__name__}: {e}"[:200])
            self.log(f"Ollama installation failed: {e!r}")
            return False
        self.problem = None
        self.log("Ollama installed")
        return self.ensure_running()
