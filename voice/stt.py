"""
voice/stt.py -- speech-to-text (requirements 3, 12).

faster-whisper (CTranslate2) runs Whisper fully offline. With an NVIDIA GPU it uses
large-v3-turbo (fast and very accurate); on CPU it uses `small` with int8 weights, which
keeps a short command to roughly a second. Language is auto-detected (feeding the
automatic language switch), and "Jarvis" is passed as a hotword so the name is recognised.
Input: float32 PCM, mono, 16 kHz, in [-1, 1].
"""
from __future__ import annotations

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
                self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            self.available = True
        except Exception:
            self.available = False

    def transcribe(self, audio_float32, sample_rate: int = 16000) -> Tuple[str, str]:
        """Return (text, 'en' | 'ko')."""
        if not self.available:
            return "", "en"
        try:
            segments, info = self.model.transcribe(
                audio_float32, language=self.language, vad_filter=True,
                beam_size=5 if self.cuda else 3, hotwords="Jarvis")
            text = " ".join(s.text.strip() for s in segments).strip()
            lang = getattr(info, "language", "en") or "en"
            return text, ("ko" if lang == "ko" else "en")
        except Exception:
            return "", "en"
