"""
voice/stt.py — speech-to-text (requirements 3, 12).

faster-whisper (CTranslate2) runs Whisper fully offline with high accuracy. Model size is
chosen from available RAM; language is auto-detected (feeds requirement 2's auto-switch).
Input is float32 PCM mono 16 kHz in [-1, 1].
"""
from __future__ import annotations

from typing import Optional, Tuple


def _auto_model() -> str:
    try:
        import psutil  # type: ignore
        ram = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        ram = 8.0
    if ram >= 24:
        return "large-v3"
    if ram >= 12:
        return "medium"
    if ram >= 8:
        return "small"
    return "base"


class Transcriber:
    def __init__(self, model: str = "auto", language: str = "auto",
                 compute_type: str = "auto"):
        self.language = None if language == "auto" else language
        self.model = None
        self.available = False
        name = _auto_model() if model == "auto" else model
        try:
            from faster_whisper import WhisperModel  # type: ignore
            ctype = "int8" if compute_type == "auto" else compute_type
            # try GPU first, fall back to CPU int8
            try:
                self.model = WhisperModel(name, device="cuda", compute_type="float16")
            except Exception:
                self.model = WhisperModel(name, device="cpu", compute_type=ctype)
            self.model_name = name
            self.available = True
        except Exception:
            self.available = False

    def transcribe(self, audio_float32, sample_rate: int = 16000) -> Tuple[str, str]:
        """Return (text, language_code)."""
        if not self.available:
            return "", "en"
        try:
            segments, info = self.model.transcribe(
                audio_float32, language=self.language, vad_filter=True, beam_size=5)
            text = " ".join(s.text.strip() for s in segments).strip()
            lang = getattr(info, "language", "en") or "en"
            return text, ("ko" if lang == "ko" else "en")
        except Exception:
            return "", "en"
