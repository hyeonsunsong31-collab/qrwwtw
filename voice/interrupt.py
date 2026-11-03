"""
voice/interrupt.py — barge-in detector (requirement 13).

While JARVIS is speaking, this watches the microphone with WebRTC VAD. If the user starts
talking (voiced frames for >~250 ms), it fires `on_interrupt`, which stops TTS and starts
capturing the new command — so the user can cut JARVIS off and say something new.
"""
from __future__ import annotations

from typing import Callable


class BargeInMonitor:
    def __init__(self, aggressiveness: int = 2, sample_rate: int = 16000,
                 trigger_ms: int = 250):
        self.sample_rate = sample_rate
        self.frame_ms = 20
        self.frame_len = int(sample_rate * self.frame_ms / 1000)  # samples per 20 ms
        self.trigger_frames = max(1, trigger_ms // self.frame_ms)
        self._voiced = 0
        self.vad = None
        self.available = False
        try:
            import webrtcvad  # type: ignore
            self.vad = webrtcvad.Vad(aggressiveness)
            self.available = True
        except Exception:
            self.available = False

    def reset(self):
        self._voiced = 0

    def feed(self, frame_int16_bytes: bytes) -> bool:
        """Feed one 20 ms int16 frame (bytes). Return True when barge-in is detected."""
        if not self.available:
            return False
        try:
            if self.vad.is_speech(frame_int16_bytes, self.sample_rate):
                self._voiced += 1
            else:
                self._voiced = max(0, self._voiced - 1)
        except Exception:
            return False
        if self._voiced >= self.trigger_frames:
            self._voiced = 0
            return True
        return False
