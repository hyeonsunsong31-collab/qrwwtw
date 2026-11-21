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

SPEED (v1.4): synthesis and playback are separate steps -- prepare() turns text into audio,
play_prepared() plays it -- so the voice engine synthesises the next sentence while the
current one is playing (no gap between sentences). Short phrases are cached, and the
frequent ones (the wake reply, "Right away, sir.") are synthesised at start-up, so they
play the instant they are needed. The Windows voice and synthesizer are created once.
"""
from __future__ import annotations

import io
import os
import threading
import wave
from collections import OrderedDict

CACHE_MAX_TEXT = 80          # phrases up to this length are cached once synthesised
CACHE_ENTRIES = 48

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Speaker:
    def __init__(self, voice_en: str = "en_GB-alan-medium", voice_ko: str = "",
                 rate: float = 1.0, root: str = ROOT):
        self.root = root
        self.rate = rate
        self._stop = threading.Event()
        self._lock = threading.Lock()         # synthesis
        self._piper = {}          # lang -> PiperVoice
        self._cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._cache_lock = threading.Lock()   # prepare() runs on two threads while pipelining
        self._winrt_synth = {}    # lang -> SpeechSynthesizer with its voice set
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
        self._stop.clear()
        self.play_prepared(self.prepare(text, lang))

    def prepare(self, text: str, lang: str = "en"):
        """Text -> {"audio", "sr"} ready to play (or {"direct": True} for SAPI, which speaks
        directly). Cached for short phrases. None when there is nothing to say."""
        text = (text or "").strip()
        if not text or not self.available:
            return None
        key = (lang, text, round(float(self.rate), 2), self.backend)
        if len(text) <= CACHE_MAX_TEXT:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit is not None:
                    self._cache.move_to_end(key)
                    return hit
        prep = self._synthesize(text, lang)
        if prep is not None and prep.get("audio") is not None and len(text) <= CACHE_MAX_TEXT:
            with self._cache_lock:
                self._cache[key] = prep
                while len(self._cache) > CACHE_ENTRIES:
                    self._cache.popitem(last=False)
        return prep

    def prewarm(self, phrases) -> int:
        """Synthesise frequent phrases ahead of time (and warm the voice up). Returns how many."""
        n = 0
        for text, lang in phrases:
            try:
                if self.prepare(text, lang) is not None:
                    n += 1
            except Exception:
                pass
        return n

    def reset_stop(self):
        self._stop.clear()

    def play_prepared(self, prep):
        """Play what prepare() returned; returns when finished or stopped."""
        if not prep:
            return
        if prep.get("direct"):
            self._speak_sapi(prep["text"], prep["lang"])
        else:
            self._play(prep["audio"], prep["sr"])

    def _synthesize(self, text: str, lang: str):
        with self._lock:
            voice = self._piper.get(lang) or (self._piper.get("en") if lang != "ko" else None)
            if voice is not None:
                audio, sr = self._piper_audio(voice, text)
                if audio is not None:
                    return {"text": text, "lang": lang, "audio": audio, "sr": sr}
            if self._winrt_ok:
                audio, sr = self._winrt_audio(text, lang)
                if audio is not None:
                    return {"text": text, "lang": lang, "audio": audio, "sr": sr}
            if self._sapi_ok:
                return {"text": text, "lang": lang, "direct": True}
        return None

    def _play(self, audio, sample_rate: int):
        if self._stop.is_set():
            return
        self._sd.play(audio, sample_rate)
        self._sd.wait()

    def _piper_audio(self, voice, text: str):
        try:
            from piper import SynthesisConfig  # type: ignore
            cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, self.rate)))
            parts, sr = [], None
            for chunk in voice.synthesize(text, syn_config=cfg):
                parts.append(self._np.frombuffer(chunk.audio_int16_bytes, dtype=self._np.int16))
                sr = chunk.sample_rate
            if not parts:
                return None, None
            return (parts[0] if len(parts) == 1 else self._np.concatenate(parts)), sr
        except Exception:
            return None, None

    # --- Windows OneCore voices through WinRT -------------------------------------
    def _winrt_audio(self, text: str, lang: str):
        try:
            wav = self._winrt_synthesize(text, lang)
            if not wav:
                return None, None
            with wave.open(io.BytesIO(wav)) as w:
                sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if width != 2:
                return None, None
            audio = self._np.frombuffer(frames, dtype=self._np.int16)
            if ch > 1:
                audio = audio.reshape(-1, ch)
            return audio, sr
        except Exception:
            return None, None

    def _winrt_synthesize(self, text: str, lang: str) -> bytes:
        import asyncio
        try:
            from winrt.runtime import init_apartment, MTA  # type: ignore
            init_apartment(MTA)
        except Exception:
            pass                      # already initialised on this thread
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer  # type: ignore
        from winrt.windows.storage.streams import DataReader  # type: ignore

        async def run(fresh: bool) -> bytes:
            synth = None if fresh else self._winrt_synth.get(lang)
            if synth is None:                          # created once per language
                voice = self._pick_winrt_voice(SpeechSynthesizer, lang)
                if voice is None:
                    return b""
                synth = SpeechSynthesizer()
                synth.voice = voice
                self._winrt_synth[lang] = synth
            stream = await synth.synthesize_text_to_stream_async(text)
            size = int(stream.size)
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)

        try:
            return asyncio.run(run(False))
        except Exception:
            self._winrt_synth.pop(lang, None)          # a stale cached object: start over once
            return asyncio.run(run(True))

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
