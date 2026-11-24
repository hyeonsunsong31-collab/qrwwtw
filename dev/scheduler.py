"""
dev/scheduler.py -- one developer job at a time, in order, across all workspaces.

The graphics card (or processor) runs one coding model at a time, so requests queue: the oldest
queued request of any workspace goes next, and a workspace's stacked requests always run in the
order you gave them. Pause holds a job where it is (and keeps its place in the queue); Resume
continues it; Stop ends it. After a restart of JARVIS, the request that was being worked on is
picked up again from the project files as they are.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Callable, Dict, Optional, Tuple

from .agent import Job, Stopped
from .workspaces import WorkspaceStore


class Scheduler:
    def __init__(self, store: WorkspaceStore, run_job: Callable[[str, dict, Job], dict],
                 on_change: Optional[Callable[[], None]] = None, log: Optional[Callable[[str], None]] = None):
        self.store = store
        self.run_job = run_job
        self.on_change = on_change or (lambda: None)
        self.log = log or (lambda m: None)
        self._wake = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.current: Optional[Tuple[str, str, Job]] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ life cycle
    def start(self) -> None:
        n = self.store.recover()
        if n:
            self.log(f"{n} request(s) were interrupted by a restart; continuing them")
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="jarvis-dev-scheduler", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._running = False
        cur = self.current
        if cur is not None:
            cur[2].stop()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------ requests
    def submit(self, wid: str, text: str, attachments=None, kind: str = "build", meta: Optional[dict] = None) -> dict:
        req = self.store.add_request(wid, text, attachments, kind=kind, meta=meta)
        self._wake.set()
        self.on_change()
        return req

    def _find(self, wid: str, rid: Optional[str], statuses) -> Optional[dict]:
        reqs = self.store.requests(wid)
        if rid:
            return next((r for r in reqs if r["id"] == rid), None)
        return next((r for r in reqs if r["status"] in statuses), None)

    def pause(self, wid: str, rid: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            r = self._find(wid, rid, ("working", "queued"))
            if r is None or r["status"] not in ("working", "queued"):
                return None
            cur = self.current
            if cur and cur[0] == wid and cur[1] == r["id"]:
                cur[2].pause()
            r = self.store.update_request(wid, r["id"], status="paused")
            self.store.update(wid, status="paused")
        self.on_change()
        return r

    def resume(self, wid: str, rid: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            r = self._find(wid, rid, ("paused",))
            if r is None or r["status"] != "paused":
                return None
            cur = self.current
            if cur and cur[0] == wid and cur[1] == r["id"]:
                r = self.store.update_request(wid, r["id"], status="working")
                self.store.update(wid, status="working")
                cur[2].resume()
            else:
                r = self.store.update_request(wid, r["id"], status="queued")
                self.store.update(wid, status="queued")
        self._wake.set()
        self.on_change()
        return r

    def stop_request(self, wid: str, rid: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            r = self._find(wid, rid, ("working", "paused", "queued"))
            if r is None or r["status"] not in ("working", "paused", "queued"):
                return None
            cur = self.current
            if cur and cur[0] == wid and cur[1] == r["id"]:
                cur[2].stop()                    # the job records "stopped" itself
            else:
                r = self.store.update_request(wid, r["id"], status="stopped", finished=time.time(),
                                              summary="stopped before it started")
        self._wake.set()
        self.on_change()
        return r

    def stop_workspace(self, wid: str) -> None:
        for r in self.store.requests(wid):
            if r["status"] in ("working", "paused", "queued"):
                self.stop_request(wid, r["id"])

    def busy(self) -> Optional[dict]:
        cur = self.current
        if cur is None:
            return None
        return {"workspace": cur[0], "request": cur[1], "paused": cur[2].paused}

    # ------------------------------------------------------------ the loop
    def _loop(self) -> None:
        while self._running:
            try:
                self._once()
            except Exception as e:             # the queue must never die: log, wait, carry on
                self.log(f"scheduler problem: {e!r}")
                self._wake.wait(5.0)
                self._wake.clear()

    def _once(self) -> None:
        if True:
            item = self.store.next_queued()
            if item is None:
                self._wake.wait(5.0)
                self._wake.clear()
                return
            wid, req = item
            job = Job()
            with self._lock:
                self.current = (wid, req["id"], job)
                self.store.update_request(wid, req["id"], status="working", started=req.get("started") or time.time())
                self.store.update(wid, status="working")
            self.on_change()
            try:
                result = self.run_job(wid, req, job) or {}
            except Stopped:
                result = {"status": "stopped", "summary": "stopped by you"}
            except Exception as e:
                self.log(f"job {req['id']} crashed: {e!r}\n{traceback.format_exc()[-1500:]}")
                result = {"status": "failed", "summary": f"the job crashed: {type(e).__name__}: {e}"}
            with self._lock:
                self.current = None
                cur = self.store.get_request(wid, req["id"]) or {}
                status = result.get("status") or cur.get("status") or "failed"
                if status == "refused":
                    status = "failed"
                if cur.get("status") in ("working", "paused"):
                    self.store.update_request(wid, req["id"], status=status if status in ("done", "failed", "stopped") else "failed",
                                              finished=time.time(), summary=result.get("summary") or cur.get("summary", ""))
                remaining = [r for r in self.store.requests(wid) if r["status"] in ("queued", "paused")]
                self.store.update(wid, status="queued" if remaining else "idle")
            self.on_change()
