"""
dev/tools.py -- the coding model's hands: files and a terminal, scoped to one workspace.

Permission model (you chose "workspace-scoped"):
  * inside the workspace (input/, project/, output/): reading, writing, deleting and running
    commands happen without asking (category dev_workspace, default "allow")
  * a path outside the workspace, in a file action or in a command: asks first (dev_outside)
  * installing software for the whole PC (winget, npm -g, dotnet tool -g, ...): asks first
    (install_software); packages INSIDE the project (pip into the project's own .venv, npm
    install into node_modules) are part of the work and do not ask
  * a command that can destroy data (the same list as JARVIS's shell commands): always asks
Every action is logged to the request's log.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional

from . import procs
from .workspaces import SKIP_DIRS

AskFn = Callable[..., bool]          # ask(category, detail, force=False, risk=None) -> granted
IS_WINDOWS = sys.platform.startswith("win")
TEXT_LIMIT = 60_000                  # characters of a file shown to the model at once
WRITE_LIMIT = 4_000_000              # largest file the model may write in one go

_INSTALL = re.compile(
    r"\b(winget|choco|chocolatey|scoop|apt(-get)?|dnf|yum|pacman|brew|snap|msiexec)\b\s+\S*\s*(install|-i|/i)|"
    r"\bnpm\s+(i|install|add)\b[^&|;]*\s(-g|--global)\b|\byarn\s+global\s+add\b|\bpnpm\s+(add|i|install)\b[^&|;]*\s(-g|--global)\b|"
    r"\bdotnet\s+tool\s+install\b[^&|;]*\s(-g|--global)\b|\bcargo\s+install\b|\bgo\s+install\b|"
    r"\bpip3?\s+install\b[^&|;]*\s--user\b|\bgem\s+install\b|\bpipx\s+install\b|\bsetx?\s+path\b",
    re.IGNORECASE)
_SERVER = re.compile(
    r"\b(npm|pnpm|yarn)\s+(run\s+)?(start|dev|serve|preview)\b|\bnpx\s+(vite|serve|http-server|live-server)\b|"
    r"\b(flask|uvicorn|gunicorn|hypercorn)\b|\bhttp\.server\b|\bnodemon\b|\brails\s+s(erver)?\b|"
    r"\bphp\s+-S\b|\bdotnet\s+(watch|run)\b|\bng\s+serve\b", re.IGNORECASE)
_WIN_ABS = re.compile(r"(?<![\w/])([A-Za-z]:[\\/][^\s\"'|&;<>]*|\\\\[^\s\"'|&;<>]+)")
_POSIX_ABS = re.compile(r"(?<![\w.~$:/-])(/[A-Za-z0-9_.@+-][^\s\"'|&;<>]*)")
_HOME = re.compile(r"(?<![\w$])(~[\\/][^\s\"'|&;<>]*|%USERPROFILE%[^\s\"'|&;<>]*|\$HOME\b[^\s\"'|&;<>]*)",
                   re.IGNORECASE)
# paths a command may name without asking: tools, devices and temporary files
POSIX_OK = ("/dev/null", "/dev/stdout", "/dev/stderr", "/usr/", "/bin/", "/opt/", "/etc/os-release", "/proc/",
            "/tmp/", "/sbin/", "/lib", "/var/tmp/", "/api/", "/static/", "/assets/", "/index", "/health")


def _danger(command: str):
    try:
        from agent.skills.shell import guard
        return guard(command)
    except Exception:                         # the dev package also runs on its own (tests, cli)
        return False, ""


class Sandbox:
    def __init__(self, workspace_dir: str, project_dir: str, ask: Optional[AskFn] = None,
                 log: Optional[Callable[[str, str], None]] = None, python: str = sys.executable,
                 mem_limit_mb: float = 0.0, should_stop: Optional[Callable[[], bool]] = None,
                 tool_dirs: Optional[List[str]] = None, data_dir: Optional[str] = None):
        self.root = os.path.abspath(workspace_dir)
        self.project = os.path.abspath(project_dir)
        self.ask = ask or (lambda *a, **k: False)
        self.log = log or (lambda kind, text: None)
        self.python = python
        self.mem_limit_mb = mem_limit_mb
        self.should_stop = should_stop
        self.tool_dirs = [d for d in (tool_dirs or []) if d]
        self.data_dir = data_dir
        self.granted: set = set()             # outside paths / installs already allowed this job
        self.background: Dict[str, procs.Background] = {}
        self.touched: List[str] = []          # files written, most recent last
        os.makedirs(self.project, exist_ok=True)

    # ------------------------------------------------------------ paths
    def inside(self, path: str) -> bool:
        p = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(self.root)
        return p == root or p.startswith(root + os.sep)

    def resolve(self, path: str, purpose: str = "read") -> str:
        """Absolute path for a project-relative (or absolute) path; outside the workspace asks."""
        path = str(path or ".").strip().strip('"').strip("'")
        if path.startswith("project/") or path.startswith("project\\"):
            path = path[8:]
        full = path if os.path.isabs(path) else os.path.join(self.project, path)
        full = os.path.abspath(os.path.expanduser(full))
        if self.inside(full):
            return full
        key = os.path.normcase(full)
        if key not in self.granted:
            if not self.ask("dev_outside", f"{purpose} outside the workspace: {full}", risk=0.6):
                raise PermissionError(f"not allowed to {purpose} {full} (it is outside the workspace)")
            self.granted.add(key)
        return full

    def rel(self, full: str) -> str:
        try:
            r = os.path.relpath(full, self.project)
        except ValueError:
            return full
        return r.replace("\\", "/") if not r.startswith("..") else full

    # ------------------------------------------------------------ files
    def list_files(self, path: str = ".", max_entries: int = 300) -> str:
        base = self.resolve(path, "list")
        if not os.path.isdir(base):
            return f"{path}: not a folder"
        lines: List[str] = []
        for cur, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            rel = os.path.relpath(cur, base)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > 6:
                dirs[:] = []
                continue
            for f in sorted(files):
                full = os.path.join(cur, f)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                lines.append(f"{os.path.relpath(full, base).replace(os.sep, '/')}  ({_size(size)})")
                if len(lines) >= max_entries:
                    lines.append(f"... more files not shown")
                    return "\n".join(lines)
        return "\n".join(lines) or "(empty)"

    def read_file(self, path: str, max_chars: int = TEXT_LIMIT) -> str:
        full = self.resolve(path, "read")
        if not os.path.isfile(full):
            return f"{path}: no such file"
        with open(full, "rb") as fh:
            raw = fh.read(max_chars * 4 + 1)
        if b"\x00" in raw[:4096]:
            return f"{path}: binary file ({_size(os.path.getsize(full))}); use `jarvis-tool inspect {path}`"
        text = raw.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (file continues; {_size(os.path.getsize(full))} total)"
        return text

    def write_file(self, path: str, content: str) -> str:
        full = self.resolve(path, "write")
        content = content if isinstance(content, str) else str(content)
        _refuse_leaks(path, content)
        if len(content) > WRITE_LIMIT:
            raise ValueError(f"{path}: content too large ({len(content)} characters)")
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        existed = os.path.exists(full)
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        self._touch(full)
        n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        self.log("write", f"{'updated' if existed else 'created'} {self.rel(full)} ({n} lines)")
        return f"{'updated' if existed else 'created'} {self.rel(full)} ({n} lines)"

    def edit_file(self, path: str, search: str, replace: str) -> str:
        full = self.resolve(path, "write")
        if not os.path.isfile(full):
            raise FileNotFoundError(f"{path}: no such file (use FILE to create it)")
        with open(full, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
        _refuse_leaks(path, replace)
        if search and search in text:
            new = text.replace(search, replace, 1)
        else:
            new = _fuzzy_replace(text, search, replace)
            if new is None:
                raise ValueError(f"{path}: the SEARCH text was not found exactly; read the file and try again, "
                                 f"or write the whole file")
        if new == text:
            # SEARCH and REPLACE are the same (measured: a 3B model "fixed" a page this way for steps)
            return (f"{self.rel(full)} NOT changed: your REPLACE text is the same as the SEARCH text. Change something, "
                    f"or edit the file the failure names instead.")
        with open(full, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
        self._touch(full)
        self.log("edit", f"edited {self.rel(full)}")
        return f"edited {self.rel(full)}"

    def delete_file(self, path: str) -> str:
        full = self.resolve(path, "delete")
        if full == self.project or full == self.root:
            raise PermissionError("refusing to delete the whole project")
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.exists(full):
            os.remove(full)
        else:
            return f"{path}: already gone"
        self.log("delete", f"deleted {self.rel(full)}")
        return f"deleted {self.rel(full)}"

    def _touch(self, full: str) -> None:
        rel = self.rel(full)
        if rel in self.touched:
            self.touched.remove(rel)
        self.touched.append(rel)
        del self.touched[:-30]

    # ------------------------------------------------------------ environment
    def venv_dir(self) -> str:
        return os.path.join(self.project, ".venv")

    def venv_python(self) -> Optional[str]:
        p = os.path.join(self.venv_dir(), "Scripts", "python.exe") if IS_WINDOWS else \
            os.path.join(self.venv_dir(), "bin", "python")
        return p if os.path.exists(p) else None

    def ensure_venv(self) -> Optional[str]:
        """The project's own Python environment (packages installed for it stay in it)."""
        vp = self.venv_python()
        if vp:
            return vp
        self.log("setup", "creating the project's Python environment (.venv)")
        r = subprocess.run([self.python, "-m", "venv", self.venv_dir()], capture_output=True, text=True,
                           creationflags=procs.CREATE_NO_WINDOW if IS_WINDOWS else 0, timeout=300)
        if r.returncode != 0:
            # some Python builds ship without ensurepip: an environment without pip still runs code
            subprocess.run([self.python, "-m", "venv", "--without-pip", "--system-site-packages",
                            self.venv_dir()], capture_output=True, timeout=300,
                           creationflags=procs.CREATE_NO_WINDOW if IS_WINDOWS else 0)
        return self.venv_python()

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        path = [os.path.join(self.project, "node_modules", ".bin")]
        if self.venv_python():
            path.insert(0, os.path.dirname(self.venv_python()))
            env["VIRTUAL_ENV"] = self.venv_dir()
        path += self.tool_dirs
        if self.data_dir:
            path.append(os.path.join(self.data_dir, "bin"))
        env["PATH"] = os.pathsep.join(path + [env.get("PATH", "")])
        env.pop("PYTHONHOME", None)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "CI": "1",
                    "BROWSER": "none", "npm_config_yes": "true", "npm_config_fund": "false",
                    "npm_config_audit": "false", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "FORCE_COLOR": "0",
                    "NO_COLOR": "1", "JARVIS_PROJECT": self.project})
        # `jarvis-tool` = the developer's file utilities (inspect, gba, patches, convert)
        env["JARVIS_TOOL"] = f'"{self.python}" -m dev.cli'
        env["PYTHONPATH"] = os.pathsep.join([_repo_root()] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return env

    # ------------------------------------------------------------ terminal
    def check_command(self, command: str) -> None:
        """Ask first where the policy says so; raise PermissionError when refused."""
        danger, why = _danger(command)
        if danger:
            if not self.ask("dev_workspace", f"Command in the workspace: {command}\n(always confirmed: it {why})",
                            force=True, risk=0.95):
                raise PermissionError(f"you did not allow this command (it {why})")
        if _INSTALL.search(command):
            key = "install:" + command.strip().lower()
            if key not in self.granted:
                if not self.ask("install_software", f"Install software for the whole PC: {command}", risk=0.7):
                    raise PermissionError("you did not allow installing software for the whole PC; "
                                          "install packages inside the project instead (pip into .venv, "
                                          "npm install without -g)")
                self.granted.add(key)
        for path in self.outside_paths(command):
            key = os.path.normcase(path)
            if key in self.granted or any(key.startswith(g + os.sep) for g in self.granted):
                continue
            if not self.ask("dev_outside", f"A command uses a path outside the workspace: {path}\nCommand: {command}",
                            risk=0.65):
                raise PermissionError(f"not allowed to use {path} (it is outside the workspace)")
            self.granted.add(key)

    def outside_paths(self, command: str) -> List[str]:
        found = []
        cands = _WIN_ABS.findall(command) + _HOME.findall(command)
        if not IS_WINDOWS:
            cands += [m for m in _POSIX_ABS.findall(command)]
        for raw in cands:
            raw = raw.rstrip(".,)")
            if raw.lower().startswith(("http:", "https:")):
                continue
            if raw.startswith("/") and raw.startswith(POSIX_OK):
                continue
            p = raw.replace("%USERPROFILE%", os.path.expanduser("~")).replace("%userprofile%", os.path.expanduser("~"))
            p = p.replace("$HOME", os.path.expanduser("~"))
            p = os.path.abspath(os.path.expanduser(p))
            if self.inside(p) or self._tool_path(p):
                continue
            found.append(p)
        return found

    def _tool_path(self, p: str) -> bool:
        np_ = os.path.normcase(p)
        allowed = [os.path.dirname(self.python), os.path.dirname(os.path.dirname(self.python))] + self.tool_dirs
        if IS_WINDOWS:
            allowed += [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", ""),
                        os.environ.get("ProgramFiles(x86)", ""), os.environ.get("TEMP", "")]
        if self.data_dir:
            allowed.append(os.path.join(self.data_dir, "bin"))
        for a in allowed:
            if a and np_.startswith(os.path.normcase(os.path.abspath(a))):
                # reading tools is fine; writing into Windows or Program Files is not
                return True
        return False

    def run(self, command: str, timeout: Optional[float] = None, stdin: Optional[str] = None,
            cwd: str = ".", quiet: bool = False) -> procs.RunResult:
        command = (command or "").strip()
        if not command:
            raise ValueError("empty command")
        self.check_command(command)
        wd = self.resolve(cwd, "run in")
        if re.search(r"\b(pip3?|python3?|py|pytest)\b", command) and not self.venv_python():
            self.ensure_venv()
        server = bool(_SERVER.search(command))
        installing = bool(re.search(r"\b(install|ci|add|restore|build|publish|compile|make|cargo|gradle|mvn)\b",
                                    command, re.IGNORECASE))
        limit = float(timeout) if timeout else (900.0 if installing else 120.0)
        limit = max(5.0, min(limit, 3600.0))
        if server:
            limit = min(limit, 25.0)
        command = command.replace("jarvis-tool", self.env()["JARVIS_TOOL"]) if "jarvis-tool" in command else command
        if not quiet:
            self.log("run", f"$ {command}")
        r = procs.run(command, wd, env=self.env(), timeout=limit, mem_limit_mb=self.mem_limit_mb,
                      stdin_text=stdin, should_stop=self.should_stop)
        if server and r.get("timed_out"):
            r["note"] = ("this command starts a server, so it never ends by itself; it ran for "
                         f"{int(limit)} s. Put it in CONFIG as \"run\" and the checker will start it.")
        if not quiet:
            self.log("result", f"exit {r.get('code')}" + (f" ({r['killed']})" if r.get("killed") else "") +
                     f" after {r.get('seconds')} s")
        return r

    def start(self, command: str, name: str, cwd: str = ".", env_extra: Optional[Dict[str, str]] = None,
              log_path: Optional[str] = None) -> procs.Background:
        self.check_command(command)
        self.stop(name)
        env = self.env()
        env.update(env_extra or {})
        bg = procs.Background(command, self.resolve(cwd, "run in"), env=env, log_path=log_path, name=name).start()
        self.background[name] = bg
        return bg

    def stop(self, name: str) -> None:
        bg = self.background.pop(name, None)
        if bg is not None:
            bg.stop()

    def stop_all(self) -> None:
        for name in list(self.background):
            self.stop(name)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


_CODE_EXT = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".cs", ".java", ".go", ".rs", ".c", ".cpp",
             ".h", ".hpp", ".kt", ".swift", ".rb", ".php", ".lua", ".gd", ".dart", ".vue", ".svelte")


