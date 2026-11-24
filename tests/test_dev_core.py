"""
tests/test_dev_core.py -- the developer's foundations (v1.5.0).

    python tests/test_dev_core.py

Workspaces and their stacked requests; files you send are copied, never moved; one history
commit per request, undo and start-over (with and without Git); the scheduler (order, pause,
resume, stop, picking up after a restart); the workspace-scoped terminal (outside paths, installs
and destructive commands ask first); process-tree kills; the verifier on real little projects.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dev.workspaces import WorkspaceStore  # noqa: E402
from dev.history import History  # noqa: E402
from dev.scheduler import Scheduler  # noqa: E402
from dev.agent import Job  # noqa: E402
from dev.tools import Sandbox  # noqa: E402
from dev import procs  # noqa: E402
from dev.verify import Verifier  # noqa: E402


def _tmp():
    return tempfile.mkdtemp(prefix="jarvis_devtest_")


def test_workspaces_stack_requests_in_order():
    root = _tmp()
    try:
        st = WorkspaceStore(root)
        a = st.create("Snake game")
        b = st.create("Bakery site", select=False)
        assert st.selected() == a["id"] and st.find("bakery")["id"] == b["id"] and st.find("snake GAME")["id"] == a["id"]
        r1 = st.add_request(a["id"], "make a snake game")
        time.sleep(0.01)
        r2 = st.add_request(b["id"], "make a bakery website")
        time.sleep(0.01)
        r3 = st.add_request(a["id"], "add a high score table")
        assert [r["id"] for r in st.requests(a["id"])] == [r1["id"], r3["id"]], "stacked in the order given"
        assert st.next_queued()[1]["id"] == r1["id"]
        st.update_request(a["id"], r1["id"], status="working")
        assert st.next_queued()[1]["id"] == r2["id"], "a workspace's later request never jumps ahead"
        st.update_request(a["id"], r1["id"], status="paused")
        st.update_request(b["id"], r2["id"], status="done")
        assert st.next_queued() is None, "a paused request holds its workspace's queue"
        st.update_request(a["id"], r1["id"], status="working")
        assert st.recover() == 1 and st.get_request(a["id"], r1["id"])["status"] == "queued", "picked up after a restart"
        st.rename(b["id"], "Bakery")
        assert st.get(b["id"])["name"] == "Bakery"
        trash = st.delete(b["id"])
        assert os.path.isdir(trash) and st.get(b["id"]) is None and len(st.list()) == 1
        st.log(a["id"], r1["id"], "run", "$ echo hi")
        assert st.log_tail(a["id"], r1["id"])[-1]["text"] == "$ echo hi"
        d = st.detail(a["id"])
        assert d["requests"] == 2 and d["request_list"][0]["text"] == "make a snake game"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sent_files_are_copied_never_moved():
    root = _tmp()
    try:
        st = WorkspaceStore(os.path.join(root, "ws"))
        src = os.path.join(root, "game.gba")
        open(src, "wb").write(b"ROM" * 100)
        folder = os.path.join(root, "proj")
        os.makedirs(os.path.join(folder, "src"))
        open(os.path.join(folder, "src", "main.py"), "w").write("print(1)")
        w = st.create("ROM hack")
        r = st.add_request(w["id"], "translate the menus", [src, src, folder])
        names = [a["name"] for a in r["attachments"]]
        assert names == ["game.gba", "game (2).gba", "proj"], names
        assert os.path.exists(src), "the original stays where it was"
        assert os.path.isfile(os.path.join(st.dir(w["id"]), "input", "proj", "src", "main.py"))
        try:
            st.add_request(w["id"], "x", [os.path.join(root, "missing.bin")])
            raise AssertionError("a missing file must be refused")
        except FileNotFoundError:
            pass
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_history_commits_undoes_and_starts_over():
    root = _tmp()
    try:
        for use_git in (True, False):
            proj = os.path.join(root, f"p{use_git}")
            h = History(proj, os.path.join(root, f"snaps{use_git}"))
            if not use_git:
                h.git = None
            if use_git and not h.git:
                continue
            base = h.begin("r1")
            open(os.path.join(proj, "app.py"), "w").write("v1")
            c1 = h.commit("r1", "first request")
            base2 = h.begin("r2")
            open(os.path.join(proj, "app.py"), "w").write("v2")
            open(os.path.join(proj, "extra.py"), "w").write("x")
            c2 = h.commit("r2", "second request")
            h.undo(base2, c2)
            assert open(os.path.join(proj, "app.py")).read() == "v1", use_git
            assert not os.path.exists(os.path.join(proj, "extra.py")), use_git
            os.makedirs(os.path.join(proj, "node_modules"), exist_ok=True)
            open(os.path.join(proj, "app.py"), "w").write("broken")
            h.reset(base2 if not use_git else c1)
            assert open(os.path.join(proj, "app.py")).read() == "v1", use_git
            assert os.path.isdir(os.path.join(proj, "node_modules")), "dependency folders are kept"
            if use_git:
                assert os.path.exists(os.path.join(proj, ".gitignore")) and "node_modules/" in open(os.path.join(proj, ".gitignore")).read()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_scheduler_order_pause_resume_stop_and_restart():
    root = _tmp()
    try:
        st = WorkspaceStore(root)
        w = st.create("A")
        ran, seen = [], {}
        gate = threading.Event()

        def run_job(wid, req, job):
            ran.append(req["text"])
            seen[req["text"]] = job
            for _ in range(400):
                job.checkpoint()
                if gate.is_set() and req["text"] != "slow":
                    break
                time.sleep(0.01)
            return {"status": "done", "summary": "ok"}

        sc = Scheduler(st, run_job)
        sc.start()
        r1 = sc.submit(w["id"], "slow")
        r2 = sc.submit(w["id"], "second")
        for _ in range(100):
            if "slow" in seen:
                break
            time.sleep(0.02)
        assert sc.busy()["request"] == r1["id"]
        sc.pause(w["id"])
        assert st.get_request(w["id"], r1["id"])["status"] == "paused" and seen["slow"].paused
        sc.resume(w["id"])
        assert st.get_request(w["id"], r1["id"])["status"] == "working" and not seen["slow"].paused
        sc.stop_request(w["id"], r1["id"])
        gate.set()
        for _ in range(200):
            if st.get_request(w["id"], r2["id"])["status"] == "done":
                break
            time.sleep(0.02)
        assert st.get_request(w["id"], r1["id"])["status"] == "stopped", st.get_request(w["id"], r1["id"])
        assert st.get_request(w["id"], r2["id"])["status"] == "done" and ran == ["slow", "second"]
        sc.stop()
        # a request that was being worked on when JARVIS stopped is picked up again
        r3 = st.add_request(w["id"], "after restart")
        st.update_request(w["id"], r3["id"], status="working")
        sc2 = Scheduler(st, run_job)
        sc2.start()
        for _ in range(200):
            if st.get_request(w["id"], r3["id"])["status"] == "done":
                break
            time.sleep(0.02)
        assert st.get_request(w["id"], r3["id"])["status"] == "done"
        sc2.stop()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_workspace_terminal_asks_where_it_must():
    root = _tmp()
    try:
        asked = []

        def ask(category, detail, force=False, risk=None):
            asked.append((category, force))
            return category == "dev_outside" and "allowed" in detail

        ws = os.path.join(root, "ws")
        proj = os.path.join(ws, "project")
        sb = Sandbox(ws, proj, ask=ask)
        assert "created app.py" in sb.write_file("app.py", "print('hi')\n")
        assert sb.read_file("app.py").startswith("print")
        assert "edited" in sb.edit_file("app.py", "print('hi')", "print('hello')")
        assert "hello" in sb.read_file("app.py")
        assert not asked, "inside the workspace: no questions"
        outside = os.path.join(root, "outside.txt")
        try:
            sb.write_file(outside, "x")
            raise AssertionError("writing outside the workspace must ask")
        except PermissionError:
            pass
        assert asked[-1] == ("dev_outside", False)
        try:
            sb.run(f"rm -rf {proj}/nothing")
            raise AssertionError("a destructive command must be confirmed")
        except PermissionError:
            pass
        assert asked[-1] == ("dev_workspace", True)
        try:
            sb.run("npm install -g left-pad")
            raise AssertionError("a global install must ask")
        except PermissionError:
            pass
        assert asked[-1][0] == "install_software"
        n = len(asked)
        r = sb.run("echo inside && python -c \"print(6*7)\"")
        assert r.ok and "42" in r["output"] and len(asked) == n, "commands inside the workspace just run"
        assert sb.venv_python(), "Python commands get the project's own environment"
        if os.path.exists("/etc/hostname"):
            try:
                sb.run("cat /etc/hostname", timeout=10)
                raise AssertionError("a command that reads outside the workspace must ask")
            except PermissionError:
                assert asked[-1][0] == "dev_outside"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_time_limits_kill_the_whole_process_tree():
    root = _tmp()
    try:
        t0 = time.time()
        r = procs.run("sleep 30 & sleep 30 & wait", root, timeout=1)
        assert r.get("timed_out") and time.time() - t0 < 10, r
        time.sleep(0.3)
        try:
            import psutil
            left = [p for p in psutil.process_iter(["cmdline"]) if p.info.get("cmdline") == ["sleep", "30"]]
            assert not left, "children of the shell are killed too"
        except ImportError:
            pass
        bg = procs.Background("python -c \"import time; print('up', flush=True); time.sleep(30)\"", root).start()
        time.sleep(1.0)
        assert bg.alive() and "up" in bg.output()
        bg.stop()
        assert not bg.alive()
    finally:
        shutil.rmtree(root, ignore_errors=True)


COUNTER = """<!doctype html><html><head><title>Counter</title></head><body>
<h1>Counter</h1><p id="n">0</p><button id="p">+</button><button id="m">-</button><button id="r">Reset</button>
<script src="app.js"></script></body></html>"""
COUNTER_JS = """let n = 0; const out = document.getElementById('n');
document.getElementById('p').onclick = () => { n++; out.textContent = n; };
document.getElementById('m').onclick = () => { n--; out.textContent = n; };
document.getElementById('r').onclick = () => { n = 0; out.textContent = n; };"""


def test_the_verifier_checks_real_little_programs():
    root = _tmp()
    try:
        ws = os.path.join(root, "ws")
        proj = os.path.join(ws, "project")
        os.makedirs(proj)
        sb = Sandbox(ws, proj)
        open(os.path.join(proj, "index.html"), "w").write(COUNTER)
        open(os.path.join(proj, "app.js"), "w").write(COUNTER_JS)
        open(os.path.join(proj, "jarvis.json"), "w").write('{"kind": "web", "run": "static"}')
        crit = [{"id": 1, "text": "starts at 0", "checks": [{"type": "web", "by": "jarvis", "steps": [{"expect_text": "0", "in": "#n"}]}]},
                {"id": 2, "text": "+ then - gives 0", "checks": [{"type": "web", "by": "jarvis", "steps": [
                    {"click": 'button:has-text("+")'}, {"click": 'button:has-text("+")'}, {"click": 'button:has-text("-")'},
                    {"expect_text": "1", "in": "#n"}]}]}]
        rep = Verifier(sb, os.path.join(ws, "state")).run(crit)
        assert rep.ok, rep.text()
        assert any("page controls" in e and "'+'" in e and "'Reset'" in e for e in rep.evidence), rep.evidence
        # a script that uses elements the page does not have
        open(os.path.join(proj, "index.html"), "w").write(COUNTER.replace('<button id="m">-</button>', ""))
        rep = Verifier(sb, os.path.join(ws, "state")).run(crit)
        ids = next(c for c in rep.checks if c["id"] == "element-ids")
        assert not ids["ok"] and "#m" in ids["detail"], ids
        crit2 = next(c for c in rep.checks if c.get("criterion") == 2)
        assert not crit2["ok"] and 'no element matching button:has-text("-")' in crit2["detail"], crit2
        assert "the page's buttons show: '+', 'Reset'" in crit2["detail"], "it says which labels the page has"
        runs = next(c for c in rep.checks if c["id"] == "runs")
        assert "(at app.js:3)" in runs["detail"], runs
        # a script that does not parse: the file and line are named (with Node.js on the PC)
        open(os.path.join(proj, "index.html"), "w").write(COUNTER)
        open(os.path.join(proj, "app.js"), "w").write(COUNTER_JS + "\nfunction broken() {\n  let x = 1;\n")
        rep = Verifier(sb, os.path.join(ws, "state")).run(crit)
        syn = next(c for c in rep.checks if c["id"] == "syntax")
        runs = next(c for c in rep.checks if c["id"] == "runs")
        assert not runs["ok"] and "these scripts do not parse: app.js" in runs["detail"], runs
        if shutil.which("node"):
            assert not syn["ok"] and "app.js:" in syn["detail"] and "SyntaxError" in syn["detail"], syn
        open(os.path.join(proj, "app.js"), "w").write(COUNTER_JS)
        # a command-line program, its own test, and tests that were weakened later
        shutil.rmtree(proj)
        os.makedirs(os.path.join(proj, "tests"))
        open(os.path.join(proj, "main.py"), "w").write("import sys\nprint(float(sys.argv[1]) * 9 / 5 + 32 if len(sys.argv) > 1 else 'usage: main.py C')\n")
        open(os.path.join(proj, "tests", "test_main.py"), "w").write(
            "import subprocess, sys\ndef test_boil():\n    out = subprocess.run([sys.executable, 'main.py', '100'], capture_output=True, text=True).stdout\n    assert '212' in out\n")
        open(os.path.join(proj, "jarvis.json"), "w").write('{"kind": "cli", "test": ["python -m pytest -q"], "run": "python main.py"}')
        crit = [{"id": 1, "text": "100 C is 212 F", "checks": [{"type": "cli", "args": "100", "expect": ["212"]}]}]
        state = os.path.join(ws, "state2")
        rep = Verifier(sb, state).run(crit)
        assert rep.ok, rep.text()
        open(os.path.join(proj, "tests", "test_main.py"), "w").write("def test_boil():\n    assert True\n")
        rep = Verifier(sb, state).run(crit)
        integ = [c for c in rep.checks if c["id"] == "test-integrity"]
        assert integ and not integ[0]["ok"] and "no longer checks anything" in integ[0]["detail"], rep.text()
        # placeholders are not a finished product
        open(os.path.join(proj, "main.py"), "w").write("def run():\n    raise NotImplementedError\nprint('x')\n")
        rep = Verifier(sb, os.path.join(ws, "state3")).run([])
        assert not next(c for c in rep.checks if c["id"] == "placeholders")["ok"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} developer core tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
