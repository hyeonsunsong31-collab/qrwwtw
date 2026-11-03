"""File skills (requirement 20). Cross-platform via pathlib."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .base import ok, fail

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "AppData", "$Recycle.Bin",
             "Windows", "Program Files", "Program Files (x86)", ".cache"}


def _user_roots() -> List[Path]:
    home = Path.home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    roots = [home / n for n in names if (home / n).exists()]
    return roots or [home]


def search_files(query: str, roots: Optional[List[str]] = None, limit: int = 50) -> dict:
    q = (query or "").strip().lower()
    if not q:
        return fail("What should I search for, sir?")
    search_roots = [Path(r) for r in roots] if roots else _user_roots()
    hits: List[str] = []
    for root in search_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if q in fn.lower():
                    hits.append(str(Path(dirpath) / fn))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    speak = (f"I found {len(hits)} file(s) matching '{query}', sir."
             if hits else f"No files matching '{query}', sir.")
    return ok(speak, {"query": query, "count": len(hits), "files": hits})


def read_file(path: str, max_bytes: int = 20000) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        return fail(f"I can't find a file at {path}, sir.")
    try:
        data = p.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return ok(f"Here are the first {len(data)} bytes of {p.name}, sir.",
                  {"path": str(p), "text": text, "truncated": p.stat().st_size > max_bytes})
    except OSError as e:
        return fail(f"I couldn't read {p.name}, sir.", str(e))


def write_file(path: str, content: str = "") -> dict:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ok(f"I've written {p.name}, sir.", {"path": str(p), "bytes": len(content.encode())})
    except OSError as e:
        return fail(f"I couldn't write {path}, sir.", str(e))


def delete_file(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return fail(f"There's nothing at {path} to delete, sir.")
    try:
        if p.is_dir():
            import shutil
            shutil.rmtree(p)
        else:
            p.unlink()
        return ok(f"I've deleted {p.name}, sir.", {"path": str(p)})
    except OSError as e:
        return fail(f"I couldn't delete {path}, sir.", str(e))


def list_dir(path: str = "~") -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():
        return fail(f"{path} isn't a folder, sir.")
    entries = sorted(os.listdir(p))[:200]
    return ok(f"{len(entries)} item(s) in {p.name or str(p)}, sir.",
              {"path": str(p), "entries": entries})
