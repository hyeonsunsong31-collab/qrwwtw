"""
boot/jarvis_service.py -- the always-on supervisor (requirement 4).

Keeps JARVIS alive in the background the whole time the PC is on, and heals itself:

  * one supervisor per logon session (named mutex; starting it twice is harmless),
  * clears out JARVIS processes left behind by an earlier run before starting fresh ones,
  * starts the backend command bus (agent.server) and the holographic overlay -- the
    Electron binary is launched directly, never through npx / electron.cmd,
  * writes everything both of them print to data/logs/backend.log and overlay.log, so a
    start-up failure is never silent,
  * restarts either one if it exits (capped backoff) and the backend if it stops answering,
  * repairs a missing overlay runtime by itself (boot/overlay_runtime.py),
  * keeps data/supervisor.json up to date for START_JARVIS.bat's verification,
  * makes sure Ollama is running, updates yt-dlp weekly, shows a tray icon with Quit.

The logon task created by register_autostart.ps1 runs this with pythonw.exe, which has no
console and no stdout/stderr at all -- nothing here may depend on them.
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _fatal(text: str) -> None:
    """Record a start-up failure even when nothing else works (pythonw shows no errors)."""
    try:
        d = os.path.join(os.environ.get("JARVIS_DATA") or os.path.join(ROOT, "data"), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "supervisor.log"), "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except OSError:
        pass


try:
    import json
    import shutil
    import signal
    import subprocess
    import threading
    import time
    from typing import Callable, Dict, List, Optional

    from boot import common, overlay_runtime
except Exception:                                   # a broken or half-extracted install
    _fatal("JARVIS supervisor could not start:\n" + traceback.format_exc())
    raise

IS_WIN = common.IS_WIN
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
FAST_EXIT_SECONDS = 8            # a child that dies this quickly did not really start
CRASHLOOP_AFTER = 5              # this many quick deaths in a row = something is broken
HEALTH_GRACE_SECONDS = 120       # model loading time before /health is expected to answer

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{common.now_iso()}] {msg}"
    with _log_lock:
        try:
            path = os.path.join(common.logs_dir(), "supervisor.log")
            common.rotate(path)
            with open(path, "a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass


def find_ollama() -> Optional[str]:
    p = shutil.which("ollama")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    cand = os.path.join(local, "Programs", "Ollama", "ollama.exe") if local else ""
    return cand if cand and os.path.exists(cand) else None


def backend_command() -> Optional[List[str]]:
    return [common.python_exe(), "-m", "agent.server"]


def overlay_command() -> Optional[List[str]]:
    exe = common.electron_exe()
    if exe is None:
        return None
    cmd = [exe]
    if not IS_WIN and common.is_admin():
        cmd.append("--no-sandbox")              # Chromium refuses to run sandboxed as root
    cmd += [a for a in os.environ.get("JARVIS_ELECTRON_ARGS", "").split() if a]
    return cmd + [ROOT]


class Child:
    """One supervised process and its restart bookkeeping."""

    def __init__(self, name: str, command: Callable[[], Optional[List[str]]], env: Dict[str, str]):
        self.name, self.command, self.env = name, command, env
        self.log_path = os.path.join(common.logs_dir(), f"{name}.log")
        self.proc: Optional[subprocess.Popen] = None
        self.started = 0.0
        self.starts = 0
        self.last_exit: Optional[int] = None
        self.backoff = 1.0
        self.next_try = 0.0
        self.fast_failures = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def info(self) -> dict:
        return {"pid": self.proc.pid if self.proc else None, "alive": self.alive(),
                "starts": self.starts, "last_exit": self.last_exit,
                "started": self.started or None, "log": self.log_path}


class Supervisor:
    def __init__(self, with_overlay: bool = True, quiet: bool = False):
        self.running = True
        # START_JARVIS.bat is watching the first minutes and reports problems itself
        self.quiet_until = time.time() + 300 if quiet else 0.0
        self.started = time.time()
        self.problems: Dict[str, str] = {}
        self._warned: set = set()
        self._status_cache = ""
        self._last_status = 0.0
        self._health_fail = 0
        self._last_ollama_try = 0.0
        self._repairing = False
        self._last_repair = 0.0

        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:backslashreplace"     # logs are UTF-8 on every code page
        overlay_env = dict(os.environ)
        overlay_env.pop("ELECTRON_RUN_AS_NODE", None)          # would turn Electron into plain Node
        overlay_env["JARVIS_SUPERVISED"] = "1"                 # "exit and I will restart you"
        self.children: Dict[str, Child] = {"backend": Child("backend", backend_command, env)}
        if with_overlay:
            self.children["overlay"] = Child("overlay", overlay_command, overlay_env)

    # ------------------------------------------------------------- problems #
    def set_problem(self, key: str, text: str, popup: Optional[str] = None) -> None:
        if self.problems.get(key) != text:
            self.problems[key] = text
            log(f"PROBLEM [{key}] {text}")
        if popup and time.time() >= self.quiet_until and key not in self._warned:
            self._warned.add(key)
            common.message_box("JARVIS", popup)

    def clear_problem(self, key: str) -> None:
        if self.problems.pop(key, None) is not None:
            log(f"resolved [{key}]")

    # --------------------------------------------------------------- status #
    def write_status(self, state: str = "running", force: bool = False) -> None:
        body = {"pid": os.getpid(), "root": ROOT, "version": common.version(), "state": state,
                "started": self.started,
                "children": {n: c.info() for n, c in self.children.items()},
                "problems": [{"id": k, "text": v} for k, v in self.problems.items()]}
        key = json.dumps(body, sort_keys=True)
        now = time.time()
        if not force and key == self._status_cache and now - self._last_status < 5:
            return
        self._status_cache, self._last_status = key, now
        body["updated"] = now
        common.write_json_atomic(common.status_file(), body)

    # ------------------------------------------------------------ processes #
    def _spawn(self, child: Child) -> None:
        cmd = child.command()
        if cmd is None:
            return
        try:
            common.rotate(child.log_path)
            with open(child.log_path, "ab", buffering=0) as fh:
                fh.write(f"\n===== {common.now_iso()} starting {child.name} =====\n".encode("utf-8"))
                child.proc = subprocess.Popen(cmd, cwd=ROOT, env=child.env, stdin=subprocess.DEVNULL,
                                              stdout=fh, stderr=subprocess.STDOUT,
                                              creationflags=common.CREATE_NO_WINDOW)
            child.started = time.time()
            child.starts += 1
            child.last_exit = None
            log(f"started {child.name} (pid {child.proc.pid}): {' '.join(cmd)}")
        except Exception as e:
            child.proc = None
            child.next_try = time.time() + child.backoff
            child.backoff = min(30.0, child.backoff * 2)
            self.set_problem(f"{child.name}_start", f"could not start the {child.name}: {e!r}")

    def _kill(self, child: Child) -> None:
        p = child.proc
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass

    def _on_exit(self, child: Child) -> bool:
        """Book-keeping for a child that has exited. Returns False if JARVIS should quit."""
        code = child.proc.poll()
        lived = time.time() - child.started
        child.last_exit = code
        child.proc = None
        if child.name == "overlay" and code == common.QUIT_EXIT_CODE:
            log("the overlay was closed with 'Quit JARVIS'; shutting everything down")
            return False
        if lived < FAST_EXIT_SECONDS:
            child.fast_failures += 1
        else:
            child.fast_failures, child.backoff = 0, 1.0
            self.clear_problem(f"{child.name}_crashloop")
        last = " | ".join(ln for ln in common.tail(child.log_path, 4) if not ln.startswith("====="))[-400:]
        log(f"{child.name} exited with code {code} after {lived:.0f}s; restarting in {child.backoff:.0f}s"
            + (f" -- last output: {last}" if last else ""))
        if child.fast_failures >= CRASHLOOP_AFTER:
            what = "on-screen display (overlay)" if child.name == "overlay" else "brain (backend)"
            hint = ("\n\nIf it keeps happening, try START_JARVIS_SAFE_GRAPHICS.bat."
                    if child.name == "overlay" else "")
            self.set_problem(f"{child.name}_crashloop",
                             f"the {child.name} keeps exiting right after it starts (code {code}); see {child.log_path}",
                             popup=f"JARVIS's {what} keeps stopping right after it starts.\n\n"
                                   f"Double-click START_JARVIS.bat to see the reason.{hint}")
        child.next_try = time.time() + child.backoff
        child.backoff = min(60.0 if child.fast_failures >= CRASHLOOP_AFTER else 30.0, child.backoff * 2)
        return True

    # ------------------------------------------------------ overlay runtime #
    def _repair_overlay_runtime(self) -> None:
        try:
            log("the overlay runtime is incomplete: " + "; ".join(overlay_runtime.problems()))
            left = overlay_runtime.ensure(log=lambda m: log("overlay runtime: " + m))
            if left:
                self.set_problem("overlay_runtime", "the overlay runtime could not be installed: " + "; ".join(left),
                                 popup="JARVIS could not download its on-screen display (Electron).\n\n"
                                       "Check the internet connection, then double-click START_JARVIS.bat.\n\n"
                                       "Details: " + left[0][:300])
            else:
                self.clear_problem("overlay_runtime")
                log("overlay runtime repaired")
        except Exception:
            log("overlay runtime repair failed:\n" + traceback.format_exc())
        finally:
            self._last_repair = time.time()
            self._repairing = False

    def _overlay_ready(self) -> bool:
        """True when the overlay can be started; otherwise kicks off a background repair."""
        if not overlay_runtime.problems():
            return True
        if not self._repairing and time.time() - self._last_repair > 600:
            self._repairing = True
            self.set_problem("overlay_runtime", "the overlay runtime is being installed")
            threading.Thread(target=self._repair_overlay_runtime, daemon=True).start()
        return False

    # --------------------------------------------------------------- health #
    def _check_backend_health(self) -> None:
        child = self.children["backend"]
        if not child.alive() or time.time() - child.started < HEALTH_GRACE_SECONDS:
            return
        if common.backend_health() is not None:
            self._health_fail = 0
            return
        self._health_fail += 1
        if self._health_fail >= 3:
            self._health_fail = 0
            log("the backend is running but not answering /health; restarting it")
            self._kill(child)

    def _ensure_ollama(self) -> None:
        if time.time() - self._last_ollama_try < 300:
            return
        status, _ = common.http_json(OLLAMA_URL, timeout=2.5)
        if status == 200:
            return
        self._last_ollama_try = time.time()
        exe = find_ollama()
        if not exe:
            log("Ollama not found; JARVIS will use its offline fallback until it is installed")
            return
        try:
            subprocess.Popen([exe, "serve"], creationflags=common.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("started the Ollama service")
        except Exception as e:
            log(f"could not start Ollama: {e!r}")

    # ----------------------------------------------------------------- loop #
    def step(self) -> bool:
        """One pass over the children. Returns False when JARVIS should shut down."""
        now = time.time()
        for child in self.children.values():
            if child.proc is not None and child.proc.poll() is not None:
                if not self._on_exit(child):
                    return False
            if child.proc is None and now >= child.next_try:
                if child.name == "overlay" and not self._overlay_ready():
                    continue
                self._spawn(child)
        return True

    def monitor(self) -> None:
        tick = 0
        while self.running:
            try:
                if not self.step():
                    break
                if tick % 20 == 0:
                    self._check_backend_health()
                if tick % 60 == 0:
                    self._ensure_ollama()
                self.write_status()
            except Exception:
                log("supervisor loop error:\n" + traceback.format_exc())
            tick += 1
            time.sleep(1)

    def stop_all(self) -> None:
        self.running = False
        for child in self.children.values():
            p = child.proc
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        deadline = time.time() + 6
        while time.time() < deadline and any(c.alive() for c in self.children.values()):
            time.sleep(0.2)
        for child in self.children.values():
            p = child.proc
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        # anything of ours the children left behind (e.g. the music player)
        leftovers = [p for p in common.find_jarvis_processes() if p["kind"] != "supervisor"]
        if leftovers:
            common.stop_processes(leftovers, timeout=3)
        self.write_status("stopped", force=True)
        log("all processes stopped")


def clear_stale_processes(sup: Supervisor) -> None:
    """We hold the lock, so every other JARVIS process is a leftover: an orphaned backend
    still holding the port, an overlay whose supervisor died, a pre-1.3 supervisor."""
    stale = common.find_jarvis_processes()
    if not stale:
        return
    log("clearing leftover JARVIS processes: " +
        ", ".join(f"{p['kind']}#{p['pid']}" for p in stale))
    _stopped, failed = common.stop_processes(stale)
    if failed:
        sup.set_problem("stale_processes",
                        "could not stop an older JARVIS that is still running: " +
                        ", ".join(f"{p['kind']}#{p['pid']} ({p['error']})" for p in failed),
                        popup="An older JARVIS is still running and could not be stopped.\n\n"
                              "Sign out of Windows and back in (or restart the PC), "
                              "then JARVIS will start normally.")


def weekly_maintenance() -> None:
    """Update yt-dlp once a week (sites change; an old yt-dlp stops resolving music)."""
    stamp = os.path.join(common.data_dir(), "maintenance.json")
    last = (common.read_json(stamp, {}) or {}).get("yt_dlp", 0)
    if time.time() - last < 7 * 86400:
        return
    try:
        subprocess.run([common.python_exe(), "-m", "pip", "install", "-q", "-U", "yt-dlp"],
                       creationflags=common.CREATE_NO_WINDOW, timeout=600, capture_output=True,
                       stdin=subprocess.DEVNULL)
        common.write_json_atomic(stamp, {"yt_dlp": time.time()})
        log("weekly maintenance: yt-dlp updated")
    except Exception as e:
        log(f"weekly maintenance skipped: {e!r}")


def run_tray(sup: Supervisor) -> None:
    """Optional system-tray icon (Quit). No-op if pystray/Pillow are missing."""
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore

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
    except Exception as e:
        log(f"tray icon not shown: {e!r}")


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    had_stdio = sys.stdout is not None and sys.stderr is not None
    common.ensure_stdio("supervisor.out")
    lock = common.SingleInstance()
    if not lock.acquire():
        log("another JARVIS supervisor already holds the lock; this one exits")
        return 3
    sup = Supervisor(with_overlay="--no-overlay" not in args, quiet="--from-launcher" in args)

    def _sig(*_):
        log("stop signal received, shutting down")
        sup.stop_all()
        os._exit(0)

    for s in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, s):
            try:
                signal.signal(getattr(signal, s), _sig)
            except Exception:
                pass

    log(f"JARVIS supervisor {common.version()} starting: root={ROOT} python={sys.executable} "
        f"console={'yes' if had_stdio else 'no'} admin={'yes' if common.is_admin() else 'no'}")
    if not lock.legacy_port_held:
        log(f"note: port {common.LEGACY_LOCK_PORT} is unavailable (an older JARVIS may still be running)")
    clear_stale_processes(sup)
    sup.write_status(force=True)
    # not during start-up: pip competes with the brain and the HUD for a cold disk
    maintenance = threading.Timer(600, weekly_maintenance)
    maintenance.daemon = True
    maintenance.start()
    run_tray(sup)
    try:
        sup.monitor()
    except KeyboardInterrupt:
        pass
    sup.stop_all()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:
        _fatal(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] JARVIS supervisor crashed:\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
