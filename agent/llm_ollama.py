"""
agent/llm_ollama.py
===================
Local LLM backend via Ollama (the offline brain for natural conversation).

* OllamaLLM     -- chat() and token-streaming chat_stream() against the local Ollama,
                   resolve_model() which picks a model that is actually installed, pull().
* ResilientLLM  -- what the server uses: Ollama when reachable, an honest offline fallback
                   otherwise. It keeps the engine working by itself (v1.4.3): starts Ollama
                   (agent/ollama_service.py), downloads a missing model while using any model
                   that is already installed, steps down to a smaller model when the PC's
                   memory is too small, and explains what is wrong (state / problem / note /
                   progress) to the HUD, the launcher, "check the language engine" and the
                   fallback replies.
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

CONNECTING (v1.4.3): requests to an Ollama on this PC never go through a proxy -- with a
system proxy set (VPN, security software, company network) they used to go to the proxy and
fail, so a healthy Ollama looked "offline". Only a refused connection now counts as offline:
a missing model or a model too big for the memory is reported as what it is and repaired.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None

from .ollama_service import OllamaService, make_client, probe, PROBLEMS as ENGINE_PROBLEMS

# approximate download sizes (GB), for the disk-space check before a pull
MODEL_GB = {"qwen2.5:14b": 9.0, "qwen2.5:7b": 4.7, "qwen2.5:3b": 1.9, "qwen2.5:1.5b": 1.0,
            "qwen2.5:0.5b": 0.4, "llama3.1:8b": 4.9, "llama3.2:3b": 2.0}
SIZE_ORDER = ("high", "medium", "low", "tiny")
NOT_CHAT = ("embed", "bge-", "mxbai", "minilm", "nomic-embed", "snowflake-arctic-embed")

MODEL_PROBLEMS = {
    "model_missing": dict(
        action="check",
        en="The language model {model} isn't downloaded yet, sir.",
        fix="I'm downloading it now{progress}.",
        ko="언어 모델 {model}이(가) 아직 다운로드되지 않았습니다, sir.",
        fix_ko="지금 다운로드하고 있습니다{progress_ko}."),
    "pull_failed": dict(
        action="check",
        en="I couldn't download the language model {model} ({detail}).",
        fix="Check the internet connection; I'll try again in a few minutes.",
        ko="언어 모델 {model}을(를) 다운로드하지 못했습니다 ({detail}).",
        fix_ko="인터넷 연결을 확인해 주세요. 몇 분 뒤에 다시 시도하겠습니다."),
    "no_space": dict(
        action="check",
        en="There isn't enough disk space to download the language model {model} ({detail}).",
        fix="Free up some space on that drive; I'll try again in a few minutes.",
        ko="언어 모델 {model}을(를) 받을 디스크 공간이 부족합니다 ({detail}).",
        fix_ko="그 드라이브의 공간을 확보해 주세요. 몇 분 뒤에 다시 시도하겠습니다."),
    "memory": dict(
        action="check",
        en="The language model {model} needs more memory than this PC has free right now, sir.",
        fix="Close some programs, or choose a smaller model in my Settings (Language model).",
        ko="언어 모델 {model}에 필요한 메모리가 지금 이 PC에 부족합니다, sir.",
        fix_ko="다른 프로그램을 닫거나 제 설정(Language model)에서 더 작은 모델을 골라 주세요."),
    "load_failed": dict(
        action="check",
        en="The language model {model} could not be loaded ({detail}).",
        fix="I'll try again in a minute; if it keeps failing, run INSTALL_JARVIS.bat.",
        ko="언어 모델 {model}을(를) 불러오지 못했습니다 ({detail}).",
        fix_ko="1분 뒤에 다시 시도하겠습니다. 계속 실패하면 INSTALL_JARVIS.bat을 실행해 주세요."),
}


def model_problem(code: str, model: str, detail: str = "", percent: Optional[float] = None) -> dict:
    p = MODEL_PROBLEMS[code]
    prog = f" ({percent:.0f}% done)" if percent is not None else ""
    prog_ko = f" ({percent:.0f}% 완료)" if percent is not None else ""
    fmt = lambda s: (s.replace("{model}", model).replace("{detail}", detail or "unknown reason")  # noqa: E731
                     .replace("{progress}", prog).replace("{progress_ko}", prog_ko))
    return {"code": code, "detail": detail or None, "action": p["action"], "model": model,
            "message": fmt(p["en"]), "fix": fmt(p["fix"]), "message_ko": fmt(p["ko"]),
            "fix_ko": fmt(p["fix_ko"]), "since": time.time()}


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


def classify(e: BaseException) -> Tuple[str, str]:
    """What an error from Ollama means: offline | model_missing | memory | timeout | other."""
    name = type(e).__name__
    text = str(e)
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            text = f"{text} {resp.text[:300]}"
        except Exception:
            pass
    low = text.lower()
    if "Connect" in name or "connection refused" in low or "remoteprotocol" in name.lower():
        return "offline", "nothing is listening"
    status = getattr(resp, "status_code", None)
    if status == 404 or ("model" in low and "not found" in low) or "try pulling it first" in low:
        return "model_missing", text[:200]
    if any(k in low for k in ("memory", "out of memory", "oom", "cudamalloc", "insufficient")):
        return "memory", text[:200]
    if "Timeout" in name:
        return "timeout", "no answer in time"
    return "other", f"{name}: {text}"[:200]


class ToolsUnsupported(RuntimeError):
    """This model has no tool calling (Ollama answers 400 '... does not support tools')."""


def _tool_calls(msg: dict) -> List[dict]:
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        if fn.get("name"):
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_text": args}
            out.append({"name": fn["name"], "arguments": args or {}})
    return out


def _messages(system: str, user: str, history: Optional[List[dict]]) -> List[dict]:
    """System + history + the new message. The history is NOT windowed here: a window that
    slides by one turn every request changes the prompt prefix every time, so Ollama would
    re-read the whole conversation. The orchestrator trims it in larger steps instead."""
    msgs = [{"role": "system", "content": system}]
    for h in history or []:
        m = {"role": h["role"], "content": h.get("text", "")}
        if h.get("calls"):                     # an assistant turn that called tools (v1.4.4)
            m["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                               for c in h["calls"]]
        if h["role"] == "tool" and h.get("name"):
            m["tool_name"] = h["name"]
        msgs.append(m)
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

    def _client(self, timeout: Optional[float] = None):
        return make_client(self.host, self.timeout if timeout is None else timeout)

    def health(self) -> bool:
        return bool(probe(self.host, timeout=3.0)["ok"])

    def local_models(self) -> List[str]:
        if httpx is None:
            return []
        try:
            with self._client(6.0) as c:
                r = c.get(f"{self.host}/api/tags")
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
        chat = sorted(n for n in installed if not any(k in n for k in NOT_CHAT))
        if chat:
            self.model = chat[0]
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
            with self._client(5.0) as c:
                r = c.get(f"{self.host}/api/ps")
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
        with self._client(900) as c:
            r = c.post(f"{self.host}/api/generate",
                       json={"model": self.model, "prompt": "", "keep_alive": self.keep_alive,
                             "options": {"num_ctx": self.num_ctx}})
            r.raise_for_status()
            load_s = time.time() - t0
            body = self._body(system, "Reply with the single word: ready", [],
                              {"temperature": 0.0, "num_predict": 8}, False)
            r = c.post(f"{self.host}/api/chat", json=body)
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

    def pull(self, name: str, on_progress: Optional[Callable[[dict], None]] = None) -> None:
        """Download a model (POST /api/pull, streamed progress). Raises on failure."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        layers: Dict[str, Tuple[int, int]] = {}
        timeout = httpx.Timeout(None, connect=10.0)
        with make_client(self.host, 10.0) as c:
            with c.stream("POST", f"{self.host}/api/pull", json={"model": name, "name": name, "stream": True},
                          timeout=timeout) as r:
                if r.status_code >= 400:
                    r.read()
                    raise RuntimeError(f"Ollama refused the download (HTTP {r.status_code}): {r.text[:200]}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"])[:300])
                    if obj.get("digest") and obj.get("total"):
                        layers[obj["digest"]] = (int(obj.get("completed") or 0), int(obj["total"]))
                    done = sum(c_ for c_, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    if on_progress is not None:
                        on_progress({"status": obj.get("status", ""), "completed": done, "total": total,
                                     "percent": round(100.0 * done / total, 1) if total else 0.0})
                    if obj.get("status") == "success":
                        return
        raise RuntimeError("the download ended before it finished")

    def chat(self, system: str, user: str, history: Optional[List[dict]] = None,
             params: Optional[dict] = None) -> str:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        with self._client() as c:
            r = c.post(f"{self.host}/api/chat", json=self._body(system, user, history, params, False))
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "").strip()

    def chat_stream_events(self, system: str, user: str, history: Optional[List[dict]] = None,
                           params: Optional[dict] = None, tools: Optional[List[dict]] = None,
                           stop_event: Optional[threading.Event] = None) -> Iterator[Tuple[str, object]]:
        """Like chat_stream, with tools the model may call: yields ("text", piece) while it
        writes and ("tools", [{"name", "arguments"}, ...]) when it calls tools. Raises
        ToolsUnsupported for a model without tool calling."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body = self._body(system, user, history, params, True)
        if tools:
            body["tools"] = tools
        with self._client() as c:
            with c.stream("POST", f"{self.host}/api/chat", json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    text = r.text or ""
                    if tools and r.status_code == 400 and "tool" in text.lower() and "support" in text.lower():
                        raise ToolsUnsupported(text[:200])
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
                    if obj.get("error"):
                        err = str(obj["error"])
                        if tools and "tool" in err.lower() and "support" in err.lower():
                            raise ToolsUnsupported(err[:200])
                        raise RuntimeError(err[:300])
                    msg = obj.get("message", {}) or {}
                    calls = _tool_calls(msg)
                    if calls:
                        yield ("tools", calls)
                    piece = msg.get("content", "")
                    if piece:
                        yield ("text", piece)
                    if obj.get("done"):
                        break

    def chat_json(self, system: str, user: str, schema: dict, params: Optional[dict] = None,
                  timeout: Optional[float] = None) -> dict:
        """One reply forced into a JSON schema (Ollama structured output). The timeout is the
        client's own (a slow PC writing a long plan needs minutes, not seconds)."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body = self._body(system, user, [], params or {"temperature": 0.0, "num_predict": 200}, False)
        body["format"] = schema
        with self._client(timeout) as c:
            r = c.post(f"{self.host}/api/chat", json=body)
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "") or "{}"
        try:
            out = json.loads(content)
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {}

    def chat_stream(self, system: str, user: str, history: Optional[List[dict]] = None,
                    params: Optional[dict] = None,
                    stop_event: Optional[threading.Event] = None) -> Iterator[str]:
        """Yield the reply as it is generated (Ollama streams one JSON object per line)."""
        if httpx is None:
            raise RuntimeError("httpx not installed")
        body = self._body(system, user, history, params, True)
        with self._client() as c:
            with c.stream("POST", f"{self.host}/api/chat", json=body) as r:
                if r.status_code >= 400:
                    r.read()
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
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"])[:300])
                    piece = obj.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if obj.get("done"):
                        break


