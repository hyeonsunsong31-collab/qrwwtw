"""
voice/stt.py -- speech-to-text (requirements 3, 12).

faster-whisper (CTranslate2) runs Whisper fully offline. With a usable NVIDIA GPU it uses
large-v3-turbo (fast and very accurate); on CPU it uses `small` with int8 weights, which
keeps a short command to roughly a second. Language is auto-detected (feeding the
automatic language switch), and "Jarvis" is passed as a hotword so the name is recognised.
Input: float32 PCM, mono, 16 kHz, in [-1, 1].

SPEED (v1.4): commands are short, so on CPU decoding is greedy (beam 1) with a single
fallback temperature, no timestamps and no conditioning on earlier text; the CPU thread
count follows the physical cores; and warm_up() runs once at start-up so the user's first
command does not pay for initialising CTranslate2 and the Silero VAD.

MODELS (v1.4.2): the Whisper model is kept in the JARVIS folder (models/whisper). Loading
tries that folder, then the user's Hugging Face cache, then downloads into the folder, so an
installer run under another Windows account, or a cleaned cache, no longer leaves JARVIS deaf.
load() records what happened (state, error, source) for the voice report, and runs in the
background: the wake word works while Whisper is still loading.
A GPU is used only after a one-off test in a separate process: with the NVIDIA driver but
without NVIDIA's cuBLAS / cuDNN libraries, CTranslate2 can abort the whole process on its
first transcription, which would take the brain down with it.

ACCURACY (v1.4.2): language detection is limited to English and Korean (a short accented
command could otherwise be "detected" as Welsh or Malay and come out garbled), segments that
Whisper itself marks as probably-not-speech are dropped, and the classic hallucinations on
noise ("Thank you.", "Thanks for watching!") are ignored when the audio is weak evidence.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# what Whisper "hears" in silence or noise (normalised: lower-case, no punctuation)
HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks", "thanks for watching", "thank you for watching",
    "thanks for watching and see you next time", "please subscribe", "subscribe", "you", "bye",
    "bye bye", "so", "the end", "uh", "um", "hmm", "oh", "okay",
    "시청해주셔서 감사합니다", "시청해 주셔서 감사합니다", "구독과 좋아요 부탁드립니다",
    "감사합니다", "mbc 뉴스", "끝", "네",
}


def normalise(text: str) -> str:
    t = re.sub(r"[^\w\s가-힣']+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


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


def explain_error(e: Optional[BaseException]) -> str:
    """Plain-English reason a model failed to load or run."""
    if e is None:
        return "unknown error"
    msg = f"{type(e).__name__}: {e}"
    low = msg.lower()
    if "dll load failed" in low or "winerror 126" in low or "vcruntime" in low or "msvcp" in low:
        return ("a Windows system library is missing - install the Microsoft Visual C++ "
                "Redistributable (x64) and run INSTALL_JARVIS.bat again (" + msg[:160] + ")")
    if "no module named" in low:
        return "the speech-recognition package is not installed - run INSTALL_JARVIS.bat (" + msg[:120] + ")"
    if any(k in low for k in ("connection", "timed out", "max retries", "name resolution", "ssl",
                              "offline", "localentrynotfound", "cannot find the requested files")):
        return ("the speech model is not on this PC and could not be downloaded (no internet?) - "
                "connect once and restart JARVIS, or run INSTALL_JARVIS.bat (" + msg[:120] + ")")
    if "memory" in low or "bad_alloc" in low:
        return "not enough memory for the speech model (" + msg[:120] + ")"
    if "cublas" in low or "cudnn" in low or "cuda" in low:
        return "the graphics card's CUDA libraries are incomplete (" + msg[:160] + ")"
    return msg[:240]


def _missing(e: BaseException) -> bool:
    low = f"{type(e).__name__}: {e}".lower()
    return any(k in low for k in ("localentrynotfound", "cannot find the requested files", "outgoing traffic",
                                  "not found", "no such file", "does not appear to have", "local_files_only"))


_CUDA_PROBE = r"""
import sys, numpy as np
from faster_whisper import WhisperModel
m = WhisperModel(sys.argv[1], device="cuda", compute_type="float16",
                 download_root=sys.argv[2] or None, local_files_only=True)
segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1,
                       without_timestamps=True)
