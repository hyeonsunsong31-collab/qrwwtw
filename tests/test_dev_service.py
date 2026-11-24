"""
tests/test_dev_service.py -- the developer inside the real JARVIS server (v1.5.0).

    python tests/test_dev_service.py

The real server (language engine offline) with its DevService, where only the coding model is a
script: the HUD's ws_* messages; a request typed to JARVIS ("build me ...") going through the
words -> skill -> workspace queue -> coding loop -> verified product -> link and spoken notice;
LAN sharing and the permission asked before downloading the public-link tool; a stacked change and
undo; png -> ico through the convert skill (no coding model needed); refusals; pause / resume /
stop reaching the job; status in words; the words that start each of these.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_dev_agent import FakeLLM, _file  # noqa: E402


def _server(tmp):
    from agent.server import JarvisServer
    cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    cfg["neural_core"]["active_neurons"] = 20000
    cfg["llm"]["manage_engine"] = False
    cfg["llm"]["host"] = "http://127.0.0.1:9"
    cp, pp = os.path.join(tmp, "c.json"), os.path.join(tmp, "p.json")
    json.dump(cfg, open(cp, "w", encoding="utf-8"))
    shutil.copy(os.path.join(ROOT, "config", "permissions.json"), pp)
    return JarvisServer(config_path=cp, permissions_path=pp, data_dir=os.path.join(tmp, "data"))


class Harness:
    """A real server whose DevService runs on a background event loop with a scripted coder."""

    def __init__(self, coder=None, allow=True):
        self.tmp = tempfile.mkdtemp(prefix="jarvis_devsvc_")
        self.srv = _server(self.tmp)
        self.events, self.sent, self.said, self.asked = [], [], [], []
        self.allow = allow
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        srv = self.srv

        async def broadcast(ev):
            self.events.append(ev)

        async def send(ws, ev):
            self.sent.append(ev)

        async def notify(text, lang="en"):
            self.said.append(text)

        async def check(req):
            self.asked.append((req.category, req.detail, req.question))
            return self.allow
        srv.manager.broadcast = broadcast
        srv.manager.send = send
        srv.notify = notify
        srv.permissions.check = check
        self.coder = coder
        srv.dev.coder = lambda job=None: (self.coder, "qwen2.5-coder:3b", 8192)
        srv.dev._lend_if_tight = lambda *a: None
        srv.dev.start(self.loop)

    def call(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def msg(self, **m):
        self.call(self.srv.dev.handle(m, None))
        return self.sent[-1] if self.sent else None

    def wait(self, pred, timeout=120, what="it"):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {what}; events: {[e.get('type') for e in self.events][-12:]}")

    def products(self):
        return [e for e in self.events if e.get("type") == "dev_product"]

    def close(self):
        try:
            self.srv.dev.stop()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            shutil.rmtree(self.tmp, ignore_errors=True)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body><main><h1>{title}</h1><p id="value">0</p><button id="plus" type="button">+</button></main>
<script src="app.js"></script></body></html>"""
JS = """const v = document.getElementById('value');
let n = 0;
document.getElementById('plus').addEventListener('click', () => { n += 1; v.textContent = n; });"""


def _web_coder(title):
    plan = {"title": title, "kind": "web", "template": "web-static",
            "criteria": ["The page shows 0 when it opens", "A '+' button adds one"], "plan": ["page + script"]}
    checks = [{"criterion": 1, "steps": [{"action": "expect_text", "target": "body", "value": "0"}]},
              {"criterion": 2, "steps": [{"action": "click", "target": 'button:has-text("+")'},
                                         {"action": "expect_text", "target": "body", "value": "1"}]}]
    reply = (_file("index.html", PAGE.format(title=title), "html") + _file("app.js", JS, "js") +
             'CONFIG: {"kind": "web", "run": "static"}\nDONE\n')
    return FakeLLM(plan, checks, [reply], review=lambda u: {"verdicts": []})