class MockLLM:
    """Deterministic offline stand-in (tests, and the fallback while Ollama is unavailable).
    With a reason_fn it says WHY the language engine is unavailable (v1.4.3)."""

    EN = ("Very good, sir. My language engine isn't reachable just now, so this is a short "
          "fallback reply. Direct commands such as opening apps or playing music still work.")
    KO = ("알겠습니다, sir. 지금은 언어 엔진에 연결되지 않아 간단한 대체 응답을 드립니다. "
          "앱 열기나 음악 재생 같은 직접 명령은 그대로 동작합니다.")

    def __init__(self, model: str = "offline-fallback", reason_fn: Optional[Callable[[str], str]] = None):
        self.model = model
        self.reason_fn = reason_fn

    def health(self) -> bool:
        return True

    def _reply(self, user: str) -> str:
        ko = any("가" <= c <= "힣" for c in user)
        reason = ""
        if self.reason_fn is not None:
            try:
                reason = self.reason_fn("ko" if ko else "en") or ""
            except Exception:
                reason = ""
        if not reason:
            return self.KO if ko else self.EN
        if ko:
            return (f"죄송합니다, sir. {reason} 그동안은 앱 열기나 음악 재생 같은 직접 명령만 수행할 수 있습니다. "
                    "'언어 엔진 확인해 줘'라고 하시면 자세히 알려 드립니다.")
        return (f"My apologies, sir: {reason} Until then I can only carry out direct commands, such as opening "
                "apps or playing music. Say 'check the language engine' for the details.")

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
    """Ollama when it works, an honest fallback when it does not -- and, in between, everything
    needed to get it working: start it, find or download a model, fit the model to the memory."""
    START_RETRY_S = (20.0, 60.0, 120.0)      # after 1, 2, 3+ failed starts
    PULL_RETRY_S = (120.0, 300.0, 900.0)

    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "auto",
                 candidates: Optional[Dict[str, str]] = None, recheck_seconds: float = 8.0,
                 keep_alive: Union[str, int, None] = "always", num_ctx: int = 4096,
                 data_dir: Optional[str] = None, log: Optional[Callable[[str], None]] = None,
                 on_model_change: Optional[Callable[[str], None]] = None, manage: bool = True):
        self.log = log or (lambda m: print("[jarvis] language engine: " + m, flush=True))
        self.service = OllamaService(host, data_dir=data_dir, log=self.log)
        self.ollama = OllamaLLM(self.service.base, model, candidates, keep_alive=keep_alive, num_ctx=num_ctx)
        self.mock = MockLLM(reason_fn=self.reason)
        self.recheck = recheck_seconds
        self.manage = manage                  # find / start / repair Ollama by itself
        self.on_model_change = on_model_change
        self._online = False
        self._checked = 0.0
        self._resolved = False
        self._lock = threading.Lock()
        self.state = "offline"         # starting | offline | not_installed | installing | pulling | loading | ready | error
        self.stats: dict = {}
        self.error = ""
        self.problem: Optional[dict] = None
        self.note = ""
        self.progress: Optional[dict] = None
        self.installed: List[str] = []
        self._warm_failed_at = 0.0
        self._warmed_model = ""
        self._no_model = False          # Ollama runs but no usable model is installed yet
        self._override = ""             # a smaller model chosen after a memory error
        self._too_big: set = set()      # models that did not fit in the memory (never used as fallback)
        self._pull_thread: Optional[threading.Thread] = None
        self._tools_ok: Dict[str, bool] = {}     # model -> it has native tool calling (v1.4.4)
        self.tools_enabled = True
        self._lent: Optional["OllamaLLM"] = None  # the developer's coding model, while memory is shared (v1.5.0)
        self._pull_failed_at = 0.0
        self._pull_failures = 0
        self._recheck_models = True
        self._install_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ status #
    def describe(self) -> dict:
        return {"state": self.state, "model": self.ollama.model, "desired": self._desired_name(),
                "installed": self.installed[:20], "problem": self.problem, "note": self.note or None,
                "progress": self.progress, "error": self.error or None, "stats": self.stats,
                "online": self._online, "engine": self.service.describe()}

    def reason(self, lang: str = "en") -> str:
        """One sentence on why replies are basic right now (for the fallback reply)."""
        ko = lang == "ko"
        p = self.problem or {}
        if self.state == "pulling" and self.progress:
            pct = self.progress.get("percent") or 0
            return (f"언어 모델 {self.progress.get('model')}을(를) 다운로드하고 있습니다({pct:.0f}% 완료)." if ko else
                    f"I'm downloading my language model {self.progress.get('model')} ({pct:.0f}% done).")
        if self.state == "starting":
            return "언어 엔진을 시작하고 있습니다." if ko else "my language engine is starting up."
        if p:
            return (p.get("message_ko") if ko else p.get("message", "")).rstrip(".") + "."
        if self.state in ("offline", "starting"):
            return ("언어 엔진(Ollama)이 실행되고 있지 않아 지금 시작하려고 하고 있습니다." if ko else
                    "my language engine (Ollama) isn't running; I'm trying to start it.")
        return ""

    # ------------------------------------------------------------ online #
    def online(self) -> bool:
        with self._lock:
            now = time.time()
            if now - self._checked >= self.recheck:
                self._checked = now
                self.ollama.host = self.service.base
                self._online = self.ollama.health()
                if self._online and not self._resolved:
                    self.ollama.resolve_model()
                    self._resolved = True
            return self._online

    def cached_online(self) -> bool:
        """The last known answer, without asking Ollama. For status reads on the event loop:
        a network call there would stall every request while Ollama is busy loading."""
        return self._online

    def _mark_offline(self):
        with self._lock:
            self._online = False
            self._checked = 0.0

    def _force_online(self):
        with self._lock:
            self.ollama.host = self.service.base
            self._online, self._checked = True, time.time()
            if not self._resolved:
                self.ollama.resolve_model()
                self._resolved = True

    def model_name(self) -> str:
        if self._lent is not None:
            return self._lent.model
        return self.ollama.model if (self._online and not self._no_model) else "offline-fallback"

    # ------------------------------------------------------------ models #
    def _desired_name(self) -> str:
        if self._override:
            return self._override
        if self.ollama.configured != "auto":
            return self.ollama.configured
        cands = self.ollama.candidates
        inst = {_norm(n) for n in self.installed}
        for key in SIZE_ORDER:
            c = cands.get(key)
            if c and _norm(c) in inst:
                return c
        return pick_model(cands)

    def _best_installed(self, exclude: str = "") -> str:
        inst = [n for n in self.installed if not any(k in n for k in NOT_CHAT) and _norm(n) != _norm(exclude)
                and _norm(n) not in self._too_big]
        if not inst:
            return ""
        norm = {_norm(n): n for n in inst}
        for key in SIZE_ORDER:
            c = self.ollama.candidates.get(key)
            if c and _norm(c) in norm:
                return norm[_norm(c)]
        return sorted(inst)[0]

    def _smaller_than(self, model: str) -> str:
        names = [self.ollama.candidates.get(k) for k in SIZE_ORDER if self.ollama.candidates.get(k)]
        norm = [_norm(n) for n in names]
        if _norm(model) in norm:
            i = norm.index(_norm(model))
            return names[i + 1] if i + 1 < len(names) else ""
        return names[-1] if names else ""

    def _is_installed(self, name: str) -> bool:
        return _norm(name) in {_norm(n) for n in self.installed}

    def _models_dir(self) -> str:
        d = os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")
        while d and not os.path.exists(d):
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return d or os.path.expanduser("~")

    def _start_pull(self, name: str) -> bool:
        if self._pull_thread is not None and self._pull_thread.is_alive():
            return True
        wait = self.PULL_RETRY_S[min(self._pull_failures, len(self.PULL_RETRY_S)) - 1] if self._pull_failures else 0
        if time.time() - self._pull_failed_at < wait:
            return False
        need = MODEL_GB.get(_norm(name).replace(":latest", ""), 5.0) * 1.1 + 0.5
        try:
            free = shutil.disk_usage(self._models_dir()).free / 1e9
        except OSError:
            free = need + 1
        if free < need:
            self.problem = model_problem("no_space", name, f"about {need:.0f} GB needed, {free:.1f} GB free")
            self._pull_failures += 1
            self._pull_failed_at = time.time()
            return False
        self.progress = {"model": name, "percent": 0.0, "status": "starting", "completed": 0, "total": 0}
        self._pull_thread = threading.Thread(target=self._pull_worker, args=(name,), name="jarvis-model-pull",
                                             daemon=True)
        self._pull_thread.start()
        return True

    def _pull_worker(self, name: str):
        self.log(f"downloading the language model {name}")
        last = 0.0

        def progress(p: dict):
            nonlocal last
            p = dict(p, model=name)
            self.progress = p
            if self._no_model and time.time() - last > 1.0:
                last = time.time()
                self.problem = model_problem("model_missing", name, percent=p.get("percent"))

        try:
            self.ollama.pull(name, on_progress=progress)
            self._pull_failures = 0
            self.log(f"language model {name} downloaded")
            if self.problem and self.problem.get("code") in ("model_missing", "pull_failed", "no_space"):
                self.problem = None
        except Exception as e:
            kind, msg = classify(e)
            self._pull_failures += 1
            self._pull_failed_at = time.time()
            self.problem = model_problem("pull_failed", name, msg if kind != "offline" else "the engine stopped")
            self.log(f"could not download {name}: {msg}")
        finally:
            self.progress = None
            self._recheck_models = True

    # ------------------------------------------------------------- warm #
    def ensure_warm(self, system: str) -> bool:
        """Called every few seconds by the server: start Ollama, get a model, (re)load and warm
        it when needed. Returns True when something changed (so the HUD can be told)."""
        def snap():
            return (self.state, (self.problem or {}).get("code"), (self.problem or {}).get("detail"), self.note,
                    int((self.progress or {}).get("percent") or 0), self.ollama.model, self.stats.get("warmed_at"))

        before = snap()
        try:
            self._ensure(system)
        except Exception as e:                       # never let a surprise stop the warm-up loop
            self.state, self.error = "error", f"{type(e).__name__}: {e}"[:300]
        return snap() != before

    def _ensure(self, system: str):
        if self._lent is not None and self.state == "ready":
            return                  # a developer job is using the memory: do not reload the chat model now
        if self._install_thread is not None and self._install_thread.is_alive():
            self.state, self.problem = "installing", self.service.problem
            return
        if not self.online():
            if not self.manage:
                self.state = "offline"
                return
            n = self.service.attempts
            wait = self.START_RETRY_S[min(n, len(self.START_RETRY_S)) - 1] if n else 0.0
            if time.time() - self.service.last_attempt < wait:
                if self.state not in ("not_installed", "offline"):
                    self.state = "offline"
                return
            self.state = "starting"
            if self.service.ensure_running():
                self.note = self.service.note
                self._force_online()
                self._recheck_models = True
            else:
                self.problem = self.service.problem
                code = (self.problem or {}).get("code")
                self.state = "not_installed" if code == "not_installed" else "offline"
                return
        # Ollama answers
        if self.problem and self.problem.get("code") in ENGINE_PROBLEMS:
            self.problem = None
        if self.service.note and self.service.note not in self.note:
            self.note = self.service.note
        self.ollama.host = self.service.base
        keep_forever = self.ollama.keep_alive == -1
        if self.state == "ready" and not self._recheck_models and self._warmed_model == self.ollama.model:
            if not keep_forever or self.ollama.loaded() is not None:
                return
            # Ollama restarted (or another app evicted the model): load it again
        if self.state == "error" and not self._recheck_models and time.time() - self._warm_failed_at < 60:
            return
        if self._recheck_models or self.state != "ready":
            self.installed = self.ollama.local_models()
            self._recheck_models = False
        desired = self._desired_name()
        pulling = self._pull_thread is not None and self._pull_thread.is_alive()
        if not self._is_installed(desired):
            fallback = self._best_installed(exclude=desired)
            started = self._start_pull(desired) or pulling
            if fallback:
                self._no_model = False
                if _norm(self.ollama.model) != _norm(fallback):
                    self.ollama.model = fallback
                self.note = (f"using {fallback} while {desired} downloads" if started
                             else f"using {fallback} ({desired} is not downloaded)")
            else:
                self._no_model = True
                self.state = "pulling" if started else "error"
                if started:
                    pct = (self.progress or {}).get("percent")
                    self.problem = model_problem("model_missing", desired, percent=pct)
                return
        else:
            self._no_model = False
            if _norm(self.ollama.model) != _norm(desired):
                self.ollama.model = desired
            if self.note.startswith("using "):
                self.note = self.service.note or ""
            if self.problem and self.problem.get("code") in ("model_missing", "pull_failed", "no_space"):
                self.problem = None
        if self.state == "ready" and self._warmed_model == self.ollama.model and \
                (not keep_forever or self.ollama.loaded() is not None):
            return
        self.state = "loading"
        try:
            self.stats = self.ollama.warm_up(system)
            self.state, self.error = "ready", ""
            self._warmed_model = self.ollama.model
            if self.problem and self.problem.get("code") in ("load_failed", "memory"):
                self.problem = None
        except Exception as e:
            kind, msg = classify(e)
            self._warm_failed_at = time.time()
            self.error = msg
            if kind == "offline":
                self._mark_offline()
                self.state = "offline"
            elif kind == "model_missing":
                self.state, self._recheck_models = "error", True
            elif kind == "memory":
                self._too_big.add(_norm(self.ollama.model))
                smaller = self._smaller_than(self.ollama.model)
                if smaller:
                    self.note = f"{self.ollama.model} needs more memory than is free; switched to {smaller}"
                    self.log(self.note)
                    self._override = smaller
                    self._recheck_models = True
                    self.state = "error"
                    if self.on_model_change is not None:
                        try:
                            self.on_model_change(smaller)
                        except Exception:
                            pass
                else:
                    self.state = "error"
                    self.problem = model_problem("memory", self.ollama.model)
            else:
                self.state = "error"
                self.problem = model_problem("load_failed", self.ollama.model, msg)

    # ----------------------------------------------------------- install #
    def install_engine(self) -> bool:
        """Install Ollama in the background ("install the language engine"). False if it
        is already installed or being installed."""
        if self._install_thread is not None and self._install_thread.is_alive():
            return False

        def work():
            if self.service.install():
                self._force_online()
                self._recheck_models = True
            self.problem = self.service.problem

        self._install_thread = threading.Thread(target=work, name="jarvis-install-ollama", daemon=True)
        self.state = "installing"
        self._install_thread.start()
        return True

    def restart_engine(self) -> bool:
        """'restart the language engine': look again now, start it if needed."""
        self.service.last_attempt = 0.0
        self.service.attempts = 0
        self._mark_offline()
        self._recheck_models = True
        ok = self.service.ensure_running()
        if ok:
            self._force_online()
        else:
            self.problem = self.service.problem
        return ok

    # -------------------------------------------------------------- chat #
    def _note_failure(self, e: BaseException):
        kind, msg = classify(e)
        self.error = msg
        if kind == "offline":
            self._mark_offline()
            if self.state in ("ready", "loading"):
                self.state = "offline"               # the HUD shows it at once; ensure_warm restarts it
        elif kind in ("model_missing", "memory"):
            self._recheck_models = True
            self.state = "error"
            if kind == "memory":
                self._warm_failed_at = 0.0          # let the next ensure_warm step down at once
        self.log(f"reply failed ({kind}): {msg}")

    # --------------------------------------------------------- tools (v1.4.4) #
    def lend(self, llm: Optional["OllamaLLM"]) -> None:
        """While a developer job runs and memory cannot hold both models, chat uses the coding
        model (None gives the chat model back)."""
        self._lent = llm

    def supports_tools(self) -> bool:
        """Whether replies use native tool calling: yes unless this model is known not to have it.
        (Deliberately independent of whether Ollama answers right now: the instructions must stay
        identical from turn to turn so Ollama can reuse its cache of them.)"""
        return bool(self.tools_enabled and self._tools_ok.get(_norm(self.ollama.model), True))

    def chat_stream_events(self, system, user, history=None, params=None, tools=None, stop_event=None):
        lent = self._lent
        if lent is not None:
            try:
                yield from lent.chat_stream_events(system, user, history, params, tools, stop_event)
                return
            except Exception as e:
                self.log(f"the shared coding model did not answer ({e}); using the chat model")
        if self.online() and not self._no_model:
            yielded = False
            try:
                for ev in self.ollama.chat_stream_events(system, user, history, params, tools, stop_event):
                    yielded = True
                    yield ev
                if tools:
                    self._tools_ok[_norm(self.ollama.model)] = True
                return
            except ToolsUnsupported:
                self._tools_ok[_norm(self.ollama.model)] = False
                self.log(f"{self.ollama.model} has no tool calling; using action lines instead")
                raise
            except Exception as e:
                self._note_failure(e)
                if yielded:
                    return
        for piece in self.mock.chat_stream(system, user, history, params, stop_event):
            yield ("text", piece)

    def chat_json(self, system, user, schema, params=None) -> dict:
        if self.online() and not self._no_model:
            try:
                return self.ollama.chat_json(system, user, schema, params)
            except Exception as e:
                self._note_failure(e)
        return {}

    def chat(self, system, user, history=None, params=None) -> str:
        if self.online() and not self._no_model:
            try:
                return self.ollama.chat(system, user, history, params)
            except Exception as e:
                self._note_failure(e)
        return self.mock.chat(system, user, history, params)

    def chat_stream(self, system, user, history=None, params=None, stop_event=None):
        lent = self._lent
        if lent is not None:
            try:
                yield from lent.chat_stream(system, user, history, params, stop_event)
                return
            except Exception:
                pass
        if self.online() and not self._no_model:
            yielded = False
            try:
                for piece in self.ollama.chat_stream(system, user, history, params, stop_event):
                    yielded = True
                    yield piece
                return
            except Exception as e:
                self._note_failure(e)
                if yielded:          # never splice a fallback onto a half-finished real reply
                    return
        yield from self.mock.chat_stream(system, user, history, params, stop_event)
