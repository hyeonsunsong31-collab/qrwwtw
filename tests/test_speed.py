"""
tests/test_speed.py -- reply latency (v1.4): what made JARVIS slow to answer, pinned down.

    python tests/test_speed.py            (or: python -m pytest tests/)

  * the neural mesh settles on every command: the new in-place step is the old step
    numerically, and the auto size keeps a command within the time budget
    (v1.3 on a 16 GB PC: ~4.5 s per command before the language model was even asked),
  * the language model stays loaded (keep_alive), is warmed up at start, is reloaded when
    Ollama restarts, and every request uses the same context size,
  * the prompt is cache-friendly: each turn's request is the previous request plus the new
    turns (system prompt and history unchanged), per-turn notes ride on the user message,
    and the history is trimmed rarely,
  * the reply is shown and spoken before bookkeeping, slow skills are acknowledged at once,
    a long first sentence starts speaking at its first clause, chat updates are throttled,
  * speech: the next sentence is synthesised while the current one plays, frequent phrases
    are cached, the Whisper options are valid for faster-whisper 1.2.1,
  * skills: the app index is used stale-while-refreshing, songs resolve in-process.
A small fake Ollama HTTP server stands in for the real one; no audio, network or models.
"""
import asyncio
import inspect
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from core.brain_mesh import BrainMesh, build_mesh  # noqa: E402
from core.scale import resolve_scale  # noqa: E402
from core.synaptic_perceptron import synapse_step  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =========================================================================== mesh
def _reference_step(m):
    """The v1.3 step, as it was (allocating, np.bincount)."""
    bs = m.module_activity("brainstem")
    arousal = float(0.9 + 0.7 * np.tanh(bs.mean()))
    pre = np.clip(m.a[m.cols], 0.0, None)
    tr, u, R = synapse_step(pre, m.w, m.u.copy(), m.R.copy(), m.gain, arousal, m.p)
    inp = np.bincount(m.rows, weights=tr, minlength=m.N).astype(np.float32)
    inp += m.bias + m.ext
    th = m.module_activity("thalamus")
    gate = float(1 / (1 + np.exp(-np.clip(1.5 * th.mean(), -40, 40))))
    g = np.ones(m.N, np.float32)
    for mod in ("cortex", "cerebellum"):
        s, e = m.module_range[mod]
        g[s:e] = 0.4 + 0.6 * gate
    inp *= g
    a = (0.15 * m.a + 0.85 * m.activation(inp)).astype(np.float32)
    np.clip(a, -1.5, 1.5, out=a)
    return a, u, R


def test_mesh_step_is_the_old_step():
    m = BrainMesh(resolve_scale(20000))
    m.ext[100:400] = 0.8
    worst = 0.0
    for _ in range(30):
        a_ref, u_ref, R_ref = _reference_step(m)
        m.step()
        worst = max(worst, float(np.abs(m.a - a_ref).max()),
                    float(np.abs(m.u - u_ref).max()), float(np.abs(m.R - R_ref).max()))
        m.a, m.u, m.R = a_ref.copy(), u_ref.copy(), R_ref.copy()
    assert worst < 1e-5, worst
    assert np.all(np.diff(m.rows) >= 0), "synapses are stored sorted by target neuron"


def test_auto_mesh_stays_within_the_command_budget():
    from core.control_layer import ControlLayer
    budget = 60.0
    mesh = build_mesh("auto", latency_budget_ms=budget, settle_steps=10)
    assert 20_000 <= mesh.N <= resolve_scale("auto").active_neurons
    assert mesh.settle_ms is not None and mesh.settle_ms < 3 * budget, mesh.settle_ms
    with tempfile.TemporaryDirectory() as d:
        ctl = ControlLayer(mesh=mesh, settle_steps=10, state_dir=d)
        ctl.compute("hello", "en")
        times = []
        for text in ("hello jarvis, how are you this evening?", "open notepad", "play some jazz") * 3:
            t0 = time.perf_counter()
            ctl.compute(text, "en")
            times.append(time.perf_counter() - t0)
    avg_ms = 1000 * sum(times) / len(times)
    # generous for slow CI machines; v1.3 took ~4500 ms here
    assert avg_ms < 4 * budget, f"compute() takes {avg_ms:.0f} ms per command"
    explicit = build_mesh(30_000, latency_budget_ms=budget)
    assert explicit.N == 30_000 and explicit.settle_ms is None, "an explicit size is used as given"


