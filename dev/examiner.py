"""
dev/examiner.py -- the acceptance checks come from the request, not from the code.

A coding model that writes its own tests tends to test what it built instead of what was asked
(measured: asked for "+", "-" and "Reset" buttons, a 3B model built "Start" and "Reset" and
"proved" the "-" criterion by clicking "+" twice). So a separate examiner:

  write_checks()  turns each acceptance criterion into checks BEFORE the code exists, the way a
                  user would try it: buttons found by their visible text (button:has-text("-")),
                  what the page or the command shows. The coding model must pass them; it may add
                  checks of its own but cannot change or remove these.
  rewrite()       when one of its checks keeps failing, writes it again against the program as it
                  really is (its buttons and text) -- still testing the same criterion.
  review()        when everything passes, reads the evidence (the page's buttons and text, what
                  each check did) and objects only with a check that demonstrates the problem; the
                  objection counts only if that check really fails when JARVIS runs it.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

ACTIONS = ["click", "fill", "press", "wait", "expect_text", "expect_not_text", "expect_visible", "expect_count",
           "expect_changed", "select"]

# "why" comes before the steps: the examiner works out what the user will see (start state, each
# action, the result -- e.g. "starts at 0; one click of - subtracts one: 0 - 1 = -1") before it
# writes the expected value. Small models get the arithmetic wrong otherwise (measured: a 3B
# examiner expected "0" after one click of "-" and so forced the coder to invent a floor at 0).
_WHY = {"type": "string", "description": "the start state, each action and what it changes, and the result the "
                                         "user must then see -- worked out step by step from the request"}
_WEB_SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "items": {
    "type": "object", "properties": {
        "criterion": {"type": "integer"},
        "why": _WHY,
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ACTIONS},
            "target": {"type": "string"}, "value": {"type": "string"}}, "required": ["action"]}}},
    "required": ["criterion", "why", "steps"]}}}, "required": ["checks"]}
_CLI_SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "items": {
    "type": "object", "properties": {
        "criterion": {"type": "integer"}, "why": _WHY, "args": {"type": "string"}, "stdin": {"type": "string"},
        "expect": {"type": "array", "items": {"type": "string"}}},
    "required": ["criterion", "why", "args", "expect"]}}}, "required": ["checks"]}
_HTTP_SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "items": {
    "type": "object", "properties": {
        "criterion": {"type": "integer"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
        "path": {"type": "string"}, "body": {"type": "string"}, "expect_status": {"type": "integer"},
        "expect": {"type": "array", "items": {"type": "string"}}},
    "required": ["criterion", "path"]}}}, "required": ["checks"]}
_FILE_SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "items": {
    "type": "object", "properties": {
        "criterion": {"type": "integer"}, "path": {"type": "string"}, "format": {"type": "string"},
        "expect": {"type": "array", "items": {"type": "string"}}},
    "required": ["criterion", "path"]}}}, "required": ["checks"]}

WEB_EXAMPLE = (
    "Example -- request \"a counter starting at 0 with + and - buttons\":\n"
    "  criterion 1 \"shows 0 when the page opens\": why \"the page opens showing 0\", steps [{\"action\": "
    "\"expect_text\", \"target\": \"body\", \"value\": \"0\"}]\n"
    "  criterion 2 \"+ adds one\": why \"starts at 0; one click of + adds one: 0 + 1 = 1\", steps [{\"action\": "
    "\"click\", \"target\": \"button:has-text(\\\"+\\\")\"}, "
    "{\"action\": \"expect_text\", \"target\": \"body\", \"value\": \"1\"}]\n"
    "  criterion 3 \"- subtracts one\": why \"starts at 0; one click of - subtracts one: 0 - 1 = -1\", steps "
    "[{\"action\": \"click\", \"target\": \"button:has-text(\\\"-\\\")\"}, "
    "{\"action\": \"expect_text\", \"target\": \"body\", \"value\": \"-1\"}] and a second check (why \"0 + 1 + 1 - 1 "
    "= 1\") [{\"action\": "
    "\"click\", \"target\": \"button:has-text(\\\"+\\\")\"}, {\"action\": \"click\", \"target\": "
    "\"button:has-text(\\\"+\\\")\"}, {\"action\": \"click\", \"target\": \"button:has-text(\\\"-\\\")\"}, "
    "{\"action\": \"expect_text\", \"target\": \"body\", \"value\": \"1\"}]\n"
    "Wrong: {\"action\": \"expect_text\", \"target\": \"0\"} (a target is an element, never the text itself); checking "
    "the page title or a heading when the criterion is about a number or a button.")

WEB_RULES = (
    "Write checks a user would do in the browser, for each criterion:\n"
    "- Find elements by what the user sees: button:has-text(\"Reset\"), text=Score, input[placeholder*=\"name\" i], "
    "h1, canvas. Never invent ids or classes the request does not mention.\n"
    "- Expect what becomes visible: expect_text with a short value (a number, a word), not a whole sentence.\n"
    "- Each check starts from a freshly loaded page. Test each control ON ITS OWN from that start state first (one "
    "click of -, expect -1 -- unless the request itself sets a limit), so a wrong behaviour cannot hide behind other "
    "clicks; add a second check for a sequence (+, +, -, expect 1).\n"
    "- A check must prove its action did something: if the action would leave the page as it already is (Reset at "
    "the start), change it first (click +, then Reset, expect 0).\n"
    "- Never add a rule the request does not state: no floors, ceilings or special cases (a counter can go below 0 "
    "unless the request says it cannot). Work out every expected value in \"why\" first.\n"
    "- For games and drawings use press (ArrowRight, Space, KeyW...) then expect_changed.\n"
    "Step format: {\"action\": click|fill|press|wait|expect_text|expect_not_text|expect_visible|expect_count|"
    "expect_changed|select, \"target\": <selector, or the key for press>, \"value\": <text to type / expected text / "
    "count / milliseconds>}.\n" + WEB_EXAMPLE)
CLI_RULES = ("Write command-line checks: the arguments a user would pass (args -- REQUIRED: the exact words typed after the "
             "program's name; a check without args runs the program with nothing), any typed input (stdin), and short "
             "texts the output must contain (expect) -- numbers or key words, not whole sentences. Use the examples the "
             "request gives. Only the program's OUTPUT can be checked (never its comments or code).\n"
             "Example -- request \"a temperature converter: 100 --to f prints 212\": {\"criterion\": 1, \"why\": \"100 C is "
             "212 F\", \"args\": \"100 --to f\", \"expect\": [\"212\"]}")
HTTP_RULES = "Write HTTP checks against the server: method, path, optional JSON body, expected status and texts in the answer."
FILE_RULES = "Write file checks: the file the program must produce (path inside the project), its format, texts it must contain."


_TAGS = {"html", "body", "main", "header", "footer", "nav", "section", "article", "aside", "div", "span", "p", "a",
         "button", "input", "select", "option", "textarea", "label", "form", "img", "canvas", "svg", "video", "audio",
         "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
         "code", "strong", "em", "b", "i", "small", "output", "progress", "dialog", "details", "summary", "figure"}


def selector_like(s: str) -> bool:
    """Does this look like an element selector (not a piece of text)?"""
    s = (s or "").strip()
    if not s:
        return False
    if s.startswith(("#", ".", "[", "text=", "role=", "css=", "xpath=", "//", "*")):
        return len(s) > 1
    if ":has-text(" in s or ":text(" in s or ":text-is(" in s or ">>" in s:
        return True
    head = re.split(r"[\s>+~.#\[:,]", s, maxsplit=1)[0].lower()
    return head in _TAGS


def _q(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def label_selector(label: str) -> str:
    """A button (or link) by what it says: "Reset" -> button:has-text("Reset"), "+" -> button:text-is("+")."""
    lab = str(label).strip().strip("'\"")
    lab = re.sub(r"^(the\s+)?(button\s+)?", "", lab, flags=re.I)
    lab = re.sub(r"\s+button$", "", lab, flags=re.I) or lab
    return exact_short(f'button:has-text("{_q(lab)}")')


def exact_short(selector: str) -> str:
    """:has-text() matches part of a label, so button:has-text("+") also finds a "+5" button. A short
    label (up to 3 characters, or no letters at all) is matched exactly instead: button:text-is("+")."""
    def swap(m):
        lab = m.group(2)
        if len(lab) <= 3 or not re.search(r"[A-Za-z\u00c0-\uffff]", lab):
            return f":text-is({m.group(1)}{lab}{m.group(1)})"
        return m.group(0)
    return re.sub(r""":has-text\((["'])(.*?)\1\)""", swap, selector or "")


def _symbols(text: str) -> List[str]:
    """Labels a criterion names: quoted text, and lone symbols such as + - x."""
    out = re.findall(r"['\"“”‘’]([^'\"“”‘’]{1,30})['\"“”‘’]", text or "")
    out += re.findall(r"(?<![\w])([+\-−×÷*/=<>])(?![\w])", text or "")
    return [o.strip() for o in out if o.strip()]


_NUMBER = re.compile(r"[-+\u2212]?\d+(?:\.\d+)?")


def _only_number(text: str) -> Optional[str]:
    """The number in a short answer ("-1", "The page shows 2."), or None when there is not exactly one."""
    found = _NUMBER.findall(text or "")
    if len(found) != 1:
        return None
    v = found[0].replace("\u2212", "-").lstrip("+")
    return v[:-2] if v.endswith(".0") else v


_PREDICT = ("You predict exactly what a program shows, following the user's request literally -- no rules the "
            "request does not state. Answer with the number only.")
_PREDICT_LONG = ("You predict exactly what a program shows, following the user's request literally. Answer with the "
                 "shown value only.")
_CALCULATE = ("You turn a user's request into arithmetic. Follow the request literally -- no rules it does not state. "
              "Answer with one calculation only, like 4 + 1, 4 * 2 or 0, starting from the number the page shows.")
# requests that state rules or limits: numbers are left to the examiner (the one-action questions ignore such rules)
_RULES = re.compile(r"\b(never|below|above|under|over|at least|at most|minimum|maximum|min|max|limit\w*|cannot|can't|"
                    r"can not|negative|positive|up to|cap|capped|clamp\w*|stops? at|between|wraps?|loops?|until|unless|"
                    r"except|reaches|no more than|no less than|multiple of|even|odd|round\w*|decimals?|percent\w*|"
                    r"modulo|random\w*)\b|음수|양수|아래|이하|이상|미만|초과|최소|최대|넘지|넘으면|제한|안\s*되|못\s*하|까지만|"
                    r"무작위|랜덤|반올림", re.IGNORECASE)


def _last_line_number(text: str) -> Optional[str]:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return _only_number(lines[-1]) if lines else None


def _calculated(text: str) -> Optional[str]:
    """Evaluate the model's calculation ("0 - 1", "3 * 2") here -- small models write the right sum
    and then get negative results wrong. Only numbers, + - * / and brackets are evaluated."""
    import ast
    import operator
    t = (text or "").strip().strip("`").replace("\u2212", "-").replace("\u00d7", "*").replace("\u00f7", "/")
    t = ([ln for ln in t.splitlines() if ln.strip()] or [""])[-1]
    t = t.split("=")[0] if "=" in t else t
    if not re.fullmatch(r"[\d\s.+\-*/()]+", t or "x"):
        return None
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        raise ValueError("not a plain calculation")
    try:
        v = ev(ast.parse(t.strip(), mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError):
        return None
    v = round(float(v), 9)
    return str(int(v)) if v == int(v) else str(v)


def _describe(step: dict) -> Optional[str]:
    """One web step in words ("clicks the '-' button"), or None for waits and expectations."""
    def label(sel: str) -> str:
        m = re.search(r""":(?:has-text|text-is)\((['"])(.*?)\1\)""", sel) or re.match(r"""^text=(['"]?)(.+?)\1$""", sel)
        if m:
            kind = "link" if sel.startswith("a") else "button" if sel.startswith("button") or ":has-text" in sel else ""
            return f"the '{m.group(2)}' {kind}" if kind else f"'{m.group(2)}'"
        m = re.search(r"""(?:placeholder|name|aria-label)\*?=(['"])(.+?)\1""", sel)
        if m:
            return f"the '{m.group(2)}' field"
        return f"the element {sel}"
    if "click" in step:
        return f"clicks {label(str(step['click']))}"
    if "dblclick" in step:
        return f"double-clicks {label(str(step['dblclick']))}"
    if "fill" in step:
        v = step["fill"]
        sel, text = (v[0], v[1]) if isinstance(v, (list, tuple)) and len(v) >= 2 else (v, step.get("text", ""))
        return f"types '{text}' into {label(str(sel))}"
    if "press" in step:
        return f"presses the {step['press']} key"
    if "select" in step:
        v = step["select"]
        sel, value = (v[0], v[1]) if isinstance(v, (list, tuple)) and len(v) >= 2 else (v, step.get("value", ""))
        return f"chooses '{value}' in {label(str(sel))}"
    return None


# "100 --to f prints 212", "`conv 0 c` gives 32", "running it with 5 outputs 25", "3 + 4 should print 7"
_TOK = r"(?:-?\d+(?:\.\d+)?|--?[A-Za-z][\w-]*|(?!(?:should|will|must|would)\b)[A-Za-z][\w.]{0,15}|[+*/×÷^%=-])"
_VERB = r"(?:(?:should|will|must|would)\s+)?(?:prints?|gives?|outputs?|returns?|shows?|displays?|->|=>|→|=)"
_EXAMPLE = re.compile(r"`([^`]{1,80})`\s+" + _VERB + r"""\s+[`'"]?(-?[\w.%°]+)|(""" + _TOK + r"(?:\s+" + _TOK + r"){0,7})\s+"
                      + _VERB + r"""\s+[`'"]?(-?[\w.%°]+)""", re.IGNORECASE)
_FILLER = {"run", "running", "runs", "it", "with", "given", "input", "inputs", "eg", "e.g.", "like", "so", "then", "and",
           "the", "a", "an", "for", "example", "typing", "type", "calling", "call", "passing", "pass", "args", "arguments"}


def _examples(request: str) -> List[tuple]:
    """(arguments, result) pairs from the request's own examples."""
    out = []
    for m in _EXAMPLE.finditer(request or ""):
        words = (m.group(1) or m.group(3) or "").split()
        result = (m.group(2) or m.group(4) or "").strip(".,;:")
        while words and words[0].lower().strip("()") in _FILLER:
            words = words[1:]
        if m.group(1) and words and re.match(r"^(python3?|py|node|\./)", words[0], re.I):
            words = words[2:] if words[0].lower().startswith(("python", "py", "node")) else words[1:]
        rest = " ".join(words[1:])
        if len(words) > 1 and re.match(r"^[A-Za-z][\w.]*$", words[0]) and re.search(r"\d|(?:^|\s)--?\w", rest):
            words = words[1:]                       # "calc 3 + 4": the program's name, then its arguments
        args = " ".join(words).strip("()`'\"")
        if args and result and re.search(r"\d|--?\w|\w", args):
            out.append((args, result))
    return out


def fill_cli_args(request: str, made: Dict[int, List[dict]]) -> List[str]:
    """A command-line check without arguments cannot test a conversion: take them from the request's
    own example whose result the check expects (measured: a 3B examiner wrote every check with empty
    args, so "expect 212" ran the program with nothing)."""
    examples = _examples(request)
    done = []
    for cid, checks in made.items():
        for chk in checks:
            if chk.get("type") != "cli" or str(chk.get("args") or "").strip() or str(chk.get("stdin") or "").strip():
                continue
            for args, result in examples:
                if any(_same(result, e) for e in chk.get("expect") or []):
                    chk["args"] = args
                    done.append(f"criterion {cid}: {args!r} -> {result}")
                    break
    return done


def _same(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(str(b).strip())) < 1e-9
    except ValueError:
        return str(a).strip().lower() == str(b).strip().lower()


class Examiner:
    def __init__(self, llm, log: Optional[Callable[[str, str], None]] = None, retry=None):
        self.llm = llm
        self.log = log or (lambda kind, text: None)
        self.retry = retry or (lambda fn, *a, **k: fn(*a, **k))

    # ------------------------------------------------------------ write
    def _ask(self, system: str, user: str, schema: dict, temperature: float = 0.1) -> list:
        out = self.retry(self.llm.chat_json, system, user, schema,
                         {"temperature": temperature, "num_predict": 2500}) or {}
        checks = out.get("checks") if isinstance(out, dict) else None
        return checks if isinstance(checks, list) else []

    def write_checks(self, request: str, criteria: List[dict], kind: str, evidence: str = "",
                     only: Optional[List[int]] = None) -> Dict[int, List[dict]]:
        crit = [c for c in criteria if not c.get("previous") and (only is None or c["id"] in only)]
        if not crit:
            return {}
        if kind in ("web", "game-web"):
            schema, rules = _WEB_SCHEMA, WEB_RULES
        elif kind in ("cli", "library", "other"):
            schema, rules = _CLI_SCHEMA, CLI_RULES
        elif kind == "api":
            schema, rules = _HTTP_SCHEMA, HTTP_RULES
        elif kind == "file":
            schema, rules = _FILE_SCHEMA, FILE_RULES
        else:
            return {}                       # desktop windows, GBA: start-up checks + the model's own tests
        system = ("You are the examiner who decides whether a program does what the user asked. You write acceptance "
                  "checks from the REQUEST, before you see any code -- never from an implementation.\n" + rules +
                  "\nAnswer with JSON only.")
        lines = "\n".join(f"{c['id']}. {c['text']}" for c in crit)
        user = f"Request: {request}\nAcceptance criteria:\n{lines}\nWrite at least one check for every criterion."
        if evidence:
            user += ("\nThe program as it is now (use its real labels where they match the request, but keep testing "
                     f"what the criterion asks):\n{evidence[:3000]}")
        raw: list = []
        for attempt in range(2):
            raw = self._ask(system, user, schema, 0.1 + 0.3 * attempt)
            if raw:
                break
        out: Dict[int, List[dict]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                cid = int(item.get("criterion"))
            except (TypeError, ValueError):
                continue
            chk = self._to_check(item, kind)
            if chk is not None and cid in {c["id"] for c in crit}:
                out.setdefault(cid, []).append(dict(chk, by="jarvis"))
        for cid in out:
            out[cid] = out[cid][:3]
        if kind in ("cli", "library", "other"):
            filled = fill_cli_args(request, out)
            if filled:
                self.log("examiner", "took the arguments from the request's own examples: " + "; ".join(filled))
            self.settle_cli_numbers(request, out)
        self.log("examiner", f"wrote checks for criteria {sorted(out)}")
        if kind in ("web", "game-web") and out:
            fixed = self.settle_numbers(request, out)
            if fixed:
                self.log("examiner", "worked through the checks one action at a time and corrected " +
                         "; ".join(fixed))
        return out

    @staticmethod
    def _to_check(item: dict, kind: str) -> Optional[dict]:
        if kind in ("web", "game-web"):
            steps = []
            for s in item.get("steps") or []:
                if not isinstance(s, dict):
                    continue
                a = str(s.get("action") or "").strip()
                tgt, val = s.get("target"), s.get("value")
                tgt = str(tgt).strip() if tgt not in (None, "") else None
                if a == "click" and tgt:
                    steps.append({"click": exact_short(tgt) if selector_like(tgt) else label_selector(tgt)})
                elif a == "fill" and tgt:
                    steps.append({"fill": [tgt if selector_like(tgt) else f'input[placeholder*="{_q(tgt)}" i]', str(val or "")]})
                elif a == "select" and tgt:
                    steps.append({"select": [tgt if selector_like(tgt) else "select", str(val or "")]})
                elif a == "press":
                    steps.append({"press": str(tgt or val or "Space")})
                elif a == "wait":
                    try:
                        steps.append({"wait": max(50, min(5000, int(float(val or tgt or 300))))})
                    except (TypeError, ValueError):
                        steps.append({"wait": 300})
                elif a == "expect_text" and (val or tgt):
                    # a target that is not an element (e.g. "0") is the expected text itself
                    if val in (None, "") or not (tgt and selector_like(tgt)):
                        text = str(val if val not in (None, "") else tgt)
                        steps.append({"expect_text": text})
                    else:
                        step = {"expect_text": str(val)}
                        if tgt not in ("body", "page", "html"):
                            step["in"] = tgt
                        steps.append(step)
                elif a == "expect_not_text" and (val or tgt):
                    steps.append({"expect_not_text": str(val if val not in (None, "") else tgt)})
                elif a == "expect_visible" and tgt:
                    steps.append({"expect_visible": exact_short(tgt) if selector_like(tgt) else f"text={tgt}"})
                elif a == "expect_count" and tgt and selector_like(tgt):
                    try:
                        steps.append({"expect_count": [tgt, int(float(val or 1))]})
                    except (TypeError, ValueError):
                        pass
                elif a == "expect_changed":
                    steps.append({"expect_changed": True})
            if not steps or not any(next(iter(s)).startswith("expect") for s in steps):
                return None
            return {"type": "web", "steps": steps}
        if kind in ("cli", "library", "other"):
            exp = [str(e) for e in (item.get("expect") or []) if str(e).strip()]
            if not exp:
                return None
            return {"type": "cli", "args": str(item.get("args") or ""), "stdin": str(item.get("stdin") or ""),
                    "expect": exp[:4]}
        if kind == "api":
            chk = {"type": "http", "method": str(item.get("method") or "GET"), "path": str(item.get("path") or "/")}
            if item.get("body"):
                try:
                    chk["json"] = json.loads(item["body"])
                except (TypeError, ValueError):
                    chk["body"] = str(item["body"])
            if item.get("expect_status"):
                chk["expect_status"] = int(item["expect_status"])
            if item.get("expect"):
                chk["expect"] = [str(e) for e in item["expect"]][:4]
            return chk
        if kind == "file":
            chk = {"type": "file", "path": str(item.get("path") or "")}
            if not chk["path"]:
                return None
            if item.get("format"):
                chk["format"] = str(item["format"]).lstrip(".")
            if item.get("expect"):
                chk["expect"] = [str(e) for e in item["expect"]][:4]
            return chk
        return None

    # ------------------------------------------------------------ numbers, one action at a time
    def settle_numbers(self, request: str, made: Dict[int, List[dict]]) -> List[str]:
        """A second opinion on every number a web check expects, worked out one action at a time.

        Asked for a whole check at once, a small model gets the arithmetic wrong (measured: a 3B
        examiner expected 0 after one click of "-" on a counter starting at 0, three times out of
        three, and so forced the coder to invent a floor at 0). Asked about ONE action ("the page
        shows 0; the user clicks '-' once; what does it show now?") in three different ways -- the
        number, the calculation (worked out here, not by the model), and "work it out" -- the
        majority was right 9 times out of 10 on two 3B models. The one miss was a rule the request
        states ("never below zero"), which all three ignored: so when the request states rules or
        limits, numbers are left to the examiner. An expected number is changed only when at least
        two answers agree with each other and not with the check. Returns what was corrected."""
        if _RULES.search(request or ""):
            return []
        fixed: List[str] = []
        memo: Dict[tuple, Optional[str]] = {}
        for cid, checks in made.items():
            for chk in checks:
                steps = chk.get("steps") or []
                if not any("expect_text" in st and _NUMBER.fullmatch(str(st["expect_text"]).strip()) for st in steps):
                    continue
                state = self._value_after(request, None, None, memo)
                trace = [str(state)]
                numbers = sum(1 for st in steps if "expect_text" in st and _NUMBER.fullmatch(str(st["expect_text"]).strip()))
                compared = 0
                for st in steps:
                    if state is None:
                        break
                    act = _describe(st)
                    if act:
                        state = self._value_after(request, state, act, memo)
                        trace.append(f"{act} -> {state}")
                    elif "expect_text" in st:
                        want = str(st["expect_text"]).strip()
                        if not _NUMBER.fullmatch(want):
                            continue
                        compared += 1
                        if float(want.replace("\u2212", "-")) != float(state):
                            st["expect_text"] = state
                            chk["settled"] = True
                            if len(trace) == 1:     # the page as it opens: the criterion's own words may say it too
                                chk.setdefault("settled_start", []).append([want, state])
                            fixed.append(f"criterion {cid}: expects {state}, not {want} ({'; '.join(trace)})")
                if numbers and compared == numbers:
                    chk["confirmed"] = True         # every number it expects was worked out the same way
        return fixed

    def settle_cli_numbers(self, request: str, made: Dict[int, List[dict]]) -> List[str]:
        """Command-line checks whose arguments AND result are the request's own example ("100 --to f prints
        212") are confirmed; nothing else is changed. A model vote was measured here and rejected: asked
        what "-100 --to f" prints, a 3B model answered with the request's example output (212) in two
        of three ways, so the vote would have made right checks wrong. Returns what was corrected (none)."""
        given = {a: r for a, r in _examples(request)}
        for checks in made.values():
            for chk in checks:
                args = str(chk.get("args") or "").strip()
                nums = [str(e).strip() for e in chk.get("expect") or [] if _NUMBER.fullmatch(str(e).strip())]
                if chk.get("type") == "cli" and args in given and len(nums) == 1 and _same(given[args], nums[0]):
                    chk["confirmed"] = True
        return []

    def _value_after(self, request: str, before: Optional[str], action: Optional[str], memo: dict) -> Optional[str]:
        """The number the page shows after one action (or when it opens), by a vote of three ways of
        asking; None when fewer than two agree. Only the request is given: a criterion's own wording
        ("decrements the number by 1") pulls a small model towards the numbers in it."""
        key = (before, action)
        if key in memo:
            return memo[key]
        if not hasattr(self.llm, "chat"):
            memo[key] = None
            return None
        if action is None:
            situation = f"Request: {request}\nThe page has just been opened."
            asks = [(_PREDICT, f"{situation}\nWhat number does it show?", 24, _only_number),
                    (_PREDICT_LONG, f"{situation}\nWhat number does it show? Work it out, then give the final number on "
                                    f"the last line.", 100, _last_line_number)]
        else:
            situation = f"Request: {request}\nThe page shows {before}. The user {action} once."
            asks = [(_PREDICT, f"{situation}\nWhat number does the page show now?", 24, _only_number),
                    (_CALCULATE, f"{situation}\nWrite the calculation that gives the number the page shows next.", 30,
                     _calculated),
                    (_PREDICT_LONG, f"{situation}\nWhat number does the page show now? Work it out, then give the final "
                                    f"number on the last line.", 100, _last_line_number)]
        votes: Dict[float, str] = {}
        counts: Dict[float, int] = {}
        for system, user, limit, read in asks:
            out = self.retry(self.llm.chat, system, user, [], {"temperature": 0.0, "num_predict": limit})
            val = read(str(out or ""))
            if val is None:
                continue
            try:
                f = float(val)
            except ValueError:
                continue
            votes[f] = val
            counts[f] = counts.get(f, 0) + 1
        best = max(counts, key=counts.get) if counts else None
        memo[key] = votes[best] if best is not None and counts[best] >= 2 else None
        return memo[key]

    @staticmethod
    def still_tests(criterion: str, check: dict) -> bool:
        """A rewritten check must still be about the criterion's own labels (+, -, 'Reset', ...)."""
        want = _symbols(criterion)
        if not want:
            return True
        blob = json.dumps(check, ensure_ascii=False).lower()
        return any(w.lower() in blob for w in want)

    # ------------------------------------------------------------ audit
    def audit(self, request: str, criterion: dict, check: dict, failure: str, evidence: str, kind: str,
              previous: List[dict]) -> dict:
        """A JARVIS check keeps failing the same way: is the CHECK wrong (it does not test the criterion
        against this program correctly) or is the PROGRAM wrong? {"verdict", "reason", "check"}."""
        schema = {"type": "object", "properties": {
            "verdict": {"type": "string", "enum": ["check_wrong", "program_wrong"]},
            "reason": {"type": "string"},
            "steps": _WEB_SCHEMA["properties"]["checks"]["items"]["properties"]["steps"]},
            "required": ["verdict", "reason"]}
        cli = kind in ("cli", "library", "other") and check.get("type") == "cli"
        if kind not in ("web", "game-web"):
            schema["properties"].pop("steps")
        if cli:                                      # a corrected command line: arguments and what it prints
            schema["properties"]["args"] = {"type": "string"}
            schema["properties"]["expect"] = {"type": "array", "items": {"type": "string"}}
        system = ("You are the examiner. One of your acceptance checks keeps failing. Decide honestly: is your CHECK "
                  "wrong (wrong element, impossible expectation, not what the criterion means), or is the PROGRAM "
                  "wrong (it really does not do what the criterion says)? If the check is wrong, write a corrected "
                  "one that still tests the criterion's meaning and labels." +
                  ("\n" + WEB_RULES if kind in ("web", "game-web") else "\n" + CLI_RULES if cli else "") +
                  "\nAnswer with JSON only.")
        user = (f"Request: {request}\nCriterion {criterion.get('id')}: {criterion.get('text')}\n"
                f"The check: {json.dumps(check, ensure_ascii=False)}\nHow it fails: {failure[:800]}\n"
                f"What the program shows:\n{evidence[:2500]}")
        out = self.retry(self.llm.chat_json, system, user, schema, {"temperature": 0.1, "num_predict": 1200}) or {}
        verdict = out.get("verdict") if isinstance(out, dict) else None
        res = {"verdict": verdict or "program_wrong", "reason": str((out or {}).get("reason") or "")[:300], "check": None}
        if verdict == "check_wrong" and kind in ("web", "game-web") and out.get("steps"):
            chk = self._to_check({"steps": out["steps"]}, kind)
            seen = [json.dumps(p.get("steps"), sort_keys=True) for p in previous]
            if chk and self.still_tests(criterion.get("text", ""), chk) and json.dumps(chk["steps"], sort_keys=True) not in seen:
                self.settle_numbers(request, {criterion.get("id", 0): [chk]})
                res["check"] = dict(chk, by="jarvis")
            else:
                res["verdict"] = "program_wrong" if not chk else "check_kept"
        elif verdict == "check_wrong" and cli and out.get("expect"):
            chk = {"type": "cli", "args": str(out["args"] if out.get("args") is not None else check.get("args", "")),
                   "stdin": str(check.get("stdin") or ""), "expect": [str(e) for e in out["expect"] if str(e).strip()][:4]}
            key = lambda k: json.dumps([str(k.get("args") or "").strip(), k.get("expect")], sort_keys=True)
            if chk["expect"]:
                self.settle_cli_numbers(request, {criterion.get("id", 0): [chk]})
            if chk["expect"] and key(chk) not in [key(p) for p in previous]:
                res["check"] = dict(chk, by="jarvis")
            else:
                res["verdict"] = "check_kept"
        return res

    # ------------------------------------------------------------ review
    def review(self, request: str, criteria: List[dict], evidence: str, kind: str, source: str = "") -> Dict[int, dict]:
        """{criterion id: {"met": False, "why", "check"}} for criteria the evidence (and the code) do not support."""
        if kind not in ("web", "game-web", "cli", "api", "file", "library", "other"):
            return {}
        schema = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
            "type": "object", "properties": {"criterion": {"type": "integer"},
                                             "why": {"type": "string", "description": "what the evidence and the code "
                                                     "show for this criterion, compared with the request"},
                                             "met": {"type": "boolean"}},
            "required": ["criterion", "why", "met"]}}}, "required": ["verdicts"]}
        system = ("You are the examiner. Decide from the EVIDENCE and the CODE whether each acceptance criterion is "
                  "really met by the program -- not whether its tests pass. Be strict about things the user named "
                  "(buttons, labels, numbers, behaviour); a criterion is met only if the evidence shows it. Code that "
                  "changes what the user asked for -- a limit, a special case or a shortcut the request did not ask "
                  "for (e.g. a '-' that stops at 0 when the request just says it subtracts one) -- means the criterion "
                  "is NOT met. Answer with JSON only.")
        lines = "\n".join(f"{c['id']}. {c['text']}" for c in criteria)
        user = f"Request: {request}\nCriteria:\n{lines}\n\nEVIDENCE:\n{evidence[:6000]}"
        if source:
            user += f"\n\nCODE:\n{source[:4000]}"
        out = self.retry(self.llm.chat_json, system, user, schema, {"temperature": 0.0, "num_predict": 1500}) or {}
        bad: Dict[int, dict] = {}
        for v in (out.get("verdicts") or []) if isinstance(out, dict) else []:
            try:
                cid = int(v.get("criterion"))
            except (TypeError, ValueError):
                continue
            if v.get("met") is False:
                bad[cid] = {"met": False, "why": str(v.get("why") or "")[:300]}
        if bad:
            proof = self.write_checks(request, [c for c in criteria if c["id"] in bad], kind, evidence=evidence)
            for cid, checks in proof.items():
                bad[cid]["checks"] = checks
        return bad
