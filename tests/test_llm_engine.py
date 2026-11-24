"""
tests/test_llm_engine.py -- the language engine keeps itself working (v1.4.3).

    python tests/test_llm_engine.py

A fake Ollama (HTTP) stands in for the real one -- and a fake `ollama` executable that starts
it -- so these run anywhere: a system proxy no longer breaks the local connection, JARVIS starts
Ollama itself, moves it off a port another program holds, downloads a missing model while
using an installed one, steps down to a smaller model when memory is short, tells a missing
model from an engine that is down, and explains all of it.
"""
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import ollama_service as svc  # noqa: E402
from agent.llm_ollama import ResilientLLM, OllamaLLM, classify  # noqa: E402

CANDS = {"high": "qwen2.5:14b", "medium": "qwen2.5:7b", "low": "qwen2.5:3b", "tiny": "qwen2.5:1.5b"}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


FAKE_SERVER = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class FakeOllama:
    def __init__(self, port, installed=("qwen2.5:3b",), version="0.40.17", too_big=(), pull_steps=4,
                 pull_delay=0.05, pull_error=None, chat_error=None):
        self.port, self.installed, self.version = port, list(installed), version
        self.too_big, self.pull_steps, self.pull_delay = set(too_big), pull_steps, pull_delay
        self.pull_error, self.chat_error = pull_error, chat_error
        self.loaded, self.pulls, self.requests = None, [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/version" and outer.version:
                    return self._json(200, {"version": outer.version})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": n} for n in outer.installed]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": [{"name": outer.loaded, "size": 100, "size_vram": 0}]
                                            if outer.loaded else []})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append((self.path, body))
                model = body.get("model") or body.get("name")
                if self.path == "/api/pull":
                    outer.pulls.append(model)
                    self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
                    if outer.pull_error:
                        self.wfile.write((json.dumps({"error": outer.pull_error}) + "\n").encode()); return
                    total = 1000
                    for i in range(1, outer.pull_steps + 1):
                        time.sleep(outer.pull_delay)
                        self.wfile.write((json.dumps({"status": "downloading", "digest": "sha256:a",
                                                      "total": total, "completed": total * i // outer.pull_steps}) + "\n").encode())
                        self.wfile.flush()
                    outer.installed.append(model)
                    self.wfile.write((json.dumps({"status": "success"}) + "\n").encode()); return
                if model not in outer.installed:
                    return self._json(404, {"error": f"model '{model}' not found, try pulling it first"})
                if model in outer.too_big:
                    return self._json(500, {"error": "model requires more system memory (5.6 GiB) than is available (3.1 GiB)"})
                if outer.chat_error and self.path == "/api/chat" and body.get("messages", [{}])[-1].get("content") != "Reply with the single word: ready":
                    return self._json(outer.chat_error[0], {"error": outer.chat_error[1]})
                outer.loaded = model
                if self.path == "/api/generate":
                    return self._json(200, {"model": model, "done": True})
                if not body.get("stream"):
                    return self._json(200, {"message": {"content": "ready"}, "done": True, "eval_count": 8,
                                            "eval_duration": 100000000})
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
                for w in ("Very", "good,", "sir."):
                    self.wfile.write((json.dumps({"message": {"content": w + " "}, "done": False}) + "\n").encode())
                self.wfile.write((json.dumps({"done": True}) + "\n").encode())

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()

if __name__ == "__main__":          # the fake `ollama serve`
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    port = int(host.rsplit(":", 1)[1])
    FakeOllama(port, installed=json.loads(os.environ.get("FAKE_INSTALLED", '["qwen2.5:3b"]')))
    print("fake ollama serving on", port, flush=True)
    while True:
        time.sleep(1)
'''

_ns: dict = {}
exec(compile(FAKE_SERVER, "fake_ollama", "exec"), _ns)
FakeOllama = _ns["FakeOllama"]


class Env:
    """Temporarily set / unset environment variables."""

    def __init__(self, **kw):
        self.kw, self.saved = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fake_ollama_on_path(tmp: str, installed=("qwen2.5:3b",)) -> str:
    """A fake `ollama` executable: `ollama serve` runs the fake server on OLLAMA_HOST."""
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    srv = os.path.join(tmp, "fake_ollama_server.py")
    with open(srv, "w", encoding="utf-8") as fh:
        fh.write(FAKE_SERVER)
    exe = os.path.join(bindir, "ollama")
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write(f"#!/bin/sh\nexport FAKE_INSTALLED='{json.dumps(list(installed))}'\n"
                 f"exec '{sys.executable}' '{srv}'\n")
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
    return bindir


def warm_until(llm, pred, limit=15.0, step=0.1):
    t0 = time.time()
    while time.time() - t0 < limit:
        llm.ensure_warm("SYSTEM")
        if pred():
            return True
        time.sleep(step)
    return False


# =========================================================================== connecting
def test_a_system_proxy_no_longer_breaks_the_local_engine():
    import httpx
    port = free_port()
    fake = FakeOllama(port)
    try:
        with Env(HTTP_PROXY="http://127.0.0.1:1", http_proxy="http://127.0.0.1:1", ALL_PROXY="http://127.0.0.1:1",
                 NO_PROXY=None, no_proxy=None):
            try:
                httpx.get(f"http://127.0.0.1:{port}/api/version", timeout=3)
                proxied_fails = False
            except Exception:
                proxied_fails = True
            assert proxied_fails, "the old client should go to the (dead) proxy"
            llm = OllamaLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b")
            assert llm.health() and llm.chat("s", "hi") == "ready"
            assert svc.system_proxy() == "http://127.0.0.1:1"
    finally:
        fake.close()


def test_ollama_host_setting_is_honoured():
    assert svc.parse_ollama_host("0.0.0.0") == "http://127.0.0.1:11434"
    assert svc.parse_ollama_host(":11500") == "http://127.0.0.1:11500"
    assert svc.parse_ollama_host("http://192.168.0.5:11434") == "http://192.168.0.5:11434"
    port = free_port()
    fake = FakeOllama(port)
    try:
        with tempfile.TemporaryDirectory() as tmp, Env(OLLAMA_HOST=f"127.0.0.1:{port}"):
            llm = ResilientLLM(svc.DEFAULT_HOST, "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            assert llm.service.base == f"http://127.0.0.1:{port}"
            assert warm_until(llm, lambda: llm.state == "ready"), (llm.state, llm.problem)
    finally:
        fake.close()


def test_old_ollama_without_a_version_endpoint_is_recognised():
    port = free_port()
    fake = FakeOllama(port, version=None)
    try:
        assert svc.probe(f"http://127.0.0.1:{port}")["ok"]
    finally:
        fake.close()


def test_errors_are_classified():
    class R:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    class E(Exception):
        def __init__(self, code, text):
            super().__init__(f"HTTP {code}")
            self.response = R(code, text)

    assert classify(E(404, "model 'x' not found, try pulling it first"))[0] == "model_missing"
    assert classify(E(500, "model requires more system memory"))[0] == "memory"
    assert classify(ConnectionRefusedError("Connection refused"))[0] == "offline"


# =========================================================================== starting Ollama
def test_ollama_is_started_when_installed_but_not_running():
    with tempfile.TemporaryDirectory() as tmp:
        bindir = fake_ollama_on_path(tmp)
        port = free_port()
        with Env(PATH=bindir + os.pathsep + os.environ.get("PATH", ""), OLLAMA_HOST=None):
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=os.path.join(tmp, "data"),
                               log=lambda m: None)
            try:
                assert warm_until(llm, lambda: llm.state == "ready", limit=40), (llm.state, llm.problem)
                assert llm.service.started_by_jarvis and llm.service.exe.startswith(bindir)
                assert "".join(llm.chat_stream("s", "hello")).strip() == "Very good, sir."
            finally:
                os.system(f"pkill -f '{tmp}/fake_ollama_server.py' >/dev/null 2>&1")


def test_a_port_held_by_another_program_moves_the_engine():
    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        bindir = fake_ollama_on_path(tmp)
        port, alt = free_port(), free_port()
        squatter = ThreadingHTTPServer(("127.0.0.1", port), Other)
        threading.Thread(target=squatter.serve_forever, daemon=True).start()
        saved = svc.ALT_PORTS[:]
        svc.ALT_PORTS[:] = [alt]
        try:
            with Env(PATH=bindir + os.pathsep + os.environ.get("PATH", ""), OLLAMA_HOST=None):
                llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS,
                                   data_dir=os.path.join(tmp, "data"), log=lambda m: None)
                assert warm_until(llm, lambda: llm.state == "ready", limit=40), (llm.state, llm.problem)
                assert llm.service.base == f"http://127.0.0.1:{alt}"
                assert "in use" in llm.note and str(alt) in llm.note, llm.note
        finally:
            svc.ALT_PORTS[:] = saved
            squatter.shutdown()
            squatter.server_close()
            os.system(f"pkill -f '{tmp}/fake_ollama_server.py' >/dev/null 2>&1")


def test_not_installed_is_explained_and_offered():
    saved = svc.find_ollama
    svc.find_ollama = lambda: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{free_port()}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            llm.ensure_warm("SYSTEM")
            assert llm.state == "not_installed" and llm.problem["code"] == "not_installed"
            assert llm.problem["action"] == "install" and "install the language engine" in llm.problem["fix"]
            reply = llm.chat("s", "hello")
            assert "isn't installed" in reply and "check the language engine" in reply, reply
            assert "설치" in llm.chat("s", "안녕")
    finally:
        svc.find_ollama = saved


# =========================================================================== models
def test_missing_model_downloads_while_an_installed_one_is_used():
    port = free_port()
    fake = FakeOllama(port, installed=["qwen2.5:1.5b"], pull_steps=5, pull_delay=0.15)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            llm.ensure_warm("SYSTEM")
            assert llm.state == "ready" and llm.ollama.model == "qwen2.5:1.5b", (llm.state, llm.ollama.model)
            assert "using qwen2.5:1.5b while qwen2.5:3b downloads" in llm.note
            assert llm.progress is not None and fake.pulls == ["qwen2.5:3b"]
            assert warm_until(llm, lambda: llm.ollama.model == "qwen2.5:3b" and llm.state == "ready", limit=10)
            assert not llm.note and llm.progress is None
    finally:
        fake.close()


def test_no_model_at_all_reports_download_progress():
    port = free_port()
    fake = FakeOllama(port, installed=[], pull_steps=6, pull_delay=0.2)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            llm.ensure_warm("SYSTEM")
            assert llm.state == "pulling" and llm.problem["code"] == "model_missing"
            time.sleep(0.5)
            assert "downloading" in llm.chat("s", "hello")
            assert warm_until(llm, lambda: llm.state == "ready", limit=10), (llm.state, llm.problem)
            assert llm.problem is None and llm.model_name() == "qwen2.5:3b"
    finally:
        fake.close()


def test_a_model_too_big_for_the_memory_steps_down():
    port = free_port()
    fake = FakeOllama(port, installed=["qwen2.5:7b", "qwen2.5:3b"], too_big=["qwen2.5:7b"])
    chosen = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:7b", CANDS, data_dir=tmp, log=lambda m: None,
                               on_model_change=chosen.append)
            assert warm_until(llm, lambda: llm.state == "ready"), (llm.state, llm.problem, llm.note)
            assert llm.ollama.model == "qwen2.5:3b" and chosen == ["qwen2.5:3b"], (llm.ollama.model, chosen)
            assert "switched to qwen2.5:3b" in llm.note
    finally:
        fake.close()


def test_a_missing_model_during_a_reply_is_not_offline():
    port = free_port()
    fake = FakeOllama(port, chat_error=(404, "model 'qwen2.5:3b' not found, try pulling it first"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            assert warm_until(llm, lambda: llm.state == "ready")
            llm.chat("s", "hello")                         # 404 from Ollama
            assert llm.cached_online(), "a missing model is not 'offline'"
            assert llm._recheck_models
    finally:
        fake.close()


def test_download_failures_and_full_disks_are_explained():
    port = free_port()
    fake = FakeOllama(port, installed=[], pull_error="pull model manifest: file does not exist")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            assert warm_until(llm, lambda: (llm.problem or {}).get("code") == "pull_failed", limit=5)
            assert "couldn't download" in llm.problem["message"]
    finally:
        fake.close()
    port = free_port()
    fake = FakeOllama(port, installed=[])
    import agent.llm_ollama as mod
    saved = mod.shutil.disk_usage
    mod.shutil.disk_usage = lambda p: type("U", (), {"free": 300_000_000})()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            llm.ensure_warm("SYSTEM")
            assert llm.problem["code"] == "no_space" and "GB" in llm.problem["detail"] and not fake.pulls
    finally:
        mod.shutil.disk_usage = saved
        fake.close()


# =========================================================================== reporting
def test_check_the_language_engine_reports_the_state():
    from agent.skills import llmcheck
    port = free_port()
    fake = FakeOllama(port)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ResilientLLM(f"http://127.0.0.1:{port}", "qwen2.5:3b", CANDS, data_dir=tmp, log=lambda m: None)
            assert warm_until(llm, lambda: llm.state == "ready")
            llmcheck.set_provider(llm)
            r = llmcheck.check()
            assert r["ok"] and "is running" in r["speak"] and "qwen2.5:3b" in r["speak"], r["speak"]
            assert "실행 중" in r["data"]["speak_ko"]
    finally:
        llmcheck.set_provider(None)
        fake.close()


def test_server_status_carries_the_language_engine_report():
    import shutil
    from agent.server import JarvisServer
    tmp = tempfile.mkdtemp(prefix="jarvis_llm_")
    try:
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
        cfg["neural_core"]["active_neurons"] = 20000
        cfg["llm"]["host"] = f"http://127.0.0.1:{free_port()}"
        cfg["llm"]["manage_engine"] = False
        cp, pp = os.path.join(tmp, "c.json"), os.path.join(tmp, "p.json")
        json.dump(cfg, open(cp, "w", encoding="utf-8"))
        shutil.copy(os.path.join(ROOT, "config", "permissions.json"), pp)
        srv = JarvisServer(config_path=cp, permissions_path=pp, data_dir=os.path.join(tmp, "data"))
        st = srv.status()
        for key in ("llm_state", "llm_problem", "llm_note", "llm_progress", "llm_host", "llm_version",
                    "llm_proxy_bypassed"):
            assert key in st, key
        assert st["version"] == "1.5.0"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    print(f"\n{len(fns) - failed}/{len(fns)} language-engine tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
