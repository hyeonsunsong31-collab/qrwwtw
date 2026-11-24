"""
dev/agent.py -- the coding loop: it keeps working until the product passes JARVIS's own checks.

  1. understand   the request (and the files you sent) -> 3-8 acceptance criteria that a test can
                  check, the kind of project, a starting template, a short plan. An adjustment keeps
                  the earlier criteria as regression checks.
  2. scaffold     a working starting point for the kind of project (dev/templates)
  3. iterate      the model writes and runs things (protocol.py), JARVIS checks the real program
                  (verify.py) and shows the model exactly what failed -- again and again
  4. never stuck  the same failure 3 times: re-plan with a different approach; 6 times: rewrite
                  the failing parts from scratch; replies it cannot use: switch how the model acts
                  (tool calls <-> plain directives). No step or time limit unless you set one
                  (dev.max_hours); Pause and Stop work at any moment.
  5. finish       only when every check passes: the history commit, the deliverable in output/,
                  a link for web products.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

from . import protocol, verify
from .examiner import Examiner
from .files import inspect as inspect_file, extract as extract_archive, pyinstaller_extract, asar_extract, refused
from .workspaces import SKIP_DIRS

IS_WINDOWS = sys.platform.startswith("win")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
KINDS = list(verify.KINDS)


class Stopped(Exception):
    pass


class Job:
    """Pause / stop control shared with the scheduler."""

    def __init__(self):
        import threading
        self._stop = threading.Event()
        self._run = threading.Event()
        self._run.set()

    def stop(self):
        self._stop.set()
        self._run.set()

    def pause(self):
        self._run.clear()

    def resume(self):
        self._run.set()

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self):
        """Blocks while paused; raises Stopped when stopped."""
        while not self._run.wait(0.5):
            pass
        if self._stop.is_set():
            raise Stopped()


def templates() -> Dict[str, dict]:
    out = {}
    if not os.path.isdir(TEMPLATES_DIR):
        return out
    for name in sorted(os.listdir(TEMPLATES_DIR)):
        meta = os.path.join(TEMPLATES_DIR, name, "template.json")
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as fh:
                    out[name] = json.load(fh)
            except (OSError, ValueError):
                pass
    return out


SYSTEM = """You are JARVIS's developer. You build and change complete, working software on the user's own PC, in a project folder. You work in steps: after each step you see what happened, and JARVIS checks the real program itself.

Rules:
- Write real, complete, runnable code. No placeholders, no TODO, no pseudo-code, never "rest of the code unchanged": every file you write is the whole file.
- Keep it simple and self-contained. Use the language and libraries that fit the request; install packages inside the project (pip installs into its .venv, npm install without -g).
- A web page must work from the project folder by itself: no CDN links unless the user asked for them.
- jarvis.json tells JARVIS how to build, test and run the project: {"kind": "web|game-web|cli|gui|game-desktop|api|library|file|gba|other", "build": [commands], "test": [commands], "run": "<command, or static for a plain web page>", "web_root": "<folder with index.html>", "product": "<the deliverable>"}.
- Use exactly the labels and texts the request names: a '+' button shows '+' (not 'Add'), a 'Reset' button shows 'Reset'. JARVIS finds buttons by their visible text.
- Every acceptance criterion needs at least one check that proves it in the running program.
- Do not start servers, games or windowed programs yourself: put the command in jarvis.json "run"; JARVIS starts and checks them.
- Only touch files inside the project. Never write malware, and never remove copy protection, licence checks or anti-cheat.
- The shell is {shell} on {os}. Tools you can use here: {tools}.

