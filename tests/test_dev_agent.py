"""
tests/test_dev_agent.py -- the never-stop coding loop, with a scripted coding model (v1.5.0).

    python tests/test_dev_agent.py

A fake model answers each kind of question the loop asks (the plan, the examiner's checks, a coding
step, an audit, the review) from a script, while everything else is real: the workspace, the
sandboxed terminal, the project's own Python environment, Git history, the verifier (and the
browser for web checks). Covered: plan -> checks written from the request -> code -> verify ->
done with a delivered product; fixing after a failure; a malformed examiner check re-written
instead of blamed on the program; an audit replacing a wrong check; stall escalation and starting
over from the request's base; a review that objects only with a failing proof; refusals; tools
mode and switching protocol; a stacked adjustment that must keep the earlier behaviour working;
the text-directive parser and tool-call mapping.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dev.workspaces import WorkspaceStore  # noqa: E402
from dev.history import History  # noqa: E402
from dev.agent import DevAgent, Job  # noqa: E402
from dev.tools import Sandbox  # noqa: E402
from dev.verify import Verifier  # noqa: E402
from dev import protocol  # noqa: E402


# ------------------------------------------------------------------ a scripted coding model
class FakeLLM:
    """Answers by what is asked. `steps` are the coding replies in order (the last one repeats); each
    is a string (text mode), a (text, calls) pair (tools mode) or a callable(state) returning one."""

    def __init__(self, plan, checks, steps, review=None, audit=None, predict=None):
        self.predict = predict
        self.plan = plan
        self.checks = checks
        self.steps = list(steps)
        self.review = review or (lambda user: {"verdicts": []})
        self.audit = audit
        self.calls = []              # (kind, system, user)
        self.states = []             # every state message a coding step saw
        self.temps = []

    def _next(self, user):
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        return step(user) if callable(step) else step

    def chat_json(self, system, user, schema, options=None, timeout=None):
        props = (schema or {}).get("properties", {})
        if "criteria" in props:
            kind, out = "plan", self.plan
        elif "verdict" in props:
            kind = "audit"
            out = self.audit(user) if self.audit else {"verdict": "program_wrong", "reason": "it does not do it"}
        elif "verdicts" in props:
            kind, out = "review", self.review(user)
        elif "checks" in props:
            kind = "checks"
            out = {"checks": self.checks(user) if callable(self.checks) else self.checks}
        else:
            raise AssertionError(f"unexpected question: {list(props)}")
        self.calls.append((kind, system, user))
        return out

    def chat(self, system, user, history=None, options=None):
        """The examiner's one-action questions (see Examiner.settle_numbers): no opinion by default."""
        self.calls.append(("predict", system, user))
        return self.predict(system, user) if self.predict else ""

    def chat_stream(self, system, user, history, options=None):
        self.calls.append(("step", system, user))
        self.states.append(user)
        self.temps.append((options or {}).get("temperature"))
        text = self._next(user)
        for i in range(0, len(text), 40):
            yield text[i:i + 40]

    def chat_stream_events(self, system, user, hist, params, tools):
        self.calls.append(("tools-step", system, user))
        self.states.append(user)
        text, calls = self._next(user)
        if text:
            yield ("text", text)
        if calls:
            yield ("tools", calls)

    def kinds(self):
        return [k for k, _s, _u in self.calls]


