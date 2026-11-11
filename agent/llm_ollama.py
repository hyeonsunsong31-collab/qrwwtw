"""
agent/llm_ollama.py
===================
Local LLM backend via Ollama (the offline brain for natural conversation).

* OllamaLLM     -- chat() and token-streaming chat_stream() against 127.0.0.1:11434, and
                   resolve_model() which picks a model that is actually installed.
* ResilientLLM  -- what the server uses: Ollama when reachable, an honest offline fallback
                   otherwise, re-checking every few seconds so JARVIS upgrades itself as soon
                   as Ollama finishes starting (at logon both start at roughly the same time).
* MockLLM       -- deterministic stand-in for tests and the offline fallback.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, Iterator, List, Optional

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None


def pick_model(candidates: Dict[str, str]) -> str:
    """Fallback choice by RAM when nothing is installed yet (the installer normally pins
    the exact model it pulled in config/jarvis.config.json)."""
    try:
        import psutil  # type: ignore
        ram = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        ram = 8.0
    if ram >= 32:
        return candidates.get("high") or candidates.get("medium") or candidates.get("low")
    if ram >= 16:
        return candidates.get("medium") or candidates.get("low")
    return candidates.get("low") or candidates.get("medium") or "qwen2.5:3b"


def _norm(name: str) -> str:
    return name if ":" in name else name + ":latest"


def _messages(system: str, user: str, history: Optional[List[dict]]) -> List[dict]:
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-12:]:
        msgs.append({"role": h["role"], "content": h["text"]})
    msgs.append({"role": "user", "content": user})
    return msgs


class OllamaLLM:
    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "auto",
                 candidates: Optional[Dict[str, str]] = None, timeout: float = 180.0):
        self.host = host.rstrip("/")
        self.candidates = candidates or {}
        self.configured = model
        self.model = pick_model(self.candidates) if model == "auto" else model
        self.timeout = timeout

    def health(self) -> bool:
        if httpx is None:
            return False
        try:
            return httpx.get(f"{self.host}/api/tags", timeout=2.5).status_code == 200
        except Exception:
            return False

    def local_models(self) -> List[str]:
        if httpx is None:
            return []
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5.0)
            return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
        except Exception:
            return []

    def resolve_model(self) -> str:
        """With model='auto', use the strongest installed candidate (or any installed model)."""
        if self.configured != "auto":
            return self.model
        installed = {_norm(n) for n in self.local_models()}
        for key in ("high", "medium", "low"):
            cand = self.candidates.get(key)
            if cand and _norm(cand) in installed:
                self.model = cand
                return cand
        if installed:
            self.model = sorted(installed)[0]
        return self.model

    def _body(self, system, user, history, params, stream: bool) -> dict:
        opts = {"temperature": 0.7, "num_predict": 400}
        opts.update(params or {})
        return {"model": self.model, "messages": _messages(system, user, history), "stream": stream,
                "options": {"temperature": float(opts["temperature"]),
                            "num_predict": int(opts["num_predict"])}}

    def chat(self, system: str, user: str, history: Optional[List[dict]] = None,
             params: Optional[dict] = None) -> str:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        r = httpx.post(f"{self.host}/api/chat", json=self._body(system, user, history, params, False),
                       timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()

    def chat_stream(self, system: str, user: str, history: Optional[List[dict]] = None,
                    params: Optional[dict] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        """Yield the reply as it is generated (Ollama streams one JSON object per line)."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body = self._body(system, user, history, params, True)
        with httpx.stream("POST", f"{self.host}/api/chat", json=body, timeout=self.timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if stop_event is not None and stop_event.is_set():
                    break
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                piece = obj.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if obj.get("done"):
                    break


class MockLLM:
    """Deterministic offline stand-in (tests, and the fallback while Ollama is unavailable)."""

    EN = ("Very good, sir. My language engine isn't reachable just now, so this is a short "
          "fallback reply. Direct commands such as opening apps or playing music still work.")
    KO = ("알겠습니다, sir. 지금은 언어 엔진에 연결되지 않아 간단한 대체 응답을 드립니다. "
          "앱 열기나 음악 재생 같은 직접 명령은 그대로 동작합니다.")

    def __init__(self, model: str = "offline-fallback"):
        self.model = model

    def health(self) -> bool:
        return True

    def _reply(self, user: str) -> str:
        return self.KO if any("가" <= c <= "힣" for c in user) else self.EN

    def chat(self, system: str, user: str, history=None, params=None) -> str:
        return self._reply(user)

    def chat_stream(self, system: str, user: str, history=None, params=None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        for word in self._reply(user).split(" "):
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.005)
            yield word + " "


class ResilientLLM:
    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "auto",
                 candidates: Optional[Dict[str, str]] = None, recheck_seconds: float = 8.0):
        self.ollama = OllamaLLM(host, model, candidates)
        self.mock = MockLLM()
        self.recheck = recheck_seconds
        self._online = False
        self._checked = 0.0
        self._resolved = False
        self._lock = threading.Lock()

    def online(self) -> bool:
        with self._lock:
            now = time.time()
            if now - self._checked >= self.recheck:
                self._checked = now
                self._online = self.ollama.health()
                if self._online and not self._resolved:
                    self.ollama.resolve_model()
                    self._resolved = True
            return self._online

    def _mark_offline(self):
        with self._lock:
            self._online = False
            self._checked = 0.0

    def model_name(self) -> str:
        return self.ollama.model if self._online else "offline-fallback"

    def chat(self, system, user, history=None, params=None) -> str:
        if self.online():
            try:
                return self.ollama.chat(system, user, history, params)
            except Exception:
                self._mark_offline()
        return self.mock.chat(system, user, history, params)

    def chat_stream(self, system, user, history=None, params=None, stop_event=None):
        if self.online():
            yielded = False
            try:
                for piece in self.ollama.chat_stream(system, user, history, params, stop_event):
                    yielded = True
                    yield piece
                return
            except Exception:
                self._mark_offline()
                if yielded:          # never splice a fallback onto a half-finished real reply
                    return
        yield from self.mock.chat_stream(system, user, history, params, stop_event)
