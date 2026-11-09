"""
voice/interrupt.py -- voice-activity detection for barge-in and end-of-speech (requirement 13).

WebRTC VAD on 20 ms int16 frames (webrtcvad-wheels provides prebuilt Windows wheels; the
import name is `webrtcvad`). Used two ways:
  * capture: detect when the user has finished speaking a command,
  * barge-in "vad" mode (headsets): fire after ~400 ms of continuous speech while JARVIS talks.
"""
from __future__ import annotations


class BargeInMonitor:
    def __init__(self, aggressiveness: int = 3, sample_rate: int = 16000, trigger_ms: int = 400):
        self.sample_rate = sample_rate
        self.frame_ms = 20
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

    def is_speech(self, frame: bytes) -> bool:
        if not self.available:
            return False
        try:
            return bool(self.vad.is_speech(frame, self.sample_rate))
        except Exception:
            return False

    def reset(self):
        self._voiced = 0

    def feed(self, frame: bytes) -> bool:
        """Feed one 20 ms frame; True once enough continuous speech has been heard."""
        if self.is_speech(frame):
            self._voiced += 1
        else:
            self._voiced = max(0, self._voiced - 2)
        if self._voiced >= self.trigger_frames:
            self._voiced = 0
            return True
        return False