{checks}
"""


class DevAgent:
    def __init__(self, llm, store, wid: str, rid: str, sandbox, verifier, job: Job,
                 emit: Callable[[dict], None], cfg: Optional[dict] = None, history=None,
                 log: Optional[Callable[[str, str], None]] = None, toolchains: str = "",
                 num_ctx: int = 8192, protocol_mode: str = "auto", model_name: str = ""):
        self.llm = llm
        self.store = store
        self.wid, self.rid = wid, rid
        self.sb = sandbox
        self.verifier = verifier
        self.job = job
        self.emit = emit
        self.cfg = cfg or {}
        self.history = history
        self.log = log or (lambda kind, text: None)
        self.toolchains = toolchains or "python"
        self.num_ctx = num_ctx
        self.model_name = model_name
        self.mode = protocol_mode if protocol_mode in ("tools", "text") else self._auto_mode(model_name)
        self.notes: List[str] = []
        self.open_files: List[str] = []
        self.last_actions: List[str] = []
        self.last_report: Optional[verify.Report] = None
        self.sig_counts: Dict[str, int] = {}
        self.guidance = ""
        self.empty_replies = 0
        self.step = 0
        self.started = time.time()
        self.last_notice = time.time()
        self.best_pass = -1
        self.no_progress = 0
        self.temperature = 0.2
        self.inputs_summary = ""
        self.examiner = Examiner(llm, log=self.log, retry=self._llm_retry)
        self.false_alarms = 0
        self.fail_streak: Dict[int, Tuple[str, int]] = {}     # criterion -> (how its JARVIS check fails, times)
        self.check_history: Dict[int, List[dict]] = {}        # criterion -> every JARVIS check it has had
        self.resets = 0

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _auto_mode(model: str) -> str:
        m = re.search(r":(\d+(?:\.\d+)?)b", (model or "").lower())
        size = float(m.group(1)) if m else 7.0
        return "tools" if size >= 7 else "text"

    @property
    def req(self) -> dict:
        return self.store.get_request(self.wid, self.rid) or {}

    def _update(self, **fields):
        self.store.update_request(self.wid, self.rid, **fields)

    def _progress(self, phase: str, activity: str = "", **extra):
        req = self.req
        crit = req.get("checklist") or []
        passed = sum(1 for c in crit if c.get("status") == "pass")
        ev = {"type": "dev_progress", "workspace": self.wid, "request": self.rid, "phase": phase,
              "activity": activity, "step": self.step, "criteria": [{k: c.get(k) for k in ("id", "text", "status")}
                                                                    for c in crit],
              "passed": passed, "total": len(crit), "elapsed": round(time.time() - self.started), **extra}
        self._update(progress={k: ev[k] for k in ("phase", "activity", "step", "passed", "total", "elapsed")})
        try:
            self.emit(ev)
        except Exception:
            pass
        if activity:
            self.log("progress", activity)

    def _notice(self, en: str, ko: str = ""):
        try:
            self.emit({"type": "dev_notice", "workspace": self.wid, "request": self.rid, "en": en, "ko": ko or en})
        except Exception:
            pass

    def _llm_retry(self, fn, *a, **kw):
        """Call the model; while the language engine is unavailable, wait and try again (never give up)."""
        delay = 5.0
        while True:
            self.job.checkpoint()
            try:
                return fn(*a, **kw)
            except Stopped:
                raise
            except Exception as e:
                self.log("error", f"the coding model did not answer: {type(e).__name__}: {str(e)[:200]}")
                self._progress("waiting", f"waiting for the language engine ({type(e).__name__})")
                end = time.time() + delay
                while time.time() < end:
                    self.job.checkpoint()
                    time.sleep(0.5)
                delay = min(delay * 2, 120.0)

    # ------------------------------------------------------------ main
    def run(self) -> dict:
        req = self.req
        why = refused(req.get("text", ""))
        if why:
            self._update(status="failed", error=why, summary=why)
            return {"status": "refused", "summary": why}
        max_h = float(self.cfg.get("max_hours") or 0)
        try:
            self._prepare_inputs()
            if not req.get("checklist"):
                self._plan()
            self._scaffold()
            while True:
                self.job.checkpoint()
                if max_h and time.time() - self.started > max_h * 3600:
                    msg = f"stopped after the {max_h:g}-hour limit set in Settings"
                    self._update(status="stopped", summary=msg)
                    return {"status": "stopped", "summary": msg}
                self.step += 1
                self._update(iterations=(self.req.get("iterations") or 0) + 1)
                actions, results, wants_check = self._one_step()
                changed = any(a["type"] in ("write", "edit", "delete", "config", "checks") for a in actions)
                if wants_check or (changed and self._due_check()) or (not actions and self.step > 1):
                    report = self._check()
                    if report.ok:
                        if self._review(report):
                            continue
                        return self._finish(self.last_report or report)
                    self._after_failure(report)
                self._periodic_notice()
        except Stopped:
            self._update(status="stopped", summary="stopped by you")
            return {"status": "stopped", "summary": "stopped by you"}

    def _due_check(self) -> bool:
        last = getattr(self, "_last_check_step", 0)
        return self.step - last >= 2

    # ------------------------------------------------------------ inputs
    def _prepare_inputs(self):
        """Look at the files you sent; unpack what can be unpacked so the model can work on it."""
        req = self.req
        if not req.get("attachments"):
            return
        root = self.store.dir(self.wid)
        project = self.sb.project
        lines = []
        for att in req["attachments"]:
            path = os.path.join(root, att["path"])
            info = inspect_file(path)
            lines.append("- " + info["summary"][:1500])
            k = info["kind"]
            empty = not any(f for f in os.listdir(project) if f not in (".git", ".gitignore", ".jarvis"))
            try:
                if k in ("zip", "tar", "gz", "7z", "rar") and empty:
                    try:
                        extract_archive(path, project)
                    except FileNotFoundError as e:
                        if "7-Zip" not in str(e):
                            raise
                        from .toolchains import install           # .7z / .rar: 7-Zip (asks first)
                        lines.append(f"  ({install('7zip', self.sb.ask, lambda m: self.log('setup', m))})")
                        from .toolchains import refresh_path
                        refresh_path()
                        extract_archive(path, project)
                    _flatten_single_folder(project)
                    lines.append(f"  (unpacked into the project folder: it is the starting point)")
                elif k == "folder" and empty:
                    shutil.copytree(path, project, dirs_exist_ok=True)
                    lines.append("  (copied into the project folder: it is the starting point)")
                elif k == "exe":
                    d = info.get("details") or {}
                    out = os.path.join(project, "recovered", os.path.splitext(att["name"])[0])
                    if "PyInstaller (Python)" in (d.get("built_with") or []):
                        r = pyinstaller_extract(path, out)
                        lines.append(f"  (PyInstaller contents extracted to recovered/: {len(r['files'])} files, "
                                     f"Python {r['python']}; main scripts: {', '.join(r['scripts'][:5])})")
                    asar = os.path.join(os.path.dirname(att.get("original", "")), "resources", "app.asar")
                    if "Electron" in (d.get("built_with") or []) and os.path.exists(asar):
                        files = asar_extract(asar, out + "_app")
                        lines.append(f"  (Electron app source extracted to recovered/: {len(files)} files)")
                elif k in ("gba",) and empty:
                    shutil.copy2(path, os.path.join(project, att["name"]))
                    lines.append(f"  (copied into the project as {att['name']}; jarvis-tool gba-info / ips-create / "
                                 f"tiles-export work on it)")
            except Exception as e:
                lines.append(f"  (could not unpack it: {e})")
            lines.append(f"  the original is kept at input/{att['name']} (do not change it)")
        self.inputs_summary = "\n".join(lines)
        self._update(meta=dict(req.get("meta") or {}, inputs=self.inputs_summary))

    # ------------------------------------------------------------ plan
    def _request_text(self) -> str:
        """The request -- and for a stacked change, the earlier requests the product was built from, so
        the examiner knows the whole product (measured: told only "add a +5 button", the vote decided
        that "+" adds five)."""
        text = self.req.get("text", "")
        earlier = []
        for r in self.store.requests(self.wid):
            if r["id"] == self.rid:
                break
            if r.get("status") == "done" and r.get("kind", "build") != "convert":
                earlier.append(r.get("text", ""))
        if not earlier:
            return text
        return ("The product was built from these earlier requests: " +
                " ".join(f"({i}) {t}" for i, t in enumerate(earlier[-6:], 1)) + f" -- and this request changes it: {text}")

    def _previous(self) -> List[dict]:
        """Criteria of the earlier finished requests of this workspace (kept working)."""
        out = []
        for r in self.store.requests(self.wid):
            if r["id"] == self.rid:
                break
            if r.get("status") == "done":
                for c in r.get("checklist") or []:
                    if c.get("checks"):
                        out.append({**c, "previous": True, "status": "pending"})
        seen, uniq = set(), []
        for c in out:
            if c["text"] not in seen:
                seen.add(c["text"])
                uniq.append(c)
        return uniq[-12:]

    def _plan(self):
        req = self.req
        self._progress("planning", "understanding the request and writing the acceptance criteria")
        tpl = templates()
        previous = self._previous()
        ws = self.store.get(self.wid) or {}
        tpl_lines = "\n".join(f"- {n}: {m.get('description', '')}" for n, m in tpl.items())
        system = ("You plan software work for JARVIS's developer. Turn the user's request into 2 to 8 acceptance "
                  "criteria: concrete facts about the finished program that a test can check -- what is shown, what "
                  "happens on a click or a key press, what a command prints, what file is produced. Name the exact "
                  "labels the user named (a '+' button, a 'Reset' button). Every thing the user asked for must be "
                  "covered. Do NOT add vague qualities (responsive, modern, clean, accessible, fast, user-friendly) "
                  "unless the user asked for them, and then say how to check them. Then choose the kind of project, a "
                  "starting template, and a short plan.\n"
                  "Kinds: web (a page or app in a browser), game-web (a browser game), cli (command line), gui "
                  "(desktop window app), game-desktop (desktop game), api (web server or API), library, file (produce "
                  "a file, e.g. a conversion), gba (Game Boy Advance), other.\n"
                  f"Templates:\n{tpl_lines}\n- none: start from nothing (any other language or kind)\n"
                  "Answer with JSON only.")
        user = f"Request: {req.get('text')}\n"
        if self.inputs_summary:
            user += f"Files the user sent:\n{self.inputs_summary}\n"
        if previous:
            user += ("This is an ADJUSTMENT of an existing product. It already does:\n" +
                     "\n".join(f"- {c['text']}" for c in previous) +
                     "\nWrite criteria only for what this request adds or changes; keep kind and template as they are.\n")
            if ws.get("kind") and ws.get("kind") != "auto":
                user += f"Existing kind: {ws['kind']}\n"
        files_now = [f for f in os.listdir(self.sb.project) if f not in (".git", ".gitignore")]
        if files_now:
            user += f"The project folder already has: {', '.join(sorted(files_now)[:30])}\n"
        schema = {"type": "object", "properties": {
            "title": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
            "template": {"type": "string", "enum": list(tpl) + ["none"]},
            "criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
            "plan": {"type": "array", "items": {"type": "string"}, "maxItems": 10}},
            "required": ["title", "kind", "template", "criteria", "plan"]}
        out = {}
        for attempt in range(3):
            out = self._llm_retry(self.llm.chat_json, system, user, schema,
                                  {"temperature": 0.2 + 0.2 * attempt, "num_predict": 1200}) or {}
            if isinstance(out.get("criteria"), list) and out["criteria"]:
                break
        criteria = [str(c).strip() for c in (out.get("criteria") or []) if str(c).strip()][:10]
        asked = " ".join([req.get("text", "")] + [c["text"] for c in previous])
        vague = [c for c in criteria if _vague(c, asked)]
        if vague and len(vague) < len(criteria):
            criteria = [c for c in criteria if c not in vague]
            self.log("plan", "left out qualities you did not ask for (nothing concrete to check): " + " | ".join(vague))
        if not criteria:
            criteria = [f"The program does what was asked: {req.get('text', '')[:200]}"]
        kind = out.get("kind") if out.get("kind") in KINDS else _guess_kind(req.get("text", ""))
        if previous and ws.get("kind") in KINDS:
            kind = ws["kind"]
        checklist = [dict(c, id=i + 1) for i, c in enumerate(previous)]
        for c in criteria:
            checklist.append({"id": len(checklist) + 1, "text": c, "checks": [], "status": "pending"})
        template = out.get("template") if (out.get("template") in tpl and not previous) else "none"
        title = (out.get("title") or ws.get("title") or req.get("text", "")[:40]).strip()[:60]
        self._update(checklist=checklist, meta=dict(self.req.get("meta") or {}, plan=out.get("plan") or [],
                                                     template=template, kind=kind, title=title))
        self._write_checks(kind)
        self.store.update(self.wid, kind=kind, title=ws.get("title") or title)
        self.log("plan", f"{len(criteria)} criteria; kind {kind}; template {template}: " +
                 " | ".join(criteria))
        self._progress("planned", f"{len(checklist)} acceptance criteria")

    def _write_checks(self, kind: str, only: Optional[List[int]] = None, evidence: str = ""):
        """The examiner's checks for the criteria (from the request, before the code)."""
        self._progress("planning", "writing the acceptance checks from your request")
        crit = self.req.get("checklist") or []
        made = self.examiner.write_checks(self._request_text(), crit, kind, evidence=evidence, only=only)
        for c in crit:
            if c["id"] in made:
                new = made[c["id"]]
                if only and c.get("checks"):
                    new = [k for k in new if Examiner.still_tests(c["text"], k)] or None
                    if not new:
                        continue
                c["checks"] = new + [k for k in (c.get("checks") or []) if k.get("by") != "jarvis"]
        self._update(checklist=crit)

    # ------------------------------------------------------------ scaffold
    def _scaffold(self):
        meta = self.req.get("meta") or {}
        name = meta.get("template") or "none"
        if name == "none" or meta.get("scaffolded"):
            return
        src = os.path.join(TEMPLATES_DIR, name, "files")
        if not os.path.isdir(src):
            return
        has_code = verify.source_files(self.sb.project)
        if has_code:
            return
        title = meta.get("title") or "JARVIS app"
        for base, _dirs, files in os.walk(src):
            for f in files:
                s = os.path.join(base, f)
                rel = os.path.relpath(s, src)
                d = os.path.join(self.sb.project, rel)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                with open(s, encoding="utf-8") as fh:
                    text = fh.read()
                text = text.replace("{{TITLE}}", title).replace("{{SLUG}}", re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "app")
                with open(d, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
        self._update(meta=dict(meta, scaffolded=True))
        self.log("scaffold", f"started from the {name} template")
        self._progress("scaffold", f"started from the {name} template")

    # ------------------------------------------------------------ one step
    def _system(self) -> str:
        shell = "cmd.exe" if IS_WINDOWS else "bash"
        osn = "Windows" if IS_WINDOWS else ("macOS" if sys.platform == "darwin" else "Linux")
        base = (SYSTEM.replace("{shell}", shell).replace("{os}", osn).replace("{tools}", self.toolchains)
                .replace("{checks}", protocol.CHECK_FORMATS))
        if self.mode == "text":
            return base + "\n" + protocol.TEXT_FORMAT
        return base + "\nAct with the tools. Call done when you believe everything works; JARVIS then checks it."

    def _params(self) -> dict:
        return {"temperature": self.temperature, "num_predict": 4096}

    def _one_step(self) -> Tuple[List[dict], List[str], bool]:
        self.job.checkpoint()
        state = self._state_message()
        system = self._system()
        self._progress("working", f"step {self.step}: asking the model for the next changes")
        actions: List[dict] = []
        results: List[str] = []
        if self.mode == "tools":
            hist: List[dict] = []
            user = state
            for _round in range(4):
                text, calls = self._llm_retry(self._call_tools, system, user, hist)
                self.log("reply", (text[:1500] + " | calls: " + ", ".join(
                    f"{c.get('name')}({str(c.get('arguments'))[:200]})" for c in calls))[:4000])
                acts = protocol.from_tool_calls(calls) or protocol.parse_text(text)
                if not acts:
                    break
                res = self._execute(acts)
                actions += acts
                results += res
                if any(a["type"] in ("done", "verify") for a in acts):
                    break
                if not any(a["type"] in ("read", "list", "run") for a in acts):
                    break
                hist = hist + [{"role": "user", "text": user},
                               {"role": "assistant", "text": text, "calls": calls}] + \
                    [{"role": "tool", "text": r[:6000], "name": (c.get("name") or "")} for c, r in zip(calls, res)]
                user = "Here are the results. Continue."
        else:
            text = self._llm_retry(self._call_text, system, state)
            self.log("reply", text[:4000])
            actions = protocol.parse_text(text)
            results = self._execute(actions) if actions else []
        if not actions:
            self.empty_replies += 1
            self.guidance = ("Your last reply contained no usable directives. Reply ONLY with directives (FILE, EDIT, "
                             "RUN, CHECKS, CONFIG, DONE)." if self.mode == "text" else
                             "Your last reply called no tools. Use the tools (write_file, run, set_checks, done).")
            if self.empty_replies >= 3:
                self._switch_mode("its replies could not be used")
                if self.mode == "text":
                    self.guidance += (" Example of a correct reply:\nFILE: index.html\n```html\n<!doctype html>...the "
                                      "whole file...\n```\nDONE")
        else:
            self.empty_replies = 0
        self.last_actions = [r for r in results][-14:]
        wants_check = any(a["type"] in ("done", "verify") for a in actions)
        return actions, results, wants_check

    def _call_tools(self, system: str, user: str, hist: List[dict]):
        text, calls = "", []
        for kind, payload in self.llm.chat_stream_events(system, user, hist, self._params(), protocol.TOOLS):
            if kind == "tools":
                calls.extend(payload or [])
            else:
                text += payload
            if self.job.stopped():
                raise Stopped()
        return text, calls

    def _call_text(self, system: str, user: str) -> str:
        out = []
        n = 0
        for piece in self.llm.chat_stream(system, user, [], self._params()):
            out.append(piece)
            n += len(piece)
            if n % 2000 < len(piece):
                m = re.findall(r"FILE:\s*(\S+)", "".join(out))
                if m:
                    self._progress("working", f"writing {m[-1]}")
            if self.job.stopped():
                raise Stopped()
        return "".join(out)

    def _model_size(self) -> float:
        m = re.search(r":(\d+(?:\.\d+)?)b", (self.model_name or "").lower())
        return float(m.group(1)) if m else 7.0

    def _switch_mode(self, why: str):
        """tools <-> text. Models under 7B stay with plain directives (they cannot use tools well):
        for them a stall is answered with a different temperature instead."""
        old = self.mode
        if old == "text" and self._model_size() < 7:
            self.temperature = {0.2: 0.5, 0.5: 0.8}.get(round(self.temperature, 1), 0.2)
            self.empty_replies = 0
            self.log("protocol", f"staying with plain directives ({why}); temperature now {self.temperature}")
            return
        self.mode = "text" if self.mode == "tools" else "tools"
        self.empty_replies = 0
        self.log("protocol", f"switching from {old} to {self.mode}: {why}")

    # ------------------------------------------------------------ actions
    def _execute(self, actions: List[dict]) -> List[str]:
        out: List[str] = []
        for a in actions:
            self.job.checkpoint()
            t = a["type"]
            try:
                if t == "write":
                    out.append(self.sb.write_file(a["path"], a.get("content", "")))
                    self._progress("working", f"wrote {a['path']}")
                elif t == "edit":
                    out.append(self.sb.edit_file(a["path"], a.get("search", ""), a.get("replace", "")))
                elif t == "read":
                    text = self.sb.read_file(a["path"])
                    if a["path"] not in self.open_files:
                        self.open_files.append(a["path"])
                    out.append(f"{a['path']}:\n{text[:12000]}")
                elif t == "list":
                    out.append(self.sb.list_files(a.get("path") or "."))
                elif t == "delete":
                    out.append(self.sb.delete_file(a["path"]))
                elif t == "run":
                    self._progress("working", f"running: {a['command'][:80]}")
                    r = self.sb.run(a["command"], timeout=a.get("timeout"))
                    note = f" -- {r['note']}" if r.get("note") else ""
                    out.append(f"$ {a['command']}\nexit {r.get('code')}{' (' + r['killed'] + ')' if r.get('killed') else ''}"
                               f"{note}\n{r.tail(2500)}")
                elif t == "config":
                    out.append(self._write_config(a.get("value") or {}))
                elif t == "checks":
                    out.append(self._set_checks(a.get("value")))
                elif t == "note":
                    self.notes = (self.notes + [a.get("text", "")])[-8:]
                    out.append("noted")
                elif t == "install":
                    from .toolchains import install
                    self._progress("working", f"installing {a.get('tool')}")
                    out.append(install(a.get("tool", ""), self.sb.ask, lambda m: self.log("setup", m)))
                elif t in ("done", "verify"):
                    out.append("checking the whole program now")
                else:
                    out.append(f"unknown action {a.get('name') or t}")
            except PermissionError as e:
                out.append(f"{t} refused: {e}")
            except Exception as e:
                out.append(f"{t} failed: {type(e).__name__}: {str(e)[:300]}")
        return out

    def _write_config(self, value) -> str:
        if not isinstance(value, dict):
            return "CONFIG must be a JSON object"
        path = os.path.join(self.sb.project, "jarvis.json")
        cur = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    cur = json.load(fh)
            except (OSError, ValueError):
                cur = {}
        cur.update({k: v for k, v in value.items() if v not in (None, "")})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=2)
        return "jarvis.json updated: " + json.dumps(cur)[:300]

    def _set_checks(self, value) -> str:
        if isinstance(value, dict):
            value = value.get("checks", [value])
        if not isinstance(value, list):
            return "CHECKS must be a JSON list"
        crit = self.req.get("checklist") or []
        by_id = {c["id"]: c for c in crit}
        added: Dict[int, List[dict]] = {}
        for chk in value:
            if not isinstance(chk, dict):
                continue
            try:
                cid = int(chk.get("criterion"))
            except (TypeError, ValueError):
                continue
            if cid in by_id and not by_id[cid].get("previous"):
                added.setdefault(cid, []).append({k: v for k, v in chk.items() if k != "criterion"})
        for cid, checks in added.items():
            mine = [dict(k, by="model") for k in checks[:3]]
            by_id[cid]["checks"] = [k for k in (by_id[cid].get("checks") or []) if k.get("by") == "jarvis"] + mine
        self._update(checklist=crit)
        missing = [c["id"] for c in crit if not c.get("checks")]
        return (f"checks set for criteria {sorted(added)}" + (f"; still without a check: {missing}" if missing else "")
                if added else "no valid checks (each needs \"criterion\": <number> and \"type\")")

    # ------------------------------------------------------------ checking
    def _check(self, _repair_depth: int = 0) -> verify.Report:
        self._last_check_step = self.step
        self._progress("checking", "checking the whole program (build, tests, running it)")
        crit = self.req.get("checklist") or []
        kind = (self.req.get("meta") or {}).get("kind", "")
        report = self.verifier.run(crit, expect_kind=kind)
        self.last_report = report
        by_id = {c["id"]: c for c in crit}
        for c in report.checks:
            cid = c.get("criterion")
            if cid in by_id:
                by_id[cid]["status"] = "pass" if c["ok"] else "fail"
                by_id[cid]["evidence"] = c.get("detail", "")[:400]
        self._update(checklist=crit, report=report.to_dict())
        self.log("check", report.summary() + ("" if report.ok else " -- " + "; ".join(
            f"{c['name']}: {c['detail'][:120]}" for c in report.failures()[:4])))
        self._progress("checked", report.summary(), shots=report.shots[-3:], url=report.url)
        if report.broken and _repair_depth < 2:
            if self._repair(report):
                return self._check(_repair_depth + 1)
        return report

    def _repair(self, report: verify.Report) -> bool:
        """JARVIS checks that could not run (a malformed selector) go back to the examiner -- the
        coder is never blamed for them. True when at least one was re-written."""
        kind = (self.req.get("meta") or {}).get("kind", "")
        crit = self.req.get("checklist") or []
        by_id = {c["id"]: c for c in crit}
        ids = sorted({b["criterion"] for b in report.broken})
        self.log("examiner", f"re-writing malformed checks for criteria {ids}: " +
                 "; ".join(b["detail"][:120] for b in report.broken))
        made = self.examiner.write_checks(self._request_text(), crit, kind, evidence=report.evidence_text(3000),
                                          only=ids)
        fixed = False
        for cid in ids:
            c = by_id.get(cid)
            if not c:
                continue
            bad = {b["index"] for b in report.broken if b["criterion"] == cid}
            keep = [k for i, k in enumerate(c.get("checks") or []) if i not in bad]
            seen = [json.dumps(k.get("steps"), sort_keys=True) for k in self.check_history.get(cid, [])]
            new = [k for k in made.get(cid, []) if Examiner.still_tests(c["text"], k)
                   and json.dumps(k.get("steps"), sort_keys=True) not in seen]
            self.check_history.setdefault(cid, []).extend(k for i, k in enumerate(c.get("checks") or []) if i in bad)
            if new:
                c["checks"] = new[:2] + keep
                fixed = True
            elif keep:
                c["checks"] = keep
                fixed = True
        self._update(checklist=crit)
        return fixed

    def _after_failure(self, report: verify.Report):
        sig = report.signature()
        n = self.sig_counts[sig] = self.sig_counts.get(sig, 0) + 1
        passed = sum(1 for c in report.checks if c["ok"])
        if passed > self.best_pass:
            self.best_pass, self.no_progress = passed, 0
        else:
            self.no_progress += 1
        first = (report.failures()[0]["detail"].splitlines() or [""])[0][:200] if report.failures() else ""
        self.guidance = ""
        if n == 3:
            self.guidance = (f"You have failed the same way 3 times ({first}). Step back: READ the files involved, "
                             "find the real cause, and try a different approach -- simpler code, another library, or "
                             "a different structure.")
            self.temperature = 0.4
            self.log("stall", "same failure 3 times: re-planning with a different approach")
        elif n == 6:
            self.guidance = ("Still the same failure after 6 attempts. Rewrite the failing files from scratch as "
                             "complete files, simpler than before, and make sure the checks match the page or "
                             "program you write.")
            self.temperature = 0.6
            self.log("stall", "same failure 6 times: rewriting the failing parts from scratch")
        elif n >= 9 and n % 3 == 0:
            self.guidance = ("Start the failing feature over with the simplest design that can possibly work, and "
                             "check your CHECKS: selectors and expected texts must match what the program really shows.")
            self.temperature = 0.3 if self.temperature > 0.5 else 0.7
        if self.no_progress and self.no_progress % 8 == 0:
            self._switch_mode(f"no progress in {self.no_progress} checks")
            if self._model_size() < 7:
                self.guidance = ("No progress for a long while. Rewrite the files that fail from scratch -- complete "
                                 "files, the simplest version that can work -- instead of patching them again.")
        if self.no_progress and self.no_progress % 24 == 0 and self.resets < 3:
            self._reset_to_base()
        self._audit(report)

    def _audit(self, report: verify.Report):
        """A JARVIS check that fails the same way 3 times is judged by the examiner: if the CHECK is
        wrong it is replaced (never by one it had before); if the PROGRAM is wrong, the coder is told
        exactly that."""
        crit = self.req.get("checklist") or []
        by_id = {c["id"]: c for c in crit}
        kind = (self.req.get("meta") or {}).get("kind", "")
        changed = False
        for f in report.failures():
            cid = f.get("criterion")
            c = by_id.get(cid)
            if not c or c.get("previous") or "JARVIS's check" not in (f.get("detail") or ""):
                continue
            how = re.sub(r"\d+", "#", (f["detail"] or "").splitlines()[0])[:200]
            prev, times = self.fail_streak.get(cid, ("", 0))
            times = times + 1 if how == prev else 1
            self.fail_streak[cid] = (how, times)
            if times % 3:
                continue
            idx = next((i for i, k in enumerate(c.get("checks") or []) if k.get("by") == "jarvis"), None)
            if idx is None:
                continue
            check = c["checks"][idx]
            if check.get("confirmed") and re.search(r"expected text '[-+\u2212]?\d", f.get("detail") or ""):
                # the numbers this check expects were worked out one action at a time by a vote: a single
                # judgement cannot overrule them (measured: a 3B auditor sided with a "-" that stops at 0)
                note = f"The examiner confirms criterion {cid} is NOT met: {(f['detail'] or '').splitlines()[0][:220]}"
                self.guidance = (self.guidance + " " if self.guidance else "") + note
                self.log("examiner", f"criterion {cid}: the numbers its check expects are confirmed -- the program is wrong")
                continue
            lab = _missing_label(f.get("detail") or "")
            asked = (self._request_text() + " " + c.get("text", "")).lower()
            if lab and lab.lower() in asked:
                # the check looks for something the request itself names ("+5"): its absence means the
                # program is unfinished, not that the check is wrong (measured: a 3B auditor replaced the
                # check because "the page only has other buttons")
                note = (f"The examiner confirms criterion {cid} is NOT met: the page has no element showing '{lab}' -- "
                        f"the request asks for it, so add it (for a button, its visible text must be exactly '{lab}').")
                self.guidance = (self.guidance + " " if self.guidance else "") + note
                self.log("examiner", f"criterion {cid}: the program has no '{lab}' yet -- the program is wrong")
                continue
            self._progress("checking", f"the examiner is re-examining its check for criterion {cid}")
            res = self.examiner.audit(self._request_text(), c, check, f["detail"], report.evidence_text(2500), kind,
                                      self.check_history.get(cid, []) + [check])
            self.check_history.setdefault(cid, []).append(check)
            if res.get("check"):
                c["checks"][idx] = res["check"]
                self.fail_streak[cid] = ("", 0)
                changed = True
                self.log("examiner", f"criterion {cid}: its check was wrong ({res.get('reason', '')[:160]}); replaced")
            elif res.get("verdict") == "program_wrong":
                self.guidance = (self.guidance + " " if self.guidance else "") + \
                    f"The examiner confirms criterion {cid} is NOT met: {res.get('reason') or f['detail'][:200]}"
                self.log("examiner", f"criterion {cid}: the program is wrong -- {res.get('reason', '')[:160]}")
        if changed:
            self._update(checklist=crit)

    def _reset_to_base(self):
        """A very long stall: start the request's work over from where it began (keeping the checklist)."""
        base = self.req.get("base")
        if self.history is None or not base:
            return
        try:
            self.history.reset(base)
        except Exception as e:
            self.log("error", f"could not start over: {e}")
            return
        self.resets += 1
        meta = dict(self.req.get("meta") or {})
        meta["scaffolded"] = False
        self._update(meta=meta)
        self._scaffold()
        self.sig_counts.clear()
        self.no_progress = 0
        self.temperature = 0.3
        self.guidance = ("JARVIS started this request over from a clean project after a long stall. Write the program "
                         "again from scratch, simply, so that every check passes.")
        self.log("stall", "a long stall: started the work over from a clean project (the checks are kept)")

    def _review(self, report: verify.Report) -> bool:
        """Everything passes: the examiner reads the evidence. True when it found a real problem
        (a check that demonstrates it fails); then the work goes on."""
        if self.false_alarms >= 2:
            return False
        meta = self.req.get("meta") or {}
        crit = self.req.get("checklist") or []
        self._progress("reviewing", "the examiner is reviewing the result against your request")
        bad = self.examiner.review(self._request_text(), crit, report.evidence_text(5000), meta.get("kind", ""),
                                   source=self._source_digest(4000))
        proofs = {cid: v.get("checks") for cid, v in bad.items() if v.get("checks")}
        if not proofs:
            if bad:
                self.false_alarms += 1
            return False
        for c in crit:
            if c["id"] in proofs:
                c["checks"] = (c.get("checks") or []) + [dict(k, review=True) for k in proofs[c["id"]][:2]]
        self._update(checklist=crit)
        again = self._check()
        if again.ok:
            self.false_alarms += 1
            self.log("examiner", "the review's doubts did not hold up: its checks pass")
            return False
        why = "; ".join(f"criterion {cid}: {v['why']}" for cid, v in bad.items() if cid in proofs)
        self._after_failure(again)              # (it sets its own guidance: the review's comes on top)
        found = f"The examiner reviewed the result and found it does not do what was asked: {why}"
        self.guidance = f"{found} {self.guidance}".strip()
        self.log("examiner", "the review found real problems: " + why[:300])
        return True

    def _source_digest(self, budget: int) -> str:
        """The program's own code (not styles or data), smallest files first, for the examiner's review."""
        skip = {".css", ".json", ".md", ".txt", ".lock"}
        files = [f for f in verify.source_files(self.sb.project) if os.path.splitext(f)[1].lower() not in skip]
        files.sort(key=lambda f: os.path.getsize(f))
        out, used = [], 0
        for full in files:
            rel = os.path.relpath(full, self.sb.project).replace("\\", "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            room = budget - used - len(rel) - 10
            if room < 200:
                break
            out.append(f"--- {rel} ---\n{text[:room]}")
            used += min(len(text), room) + len(rel) + 10
        return "\n".join(out)

    def _periodic_notice(self):
        if time.time() - self.last_notice < 600:
            return
        self.last_notice = time.time()
        crit = self.req.get("checklist") or []
        passed = sum(1 for c in crit if c.get("status") == "pass")
        mins = int((time.time() - self.started) / 60)
        title = (self.req.get("meta") or {}).get("title") or "the project"
        self._notice(f"Still working on {title}, sir: {passed} of {len(crit)} checks pass after {mins} minutes.",
                     f"{title} 작업을 계속하고 있습니다, sir: {mins}분째, 확인 항목 {len(crit)}개 중 {passed}개 통과.")

    # ------------------------------------------------------------ the message for one step
    def _budget(self) -> int:
        return max(6000, (self.num_ctx - 1800 - 4096) * 3)

    def _state_message(self) -> str:
        req = self.req
        meta = req.get("meta") or {}
        budget = self._budget()
        parts = [f"REQUEST: {req.get('text')}"]
        if any(c.get("previous") for c in req.get("checklist") or []):
            parts.append("This changes an EXISTING product: keep everything that already works working.")
        if meta.get("inputs"):
            parts.append("FILES THE USER SENT:\n" + meta["inputs"][:3000])
        crit_lines = []
        for c in req.get("checklist") or []:
            st = {"pass": "PASS", "fail": "FAIL"}.get(c.get("status"), "....")
            checks = c.get("checks") or []
            desc = "; ".join(("JARVIS: " if k.get("by") == "jarvis" else "yours: ") + _check_brief(k) for k in checks) \
                if checks else "NO CHECK YET -- add one in CHECKS"
            crit_lines.append(f" {c['id']}. [{st}] {c['text']}   (checks: {desc})")
        parts.append("ACCEPTANCE CRITERIA (JARVIS checks each one in the real program; checks marked JARVIS were "
                     "written from the request: make the program pass them, they cannot be changed):\n" + "\n".join(crit_lines))
        cfg_path = os.path.join(self.sb.project, "jarvis.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8", errors="replace") as fh:
                parts.append("PROJECT SETUP (jarvis.json): " + fh.read()[:1500].strip())
        else:
            parts.append("PROJECT SETUP: jarvis.json is missing -- write it (CONFIG).")
        parts.append("FILES:\n" + self.sb.list_files(".", max_entries=80))
        if self.last_report is not None:
            parts.append("LAST CHECK BY JARVIS:\n" + self.last_report.text(3500))
        if self.last_actions:
            parts.append("WHAT HAPPENED IN YOUR LAST STEP:\n" + "\n".join(r[:1500] for r in self.last_actions)[-4000:])
        if self.notes:
            parts.append("YOUR NOTES: " + " | ".join(self.notes))
        used = sum(len(p) for p in parts)
        files_part = self._open_files_text(max(2000, budget - used - 1500))
        if files_part:
            parts.insert(-1 if self.last_report is None else len(parts) - (2 if self.last_actions else 1), files_part)
        if self.guidance:
            parts.append("IMPORTANT: " + self.guidance)
        parts.append(f"STEP {self.step}: {self._instruction()}")
        return "\n\n".join(parts)

    def _instruction(self) -> str:
        meta = self.req.get("meta") or {}
        if meta.get("scaffolded") and self.step == 1 and self.last_report is None:
            return ("The project starts from a template: rewrite its files so they do exactly what the request asks -- "
                    "replace the template's placeholder content (its title, its example button and text). Write the page "
                    "(HTML) and its script together, whole files, plus the CHECKS for every criterion, then DONE."
                    if self.mode == "text" else
                    "The project starts from a template: rewrite its files (write_file, whole files) so they do exactly "
                    "what the request asks -- replace the template's placeholder content -- then set_checks and done.")
        if not verify.source_files(self.sb.project):
            return ("Write the whole program now: every file, jarvis.json, and the CHECKS for every criterion."
                    if self.mode == "text" else
                    "Write the whole program now (write_file for every file), set_config, set_checks for every "
                    "criterion, then call done.")
        crit = self.req.get("checklist") or []
        if any(not c.get("checks") for c in crit):
            return "Add checks for every criterion that has none, and fix anything that fails."
        if self.last_report is not None and not self.last_report.ok:
            return "Fix what failed above. Change only what is needed (write whole files or exact edits), then DONE."
        return "Finish the program so that every criterion passes, then DONE."

    def _open_files_text(self, budget: int) -> str:
        files = [os.path.relpath(f, self.sb.project).replace("\\", "/")
                 for f in verify.source_files(self.sb.project, verify.SOURCE_EXT | {".json", ".md", ".txt", ".toml",
                                                                                   ".cfg", ".ini", ".yaml", ".yml"})]
        files = [f for f in files if not re.search(r"(package-lock|yarn\.lock|pnpm-lock|\.min\.)", f)]
        mentioned = set()
        if self.last_report is not None:
            blob = self.last_report.text(20000)
            for f in files:
                if os.path.basename(f) in blob:
                    mentioned.add(f)
        order = [f for f in self.open_files if f in files] + [f for f in reversed(self.sb.touched) if f in files] + \
                sorted(mentioned) + sorted(files, key=lambda f: os.path.getsize(os.path.join(self.sb.project, f)))
        seen, chunks, used = set(), [], 0
        for f in order:
            if f in seen or f == "jarvis.json":
                continue
            seen.add(f)
            full = os.path.join(self.sb.project, f)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            cap = min(12000, budget - used)
            if cap < 400:
                break
            if len(text) > cap:
                text = text[:cap // 2] + f"\n... (middle of {f} not shown; READ it to see all) ...\n" + text[-cap // 2:]
            chunks.append(f"--- {f} ---\n{text}")
            used += len(text) + len(f) + 10
        return ("CURRENT FILES:\n" + "\n".join(chunks)) if chunks else ""

    # ------------------------------------------------------------ finish
    def _finish(self, report: verify.Report) -> dict:
        self._progress("finishing", "all checks pass: saving the result")
        req = self.req
        meta = req.get("meta") or {}
        title = meta.get("title") or "project"
        cfg, _ = verify.load_config(self.sb.project)
        cfg = cfg or {}
        product = self._deliverable(cfg, title)
        commit = None
        if self.history is not None:
            try:
                commit = self.history.commit(self.rid, req.get("text", "")[:120])
            except Exception as e:
                self.log("error", f"could not record the history: {e}")
        crit = req.get("checklist") or []
        summary = (f"{title}: all {len(crit)} acceptance criteria pass after {self.step} steps "
                   f"({int((time.time() - self.started) / 60)} min).")
        self._update(status="done", finished=time.time(), product=product, summary=summary, commit=commit,
                     report=report.to_dict())
        self.store.update(self.wid, status="idle", product=product)
        self.log("done", summary)
        return {"status": "done", "summary": summary, "product": product, "config": cfg, "report": report.to_dict()}

    def _deliverable(self, cfg: dict, title: str) -> dict:
        """Copy the result into output/: the declared product, the web build, or the project."""
        out_dir = self.store.paths(self.wid)["output"]
        os.makedirs(out_dir, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:40] or "product"
        project = self.sb.project
        kind = cfg.get("kind", "other")
        prod = cfg.get("product") or ""
        product = {"kind": kind, "title": title, "run": cfg.get("run") or ""}
        if kind in verify.WEB_KINDS:
            root = os.path.join(project, cfg.get("web_root") or ".")
            if os.path.isdir(root):
                dest = os.path.join(out_dir, slug)
                shutil.rmtree(dest, ignore_errors=True)
                if cfg.get("run") in ("", "static"):
                    shutil.copytree(root, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS, "jarvis.json"))
                else:
                    shutil.copytree(project, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS))
                product["folder"] = dest
                product["path"] = shutil.make_archive(dest, "zip", dest)
                product["web"] = True
                product["serve"] = ({"root": dest} if cfg.get("run") in ("", "static") else
                                    {"command": cfg["run"], "cwd": project, "port": cfg.get("port") or 0,
                                     "url_path": cfg.get("url_path") or "/"})
            return product
        src = os.path.join(project, prod) if prod else ""
        if src and os.path.isfile(src):
            dest = os.path.join(out_dir, os.path.basename(src))
            shutil.copy2(src, dest)
            product["path"] = dest
        elif src and os.path.isdir(src):
            dest = os.path.join(out_dir, slug)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest)
            product["path"] = shutil.make_archive(dest, "zip", dest)
        else:
            dest = os.path.join(out_dir, slug)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(project, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS, ".git", ".gitignore"))
            product["path"] = shutil.make_archive(dest, "zip", dest)
            product["folder"] = dest
        return product


