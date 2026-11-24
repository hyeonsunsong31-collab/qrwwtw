"""
dev/verify.py -- the definition of done.

A request is finished only when a fresh, deterministic check of the real program passes. The
coding model cannot talk its way past it: these checks are run by JARVIS, live outside the
project folder, and the evidence for each acceptance criterion comes from a check JARVIS ran.

Checks, by what the project is (jarvis.json, written by the model, says how to build and run it;
JARVIS infers it when it is missing):
  config        jarvis.json is valid (or the project type is recognised)
  placeholders  no "your code here" / NotImplementedError stubs in the product
  syntax        Python files compile, JSON files parse
  references    files that web pages load exist
  build         every build command succeeds (dependencies are installed into the project)
  tests         the test commands pass; tests that were deleted, emptied or reduced to
                `assert True` since the last check fail this check (test-integrity)
  runs          web page / game: loads in a real browser with no errors, is not blank, and
                REACTS (clicking, typing or key presses change it -- "does something");
                server / API: answers; command-line program: runs and prints something;
                desktop app / game: starts, stays up, opens a window; file product: exists and
                really is the format it claims; .gba: a valid cartridge header
  criteria      every acceptance criterion has at least one check of its own, and all pass
  product       the deliverable exists
"""
from __future__ import annotations

import hashlib
import json
import os
import py_compile
import re
import shlex
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import procs
from .workspaces import SKIP_DIRS

IS_WINDOWS = sys.platform.startswith("win")
KINDS = ("web", "game-web", "api", "cli", "gui", "game-desktop", "library", "file", "gba", "other")
WEB_KINDS = ("web", "game-web")
SOURCE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".cs", ".java", ".go", ".rs",
              ".c", ".cpp", ".h", ".hpp", ".kt", ".swift", ".rb", ".php", ".lua", ".gd", ".dart", ".vue", ".svelte"}
PLACEHOLDER = re.compile(r"your code here|implement (this|me)\b|todo:\s*implement|raise NotImplementedError|"
                         r"throw new Error\(\s*['\"]not implemented|lorem ipsum dolor sit amet, placeholder",
                         re.IGNORECASE)
INSTALL_CMD = re.compile(r"\b(npm (i|install|ci)|yarn( install)?$|pnpm (i|install)|pip3? install|"
                         r"python3? -m pip install|dotnet restore|cargo fetch|go mod download)\b", re.IGNORECASE)


# ============================================================== "does the output show X?"
_NUM_WANT = re.compile(r"[-+\u2212]?\d[\d,]*(?:\.\d+)?")
# a sign counts only when it is not glued to a word ("x-1", "1-10"); digits never start inside a number
_NUM_TOKEN = re.compile(r"(?:(?<![\w.])([-+\u2212])|(?<![\d.]))(\d[\d,]*(?:\.\d+)?)")


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace("\u2212", "-").replace(",", ""))
    except ValueError:
        return None


def _num_equal(token: str, want: str) -> bool:
    t, w = _num(token), _num(want)
    if t is None or w is None:
        return False
    if abs(t - w) < 1e-9:
        return True
    if "." in want:                         # 37.78 is shown by 37.7777... (rounded or cut to its decimals)
        dp = len(want.split(".")[1])
        if "." in token and len(token.split(".")[1]) > dp:
            q = 10 ** dp
            return abs(round(t * q) / q - w) < 1e-9 or abs(int(t * q) / q - w) < 1e-9
    return False


def shows(text: str, want) -> bool:
    """Does `text` show `want`? A number must be there as that number -- 1 is not shown by -1, 10 or
    1.5, while 212 is shown by 212.0 -- anything else is a case-insensitive substring."""
    w = str(want).strip()
    text = text or ""
    if _NUM_WANT.fullmatch(w):
        return any(_num_equal((m.group(1) or "") + m.group(2), w) for m in _NUM_TOKEN.finditer(text))
    return w.lower() in text.lower()


