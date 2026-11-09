"""
voice/tts.py -- text-to-speech in a British gentleman's voice (requirements 3, 8, 21).

Voice selection, per language:
  English: Piper `en_GB-alan-medium` (offline neural British male, prebuilt Windows wheel)
           -> Windows OneCore "George" (British male) via WinRT -> SAPI5 (prefers en-GB)
  Korean:  a Korean Piper voice if one is configured (none official today)
           -> Windows OneCore Korean voice (e.g. "Heami") via WinRT -> SAPI5 Korean voice
The installer adds the en-GB and ko-KR Windows speech voices, so the WinRT path has them.

Speech plays sentence by sentence and stop() cuts it off instantly (voice interruption,
requirement 13).
"""
from __future__ import annotations

import io
import os
import threading
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Speaker:
    def __init__(self, voice_en: str = "en_GB-alan-medium", voice_ko: str = "",
                 rate: float = 1.0, root: str = ROOT):
        self.root = root
        self.rate = rate
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._piper = {}          # lang -> PiperVoice
        try:
            import numpy as np  # type: ignore
            import sounddevice as sd  # type: ignore
            self._np, self._sd = np, sd
        except Exception:
            self._np = self._sd = None
        if self._sd is not None:
            self._init_piper({"en": voice_en, "ko": voice_ko})
        self._winrt_ok = self._sd is not None and self._probe_winrt()
        self._sapi_ok = self._probe_sapi()
        self.available = bool(self._piper) or self._winrt_ok or self._sapi_ok

    # ---------------------------------------------------------- backends #
    def _init_piper(self, voices: dict):
        try:
            from piper import PiperVoice  # type: ignore
        except Exception:
            return
        for lang, name in voices.items():
            path = os.path.join(self.root, "voices", (name or "") + ".onnx")
            if name and os.path.exists(path):
                try:
                    self._piper[lang] = PiperVoice.load(path)
                except Exception:
                    pass

    @staticmethod
    def _probe_winrt() -> bool:
        try:
            from winrt.windows.media.speechsynthesis import SpeechSynthesizer  # type: ignore  # noqa: F401
            from winrt.windows.storage.streams import DataReader  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _probe_sapi() -> bool:
        try:
            import win32com.client  # type: ignore  # noqa: F401
            import pythoncom  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def backend(self) -> str:
        return ("piper" if self._piper else "winrt" if self._winrt_ok
                else "sapi5" if self._sapi_ok else "none")

    # ------------------------------------------------------------- speak #
    def speak(self, text: str, lang: str = "en"):
        """Blocking synthesis + playback; returns when finished or stopped."""
        text = (text or "").strip()
        if not text or not self.available:
            return
        self._stop.clear()
        with self._lock:
            voice = self._piper.get(lang) or (self._piper.get("en") if lang != "ko" else None)
            if voice is not None:
                self._speak_piper(voice, text)
            elif self._winrt_ok and self._speak_winrt(text, lang):
                pass
            elif self._sapi_ok:
                self._speak_sapi(text, lang)

    def _play(self, audio, sample_rate: int):
        if self._stop.is_set():
            return
        self._sd.play(audio, sample_rate)
        self._sd.wait()

    def _speak_piper(self, voice, text: str):
        try:
            from piper import SynthesisConfig  # type: ignore
            cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, self.rate)))
            for chunk in voice.synthesize(text, syn_config=cfg):
                if self._stop.is_set():
                    break
                self._play(self._np.frombuffer(chunk.audio_int16_bytes, dtype=self._np.int16),
                           chunk.sample_rate)
        except Exception:
            pass

    # --- Windows OneCore voices through WinRT -------------------------------------
    def _speak_winrt(self, text: str, lang: str) -> bool:
        try:
            wav = self._winrt_synthesize(text, lang)
            if not wav:
                return False
            with wave.open(io.BytesIO(wav)) as w:
                sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if width != 2:
                return False
            audio = self._np.frombuffer(frames, dtype=self._np.int16)
            if ch > 1:
                audio = audio.reshape(-1, ch)
            self._play(audio, sr)
            return True
        except Exception:
            return False

    def _winrt_synthesize(self, text: str, lang: str) -> bytes:
        import asyncio
        try:
            from winrt.runtime import init_apartment, MTA  # type: ignore
            init_apartment(MTA)
        except Exception:
            pass                      # already initialised on this thread
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer  # type: ignore
        from winrt.windows.storage.streams import DataReader  # type: ignore

        async def run() -> bytes:
            synth = SpeechSynthesizer()
            voice = self._pick_winrt_voice(SpeechSynthesizer, lang)
            if voice is None:
                return b""
            synth.voice = voice
            stream = await synth.synthesize_text_to_stream_async(text)
            size = int(stream.size)
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)

        return asyncio.run(run())

    @staticmethod
    def _pick_winrt_voice(SpeechSynthesizer, lang: str):
        voices = SpeechSynthesizer.all_voices
        voices = list(voices() if callable(voices) else voices)
        want = "ko" if lang == "ko" else "en-gb"
        cands = [v for v in voices if v.language.lower().startswith(want)]
        if lang != "ko":
            if not cands:
                cands = [v for v in voices if v.language.lower().startswith("en")]
            male = [v for v in cands if "george" in v.display_name.lower() or int(v.gender) == 0]
            cands = male or cands
        return cands[0] if cands else None

    # --- classic SAPI5 voices -------------------------------------------------------
    def _speak_sapi(self, text: str, lang: str):
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except Exception:
            return
        pythoncom.CoInitialize()
        try:
            v = win32com.client.Dispatch("SAPI.SpVoice")
            tok = self._pick_sapi_voice(v, lang)
            if tok is None and lang == "ko":
                return                                 # no Korean voice installed: stay silent
            if tok is not None:
                v.Voice = tok
            v.Rate = int(max(-10, min(10, round((self.rate - 1.0) * 10))))
            v.Speak(text, 1)                           # SVSFlagsAsync
            while not v.WaitUntilDone(100):
                if self._stop.is_set():
                    v.Speak("", 3)                     # async + purge: stop immediately
                    break
        except Exception:
            pass
        finally:
            pythoncom.CoUninitialize()

    @staticmethod
    def _pick_sapi_voice(v, lang: str):
        prefs = (["korean", "heami", "injoon"] if lang == "ko"
                 else ["george", "united kingdom", "great britain", "hazel", "english"])
        best, best_rank = None, len(prefs)
        try:
            toks = v.GetVoices()
            for i in range(toks.Count):
                tok = toks.Item(i)
                desc = tok.GetDescription().lower()
                for rank, word in enumerate(prefs):
                    if word in desc and rank < best_rank:
                        best, best_rank = tok, rank
        except Exception:
            return None
        return best

    def stop(self):
        """Stop speaking immediately (voice interruption)."""
        self._stop.set()
        try:
            if self._sd is not None:
                self._sd.stop()
        except Exception:
            pass
