"""
dev/history.py -- the project's history: one commit per finished request.

With Git (installed with JARVIS's developer tools, or already on the PC) every request becomes a
commit in project/.git, so "undo the last request" reverts exactly what that request changed
and every earlier state can be looked at. Without Git, a zip snapshot of the project is taken
before each request and "undo" restores it.

Dependency folders (node_modules, .venv, build output) are never put in the history.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from typing import Optional

IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
IGNORE = ["node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".pytest_cache/", "dist/", "build/",
          "target/", "obj/", ".next/", ".cache/", ".jarvis/", "*.log"]
SNAP_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "target", "obj", ".next",
             ".cache", ".jarvis"}


def git_exe() -> Optional[str]:
    g = shutil.which("git")
    if g:
        return g
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            p = os.path.join(base, "Git", "cmd", "git.exe") if base else ""
            if p and os.path.exists(p):
                return p
            p = os.path.join(base, "Programs", "Git", "cmd", "git.exe") if base else ""
            if p and os.path.exists(p):
                return p
    return None


class History:
    def __init__(self, project_dir: str, snapshots_dir: str):
        self.project = project_dir
        self.snaps = snapshots_dir
        self.git = git_exe()

    # ------------------------------------------------------------ git helpers
    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        assert self.git
        r = subprocess.run([self.git, "-c", "user.name=JARVIS", "-c", "user.email=jarvis@localhost",
                            "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
                            "-c", "commit.gpgsign=false", *args],
                           cwd=self.project, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=CREATE_NO_WINDOW, timeout=300)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r

    def uses_git(self) -> bool:
        return bool(self.git)

    def init(self) -> None:
        os.makedirs(self.project, exist_ok=True)
        gi = os.path.join(self.project, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(IGNORE) + "\n")
        if self.git and not os.path.isdir(os.path.join(self.project, ".git")):
            self._git("init", "-q", check=True)
            self._git("add", "-A")
            self._git("commit", "-q", "--allow-empty", "-m", "Workspace created")

    def head(self) -> Optional[str]:
        if not self.git or not os.path.isdir(os.path.join(self.project, ".git")):
            return None
        r = self._git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None

    # ------------------------------------------------------------ request boundaries
    def begin(self, rid: str) -> Optional[str]:
        """Mark where a request starts; returns the base (a commit or a snapshot name)."""
        self.init()
        if self.git:
            # anything changed outside a request (by hand) is kept as its own commit first
            self._git("add", "-A")
            if self._git("diff", "--cached", "--quiet").returncode != 0:
                self._git("commit", "-q", "-m", "Changes made outside JARVIS")
            return self.head()
        return self._snapshot(f"before-{rid}")

    def commit(self, rid: str, message: str) -> Optional[str]:
        """Record what a finished request changed."""
        if self.git:
            self._git("add", "-A")
            msg = (message or "JARVIS request").strip().splitlines()[0][:200]
            self._git("commit", "-q", "--allow-empty", "-m", f"{msg}\n\nJARVIS request {rid}")
            return self.head()
        return self._snapshot(f"after-{rid}")

    def changed_files(self, base: Optional[str]) -> list:
        if not (self.git and base):
            return []
        r = self._git("diff", "--name-only", base, "HEAD")
        return [x for x in r.stdout.splitlines() if x.strip()]

    def undo(self, base: Optional[str], commit: Optional[str]) -> str:
        """Put the project back to how it was before the request. Returns what was done."""
        if self.git and commit:
            r = self._git("revert", "--no-edit", commit)
            if r.returncode == 0:
                return "reverted"
            self._git("revert", "--abort")
            if base:
                # later changes touch the same lines: restore the files to the base state
                self._git("checkout", base, "--", ".")
                self._git("add", "-A")
                self._git("commit", "-q", "--allow-empty", "-m", f"Undo request (restored {base[:8]})")
                return "restored"
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        if base and os.path.exists(os.path.join(self.snaps, base + ".zip")):
            self._restore(base)
            return "restored"
        raise RuntimeError("there is no saved state to go back to")

    def reset(self, base: Optional[str]) -> None:
        """Put the project back exactly as it was at `base` (dependency folders are kept)."""
        if self.git and base and not base.startswith("before-"):
            r = self._git("reset", "--hard", base)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:300])
            self._git("clean", "-fd", "-e", ".venv", "-e", "node_modules", "-e", ".jarvis")
            return
        if base and os.path.exists(os.path.join(self.snaps, base + ".zip")):
            self._restore(base)
            return
        raise RuntimeError("there is no saved state to go back to")

    # ------------------------------------------------------------ snapshots (no git)
    def _snapshot(self, name: str) -> str:
        os.makedirs(self.snaps, exist_ok=True)
        path = os.path.join(self.snaps, name + ".zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for base, dirs, files in os.walk(self.project):
                dirs[:] = [d for d in dirs if d not in SNAP_SKIP]
                for f in files:
                    full = os.path.join(base, f)
                    z.write(full, os.path.relpath(full, self.project))
        return name

    def _restore(self, name: str) -> None:
        path = os.path.join(self.snaps, name + ".zip")
        for entry in os.listdir(self.project):
            if entry in SNAP_SKIP:
                continue
            full = os.path.join(self.project, entry)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass
        with zipfile.ZipFile(path) as z:
            z.extractall(self.project)
        os.utime(self.project, (time.time(), time.time()))
