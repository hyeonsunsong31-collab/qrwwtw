"""
voice/wakeword.py -- "wake up Jarvis" detection (requirement 7) and voice-interrupt keywords.

Two engines behind one interface:  process(frame_bytes) -> (text, is_final) | None

* VoskPhraseDetector (default): Vosk's small English model in GRAMMAR mode, restricted to
  the words of ["wake up jarvis", "jarvis", "stop", "wait", "hold on", "[unk]"]. Because the
  recognizer can only output those words, the exact wake phrase is spotted reliably without
  training a custom model, and the same recognizer hears "stop" / "wait" for interruption.
* OpenWakeWordDetector: openWakeWord's neural detector on onnxruntime (tflite is not
  available on Windows). Uses models/wake_up_jarvis.onnx if you trained one, otherwise the
  bundled "hey jarvis" model. It knows only the wake phrase (no stop/wait keywords).

Audio: 16 kHz mono int16, 20 ms frames (640 bytes).
"""
from __future__ import annotations

import json
import os
from typing import Optional, Tuple

Result = Optional[Tuple[str, bool]]
WAKE_PHRASE = "wake up jarvis"


class VoskPhraseDetector:
    GRAMMAR = ["wake up jarvis", "jarvis", "stop", "wait", "hold on", "[unk]"]
    kind = "vosk"
    knows_keywords = True

    def __init__(self, model_dir: str, sample_rate: int = 16000):
        self.available = False
        self._last_partial = ""
        try:
            import vosk  # type: ignore
            vosk.SetLogLevel(-1)
            if not os.path.isdir(model_dir):
                return
            self._model = vosk.Model(model_dir)
            self._rec = vosk.KaldiRecognizer(self._model, sample_rate, json.dumps(self.GRAMMAR))
            self.available = True
        except Exception:
            self.available = False

    def process(self, frame: bytes) -> Result:
        if not self.available:
            return None
        try:
            if self._rec.AcceptWaveform(frame):
                text = json.loads(self._rec.Result()).get("text", "").strip()
                self._last_partial = ""
                return (text, True) if text else None
            partial = json.loads(self._rec.PartialResult()).get("partial", "").strip()
            if partial and partial != self._last_partial:
                self._last_partial = partial
                return (partial, False)
        except Exception:
            pass
        return None

    def reset(self):
        if self.available:
            try:
                self._rec.Reset()
            except Exception:
                pass
        self._last_partial = ""


class OpenWakeWordDetector:
    kind = "openwakeword"
    knows_keywords = False
    CHUNK = 1280 * 2   # 80 ms of int16 audio, what openWakeWord expects

    def __init__(self, model_path: str, sensitivity: float = 0.6):
        self.available = False
        self.sensitivity = sensitivity
        self._buf = bytearray()
        self._key = None
        try:
            from openwakeword.model import Model  # type: ignore
            import numpy as np  # type: ignore
            self._np = np
            if os.path.exists(model_path):
                self._model = Model(wakeword_models=[model_path], inference_framework="onnx")
            else:
                try:
                    from openwakeword import utils  # type: ignore
                    utils.download_models(model_names=["hey_jarvis"])
                except Exception:
                    pass
                self._model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            self._key = next(iter(self._model.models.keys()), None)
            self.available = self._key is not None
        except Exception:
            self.available = False

    def process(self, frame: bytes) -> Result:
        if not self.available:
            return None
        self._buf += frame
        if len(self._buf) < self.CHUNK:
            return None
        chunk = self._np.frombuffer(bytes(self._buf[:self.CHUNK]), dtype=self._np.int16)
        del self._buf[:self.CHUNK]
        try:
            score = float(self._model.predict(chunk).get(self._key, 0.0))
        except Exception:
            return None
        if score >= self.sensitivity:
            self.reset()
            return (WAKE_PHRASE, True)
        return None

    def reset(self):
        self._buf.clear()
        if self.available:
            try:
                self._model.reset()
            except Exception:
                pass


def make_detector(config: dict, root: str):
    """Build the configured detector, falling back to the other engine if needed."""
    ww = config.get("voice", {}).get("wakeword", {})
    vosk_dir = os.path.join(root, ww.get("vosk_model", "models/vosk-small-en"))
    oww_path = os.path.join(root, ww.get("openwakeword_model", "models/wake_up_jarvis.onnx"))
    sens = float(ww.get("sensitivity", 0.6))
    order = ["vosk", "openwakeword"] if ww.get("engine", "vosk") == "vosk" else ["openwakeword", "vosk"]
    for kind in order:
        det = VoskPhraseDetector(vosk_dir) if kind == "vosk" else OpenWakeWordDetector(oww_path, sens)
        if det.available:
            return det
    return None


def heard_wake(text: str, lenient: bool = False) -> bool:
    t = " " + (text or "").lower() + " "
    if " wake up jarvis " in t:
        return True
    return lenient and " jarvis " in t


def heard_keyword(text: str, keywords, spoken_now: str = "") -> Optional[str]:
    """Return the interrupt keyword the user said, ignoring words JARVIS itself is saying
    right now (so its own voice through the speakers can't interrupt it)."""
    t = " " + (text or "").lower() + " "
    own = " " + (spoken_now or "").lower() + " "
    for kw in keywords:
        k = f" {kw.lower()} "
        if k in t and k not in own:
            return kw
    return None