# ------------------------------------------------------------------ helpers
def _missing_label(detail: str) -> str:
    """The label a failing check looked for and did not find ("" when the failure is something else)."""
    if not re.search(r"no element matching|is not visible", detail or ""):
        return ""
    m = re.search(r""":(?:has-text|text-is)\((?:\\)?(['"])(.*?)(?:\\)?\1\)""", detail) or \
        re.search(r"""text=(['"]?)([^'"\s]+)\1""", detail)
    return m.group(2).strip() if m else ""


def _check_brief(chk: dict) -> str:
    t = chk.get("type", "?")
    if t == "web":
        steps = chk.get("steps") or []
        return "web " + " > ".join(f"{k} {json.dumps(v)[:30]}" for s in steps[:4] if isinstance(s, dict) for k, v in s.items())
    if t == "cli":
        return f"cli {chk.get('args', '')!r} expect {chk.get('expect')}"
    if t == "http":
        return f"http {chk.get('method', 'GET')} {chk.get('path')} expect {chk.get('expect')}"
    if t == "file":
        return f"file {chk.get('path')} {chk.get('format', '')}"
    if t == "run":
        return f"run {chk.get('command', '')[:40]!r} expect {chk.get('expect')}"
    return t


_VAGUE = re.compile(r"\b(responsive\w*|accessib\w*|user[- ]friendly|intuitive\w*|modern|clean|sleek|beautiful\w*|attractive|"
                    r"(visually )?appealing|pretty|elegant\w*|fast|performant|efficient\w*|smooth\w*|seamless\w*|"
                    r"well[- ](structured|organi[sz]ed|designed|written)|maintainab\w*|readab\w*|best practices|"
                    r"good (ux|ui|design|user experience)|easy to use|professional\w*|polish\w*|engaging|robust\w*|"
                    r"scalab\w*|mobile[- ]friendly|cross[- ]browser|aesthetic\w*|consistent\w*)\b", re.IGNORECASE)