def test_auto_mesh_size_is_kept_across_restarts():
    """The timing behind the auto size varies run to run; a new size would throw away the
    synaptic gains the mesh learned. The chosen size is remembered while it fits the budget."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "brain", "mesh_size.json")
        first = build_mesh("auto", latency_budget_ms=60, settle_steps=10, remember=path)
        stored = json.load(open(path, encoding="utf-8"))
        assert stored["neurons"] == first.N and stored["budget_ms"] == 60
        again = build_mesh("auto", latency_budget_ms=60, settle_steps=10, remember=path)
        assert again.N == first.N
        # a stored size that no longer fits (e.g. the PC got slower) is recalibrated
        json.dump(dict(stored, neurons=400_000), open(path, "w", encoding="utf-8"))
        logs = []
        third = build_mesh("auto", latency_budget_ms=60, settle_steps=10, remember=path, log=logs.append)
        assert third.N < 400_000 and any("recalibrating" in m for m in logs), logs
        # a different budget means a different size decision
        json.dump(dict(stored, budget_ms=500), open(path, "w", encoding="utf-8"))
        fourth = build_mesh("auto", latency_budget_ms=60, settle_steps=10, remember=path)
        assert json.load(open(path, encoding="utf-8"))["budget_ms"] == 60 and fourth.N >= 20_000


# =========================================================================== fake Ollama
class FakeOllama:
    REPLY = "Very good, sir. The answer, I am pleased to report, is forty-two. Anything else?"

    def __init__(self, model="qwen2.5:3b", load_delay=0.0):
        self.model, self.load_delay = model, load_delay
        self.loaded = False
        self.requests = []          # (path, body)
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": outer.model}]})
                if self.path == "/api/ps":
                    loaded = [{"name": outer.model, "size": 2_000_000, "size_vram": 1_500_000}] if outer.loaded else []
                    return self._json(200, {"models": loaded})
                self._json(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append((self.path, body))
                if self.path == "/api/generate":
                    time.sleep(outer.load_delay)
                    outer.loaded = True
                    return self._json(200, {"model": outer.model, "done": True})
                if self.path == "/api/chat":
                    outer.loaded = True
                    if not body.get("stream"):
                        return self._json(200, {"message": {"content": "ready"}, "done": True,
                                                "eval_count": 8, "eval_duration": 200_000_000,
                                                "prompt_eval_count": 400, "prompt_eval_duration": 100_000_000})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for word in outer.REPLY.split(" "):
                        line = json.dumps({"message": {"content": word + " "}, "done": False}) + "\n"
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                    self.wfile.write((json.dumps({"done": True}) + "\n").encode())
                    return
                self._json(404, {})

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def host(self):
        return f"http://127.0.0.1:{self.port}"

    def chats(self):
        return [b for p, b in self.requests if p == "/api/chat" and b.get("stream")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_requests_keep_the_model_loaded_with_a_constant_context():
    from agent.llm_ollama import OllamaLLM, keep_alive_value
    assert keep_alive_value("always") == -1 and keep_alive_value(None) == -1 and keep_alive_value("15m") == "15m"
    fake = FakeOllama()
    try:
        llm = OllamaLLM(fake.host, model="qwen2.5:3b", num_ctx=4096)
        assert "".join(llm.chat_stream("sys", "hi", [], {"temperature": 0.3, "num_predict": 50})).strip() == FakeOllama.REPLY
        llm.chat_stream_list = list(llm.chat_stream("sys", "again", [], {"temperature": 0.9, "num_predict": 300}))
        bodies = fake.chats()
        assert len(bodies) == 2
        for b in bodies:
            assert b["keep_alive"] == -1, "the model must not be unloaded after 5 idle minutes"
            assert b["options"]["num_ctx"] == 4096, "a changing num_ctx makes Ollama reload the model"
        assert {b["options"]["temperature"] for b in bodies} == {0.3, 0.9}    # sampling may vary
        short = OllamaLLM(fake.host, model="qwen2.5:3b", keep_alive="5m")
        assert short._body("s", "u", [], None, False)["keep_alive"] == "5m"
    finally:
        fake.close()


def test_warm_up_loads_prefills_and_measures():
    from agent.llm_ollama import ResilientLLM
    fake = FakeOllama(load_delay=0.15)
    try:
        llm = ResilientLLM(fake.host, model="qwen2.5:3b", recheck_seconds=0.0)
        assert llm.state == "offline"
        assert llm.ensure_warm("SYSTEM PROMPT") is True and llm.state == "ready", (llm.state, llm.error)
        st = llm.stats
        assert st["tokens_per_s"] == 40.0 and st["gpu"] == 0.75 and st["load_seconds"] >= 0.15, st
        paths = [p for p, _ in fake.requests]
        assert paths[:2] == ["/api/generate", "/api/chat"], paths
        gen, warm = fake.requests[0][1], fake.requests[1][1]
        assert gen["prompt"] == "" and gen["keep_alive"] == -1 and gen["options"]["num_ctx"] == 4096
        assert warm["messages"][0] == {"role": "system", "content": "SYSTEM PROMPT"}, "prefills the real system prompt"
        assert warm["options"]["num_ctx"] == gen["options"]["num_ctx"]
        # loaded and nothing changed: no new requests
        n = len(fake.requests)
        assert llm.ensure_warm("SYSTEM PROMPT") is False and len(fake.requests) == n
        # Ollama restarted (model gone from /api/ps): load it again
        fake.loaded = False
        assert llm.ensure_warm("SYSTEM PROMPT") is True and llm.state == "ready" and fake.loaded
        # with a keep-alive duration chosen by the user, an unloaded model is left unloaded
        fake.loaded = False
        llm.ollama.keep_alive = "5m"
        n = len(fake.requests)
        assert llm.ensure_warm("SYSTEM PROMPT") is False and [p for p, _ in fake.requests[n:]] == []
    finally:
        fake.close()
    # Ollama gone: offline, without raising
    assert llm.ensure_warm("SYSTEM PROMPT") in (True, False) and llm.state == "offline"


def _orchestrator(tmp, llm, events):
    from agent.orchestrator import Orchestrator
    from agent.permissions import PermissionManager
    from core.control_layer import ControlLayer
    from recorder import Recorder

    async def emit(ev):
        events.append((time.perf_counter(), ev))

    cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    rec = Recorder(os.path.join(tmp, "data"))
    control = ControlLayer(mesh=BrainMesh(resolve_scale(20000)), state_dir=os.path.join(tmp, "data", "brain"))
    return Orchestrator(cfg, llm, recorder=rec, control=control, emit=emit,
                        permissions=PermissionManager(config_path=os.path.join(ROOT, "config", "permissions.json"),
                                                      recorder=rec))


def test_each_request_extends_the_previous_one():
    """Ollama re-reads only what is new after the longest prefix it has cached. So request
    k+1 must start with request k exactly (same system prompt, same history), and per-turn
    information must ride on the newest user message."""
    from agent.llm_ollama import ResilientLLM
    from agent import orchestrator as orch_mod
    fake = FakeOllama()
    try:
        llm = ResilientLLM(fake.host, model="qwen2.5:3b", recheck_seconds=60)
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            orch = _orchestrator(tmp, llm, events)
            turns = ["hello jarvis, how are you this evening?", "tell me about the weather on mars",
                     "open the 3D project space",            # a skill turn in between
                     "why is the sky blue", "thank you, that was helpful",
                     "remember that I prefer short answers",  # changes the system prompt once
                     "what should I cook tonight", "and for dessert?"]
            for text in turns:
                asyncio.run(orch.handle_command(text))
            chats = [b["messages"] for b in fake.chats()]
            assert len(chats) == 6, len(chats)              # the skill and the remember turn are not LLM turns
            systems = [m[0]["content"] for m in chats]
            # 4 model turns before "remember that ...", 2 after it
            assert len(set(systems[:4])) == 1 and len(set(systems[4:])) == 1, "system prompt stays the same"
            assert "I prefer short answers" in systems[4] and "I prefer short answers" not in systems[0]
            for prev, nxt in zip(chats, chats[1:]):
                if prev[0] != nxt[0]:
                    continue                                 # the one turn after "remember that ..."
                assert nxt[:len(prev) - 1] == prev[:-1], "history must be unchanged between turns"
                assert nxt[len(prev) - 1]["content"] == prev[-1]["content"], "last turn kept exactly as sent"
            for msgs in chats:
                # (the system prompt explains the note format, so look at the conversation itself)
                notes = [m for m in msgs[1:] if "[Host note - not from the user:" in m["content"]]
                assert notes and all(m["role"] == "user" for m in notes)
                assert "[Host note" in msgs[-1]["content"], "the per-turn note rides on the newest message"
                system = msgs[0]["content"]
                assert "STYLE:" not in system and "Stay in character as JARVIS, a courteous" not in system, \
                    "no per-turn style in the cached system prompt"
            assistant = [m for m in chats[-1] if m["role"] == "assistant"]
            assert any(m["content"].strip() == FakeOllama.REPLY for m in assistant), "raw reply kept as generated"
            assert any("3D project space" in m["content"] for m in chats[-1]), "skill turns are context too"

            # many more turns: the history is bounded and trimmed in steps, not every turn
            prefixes_broken = 0
            for i in range(24):
                asyncio.run(orch.handle_command(f"question number {i}: what is {i} times {i}?"))
            chats = [b["messages"] for b in fake.chats()][-24:]
            for prev, nxt in zip(chats, chats[1:]):
                if nxt[:len(prev) - 1] != prev[:-1]:
                    prefixes_broken += 1
            assert len(orch.llm_history) <= orch_mod.HISTORY_MAX_MESSAGES
            assert prefixes_broken <= 6, f"history trimmed {prefixes_broken} times in 24 turns"
            assert all(b["keep_alive"] == -1 and b["options"]["num_ctx"] == 4096 for b in fake.chats())
    finally:
        fake.close()


def test_reply_is_shown_and_spoken_before_bookkeeping_and_slow_skills_are_acknowledged():
    from agent.llm_ollama import MockLLM
    from agent.skills import REGISTRY, Skill

    def slow_music(args):
        time.sleep(0.3)
        return {"ok": True, "speak": f"Now playing {args.get('query')}, sir.",
                "data": {"title": args.get("query"), "mode": "stream"}, "error": None}

    saved = REGISTRY["play_music"]
    REGISTRY["play_music"] = Skill(slow_music, saved.category, saved.eta)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            orch = _orchestrator(tmp, MockLLM(), events)
            t0 = time.perf_counter()
            asyncio.run(orch.handle_command("play some lofi jazz"))
            says = [(t, e["text"]) for t, e in events if e["type"] == "say"]
            assert says[0][1] == "Right away, sir." and says[0][0] - t0 < 0.25, says
            assert says[1][1].startswith("Now playing") and says[1][0] - says[0][0] >= 0.25
            kinds = [e["type"] for _, e in events]
            final = max(i for i, k in enumerate(kinds) if k == "transcript")
            last_control = max(i for i, k in enumerate(kinds) if k == "control")
            assert final < last_control, "the reply is emitted before the drive / self-model updates"
            # Korean acknowledgement
            events.clear()
            asyncio.run(orch.handle_command("로파이 재즈 틀어줘"))
            assert [e["text"] for _, e in events if e["type"] == "say"][0] == "바로 하겠습니다, sir."
    finally:
        REGISTRY["play_music"] = saved


def test_first_clause_is_spoken_early_and_deltas_are_throttled():
    from agent.orchestrator import pop_first_clause, pop_sentences, DELTA_INTERVAL
    assert pop_first_clause("Certainly, sir, the capital of") == (None, "Certainly, sir, the capital of")
    clause, rest = pop_first_clause("Certainly, sir, the capital of France is Paris, a city of light and ")
    assert clause == "Certainly, sir, the capital of France is Paris," and rest.startswith("a city")
    clause, _ = pop_first_clause("x" * 30 + " " + "y" * 200)
    assert clause == "x" * 30, "very long runs without punctuation break at a word"
    assert pop_sentences("Very good, sir. The answer")[0] == ["Very good, sir."]

    class SlowSentenceLLM:
        """Streams one long opening sentence, then more, a word at a time."""
        TEXT = ("Of all the theories put forward over the centuries, sir, the one that holds up best "
                "is rather simple. It involves light. It involves air.")

        def model_name(self):
            return "fake"

        def chat_stream(self, system, user, history=None, params=None, stop_event=None):
            for w in self.TEXT.split(" "):
                time.sleep(0.004)
                yield w + " "

    with tempfile.TemporaryDirectory() as tmp:
        events = []
        orch = _orchestrator(tmp, SlowSentenceLLM(), events)
        asyncio.run(orch.handle_command("why is the sky blue, in your view, honestly?"))
        says = [e["text"] for _, e in events if e["type"] == "say"]
        assert says[0] == "Of all the theories put forward over the centuries,", says
        deltas = [e for _, e in events if e["type"] == "transcript_delta"]
        words = len(SlowSentenceLLM.TEXT.split(" "))
        assert 1 <= len(deltas) < words, (len(deltas), words)      # throttled, last one flushed
        assert deltas[-1]["text"].endswith("It involves air."), deltas[-1]["text"]
        assert DELTA_INTERVAL <= 0.05


# =========================================================================== speech
fake_sd = types.ModuleType("sounddevice")
fake_sd.RawInputStream = lambda **kw: None
fake_sd.play = lambda *a, **k: None
fake_sd.wait = lambda *a, **k: None
fake_sd.stop = lambda *a, **k: None


class PipeSpeaker:
    """prepare() takes `synth` seconds, play_prepared() takes `play` seconds."""
    available = True

    def __init__(self, synth=0.12, play=0.12):
        self.synth, self.play, self.rate = synth, play, 1.0
        self.prepared, self.played, self.stops = [], [], 0
        self._stop = threading.Event()

    def prepare(self, text, lang="en"):
        time.sleep(self.synth)
        self.prepared.append(text)
        return {"text": text, "audio": b"x", "sr": 22050}

    def reset_stop(self):
        self._stop.clear()

    def play_prepared(self, prep):
        t0 = time.time()
        while time.time() - t0 < self.play and not self._stop.is_set():
            time.sleep(0.005)
        if not self._stop.is_set():
            self.played.append(prep["text"])

    def speak(self, text, lang="en"):
        self.play_prepared(self.prepare(text, lang))

    def stop(self):
        self._stop.set()
        self.stops += 1


def _engine(speaker):
    sys.modules.setdefault("sounddevice", fake_sd)
    from voice.engine import VoiceEngine
    states = []

    async def noop(*a):
        return None

    async def on_state(s):
        states.append(s)

    eng = VoiceEngine({"voice": {"tts": {}, "stt": {"end_silence_ms": 500}}}, noop, noop, noop, on_state)
    eng.detector, eng.speaker = None, speaker
    eng.vad = types.SimpleNamespace(reset=lambda: None, available=False)
    return eng, states


def test_next_sentence_is_synthesised_while_the_current_one_plays():
    speaker = PipeSpeaker(0.12, 0.12)
    eng, states = _engine(speaker)
    assert eng.end_silence_frames == 25, "end-of-speech pause comes from voice.stt.end_silence_ms"

    async def run():
        eng._loop = asyncio.get_event_loop()
        eng._frames, eng._speech, eng._running = asyncio.Queue(), asyncio.Queue(), True
        worker = asyncio.ensure_future(eng._speech_worker())
        t0 = time.perf_counter()
        for i in range(4):
            eng.enqueue(f"Sentence number {i}.")
        while len(speaker.played) < 4 and time.perf_counter() - t0 < 5:
            await asyncio.sleep(0.01)
        took = time.perf_counter() - t0
        await asyncio.sleep(0.05)
        worker.cancel()
        return took

    took = asyncio.run(run())
    assert speaker.played == [f"Sentence number {i}." for i in range(4)]
    # sequential would be 4 x (0.12 + 0.12) = 0.96 s; pipelined ~ 0.12 + 4 x 0.12 = 0.60 s
    assert took < 0.85, f"{took:.2f}s"
    assert states[-1] == "idle"


def test_stop_drops_the_sentence_synthesised_ahead():
    speaker = PipeSpeaker(0.05, 0.4)
    eng, states = _engine(speaker)

    async def run():
        eng._loop = asyncio.get_event_loop()
        eng._frames, eng._speech, eng._running = asyncio.Queue(), asyncio.Queue(), True
        worker = asyncio.ensure_future(eng._speech_worker())
        eng.enqueue("First sentence, which will be interrupted.")
        eng.enqueue("Second sentence, synthesised ahead.")
        await asyncio.sleep(0.2)                     # first is playing, second is prepared
        eng.stop_speaking()
        await asyncio.sleep(0.6)
        worker.cancel()

    asyncio.run(run())
    assert speaker.played == [], speaker.played      # first cut off, second never played
    assert "Second sentence, synthesised ahead." in speaker.prepared
    assert states[-1] == "idle", "after an interruption the engine goes back to listening for the wake word"


def test_frequent_phrases_are_cached():
    sys.modules.setdefault("sounddevice", fake_sd)
    from voice.tts import Speaker, CACHE_MAX_TEXT

    class CountingSpeaker(Speaker):
        def __init__(self):
            self.calls = 0
            self.rate = 1.0
            self.available = True
            self._piper, self._winrt_ok, self._sapi_ok = {}, False, False
            self._stop = threading.Event()
            self._lock = threading.Lock()
            from collections import OrderedDict
            self._cache = OrderedDict()
            self._cache_lock = threading.Lock()

        def _synthesize(self, text, lang):
            self.calls += 1
            return {"text": text, "lang": lang, "audio": np.zeros(10, np.int16), "sr": 22050}

    sp = CountingSpeaker()
    assert sp.prewarm([("Jarvis at your service sir", "en"), ("Right away, sir.", "en")]) == 2
    calls = sp.calls
    assert sp.prepare("Jarvis at your service sir") is not None and sp.calls == calls, "wake reply comes from the cache"
    long_text = "word " * (CACHE_MAX_TEXT // 4)
    sp.prepare(long_text)
    sp.prepare(long_text)
    assert sp.calls == calls + 2, "long sentences are not cached"
    sp.rate = 1.3
    sp.prepare("Right away, sir.")
    assert sp.calls == calls + 3, "a different speaking rate is a different recording"


# faster-whisper 1.2.1 WhisperModel.transcribe() parameters (from its source), used when the
# package is not installed on the test machine
FW_121_TRANSCRIBE = {
    "audio", "language", "task", "log_progress", "beam_size", "best_of", "patience", "length_penalty",
    "repetition_penalty", "no_repeat_ngram_size", "temperature", "compression_ratio_threshold",
    "log_prob_threshold", "no_speech_threshold", "condition_on_previous_text", "prompt_reset_on_temperature",
    "initial_prompt", "prefix", "suppress_blank", "suppress_tokens", "without_timestamps",
    "max_initial_timestamp", "word_timestamps", "prepend_punctuations", "append_punctuations",
    "multilingual", "vad_filter", "vad_parameters", "max_new_tokens", "chunk_length", "clip_timestamps",
    "hallucination_silence_threshold", "hotwords", "language_detection_threshold",
    "language_detection_segments"}


def test_whisper_options_are_valid_and_fast():
    from voice.stt import Transcriber, cpu_threads
    try:
        from faster_whisper import WhisperModel  # type: ignore
        valid = set(inspect.signature(WhisperModel.transcribe).parameters) - {"self"}
    except Exception:
        valid = FW_121_TRANSCRIBE
    t = Transcriber.__new__(Transcriber)
    t.language, t.cuda = None, False
    opts = t.options()
    assert set(opts) <= valid, set(opts) - valid
    assert opts["beam_size"] == 1 and opts["without_timestamps"] and not opts["condition_on_previous_text"]
    assert tuple(opts["temperature"]) == (0.0, 0.4)
    t.cuda = True
    assert t.options()["beam_size"] == 3
    assert 2 <= cpu_threads() <= 8


# =========================================================================== skills
def test_app_index_is_used_while_it_refreshes():
    from agent.skills import apps
    saved = (apps.IS_WINDOWS, apps._scan_apps, dict(apps._INDEX))
    scans = []

    def slow_scan():
        time.sleep(0.3)
        scans.append(time.time())
        return [("Spotify", "aumid", f"Spotify!App{len(scans)}")]

    try:
        apps.IS_WINDOWS, apps._scan_apps = True, slow_scan
        apps._INDEX.update(t=0.0, items=[])
        assert apps.build_index()[0][2] == "Spotify!App1" and len(scans) == 1      # first build waits
        apps._INDEX["t"] = time.time() - apps._INDEX_TTL - 1                         # now stale
        t0 = time.perf_counter()
        items = apps.build_index()
        assert time.perf_counter() - t0 < 0.1 and items[0][2] == "Spotify!App1", "stale index answers at once"
        deadline = time.time() + 3
        while len(scans) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert apps.build_index()[0][2] == "Spotify!App2", "and is replaced in the background"
    finally:
        apps.IS_WINDOWS, apps._scan_apps = saved[0], saved[1]
        apps._INDEX.clear()
        apps._INDEX.update(saved[2])


def test_songs_resolve_in_process_and_the_player_is_remembered():
    from agent.skills import music
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=True):
            seen["query"], seen["download"] = query, download
            return {"entries": [None, {"title": "Lofi Girl", "url": "https://stream.example/a"}]}

    saved_mod = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)
    try:
        assert music._resolve_stream("lofi jazz") == ("Lofi Girl", "https://stream.example/a")
        assert seen["download"] is False and seen["opts"]["default_search"] == "ytsearch1"
    finally:
        if saved_mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = saved_mod
    calls = []
    saved = (music._find_player, dict(music._PLAYER))
    try:
        music._PLAYER.update(found=False, value=None)
        music._find_player = lambda: calls.append(1) or ("ffplay", "/usr/bin/ffplay")
        assert music.find_player() == ("ffplay", "/usr/bin/ffplay")
        assert music.find_player() == ("ffplay", "/usr/bin/ffplay") and len(calls) == 1
    finally:
        music._find_player = saved[0]
        music._PLAYER.clear()
        music._PLAYER.update(saved[1])


def test_settings_accept_the_speed_options():
    from agent.settings import validate
    assert validate("llm.keep_alive", "always") == "always" and validate("llm.keep_alive", "5m") == "5m"
    assert validate("voice.stt.end_silence_ms", "650") == 650
    assert validate("llm.context_tokens", 8192.0) == 8192
    for key, bad in (("llm.keep_alive", "forever-ish"), ("voice.stt.end_silence_ms", 50),
                     ("llm.context_tokens", "lots")):
        try:
            validate(key, bad)
            raise AssertionError(f"{key}={bad!r} should be rejected")
        except ValueError:
            pass


# =========================================================================== server
def test_server_warms_the_model_and_reports_it():
    try:
        import httpx
        import uvicorn
        from agent.server import JarvisServer, create_app
    except Exception as e:  # pragma: no cover
        print(f"  (skipped: {e})")
        return
    fake = FakeOllama()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
        cfg["llm"]["host"], cfg["llm"]["model"] = fake.host, "qwen2.5:3b"
        cfg["neural_core"]["active_neurons"] = 20000
        cfg["voice"]["enabled"] = False
        port = free_port()
        cfg["server"]["port"] = port
        cfg_path = os.path.join(tmp, "config.json")
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))
        perm = os.path.join(tmp, "permissions.json")
        json.dump(json.load(open(os.path.join(ROOT, "config", "permissions.json"), encoding="utf-8")),
                  open(perm, "w", encoding="utf-8"))
        srv = JarvisServer(config_path=cfg_path, permissions_path=perm, data_dir=os.path.join(tmp, "data"))
        server = uvicorn.Server(uvicorn.Config(create_app(srv, start_voice=False), host="127.0.0.1",
                                               port=port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        try:
            status = {}
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    status = httpx.get(f"http://127.0.0.1:{port}/status",
                                       headers={"x-jarvis-token": srv.token}, timeout=2).json()
                    if status.get("llm_state") == "ready":
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            assert status.get("llm_state") == "ready", status
            assert status["llm_tokens_per_s"] == 40.0 and status["llm_gpu"] == 0.75, status
            assert status["version"] == "1.4.0"
            warm = [b for p, b in fake.requests if p == "/api/chat" and not b.get("stream")]
            assert warm and warm[0]["messages"][0]["content"] == srv.orch.system_prompt("en"), \
                "the system prompt JARVIS actually uses is the one read into the cache"
        finally:
            server.should_exit = True
            th.join(timeout=10)
            fake.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        t0 = time.time()
        try:
            t()
            print(f"  ok  {t.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  XX  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} speed tests passed")
    sys.exit(1 if failed else 0)
