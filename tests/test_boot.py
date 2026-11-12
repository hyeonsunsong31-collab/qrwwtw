"""
tests/test_boot.py -- the start-up chain: supervisor, START launcher, overlay runtime.

    python tests/test_boot.py            (or: python -m pytest tests/)

These tests exist because of a real failure: double-clicking START_JARVIS.bat showed
nothing and said nothing. They pin down every way that could happen:
  * a process without stdout/stderr (pythonw.exe) must not crash the backend,
  * the supervisor must restart children, capture their output, notice crash loops and
    honour "Quit JARVIS",
  * only one supervisor may run, and only JARVIS's own processes may ever be stopped,
  * the overlay runtime check/repair must be exact and must refuse tampered downloads,
  * the launcher must recognise "it is up" and name the stage that failed when it is not.
No network, no Electron, no audio needed.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from boot import common, overlay_runtime  # noqa: E402
from boot import jarvis_service as service  # noqa: E402
from boot import launcher  # noqa: E402

PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sandbox:
    """A throwaway data folder + config (own port), restored afterwards."""

    def __init__(self, port: int = 0):
        self.dir = tempfile.mkdtemp(prefix="jarvis_boot_")
        self.port = port or free_port()
        self.keys = ("JARVIS_DATA", "JARVIS_CONFIG", "JARVIS_PERMISSIONS")
        self.saved = {k: os.environ.get(k) for k in self.keys}

    def __enter__(self):
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
        cfg["neural_core"]["active_neurons"] = 20000
        cfg["llm"]["host"] = "http://127.0.0.1:9"
        cfg["voice"]["enabled"] = False
        cfg["server"]["port"] = self.port
        cfg_path = os.path.join(self.dir, "jarvis.config.json")
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))
        perm = os.path.join(self.dir, "permissions.json")
        shutil.copy(os.path.join(ROOT, "config", "permissions.json"), perm)
        os.environ["JARVIS_DATA"] = os.path.join(self.dir, "data")
        os.environ["JARVIS_CONFIG"] = cfg_path
        os.environ["JARVIS_PERMISSIONS"] = perm
        common._port_cache = None
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        common._port_cache = None
        shutil.rmtree(self.dir, ignore_errors=True)


# ----------------------------------------------------------------- the original bug
def test_backend_survives_without_stdout_and_stderr():
    """pythonw.exe gives a process no stdout/stderr (None). uvicorn's logging set-up calls
    sys.stdout.isatty(); before the fix the backend died there, silently."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except Exception as e:  # pragma: no cover
        print("  (skipped: fastapi/uvicorn not installed: %s)" % e)
        return
    with Sandbox() as box:
        code = "import sys; sys.stdout = None; sys.stderr = None; from agent.server import main; main()"
        env = dict(os.environ, PYTHONPATH=ROOT)
        proc = subprocess.Popen([PY, "-c", code], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            health = common.wait_until(common.backend_health, timeout=60)
            assert health and health.get("ok") is True, f"backend did not come up (exit code {proc.poll()})"
            state, status = common.backend_status()
            assert state == "ok" and status["overlay_clients"] == 0 and status["voice_state"] == "off", (state, status)
            token = common.read_token()
            assert common.http_json(common.backend_url("/announce"), method="POST")[0] == 401
            code_, body = common.http_json(common.backend_url("/announce"), token=token, method="POST")
            assert code_ == 200 and body == {"ok": True, "clients": 0}, (code_, body)
            assert status["pid"] > 0 and status["version"]
        finally:
            proc.kill()
            proc.wait(timeout=10)


def test_ensure_stdio_replaces_missing_streams():
    with Sandbox():
        saved = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = None
            common.ensure_stdio("unit.out")
            assert sys.stdout is not None and sys.stderr is not None
            print("written while stdout was missing")          # must not raise
            sys.stdout.flush()
            sys.stdout.close()
        finally:
            sys.stdout, sys.stderr = saved
        text = open(os.path.join(common.logs_dir(), "unit.out"), encoding="utf-8").read()
        assert "written while stdout was missing" in text


# ----------------------------------------------------------------- process identity
def test_only_jarvis_processes_are_ever_matched():
    R = ROOT
    cases = [
        ("pythonw.exe", "", ["pythonw.exe", os.path.join(R, "boot", "jarvis_service.py")], R, "supervisor"),
        ("python", "", ["python", os.path.join("boot", "jarvis_service.py"), "--from-launcher"], R, "supervisor"),
        ("python.exe", "", ["python.exe", "-m", "agent.server"], R, "backend"),
        ("python.exe", "", ["python.exe", "-m", "agent.server"], tempfile.gettempdir(), None),   # not a JARVIS folder
        ("python.exe", "", ["python.exe", "-m", "http.server", "8765"], R, None),               # someone's own python
        ("python.exe", "", ["python.exe", "my_script.py"], R, None),
        ("electron.exe", os.path.join(R, "node_modules", "electron", "dist", "electron.exe"), ["x", R], R, "overlay"),
        ("electron", "/opt/vscode/node_modules/electron/dist/electron", ["x", "."], "/opt/vscode", None),
        ("electron.exe", "C:/Program Files/SomeApp/electron.exe", ["x"], "C:/", None),          # another Electron app
        ("ffplay.exe", "ffplay.exe", ["ffplay.exe", "-window_title", "JARVIS-music", "http://x"], "/", "music"),
        ("ffplay.exe", "ffplay.exe", ["ffplay.exe", "holiday.mp4"], "/", None),                 # the user's own player
        ("notepad.exe", "", ["notepad.exe", os.path.join(R, "boot", "jarvis_service.py")], R, None),
        ("chrome.exe", "", ["chrome.exe"], R, None),
    ]
    for name, exe, cmd, cwd, want in cases:
        got = common.classify_process(name, exe, cmd, cwd)
        assert (got["kind"] if got else None) == want, (name, cmd, got, want)
    me = common.my_lineage()
    assert os.getpid() in me
    assert all(p["pid"] not in me for p in common.find_jarvis_processes())


def test_single_instance_lock():
    with Sandbox():
        first = common.SingleInstance("JARVIS.Test.%d" % os.getpid())
        second = common.SingleInstance("JARVIS.Test.%d" % os.getpid())
        assert first.acquire() is True
        assert second.acquire() is False, "a second supervisor must be refused"


# ----------------------------------------------------------------- small helpers
def test_tail_rotate_and_atomic_json():
    with Sandbox() as box:
        p = os.path.join(box.dir, "x.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"line {i}" for i in range(100)) + "\n\n")
        assert common.tail(p, 3) == ["line 97", "line 98", "line 99"]
        assert common.tail(os.path.join(box.dir, "missing.log")) == []
        common.rotate(p, max_bytes=10)
        assert os.path.exists(p + ".1") and not os.path.exists(p)
        j = os.path.join(box.dir, "deep", "state.json")
        assert common.write_json_atomic(j, {"a": 1, "k": "한글"})
        assert common.read_json(j) == {"a": 1, "k": "한글"}
        assert common.read_json(os.path.join(box.dir, "nope.json"), {"d": 1}) == {"d": 1}
        assert common.backend_port() == box.port                 # read from the config, not hard-coded
        assert common.http_json(common.backend_url("/health"), timeout=1)[0] == 0   # nothing listening


# ----------------------------------------------------------------- overlay runtime
def _fake_runtime(root: str, complete: bool = True):
    shutil.copy(os.path.join(ROOT, "package-lock.json"), root)
    lock = overlay_runtime.read_lock(root)
    for key, sentinel in overlay_runtime.PACKAGES.items():
        d = os.path.join(root, *key.split("/"))
        os.makedirs(os.path.dirname(os.path.join(d, sentinel)), exist_ok=True)
        json.dump({"version": lock[key]["version"]}, open(os.path.join(d, "package.json"), "w"))
        open(os.path.join(d, sentinel), "w").write("{}")
    if complete:
        _plat, _arch, exe = overlay_runtime.electron_platform()
        dist = os.path.join(root, "node_modules", "electron", "dist")
        os.makedirs(os.path.dirname(os.path.join(dist, *exe.split("/"))), exist_ok=True)
        open(os.path.join(dist, *exe.split("/")), "w").write("binary")
        open(os.path.join(dist, "version"), "w").write("v" + lock["node_modules/electron"]["version"])
        open(os.path.join(root, "node_modules", "electron", "path.txt"), "w").write(exe)
    return lock


def test_overlay_runtime_detects_exactly_what_is_missing():
    assert overlay_runtime.electron_platform("win32", "AMD64") == ("win32", "x64", "electron.exe")
    assert overlay_runtime.electron_platform("win32", "ARM64") == ("win32", "arm64", "electron.exe")
    assert overlay_runtime.electron_platform("linux", "x86_64") == ("linux", "x64", "electron")
    assert overlay_runtime.electron_platform("darwin", "arm64")[2].endswith("MacOS/Electron")
    with tempfile.TemporaryDirectory() as root:
        assert "package-lock.json" in overlay_runtime.problems(root)[0]          # not a JARVIS folder
        shutil.copy(os.path.join(ROOT, "package-lock.json"), root)
        assert len(overlay_runtime.problems(root)) == 4                           # 3 packages + the binary
        lock = _fake_runtime(root, complete=False)
        left = overlay_runtime.problems(root)
        assert len(left) == 1 and "electron/dist" in left[0], left               # the v1.2 failure: npm "succeeded"
        _fake_runtime(root, complete=True)
        assert overlay_runtime.problems(root) == []
        # an Electron binary of another version does not count
        open(os.path.join(root, "node_modules", "electron", "dist", "version"), "w").write("v1.0.0")
        assert len(overlay_runtime.problems(root)) == 1
        # a package of another version does not count either
        _fake_runtime(root, complete=True)
        json.dump({"version": "0.0.1"}, open(os.path.join(root, "node_modules", "three", "package.json"), "w"))
        left = overlay_runtime.problems(root)
        assert len(left) == 1 and "three " + lock["node_modules/three"]["version"] in left[0], left


def test_overlay_runtime_refuses_unsafe_or_tampered_input():
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "out")
        assert overlay_runtime._safe_join(base, "a/b.txt") == os.path.join(base, "a", "b.txt")
        for evil in ("../x", "a/../../x", "/etc/passwd", "C:/Windows/x", "..\\x"):
            assert overlay_runtime._safe_join(base, evil) is None, evil
        tgz = os.path.join(d, "evil.tgz")
        with tarfile.open(tgz, "w:gz") as tf:
            ti = tarfile.TarInfo("package/../../escape.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        try:
            overlay_runtime.extract_tgz(tgz, os.path.join(d, "t"))
            raise AssertionError("path traversal in a tarball must be refused")
        except overlay_runtime.RuntimeProblem:
            pass
        zp = os.path.join(d, "evil.zip")
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("../escape.txt", "x")
        try:
            overlay_runtime.extract_zip(zp, os.path.join(d, "z"))
            raise AssertionError("path traversal in a zip must be refused")
        except overlay_runtime.RuntimeProblem:
            pass
        assert not os.path.exists(os.path.join(d, "escape.txt"))
        # good archives extract; the npm tarball's top folder is dropped
        good = os.path.join(d, "good.tgz")
        with tarfile.open(good, "w:gz") as tf:
            ti = tarfile.TarInfo("package/build/a.js")
            ti.size = 3
            tf.addfile(ti, io.BytesIO(b"abc"))
        assert overlay_runtime.extract_tgz(good, os.path.join(d, "g")) == 1
        assert open(os.path.join(d, "g", "build", "a.js")).read() == "abc"
        # integrity: npm's sha512 form and Electron's hex sha256 form
        import base64
        import hashlib
        blob = os.path.join(d, "blob")
        open(blob, "wb").write(b"jarvis")
        sri = "sha512-" + base64.b64encode(hashlib.sha512(b"jarvis").digest()).decode()
        assert overlay_runtime.file_matches(blob, sri)
        assert overlay_runtime.file_matches(blob, hashlib.sha256(b"jarvis").hexdigest())
        assert not overlay_runtime.file_matches(blob, "sha512-" + base64.b64encode(b"0" * 64).decode())
        assert not overlay_runtime.file_matches(blob, "0" * 64)
        # downloads only from the pinned hosts
        try:
            overlay_runtime.download("https://example.com/electron.zip", os.path.join(d, "e.zip"), "0" * 64, lambda m: None)
            raise AssertionError("an unexpected download host must be refused")
        except overlay_runtime.RuntimeProblem:
            pass


# ----------------------------------------------------------------- supervisor
def _child(sup, name, code):
    sup.children[name] = service.Child(name, lambda: [PY, "-c", code], dict(os.environ))
    return sup.children[name]


def test_supervisor_restarts_logs_and_reports():
    with Sandbox():
        sup = service.Supervisor(with_overlay=False, quiet=True)
        child = _child(sup, "backend", "print('hello from the child', flush=True); import time; time.sleep(60)")
        try:
            assert sup.step() is True and child.alive() and child.starts == 1
            first_pid = child.proc.pid
            assert common.wait_until(lambda: "hello from the child" in "\n".join(common.tail(child.log_path)), 10), \
                "child output must be captured in data/logs"
            sup.write_status(force=True)
            st = common.read_json(common.status_file())
            assert st["pid"] == os.getpid() and st["state"] == "running" and common.same_path(st["root"], ROOT)
            assert st["children"]["backend"]["alive"] is True and st["children"]["backend"]["pid"] == first_pid
            # it dies -> noticed, scheduled, restarted
            child.proc.kill()
            child.proc.wait(timeout=10)
            assert sup.step() is True and child.proc is None and child.last_exit is not None
            child.next_try = 0
            assert sup.step() is True and child.alive() and child.starts == 2 and child.proc.pid != first_pid
        finally:
            sup.stop_all()
        assert not child.alive()
        assert common.read_json(common.status_file())["state"] == "stopped"


def test_supervisor_flags_a_crash_loop_and_honours_quit():
    with Sandbox():
        sup = service.Supervisor(with_overlay=False, quiet=True)
        child = _child(sup, "backend", "import sys; print('boom: cannot start'); sys.exit(7)")
        try:
            for _ in range(service.CRASHLOOP_AFTER):
                child.next_try = 0
                assert sup.step() is True                      # spawn
                child.proc.wait(timeout=20)
                assert sup.step() is True                      # notice the exit
            assert child.last_exit == 7 and child.fast_failures >= service.CRASHLOOP_AFTER
            assert "backend_crashloop" in sup.problems, sup.problems
            sup.write_status(force=True)
            ids = [p["id"] for p in common.read_json(common.status_file())["problems"]]
            assert "backend_crashloop" in ids
            assert child.next_try > time.time(), "a crash loop must back off, not spin"
            # "Quit JARVIS" in the overlay: exit code 42 means stop everything, do not restart
            child.next_try = time.time() + 3600
            overlay = _child(sup, "overlay", f"import sys; sys.exit({common.QUIT_EXIT_CODE})")
            assert sup.step() is True
            overlay.proc.wait(timeout=20)
            assert sup.step() is False, "exit code 42 from the overlay must shut JARVIS down"
        finally:
            sup.stop_all()


def test_supervisor_never_uses_npx_and_waits_for_the_runtime():
    cmd = service.overlay_command()
    if cmd is not None:
        assert os.path.isabs(cmd[0]) and os.path.isfile(cmd[0]) and "npx" not in " ".join(cmd).lower()
        assert cmd[-1] == ROOT
    assert service.backend_command()[1:] == ["-m", "agent.server"]


# ----------------------------------------------------------------- launcher
def test_launcher_classifies_backend_failures():
    tb = lambda *last: ["Traceback (most recent call last):", '  File "x.py", line 1, in <module>',
                        '    raise JSONDecodeError("Expecting value", s, err.value) from None', *last]
    cases = [
        (tb("json.decoder.JSONDecodeError: Expecting value: line 2 column 1 (char 13)"), "config"),
        (tb("ModuleNotFoundError: No module named 'fastapi'"), "missing_module"),
        (tb("ImportError: DLL load failed while importing _ctranslate2: The specified module could not be found."), "dll"),
        (tb("PermissionError: [Errno 13] Permission denied: 'data\\\\session.token.tmp'"), "permission"),
        (["ERROR:    [Errno 98] error while attempting to bind on address ('127.0.0.1', 8765): address already in use"], "port_in_use"),
        (["ERROR:    [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8765): only one usage"], "port_in_use"),
        (["[jarvis] ready (0.2s)"], ""),
    ]
    for lines, want in cases:
        assert launcher.backend_sign(lines)[0] == want, (lines[-1], launcher.backend_sign(lines))
    assert launcher.backend_sign(tb("ModuleNotFoundError: No module named 'fastapi'"))[1] == "fastapi"


def test_launcher_explains_every_failure_in_plain_words():
    with Sandbox():
        os.makedirs(common.logs_dir(), exist_ok=True)
        with open(launcher.log_path("backend.log"), "w", encoding="utf-8") as fh:
            fh.write("Traceback (most recent call last):\nModuleNotFoundError: No module named 'uvicorn'\n")
        what, todo = launcher.diagnose({"reason": "backend_crashloop"})
        assert "uvicorn" in what and "INSTALL_JARVIS.bat" in todo
        for reason, must in (("overlay_runtime", "internet"), ("stale_processes", "Sign out"),
                             ("port_foreign", "port"), ("overlay_not_visible", "SAFE_GRAPHICS"),
                             ("overlay_crashloop", "SAFE_GRAPHICS"), ("not_connected", "127.0.0.1"),
                             ("supervisor_silent", "INSTALL_JARVIS.bat"), ("needs_install", "INSTALL_JARVIS.bat")):
            what, todo = launcher.diagnose({"reason": reason, "detail": "x"})
            assert what and todo and must.lower() in (what + " " + todo).lower(), (reason, what, todo)
        report = launcher.build_report({"reason": "backend_crashloop", "stages": {"supervisor": 0.5}}, "w", "t")
        assert "JARVIS start-up report" in report and "backend.log" in report and "ModuleNotFoundError" in report
        assert all(len(ln) <= 80 for ln in launcher.wrap("word " * 80))


class _FakeBackend(BaseHTTPRequestHandler):
    token = ""
    clients = 1

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "version": "test"})
        if self.path == "/status":
            if self.headers.get("x-jarvis-token") != self.token:
                return self._send(401, {"detail": "no"})
            return self._send(200, {"ok": True, "version": "test", "overlay_clients": self.clients, "neurons_active": 5})
        self._send(404, {})

    def do_POST(self):
        self._send(200, {"ok": True, "clients": self.clients})

    def log_message(self, *a):
        pass