class Rig:
    """A real workspace, sandbox, history and verifier around one request."""

    def __init__(self, llm, request, model="qwen2.5-coder:3b", mode="auto", root=None, store=None, wid=None):
        self.root = root or tempfile.mkdtemp(prefix="jarvis_agenttest_")
        self.store = store or WorkspaceStore(os.path.join(self.root, "workspaces"))
        self.wid = wid or self.store.create("Test")["id"]
        self.req = self.store.add_request(self.wid, request)
        self.rid = self.req["id"]
        p = self.store.paths(self.wid)
        self.project = p["project"]
        self.logs = []
        self.events = []
        log = lambda kind, text: self.logs.append((kind, text))
        self.sb = Sandbox(p["root"], p["project"], ask=lambda *a, **k: True, log=log, data_dir=self.root)
        self.hist = History(p["project"], os.path.join(p["root"], "snapshots"))
        self.base = self.hist.begin(self.rid)
        self.store.update_request(self.wid, self.rid, base=self.base)
        ver = Verifier(self.sb, self.store.request_dir(self.wid, self.rid), log=log)
        self.job = Job()
        self.llm = llm
        self.agent = DevAgent(llm, self.store, self.wid, self.rid, self.sb, ver, self.job, self.events.append,
                              cfg={}, history=self.hist, log=log, toolchains="python", num_ctx=8192,
                              protocol_mode=mode, model_name=model)

    def run(self, limit=240.0):
        out = {}
        t = threading.Thread(target=lambda: out.update(self.agent.run()), daemon=True)
        t.start()
        t.join(limit)
        if t.is_alive():
            self.job.stop()
            t.join(30)
            raise AssertionError(f"the loop did not finish within {limit:.0f}s; last logs: {self.logs[-6:]}")
        self.sb.stop_all()
        return out

    @property
    def request(self):
        return self.store.get_request(self.wid, self.rid)

    def logged(self, kind, pattern=""):
        return [t for k, t in self.logs if k == kind and re.search(pattern, t)]

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _file(path, code, lang="python"):
    return f"FILE: {path}\n```{lang}\n{code.strip()}\n```\n"


CONV_PLAN = {"title": "Temperature converter", "kind": "cli", "template": "none",
             "criteria": ["`100 --to f` prints 212", "`0 --to f` prints 32"], "plan": ["write conv.py"]}
CONV_CHECKS = [{"criterion": 1, "args": "100 --to f", "expect": ["212"]},
               {"criterion": 2, "args": "0 --to f", "expect": ["32"]}]
CONV_GOOD = _file("conv.py", """
import sys
if len(sys.argv) < 2:
    print("usage: conv.py <celsius> --to f")
    sys.exit(0)
c = float(sys.argv[1])
print(c * 9 / 5 + 32)
""") + 'CONFIG: {"kind": "cli", "run": "python conv.py"}\nDONE\n'
CONV_WRONG = _file("conv.py", """
import sys
if len(sys.argv) < 2:
    print("usage: conv.py <celsius> --to f")
    sys.exit(0)
c = float(sys.argv[1])
print(c * 5 / 9 + 32)
""") + 'CONFIG: {"kind": "cli", "run": "python conv.py"}\nDONE\n'
ALL_MET = lambda user: {"verdicts": [{"criterion": int(n), "met": True, "why": "shown"}
                                     for n in re.findall(r"^(\d+)\. ", user, re.M)]}


# ------------------------------------------------------------------ the loop
def test_plan_checks_code_verify_and_deliver():
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [CONV_GOOD], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert [k for k in llm.kinds() if k != "predict"][:3] == ["plan", "checks", "step"], \
            "the checks are written from the request BEFORE any code"
        assert llm.kinds()[-1] == "review"
        r = rig.request
        assert [c["status"] for c in r["checklist"]] == ["pass", "pass"] and r["iterations"] == 1
        assert all(c["checks"][0]["by"] == "jarvis" for c in r["checklist"])
        first = llm.states[0]
        assert "REQUEST: Make a command-line" in first and "JARVIS: cli '100 --to f' expect ['212']" in first, first[:1500]
        assert "CODE:" in llm.calls[-1][2] and "conv.py" in llm.calls[-1][2], "the review reads the code too"
        prod = r["product"]
        assert prod["path"].endswith(".zip") and os.path.exists(prod["path"]), prod
        assert r["commit"] and r["commit"] != rig.base, "the finished request is one history commit"
        assert "conv.py" in rig.hist.changed_files(rig.base), rig.hist.changed_files(rig.base)
        phases = [e.get("phase") for e in rig.events if e.get("type") == "dev_progress"]
        assert "planned" in phases and "checked" in phases and "finishing" in phases, phases
    finally:
        rig.close()


def test_a_failure_is_shown_to_the_model_and_fixed():
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [CONV_WRONG, CONV_GOOD], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        res = rig.run()
        assert res["status"] == "done" and rig.request["iterations"] == 2, (res, rig.logs[-8:])
        second = llm.states[1]
        assert "LAST CHECK BY JARVIS" in second and "expected ['212']" in second and "87.5" in second, second[-3000:]
        assert "Fix what failed above" in second
    finally:
        rig.close()


