"""
recorder/recorder.py
====================
Records EVERYTHING (requirement 2):

  data/
  ├─ runs/                 every task run (runs task): steps, status, timings
  ├─ commands/             every command input (voice or text), daily JSONL
  ├─ results/
  │   └─ files/            every generated file, all types (+ index.jsonl)
  ├─ text/
  │   ├─ conversation/     (1) conversation transcript, daily JSONL
  │   ├─ explanations/
  │   │   ├─ errors/       (2-1) explanations of errors
  │   │   ├─ answers/      (2-2) explanations answering the user's questions
  │   │   ├─ process/      (2-3) how a task succeeded or failed
  │   │   └─ eta/          (2-4) time-to-complete estimates
  │   ├─ options/          (3) options offered for further adjustment/instructions
  │   └─ suggestions/      (4) recommendations for (3)
  ├─ checkpoints/          latest workflow checkpoint (+ history) — resumable
  ├─ audit/                permission/audit log for every PC action
  └─ brain/                persisted neural-control state

Thread-safe (voice, server and skills log concurrently). Plain JSON/JSONL so it's
human-readable and greppable; the overlay's History panel reads from here.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

EXPLANATION_KINDS = {"error": "errors", "answer": "answers", "process": "process", "eta": "eta"}

DEFAULT_ETA_SECONDS = {
    "chat": 3, "open_app": 4, "play_music": 8, "open_space3d": 3, "file_search": 10,
    "file_write": 4, "browser": 6, "system_info": 2, "settings": 5, "devices": 4,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Recorder:
    def __init__(self, root: str = "data"):
        self.root = os.path.abspath(root)
        self._lock = threading.RLock()
        self.dirs = {
            "runs": "runs",
            "commands": "commands",
            "files": os.path.join("results", "files"),
            "conversation": os.path.join("text", "conversation"),
            "options": os.path.join("text", "options"),
            "suggestions": os.path.join("text", "suggestions"),
            "checkpoints": "checkpoints",
            "audit": "audit",
            "brain": "brain",
        }
        for kind in EXPLANATION_KINDS.values():
            self.dirs[f"expl_{kind}"] = os.path.join("text", "explanations", kind)
        for rel in self.dirs.values():
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
        self._runs: Dict[str, dict] = {}

    # ------------------------------------------------------------ helpers #
    def path(self, key: str, *parts: str) -> str:
        return os.path.join(self.root, self.dirs[key], *parts)

    def _append_jsonl(self, key: str, record: dict, name: Optional[str] = None) -> None:
        fn = self.path(key, f"{name or _today()}.jsonl")
        with self._lock, open(fn, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_json(self, fn: str, obj: Any) -> None:
        tmp = fn + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, fn)  # atomic

    # ----------------------------------------------------------- commands #
    def log_command(self, text: str, source: str = "text", lang: str = "en",
                    meta: Optional[dict] = None) -> str:
        cid = "cmd_" + uuid.uuid4().hex[:12]
        self._append_jsonl("commands", {"id": cid, "ts": _now_iso(), "source": source,
                                        "lang": lang, "text": text, "meta": meta or {}})
        return cid

    # --------------------------------------------------------------- runs #
    def start_run(self, command_id: str, intent: str, description: str = "",
                  eta_seconds: Optional[float] = None) -> str:
        rid = "run_" + uuid.uuid4().hex[:12]
        eta = eta_seconds if eta_seconds is not None else self.estimate_eta(intent)
        run = {"id": rid, "command_id": command_id, "intent": intent,
               "description": description, "status": "running",
               "started_at": _now_iso(), "t0": time.time(), "eta_seconds": eta,
               "steps": []}
        with self._lock:
            self._runs[rid] = run
        self._write_json(self.path("runs", f"{rid}.json"), run)
        self.log_explanation("eta", f"Estimated time for '{intent}': about {eta:.0f} second(s).",
                             run_id=rid)
        return rid

    def run_step(self, run_id: str, description: str, status: str = "ok",
                 data: Optional[dict] = None) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run["steps"].append({"ts": _now_iso(), "status": status,
                                 "description": description, "data": data or {}})
            self._write_json(self.path("runs", f"{run_id}.json"), run)

    def end_run(self, run_id: str, status: str, summary: str = "",
                error: Optional[str] = None) -> dict:
        with self._lock:
            run = self._runs.pop(run_id, None)
            if not run:
                return {}
            run["status"] = status
            run["ended_at"] = _now_iso()
            run["duration_seconds"] = round(time.time() - run.pop("t0"), 3)
            run["summary"] = summary
            if error:
                run["error"] = error
            self._write_json(self.path("runs", f"{run_id}.json"), run)
        # (2-3) process explanation, (2-1) error explanation
        verdict = {"success": "succeeded", "cancelled": "was interrupted"}.get(status, "failed")
        steps = "; ".join(s["description"] for s in run["steps"]) or "no intermediate steps"
        self.log_explanation("process",
                             f"Task '{run['intent']}' {verdict} in {run['duration_seconds']}s. "
                             f"Steps: {steps}. {summary}".strip(), run_id=run_id)
        if error:
            self.log_explanation("error", error, run_id=run_id)
        self._record_duration(run["intent"], run["duration_seconds"])
        return run

    # ------------------------------------------------------------ ETA (2-4) #
    def _durations_file(self) -> str:
        return self.path("runs", "_durations.json")

    def _record_duration(self, intent: str, seconds: float) -> None:
        fn = self._durations_file()
        with self._lock:
            data = {}
            if os.path.exists(fn):
                try:
                    with open(fn, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    data = {}
            hist = data.setdefault(intent, [])
            hist.append(seconds)
            data[intent] = hist[-50:]
            self._write_json(fn, data)

    def estimate_eta(self, intent: str) -> float:
        fn = self._durations_file()
        try:
            with open(fn, encoding="utf-8") as fh:
                hist = json.load(fh).get(intent, [])
            if len(hist) >= 3:
                return round(statistics.median(hist) * 1.15, 1)  # median + margin
        except (OSError, ValueError):
            pass
        return float(DEFAULT_ETA_SECONDS.get(intent, 5))

    # --------------------------------------------------------------- text #
    def log_conversation(self, role: str, text: str, lang: str = "en",
                         run_id: Optional[str] = None) -> None:
        self._append_jsonl("conversation", {"ts": _now_iso(), "role": role, "lang": lang,
                                            "text": text, "run_id": run_id})

    def log_explanation(self, kind: str, text: str, run_id: Optional[str] = None) -> None:
        folder = EXPLANATION_KINDS.get(kind)
        if not folder:
            raise ValueError(f"unknown explanation kind: {kind}")
        self._append_jsonl(f"expl_{folder}", {"ts": _now_iso(), "kind": kind,
                                              "text": text, "run_id": run_id})

    def log_options(self, options: List[str], run_id: Optional[str] = None) -> None:
        self._append_jsonl("options", {"ts": _now_iso(), "options": options, "run_id": run_id})

    def log_suggestions(self, suggestions: List[str], run_id: Optional[str] = None) -> None:
        self._append_jsonl("suggestions", {"ts": _now_iso(), "suggestions": suggestions,
                                           "run_id": run_id})

    # -------------------------------------------------------------- files #
    def register_file(self, src_path: str, run_id: Optional[str] = None,
                      copy: bool = True, note: str = "") -> str:
        """Store/register a generated file of ANY type and index it."""
        src_path = os.path.abspath(src_path)
        stored = src_path
        if copy and os.path.isfile(src_path):
            day_dir = self.path("files", _today())
            os.makedirs(day_dir, exist_ok=True)
            base = os.path.basename(src_path)
            stored = os.path.join(day_dir, f"{datetime.now().strftime('%H%M%S')}_{base}")
            shutil.copy2(src_path, stored)
        size = os.path.getsize(stored) if os.path.exists(stored) else 0
        self._append_jsonl("files", {"ts": _now_iso(), "original": src_path, "stored": stored,
                                     "ext": os.path.splitext(stored)[1].lower(), "bytes": size,
                                     "run_id": run_id, "note": note}, name="index")
        return stored

    # -------------------------------------------------------- checkpoints #
    def checkpoint(self, workflow: dict) -> str:
        """Save the latest workflow checkpoint (and keep a timestamped history copy)."""
        snap = {"ts": _now_iso(), "workflow": workflow}
        latest = self.path("checkpoints", "latest.json")
        self._write_json(latest, snap)
        hist = self.path("checkpoints", f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
        self._write_json(hist, snap)
        self._prune_checkpoints(keep=200)
        return latest

    def load_latest_checkpoint(self) -> Optional[dict]:
        fn = self.path("checkpoints", "latest.json")
        try:
            with open(fn, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _prune_checkpoints(self, keep: int) -> None:
        d = self.path("checkpoints")
        files = sorted(f for f in os.listdir(d) if f[0].isdigit() and f.endswith(".json"))
        for f in files[:-keep]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    # -------------------------------------------------------------- audit #
    def audit(self, action: str, category: str, decision: str, detail: Any = None) -> None:
        line = {"ts": _now_iso(), "action": action, "category": category,
                "decision": decision, "detail": detail}
        fn = self.path("audit", "audit.log")
        with self._lock, open(fn, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # -------------------------------------------------------------- query #
    def recent(self, key: str, n: int = 50) -> List[dict]:
        """Most recent records from a JSONL category (for the overlay History panel)."""
        d = self.path(key)
        files = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))
        out: List[dict] = []
        for f in reversed(files):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                lines = fh.readlines()
            for line in reversed(lines):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= n:
                    return out
        return out