def _write_running_state(overlay_pid=4242, problems=None):
    now = time.time()
    common.write_json_atomic(common.status_file(), {
        "pid": os.getpid(), "root": ROOT, "version": "test", "state": "running", "started": now, "updated": now,
        "children": {"backend": {"pid": 1, "alive": True}, "overlay": {"pid": overlay_pid, "alive": True}},
        "problems": problems or []})
    common.write_json_atomic(common.overlay_state_file(), {
        "pid": overlay_pid, "visible": True, "loaded": True, "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1040}})


def test_launcher_recognises_up_and_names_the_failed_stage():
    quiet = lambda *a, **k: None
    saved = launcher.ok, launcher.note, launcher.say
    launcher.ok = launcher.note = launcher.say = quiet
    try:
        with Sandbox() as box:
            _FakeBackend.token, _FakeBackend.clients = "tok-123", 1
            httpd = HTTPServer(("127.0.0.1", box.port), _FakeBackend)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                os.makedirs(common.data_dir(), exist_ok=True)
                open(os.path.join(common.data_dir(), "session.token"), "w").write("tok-123")
                # 1) everything is up -> success, and inspect() agrees
                _write_running_state()
                good, ctx = launcher.wait_ready(scale=0.05)
                assert good and set(ctx["stages"]) == {"supervisor", "overlay", "backend", "connected"}, ctx
                st = launcher.inspect()
                assert st["healthy"] and st["connected"] and st["backend"] == "ok"
                # 2) the HUD is up but nothing is connected to the brain
                _FakeBackend.clients = 0
                good, ctx = launcher.wait_ready(scale=0.02)
                assert not good and ctx["reason"] == "not_connected", ctx
                _FakeBackend.clients = 1
                # 3) the overlay process runs but its window never loaded (stale overlay.json: other pid)
                _write_running_state(overlay_pid=4242)
                common.write_json_atomic(common.overlay_state_file(), {"pid": 9, "visible": True, "loaded": True})
                good, ctx = launcher.wait_ready(scale=0.02)
                assert not good and ctx["reason"] == "overlay_not_visible", ctx
                # 4) the supervisor reports a crash loop -> fail at once with its text
                _write_running_state(problems=[{"id": "backend_crashloop", "text": "keeps exiting (code 1)"}])
                t0 = time.time()
                good, ctx = launcher.wait_ready()
                assert not good and ctx["reason"] == "backend_crashloop" and time.time() - t0 < 5, ctx
                # 5) the port answers but rejects this folder's token -> another copy/program owns it
                _write_running_state()
                open(os.path.join(common.data_dir(), "session.token"), "w").write("some-other-token")
                assert common.backend_status()[0] == "foreign"
                assert launcher.inspect()["healthy"] is False
                # 6) no supervisor at all
                os.remove(common.status_file())
                good, ctx = launcher.wait_ready(scale=0.03)
                assert not good and ctx["reason"] == "supervisor_silent", ctx
            finally:
                httpd.shutdown()
                httpd.server_close()
    finally:
        launcher.ok, launcher.note, launcher.say = saved


def test_launcher_preflight_and_notes():
    assert launcher.missing_files() == []
    ctx = {}
    saved = launcher.REQUIRED_PACKAGES
    launcher.REQUIRED_PACKAGES = {"a_package_that_does_not_exist_xyz": "test"}
    quiet = launcher.ok, launcher.note
    launcher.ok = launcher.note = lambda *a: None
    try:
        assert launcher.preflight(ctx) is False and ctx["reason"] == "needs_install"
        assert "a_package_that_does_not_exist_xyz" in ctx["detail"]
    finally:
        launcher.REQUIRED_PACKAGES = saved
        launcher.ok, launcher.note = quiet
    assert launcher.diagnose(ctx)[1].startswith("Double-click INSTALL_JARVIS.bat")


def test_windows_scripts_are_ascii_and_wired_to_the_launcher():
    """cmd.exe / Windows PowerShell 5.1 read these in the system code page: ASCII only."""
    for name in ("START_JARVIS.bat", "STOP_JARVIS.bat", "START_JARVIS_SAFE_GRAPHICS.bat",
                 "INSTALL_JARVIS.bat", "UNINSTALL_JARVIS.bat", os.path.join("scripts", "setup_windows.ps1"),
                 os.path.join("boot", "register_autostart.ps1"), os.path.join("overlay", "native", "workerw.ps1")):
        data = open(os.path.join(ROOT, name), "rb").read()
        assert all(b < 128 for b in data), name + " contains non-ASCII bytes"
    start = open(os.path.join(ROOT, "START_JARVIS.bat"), encoding="ascii").read()
    assert "boot\\launcher.py" in start and "schtasks" not in start.lower(), "START must run the verifying launcher"
    assert 'if not exist ".venv\\Scripts\\python.exe" goto notinstalled' in start
    setup = open(os.path.join(ROOT, "scripts", "setup_windows.ps1"), encoding="ascii").read()
    assert "boot\\overlay_runtime.py" in setup and "launcher.py\") --wait" in setup
    assert "npx" not in setup.lower()
    task = open(os.path.join(ROOT, "boot", "register_autostart.ps1"), encoding="ascii").read()
    assert "boot\\jarvis_service.py" in task and "pythonw.exe" in task


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception:
            import traceback
            failed += 1
            print(f"  XX  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} boot tests passed")
    sys.exit(1 if failed else 0)