def _get(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ tests
def test_hud_messages_manage_workspaces():
    h = Harness()
    try:
        a = h.msg(type="ws_create", name="Snake game")["workspace"]
        b = h.msg(type="ws_create", name="Bakery site")["workspace"]
        st = h.msg(type="ws_list")
        assert st["type"] == "ws_state" and {w["name"] for w in st["workspaces"]} == {"Snake game", "Bakery site"}
        h.msg(type="ws_select", id=a["id"])
        assert h.srv.dev.store.selected() == a["id"]
        h.msg(type="ws_rename", id=b["id"], name="Bakery")
        assert h.srv.dev.store.get(b["id"])["name"] == "Bakery"
        d = h.msg(type="ws_get", id=a["id"])
        assert d["type"] == "ws_detail" and d["detail"]["id"] == a["id"] and "request_list" in d["detail"], d
        h.msg(type="ws_delete", id=b["id"])
        assert h.srv.dev.store.get(b["id"]) is None
        assert any(e.get("type") == "ws_state" for e in h.events), "the HUD hears about every change"
        bad = h.msg(type="ws_request", text="crack this game so it runs without a licence key")
        assert bad["type"] == "ws_error" and "licen" in bad["message"].lower(), bad
    finally:
        h.close()


def test_a_typed_request_becomes_a_checked_product_with_a_link():
    h = Harness(_web_coder("Counter"))
    try:
        out = h.call(h.srv.orch.handle_command("build me a counter web page with a plus button"), timeout=60)
        p = h.wait(lambda: h.products(), timeout=240, what="the product")[-1]["product"]
        assert p["web"] and p["links"]["local"].startswith("http://127.0.0.1:"), p
        html = _get(p["links"]["local"])
        assert "<h1>Counter</h1>" in html, html[:300]
        assert any("ready and checked" in s and p["links"]["local"] in s for s in h.said), h.said
        ws = h.srv.dev.store.get(h.srv.dev.store.selected())
        req = h.srv.dev.store.requests(ws["id"])[-1]
        assert req["status"] == "done" and req["text"].startswith("build me a counter"), req
        rec = h.srv.recorder.recent("files", 10)
        assert any(r["original"] == os.path.abspath(p["path"]) and os.path.exists(r["stored"]) for r in rec), \
            "the product is registered with the recorder"
        # the same product on the local network, and the public link asks before downloading its tool
        lan = h.srv.dev.share("lan")
        assert lan.get("lan") and "<h1>Counter</h1>" in _get(lan["lan"]), lan
        h.srv.dev.links.cloudflared = lambda: None
        h.allow = False
        try:
            h.srv.dev.share("public")
            raise AssertionError("a public link needs permission to download its tool")
        except PermissionError:
            pass
        assert any(c == "install_software" and "cloudflared" in d for c, d, q in h.asked), h.asked
        h.allow = True
        assert "lan" not in h.srv.dev.share("stop")
        n = len(h.events)
        h.call(h.srv.orch.handle_command("how is the build going"), timeout=60)
        replies = [e["text"] for e in h.events[n:] if e.get("type") == "transcript" and e.get("role") == "assistant"]
        assert any("not building anything" in r and "1 workspace" in r for r in replies), replies
    finally:
        h.close()


def test_a_stacked_change_and_undo():
    h = Harness(_web_coder("Counter"))
    try:
        r1 = h.srv.dev.request("make a counter page with a + button", new=True)
        wid = r1["workspace"]["id"]
        h.wait(lambda: len(h.products()) >= 1, timeout=240, what="the first product")
        h.coder = _web_coder("Bigger Counter")
        h.coder.plan = dict(h.coder.plan, criteria=["The heading says Bigger Counter"])
        h.coder.checks = [{"criterion": 3, "steps": [{"action": "expect_text", "target": "h1", "value": "Bigger Counter"}]}]
        h.srv.dev.request("rename the heading to Bigger Counter", workspace=wid)
        h.wait(lambda: len(h.products()) >= 2, timeout=240, what="the second product")
        reqs = h.srv.dev.store.requests(wid)
        assert [r["status"] for r in reqs] == ["done", "done"], [(r["status"], r.get("summary")) for r in reqs]
        assert reqs[1]["checklist"][0].get("previous"), "the earlier criteria are checked again"
        project = h.srv.dev.store.paths(wid)["project"]
        assert "Bigger Counter" in open(os.path.join(project, "index.html")).read()
        r = h.msg(type="ws_undo", id=wid)
        assert r["type"] == "ws_undone" and r["how"] in ("reverted", "restored"), r
        assert "Bigger Counter" not in open(os.path.join(project, "index.html")).read()
        assert h.srv.dev.store.requests(wid)[1]["status"] == "undone"
        assert h.srv.dev.store.get(wid)["product"]["title"] == "Counter", "the product is the earlier one again"
    finally:
        h.close()


def test_png_to_ico_through_the_convert_skill_and_words():
    from agent.nlu import parse_intent
    from agent.skills import devskills
    h = Harness()                     # no coding model: a ready-made converter does it
    try:
        from PIL import Image
        src = os.path.join(h.tmp, "logo.png")
        Image.new("RGBA", (256, 256), (200, 40, 40, 255)).save(src)
        mine = h.srv.dev.store.create("My game")
        it = parse_intent(f"convert {src} to ico")
        assert it and it.name == "dev_convert" and it.args["fmt"] == "ico" and it.args["src"] == src, it
        r = devskills.dev_convert(**it.args)
        assert r["ok"], r
        p = h.wait(lambda: h.products(), timeout=60, what="the icon")[-1]["product"]
        assert p["path"].endswith("logo.ico") and os.path.exists(p["path"]), p
        from dev.files import detect
        assert detect(p["path"])["kind"] == "ico"
        assert any("ready and checked" in s and "logo.ico" in s for s in h.said), h.said
        conv = h.srv.dev.store.find("Conversions")
        assert conv and h.srv.dev.store.get(conv["id"])["product"]["path"] == p["path"], "conversions have their own slot"
        assert h.srv.dev.store.selected() == mine["id"] and not h.srv.dev.store.get(mine["id"]).get("product"), \
            "the workspace you work in stays selected and keeps its product"
        # "convert it to png": the file attached last (in any workspace) of the request before
        it = parse_intent("convert this ico file to png")
        assert it.args["src"] == "it:ico", it.args
        r = devskills.dev_convert(**it.args)
        assert r["ok"], r
    finally:
        h.close()


def test_pause_resume_and_stop_reach_the_job():
    gate = threading.Event()

    def slow(state):
        gate.wait(0.3)
        return "Let me think about it."            # no directives: the loop keeps going

    coder = FakeLLM({"title": "Slow", "kind": "cli", "template": "none", "criteria": ["it prints hi"], "plan": []},
                    [{"criterion": 1, "args": "", "expect": ["hi"]}], [slow])
    h = Harness(coder)
    try:
        r = h.srv.dev.request("make a command-line tool that prints hi", new=True)
        wid, rid = r["workspace"]["id"], r["request"]["id"]
        get = lambda: h.srv.dev.store.get_request(wid, rid)["status"]
        h.wait(lambda: get() == "working", timeout=60, what="the job to start")
        h.msg(type="ws_pause", id=wid)
        assert get() == "paused" and h.srv.dev.scheduler.busy()["paused"]
        h.msg(type="ws_resume", id=wid)
        assert get() == "working"
        h.msg(type="ws_stop", id=wid)
        h.wait(lambda: get() == "stopped", timeout=60, what="the job to stop")
        assert any("stopped" in s.lower() for s in h.said), h.said
        assert h.srv.dev.scheduler.busy() is None
    finally:
        gate.set()
        h.close()


def test_the_words_for_the_developer():
    from agent.nlu import parse_intent
    cases = {
        "build me a snake game": ("dev_request", {"text": "build me a snake game"}),
        "make a website for my bakery": ("dev_request", None),
        "add a high score table to the game": ("dev_request", None),
        "뱀 게임 만들어 줘": ("dev_request", None),
        "convert this ini file to an exe": ("dev_convert", {"src": "it:ini", "fmt": "exe"}),
        "new workspace called Portfolio": ("ws_new", {"name": "Portfolio"}),
        "switch to workspace portfolio": ("ws_switch", {"name": "portfolio"}),
        "how is the build going": ("ws_status", {}),
        "share it publicly": ("ws_share", {"mode": "public"}),
        "share it on my network": ("ws_share", {"mode": "lan"}),
        "stop sharing": ("ws_share", {"mode": "stop"}),
        "pause the build": ("ws_pause", {}),
        "carry on": ("ws_resume", {}),
        "stop coding": ("ws_stop", {}),
        "undo the last change": ("ws_undo", {}),
        "show my workspaces": ("open_workspaces", {}),
        "start a new project called Portfolio": ("ws_new", {"name": "Portfolio"}),
    }
    for text, (name, args) in cases.items():
        it = parse_intent(text)
        assert it is not None and it.name == name, (text, it)
        if args:
            for k, v in args.items():
                assert it.args.get(k) == v, (text, it.args)
    # no app is named: never "close the app called 'the'"
    for text in ("close the app", "close this window", "close the program"):
        it = parse_intent(text)
        assert it is None or it.name != "close_app", (text, it)
    assert parse_intent("close the spotify app").args["app"] == "spotify"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    only = sys.argv[1:]
    if only:
        fns = [f for f in fns if any(o in f.__name__ for o in only)]
    failed = 0
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  FAIL {fn.__name__} ({time.time() - t0:.1f}s)")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} developer service tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