def _refuse_leaks(path: str, content: str) -> None:
    """A file must not receive the reply's own protocol: merge markers, or (in code) lines such as
    "EDIT: index.html" / "DONE" -- that happens when a block was left open, and it breaks the file."""
    from .protocol import _STRICT
    code = (path or "").lower().endswith(_CODE_EXT)
    for line in (content or "").splitlines():
        if re.match(r"^(<{7}|>{7})(\s|$)", line) or (code and _STRICT.match(line)):
            raise ValueError(f"{path}: not written -- the content contains {line.strip()[:40]!r}, a line of your reply's "
                             f"format, not of the file (a block was left open). Write the file's own content only.")


def _fuzzy_replace(text: str, search: str, replace: str) -> Optional[str]:
    """SEARCH text that differs only in indentation or trailing spaces still matches once."""
    if not search.strip():
        return None
    norm = lambda s: [ln.strip() for ln in s.strip("\n").splitlines()]
    want = norm(search)
    lines = text.splitlines(keepends=True)
    stripped = [ln.strip() for ln in lines]
    n = len(want)
    hits = [i for i in range(len(lines) - n + 1) if stripped[i:i + n] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    indent = re.match(r"\s*", lines[i]).group(0)
    rep_lines = replace.strip("\n").splitlines()
    base_indent = min((len(l) - len(l.lstrip()) for l in rep_lines if l.strip()), default=0)
    eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
    new_block = "".join(indent + l[base_indent:] + eol if l.strip() else eol for l in rep_lines)
    return "".join(lines[:i]) + new_block + "".join(lines[i + n:])
