"""
dev/workspaces.py -- workspaces ("session slots") and their stacked requests.

  data/workspaces/
    selected.json                 which workspace new requests go to
    <id>/
      workspace.json              name, kind, created, status, product (the latest deliverable)
      requests.json               the stacked requests, oldest first, with status and progress
      input/                      copies of the files you sent (the originals are never touched)
      project/                    the working tree (the code JARVIS writes), with history
      output/                     deliverables: the program, the converted file, the web build
      logs/<request id>.jsonl     everything JARVIS did for that request, step by step
      logs/<request id>/          screenshots and the check report

Request status: queued -> working -> done | failed | stopped   (paused while you pause it;
"undone" after "undo the last request"). After a restart, "working" requests are queued again
and pick up from the project files as they are.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

ACTIVE = ("queued", "working", "paused")
FINISHED = ("done", "failed", "stopped", "undone")
MAX_ATTACH_BYTES = 8 * 1024 ** 3          # 8 GB per request, a sanity limit
_SLUG = re.compile(r"[^a-z0-9]+")


def _now() -> float:
    return round(time.time(), 3)


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def slug(name: str) -> str:
    s = _SLUG.sub("-", (name or "").lower()).strip("-")
    return s[:24] or "workspace"


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


class WorkspaceStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ paths
    def dir(self, wid: str) -> str:
        if not wid or "/" in wid or "\\" in wid or wid.startswith("."):
            raise KeyError(wid)
        return os.path.join(self.root, wid)

    def paths(self, wid: str) -> Dict[str, str]:
        d = self.dir(wid)
        return {"root": d, "input": os.path.join(d, "input"), "project": os.path.join(d, "project"),
                "output": os.path.join(d, "output"), "logs": os.path.join(d, "logs")}

    # ------------------------------------------------------------ workspaces
    def list(self) -> List[dict]:
        out = []
        with self._lock:
            for name in sorted(os.listdir(self.root)):
                if name.startswith(".") or not os.path.isdir(os.path.join(self.root, name)):
                    continue
                ws = _read_json(os.path.join(self.root, name, "workspace.json"), None)
                if ws:
                    out.append(self.summary(ws))
        out.sort(key=lambda w: w.get("created", 0))
        return out

    def summary(self, ws: dict) -> dict:
        reqs = self.requests(ws["id"])
        counts: Dict[str, int] = {}
        for r in reqs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        working = next((r for r in reqs if r["status"] == "working"), None)
        return {**ws, "requests": len(reqs), "counts": counts,
                "busy": bool(working), "active_request": working["id"] if working else None,
                "selected": ws["id"] == self.selected()}

    def get(self, wid: str) -> Optional[dict]:
        try:
            return _read_json(os.path.join(self.dir(wid), "workspace.json"), None)
        except KeyError:
            return None

    def find(self, name_or_id: str) -> Optional[dict]:
        """By id, then exact name, then a name containing the words (case-insensitive)."""
        key = (name_or_id or "").strip()
        if not key:
            return None
        ws = self.get(key) if re.fullmatch(r"[a-z0-9-]{3,40}", key) else None
        if ws:
            return ws
        items = self.list()
        low = key.lower()
        for w in items:
            if w["name"].lower() == low:
                return w
        for w in items:
            if low in w["name"].lower() or w["name"].lower() in low:
                return w
        words = set(re.findall(r"\w+", low))
        best, score = None, 0
        for w in items:
            s = len(words & set(re.findall(r"\w+", w["name"].lower())))
            if s > score:
                best, score = w, s
        return best

    def create(self, name: str = "", kind: str = "auto", select: bool = True) -> dict:
        with self._lock:
            name = (name or "").strip()[:60] or self._next_name()
            wid = f"{slug(name)}-{uuid.uuid4().hex[:6]}"
            p = self.paths(wid)
            for key in ("input", "project", "output", "logs"):
                os.makedirs(p[key], exist_ok=True)
            ws = {"id": wid, "name": name, "kind": kind, "created": _now(), "updated": _now(),
                  "status": "idle", "product": None, "title": None}
            _write_json(os.path.join(p["root"], "workspace.json"), ws)
            _write_json(os.path.join(p["root"], "requests.json"), [])
            if select:
                self.select(wid)
            return ws

    def _next_name(self) -> str:
        names = {w["name"] for w in self.list()}
        i = 1
        while f"Workspace {i}" in names:
            i += 1
        return f"Workspace {i}"

    def update(self, wid: str, **fields) -> dict:
        with self._lock:
            ws = self.get(wid)
            if ws is None:
                raise KeyError(wid)
            ws.update(fields)
            ws["updated"] = _now()
            _write_json(os.path.join(self.dir(wid), "workspace.json"), ws)
            return ws

    def rename(self, wid: str, name: str) -> dict:
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("empty name")
        return self.update(wid, name=name)

    def delete(self, wid: str) -> str:
        """Move the workspace to data/workspaces/.trash (kept until you empty it)."""
        with self._lock:
            src = self.dir(wid)
            if not os.path.isdir(src):
                raise KeyError(wid)
            trash = os.path.join(self.root, ".trash")
            os.makedirs(trash, exist_ok=True)
            dst = os.path.join(trash, f"{wid}-{int(time.time())}")
            shutil.move(src, dst)
            if self.selected() == wid:
                rest = self.list()
                self.select(rest[-1]["id"] if rest else None)
            return dst

    def selected(self) -> Optional[str]:
        wid = _read_json(os.path.join(self.root, "selected.json"), {}).get("id")
        if wid and os.path.isdir(os.path.join(self.root, wid)):
            return wid
        return None

    def select(self, wid: Optional[str]) -> None:
        if wid is not None and self.get(wid) is None:
            raise KeyError(wid)
        _write_json(os.path.join(self.root, "selected.json"), {"id": wid})

    # ------------------------------------------------------------ requests
    def requests(self, wid: str) -> List[dict]:
        try:
            return _read_json(os.path.join(self.dir(wid), "requests.json"), [])
        except KeyError:
            return []

    def _save_requests(self, wid: str, reqs: List[dict]) -> None:
        _write_json(os.path.join(self.dir(wid), "requests.json"), reqs)

    def get_request(self, wid: str, rid: str) -> Optional[dict]:
        return next((r for r in self.requests(wid) if r["id"] == rid), None)

    def add_request(self, wid: str, text: str, attachments: Optional[List[str]] = None,
                    kind: str = "build", meta: Optional[dict] = None) -> dict:
        """Stack a request on the workspace; copies the attached files into input/."""
        text = (text or "").strip()
        if not text and not attachments:
            raise ValueError("empty request")
        with self._lock:
            if self.get(wid) is None:
                raise KeyError(wid)
            rid = "r" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
            stored = [self._copy_in(wid, a) for a in (attachments or [])]
            req = {"id": rid, "text": text, "kind": kind, "attachments": stored, "status": "queued",
                   "created": _now(), "started": None, "finished": None, "progress": {},
                   "checklist": [], "product": None, "summary": "", "error": None,
                   "base": None, "commit": None, "iterations": 0, "meta": meta or {}}
            reqs = self.requests(wid)
            reqs.append(req)
            self._save_requests(wid, reqs)
            self.update(wid, status="queued" if self.get(wid).get("status") != "working" else "working")
            return req

    def _copy_in(self, wid: str, src: str) -> dict:
        src = os.path.abspath(os.path.expanduser(str(src)))
        if not os.path.exists(src):
            raise FileNotFoundError(src)
        size = _dir_size(src) if os.path.isdir(src) else os.path.getsize(src)
        if size > MAX_ATTACH_BYTES:
            raise ValueError(f"{os.path.basename(src)} is larger than 8 GB")
        dest_dir = self.paths(wid)["input"]
        base = os.path.basename(src.rstrip("\\/")) or "file"
        dest = os.path.join(dest_dir, base)
        stem, ext = os.path.splitext(base)
        i = 2
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stem} ({i}){ext}")
            i += 1
        if os.path.isdir(src):
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
        return {"name": os.path.basename(dest), "path": os.path.relpath(dest, self.dir(wid)),
                "original": src, "bytes": size, "dir": os.path.isdir(src)}

    def update_request(self, wid: str, rid: str, **fields) -> dict:
        with self._lock:
            reqs = self.requests(wid)
            for r in reqs:
                if r["id"] == rid:
                    r.update(fields)
                    self._save_requests(wid, reqs)
                    return r
        raise KeyError(rid)

    def all_requests(self) -> List[Tuple[str, dict]]:
        out = []
        for ws in self.list():
            for r in self.requests(ws["id"]):
                out.append((ws["id"], r))
        return out

    def next_queued(self) -> Optional[Tuple[str, dict]]:
        """The oldest queued request of any workspace (requests of one workspace run in order)."""
        best = None
        for ws in self.list():
            reqs = self.requests(ws["id"])
            # a workspace's requests stack: never skip ahead of an earlier paused one
            first_active = next((r for r in reqs if r["status"] in ACTIVE), None)
            if first_active is None or first_active["status"] != "queued":
                continue
            if best is None or first_active["created"] < best[1]["created"]:
                best = (ws["id"], first_active)
        return best

    def recover(self) -> int:
        """After a restart: requests that were being worked on are queued again."""
        n = 0
        for ws in self.list():
            reqs = self.requests(ws["id"])
            changed = False
            for r in reqs:
                if r["status"] == "working":
                    r["status"] = "queued"
                    r.setdefault("meta", {})["resumed"] = r.get("meta", {}).get("resumed", 0) + 1
                    changed = True
                    n += 1
            if changed:
                self._save_requests(ws["id"], reqs)
                self.update(ws["id"], status="queued")
        return n

    # ------------------------------------------------------------ logs
    def log(self, wid: str, rid: str, kind: str, text: str, **extra) -> dict:
        rec = {"ts": _now(), "kind": kind, "text": text, **extra}
        path = os.path.join(self.paths(wid)["logs"], f"{rid}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def log_tail(self, wid: str, rid: str, n: int = 60) -> List[dict]:
        path = os.path.join(self.paths(wid)["logs"], f"{rid}.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def request_dir(self, wid: str, rid: str) -> str:
        d = os.path.join(self.paths(wid)["logs"], rid)
        os.makedirs(d, exist_ok=True)
        return d

    # ------------------------------------------------------------ detail for the HUD
    def detail(self, wid: str, log_lines: int = 40) -> Optional[dict]:
        ws = self.get(wid)
        if ws is None:
            return None
        reqs = self.requests(wid)
        active = next((r for r in reqs if r["status"] == "working"), None) or (reqs[-1] if reqs else None)
        p = self.paths(wid)
        files = []
        for base, dirs, fnames in os.walk(p["project"]):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in fnames:
                full = os.path.join(base, f)
                files.append(os.path.relpath(full, p["project"]).replace("\\", "/"))
                if len(files) >= 200:
                    break
            if len(files) >= 200:
                break
        return {**self.summary(ws), "request_list": reqs, "paths": p, "files": sorted(files),
                "log": self.log_tail(wid, active["id"], log_lines) if active else []}


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build",
             ".next", ".cache", "target", "obj", ".jarvis"}