def test_refused_requests_never_reach_the_model():
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [CONV_GOOD])
    rig = Rig(llm, "remove the license check from setup.exe so it runs without a key")
    try:
        res = rig.run()
        assert res["status"] == "refused" and not llm.calls, res
        assert rig.request["status"] == "failed" and rig.request["error"]
    finally:
        rig.close()


def test_stalls_escalate_and_finally_start_over_from_the_base():
    """The same failure again and again: guidance at 3 and 6, a temperature change (a 3B model stays
    with plain directives), and after a long stall the project is put back to where the request
    began and scaffolded again -- then the work goes on until it passes."""
    n = {"i": 0}

    def step(state):
        n["i"] += 1
        if n["i"] == 1:
            return CONV_WRONG + _file("junk.txt", "left over", "text")
        return CONV_GOOD if n["i"] > 26 else CONV_WRONG

    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [step], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        res = rig.run(limit=400)
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert rig.logged("stall", "same failure 3 times") and rig.logged("stall", "same failure 6 times")
        assert rig.logged("protocol", "staying with plain directives"), "a 3B model is not switched to tools"
        assert rig.agent.mode == "text"
        assert rig.logged("stall", "started the work over"), [t for k, t in rig.logs if k == "stall"]
        assert not os.path.exists(os.path.join(rig.project, "junk.txt")), "starting over removes what the request added"
        assert len(set(t for t in llm.temps if t is not None)) >= 2, llm.temps
        assert any("IMPORTANT: " in s and "from scratch" in s for s in llm.states), "the model is told to rewrite"
    finally:
        rig.close()


def test_the_review_objects_only_with_a_failing_proof():
    """Everything passes, then the examiner's review doubts criterion 1 and proves it with a check
    that fails (negative numbers): the work goes on. A doubt whose proof passes is a false alarm."""
    plan = dict(CONV_PLAN, criteria=["`100 --to f` prints 212", "`-40 --to f` prints -40"])
    checks_first = [{"criterion": 1, "args": "100 --to f", "expect": ["212"]},
                    {"criterion": 2, "args": "", "expect": ["usage"]}]                    # a weak first check
    proof = [{"criterion": 2, "args": "-40 --to f", "expect": ["-40"]}]
    abs_bug = _file("conv.py", """
import sys
args = [a for a in sys.argv[1:] if a != "--"]
if not args:
    print("usage: conv.py <celsius> --to f")
    sys.exit(0)
try:
    c = float(args[0])
except ValueError:
    print("usage: conv.py <celsius> --to f")
    sys.exit(0)
print(abs(c) * 9 / 5 + 32)
""") + 'CONFIG: {"kind": "cli", "run": "python conv.py"}\nDONE\n'
    fixed = abs_bug.replace("abs(c)", "c")
    reviews = {"n": 0}

    def review(user):
        reviews["n"] += 1
        if reviews["n"] == 1:
            return {"verdicts": [{"criterion": 1, "met": True, "why": "ok"},
                                 {"criterion": 2, "met": False, "why": "negative input is turned positive (abs)"}]}
        return ALL_MET(user)

    asked = {"n": 0}

    def checks(user):
        asked["n"] += 1
        return checks_first if asked["n"] == 1 else proof

    llm = FakeLLM(plan, checks, [abs_bug, fixed], review=review)
    rig = Rig(llm, "Make a command-line temperature converter that also handles negative temperatures")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert rig.logged("examiner", "the review found real problems"), rig.logs[-12:]
        crit2 = rig.request["checklist"][1]
        assert any(k.get("review") for k in crit2["checks"]), "the proof stays as a check"
        assert any("does not do what was asked" in s for s in llm.states[1:]), "the coder is told what the review found"
        assert rig.request["iterations"] == 2
    finally:
        rig.close()
    # a doubt that its own proof does not confirm is ignored (counted as a false alarm)
    asked2 = {"n": 0}

    def checks2(user):
        asked2["n"] += 1
        return CONV_CHECKS if asked2["n"] == 1 else [{"criterion": 1, "args": "100 --to f", "expect": ["212"]}]

    llm = FakeLLM(CONV_PLAN, checks2, [CONV_GOOD],
                  review=lambda u: {"verdicts": [{"criterion": 1, "met": False, "why": "not sure"}]})
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        res = rig.run()
        assert res["status"] == "done" and rig.agent.false_alarms >= 1, (res, rig.logs[-6:])
        assert rig.logged("examiner", "did not hold up")
    finally:
        rig.close()