# ============================================================== config
def load_config(project: str) -> Tuple[Optional[dict], str]:
    """(config, problem). Missing jarvis.json -> inferred config; invalid JSON -> problem."""
    path = os.path.join(project, "jarvis.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                return None, "jarvis.json must be a JSON object"
            return normalise(cfg, project), ""
        except ValueError as e:
            return None, f"jarvis.json is not valid JSON: {e}"
    inferred = infer_config(project)
    return (normalise(inferred, project), "") if inferred else (None, "no jarvis.json, and the project type "
                                                                      "could not be recognised")


def normalise(cfg: dict, project: str) -> dict:
    c = dict(cfg)
    c["kind"] = str(c.get("kind") or "other").lower().strip()
    if c["kind"] not in KINDS:
        alias = {"website": "web", "webapp": "web", "web-app": "web", "site": "web", "game": "game-web",
                 "html": "web", "server": "api", "backend": "api", "desktop": "gui", "app": "gui",
                 "console": "cli", "script": "cli", "tool": "cli", "ai": "web", "bot": "cli", "rom": "gba",
                 "conversion": "file", "convert": "file"}
        c["kind"] = alias.get(c["kind"], "other")
    for key in ("build", "test"):
        v = c.get(key) or []
        c[key] = [v] if isinstance(v, str) else [str(x) for x in v if str(x).strip()]
    c["run"] = str(c.get("run") or "").strip()
    if c["kind"] in WEB_KINDS and not c["run"]:
        c["run"] = "static"
    c["web_root"] = str(c.get("web_root") or ".").strip() or "."
    c["url_path"] = str(c.get("url_path") or "/")
    if not c["url_path"].startswith("/"):
        c["url_path"] = "/" + c["url_path"]
    try:
        c["port"] = int(c.get("port") or 0)
    except (TypeError, ValueError):
        c["port"] = 0
    c["product"] = str(c.get("product") or "").strip()
    return c


def infer_config(project: str) -> Optional[dict]:
    has = lambda *p: os.path.exists(os.path.join(project, *p))
    if has("package.json"):
        try:
            with open(os.path.join(project, "package.json"), encoding="utf-8") as fh:
                pkg = json.load(fh)
        except (OSError, ValueError):
            pkg = {}
        scripts = pkg.get("scripts") or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        build = ["npm install"] + (["npm run build"] if "build" in scripts else [])
        test = ["npm test"] if scripts.get("test") and "no test specified" not in scripts.get("test", "") else []
        if "vite" in deps or "react-scripts" in deps:
            root = "dist" if "vite" in deps else "build"
            return {"kind": "web", "build": build, "test": test, "run": "static", "web_root": root}
        if "express" in deps or "fastify" in deps or "koa" in deps:
            return {"kind": "web" if has("public") else "api", "build": build, "test": test, "run": "npm start"}
        main = pkg.get("main") or ("index.js" if has("index.js") else "")
        return {"kind": "cli", "build": build, "test": test, "run": f"node {main}" if main else "npm start"}
    if has("index.html"):
        return {"kind": "web", "run": "static", "web_root": "."}
    for name in ("main.py", "app.py", "game.py", "run.py", "cli.py", "__main__.py"):
        if has(name):
            text = _read(os.path.join(project, name))
            kind = "game-desktop" if "pygame" in text else ("gui" if re.search(r"tkinter|PySide|PyQt|wx\b", text)
                                                             else "api" if re.search(r"flask|fastapi|uvicorn", text, re.I)
                                                             else "cli")
            test = ["python -m pytest -q"] if _has_python_tests(project) else []
            build = ["python -m pip install -r requirements.txt"] if has("requirements.txt") else []
            return {"kind": kind, "build": build, "test": test, "run": f"python {name}"}
    if has("Cargo.toml"):
        return {"kind": "cli", "build": ["cargo build"], "test": ["cargo test"], "run": "cargo run --quiet"}
    if has("go.mod"):
        return {"kind": "cli", "build": ["go build ./..."], "test": ["go test ./..."], "run": "go run ."}
    if any(f.endswith(".csproj") for f in _listdir(project)):
        return {"kind": "cli", "build": ["dotnet build"], "run": "dotnet run"}
    if has("Makefile") and any(f.endswith((".c", ".s")) for f in _listdir(project)):
        return {"kind": "gba" if "gba" in _read(os.path.join(project, "Makefile")).lower() else "cli",
                "build": ["make"], "run": ""}
    return None


def _listdir(d: str) -> List[str]:
    try:
        return os.listdir(d)
    except OSError:
        return []


def _read(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _has_python_tests(project: str) -> bool:
    for base, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if any(re.match(r"test_.*\.py$|.*_test\.py$", f) for f in files):
            return True
    return False


def source_files(project: str, exts=SOURCE_EXT, limit: int = 2000) -> List[str]:
    out = []
    for base, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.join(base, f))
                if len(out) >= limit:
                    return out
    return out


# ============================================================== report
class Report:
    def __init__(self):
        self.checks: List[dict] = []
        self.shots: List[str] = []
        self.evidence: List[str] = []            # what JARVIS saw: the page's controls and text, outputs
        self.broken: List[dict] = []             # JARVIS checks that could not run (a bad selector): re-written
        self.url: Optional[str] = None
        self.started = time.time()
        self.seconds = 0.0

    def add(self, cid: str, name: str, ok: bool, detail: str = "", criterion=None, **evidence) -> dict:
        c = {"id": cid, "name": name, "ok": bool(ok), "detail": (detail or "")[:4000], "criterion": criterion,
             **evidence}
        self.checks.append(c)
        return c

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)

    def failures(self) -> List[dict]:
        return [c for c in self.checks if not c["ok"]]

    def summary(self) -> str:
        n, bad = len(self.checks), len(self.failures())
        return "all checks pass" if not bad and n else f"{n - bad} of {n} checks pass"

    def signature(self) -> str:
        """A fingerprint of HOW it fails: the same fingerprint again means no progress."""
        parts = []
        for c in self.failures():
            first = (c["detail"] or "").strip().splitlines()[0] if c["detail"] else ""
            first = re.sub(r"\d+", "#", first)
            first = re.sub(r"(['\"]).{0,80}?\1", "'…'", first)[:120]
            parts.append(f"{c['id']}:{first}")
        return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:12] if parts else ""

    def evidence_text(self, max_chars: int = 3000) -> str:
        out = "\n".join(self.evidence)
        return out if len(out) <= max_chars else out[:max_chars] + "\n..."

    def text(self, max_chars: int = 6000) -> str:
        lines = [f"CHECK RESULT: {self.summary()}."]
        for c in self.failures():
            crit = f" (criterion {c['criterion']})" if c.get("criterion") else ""
            lines.append(f"FAIL {c['name']}{crit}: {c['detail'].strip()}")
        passed = [c for c in self.checks if c["ok"]]
        if passed:
            lines.append("PASS: " + "; ".join(c["name"] for c in passed)[:1500])
        if self.evidence:
            lines.append("WHAT JARVIS SAW:\n" + self.evidence_text(1800))
        out = "\n".join(lines)
        return out if len(out) <= max_chars else out[:max_chars - 40] + "\n... (more failures not shown)"

    def to_dict(self) -> dict:
        return {"ok": self.ok, "summary": self.summary(), "checks": self.checks, "shots": self.shots,
                "url": self.url, "seconds": round(self.seconds, 1), "signature": self.signature(),
                "evidence": self.evidence[:40], "broken": self.broken}


