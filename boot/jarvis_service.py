"""
boot/jarvis_service.py — the always-on supervisor (requirement 4).

Keeps JARVIS alive in the background the whole time the PC is on:
  * launches the backend command bus (agent.server) and the Electron overlay,
  * restarts either one automatically if it crashes (capped exponential backoff),
  * provides a tray icon (Quit) when pystray is available,
  * logs to data/service.log.

This is the program the boot auto-start task runs (see register_autostart.ps1). Running it
directly (START_JARVIS.bat) starts JARVIS immediately for this session too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WIN = sys.platform.startswith("win")


def log(msg: str):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        with open(os.path.join(ROOT, "data", "service.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def python_exe() -> str:
    cand = [os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
            os.path.join(ROOT, ".venv", "bin", "python")]
    for c in cand:
        if os.path.exists(c):
            return c
    return sys.executable


def electron_cmd():
    names = ["electron.cmd", "electron"] if IS_WIN else ["electron"]
    for n in names:
        p = os.path.join(ROOT, "node_modules", ".bin", n)
        if os.path.exists(p):
            return [p, "."]
    # fall back to npx (resolves electron from node_modules)
    npx = "npx.cmd" if IS_WIN else "npx"
    return [npx, "electron", "."]


class Supervisor:
    def __init__(self, with_overlay=True):
        self.with_overlay = with_overlay
        self.procs = {}       # name -> Popen
        self.backoff = {}     # name -> seconds
        self.running = True
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        self.env = env
        self.specs = {"backend": ([python_exe(), "-m", "agent.server"], ROOT)}
        if with_overlay:
            self.specs["overlay"] = (electron_cmd(), ROOT)

    def _spawn(self, name):
        cmd, cwd = self.specs[name]
        try:
            flags = 0
            if IS_WIN:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.procs[name] = subprocess.Popen(cmd, cwd=cwd, env=self.env, creationflags=flags)
            log(f"started {name}: {' '.join(cmd)} (pid {self.procs[name].pid})")
        except Exception as e:
            log(f"failed to start {name}: {e!r}")
            self.procs[name] = None

    def start_all(self):
        for name in self.specs:
            self._spawn(name)
            self.backoff[name] = 1.0

    def monitor(self):
        while self.running:
            for name in list(self.specs):
                p = self.procs.get(name)
                if p is None or p.poll() is not None:
                    if not self.running:
                        break
                    wait = self.backoff.get(name, 1.0)
                    log(f"{name} is down (exit {p.poll() if p else 'n/a'}); restarting in {wait:.0f}s")
                    time.sleep(wait)
                    self.backoff[name] = min(30.0, wait * 2)
                    self._spawn(name)
                else:
                    self.backoff[name] = 1.0
            time.sleep(2)

    def stop_all(self):
        self.running = False
        for name, p in self.procs.items():
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(1.5)
        for name, p in self.procs.items():
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        log("all processes stopped")


def run_tray(sup: "Supervisor"):
    """Optional system-tray icon (Quit). No-op if pystray/Pillow are missing."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), outline=(56, 225, 255, 255), width=4)
    d.ellipse((24, 24, 40, 40), fill=(56, 225, 255, 255))

    def _quit(icon, item):
        sup.stop_all()
        icon.stop()
        os._exit(0)

    icon = pystray.Icon("JARVIS", img, "JARVIS",
                        menu=pystray.Menu(pystray.MenuItem("Quit JARVIS", _quit)))
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main():
    with_overlay = "--no-overlay" not in sys.argv
    sup = Supervisor(with_overlay=with_overlay)

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

    log(f"JARVIS supervisor starting (root={ROOT}, overlay={with_overlay})")
    sup.start_all()
    run_tray(sup)
    try:
        sup.monitor()
    except KeyboardInterrupt:
        _sig()


if __name__ == "__main__":
    main()
