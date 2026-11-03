"""
voice/tts.py — text-to-speech in a British gentleman's voice (requirements 3, 8, 21).

Primary: Piper (offline neural TTS). The signature English voice is `en_GB-alan-medium`
(a British male). Korean input is spoken with a Korean Piper voice (requirement 2's
auto-switch); the English butler voice remains the default/signature.
Fallback (Windows): SAPI5 via pyttsx3, preferring the British male "George".

Playback goes through sounddevice so it can be stopped instantly for barge-in
(requirement 13). stop() halts the current utterance.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

VOICE_DIR = "voices"


class Speaker:
    def __init__(self, voice_en: str = "en_GB-alan-medium",
                 voice_ko: str = "ko_KR-glow", rate: float = 1.0):
        self.voice_en = voice_en
        self.voice_ko = voice_ko
        self.rate = rate
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.backend = None
        self._piper = {}       # lang -> PiperVoice
        self._sd = None
        self._sr = 22050
        self.available = False
        self._init_piper() or self._init_sapi()

    # ---------------------------------------------------------- backends #
    def _voice_path(self, voice: str) -> Optional[str]:
        p = os.path.join(VOICE_DIR, voice + ".onnx")
        return p if os.path.exists(p) else None

    def _init_piper(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore
            from piper.voice import PiperVoice  # type: ignore
        except Exception:
            return False
        loaded = {}
        for lang, voice in (("en", self.voice_en), ("ko", self.voice_ko)):
            vp = self._voice_path(voice)
            if vp:
                try:
                    loaded[lang] = PiperVoice.load(vp)
                except Exception:
                    pass
        if not loaded:
            return False  # voices not downloaded yet
        self._piper = loaded
        self._sd = sd
        self.backend = "piper"
        self.available = True
        return True

    def _init_sapi(self) -> bool:
        try:
            import pyttsx3  # type: ignore
        except Exception:
            return False
        try:
            eng = pyttsx3.init()
            # prefer a British male voice (e.g. "George")
            for v in eng.getProperty("voices"):
                name = (v.name or "").lower()
                if "george" in name or ("english (great britain)" in name and "male" in name) \
                        or "hazel" in name:
                    eng.setProperty("voice", v.id)
                    break
            self._sapi = eng
            self.backend = "sapi5"
            self.available = True
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- speak #
    def speak(self, text: str, lang: str = "en"):
        """Blocking synth+playback; returns when done or stopped."""
        text = (text or "").strip()
        if not text or not self.available:
            return
        self._stop.clear()
        with self._lock:
            if self.backend == "piper":
                self._speak_piper(text, lang)
            elif self.backend == "sapi5":
                self._speak_sapi(text)

    def _speak_piper(self, text: str, lang: str):
        import numpy as np
        voice = self._piper.get(lang) or self._piper.get("en") or next(iter(self._piper.values()))
        sr = getattr(getattr(voice, "config", None), "sample_rate", self._sr)
        try:
            self._sd.stop()
            for chunk in voice.synthesize_stream_raw(text):
                if self._stop.is_set():
                    break
                audio = np.frombuffer(chunk, dtype=np.int16)
                self._sd.play(audio, sr, blocking=True)
        except Exception:
            pass

    def _speak_sapi(self, text: str):
        try:
            self._sapi.say(text)
            self._sapi.runAndWait()
        except Exception:
            pass

    def stop(self):
        """Stop speaking immediately (barge-in)."""
        self._stop.set()
        try:
            if self.backend == "piper" and self._sd:
                self._sd.stop()
            elif self.backend == "sapi5":
                self._sapi.stop()
        except Exception:
            pass
