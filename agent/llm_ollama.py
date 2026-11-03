"""
agent/llm_ollama.py
===================
Local LLM backend via Ollama (requirement: offline brain for natural conversation).

Ollama is a native app (https://ollama.com) exposing an HTTP API on 127.0.0.1:11434.
The installer downloads Ollama and pulls a model sized to the machine. This module:
  * picks a model by detected RAM/VRAM (unless one is configured),
  * exposes chat()/health(), and
  * degrades gracefully when Ollama is not running, so the rest of JARVIS still works
    (the user just gets a polite "my language brain is offline" style reply).
A MockLLM is provided for tests / CI on machines without Ollama.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None


def pick_model(candidates: Dict[str, str]) -> str:
    """Choose high/medium/low model by available RAM (GB)."""
    try:
        import psutil  # type: ignore
        ram = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        ram = 8.0
    if ram >= 32:
        return candidates.get("high") or candidates.get("medium") or candidates.get("low")
    if ram >= 16:
        return candidates.get("medium") or candidates.get("low")
    return candidates.get("low") or candidates.get("medium") or "qwen2.5:3b-instruct"


class OllamaLLM:
    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "auto",
                 candidates: Optional[Dict[str, str]] = None, timeout: float = 120.0):
        self.host = host.rstrip("/")
        self.candidates = candidates or {}
        self.model = pick_model(self.candidates) if model == "auto" else model
        self.timeout = timeout

    def health(self) -> bool:
        if httpx is None:
            return False
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False

    def ensure_model(self) -> bool:
        """Return True if the chosen model is present locally."""
        if httpx is None:
            return False
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5.0)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(self.model.split(":")[0] in n for n in names)
        except Exception:
            return False

    def chat(self, system: str, user: str, history: Optional[List[dict]] = None,
             params: Optional[dict] = None) -> str:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        messages = [{"role": "system", "content": system}]
        for h in (history or [])[-12:]:
            messages.append({"role": h["role"], "content": h["text"]})
        messages.append({"role": "user", "content": user})
        opts = {"temperature": 0.7, "num_predict": 400}
        opts.update(params or {})
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"temperature": opts["temperature"],
                            "num_predict": int(opts["num_predict"])}}
        r = httpx.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()


class MockLLM:
    """Deterministic offline stand-in used by tests and when Ollama is unavailable."""

    def __init__(self, model: str = "mock"):
        self.model = model

    def health(self) -> bool:
        return True

    def ensure_model(self) -> bool:
        return True

    def chat(self, system: str, user: str, history: Optional[List[dict]] = None,
             params: Optional[dict] = None) -> str:
        ko = any("가" <= c <= "힣" for c in user)
        if ko:
            return "알겠습니다, sir. (오프라인 목 응답입니다.)"
        return "Very good, sir. (This is an offline mock reply; install Ollama for full conversation.)"
