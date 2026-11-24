"""
dev/procs.py -- running programs for the developer: the workspace terminal, the programs under
test and the servers of web products.

* run()          one command through the system shell (cmd.exe on Windows, bash elsewhere):
                 combined output, a time limit and a memory limit, and the WHOLE process tree
                 is killed when either is passed (a dev server started by `npm start` is a
                 grandchild of the shell, so killing the shell alone would leave it running)
* Background     a long-running process (a web server, a game, a desktop app being checked):
                 output to a log file and a ring buffer, alive(), stop()
* free_port(), wait_port(), http_get()  -- loopback helpers that never go through a proxy
"""
from __future__ import annotations

import collections
import locale
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil is in requirements.txt
    psutil = None

IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
MAX_KEEP_CHARS = 400_000          # output kept in memory per command (head + tail)


def shell_name() -> str:
    return "cmd.exe" if IS_WINDOWS else os.path.basename(shutil.which("bash") or "sh")


def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) or "latin-1"
        try:
            return b.decode(enc, errors="replace")
        except LookupError:
            return b.decode("latin-1", errors="replace")


def popen(command: str, cwd: str, env: Optional[Dict[str, str]] = None, stdin_pipe: bool = False,
          stdout=None) -> subprocess.Popen:
    """Start `command` through the system shell in its own process group."""
    stdout = subprocess.PIPE if stdout is None else stdout
    if IS_WINDOWS:
        comspec = os.environ.get("ComSpec") or "cmd.exe"
        # /s with the whole command in quotes: cmd strips the outer quotes and runs the rest
        # exactly as written (&&, ||, pipes and redirections all work)
        return subprocess.Popen(f'"{comspec}" /d /s /c "{command}"', cwd=cwd, env=env,
                                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    sh = shutil.which("bash") or "/bin/sh"
    return subprocess.Popen([sh, "-c", command], cwd=cwd, env=env,
                            stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                            stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True)


def tree_pids(pid: int) -> List[int]:
    if psutil is None:
        return [pid]
    try:
        p = psutil.Process(pid)
        return [pid] + [c.pid for c in p.children(recursive=True)]
    except Exception:
        return [pid]


def tree_rss_mb(pid: int) -> float:
    if psutil is None:
        return 0.0
    total = 0
    try:
        p = psutil.Process(pid)
        for q in [p] + p.children(recursive=True):
            try:
                total += q.memory_info().rss
            except Exception:
                pass
    except Exception:
        return 0.0
    return total / (1024 * 1024)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and everything it started."""
    if proc.poll() is not None and psutil is None:
        return
    pids = tree_pids(proc.pid)
    if psutil is not None:
        procs = []
        for pid in reversed(pids):
            try:
                q = psutil.Process(pid)
                q.kill()
                procs.append(q)
            except Exception:
                pass
        try:
            psutil.wait_procs(procs, timeout=3)
        except Exception:
            pass
    elif IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


class RunResult(dict):
    """{"command", "code", "output", "timed_out", "killed", "seconds"} -- a dict, so it can be
    logged and sent to the HUD as is."""

    @property
    def ok(self) -> bool:
        return self.get("code") == 0 and not self.get("killed")

    def tail(self, chars: int = 3000) -> str:
        out = self.get("output") or ""
        return out if len(out) <= chars else "...\n" + out[-chars:]


def run(command: str, cwd: str, env: Optional[Dict[str, str]] = None, timeout: float = 120.0,
        mem_limit_mb: float = 0.0, stdin_text: Optional[str] = None,
        on_line: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None) -> RunResult:
    """Run one command; never raises for the command's own failure."""
    t0 = time.time()
    try:
        proc = popen(command, cwd, env, stdin_pipe=stdin_text is not None)
    except Exception as e:
        return RunResult(command=command, code=None, output=f"could not start: {e}", timed_out=False,
                         killed="start failed", seconds=0.0)
    head: List[str] = []
    tail: collections.deque = collections.deque()
    size = {"head": 0, "tail": 0}

    def reader():
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = _decode(raw)
            if size["head"] < MAX_KEEP_CHARS // 2:
                head.append(line)
                size["head"] += len(line)
            else:
                tail.append(line)
                size["tail"] += len(line)
                while size["tail"] > MAX_KEEP_CHARS // 2 and tail:
                    size["tail"] -= len(tail.popleft())
            if on_line is not None:
                try:
                    on_line(line.rstrip("\r\n"))
                except Exception:
                    pass
        try:
            proc.stdout.close()
        except Exception:
            pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except Exception:
            pass
    killed = ""
    while proc.poll() is None:
        el = time.time() - t0
        if timeout and el > timeout:
            killed = f"time limit ({int(timeout)} s)"
            break
        if mem_limit_mb and int(el * 4) % 4 == 0 and tree_rss_mb(proc.pid) > mem_limit_mb:
            killed = f"memory limit ({int(mem_limit_mb)} MB)"
            break
        if should_stop is not None and should_stop():
            killed = "stopped"
            break
        time.sleep(0.05 if el < 2 else 0.25)
    if killed:
        kill_tree(proc)
    th.join(timeout=5)
    out = "".join(head)
    if tail:
        out += "\n... (output shortened) ...\n" + "".join(tail)
    return RunResult(command=command, code=proc.returncode if not killed else None, output=out,
                     timed_out=killed.startswith("time"), killed=killed or None,
                     seconds=round(time.time() - t0, 2))


class Background:
    """A long-running process (server, game, desktop app): output in a ring buffer + log file."""

    def __init__(self, command: str, cwd: str, env: Optional[Dict[str, str]] = None,
                 log_path: Optional[str] = None, name: str = ""):
        self.command, self.cwd, self.env, self.name = command, cwd, env, name or command[:40]
        self.log_path = log_path
        self.lines: collections.deque = collections.deque(maxlen=400)
        self.proc: Optional[subprocess.Popen] = None
        self.started = 0.0
        self._fh = None

    def start(self) -> "Background":
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            self._fh = open(self.log_path, "ab")
        self.proc = popen(self.command, self.cwd, self.env)
        self.started = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self):
        assert self.proc is not None and self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            self.lines.append(_decode(raw).rstrip("\r\n"))
            if self._fh is not None:
                try:
                    self._fh.write(raw)
                    self._fh.flush()
                except Exception:
                    pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc is not None else None

    def output(self, n: int = 80) -> str:
        return "\n".join(list(self.lines)[-n:])

    def stop(self) -> None:
        if self.proc is not None:
            kill_tree(self.proc)
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


# ------------------------------------------------------------------ network helpers
def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port: int, host: str = "127.0.0.1", timeout: float = 30.0,
              alive: Optional[Callable[[], bool]] = None) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port, host):
            return True
        if alive is not None and not alive():
            return False
        time.sleep(0.25)
    return False


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, timeout: float = 10.0, method: str = "GET", data: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None) -> Dict:
    """(status, body text, headers) for a URL on this PC -- straight, never through a proxy."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            body = r.read(2_000_000)
            return {"status": r.status, "body": body.decode("utf-8", errors="replace"),
                    "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        body = e.read(200_000) if hasattr(e, "read") else b""
        return {"status": e.code, "body": body.decode("utf-8", errors="replace"), "headers": dict(e.headers or {})}
    except Exception as e:
        return {"status": None, "body": "", "error": f"{type(e).__name__}: {e}", "headers": {}}


def lan_ip() -> Optional[str]:
    """This PC's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None
