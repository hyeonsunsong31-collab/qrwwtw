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

SPEED (v1.4) -- why replies used to take long, and what keeps them fast now:
  * Ollama unloads a model after 5 idle minutes by default, so the next reply had to load it
    from disk first (5-30 s). Every request now carries `keep_alive` (default: keep it
    loaded), and the server warms the model up at start and reloads it if Ollama restarts.
  * Ollama reuses its cache of a prompt prefix it has already read. Requests now use a
    constant `num_ctx` (a different value reloads the model) and the orchestrator keeps the
    system prompt and the history identical from turn to turn, so only the new message has
    to be read.
  * warm_up() reads the system prompt once ahead of time and measures generation speed,
    which /status reports (llm_state, llm_tokens_per_s, llm_gpu).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Dict, Iterator, List, Optional, Union

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None


def gpu_memory_mb() -> int:
    """Total memory of the first NVIDIA GPU in MB (0 if there is none or nvidia-smi is absent)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0
    try:
        out = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return 0


def pick_model(candidates: Dict[str, str]) -> str:
    """Fallback choice when the config says "auto" and nothing is installed yet (the installer
    normally pins the exact model it pulled). Sized for SPEED: without a GPU a 7B model
    produces only a few words a second, so CPU-only PCs get the small model."""
    vram = gpu_memory_mb()
    if vram >= 12000:
        return candidates.get("high") or candidates.get("medium") or candidates.get("low")
    if vram >= 6000:
        return candidates.get("medium") or candidates.get("low")
    return candidates.get("low") or candidates.get("medium") or "qwen2.5:3b"


def keep_alive_value(setting: Union[str, int, None]) -> Union[str, int]:
    """Config/Settings value -> what Ollama expects: -1 keeps the model loaded indefinitely."""
    s = str(setting if setting is not None else "always").strip().lower()
    if s in ("always", "forever", "-1", ""):
        return -1
    return s                       # Ollama durations: "5m", "15m", "1h", ...


def _norm(name: str) -> str:
    return name if ":" in name else name + ":latest"


def _messages(system: str, user: str, history: Optional[List[dict]]) -> List[dict]:
    """System + history + the new message. The history is NOT windowed here: a window that
    slides by one turn every request changes the prompt prefix every time, so Ollama would
    re-read the whole conversation. The orchestrator trims it in larger steps instead."""
    msgs = [{"role": "system", "content": system}]
    for h in history or []:
        msgs.append({"role": h["role"], "content": h["text"]})
    msgs.append({"role": "user", "content": user})
    return msgs


class OllamaLLM:
    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "auto",
                 candidates: Optional[Dict[str, str]] = None, timeout: float = 180.0,
                 keep_alive: Union[str, int, None] = "always", num_ctx: int = 4096):
        self.host = host.rstrip("/")
        self.candidates = candidates or {}
        self.configured = model
        self.model = pick_model(self.candidates) if model == "auto" else model
        self.timeout = timeout
        self.keep_alive = keep_alive_value(keep_alive)
        # constant on every request: a request with a different num_ctx makes Ollama reload
        self.num_ctx = max(1024, min(32768, int(num_ctx or 4096)))

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
        for key in ("high", "medium", "low", "tiny"):
            cand = self.candidates.get(key)
            if cand and _norm(cand) in installed:
                self.model = cand
                return cand
        if installed:
            self.model = sorted(installed)[0]
        return self.model

    def _body(self, system, user, history, params, stream: bool) -> dict:
        opts = {"temperature": 0.7, "num_predict": 300}
        opts.update(params or {})
        return {"model": self.model, "messages": _messages(system, user, history), "stream": stream,
                "keep_alive": self.keep_alive,
                "options": {"temperature": float(opts["temperature"]),
                            "num_predict": int(opts["num_predict"]),
                            "num_ctx": self.num_ctx}}

    # ---------------------------------------------------------------- warm-up #
    def loaded(self) -> Optional[dict]:
        """Ollama's entry for this model if it is in memory right now (GET /api/ps)."""
        if httpx is None:
            return None
        try:
            r = httpx.get(f"{self.host}/api/ps", timeout=4.0)
            for m in r.json().get("models", []) or []:
                if _norm(m.get("name") or m.get("model") or "") == _norm(self.model):
                    return m
        except Exception:
            pass
        return None

    def warm_up(self, system: str) -> dict:
        """Load the model, read the system prompt once (so the first real reply finds it
        cached) and measure how fast this PC generates. Raises if Ollama refuses."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        t0 = time.time()
        r = httpx.post(f"{self.host}/api/generate",
                       json={"model": self.model, "prompt": "", "keep_alive": self.keep_alive,
                             "options": {"num_ctx": self.num_ctx}}, timeout=900)
        if r.status_code >= 400:
            raise RuntimeError(f"Ollama could not load {self.model}: {r.text[:200]}")
        load_s = time.time() - t0
        body = self._body(system, "Reply with the single word: ready", [],
                          {"temperature": 0.0, "num_predict": 8}, False)
        r = httpx.post(f"{self.host}/api/chat", json=body, timeout=900)
        r.raise_for_status()
        j = r.json()
        ev_n, ev_ns = int(j.get("eval_count") or 0), int(j.get("eval_duration") or 0)
        pr_n, pr_ns = int(j.get("prompt_eval_count") or 0), int(j.get("prompt_eval_duration") or 0)
        ps = self.loaded() or {}
        size, vram = int(ps.get("size") or 0), int(ps.get("size_vram") or 0)
        return {"model": self.model, "load_seconds": round(load_s, 2),
                "tokens_per_s": round(ev_n / (ev_ns / 1e9), 1) if ev_n and ev_ns else None,
                "prompt_tokens_per_s": round(pr_n / (pr_ns / 1e9), 1) if pr_n and pr_ns else None,
                "system_tokens": pr_n or None,
                "gpu": round(vram / size, 2) if size else None,
                "warmed_at": time.time()}

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
                 candidates: Optional[Dict[str, str]] = None, recheck_seconds: float = 8.0,
                 keep_alive: Union[str, int, None] = "always", num_ctx: int = 4096):
        self.ollama = OllamaLLM(host, model, candidates, keep_alive=keep_alive, num_ctx=num_ctx)
        self.mock = MockLLM()
        self.recheck = recheck_seconds
        self._online = False
        self._checked = 0.0
        self._resolved = False
        self._lock = threading.Lock()
        self.state = "offline"         # offline | loading | ready | error
        self.stats: dict = {}
        self.error = ""
        self._warm_failed_at = 0.0

    def ensure_warm(self, system: str) -> bool:
        """Called every few seconds by the server: (re)load and warm the model when needed.
        Returns True when something changed (so the HUD can be told)."""
        if not self.online():
            changed = self.state != "offline"
            self.state = "offline"
            return changed
        keep_forever = self.ollama.keep_alive == -1
        if self.state == "ready":
            if not keep_forever or self.ollama.loaded() is not None:
                return False
            # Ollama restarted (or another app evicted the model): load it again
        if self.state == "error" and time.time() - self._warm_failed_at < 300:
            return False
        self.state = "loading"
        try:
            self.stats = self.ollama.warm_up(system)
            self.state, self.error = "ready", ""
        except Exception as e:
            self.state, self.error = "error", str(e)[:300]
            self._warm_failed_at = time.time()
        return True

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