def test_tools_mode_and_switching_when_tools_are_not_used():
    calls_good = [{"name": "write_file", "arguments": {"path": "conv.py", "content": CONV_GOOD.split("```python\n")[1].split("```")[0]}},
                  {"name": "set_config", "arguments": {"kind": "cli", "run": "python conv.py"}},
                  {"name": "done", "arguments": {"summary": "converter"}}]
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [("I'll write it.", calls_good)], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212", model="qwen2.5-coder:14b")
    try:
        assert rig.agent.mode == "tools", "a 14B model uses native tools"
        res = rig.run()
        assert res["status"] == "done" and llm.kinds().count("tools-step") == 1, (res, rig.logs[-6:])
    finally:
        rig.close()
    # three replies without tool calls -> plain directives (a 14B model may switch)
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [("Sure, here is my plan.", []), ("Sure, here is my plan.", []),
                                           ("Sure, here is my plan.", []), CONV_GOOD], review=ALL_MET)

    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212", model="qwen2.5-coder:14b")
    try:
        res = rig.run()
        assert res["status"] == "done" and rig.agent.mode == "text", (res, rig.agent.mode, rig.logs[-6:])
        assert rig.logged("protocol", "switching from tools to text")
        assert llm.kinds().count("tools-step") == 3 and llm.kinds().count("step") >= 1
    finally:
        rig.close()


COUNTER_PLAN = {"title": "Counter", "kind": "web", "template": "web-static",
                "criteria": ["The page shows 0 when it opens", "A '+' button adds one", "A '-' button subtracts one"],
                "plan": ["write index.html and app.js"]}
