"""
boot/jarvis_service.py -- the always-on supervisor (requirement 4).

Keeps JARVIS alive in the background the whole time the PC is on, and heals itself:
  * single instance -- starting it twice (e.g. START_JARVIS.bat while it already runs) is a no-op,
  * launches the backend command bus (agent.server) and the Electron overlay,
  * restarts either one if it exits (capped exponential backoff),
  * watchdog: restarts the backend if it stops answering /health (hung, not just crashed),
  * makes sure the Ollama service is running (the language brain),
  * weekly maintenance: updates yt-dlp so web music keeps working as sites change,
  * tray icon with Quit (pystray), rotating log at data/service.log.

The logon task created by register_autostart.ps1 runs this with pythonw.exe (no console).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
IS_WIN = sys.platform.startswith("win")
LOCK_PORT = 8799
HEALTH_URL = "http://127.0.0.1:8765/health"
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
_lock_socket = None


def log(msg: str):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA, exist_ok=True)
        path = os.path.join(DATA, "service.log")
        if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".1")             # simple rotation
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def acquire_single_instance() -> bool:
    """Bind a loopback port as a lock; released automatically when this process exits."""
    global _lock_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_socket = s
    return True


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def python_exe(gui: bool = False) -> str:
    names = (["pythonw.exe", "python.exe"] if gui else ["python.exe"]) if IS_WIN else ["python"]
    for n in names:
        for folder in ("Scripts", "bin"):
            p = os.path.join(ROOT, ".venv", folder, n)
            if os.path.exists(p):
                return p
    return sys.executable


def electron_cmd():
    for n in (["electron.cmd", "electron"] if IS_WIN else ["electron"]):
        p = os.path.join(ROOT, "node_modules", ".bin", n)
        if os.path.exists(p):
            return [p, "."]
    return ["npx.cmd" if IS_WIN else "npx", "electron", "."]


def find_ollama():
    p = shutil.which("ollama")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    cand = os.path.join(local, "Programs", "Ollama", "ollama.exe") if local else ""
    return cand if cand and os.path.exists(cand) else None


class Supervisor:
    def __init__(self, with_overlay: bool = True):
        self.procs, self.backoff, self.started = {}, {}, {}
        self.running = True
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        self.env = env
        self.specs = {"backend": ([python_exe(), "-m", "agent.server"], ROOT)}
        if with_overlay:
            self.specs["overlay"] = (electron_cmd(), ROOT)
        self._health_fail = 0
        self._last_ollama_try = 0.0

    # ------------------------------------------------------------ processes #
    def _spawn(self, name):
        cmd, cwd = self.specs[name]
        try:
            self.procs[name] = subprocess.Popen(cmd, cwd=cwd, env=self.env, creationflags=NO_WINDOW)
            self.started[name] = time.time()
            log(f"started {name} (pid {self.procs[name].pid}): {' '.join(cmd)}")
        except Exception as e:
            log(f"failed to start {name}: {e!r}")
            self.procs[name] = None

    def _restart(self, name, reason):
        p = self.procs.get(name)
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        wait = self.backoff.get(name, 1.0)
        log(f"restarting {name} in {wait:.0f}s ({reason})")
        time.sleep(wait)
        self.backoff[name] = min(30.0, wait * 2)
        self._spawn(name)

    def start_all(self):
        for name in self.specs:
            self.backoff[name] = 1.0
            self._spawn(name)

    # --------------------------------------------------------------- health #
    def _check_backend_health(self):
        p = self.procs.get("backend")
        if not p or p.poll() is not None:
            return
        if time.time() - self.started.get("backend", 0) < 90:   # model loading grace period
            return
        if http_ok(HEALTH_URL):
            self._health_fail = 0
            self.backoff["backend"] = 1.0
            return
        self._health_fail += 1
        if self._health_fail >= 3:
            self._health_fail = 0
            self._restart("backend", "not answering /health")

    def _ensure_ollama(self):
        if http_ok(OLLAMA_URL, timeout=2.5) or time.time() - self._last_ollama_try < 300:
            return
        self._last_ollama_try = time.time()
        exe = find_ollama()
        if not exe:
            log("Ollama not found; JARVIS will use its offline fallback until it is installed")
            return
        try:
            subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("started the Ollama service")
        except Exception as e:
            log(f"could not start Ollama: {e!r}")

    def monitor(self):
        tick = 0
        while self.running:
            for name in list(self.specs):
                p = self.procs.get(name)
                if p is None or p.poll() is not None:
                    if self.running:
                        self._restart(name, f"exited with {p.poll() if p else 'n/a'}")
            if tick % 10 == 0:            # every ~20 s
                self._check_backend_health()
            if tick % 30 == 0:            # every ~60 s
                self._ensure_ollama()
            tick += 1
            time.sleep(2)

    def stop_all(self):
        self.running = False
        for p in self.procs.values():
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(1.5)
        for p in self.procs.values():
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        log("all processes stopped")


def weekly_maintenance():
    """Update yt-dlp once a week (sites change; an old yt-dlp stops resolving music)."""
    stamp = os.path.join(DATA, "maintenance.json")
    try:
        with open(stamp, encoding="utf-8") as fh:
            last = json.load(fh).get("yt_dlp", 0)
    except (OSError, ValueError):
        last = 0
    if time.time() - last < 7 * 86400:
        return
    try:
        subprocess.run([python_exe(), "-m", "pip", "install", "-q", "-U", "yt-dlp"],
                       creationflags=NO_WINDOW, timeout=600, capture_output=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            json.dump({"yt_dlp": time.time()}, fh)
        log("weekly maintenance: yt-dlp updated")
    except Exception as e:
        log(f"weekly maintenance skipped: {e!r}")


def run_tray(sup: "Supervisor"):
    """Optional system-tray icon (Quit). No-op if pystray/Pillow are missing."""
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), outline=(56, 225, 255, 255), width=4)
    d.ellipse((24, 24, 40, 40), fill=(56, 225, 255, 255))

    def _quit(icon, _item):
        sup.stop_all()
        icon.stop()
        os._exit(0)

    icon = pystray.Icon("JARVIS-service", img, "JARVIS (background service)",
                        menu=pystray.Menu(pystray.MenuItem("Quit JARVIS", _quit)))
    threading.Thread(target=icon.run, daemon=True).start()


def main():
    if not acquire_single_instance():
        log("JARVIS supervisor is already running; nothing to do")
        return
    sup = Supervisor(with_overlay="--no-overlay" not in sys.argv)

    def _sig(*_):
        log("signal received, shutting down")
        sup.stop_all()
        os._exit(0)

    for s in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, s):
            try:
                signal.signal(getattr(signal, s), _sig)
            except Exception:
                pass

    log(f"JARVIS supervisor starting (root={ROOT})")
    sup.start_all()
    threading.Thread(target=weekly_maintenance, daemon=True).start()
    run_tray(sup)
    try:
        sup.monitor()
    except KeyboardInterrupt:
        _sig()


if __name__ == "__main__":
    main()
