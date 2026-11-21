"""
voice/stt.py -- speech-to-text (requirements 3, 12).

faster-whisper (CTranslate2) runs Whisper fully offline. With an NVIDIA GPU it uses
large-v3-turbo (fast and very accurate); on CPU it uses `small` with int8 weights, which
keeps a short command to roughly a second. Language is auto-detected (feeding the
automatic language switch), and "Jarvis" is passed as a hotword so the name is recognised.
Input: float32 PCM, mono, 16 kHz, in [-1, 1].

SPEED (v1.4): commands are short, so on CPU decoding is greedy (beam 1) with a single
fallback temperature, no timestamps and no conditioning on earlier text; the CPU thread
count follows the physical cores; and warm_up() runs once at start-up so the user's first
command does not pay for initialising CTranslate2 and the Silero VAD.
"""
from __future__ import annotations

import os
from typing import Tuple


def cuda_available() -> bool:
    try:
        import ctranslate2  # type: ignore
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def auto_model(cuda: bool) -> str:
    if cuda:
        return "large-v3-turbo"
    try:
        import psutil  # type: ignore
        ram = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        ram = 8.0
    return "small" if ram >= 8 else "base"


def cpu_threads() -> int:
    try:
        import psutil  # type: ignore
        n = psutil.cpu_count(logical=False) or os.cpu_count() or 4
    except Exception:
        n = os.cpu_count() or 4
    return max(2, min(8, int(n)))


class Transcriber:
    def __init__(self, model: str = "auto", language: str = "auto"):
        self.language = None if language == "auto" else language
        self.model = None
        self.available = False
        self.cuda = cuda_available()
        self.model_name = auto_model(self.cuda) if model == "auto" else model
        try:
            from faster_whisper import WhisperModel  # type: ignore
            if self.cuda:
                try:
                    self.model = WhisperModel(self.model_name, device="cuda", compute_type="float16")
                except Exception:
                    self.cuda = False
            if self.model is None:
                if model == "auto" and self.model_name.startswith("large"):
                    self.model_name = auto_model(False)
                self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                          cpu_threads=cpu_threads())
            self.available = True
        except Exception:
            self.available = False

    def options(self) -> dict:
        """Decoding options (all valid for faster-whisper 1.2.1; tests check the signature)."""
        return {"language": self.language, "vad_filter": True, "hotwords": "Jarvis",
                "beam_size": 3 if self.cuda else 1, "best_of": 1,
                "temperature": (0.0, 0.4), "condition_on_previous_text": False,
                "without_timestamps": True}

    def warm_up(self) -> bool:
        """Run the models once on a little noise, so the first real command is fast."""
        if not self.available:
            return False
        try:
            import numpy as np  # type: ignore
            noise = (np.random.default_rng(0).standard_normal(16000) * 0.003).astype(np.float32)
            for vad in (False, True):                  # the decoder, then the VAD model
                segments, _ = self.model.transcribe(noise, language="en", beam_size=1, vad_filter=vad,
                                                    without_timestamps=True, condition_on_previous_text=False)
                list(segments)                         # transcription runs while iterating
            return True
        except Exception:
            return False

    def transcribe(self, audio_float32, sample_rate: int = 16000) -> Tuple[str, str]:
        """Return (text, 'en' | 'ko')."""
        if not self.available:
            return "", "en"
        try:
            segments, info = self.model.transcribe(audio_float32, **self.options())
            text = " ".join(s.text.strip() for s in segments).strip()
            lang = getattr(info, "language", "en") or "en"
            return text, ("ko" if lang == "ko" else "en")
        except Exception:
            return "", "en"