COUNTER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Counter</title></head>
<body><main><p id="value">0</p>
<button id="plus" type="button">+</button><button id="minus" type="button">-</button></main>
<script src="app.js"></script></body></html>"""
COUNTER_JS = """const v = document.getElementById('value');
let n = 0;
document.getElementById('plus').addEventListener('click', () => { n += 1; v.textContent = n; });
document.getElementById('minus').addEventListener('click', () => { n -= 1; v.textContent = n; });"""
COUNTER_REPLY = (_file("index.html", COUNTER_HTML, "html") + _file("app.js", COUNTER_JS, "js") +
                 'CONFIG: {"kind": "web", "run": "static"}\nDONE\n')
PLUS, MINUS = 'button:has-text(\\"+\\")', 'button:has-text(\\"-\\")'


def test_a_malformed_examiner_check_is_rewritten_not_blamed_on_the_program():
    good = [{"criterion": 1, "steps": [{"action": "expect_text", "target": "body", "value": "0"}]},
            {"criterion": 2, "steps": [{"action": "click", "target": 'button:has-text("+")'},
                                       {"action": "expect_text", "target": "body", "value": "1"}]},
            {"criterion": 3, "steps": [{"action": "click", "target": 'button:has-text("-")'},
                                       {"action": "expect_text", "target": "body", "value": "-1"}]}]
    broken = [dict(good[0]), dict(good[1]),
              {"criterion": 3, "steps": [{"action": "click", "target": 'button:has-text("-"'},      # unbalanced
                                         {"action": "expect_text", "target": "body", "value": "-1"}]}]
    asked = {"n": 0}

    def checks(user):
        asked["n"] += 1
        return broken if asked["n"] == 1 else [good[2]]

    llm = FakeLLM(COUNTER_PLAN, checks, [COUNTER_REPLY], review=ALL_MET)
    rig = Rig(llm, "Make a web page with a counter starting at 0, a + button and a - button")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert rig.logged("examiner", "re-writing malformed checks for criteria \\[3\\]"), rig.logs
        assert asked["n"] >= 2
        assert all("[broken check]" not in s and "not a valid selector" not in s for s in llm.states), \
            "the coder never sees the examiner's mistake as a program failure"
        c3 = rig.request["checklist"][2]["checks"]
        assert json.dumps(c3).count('text-is(\\"-\\")') >= 1 and c3[0]["by"] == "jarvis", c3
        assert rig.request["iterations"] == 1, "the program was right the first time"
    finally:
        rig.close()


def test_an_audit_cannot_overrule_numbers_the_vote_confirmed():
    """The program stops "-" at 0; the confirmed check expects -1. The auditor (a weak model) would call
    the check wrong -- it is never asked: the coder is told the program is wrong, and fixes it."""
    clamp = COUNTER_REPLY.replace("n -= 1;", "n = Math.max(0, n - 1);")
    checks = [{"criterion": 1, "steps": [{"action": "expect_text", "target": "body", "value": "0"}]},
              {"criterion": 2, "steps": [{"action": "click", "target": 'button:has-text("+")'},
                                         {"action": "expect_text", "target": "body", "value": "1"}]},
              {"criterion": 3, "steps": [{"action": "click", "target": 'button:has-text("-")'},
                                         {"action": "expect_text", "target": "body", "value": "-1"}]}]
    audits = []
    llm = FakeLLM(COUNTER_PLAN, checks, [clamp, clamp, clamp, COUNTER_REPLY], review=ALL_MET, predict=_counter_mind(),
                  audit=lambda u: audits.append(u) or {"verdict": "check_wrong", "reason": "it stops at 0",
                                                       "steps": [{"action": "click", "target": 'button:has-text("-")'},
                                                                 {"action": "expect_text", "target": "body", "value": "0"}]})
    rig = Rig(llm, "Make a web page with a counter starting at 0, a + button and a - button")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert not audits, "the auditor is not asked about confirmed numbers"
        assert rig.logged("examiner", "criterion 3: the numbers its check expects are confirmed"), rig.logs[-12:]
        assert any("confirms criterion 3 is NOT met" in st for st in llm.states), "the coder is told"
        c3 = rig.request["checklist"][2]["checks"][0]
        assert c3["steps"][-1] == {"expect_text": "-1"} and c3.get("confirmed"), c3
    finally:
        rig.close()


def test_an_element_the_request_names_is_never_audited_away():
    """The request names a "+5" button; the program does not have it yet. The auditor (a weak model)
    would call the check wrong "because the page only has other buttons" -- it is not asked."""
    plan = {"title": "Counter", "kind": "web", "template": "none", "criteria": ["There is a '+5' button that adds five"],
            "plan": []}
    checks = [{"criterion": 1, "steps": [{"action": "click", "target": 'button:has-text("+5")'},
                                         {"action": "expect_text", "target": "body", "value": "5"}]}]
    no_button = COUNTER_REPLY
    with_button = (COUNTER_REPLY.replace('<button id="minus" type="button">-</button>',
                                         '<button id="minus" type="button">-</button><button id="five" type="button">+5</button>')
                   .replace("v.textContent = n; });\n```", "v.textContent = n; });\n"
                            "document.getElementById('five').addEventListener('click', () => { n += 5; v.textContent = n; });\n```"))
    assert "id=\"five\"" in with_button and "n += 5" in with_button
    audits = []
    llm = FakeLLM(plan, checks, [no_button, no_button, no_button, with_button], review=ALL_MET,
                  audit=lambda u: audits.append(u) or {"verdict": "check_wrong", "reason": "the page only has + and -",
                                                       "steps": [{"action": "expect_text", "target": "body", "value": "0"}]})
    rig = Rig(llm, "Add a +5 button that adds five to the number")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert not audits, "the auditor is not asked about an element the request names"
        assert rig.logged("examiner", "criterion 1: the program has no '\\+5' yet"), rig.logs[-12:]
        assert any("has no element showing '+5'" in st for st in llm.states), "the coder is told exactly what to add"
        assert any("the page's buttons show: '+', '-'" in st for st in llm.states), "and which buttons the page has"
    finally:
        rig.close()


def test_an_audit_replaces_a_check_that_tests_the_wrong_thing():
    wrong = [{"criterion": 1, "steps": [{"action": "expect_visible", "target": "#counter-display"}]},     # invented id
             {"criterion": 2, "steps": [{"action": "click", "target": 'button:has-text("+")'},
                                        {"action": "expect_text", "target": "body", "value": "1"}]},
             {"criterion": 3, "steps": [{"action": "click", "target": 'button:has-text("-")'},
                                        {"action": "expect_text", "target": "body", "value": "-1"}]}]
    audits = []

    def audit(user):
        audits.append(user)
        return {"verdict": "check_wrong", "reason": "the request never named #counter-display",
                "steps": [{"action": "expect_text", "target": "body", "value": "0"}]}

    llm = FakeLLM(COUNTER_PLAN, wrong, [COUNTER_REPLY, "DONE\n"], review=ALL_MET, audit=audit)
    rig = Rig(llm, "Make a web page with a counter starting at 0, a + button and a - button")
    try:
        res = rig.run()
        assert res["status"] == "done", (res, rig.logs[-8:])
        assert len(audits) == 1 and "#counter-display" in audits[0], audits
        assert rig.logged("examiner", "criterion 1: its check was wrong"), rig.logs[-10:]
        c1 = rig.request["checklist"][0]["checks"][0]
        assert c1["steps"] == [{"expect_text": "0"}] and c1["by"] == "jarvis", c1
    finally:
        rig.close()


def test_a_stacked_adjustment_keeps_what_already_works():
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [CONV_GOOD], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        assert rig.run()["status"] == "done"
        # the second request of the same workspace: add Celsius output
        plan2 = {"title": "Temperature converter", "kind": "cli", "template": "none",
                 "criteria": ["`212 --to c` prints 100"], "plan": ["add --to c"]}
        checks2 = [{"criterion": 3, "args": "212 --to c", "expect": ["100"]}]
        only_c = _file("conv.py", """