# ============================================================== verifier
class Verifier:
    def __init__(self, sandbox, state_dir: str, log: Optional[Callable[[str, str], None]] = None,
                 smoke_seconds: float = 4.0, browser: bool = True):
        self.sb = sandbox
        self.state_dir = state_dir             # outside project/: test-integrity records, screenshots
        self.shots_dir = os.path.join(state_dir, "shots")
        self.log = log or (lambda kind, text: None)
        self.smoke_seconds = smoke_seconds
        self.browser = browser
        self._last_seen = ""
        os.makedirs(self.shots_dir, exist_ok=True)

    # ------------------------------------------------------------ entry point
    def run(self, criteria: List[dict], expect_kind: str = "") -> Report:
        rep = Report()
        project = self.sb.project
        cfg, problem = load_config(project)
        if cfg is None:
            rep.add("config", "project setup (jarvis.json)", False,
                    problem + '. Write jarvis.json, e.g. {"kind": "web", "run": "static"} or '
                              '{"kind": "cli", "build": [], "test": ["python -m pytest -q"], "run": "python main.py"}.')
            self._criteria(rep, criteria, None, skip_reason="the project setup is missing")
            rep.seconds = time.time() - rep.started
            return rep
        if expect_kind and cfg["kind"] == "other" and expect_kind in KINDS:
            cfg["kind"] = expect_kind
        rep.add("config", "project setup (jarvis.json)", True, f"kind {cfg['kind']}")
        self.cfg = cfg
        if not source_files(project) and cfg["kind"] not in ("file",):
            rep.add("files", "source files", False, "the project has no source files yet")
            self._criteria(rep, criteria, cfg, skip_reason="nothing has been written yet")
            rep.seconds = time.time() - rep.started
            return rep
        self._placeholders(rep, project)
        self._syntax(rep, project)
        if cfg["kind"] in WEB_KINDS:
            self._references(rep, project, cfg)
        built = self._build(rep, cfg)
        if built:
            self._tests(rep, cfg)
            runner = {"web": self._web, "game-web": self._web, "api": self._api, "cli": self._cli,
                      "gui": self._gui, "game-desktop": self._gui, "library": self._library,
                      "file": self._file_product, "gba": self._gba}.get(cfg["kind"], self._other)
            try:
                runner(rep, cfg, criteria)
            finally:
                self.sb.stop_all()
        else:
            self._criteria(rep, criteria, cfg, skip_reason="the build failed")
        self._product(rep, cfg)
        rep.seconds = time.time() - rep.started
        return rep

    # ------------------------------------------------------------ static checks
    def _placeholders(self, rep: Report, project: str) -> None:
        hits = []
        for f in source_files(project):
            rel = os.path.relpath(f, project).replace("\\", "/")
            if re.search(r"(^|/)(tests?|__tests__|spec)/|test_|_test\.|\.test\.|\.spec\.", rel):
                continue
            for i, line in enumerate(_read(f).splitlines(), 1):
                if PLACEHOLDER.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:100]}")
                    break
        rep.add("placeholders", "no unfinished stubs", not hits,
                ("unfinished code left in: " + "; ".join(hits[:6])) if hits else "")

    def _syntax(self, rep: Report, project: str) -> None:
        errors = []
        for f in source_files(project, {".py"}):
            try:
                py_compile.compile(f, doraise=True, cfile=os.devnull if not IS_WINDOWS else None)
            except py_compile.PyCompileError as e:
                msg = str(e.msg if hasattr(e, "msg") else e).strip().splitlines()
                errors.append(f"{os.path.relpath(f, project)}: {' | '.join(msg[-3:])}"[:400])
            except Exception:
                pass
        for f in source_files(project, {".json"}, limit=200):
            if os.path.getsize(f) > 2_000_000:
                continue
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    json.load(fh)
            except ValueError as e:
                errors.append(f"{os.path.relpath(f, project)}: invalid JSON ({e})")
            except OSError:
                pass
        errors += self._js_syntax(project)
        rep.add("syntax", "code parses", not errors, "\n".join(errors[:8]))

    def _js_syntax(self, project: str) -> List[str]:
        """JavaScript parse errors with file and line (node --check), when Node.js is on the PC; the
        browser's own message ("Unexpected end of input") names neither."""
        import shutil
        import tempfile
        node = shutil.which("node", path=self.sb.env().get("PATH"))
        if not node:
            return []
        out: List[str] = []
        files = source_files(project, {".js", ".mjs", ".cjs"}, limit=40)
        tmp = tempfile.mkdtemp(prefix="jarvis_js_")
        try:
            for f in files:
                rel = os.path.relpath(f, project).replace("\\", "/")
                if re.search(r"(^|/)(dist|build|vendor|lib)/|\.min\.js$", rel) or os.path.getsize(f) > 1_000_000:
                    continue
                target = f
                text = _read(f)
                if f.endswith(".js") and re.search(r"^\s*(import\s[^(]|export\s)", text, re.M):
                    target = os.path.join(tmp, "check.mjs")          # an ES module: checked as one
                    with open(target, "w", encoding="utf-8") as fh:
                        fh.write(text)
                r = procs.run(f'"{node}" --check "{target}"', cwd=project, timeout=20)
                if r.get("code") not in (0, None) and not r.get("timed_out"):
                    msg = (r.get("output") or "").strip().splitlines()
                    where = next((ln for ln in msg if re.search(r":\d+\s*$", ln)), "")
                    line = re.search(r":(\d+)\s*$", where).group(1) if where else "?"
                    err = next((ln.strip() for ln in msg if re.match(r"\s*\w*Error\b", ln)), msg[-1] if msg else "")
                    out.append(f"{rel}:{line}: {err}"[:300])
        except Exception:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return out

    def _references(self, rep: Report, project: str, cfg: dict) -> None:
        """<script src>, <link href>, <img src> pointing at local files that do not exist."""
        root = os.path.join(project, cfg["web_root"])
        if cfg["web_root"] not in (".", "") and not os.path.isdir(root):
            return                                           # produced by the build: checked in the browser
        missing = []
        for f in source_files(root, {".html"}, limit=50):
            text = _read(f)
            for m in re.finditer(r"""<(?:script|img|link|audio|video|source)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"'#?]+)""",
                                 text, re.IGNORECASE):
                ref = m.group(1).strip()
                if re.match(r"^(https?:|data:|mailto:|javascript:|//|\{|\$)", ref, re.I) or not ref:
                    continue
                base = root if ref.startswith("/") else os.path.dirname(f)
                target = os.path.normpath(os.path.join(base, ref.lstrip("/")))
                if not os.path.exists(target):
                    missing.append(f"{os.path.relpath(f, project)} loads '{ref}', which does not exist")
        rep.add("references", "files the pages load exist", not missing, "\n".join(missing[:8]))
        # scripts that look up elements the pages do not have ("#add" in app.js, no id="add" anywhere)
        defined, used = set(), {}
        for f in source_files(root, {".html", ".htm", ".js", ".mjs", ".jsx", ".tsx", ".vue", ".svelte"}, limit=200):
            text = _read(f)
            defined.update(re.findall(r"""\bid\s*=\s*["'`{]?\s*["']?([A-Za-z][\w:-]*)""", text))
            defined.update(re.findall(r"""\.id\s*=\s*["'`]([A-Za-z][\w:-]*)""", text))
            defined.update(re.findall(r"""\bid\s*:\s*["'`]([A-Za-z][\w:-]*)""", text))
            if f.endswith((".js", ".mjs", ".jsx", ".tsx", ".vue", ".svelte")):
                rel = os.path.relpath(f, project).replace("\\", "/")
                for ident in re.findall(r"""getElementById\(\s*["'`]([A-Za-z][\w:-]*)["'`]\s*\)""", text) + \
                        re.findall(r"""querySelector(?:All)?\(\s*["'`]#([A-Za-z][\w-]*)["'`]\s*\)""", text):
                    used.setdefault(ident, rel)
        lost = sorted(i for i in used if i not in defined)
        if lost:
            by_file: Dict[str, List[str]] = {}
            for i in lost:
                by_file.setdefault(used[i], []).append("#" + i)
            # name the page to change and the element to add: small models act on the concrete line
            pages = [os.path.relpath(f, project).replace("\\", "/") for f in source_files(root, {".html", ".htm"}, limit=20)]
            page = next((pg for pg in pages if pg.endswith("index.html")), pages[0] if pages else "the HTML page")
            add = " ".join(f'<{"button" if re.search(r"(btn|button|add|plus|minus|sub|reset|inc|dec|clear|start|stop|go|send|save|del)", i, re.I) else "div"} id="{i}">' for i in lost[:4])
            rep.add("element-ids", "every element the scripts use exists", False,
                    "; ".join(f"{f} uses {', '.join(ids)}" for f, ids in by_file.items()) +
                    f" -- but {page} has no element with {'that id' if len(lost) == 1 else 'those ids'}. Add "
                    f"{'it' if len(lost) == 1 else 'them'} to {page} (e.g. {add}) -- or change the script.")

    # ------------------------------------------------------------ build + tests
    def _build_commands(self, cfg: dict) -> List[str]:
        cmds = list(cfg["build"])
        project = self.sb.project
        if os.path.exists(os.path.join(project, "package.json")) and \
                not any(re.search(r"\b(npm (i|install|ci)|yarn|pnpm)\b", c) for c in cmds):
            cmds.insert(0, "npm install")
        if os.path.exists(os.path.join(project, "requirements.txt")) and \
                not any(re.search(r"pip3? install", c) for c in cmds) and \
                _read(os.path.join(project, "requirements.txt")).strip():
            cmds.insert(0, "python -m pip install -r requirements.txt")
        return cmds

    def _cache_key(self, command: str) -> str:
        project = self.sb.project
        h = hashlib.sha1(command.encode())
        for name in ("package.json", "package-lock.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod"):
            p = os.path.join(project, name)
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    h.update(fh.read())
        for d in ("node_modules", ".venv"):
            h.update(str(os.path.isdir(os.path.join(project, d))).encode())
        return h.hexdigest()

    def _cache(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, "install_cache.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        with open(os.path.join(self.state_dir, "install_cache.json"), "w", encoding="utf-8") as fh:
            json.dump(cache, fh)

    def _build(self, rep: Report, cfg: dict) -> bool:
        cache = self._cache()
        for cmd in self._build_commands(cfg):
            key = self._cache_key(cmd)
            if INSTALL_CMD.search(cmd) and cache.get(cmd) == key:
                continue                                       # same dependencies, already installed
            self.log("verify", f"build: {cmd}")
            try:
                r = self.sb.run(cmd, quiet=True)
            except PermissionError as e:
                rep.add("build", "build", False, f"`{cmd}` was not allowed: {e}")
                return False
            if not r.ok:
                rep.add("build", "build", False, f"`{cmd}` failed (exit {r.get('code')}"
                                                 f"{', ' + r['killed'] if r.get('killed') else ''}):\n{r.tail(2500)}")
                return False
            if INSTALL_CMD.search(cmd):
                cache[cmd] = self._cache_key(cmd)
                self._save_cache(cache)
        rep.add("build", "build", True)
        return True

    def _test_commands(self, cfg: dict) -> List[str]:
        cmds = list(cfg["test"])
        if not cmds and _has_python_tests(self.sb.project):
            cmds = ["python -m pytest -q"]
        return cmds

    def _tests(self, rep: Report, cfg: dict) -> None:
        cmds = self._test_commands(cfg)
        integrity = self._test_integrity()
        if integrity:
            rep.add("test-integrity", "tests were not weakened", False, integrity)
        if not cmds:
            return
        for cmd in cmds:
            if re.search(r"\bpytest\b", cmd):
                self._ensure_pytest()
            self.log("verify", f"tests: {cmd}")
            r = self.sb.run(cmd, timeout=900, quiet=True)
            out = r.get("output") or ""
            if not r.ok:
                if re.search(r"no tests ran|collected 0 items", out, re.I) and r.get("code") == 5:
                    rep.add("tests", "tests", False, f"`{cmd}` found no tests")
                else:
                    rep.add("tests", "tests", False, f"`{cmd}` failed (exit {r.get('code')}):\n{r.tail(3000)}")
                return
        rep.add("tests", "tests", True)

    def _ensure_pytest(self) -> None:
        vp = self.sb.ensure_venv()
        if not vp:
            return
        r = procs.run(f'"{vp}" -c "import pytest"', self.sb.project, env=self.sb.env(), timeout=60)
        if not r.ok:
            self.log("verify", "installing pytest into the project's environment")
            procs.run(f'"{vp}" -m pip install -q pytest', self.sb.project, env=self.sb.env(), timeout=600)

    def _test_integrity(self) -> str:
        """Tests that existed at the last check must not disappear or lose their assertions."""
        record_path = os.path.join(self.state_dir, "tests.json")
        try:
            with open(record_path, encoding="utf-8") as fh:
                before = json.load(fh)
        except (OSError, ValueError):
            before = {}
        now: Dict[str, int] = {}
        project = self.sb.project
        for f in source_files(project, {".py", ".js", ".mjs", ".ts", ".rs", ".go", ".cs", ".java"}):
            rel = os.path.relpath(f, project).replace("\\", "/")
            if not re.search(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", rel):
                continue
            text = _read(f)
            real = len(re.findall(r"\bassert\b(?!\s+(True|true|1)\b)|\bexpect\s*\(|\bassert[A-Z]\w*\(|"
                                  r"\bassert_eq!|\bt\.(Error|Fatal)|Assert\.\w+\(", text))
            now[rel] = real
        problems = []
        for rel, n in before.items():
            if n > 0 and rel not in now:
                problems.append(f"{rel} was deleted")
            elif n > 0 and now.get(rel, 0) == 0:
                problems.append(f"{rel} no longer checks anything (its assertions were removed)")
        merged = dict(before)
        merged.update(now)
        for rel in list(merged):
            if rel not in now and not problems:
                merged.pop(rel)                     # removed AND accepted before: forget it
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(merged if problems else now, fh)
        return ("; ".join(problems) + ". Restore the tests and fix the program instead.") if problems else ""

    # ------------------------------------------------------------ runtime checks
    def _run_cmd(self, cfg: dict) -> str:
        run = cfg["run"]
        if not run or run == "static":
            return ""
        return run

    def _cli(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        run = self._run_cmd(cfg)
        if not run:
            rep.add("runs", "the program runs", False, 'jarvis.json has no "run" command (how to start the program)')
            self._criteria(rep, criteria, cfg)
            return
        args = str(cfg.get("sample_args") or "")
        stdin = cfg.get("sample_input")
        r = self.sb.run(f"{run} {args}".strip(), timeout=60, stdin=stdin if stdin is not None else "", quiet=True)
        out = (r.get("output") or "").strip()
        if r.get("timed_out"):
            rep.add("runs", "the program runs", False,
                    f"`{run} {args}` did not finish within 60 s (is it waiting for input? give it sample_input in "
                    f"jarvis.json, or make it a one-shot command)\n{r.tail(1200)}")
        elif not r.ok:
            rep.add("runs", "the program runs", False, f"`{run} {args}` exited with {r.get('code')}:\n{r.tail(2500)}")
        elif not out:
            rep.add("runs", "the program does something", False, f"`{run} {args}` printed nothing")
        else:
            rep.add("runs", "the program runs and prints output", True, out[-300:])
        self._criteria(rep, criteria, cfg)

    def _library(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        if not self._test_commands(cfg):
            rep.add("tests", "tests", False, "a library needs tests that use it (add tests and a \"test\" command)")
        self._criteria(rep, criteria, cfg)

    def _other(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        if self._run_cmd(cfg):
            self._cli(rep, cfg, criteria)
        else:
            self._criteria(rep, criteria, cfg)

    def _gui(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        run = self._run_cmd(cfg)
        if not run:
            rep.add("runs", "the program starts", False, 'jarvis.json has no "run" command')
            self._criteria(rep, criteria, cfg)
            return
        env = {}
        if not IS_WINDOWS and not os.environ.get("DISPLAY"):
            env = {"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy", "MPLBACKEND": "Agg"}
        bg = self.sb.start(run, "app", env_extra=env, log_path=os.path.join(self.state_dir, "app.log"))
        time.sleep(self.smoke_seconds)
        out = bg.output(60)
        crashed = re.search(r"Traceback \(most recent call last\)|Unhandled exception|panicked at|"
                            r"Exception in thread|Segmentation fault|error:", out)
        alive = bg.alive()
        code = bg.proc.poll() if bg.proc else None
        windows = _windows_of(bg.pid) if IS_WINDOWS and alive else []
        shot = None
        if windows:
            shot = _grab_window(windows[0], os.path.join(self.shots_dir, f"app-{int(time.time())}.png"))
            if shot:
                rep.shots.append(shot)
        bg_ok = (alive or code == 0) and not crashed
        if not alive and code not in (0, None):
            rep.add("runs", "the program starts and stays up", False, f"it exited with {code}:\n{out[-2500:]}")
        elif crashed:
            rep.add("runs", "the program starts without errors", False, out[-2500:])
        elif IS_WINDOWS and alive and not windows:
            rep.add("runs", "the program opens a window", False,
                    f"it is running but no window appeared within {self.smoke_seconds:.0f} s\n{out[-800:]}")
        else:
            rep.add("runs", "the program starts and stays up", bg_ok,
                    "window: " + windows[0]["title"] if windows else (out[-300:] or "running"), shot=shot)
        self.sb.stop("app")
        self._criteria(rep, criteria, cfg)

    def _file_product(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        if self._run_cmd(cfg):
            r = self.sb.run(self._run_cmd(cfg), timeout=600, quiet=True)
            if not r.ok:
                rep.add("runs", "the conversion runs", False, f"exit {r.get('code')}:\n{r.tail(2500)}")
                self._criteria(rep, criteria, cfg)
                return
        self._criteria(rep, criteria, cfg)

    def _gba(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        from . import files as filetools
        prod = cfg.get("product") or ""
        path = os.path.join(self.sb.project, prod) if prod else ""
        if not prod or not os.path.isfile(path):
            rep.add("runs", "the cartridge was built", False, f"no .gba file at '{prod or '(product not set)'}'")
        else:
            info = filetools.gba_header(path)
            rep.add("runs", "valid GBA cartridge header", info.get("ok", False),
                    info.get("problem") or f"title {info.get('title')!r}, code {info.get('code')!r}")
            shot = filetools.gba_screenshot(path, os.path.join(self.shots_dir, f"gba-{int(time.time())}.png"))
            if shot.get("path"):
                rep.shots.append(shot["path"])
                rep.add("runs-emulator", "it shows a picture in the emulator", not shot.get("blank"),
                        "the screen stayed blank" if shot.get("blank") else "", shot=shot["path"])
        self._criteria(rep, criteria, cfg)

    # ------------------------------------------------------------ servers and the browser
    def _serve(self, cfg: dict) -> Tuple[Optional[str], str]:
        """Start what the project needs to be opened in a browser; (url, problem)."""
        if cfg["run"] == "static" or not cfg["run"]:
            root = os.path.join(self.sb.project, cfg["web_root"])
            if not os.path.isdir(root):
                return None, f"the web folder '{cfg['web_root']}' does not exist (did the build produce it?)"
            if not os.path.exists(os.path.join(root, "index.html")) and cfg["url_path"] == "/":
                return None, f"there is no index.html in '{cfg['web_root']}'"
            from .serve import StaticServer
            srv = StaticServer(root).start()
            self._static = srv
            return f"http://127.0.0.1:{srv.port}{cfg['url_path']}", ""
        port = cfg["port"] or procs.free_port()
        bg = self.sb.start(cfg["run"], "server", env_extra={"PORT": str(port), "HOST": "127.0.0.1"},
                           log_path=os.path.join(self.state_dir, "server.log"))
        if not procs.wait_port(port, timeout=90, alive=bg.alive):
            out = bg.output(60)
            if not bg.alive():
                return None, f"`{cfg['run']}` stopped (exit {bg.proc.poll()}):\n{out[-2500:]}"
            return None, (f"`{cfg['run']}` did not start listening on port {port} within 90 s. Read the PORT "
                          f"environment variable (or set \"port\" in jarvis.json).\n{out[-1500:]}")
        return f"http://127.0.0.1:{port}{cfg['url_path']}", ""

    def _stop_static(self):
        srv = getattr(self, "_static", None)
        if srv is not None:
            srv.stop()
            self._static = None

    def _api(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        url, problem = self._serve(cfg)
        if not url:
            rep.add("runs", "the server starts", False, problem)
            self._criteria(rep, criteria, cfg, skip_reason="the server did not start")
            return
        rep.url = url
        r = procs.http_get(url, timeout=15)
        ok = r.get("status") is not None and r["status"] < 500
        rep.add("runs", "the server answers", ok, f"GET {cfg['url_path']} -> {r.get('status')} {r.get('error', '')}"
                if not ok else f"GET {cfg['url_path']} -> {r['status']}")
        self._criteria(rep, criteria, cfg, base_url=url)
        self._stop_static()

    def _web(self, rep: Report, cfg: dict, criteria: List[dict]) -> None:
        url, problem = self._serve(cfg)
        if not url:
            rep.add("runs", "the page can be opened", False, problem)
            self._criteria(rep, criteria, cfg, skip_reason="the page could not be opened")
            return
        rep.url = url
        try:
            if not self.browser or not _playwright_ok():
                self._web_basic(rep, url)
                self._criteria(rep, criteria, cfg, base_url=url, browser=None)
                return
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = _launch(p)
                try:
                    self._web_page(rep, browser, url, cfg)
                    self._criteria(rep, criteria, cfg, base_url=url, browser=browser)
                finally:
                    browser.close()
        except Exception as e:
            rep.add("runs", "browser check", False, f"the browser check could not run: {type(e).__name__}: {e}")
        finally:
            self._stop_static()

    def _web_basic(self, rep: Report, url: str) -> None:
        r = procs.http_get(url)
        rep.add("runs", "the page loads", r.get("status") == 200 and bool(r.get("body", "").strip()),
                f"GET -> {r.get('status')} {r.get('error', '')}")

    def _new_page(self, browser, errors: List[str], base: str):
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        page.set_default_timeout(5000)
        origin = re.match(r"https?://[^/]+", base).group(0)
        page.on("pageerror", lambda e: errors.append(f"uncaught error: {e}{_where(getattr(e, 'stack', '') or '')}"))
        page.on("console", lambda m: errors.append(f"console error: {m.text}{_at(m)}"[:500])
                if m.type == "error" and "favicon" not in m.text.lower() else None)
        page.on("requestfailed", lambda r: errors.append(f"could not load {r.url}") if
                r.url.startswith(origin) and "favicon" not in r.url else None)
        page.on("response", lambda r: errors.append(f"{r.status} for {r.url}") if
                r.status >= 400 and r.url.startswith(origin) and "favicon" not in r.url else None)
        return page

    def _web_page(self, rep: Report, browser, url: str, cfg: dict) -> None:
        errors: List[str] = []
        page = self._new_page(browser, errors, url)
        try:
            page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            rep.add("runs", "the page loads", False, f"{type(e).__name__}: {str(e)[:400]}")
            page.close()
            return
        page.wait_for_timeout(800)
        shot = os.path.join(self.shots_dir, f"page-{int(time.time() * 1000)}.png")
        try:
            page.screenshot(path=shot)
            rep.shots.append(shot)
        except Exception:
            shot = None
        blank = _blank(shot) if shot else None
        info = page.evaluate("""() => ({
            text: (document.body && document.body.innerText || '').trim().length,
            canvas: document.querySelectorAll('canvas').length,
            media: document.querySelectorAll('img,svg,video').length,
            title: document.title })""")
        rep.evidence += _page_evidence(page)
        if any("SyntaxError" in e or "Unexpected" in e for e in errors if "(at " not in e):
            bad = _unparsable_scripts(page)
            if bad:
                errors.append("these scripts do not parse: " + ", ".join(bad))
        rep.add("runs", "the page loads without errors", not errors,
                "\n".join(dict.fromkeys(errors))[:2500] if errors else f"title {info.get('title')!r}", shot=shot)
        visible = blank is False or (blank is None and (info["text"] or info["canvas"] or info["media"]))
        rep.add("visible", "the page shows something", bool(visible),
                "the page is blank (nothing visible was drawn)" if not visible else "")
        reacts, how = self._probe(page, cfg["kind"] == "game-web" or (info["canvas"] and not info["text"]))
        if reacts is not None:
            rep.add("reacts", "the page does something (reacts to input)", reacts, how)
        late = [e for e in errors if e not in rep.checks[-3]["detail"]] if errors else []
        if late:
            rep.add("runs-after-input", "no errors while using the page", False, "\n".join(dict.fromkeys(late))[:2000])
        page.close()

    def _probe(self, page, game: bool) -> Tuple[Optional[bool], str]:
        """Click, type and press keys like a person trying the page; did anything change?"""
        def state():
            try:
                return page.evaluate("""() => {
                  const b = document.body; if (!b) return '';
                  const c = [...document.querySelectorAll('canvas')].map(x => { try { return x.toDataURL().length + ':' +
                    x.toDataURL().slice(-64) } catch (e) { return 'tainted' } }).join(',');
                  return b.innerText + '|' + b.innerHTML.length + '|' + c; }""")
            except Exception:
                return ""

        def shot_hash():
            try:
                return hashlib.sha1(page.screenshot()).hexdigest()
            except Exception:
                return ""

        before = state()
        hb = shot_hash() if game else ""
        try:
            inputs = page.locator("input:visible:not([type=button]):not([type=submit]):not([type=checkbox])"
                                  ":not([type=radio]):not([type=hidden]), textarea:visible").all()[:4]
        except Exception:
            inputs = []
        for el in inputs:
            try:
                t = (el.get_attribute("type") or "text").lower()
                el.fill("42" if t in ("number", "range", "tel") else "hello")
            except Exception:
                pass
        if inputs and state() != before:
            return True, "typing into the inputs changed the page"
        try:
            buttons = page.locator("button:visible, [role=button]:visible, input[type=button]:visible, "
                                   "input[type=submit]:visible, a[href='#']:visible, [onclick]:visible").all()[:8]
        except Exception:
            buttons = []
        for el in buttons:
            try:
                el.click(timeout=2000)
                page.wait_for_timeout(250)
            except Exception:
                continue
            now = state()
            if now != before:
                return True, "clicking changed the page"
        if inputs:
            try:
                inputs[0].press("Enter")
                page.wait_for_timeout(250)
                if state() != before:
                    return True, "pressing Enter in the form changed the page"
            except Exception:
                pass
        selects = []
        try:
            selects = page.locator("select:visible").all()[:3]
        except Exception:
            pass
        for s in selects:
            try:
                opts = s.locator("option").all()
                if len(opts) > 1:
                    s.select_option(index=len(opts) - 1)
                    page.wait_for_timeout(200)
                    if state() != before:
                        return True, "choosing an option changed the page"
            except Exception:
                pass
        canvas = False
        try:
            canvas = page.locator("canvas").count() > 0
        except Exception:
            pass
        if game or canvas or not (buttons or inputs or selects):
            try:
                page.mouse.click(550, 380)
            except Exception:
                pass
            for key in ("Space", "ArrowRight", "ArrowUp", "ArrowLeft", "Enter", "KeyD", "KeyW"):
                try:
                    page.keyboard.down(key)
                    page.wait_for_timeout(180)
                    page.keyboard.up(key)
                except Exception:
                    pass
            page.wait_for_timeout(400)
            if state() != before or (hb and shot_hash() != hb):
                return True, "keys or time change what is shown"
            if not (buttons or inputs or selects or canvas):
                return None, "a page with nothing to click or type into"
            return False, ("nothing changed after clicking, typing and pressing keys (Space, arrows, Enter, W, D): "
                           "the game or page does not respond")
        return False, ("nothing changed after typing into the inputs and clicking every button: "
                       "the buttons do nothing")

    # ------------------------------------------------------------ acceptance criteria
    def _criteria(self, rep: Report, criteria: List[dict], cfg: Optional[dict], base_url: Optional[str] = None,
                  browser=None, skip_reason: str = "") -> None:
        for crit in criteria or []:
            cid = crit.get("id")
            name = crit.get("text", "")[:120]
            checks = [c for c in (crit.get("checks") or []) if isinstance(c, dict)]
            if skip_reason:
                rep.add(f"criterion-{cid}", name, False, f"not checked: {skip_reason}", criterion=cid)
                continue
            if not checks:
                rep.add(f"criterion-{cid}", name, False,
                        "no check proves this yet: add one in CHECKS (cli / web / http / file / run)", criterion=cid)
                continue
            fails, broken_only = [], True
            # the examiner's checks decide; the coder's own extra checks for the same criterion are
            # advisory (a wrong self-written check must not block a product the examiner accepts)
            has_jarvis = any(c.get("by") == "jarvis" for c in checks)
            for i, chk in enumerate(checks, 1):
                self._last_seen = ""
                ok, detail = self._one(chk, cfg or {}, base_url, browser)
                mine = chk.get("by") == "jarvis"
                by = "JARVIS's check" if mine else "your check"
                if not ok and detail.startswith(BROKEN):
                    if mine:                    # not the program's fault: the examiner re-writes it
                        rep.broken.append({"criterion": cid, "index": i - 1, "detail": detail[len(BROKEN):].strip()})
                        continue
                    detail = "this check of yours is malformed -- " + detail[len(BROKEN):].strip()
                if not ok and has_jarvis and not mine:
                    rep.evidence.append(f"note: your own extra check {i} for criterion {cid} fails (it does not "
                                        f"block; fix or drop it if it is wrong): {detail[:240]}")
                    continue
                if not ok:
                    broken_only = False
                    fails.append(f"check {i} ({by}, {chk.get('type', '?')}): {detail}")
                else:
                    broken_only = False
                    if self._last_seen:
                        rep.evidence.append(f"criterion {cid} check {i}: {self._last_seen}"[:400])
            if broken_only and checks:
                rep.add(f"criterion-{cid}", name, False, "not checked yet: JARVIS is re-writing its check",
                        criterion=cid)
                continue
            rep.add(f"criterion-{cid}", name, not fails, "\n".join(fails)[:2500], criterion=cid)

    def _one(self, chk: dict, cfg: dict, base_url: Optional[str], browser) -> Tuple[bool, str]:
        t = str(chk.get("type") or "").lower()
        try:
            if t == "cli":
                return self._check_cli(chk, cfg)
            if t == "run":
                return self._check_run(chk)
            if t == "file":
                return self._check_file(chk)
            if t == "http":
                return self._check_http(chk, base_url)
            if t == "web":
                if base_url is None:
                    return False, "a web check needs a web page (kind web or game-web)"
                if browser is None:
                    return False, "the browser is not available for this check"
                return self._check_web(chk, base_url, browser)
            if t == "gui":
                return True, "covered by the start-up check"
            return False, f"unknown check type '{t}' (use cli, run, file, http or web)"
        except PermissionError as e:
            return False, f"not allowed: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:300]}"

    @staticmethod
    def _expect(output: str, chk: dict) -> Tuple[bool, str]:
        exp = chk.get("expect") or []
        exp = [exp] if isinstance(exp, str) else exp
        exp = [str(e) for e in exp if str(e).strip()]
        not_exp = chk.get("not_expect") or []
        not_exp = [not_exp] if isinstance(not_exp, str) else not_exp
        missing = [e for e in exp if not shows(output, e)]
        if missing:
            return False, f"expected {missing!r} in the output, got: {output.strip()[-600:]!r}"
        present = [e for e in not_exp if str(e).strip() and shows(output, e)]
        if present:
            return False, f"did not expect {present!r} in the output: {output.strip()[-600:]!r}"
        regex = chk.get("regex")
        if regex and not re.search(str(regex), output, re.MULTILINE):
            return False, f"the output does not match /{regex}/: {output.strip()[-600:]!r}"
        return True, ""

    def _check_cli(self, chk: dict, cfg: dict) -> Tuple[bool, str]:
        run = self._run_cmd(cfg) if cfg else ""
        if not run:
            return False, 'jarvis.json has no "run" command to run the program with'
        if not (chk.get("expect") or chk.get("regex") or "expect_exit" in chk):
            return False, 'the check has no "expect" (what the output must contain)'
        args = chk.get("args", "")
        if isinstance(args, list):
            args = " ".join(shlex.quote(str(a)) if not IS_WINDOWS else (f'"{a}"' if " " in str(a) else str(a))
                            for a in args)
        stdin = chk.get("stdin")
        r = self.sb.run(f"{run} {args}".strip(), timeout=float(chk.get("timeout") or 60),
                        stdin=str(stdin) if stdin is not None else "", quiet=True)
        if r.get("killed"):
            return False, f"`{run} {args}` was stopped ({r['killed']}); output: {r.tail(500)!r}"
        want = chk.get("expect_exit", 0)
        if want is not None and r.get("code") != int(want):
            return False, f"`{run} {args}` exited with {r.get('code')} (expected {want}): {r.tail(800)}"
        self._last_seen = f"`{run} {args}` printed: {(r.get('output') or '').strip()[-200:]!r}"
        return self._expect(r.get("output") or "", chk)

    def _check_run(self, chk: dict) -> Tuple[bool, str]:
        cmd = str(chk.get("command") or "").strip()
        if not cmd:
            return False, 'a "run" check needs a "command"'
        r = self.sb.run(cmd, timeout=float(chk.get("timeout") or 120), stdin=chk.get("stdin"), quiet=True)
        want = chk.get("expect_exit", 0)
        if r.get("killed"):
            return False, f"`{cmd}` was stopped ({r['killed']})"
        if want is not None and r.get("code") != int(want):
            return False, f"`{cmd}` exited with {r.get('code')} (expected {want}): {r.tail(1200)}"
        return self._expect(r.get("output") or "", chk)

    def _check_file(self, chk: dict) -> Tuple[bool, str]:
        rel = str(chk.get("path") or "").strip()
        if not rel:
            return False, 'a "file" check needs a "path"'
        full = self.sb.resolve(rel, "check")
        if not os.path.exists(full):
            return False, f"{rel} does not exist"
        if os.path.isdir(full):
            return (True, "") if os.listdir(full) else (False, f"{rel} is an empty folder")
        size = os.path.getsize(full)
        if size < int(chk.get("min_bytes") or 1):
            return False, f"{rel} is too small ({size} bytes)"
        fmt = str(chk.get("format") or "").lower().lstrip(".")
        if fmt:
            from . import files as filetools
            got = filetools.detect(full)
            if not filetools.format_matches(got, fmt):
                return False, f"{rel} is not a valid .{fmt} file (it looks like: {got.get('kind')})"
        if chk.get("expect") or chk.get("regex"):
            return self._expect(_read(full), chk)
        return True, ""

    def _check_http(self, chk: dict, base_url: Optional[str]) -> Tuple[bool, str]:
        if not base_url:
            return False, "an http check needs a running server (kind api or web with a run command)"
        origin = re.match(r"https?://[^/]+", base_url).group(0)
        path = str(chk.get("path") or "/")
        method = str(chk.get("method") or "GET").upper()
        data, headers = None, {}
        if chk.get("json") is not None:
            data = json.dumps(chk["json"]).encode()
            headers["Content-Type"] = "application/json"
        elif chk.get("body") is not None:
            data = str(chk["body"]).encode()
        r = procs.http_get(origin + (path if path.startswith("/") else "/" + path), timeout=20, method=method,
                           data=data, headers=headers)
        want = chk.get("expect_status")
        if r.get("status") is None:
            return False, f"{method} {path}: no answer ({r.get('error')})"
        if want is not None and r["status"] != int(want):
            return False, f"{method} {path} -> {r['status']} (expected {want}): {r['body'][:400]!r}"
        if want is None and r["status"] >= 400:
            return False, f"{method} {path} -> {r['status']}: {r['body'][:400]!r}"
        return self._expect(r.get("body") or "", chk)

    def _check_web(self, chk: dict, base_url: str, browser) -> Tuple[bool, str]:
        origin = re.match(r"https?://[^/]+", base_url).group(0)
        errors: List[str] = []
        page = self._new_page(browser, errors, base_url)
        try:
            path = chk.get("path")
            page.goto(origin + str(path) if path else base_url, wait_until="load", timeout=30000)
            page.wait_for_timeout(300)
            steps = chk.get("steps") or []
            if not isinstance(steps, list) or not steps:
                return False, BROKEN + 'a web check needs "steps", e.g. [{"click": "#add"}, {"expect_text": "1"}]'
            if not any(any(k.startswith("expect") for k in s) for s in steps if isinstance(s, dict)):
                return False, BROKEN + "the steps never check anything (add an expect_text / expect_visible step)"
            last = self._page_state(page)
            for i, step in enumerate(steps, 1):
                if not isinstance(step, dict):
                    return False, BROKEN + f"step {i} is not an object"
                try:
                    ok, detail, last = self._step(page, step, last)
                except Exception as e:
                    msg = str(e)
                    if _SELECTOR_ERROR.search(msg):
                        return False, BROKEN + f"step {i} {json.dumps(step)[:120]}: not a valid selector ({msg.splitlines()[0][:160]})"
                    m = re.search(r"waiting for (?:get_by_\w+\(|locator\()(['\"])((?:\\.|(?!\1).)+)\1", msg)
                    if "Timeout" in msg and m:
                        what = m.group(2).replace('\\"', '"').replace("\\'", "'")
                        return False, (f"step {i} {json.dumps(step)[:120]}: there is no element matching {what} "
                                       f"on the page (waited 4 s){_controls_hint(page, what)}")
                    return False, f"step {i} {json.dumps(step)[:120]}: {type(e).__name__}: {msg.splitlines()[0][:240]}"
                if not ok:
                    if detail.startswith("unknown step"):
                        return False, BROKEN + f"step {i} {json.dumps(step)[:120]}: {detail}"
                    return False, f"step {i} {json.dumps(step)[:120]}: {detail}"
            bad = [e for e in errors if "favicon" not in e]
            if bad and not chk.get("allow_errors"):
                return False, "errors while doing it: " + "; ".join(dict.fromkeys(bad))[:600]
            try:
                shown = page.inner_text("body").strip().replace("\n", " / ")[:160]
            except Exception:
                shown = ""
            self._last_seen = " > ".join(f"{k} {json.dumps(v)[:40]}" for s_ in steps for k, v in s_.items()) + \
                f" -- page then shows: {shown}"
            return True, ""
        finally:
            page.close()

    @staticmethod
    def _page_state(page) -> str:
        try:
            return page.evaluate("""() => (document.body ? document.body.innerText + '|' + document.body.innerHTML.length : '')
              + [...document.querySelectorAll('canvas')].map(c => { try { return c.toDataURL().slice(-80) } catch (e) { return '' } }).join()""")
        except Exception:
            return ""

    def _step(self, page, s: dict, last: str):
        def sel_of(v):
            return v[0] if isinstance(v, (list, tuple)) else v
        if "click" in s:
            page.locator(str(s["click"])).first.click(timeout=4000)
            page.wait_for_timeout(int(s.get("wait", 150)))
        elif "dblclick" in s:
            page.locator(str(s["dblclick"])).first.dblclick(timeout=4000)
        elif "fill" in s:
            v = s["fill"]
            sel, text = (v[0], v[1]) if isinstance(v, (list, tuple)) and len(v) >= 2 else (v, s.get("text", ""))
            page.locator(str(sel)).first.fill(str(text), timeout=4000)
        elif "select" in s:
            v = s["select"]
            sel, value = (v[0], v[1]) if isinstance(v, (list, tuple)) else (v, s.get("value", ""))
            page.locator(str(sel)).first.select_option(str(value), timeout=4000)
        elif "press" in s:
            key = str(s["press"])
            if s.get("on"):
                page.locator(str(s["on"])).first.press(key, timeout=4000)
            else:
                hold = int(s.get("hold", 120))
                page.keyboard.down(key)
                page.wait_for_timeout(hold)
                page.keyboard.up(key)
            page.wait_for_timeout(int(s.get("wait", 150)))
        elif "wait" in s:
            page.wait_for_timeout(min(10000, int(s["wait"])))
        elif "expect_text" in s:
            want = str(s["expect_text"])
            where = s.get("in")
            got = page.locator(str(where)).first.inner_text(timeout=4000) if where else page.inner_text("body")
            if not shows(got, want):
                return False, f"expected text {want!r}, the {'element' if where else 'page'} shows {got.strip()[:300]!r}", last
        elif "expect_not_text" in s:
            want = str(s["expect_not_text"])
            got = page.inner_text("body")
            if shows(got, want):
                return False, f"did not expect {want!r} on the page", last
        elif "expect_visible" in s:
            loc = page.locator(str(sel_of(s["expect_visible"]))).first
            if not loc.is_visible(timeout=3000):
                return False, f"{s['expect_visible']!r} is not visible", last
        elif "expect_count" in s:
            v = s["expect_count"]
            sel, n = (v[0], int(v[1])) if isinstance(v, (list, tuple)) else (v, int(s.get("count", 1)))
            got = page.locator(str(sel)).count()
            if got != n:
                return False, f"expected {n} elements matching {sel!r}, found {got}", last
        elif "expect_changed" in s:
            now = self._page_state(page)
            if now == last:
                return False, "nothing on the page changed", last
            return True, "", now
        elif "eval" in s:
            got = page.evaluate(str(s["eval"]))
            if "expect" in s and str(got) != str(s["expect"]):
                return False, f"{s['eval']!r} returned {got!r}, expected {s['expect']!r}", last
        else:
            return False, "unknown step (use click, fill, select, press, wait, expect_text, expect_visible, " \
                          "expect_count, expect_changed or eval)", last
        return True, "", self._page_state(page) if "expect_changed" not in s else last

    # ------------------------------------------------------------ product
    def _product(self, rep: Report, cfg: dict) -> None:
        prod = cfg.get("product")
        if not prod:
            return
        full = os.path.join(self.sb.project, prod)
        ok = os.path.exists(full) and (os.path.isdir(full) or os.path.getsize(full) > 0)
        rep.add("product", "the deliverable exists", ok, "" if ok else f"'{prod}' (jarvis.json \"product\") does not exist")


# ============================================================== helpers
def _where(stack: str) -> str:
    m = re.search(r"/([^/\s:]+\.(?:m?js|cjs|html?)):(\d+)(?::\d+)?", stack or "")
    return f" (at {m.group(1)}:{m.group(2)})" if m else ""


def _at(msg) -> str:
    try:
        loc = msg.location or {}
        url = str(loc.get("url") or "")
        if url and not url.startswith(("data:", "about:")):
            return f" (at {url.rsplit('/', 1)[-1] or url}:{int(loc.get('lineNumber') or 0) + 1})"
    except Exception:
        pass
    return ""


def _unparsable_scripts(page) -> List[str]:
    """Which of the page's own (non-module) scripts fail to compile -- a parse error in the browser
    names no file (with Node.js on the PC the syntax check gives the line as well)."""
    try:
        return page.evaluate("""async () => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                if (s.type === 'module' || !s.src.startsWith(location.origin)) continue;
                try { new Function(await (await fetch(s.src)).text()); }
                catch (e) { out.push(s.src.split('/').pop() + ' (' + e.message + ')'); }
            }
            for (const s of document.querySelectorAll('script:not([src])')) {
                if (s.type === 'module') continue;
                try { new Function(s.textContent); } catch (e) { out.push('the inline <script> (' + e.message + ')'); }
            }
            return out; }""") or []
    except Exception:
        return []


def _controls_hint(page, selector: str) -> str:
    """A check looked for a button by its text and found none: say what the page's buttons do show,
    so the program can be given the label the request names ("+" -- not "Add")."""
    m = re.search(r""":(?:has-text|text-is)\((['"])(.*?)\1\)""", selector) or re.match(r"""^text=(['"]?)(.+?)\1$""", selector)
    if not m:
        return ""
    try:
        labels = page.evaluate("""() => [...document.querySelectorAll(
              'button, [role=button], input[type=button], input[type=submit], a')]
            .filter(e => e.getClientRects().length)
            .map(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()).filter(Boolean).slice(0, 12)""")
    except Exception:
        return ""
    shown = ", ".join(repr(str(x)[:30]) for x in labels) if labels else "none"
    return (f" -- the request needs an element whose visible text is {m.group(2)!r} (e.g. a button showing exactly "
            f"{m.group(2)!r}); the page's buttons show: {shown}")


BROKEN = "[broken check] "
_SELECTOR_ERROR = re.compile(r"parsing css selector|is not a valid selector|Unexpected token|Unknown engine|"
                             r"Unsupported token|Malformed selector|Invalid selector|SyntaxError: Failed to execute "
                             r"'querySelector", re.IGNORECASE)
_PW_OK: Optional[bool] = None


def _playwright_ok() -> bool:
    global _PW_OK
    if _PW_OK is None:
        try:
            import playwright.sync_api  # noqa: F401
            _PW_OK = True
        except Exception:
            _PW_OK = False
    return _PW_OK


def _launch(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception as first:
        # first use on this PC: fetch the browser engine (the same one "read this page" uses)
        import subprocess
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], capture_output=True,
                           text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"the browser engine is not installed and could not be installed: {first}")
        return p.chromium.launch(headless=True)


_EVIDENCE_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('placeholder') || el.getAttribute('alt') || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
  const controls = [...document.querySelectorAll('button, [role=button], input, select, textarea, a[href]')]
    .filter(vis).slice(0, 40).map(el => {
      const t = el.tagName.toLowerCase(), type = el.getAttribute('type');
      return t + (type && t === 'input' ? '[' + type + ']' : '') + " '" + label(el) + "'" + (el.id ? ' #' + el.id : ''); });
  const heads = [...document.querySelectorAll('h1,h2,h3')].filter(vis).map(h => h.innerText.trim().slice(0, 60)).slice(0, 8);
  return { controls, heads, text: (document.body ? document.body.innerText : '').trim().replace(/\\n{2,}/g, '\\n').slice(0, 900),
           canvas: document.querySelectorAll('canvas').length };
}"""


def _page_evidence(page) -> List[str]:
    try:
        d = page.evaluate(_EVIDENCE_JS)
    except Exception:
        return []
    out = [f"page controls: {', '.join(d.get('controls') or []) or 'none'}"]
    if d.get("heads"):
        out.append(f"headings: {' | '.join(d['heads'])}")
    if d.get("canvas"):
        out.append(f"canvas elements: {d['canvas']}")
    out.append("visible text when the page opens: " + (d.get("text") or "(none)").replace("\n", " / ")[:600])
    return out


def _blank(path: Optional[str]) -> Optional[bool]:
    """True when a screenshot shows nothing: (almost) every pixel is the background colour. Counted at
    full size -- shrinking first averages a small but real page (a "0" and two buttons) into "blank"."""
    if not path:
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            colours = rgb.getcolors(maxcolors=4096)
            if colours is None:                       # thousands of colours: something is drawn
                return False
            _n, bg = max(colours)
            near = sum(n for n, c in colours if max(abs(c[0] - bg[0]), abs(c[1] - bg[1]), abs(c[2] - bg[2])) <= 24)
            return rgb.width * rgb.height - near < 50
    except Exception:
        return None


def _windows_of(pid: Optional[int]) -> List[dict]:
    """Visible top-level windows of a process tree (Windows)."""
    if not IS_WINDOWS or not pid:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        pids = set(procs.tree_pids(pid))
        user32 = ctypes.windll.user32
        found: List[dict] = []
        cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value in pids:
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.right - rect.left > 40 and rect.bottom - rect.top > 40:
                    found.append({"hwnd": hwnd, "title": buf.value, "rect": (rect.left, rect.top, rect.right, rect.bottom)})
            return True

        user32.EnumWindows(cb_t(cb), 0)
        return found
    except Exception:
        return []


def _grab_window(win: dict, path: str) -> Optional[str]:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=win["rect"], all_screens=True)
        img.save(path)
        return path
    except Exception:
        return None