list(segs)
print("CUDA-OK")
"""


def cuda_usable(model_name: str, download_root: Optional[str], cache_file: Optional[str] = None,
                timeout: float = 180.0) -> Tuple[bool, str]:
    """Load and run the model on the GPU in a separate process. Returns (usable, detail);
    the verdict is remembered in cache_file so the test runs once per model and library."""
    try:
        import ctranslate2  # type: ignore
        key = f"{model_name}|{getattr(ctranslate2, '__version__', '?')}|{download_root or ''}"
    except Exception as e:
        return False, f"ctranslate2 unavailable ({e})"
    if cache_file:
        try:
            with open(cache_file, encoding="utf-8") as fh:
                seen = json.load(fh)
            if seen.get("key") == key:
                return bool(seen.get("ok")), str(seen.get("detail", ""))
        except (OSError, ValueError):
            pass
    flags = 0x08000000 if sys.platform.startswith("win") else 0          # CREATE_NO_WINDOW
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    try:
        r = subprocess.run([exe, "-c", _CUDA_PROBE, model_name, download_root or ""],
                           capture_output=True, text=True, timeout=timeout, creationflags=flags)
        ok = r.returncode == 0 and "CUDA-OK" in (r.stdout or "")
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-2:]
        detail = "ok" if ok else f"exit code {r.returncode}: " + " | ".join(tail)[:300]
    except subprocess.TimeoutExpired:
        ok, detail = False, "timed out"
    except Exception as e:
        ok, detail = False, repr(e)[:200]
    if cache_file and "not found" not in detail.lower() and "localentrynotfound" not in detail.lower():
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "ok": ok, "detail": detail, "at": time.time()}, fh)
        except OSError:
            pass
    return ok, detail


class Transcriber:
    def __init__(self, model: str = "auto", language: str = "auto", root: str = ROOT,
                 lazy: bool = False, log=None, data_dir: Optional[str] = None):
        self.language = None if language in (None, "", "auto") else language
        self.requested = model or "auto"
        self.root = root
        self.model = None
        self.available = False
        self.state = "idle"               # idle | loading | downloading | ready | failed
        self.error = ""
        self.source = ""                  # folder | cache | download
        self.cuda = False
        self.cuda_note = ""
        self.model_name = ""
        self.load_seconds = None
        self.last = {}                    # the last transcription's details
        self.download_root = os.path.join(root, "models", "whisper")
        self.data_dir = data_dir or os.environ.get("JARVIS_DATA") or os.path.join(root, "data")
        self.log = log or (lambda m: None)
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        if not lazy:
            self.load()

    # -------------------------------------------------------------- load #
    def load(self) -> bool:
        with self._load_lock:
            if self.state == "ready":
                return True
            self.state, t0 = "loading", time.time()
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as e:
                self.error, self.state = explain_error(e), "failed"
                self.log(f"speech recogniser unavailable: {self.error}")
                return False
            want_cuda = cuda_available()
            name = auto_model(want_cuda) if self.requested == "auto" else self.requested
            plans = []
            if want_cuda:
                plans.append(("cuda", "float16", name))
            cpu_name = auto_model(False) if (self.requested == "auto" and name.startswith("large")) else name
            plans.append(("cpu", "int8", cpu_name))
            last: Optional[BaseException] = None
            for device, ctype, model_name in plans:
                for source, root, local in (("folder", self.download_root, True),
                                            ("cache", None, True),
                                            ("download", self.download_root, False)):
                    try:
                        if source == "download":
                            os.makedirs(self.download_root, exist_ok=True)
                            self.state = "downloading"
                            self.log(f"downloading the speech model '{model_name}' into models/whisper "
                                     f"(first run only)")
                        kw = dict(device=device, compute_type=ctype, download_root=root,
                                  local_files_only=local)
                        if device == "cpu":
                            kw["cpu_threads"] = cpu_threads()
                        model = WhisperModel(model_name, **kw)
                        if device == "cuda":
                            ok, detail = cuda_usable(model_name, root, os.path.join(
                                self.data_dir, "voice", "cuda_probe.json"))
                            if not ok:
                                del model
                                self.cuda_note = f"GPU not used: {detail}"
                                self.log(f"speech recogniser: the GPU test failed ({detail}); using the CPU")
                                break                # next plan: the CPU
                        self.model, self.model_name, self.source = model, model_name, source
                        self.cuda = device == "cuda"
                        self.available, self.state, self.error = True, "ready", ""
                        self.load_seconds = round(time.time() - t0, 1)
                        self.log(f"speech recogniser: Whisper {model_name} on the {device.upper()} "
                                 f"({'from ' + source if source != 'download' else 'downloaded'}, "
                                 f"{self.load_seconds}s)")
                        return True
                    except Exception as e:
                        last = e
                        if source != "download" and _missing(e):
                            continue                 # not stored there: try the next place
                        if device == "cuda":
                            self.cuda_note = f"GPU not used: {explain_error(e)}"
                            break                    # the GPU itself failed: use the CPU
            self.model, self.available, self.state = None, False, "failed"
            self.error = explain_error(last)
            self.log(f"speech recogniser could not be loaded: {self.error}")
            return False

    def describe(self) -> dict:
        return {"state": self.state, "model": self.model_name or self.requested,
                "device": ("cuda" if self.cuda else "cpu") if self.available else None,
                "source": self.source or None, "error": self.error or None,
                "gpu_note": self.cuda_note or None, "load_seconds": self.load_seconds}

    # ---------------------------------------------------------- decoding #
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
            with self._run_lock:
                for vad in (False, True):              # the decoder, then the VAD model
                    segments, _ = self.model.transcribe(noise, language="en", beam_size=1, vad_filter=vad,
                                                        without_timestamps=True,
                                                        condition_on_previous_text=False)
                    list(segments)                     # transcription runs while iterating
            return True
        except Exception as e:
            self.error = explain_error(e)
            return False

    def _run(self, audio, language):
        opts = self.options()
        opts["language"] = language
        with self._run_lock:
            segments, info = self.model.transcribe(audio, **opts)
            return list(segments), info

    def transcribe_detail(self, audio_float32, sample_rate: int = 16000) -> dict:
        """Transcribe and say how sure it is. Returns {'text', 'lang', 'dropped', 'detected',
        'seconds', 'no_speech', 'logprob'}; dropped explains an empty text (None otherwise)."""
        out = {"text": "", "lang": "en", "dropped": None, "detected": None, "seconds": 0.0,
               "no_speech": None, "logprob": None}
        if not self.available:
            out["dropped"] = "unavailable"
            return out
        t0 = time.time()
        try:
            segs, info = self._run(audio_float32, self.language)
            lang = getattr(info, "language", None) or "en"
            out["detected"] = lang
            if self.language is None and lang not in ("en", "ko"):
                probs = dict(getattr(info, "all_language_probs", None) or [])
                lang = "ko" if probs.get("ko", 0.0) > probs.get("en", 0.0) else "en"
                segs, info = self._run(audio_float32, lang)
            kept = [s for s in segs
                    if not (getattr(s, "no_speech_prob", 0.0) > 0.6 and getattr(s, "avg_logprob", 0.0) < -1.0)
                    and getattr(s, "compression_ratio", 1.0) < 2.6]
            text = " ".join(s.text.strip() for s in kept).strip()
            if kept:
                out["no_speech"] = round(max(getattr(s, "no_speech_prob", 0.0) for s in kept), 3)
                out["logprob"] = round(sum(getattr(s, "avg_logprob", 0.0) for s in kept) / len(kept), 3)
            norm = normalise(text)
            weak = (not kept) or (out["no_speech"] or 0) > 0.35 or (out["logprob"] or 0) < -0.7
            if not norm:
                out["dropped"] = "no speech" if not segs else "not speech"
                text = ""
            elif norm in HALLUCINATIONS and weak:
                out["dropped"] = f"probably noise ('{text}')"
                text = ""
            out["text"], out["lang"] = text, ("ko" if lang == "ko" else "en")
        except Exception as e:
            out["dropped"] = "error"
            out["error"] = explain_error(e)
            self.error = out["error"]
        out["seconds"] = round(time.time() - t0, 2)
        self.last = dict(out)
        return out

    def transcribe(self, audio_float32, sample_rate: int = 16000) -> Tuple[str, str]:
        """Return (text, 'en' | 'ko')."""
        r = self.transcribe_detail(audio_float32, sample_rate)
        return r["text"], r["lang"]
