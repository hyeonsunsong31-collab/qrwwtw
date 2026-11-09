"""
voice/engine.py -- the full-duplex voice loop (requirements 3, 7, 8, 12, 13, 21).

  IDLE      listen for "wake up Jarvis"  -> on_wake()  (server speaks the response and calls
                                            begin_capture(after_speech=True))
  CAPTURE   record until the user stops talking -> Whisper -> on_command(text)
  SPEAKING  replies play from a queue, sentence by sentence; barge-in can cut them off:
              keyword (default): "stop" / "wait" / "hold on" / "Jarvis"  (immune to speaker echo)
              vad:               any speech (best with a headset)
              off:               no voice interruption
  MUTED     the AI-mic mute button closes the microphone stream entirely (privacy)

The WAKE button / Ctrl+Alt+J go through the same on_wake path, and the overlay's mic
button calls begin_capture() directly (push-to-talk), so all of them reach the identical
capture -> Whisper -> command flow.

If audio libraries are missing (e.g. while testing on Linux) `available` is False and the
server simply skips the engine; typed commands still work.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from typing import Callable, Optional

from .wakeword import make_detector, heard_wake, heard_keyword
from .stt import Transcriber
from .tts import Speaker
from .interrupt import BargeInMonitor

SR = 16000
FRAME = SR * 20 // 1000          # 320 samples = 20 ms
END_SILENCE_FRAMES = 40          # 0.8 s of silence ends a command
MAX_CAPTURE_FRAMES = 750         # 15 s cap
NO_SPEECH_FRAMES = 300           # give up after 6 s without speech
PARTIAL_KEYWORD_FRAMES = 4       # a keyword must persist this many frames in a partial result

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class VoiceEngine:
    def __init__(self, config: dict, on_wake: Callable, on_command: Callable,
                 on_interrupt: Callable, on_state: Optional[Callable] = None, root: str = ROOT):
        self.on_wake, self.on_command = on_wake, on_command
        self.on_interrupt, self.on_state = on_interrupt, on_state
        self.root = root
        self.state = "idle"
        self.listening = True
        self.current_text = ""
        self._pending_capture = False
        self._running = False
        self._stream = None
        self._loop = None
        self._frames: Optional[asyncio.Queue] = None
        self._speech: Optional[asyncio.Queue] = None
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore
            self._sd, self._np = sd, np
        except Exception:
            self.available = False
            return
        v = config.get("voice", {})
        tts = v.get("tts", {})
        self.speaker = Speaker(tts.get("voice_en", "en_GB-alan-medium"), tts.get("voice_ko", ""),
                               float(tts.get("rate", 1.0)), root=root)
        self.stt = Transcriber(v.get("stt", {}).get("model", "auto"),
                               v.get("stt", {}).get("language", "auto"))
        self.detector = make_detector(config, root)
        self.vad = BargeInMonitor()
        self.reconfigure(config)
        self.available = True

    # ------------------------------------------------------------ config #
    def reconfigure(self, config: dict):
        v = config.get("voice", {})
        bi = v.get("barge_in", {})
        self.barge_mode = bi.get("mode", "keyword")
        self.keywords = [k.lower() for k in bi.get("keywords", ["stop", "wait", "hold on", "jarvis"])]
        ww = v.get("wakeword", {})
        self.lenient = bool(ww.get("lenient", False))
        if self.detector is not None and hasattr(self.detector, "sensitivity"):
            self.detector.sensitivity = float(ww.get("sensitivity", 0.6))
        if getattr(self, "speaker", None) is not None:
            self.speaker.rate = float(v.get("tts", {}).get("rate", 1.0))

    async def _set_state(self, state: str):
        if state == self.state:
            return
        self.state = state
        if self.on_state:
            try:
                await _maybe_await(self.on_state(state))
            except Exception:
                pass

    # ---------------------------------------------------------- speaking #
    def enqueue(self, text: str, lang: str = "en"):
        """Queue a sentence to speak (non-blocking)."""
        if self.available and self._speech is not None and (text or "").strip():
            self._speech.put_nowait((text, lang))

    def stop_speaking(self):
        if not self.available:
            return
        self._pending_capture = False
        if self._speech is not None:
            while not self._speech.empty():
                try:
                    self._speech.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self.speaker.stop()

    async def _speech_worker(self):
        while self._running:
            text, lang = await self._speech.get()
            self.current_text = text
            self.vad.reset()
            await self._set_state("speaking")
            await self._loop.run_in_executor(None, self.speaker.speak, text, lang)
            self.current_text = ""
            if self._speech.empty() and self.state == "speaking":
                if self._pending_capture:
                    self._pending_capture = False
                    await self._enter_capture()
                else:
                    await self._set_state("idle" if self.listening else "muted")

    # ----------------------------------------------------------- capture #
    def begin_capture(self, after_speech: bool = True):
        """Listen for one command now, or right after current speech finishes."""
        if not self.available or not self.listening:
            return
        speaking = self.state == "speaking" or (self._speech is not None and not self._speech.empty())
        if after_speech and speaking:
            self._pending_capture = True
        else:
            self._schedule(self._enter_capture())

    def _schedule(self, coro):
        """Run a coroutine on the engine's loop from either that loop or another thread."""
        if self._loop is None:
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            asyncio.ensure_future(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _enter_capture(self):
        self._cap = bytearray()
        self._cap_frames = self._silence = 0
        self._had_speech = False
        await self._set_state("capture")

    def set_listening(self, enabled: bool):
        """AI-mic mute (requirement 6). Muting closes the microphone stream entirely."""
        self.listening = bool(enabled)
        if not enabled:
            self._pending_capture = False
        self._schedule(self._set_state("idle" if enabled else "muted"))

    # -------------------------------------------------------------- mic #
    def _callback(self, indata, frames, time_info, status):
        if self._loop is not None and self._frames is not None:
            self._loop.call_soon_threadsafe(self._frames.put_nowait, bytes(indata))

    def _open_stream(self) -> bool:
        try:
            self._stream = self._sd.RawInputStream(samplerate=SR, blocksize=FRAME, dtype="int16",
                                                   channels=1, callback=self._callback)
            self._stream.start()
            return True
        except Exception:
            self._stream = None
            return False

    def _close_stream(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    async def run(self):
        if not self.available:
            return
        self._loop = asyncio.get_event_loop()
        self._frames, self._speech = asyncio.Queue(), asyncio.Queue()
        self._running = True
        worker = asyncio.ensure_future(self._speech_worker())
        kw_hits = {}
        try:
            while self._running:
                # privacy: keep the microphone closed while JARVIS is muted
                if not self.listening:
                    if self._stream is not None:
                        self._close_stream()
                    await asyncio.sleep(0.2)
                    continue
                if self._stream is None and not self._open_stream():
                    await asyncio.sleep(2.0)          # no microphone yet; retry
                    continue
                try:
                    frame = await asyncio.wait_for(self._frames.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if self.state == "idle":
                    res = self.detector.process(frame) if self.detector else None
                    if res and heard_wake(res[0], self.lenient):
                        self.detector.reset()
                        await _maybe_await(self.on_wake())
                        if self.state == "idle" and not self._pending_capture:
                            await self._enter_capture()   # nothing to say first: listen now

                elif self.state == "capture":
                    await self._capture_frame(frame)

                elif self.state == "speaking":
                    if await self._barge_in(frame, kw_hits):
                        self.stop_speaking()
                        await _maybe_await(self.on_interrupt())
                        if self.detector:
                            self.detector.reset()
                        await self._enter_capture()
        finally:
            worker.cancel()
            self._close_stream()

    async def _barge_in(self, frame: bytes, kw_hits: dict) -> bool:
        if self.barge_mode == "off":
            return False
        if self.barge_mode == "vad":
            return self.vad.feed(frame)
        # keyword mode
        if not self.detector or not getattr(self.detector, "knows_keywords", False):
            res = self.detector.process(frame) if self.detector else None
            return bool(res and heard_wake(res[0], lenient=True))
        res = self.detector.process(frame)
        if not res:
            return False
        text, final = res
        kw = heard_keyword(text, self.keywords, spoken_now=self.current_text)
        if not kw:
            kw_hits.clear()
            return False
        if final:
            kw_hits.clear()
            return True
        kw_hits[kw] = kw_hits.get(kw, 0) + 1
        return kw_hits[kw] >= PARTIAL_KEYWORD_FRAMES

    async def _capture_frame(self, frame: bytes):
        self._cap += frame
        self._cap_frames += 1
        speech = self.vad.is_speech(frame) if self.vad.available else True
        self._had_speech = self._had_speech or speech
        self._silence = 0 if speech else self._silence + 1
        done = (self._had_speech and self._silence >= END_SILENCE_FRAMES) \
            or self._cap_frames >= MAX_CAPTURE_FRAMES
        if not done and not (not self._had_speech and self._cap_frames >= NO_SPEECH_FRAMES):
            return
        audio = None
        if self._had_speech:
            audio = self._np.frombuffer(bytes(self._cap), dtype=self._np.int16).astype(
                self._np.float32) / 32768.0
        self._cap = bytearray()
        await self._set_state("idle")
        if self.detector:
            self.detector.reset()
        if audio is not None:
            text, _lang = await self._loop.run_in_executor(None, self.stt.transcribe, audio, SR)
            if text.strip():
                await _maybe_await(self.on_command(text.strip()))

    def stop(self):
        self._running = False
        if self.available:
            self.stop_speaking()
        self._close_stream()