_CONCRETE = re.compile(r"['\"“”‘’]|\d|(?<![\w])[+\-−×÷*/=<>](?![\w])|\b(click\w*|press\w*|tap\w*|typ\w+|enter\w*|"
                       r"show\w*|display\w*|print\w*|output\w*|return\w*|sav\w+|load\w*|open\w*|move\w*|"
                       r"add\w*|remov\w*|delet\w*|convert\w*|calculat\w*|sort\w*|search\w*|play\w*|score\w*)\b",
                       re.IGNORECASE)


def _vague(criterion: str, asked: str) -> bool:
    """A quality the user did not ask for and that names nothing checkable ("The page should be
    responsive and accessible"): small planners add these although told not to (measured)."""
    words = [m.group(0).lower() for m in _VAGUE.finditer(criterion or "")]
    if not words or _CONCRETE.search(criterion or ""):
        return False
    low = (asked or "").lower()
    return not any(w[:6] in low for w in words)


def _guess_kind(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(gba|game ?boy)\b", t):
        return "gba"
    if re.search(r"\bgame\b", t):
        return "game-desktop" if re.search(r"\b(pygame|desktop|window|exe)\b", t) else "game-web"
    if re.search(r"\b(website|web ?site|web ?page|web app|landing page|html|browser)\b", t):
        return "web"
    if re.search(r"\b(api|server|backend|endpoint)\b", t):
        return "api"
    if re.search(r"\b(convert|conversion|into a|to a)\b.*\.(\w+)", t):
        return "file"
    if re.search(r"\b(gui|window|desktop app|tkinter)\b", t):
        return "gui"
    if re.search(r"\b(cli|command[- ]line|terminal|script)\b", t):
        return "cli"
    return "other"


def _flatten_single_folder(project: str):
    """An archive that holds one top folder: use that folder's contents as the project."""
    entries = [e for e in os.listdir(project) if e not in (".git", ".gitignore", ".jarvis")]
    if len(entries) == 1 and os.path.isdir(os.path.join(project, entries[0])):
        inner = os.path.join(project, entries[0])
        for e in os.listdir(inner):
            shutil.move(os.path.join(inner, e), os.path.join(project, e))
        os.rmdir(inner)
