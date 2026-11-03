"""
voice/engine.py — full-duplex voice loop (requirements 3, 7, 8, 12, 13, 21).

A single microphone stream drives a small state machine:

  IDLE ──wake word ("wake up Jarvis")──► (speak "Jarvis at your service sir")
       └─► CAPTURE ──record until silence──► transcribe ──► on_command(text)
  SPEAKING (while a reply is spoken): VAD barge-in ──► stop TTS ──► CAPTURE (new command)

Runs in-process inside the backend. If audio libraries aren't installed (e.g. during
Linux testing) `available` is False and the backend simply skips it — text still works,
and the overlay's emergency wake button / typed commands reach the same command bus.

Callbacks may be sync or async; both are awaited. The backend passes:
  on_wake()           -> coroutine that emits the wake response (and speaks it)
  on_command(text)    -> coroutine that submits the recognised command
  on_interrupt()      -> coroutine that cancels the in-flight reply
"""
from __future__ import annotations

import asyncio
import inspect
import queue
from typing import Callable

from .wakeword import WakeWordDetector
from .stt import Transcriber
from .tts import Speaker
from .interrupt import BargeInMonitor
from .response import wake_response

SR = 16000
FRAME_MS = 20
FRAME = SR * FRAME_MS // 1000           # 320 samples / 20 ms
WAKE_FRAME = 1280                        # 80 ms for openWakeWord


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class VoiceEngine:
    def __init__(self, config: dict, on_wake: Callable, on_command: Callable,
                 on_interrupt: Callable):
        self.config = config
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_interrupt = on_interrupt
        self.state = "idle"
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._stream = None
        self._running = False

        v = config.get("voice", {})
        ww = v.get("wakeword", {})
        tts = v.get("tts", {})
        stt = v.get("stt", {})

        self._sd = None
        self._np = None
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore
            self._sd, self._np = sd, np
        except Exception:
            self.available = False
            return

        self.detector = WakeWordDetector(ww.get("phrase", "wake up jarvis"),
                                         ww.get("model", "models/wake_up_jarvis.onnx"),
                                         float(ww.get("sensitivity", 0.6)))
        self.stt = Transcriber(stt.get("model", "auto"), stt.get("language", "auto"))
        self.speaker = Speaker(tts.get("voice_en", "en_GB-alan-medium"),
                               tts.get("voice_ko", "ko_KR-glow"),
                               float(tts.get("rate", 1.0)))
        self.barge = BargeInMonitor()
        self.available = True

    # ------------------------------------------------------------- TTS #
    async def speak(self, text: str, lang: str = "en"):
        if not self.available or not self.speaker.available:
            return
        self.state = "speaking"
        self.barge.reset()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.speaker.speak, text, lang)
        if self.state == "speaking":
            self.state = "idle"

    def stop_speaking(self):
        if self.available and self.speaker.available:
            self.speaker.stop()

    # ---------------------------------------------------------- mic in #
    def _callback(self, indata, frames, time_info, status):
        self._q.put(bytes(indata))

    async def run(self):
        if not self.available:
            return
        self._running = True
        wake_acc = bytearray()
        cap = bytearray()
        silence = 0
        had_speech = False
        cap_frames = 0
        try:
            self._stream = self._sd.RawInputStream(
                samplerate=SR, blocksize=FRAME, dtype="int16", channels=1,
                callback=self._callback)
            self._stream.start()
        except Exception:
            self.available = False
            return

        while self._running:
            try:
                frame = self._q.get(timeout=0.2)
            except queue.Empty:
                await asyncio.sleep(0)
                continue

            if self.state == "idle":
                wake_acc += frame
                if len(wake_acc) >= WAKE_FRAME * 2:  # 80 ms of int16 = 2560 bytes
                    chunk = self._np.frombuffer(bytes(wake_acc[:WAKE_FRAME * 2]), dtype=self._np.int16)
                    del wake_acc[:WAKE_FRAME * 2]
                    if self.detector.triggered(chunk):
                        self.detector.reset()
                        await _maybe_await(self.on_wake())
                        self.state, cap, silence, had_speech, cap_frames = "capture", bytearray(), 0, False, 0

            elif self.state == "capture":
                cap += frame
                cap_frames += 1
                speech_now = _is_voiced(self.barge, frame)
                had_speech = had_speech or speech_now
                silence = 0 if speech_now else silence + 1
                end = (had_speech and silence > 40) or cap_frames > 600  # ~0.8s silence / 12s max
                no_speech_timeout = (not had_speech and cap_frames > 300)  # ~6s
                if end or no_speech_timeout:
                    text = ""
                    if had_speech:
                        audio = self._np.frombuffer(bytes(cap), dtype=self._np.int16).astype(
                            self._np.float32) / 32768.0
                        text, _lang = self.stt.transcribe(audio, SR)
                    self.state = "idle"
                    if text.strip():
                        await _maybe_await(self.on_command(text.strip()))

            elif self.state == "speaking":
                if self.barge.available and self.barge.feed(frame):
                    self.stop_speaking()
                    await _maybe_await(self.on_interrupt())
                    self.state, cap, silence, had_speech, cap_frames = "capture", bytearray(), 0, False, 0

            await asyncio.sleep(0)

    def stop(self):
        self._running = False
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass


def _is_voiced(barge: BargeInMonitor, frame_bytes: bytes) -> bool:
    if not barge.available:
        return len(frame_bytes) > 0  # without VAD, assume voiced so capture still ends on max
    try:
        return barge.vad.is_speech(frame_bytes, barge.sample_rate)
    except Exception:
        return False
