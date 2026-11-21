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

Audio: 16 kHz mono int16, 20 ms frames (640 bytes); process() also accepts several frames
joined together (the engine batches frames when the CPU is busy).

ACCENTS (v1.4.2): Vosk's small English model hears a native "wake up Jarvis" reliably, but
with an accent it often hears only "[unk] jarvis" or "wake up [unk]", and in grammar mode
ordinary words such as "service" or "nervous" come out as "jarvis". So Vosk is now the fast
first stage: the exact phrase (or "Jarvis, wake up") wakes at once, and anything close --
"wake", "up" or "jarvis" heard in an utterance -- is checked by Whisper on that audio, which
understands accented English (and Korean: "자비스, 일어나"). wake_verdict() judges Whisper's
text.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

Result = Optional[Tuple[str, bool]]
WAKE_PHRASE = "wake up jarvis"


class VoskPhraseDetector:
    GRAMMAR = ["wake up jarvis", "jarvis", "stop", "wait", "hold on", "[unk]"]
    kind = "vosk"
    knows_keywords = True

    def __init__(self, model_dir: str, sample_rate: int = 16000):
        self.available = False
        self.error = ""
        self.model_dir = model_dir
        self._last_partial = ""
        try:
            import vosk  # type: ignore
        except Exception as e:
            self.error = f"the vosk package is not installed ({e})"
            return
        try:
            vosk.SetLogLevel(-1)
            if not os.path.isdir(os.path.join(model_dir, "conf")):
                self.error = f"the Vosk model is missing ({model_dir})"
                return
            self._model = vosk.Model(model_dir)
            self._rec = vosk.KaldiRecognizer(self._model, sample_rate, json.dumps(self.GRAMMAR))
            self.available = True
        except Exception as e:
            self.error = f"the Vosk model could not be loaded ({e})"
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
        self.error = ""
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
            if not self.available:
                self.error = "openWakeWord loaded no model"
        except Exception as e:
            self.error = f"openWakeWord is unavailable ({e})"
            self.available = False

    def process(self, frame: bytes) -> Result:
        if not self.available:
            return None
        self._buf += frame
        while len(self._buf) >= self.CHUNK:
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


def make_detector_ex(config: dict, root: str):
    """Build the configured detector, falling back to the other engine if needed.
    Returns (detector or None, info) -- info says which engine runs, or why none does."""
    ww = config.get("voice", {}).get("wakeword", {})
    vosk_rel = ww.get("vosk_model", "models/vosk-small-en")
    vosk_dir = os.path.join(root, vosk_rel)
    oww_path = os.path.join(root, ww.get("openwakeword_model", "models/wake_up_jarvis.onnx"))
    sens = float(ww.get("sensitivity", 0.6))
    order = ["vosk", "openwakeword"] if ww.get("engine", "vosk") == "vosk" else ["openwakeword", "vosk"]
    info = {"engine": None, "ready": False, "tried": [], "error": "", "vosk_dir": vosk_dir}
    for kind in order:
        det = VoskPhraseDetector(vosk_dir) if kind == "vosk" else OpenWakeWordDetector(oww_path, sens)
        if det.available:
            info.update(engine=kind, ready=True,
                        phrase="wake up jarvis" if kind == "vosk" else "hey jarvis (openWakeWord)")
            return det, info
        info["tried"].append(f"{kind}: {getattr(det, 'error', '') or 'unavailable'}")
    info["error"] = "; ".join(info["tried"])
    return None, info


def make_detector(config: dict, root: str):
    """Build the configured detector (None if no engine works)."""
    return make_detector_ex(config, root)[0]


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


# --------------------------------------------------------------------------- #
# judging what was heard
# --------------------------------------------------------------------------- #
def tokens(text: str):
    return [t for t in re.split(r"[^\w'\[\]가-힣]+", (text or "").lower()) if t]


CANONICAL = (("wake", "up", "jarvis"), ("jarvis", "wake", "up"))


def exact_wake(text: str, lenient: bool = False) -> bool:
    """Exactly the wake phrase and nothing else ("wake up jarvis" / "jarvis wake up"; with
    lenient also "jarvis"). Vosk in grammar mode forces unknown words into its vocabulary --
    "wake up early" can come out as "wake up jarvis" -- so anything less tidy is double-checked."""
    toks = tuple(tokens(text))
    return toks in CANONICAL or (lenient and toks == ("jarvis",))


def loose_wake(text: str) -> bool:
    """Vosk's final text holds the name together with 'wake' (any order), or 'up' before
    the name: "jarvis wake up", "wake jarvis", "[unk] up jarvis"."""
    toks = tokens(text)
    if "jarvis" not in toks:
        return False
    i = toks.index("jarvis")
    return "wake" in toks or "up" in toks[:i]


def wake_candidate(text: str) -> bool:
    """Worth a closer look by Whisper: anything that could be part of the wake phrase."""
    toks = tokens(text)
    return any(t in ("wake", "up", "jarvis") for t in toks)


# how Whisper writes the name, with an accent or in Korean
NAME_VARIANTS = {"jarvis", "jarvis'", "jarvis's", "jervis", "javis", "jarviz", "jarvi", "jarvy",
                 "jarbis", "jabis", "jabbis", "jarvas", "jarvus", "jarviss", "jaavis", "zarvis",
                 "chavis", "harvis", "garvis", "jarvice"}
KO_NAMES = ("자비스", "쟈비스", "자비즈", "재비스", "짜비스", "자르비스")
_WAKE_EN = re.compile(r"\b(wake|woke|waking|wakey|wakeup|weak\s+up|wait\s+up)\b")
_WAKE_KO = re.compile(r"(일어나|일어나라|일어나요|깨어나|기상|웨이크)")


def _lev(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance (stops early once it exceeds cap)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def name_like(token: str) -> bool:
    t = token.strip("'’.,!?")
    if t in NAME_VARIANTS:
        return True
    if len(t) >= 5 and _lev(t, "jarvis") <= 1:
        return True
    return len(t) >= 5 and t[0] == "j" and t[-1] in "sz" and _lev(t, "jarvis") <= 2


def _skeleton(word: str) -> str:
    """Consonant skeleton: 'Waikop', 'Weckup', 'wacka up' and 'wake up' all become 'wkp'."""
    s = re.sub(r"ck|c|q", "k", word.lower())
    s = re.sub(r"[aeiouy'\s-]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def wake_up_like(words) -> bool:
    """One or two neighbouring words that sound like 'wake up' however Whisper spelled them
    (an accent often turns it into 'Waikop' or 'Wacka up'), or 'get up'."""
    words = [w.strip("'’.,!?") for w in words]
    for i in range(len(words)):
        if words[i] == "get" and i + 1 < len(words) and words[i + 1] == "up":
            return True
        for j in (1, 2):
            chunk = "".join(words[i:i + j])
            if 4 <= len(chunk) <= 9 and chunk[0] in "wvb" and _skeleton(chunk) in ("wkp", "vkp", "bkp"):
                return True                  # w/v/b swaps: an accent often comes out as "Vaku up" / "Back up"
    return False


def wake_verdict(text: str) -> str:
    """Judge Whisper's transcript of a wake candidate: 'wake' (the wake phrase, in any
    order, English or Korean), 'name' (only the name) or '' (neither)."""
    low = (text or "").lower()
    toks = tokens(low)
    has_name = any(name_like(t) for t in toks) or any(k in low for k in KO_NAMES)
    if not has_name:
        return ""
    others = [t for t in toks if not name_like(t)]
    if _WAKE_EN.search(low) or _WAKE_KO.search(low) or wake_up_like(others):
        return "wake"
    return "name"
