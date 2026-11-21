"""
tests/test_startup.py -- the brain (backend) starts quickly and cannot be held up (v1.4.1).

    python tests/test_startup.py            (or: python -m pytest tests/)

Pins down what made "still waiting for the brain" last minutes:
  * another program on port 8765 (a popular default: AnkiConnect, websocket tools) -- the
    brain now claims the next free port and publishes it in data/runtime.json, which the
    HUD, the launcher and the supervisor read,
  * the start-up phase is visible while it runs, with a timing line in backend.log,
  * /status never waits for Ollama (it used to call it on the event loop),
  * the supervisor never kills a brain that is still starting, counts starts that never got
    ready as failures however long they took, and only restarts a brain that hung,
  * the launcher shows the phase, the port it moved to and restarts as they happen,
  * the health check no longer crashes on Korean Windows (cp949 has no em dash),
  * wake-word detection runs off the event loop and skips ahead when it falls behind.
No network, audio or models needed.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from boot import common  # noqa: E402

PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sandbox:
    """Throwaway data folder + config, restored afterwards."""

    def __init__(self, port: int = 0, llm_host: str = "http://127.0.0.1:9"):
        self.dir = tempfile.mkdtemp(prefix="jarvis_start_")
        self.port = port or free_port()
        self.llm_host = llm_host
        self.keys = ("JARVIS_DATA", "JARVIS_CONFIG", "JARVIS_PERMISSIONS")
        self.saved = {k: os.environ.get(k) for k in self.keys}

    def __enter__(self):
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
        cfg["neural_core"]["active_neurons"] = 20000
        cfg["llm"]["host"] = self.llm_host
        cfg["voice"]["enabled"] = False
        cfg["server"]["port"] = self.port
        self.cfg_path = os.path.join(self.dir, "jarvis.config.json")
        json.dump(cfg, open(self.cfg_path, "w", encoding="utf-8"))
        self.perm_path = os.path.join(self.dir, "permissions.json")
        shutil.copy(os.path.join(ROOT, "config", "permissions.json"), self.perm_path)
        os.environ["JARVIS_DATA"] = os.path.join(self.dir, "data")
        os.environ["JARVIS_CONFIG"] = self.cfg_path
        os.environ["JARVIS_PERMISSIONS"] = self.perm_path
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


def _start_backend(box):
    log = open(os.path.join(box.dir, "backend.out"), "wb")
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([PY, "-m", "agent.server"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def _wait_listening(timeout=60):
    t0 = time.time()
    phases = []
    while time.time() - t0 < timeout:
        rt = common.read_runtime()
        if rt.get("phase") and (not phases or phases[-1] != rt["phase"]):
            phases.append(rt["phase"])
        if rt.get("phase") == "listening" and common.backend_health():
            return rt, phases, time.time() - t0
        time.sleep(0.05)
    return common.read_runtime(), phases, None


# ============================================================================ port
def test_brain_moves_to_a_free_port_when_its_port_is_taken():
    """What AnkiConnect (or anything else on 8765) did to v1.4: the brain could never listen.
    Now it takes the next free port and everyone finds it through data/runtime.json."""
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(5)                       # holds the port and never answers, like a foreign service
    taken = squatter.getsockname()[1]
    try:
        with Sandbox(port=taken) as box:
            proc, log = _start_backend(box)
            try:
                rt, phases, took = _wait_listening()
                out = open(os.path.join(box.dir, "backend.out"), encoding="utf-8", errors="replace").read()
                assert took is not None, f"the brain never came up; phases {phases}\n{out[-1500:]}"
                assert rt["port"] != taken and rt["pid"] > 0, rt
                assert f"port {taken}" in (rt.get("port_note") or ""), rt
                assert common.backend_port() == rt["port"], "clients follow the port the brain took"
                assert common.backend_health()["version"] == "1.4.2"
                assert phases[-1] == "listening", phases
                assert "start-up: python + imports" in out and f"uses port {rt['port']}" in out, out[-800:]
                assert set(rt["timings"]) >= {"starting", "neural-mesh", "agent", "listening"}, rt["timings"]
                assert took < 40, f"{took:.1f}s to come up"
                status, body = common.http_json(common.backend_url("/status"), token=common.read_token())
                assert status == 200 and body["port"] == rt["port"] and body["startup"]["total_s"] > 0, body
            finally:
                proc.terminate()
                proc.wait(timeout=15)
                log.close()
            assert common.read_runtime() == {} or common.read_runtime().get("phase") == "stopped"
            assert common.backend_port() == taken, "without a running brain the configured port applies"
    finally:
        squatter.close()


def test_bind_port_prefers_the_configured_port():
    from agent.server import bind_port
    port = free_port()
    sock, got, note = bind_port("127.0.0.1", port)
    try:
        assert got == port and note == ""
        sock2, got2, note2 = bind_port("127.0.0.1", port)          # now taken by sock
        try:
            assert got2 != port and f"port {port}" in note2 and str(got2) in note2
        finally:
            sock2.close()
    finally:
        sock.close()


# ============================================================================ status
def test_status_never_waits_for_ollama():
    """v1.4 asked Ollama inside /status, on the event loop: while Ollama was busy loading a
    model, every request to the brain -- the launcher's checks, the HUD -- stalled."""
    try:
        import httpx
        import uvicorn
        from agent.server import JarvisServer, create_app
    except Exception as e:  # pragma: no cover
        print(f"  (skipped: {e})")
        return

    class Stuck(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(30)                   # Ollama "busy": accepts and never answers

        def log_message(self, *a):
            pass

    stuck = ThreadingHTTPServer(("127.0.0.1", 0), Stuck)
    stuck.daemon_threads = True
    threading.Thread(target=stuck.serve_forever, daemon=True).start()
    with Sandbox(llm_host=f"http://127.0.0.1:{stuck.server_address[1]}") as box:
        srv = JarvisServer(config_path=box.cfg_path, permissions_path=box.perm_path,
                           data_dir=os.path.join(box.dir, "data"))
        server = uvicorn.Server(uvicorn.Config(create_app(srv, start_voice=False), host="127.0.0.1",
                                               port=box.port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    if httpx.get(f"http://127.0.0.1:{box.port}/health", timeout=1).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            worst = 0.0
            for _ in range(12):                                 # spans the 8 s Ollama re-check
                t0 = time.perf_counter()
                r = httpx.get(f"http://127.0.0.1:{box.port}/status", headers={"x-jarvis-token": srv.token}, timeout=5)
                worst = max(worst, time.perf_counter() - t0)
                assert r.status_code == 200
                time.sleep(0.8)
            assert worst < 1.0, f"/status took {worst:.2f}s while Ollama was not answering"
        finally:
            server.should_exit = True
            th.join(timeout=15)
            stuck.shutdown()


# ============================================================================ supervisor
def _child(sup, name, code):
    from boot import jarvis_service as service
    sup.children[name] = service.Child(name, lambda: [PY, "-c", code], dict(os.environ))
    return sup.children[name]


def test_supervisor_counts_slow_failed_starts_and_never_kills_a_starting_brain():
    from boot import jarvis_service as service
    with Sandbox():
        sup = service.Supervisor(with_overlay=False, quiet=True)
        # 1) a brain that takes a while and then dies without ever answering: a failed start
        child = _child(sup, "backend", "import time; time.sleep(0.4); raise SystemExit(5)")
        try:
            for _ in range(service.CRASHLOOP_AFTER):
                child.next_try = 0
                assert sup.step() is True
                child.proc.wait(timeout=20)
                assert sup.step() is True
            assert child.failed_starts == service.CRASHLOOP_AFTER and "backend_crashloop" in sup.problems
        finally:
            sup.stop_all()

        # 2) a brain that is still starting (alive, never answering) is never killed
        sup = service.Supervisor(with_overlay=False, quiet=True)
        child = _child(sup, "backend", "import time; time.sleep(60)")
        try:
            sup.step()
            for _ in range(service.HUNG_AFTER_CHECKS * 3):
                sup._check_backend_health()
            assert child.alive() and not child.ready, "a starting brain must not be restarted"
        finally:
            sup.stop_all()


def test_supervisor_marks_a_brain_ready_and_restarts_it_only_when_it_hangs():
    from boot import jarvis_service as service
    with Sandbox() as box:
        data = os.environ["JARVIS_DATA"]
        code = f"""
import json, os, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if os.path.exists(os.path.join({data!r}, "hang")):
            time.sleep(30)
        body = json.dumps({{"ok": True, "version": "test"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", {box.port}), H)
os.makedirs({data!r}, exist_ok=True)
json.dump({{"pid": os.getpid(), "port": {box.port}, "phase": "listening", "started": time.time()}},
          open(os.path.join({data!r}, "runtime.json"), "w"))
srv.serve_forever()
"""
        sup = service.Supervisor(with_overlay=False, quiet=True)
        child = _child(sup, "backend", code)
        try:
            sup.step()
            deadline = time.time() + 20
            while not child.ready and time.time() < deadline:
                sup.step()
                time.sleep(0.1)
            assert child.ready and child.ready_after is not None, "answering + listening = ready"
            assert child.failed_starts == 0
            sup._check_backend_health()
            assert child.alive() and sup._health_fail == 0
            # now it hangs: restarted after HUNG_AFTER_CHECKS missed checks
            open(os.path.join(data, "hang"), "w").close()
            saved = common.backend_health
            common.backend_health = lambda: None            # its /health no longer answers
            try:
                for _ in range(service.HUNG_AFTER_CHECKS):
                    sup._check_backend_health()
            finally:
                common.backend_health = saved
            child.proc.wait(timeout=15)
            assert not child.alive(), "a brain that hung is restarted"
            assert sup.step() is True and child.failed_starts == 0, "it had been ready: not a failed start"
            assert child.unstable == 1 and "backend_crashloop" not in sup.problems
            # dying soon after getting ready, again and again, is flagged too
            child.unstable = service.CRASHLOOP_AFTER - 1
            child.ready, child.ready_after = True, 0.1
            child.proc = types.SimpleNamespace(poll=lambda: 9, pid=0)
            child.started = time.time()
            assert sup._on_exit(child) is True
            assert child.unstable == service.CRASHLOOP_AFTER and "backend_crashloop" in sup.problems
            assert "within two minutes" in sup.problems["backend_crashloop"]
        finally:
            sup.stop_all()


# ============================================================================ launcher
def test_launcher_shows_the_phase_the_port_and_restarts():
    from boot import launcher
    said = []
    saved = launcher.ok, launcher.note, launcher.say
    launcher.ok = lambda t: said.append("ok: " + t)
    launcher.note = lambda t: said.append("..: " + t)
    launcher.say = lambda t="": said.append(t)
    try:
        with Sandbox():
            now = time.time()
            os.makedirs(common.data_dir(), exist_ok=True)
            sup = {"pid": os.getpid(), "root": ROOT, "state": "running", "started": now, "updated": now,
                   "children": {"backend": {"pid": 1, "alive": True, "starts": 1},
                                "overlay": {"pid": 4242, "alive": True}}, "problems": []}
            common.write_json_atomic(common.status_file(), sup)
            common.write_json_atomic(common.overlay_state_file(), {"pid": 4242, "visible": True, "loaded": True})
            common.write_json_atomic(common.runtime_file(), {
                "pid": os.getpid(), "port": 8766, "phase": "neural-mesh", "phase_text": "building the neural mesh",
                "started": now, "port_note": "port 8765 is in use by anki.exe (pid 99); the command bus uses port 8766 instead"})

            def restart_soon():
                time.sleep(0.6)
                common.write_json_atomic(common.status_file(), dict(sup, updated=time.time(), children={
                    "backend": {"pid": 2, "alive": True, "starts": 2, "last_exit": 1},
                    "overlay": {"pid": 4242, "alive": True}}))

            threading.Thread(target=restart_soon, daemon=True).start()
            saved_interval = launcher.T_BACKEND
            launcher.T_BACKEND = 2.0
            try:
                good, ctx = launcher.wait_ready(scale=1.0)
            finally:
                launcher.T_BACKEND = saved_interval
            text = "\n".join(said)
            assert not good and ctx["reason"] == "backend_not_ready", ctx
            assert "port 8765 is in use by anki.exe" in text, text
            assert "the brain stopped (code 1) and is starting again" in text, text
            assert ctx["runtime"]["phase"] == "neural-mesh"
            report = launcher.build_report(ctx, "w", "t")
            assert "runtime.json" in report and "neural-mesh" in report
    finally:
        launcher.ok, launcher.note, launcher.say = saved


def test_launcher_reports_the_start_up_time():
    from boot import launcher
    assert "started in" in open(launcher.__file__, encoding="utf-8").read()


# ============================================================================ health check
def test_health_check_survives_the_korean_code_page():
    """The installer pipes the health check; Korean Windows then encodes its output as cp949,
    which has no em dash. v1.4 crashed at 'Optional runtime libraries' without a summary."""
    env = dict(os.environ, PYTHONIOENCODING="cp949", PYTHONPATH=ROOT)
    for k in ("JARVIS_DATA", "JARVIS_CONFIG", "JARVIS_PERMISSIONS"):
        env.pop(k, None)
    r = subprocess.run([PY, os.path.join(ROOT, "scripts", "healthcheck.py")], cwd=ROOT, env=env,
                       capture_output=True, timeout=300)
    out = r.stdout.decode("cp949", errors="replace")
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-1500:]
    assert "Optional runtime libraries" in out and "Summary" in out and "Core is healthy" in out, out[-800:]


# ============================================================================ voice
def test_wake_detection_runs_off_the_event_loop_and_skips_ahead():
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.RawInputStream = lambda **kw: None
    fake_sd.play = fake_sd.wait = fake_sd.stop = lambda *a, **k: None
    sys.modules.setdefault("sounddevice", fake_sd)
    from voice.engine import VoiceEngine, MAX_BACKLOG_FRAMES

    class SlowDetector:
        knows_keywords = True

        def __init__(self):
            self.calls, self.threads, self.resets = 0, set(), 0

        def process(self, frame):
            self.calls += 1
            self.threads.add(threading.current_thread().name)
            time.sleep(0.03)                 # slower than real time (20 ms frames)
            return None

        def reset(self):
            self.resets += 1

    async def noop(*a):
        return None

    eng = VoiceEngine({"voice": {"tts": {}}}, noop, noop, noop, noop)
    det = SlowDetector()
    eng.detector = det
    eng.vad = types.SimpleNamespace(reset=lambda: None, available=False, is_speech=lambda f: False)
    eng.speaker = types.SimpleNamespace(available=True, stop=lambda: None, speak=lambda *a: None)

    class Stream:
        def __init__(self, **kw):
            self.callback = kw["callback"]

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    eng._sd = eng.mic.sd = types.SimpleNamespace(RawInputStream=lambda **kw: Stream(**kw))
    noise = [(np.random.default_rng(i).standard_normal(320) * 30).astype(np.int16).tobytes() for i in range(8)]
    beats = []

    async def run():
        task = asyncio.ensure_future(eng.run())

        async def heartbeat():
            while True:
                beats.append(time.perf_counter())
                await asyncio.sleep(0.02)

        hb = asyncio.ensure_future(heartbeat())
        while eng._stream is None:
            await asyncio.sleep(0.005)
        for i in range(400):                 # 8 s of audio delivered in ~0.4 s
            eng._stream.callback(noise[i % 8], 320, None, None)
            await asyncio.sleep(0.001)
        await asyncio.sleep(1.0)
        eng._running = False
        hb.cancel()
        task.cancel()

    asyncio.run(run())
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert det.calls < 400 and det.resets >= 1, (det.calls, det.resets)       # skipped ahead
    assert det.threads == {next(iter(det.threads))} and all(t.startswith("jarvis-audio") for t in det.threads)
    assert max(gaps) < 0.25, f"the event loop stalled for {max(gaps):.2f}s"
    assert MAX_BACKLOG_FRAMES == 150         # v1.4.2: frames are batched, so it falls behind less


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        t0 = time.time()
        try:
            t()
            print(f"  ok  {t.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  XX  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} start-up tests passed")
    sys.exit(1 if failed else 0)