import sys
if len(sys.argv) < 2:
    print("usage: conv.py <degrees> --to c")
    sys.exit(0)
print((float(sys.argv[1]) - 32) * 5 / 9)
""") + "DONE\n"
        both = _file("conv.py", """
import sys
if len(sys.argv) < 2:
    print("usage: conv.py <degrees> --to f|c")
    sys.exit(0)
x = float(sys.argv[1])
to = sys.argv[3] if len(sys.argv) > 3 else "f"
print((x - 32) * 5 / 9 if to == "c" else x * 9 / 5 + 32)
""") + "DONE\n"
        llm2 = FakeLLM(plan2, checks2, [only_c, both], review=ALL_MET)
        rig2 = Rig(llm2, "Also let it convert to Celsius with --to c", root=rig.root, store=rig.store, wid=rig.wid)
        res = rig2.run()
        assert res["status"] == "done", (res, rig2.logs[-8:])
        crit = rig2.request["checklist"]
        assert [c["text"] for c in crit][:2] == CONV_PLAN["criteria"] and crit[0].get("previous"), crit
        assert "ADJUSTMENT of an existing product" in llm2.calls[0][2], "the plan knows what already exists"
        assert "keep everything that already works working" in llm2.states[0]
        assert rig2.request["iterations"] == 2 and "FAIL" in llm2.states[1] and "criterion 1" in llm2.states[1], \
            "breaking the earlier behaviour is a failure the coder must fix"
        assert len([r for r in rig.store.requests(rig.wid) if r["status"] == "done"]) == 2
    finally:
        rig.close()


def _counter_mind(wrong_minus_from_zero=False, quiet=False):
    """A scripted model answering the examiner's one-action questions about a counter: + adds one,
    - subtracts one, Reset gives 0. It can be made to slip like a 3B model on one question."""
    def predict(system, user):
        if quiet:
            return "I am not sure."
        m = re.search(r"The page shows (-?\d+)\. The user clicks the '([^']+)' button once", user)
        if not m:
            return "0" if "just been opened" in user else ""
        n, label = int(m.group(1)), m.group(2)
        after = {"+": n + 1, "-": n - 1, "Reset": 0}.get(label)
        if after is None:
            return "hm"
        if "calculation" in user:
            return {"+": f"{n} + 1", "-": f"{n} - 1", "Reset": "0"}[label]
        if wrong_minus_from_zero and label == "-" and n == 0 and "number only" in system:
            return "1"                                    # one of the three answers slips
        return f"Working it out: {after}\n{after}" if "last line" in user else str(after)
    return predict


def test_short_labels_are_matched_exactly_and_stacked_changes_see_the_whole_product():
    from dev.examiner import Examiner, exact_short
    assert exact_short('button:has-text("+")') == 'button:text-is("+")', "\"+\" must not also find a \"+5\" button"
    assert exact_short('button:has-text("Reset")') == 'button:has-text("Reset")'
    chk = Examiner._to_check({"steps": [{"action": "click", "target": "+"},
                                        {"action": "expect_text", "target": "body", "value": "1"}]}, "web")
    assert chk["steps"][0] == {"click": 'button:text-is("+")'}, chk
    llm = FakeLLM(CONV_PLAN, CONV_CHECKS, [CONV_GOOD], review=ALL_MET)
    rig = Rig(llm, "Make a command-line temperature converter: 100 --to f prints 212")
    try:
        assert rig.run()["status"] == "done"
        rig2 = Rig(FakeLLM(dict(CONV_PLAN, criteria=["`212 --to c` prints 100"]), [], [CONV_GOOD]),
                   "Also convert to Celsius with --to c", root=rig.root, store=rig.store, wid=rig.wid)
        text = rig2.agent._request_text()
        assert text.startswith("The product was built from these earlier requests: (1) Make a command-line") and \
            text.endswith("this request changes it: Also convert to Celsius with --to c"), text
    finally:
        rig.close()


def test_the_examiner_works_numbers_out_one_action_at_a_time():
    from dev.examiner import Examiner
    req = "Make a web page with a counter: it shows a number starting at 0, a + button adds one, a - button subtracts one."

    def made():
        return {2: [{"type": "web", "steps": [{"click": 'button:has-text("+")'}, {"expect_text": "1"}]}],
                3: [{"type": "web", "steps": [{"click": 'button:has-text("-")'}, {"expect_text": "0"}]}],      # wrong
                4: [{"type": "web", "steps": [{"click": 'button:has-text("+")'}, {"click": 'button:has-text("+")'},
                                              {"click": 'button:has-text("-")'}, {"expect_text": "2"}]}]}    # wrong
    m = made()
    llm = FakeLLM({}, [], [""], predict=_counter_mind(wrong_minus_from_zero=True))
    fixed = Examiner(llm).settle_numbers(req, m)
    assert m[3][0]["steps"][1] == {"expect_text": "-1"} and m[3][0]["settled"], m[3]
    assert m[4][0]["steps"][3] == {"expect_text": "1"}, m[4]
    assert m[2][0]["steps"][1] == {"expect_text": "1"} and "settled" not in m[2][0]
    assert len(fixed) == 2 and "expects -1, not 0" in fixed[0], fixed
    assert all(k[0].get("confirmed") for k in m.values()), "every number was worked out: the vote vouches for them"
    asked = [u for k, _s, u in llm.calls if k == "predict"]
    assert len(asked) == len(set(asked)), "the same question is not asked twice"
    # no agreement -> no change; a request with its own rules -> left to the examiner
    m = made()
    Examiner(FakeLLM({}, [], [""], predict=_counter_mind(quiet=True))).settle_numbers(req, m)
    assert m[3][0]["steps"][1] == {"expect_text": "0"}
    m = made()
    assert Examiner(FakeLLM({}, [], [""], predict=_counter_mind())).settle_numbers(
        req + " The number must never go below zero.", m) == [] and m[3][0]["steps"][1] == {"expect_text": "0"}


# ------------------------------------------------------------------ the protocol
def test_the_text_parser_is_forgiving():
    reply = """Sure! Here is the code.

