"""
voice/wakeword.py — "wake up Jarvis" detector (requirement 7).

Uses openWakeWord (Apache-2.0, fully offline, custom phrases). The installer places a
trained model at models/wake_up_jarvis.onnx; until then a bundled generic model or the
energy fallback is used. Audio is 16 kHz mono int16, 80 ms frames (1280 samples).

Train your own phrase model (optional, improves accuracy):
    pip install openwakeword[training]
    python -m openwakeword.train --phrase "wake up jarvis" --out models/wake_up_jarvis.onnx
"""
from __future__ import annotations

import os
from typing import Optional

FRAME = 1280  # 80 ms @ 16 kHz


class WakeWordDetector:
    def __init__(self, phrase: str = "wake up jarvis",
                 model_path: str = "models/wake_up_jarvis.onnx",
                 sensitivity: float = 0.6):
        self.phrase = phrase
        self.sensitivity = sensitivity
        self.model = None
        self.available = False
        self._key = None
        try:
            from openwakeword.model import Model  # type: ignore
            if os.path.exists(model_path):
                self.model = Model(wakeword_models=[model_path])
                self._key = os.path.splitext(os.path.basename(model_path))[0]
            else:
                # fall back to openWakeWord's pre-shipped models (e.g. "hey jarvis")
                self.model = Model()
                # pick the closest bundled key if present
                for k in self.model.models.keys():
                    if "jarvis" in k.lower():
                        self._key = k
                        break
                self._key = self._key or next(iter(self.model.models.keys()), None)
            self.available = self.model is not None and self._key is not None
        except Exception:
            self.available = False

    def predict(self, frame_int16) -> float:
        """Return wake score in [0,1] for one 80 ms int16 frame."""
        if not self.available:
            return 0.0
        try:
            scores = self.model.predict(frame_int16)
            return float(scores.get(self._key, 0.0))
        except Exception:
            return 0.0

    def triggered(self, frame_int16) -> bool:
        return self.predict(frame_int16) >= self.sensitivity

    def reset(self):
        if self.available:
            try:
                self.model.reset()
            except Exception:
                pass