File: `src/main.py`
```python
print("hi")
```

### index.html
```html
<p>x</p>
```

```css style.css
p { color: red }
```

```js:app.js
console.log(1)
```

EDIT: src/main.py
<<<<<<< SEARCH
print("hi")
=======
print("hello")
>>>>>>> REPLACE

CONFIG: {"kind": "cli",
  "run": "python src/main.py"}
CHECKS:
```json
[{"criterion": 1, "type": "cli", "args": "", "expect": ["hello"]}]
```
$ python -m pytest -q
RUN: `pip install requests`
INSTALL: node
NOTE: the entry point is src/main.py
DONE all written
"""
    acts = protocol.parse_text(reply)
    kinds = [a["type"] for a in acts]
    assert kinds == ["write", "write", "write", "write", "edit", "config", "checks", "run", "run", "install", "note",
                     "done"], kinds
    paths = [a["path"] for a in acts if a["type"] == "write"]
    assert paths == ["src/main.py", "index.html", "style.css", "app.js"], paths
    edit = acts[4]
    assert edit["search"] == 'print("hi")' and edit["replace"] == 'print("hello")'
    assert acts[5]["value"] == {"kind": "cli", "run": "python src/main.py"}
    assert acts[6]["value"][0]["expect"] == ["hello"]
    assert acts[8]["command"] == "pip install requests" and acts[9]["tool"] == "node"
    assert protocol.parse_text("I think we should use React.") == []


def test_a_block_left_open_never_swallows_the_rest_of_the_reply():
    """Measured with a 3B model: an EDIT written git-style ("<<<<<<< HEAD") with no ">>>>>>> REPLACE"
    turned app.js into "DONE / EDIT: index.html / ..." and the page stayed broken for 30 steps."""
    reply = """FILE: app.js
```javascript
const c = document.getElementById('count');
DONE
EDIT: index.html
<<<<<<< HEAD
<button id="add">Add</button>
=======
<button id="add">+</button>
EDIT: style.css
<<<<<<< SEARCH
body { margin: 0 }
=======
body { margin: 1px }
>>>>>>> REPLACE
DONE
"""
    acts = protocol.parse_text(reply)
    assert [(a["type"], a.get("path")) for a in acts] == [("write", "app.js"), ("done", None), ("edit", "index.html"),
                                                          ("edit", "style.css"), ("done", None)], acts
    assert acts[0]["content"] == "const c = document.getElementById('count');\n"
    assert acts[2]["search"] == '<button id="add">Add</button>' and acts[2]["replace"] == '<button id="add">+</button>'
    md = protocol.parse_text("FILE: NOTES.md\n```md\nNOTE: keep this line\n```\n")
    assert md[0]["content"] == "NOTE: keep this line\n", "a document may contain such words"
    root = tempfile.mkdtemp(prefix="jarvis_agenttest_")
    try:
        sb = Sandbox(root, os.path.join(root, "project"))
        os.makedirs(sb.project, exist_ok=True)
        for bad in ("let a = 1;\nDONE\n", "<<<<<<< HEAD\nlet a = 1;\n"):
            try:
                sb.write_file("app.js", bad)
                raise AssertionError("a file must not receive the reply's own format")
            except ValueError as e:
                assert "not written" in str(e)
        sb.write_file("README.md", "NOTE: fine in a document\n")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tool_calls_map_to_actions():
    acts = protocol.from_tool_calls([
        {"name": "write_file", "arguments": json.dumps({"path": "./a.py", "content": "x=1\n"})},
        {"name": "bash", "arguments": {"command": "python a.py"}},
        {"name": "set_checks", "arguments": {"checks": [{"criterion": 1, "type": "cli", "expect": ["1"]}]}},
        {"name": "finish", "arguments": {}},
        {"name": "make_coffee", "arguments": {}}])
    assert [a["type"] for a in acts] == ["write", "run", "checks", "done", "unknown"], acts
    assert acts[0]["path"] == "a.py" and acts[1]["command"] == "python a.py"
    assert acts[2]["value"][0]["criterion"] == 1


def test_the_coders_own_checks_cannot_block_what_the_examiner_accepts():
    root = tempfile.mkdtemp(prefix="jarvis_agenttest_")
    try:
        ws, proj = os.path.join(root, "ws"), os.path.join(root, "ws", "project")
        os.makedirs(proj)
        open(os.path.join(proj, "main.py"), "w").write("import sys\nprint(int(sys.argv[1]) * 2 if len(sys.argv) > 1 else 'usage')\n")
        open(os.path.join(proj, "jarvis.json"), "w").write('{"kind": "cli", "run": "python main.py"}')
        sb = Sandbox(ws, proj, data_dir=root)
        crit = [{"id": 1, "text": "doubles 21", "checks": [
            {"type": "cli", "by": "jarvis", "args": "21", "expect": ["42"]},
            {"type": "cli", "by": "model", "args": "21", "expect": ["43"]}]}]               # the coder's wrong check
        rep = Verifier(sb, os.path.join(ws, "state")).run(crit)
        assert rep.ok, rep.text()
        assert any("your own extra check 2" in e and "does not block" in e for e in rep.evidence), rep.evidence
        crit[0]["checks"] = [crit[0]["checks"][1]]                                           # no examiner check
        assert not Verifier(sb, os.path.join(ws, "state")).run(crit).ok
        sb.stop_all()
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
    print(f"\n{len(fns) - failed}/{len(fns)} developer agent tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
