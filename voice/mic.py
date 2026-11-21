"""
voice/mic.py -- the microphone, opened robustly, and a meter that notices silence (v1.4.2).

WHY: up to v1.4.1 the voice engine opened exactly one stream -- the default device at
16 kHz mono -- and when that failed, or delivered nothing but digital silence, it retried
quietly forever. A PC without a default recording device, a microphone that only runs at
48 kHz, Windows' microphone privacy switch and a muted microphone therefore all looked the
same: JARVIS simply never heard anything, and nothing said why.

MicStream
  Tries, in order: the device chosen in Settings (voice.input_device), the default input,
  the same device through the other Windows audio systems (MME, DirectSound, WASAPI), then
  every other real microphone; for each, the formats a device is likely to accept (16 kHz
  mono, then its own rate in mono and stereo, then 48 / 44.1 kHz). Whatever opens is
  converted on the fly to the 16 kHz mono 20 ms frames the recognisers expect. It records
  what it tried and why each attempt failed, for the HUD, the launcher and the "check the
  microphone" command. Loopback devices (Stereo Mix, virtual cables) are never chosen
  automatically: through them JARVIS would hear its own voice.

LevelMeter
  Measures every frame: level, noise floor (for the engine's speech detection) and runs of
  digital silence -- exact zeros are what Windows delivers when the microphone is muted or
  blocked by the privacy settings, which a real microphone never produces.

privacy_status() / endpoint_status()
  Read the Windows settings behind a silent microphone (Settings > Privacy & security >
  Microphone, and the recording device's mute and level), so a report can name the switch.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

SR = 16000
FRAME_SAMPLES = 320                  # 20 ms at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2      # int16
IS_WINDOWS = sys.platform.startswith("win")
PRIVACY_PAGE = "ms-settings:privacy-microphone"
SOUND_PAGE = "ms-settings:sound"

# lower-case substrings of device names (English and Korean Windows)
LOOPBACK_WORDS = ("stereo mix", "what u hear", "what you hear", "wave out", "loopback",
                  "cable output", "voicemeeter out", "monitor of", "스테레오 믹스", "스테레오믹스")
MIC_WORDS = ("microphone", "mic", "마이크", "headset", "헤드셋", "array", "webcam", "웹캠",
             "usb", "communications")
VIRTUAL_WORDS = ("steam streaming", "virtual", "vb-audio", "voicemeeter", "nvidia broadcast",
                 "oculus", "wo mic", "droidcam", "sound mapper", "primary sound capture")
# lower rank = tried first. MME converts any sample rate; WDM-KS is often exclusive and flaky.
HOSTAPI_RANK = {"mme": 0, "windows directsound": 1, "windows wasapi": 2, "windows wdm-ks": 4,
                "alsa": 0, "pulseaudio": 0, "core audio": 0, "jack audio connection kit": 3}


def _lc(s) -> str:
    return str(s or "").strip().lower()


def is_loopback(name: str) -> bool:
    n = _lc(name)
    return any(w in n for w in LOOPBACK_WORDS)


def mic_score(name: str) -> int:
    """How much a device name looks like a real microphone (higher is better)."""
    n = _lc(name)
    score = 2 if any(w in n for w in MIC_WORDS) else 0
    if any(w in n for w in VIRTUAL_WORDS):
        score -= 1
    if is_loopback(n):
        score -= 5
    return score


def hostapi_rank(api: str) -> int:
    return HOSTAPI_RANK.get(_lc(api), 3)


def names_match(a: str, b: str) -> bool:
    """Device names across Windows audio systems: MME cuts names to 31 characters."""
    a, b = _lc(a), _lc(b)
    if not a or not b:
        return False
    return a == b or a.startswith(b[:31]) or b.startswith(a[:31]) or (len(b) >= 4 and b in a)


# --------------------------------------------------------------------------- #
# device discovery
# --------------------------------------------------------------------------- #
def default_input_index(sd) -> int:
    """PortAudio's default input device index, or -1 if there is none."""
    try:
        dev = sd.default.device
        idx = dev[0] if isinstance(dev, (list, tuple)) or hasattr(dev, "__getitem__") else dev
        if isinstance(idx, int) and idx >= 0:
            return idx
    except Exception:
        pass
    try:
        api = sd.query_hostapis(sd.default.hostapi)
        return int(api.get("default_input_device", -1))
    except Exception:
        return -1


def list_input_devices(sd) -> List[dict]:
    """Every device with input channels: index, name, host API, channels, default rate."""
    try:
        devs = list(sd.query_devices())
        apis = list(sd.query_hostapis())
    except Exception:
        return []
    default_in = default_input_index(sd)
    out = []
    for i, d in enumerate(devs):
        try:
            ch = int(d.get("max_input_channels", 0) or 0)
            if ch <= 0:
                continue
            idx = int(d.get("index", i))
            api = apis[int(d.get("hostapi", 0))] if apis else {}
            out.append({"index": idx, "name": str(d.get("name", f"device {idx}")),
                        "hostapi": str(api.get("name", "")), "channels": ch,
                        "rate": float(d.get("default_samplerate") or 0.0),
                        "default": idx == default_in,
                        "api_default": idx == int(api.get("default_input_device", -2)),
                        "loopback": is_loopback(d.get("name", ""))})
        except Exception:
            continue
    return out


def device_choices(sd) -> List[str]:
    """Names for the Settings drop-down: 'default', then each microphone once."""
    names: List[str] = []
    for d in sorted(list_input_devices(sd), key=lambda d: (d["loopback"], -mic_score(d["name"]),
                                                            hostapi_rank(d["hostapi"]))):
        if not any(names_match(d["name"], n) for n in names):
            names.append(d["name"])
    return ["default"] + names


def refresh_portaudio(sd) -> bool:
    """Re-scan audio devices. PortAudio lists devices only when it starts, so a microphone
    plugged in later stays invisible until it restarts. Only call this while no stream is
    open (it ends every stream). Returns True when the re-scan happened."""
    term, init = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
    if not callable(term) or not callable(init):
        return False
    try:
        term()
        init()
        return True
    except Exception:
        try:
            init()
        except Exception:
            pass
        return False


def explain_error(err: str) -> str:
    """Plain-English reason for a PortAudio error message."""
    e = _lc(err)
    if "querying device -1" in e or "no default input" in e or "device unavailable" in e and "-1" in e:
        return "no microphone was found (Windows has no default recording device)"
    if "device unavailable" in e or "unanticipated host error" in e or "mme error" in e:
        return "the microphone could not be opened (unplugged, disabled, or held by another program)"
    if "invalid sample rate" in e or "invalid number of channels" in e or "sample format" in e:
        return "the microphone rejected every audio format JARVIS tried"
    if "access" in e and "denied" in e:
        return "Windows denied access to the microphone (privacy settings)"
    return err.strip() or "unknown error"


# --------------------------------------------------------------------------- #
# format conversion: any rate / channel count -> 16 kHz mono int16, 20 ms frames
# --------------------------------------------------------------------------- #
class Converter:
    """Streaming conversion of int16 PCM to 16 kHz mono: channel mix-down, an anti-alias
    low-pass FIR, then fractional resampling (linear interpolation). State carries across
    blocks, so consecutive callbacks join seamlessly."""

    def __init__(self, np, rate: int, channels: int = 1, out_rate: int = SR):
        self.np = np
        self.rate, self.channels, self.out_rate = int(rate), max(1, int(channels)), int(out_rate)
        self.identity = self.rate == self.out_rate and self.channels == 1
        self.resample = self.rate != self.out_rate
        if self.resample:
            self.step = self.rate / self.out_rate
            if self.rate > self.out_rate:                          # downsampling: anti-alias first
                taps = max(31, int(32 * self.step)) | 1
                fc = 0.45 * self.out_rate / self.rate              # cycles per input sample
                n = np.arange(taps) - (taps - 1) / 2.0
                h = 2 * fc * np.sinc(2 * fc * n) * np.blackman(taps)
            else:                                                  # upsampling (e.g. an 8 kHz headset)
                taps, h = 1, np.ones(1)
            self._h = (h / h.sum()).astype(np.float32)
            self._hist = np.zeros(taps - 1, dtype=np.float32)
            self._buf = np.zeros(0, dtype=np.float32)
            self._t = 0.0

    def process(self, data: bytes) -> bytes:
        if self.identity:
            return bytes(data)
        np = self.np
        x = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
        if self.channels > 1:
            n = len(x) // self.channels
            x = x[: n * self.channels].reshape(n, self.channels).mean(axis=1)
        if not self.resample:
            y = x
        else:
            xx = np.concatenate([self._hist, x])
            f = np.convolve(xx, self._h, mode="valid")              # len(x) filtered samples
            self._hist = xx[len(xx) - (len(self._h) - 1):]
            buf = np.concatenate([self._buf, f]) if self._buf.size else f
            last = len(buf) - 2                                     # pos + 1 must stay inside buf
            if last < self._t:
                self._buf = buf
                return b""
            n = int((last - self._t) // self.step) + 1
            pos = self._t + self.step * np.arange(n)
            i = pos.astype(np.int64)
            frac = (pos - i).astype(np.float32)
            y = buf[i] * (1.0 - frac) + buf[i + 1] * frac
            t_next = self._t + self.step * n
            drop = int(t_next)
            self._buf = buf[drop:]
            self._t = t_next - drop
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


class Framer:
    """Cuts a byte stream into 640-byte (20 ms, 16 kHz, int16) frames."""

    def __init__(self, size: int = FRAME_BYTES):
        self.size = size
        self._rest = b""

    def push(self, pcm: bytes) -> List[bytes]:
        data = self._rest + pcm if self._rest else pcm
        n = len(data) // self.size
        frames = [data[i * self.size:(i + 1) * self.size] for i in range(n)]
        self._rest = data[n * self.size:]
        return frames

    def reset(self):
        self._rest = b""


# --------------------------------------------------------------------------- #
# level meter
# --------------------------------------------------------------------------- #
class LevelMeter:
    """Per-frame level in dBFS, a running noise floor and digital-silence detection."""
    FLOOR_FRAMES = 250                   # the floor is estimated over the last 5 s
    SILENT_PEAK = 2                      # |sample| <= 2 everywhere: digital silence

    def __init__(self, np):
        self.np = np
        self.db = -100.0                 # last frame
        self.peak_db = -100.0            # decaying peak, for the HUD meter
        self.floor_db = -60.0            # noise floor (15th percentile of recent frames)
        self.zero_run = 0                # consecutive frames of digital silence
        self.frames = 0
        self.loudest_db = -100.0         # loudest frame since the last reset_stats()
        self._hist: deque = deque(maxlen=self.FLOOR_FRAMES)

    def update(self, frame: bytes) -> float:
        np = self.np
        x = np.frombuffer(frame, dtype=np.int16)
        self.frames += 1
        if not x.size:
            return self.db
        peak = max(int(x.max()), -int(x.min()))
        self.zero_run = self.zero_run + 1 if peak <= self.SILENT_PEAK else 0
        rms = float(np.sqrt(np.mean(np.square(x.astype(np.float32)))))
        db = 20.0 * math.log10(max(rms, 0.33) / 32768.0)          # floor at about -100 dBFS
        self.db = db
        self.peak_db = max(db, self.peak_db - 1.0)                  # ~50 dB/s decay
        self.loudest_db = max(self.loudest_db, db)
        self._hist.append(db)
        if self.frames % 25 == 0 or self.frames < 25:
            self.floor_db = float(np.percentile(np.fromiter(self._hist, dtype=np.float32), 15))
        return db

    @property
    def silent_seconds(self) -> float:
        return self.zero_run * FRAME_SAMPLES / SR

    def level(self) -> float:
        """0..1 for the HUD meter (-70 dBFS .. -10 dBFS)."""
        return max(0.0, min(1.0, (self.peak_db + 70.0) / 60.0))

    def reset_stats(self):
        self.loudest_db = -100.0

    def reset(self):
        self.zero_run = 0
        self.frames = 0
        self._hist.clear()
        self.floor_db = -60.0
        self.peak_db = self.db = -100.0


# --------------------------------------------------------------------------- #
# Windows settings that silence a microphone
# --------------------------------------------------------------------------- #
def privacy_status() -> dict:
    """Windows microphone privacy switches (Settings > Privacy & security > Microphone).
    {'device': 'Allow'|'Deny', 'apps': ..., 'desktop_apps': ..., 'blocked': bool, 'which': [...]}"""
    out: Dict[str, object] = {"blocked": False, "which": []}
    if not IS_WINDOWS:
        return out
    try:
        import winreg  # type: ignore
    except Exception:
        return out
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    probes = [("device", winreg.HKEY_LOCAL_MACHINE, base),
              ("apps", winreg.HKEY_CURRENT_USER, base),
              ("desktop_apps", winreg.HKEY_CURRENT_USER, base + r"\NonPackaged")]
    for label, hive, path in probes:
        try:
            with winreg.OpenKey(hive, path) as k:
                val, _ = winreg.QueryValueEx(k, "Value")
            out[label] = str(val)
            if str(val).strip().lower() == "deny":
                out["blocked"] = True
                out["which"].append(label)
        except OSError:
            pass
    try:   # group policy that forces microphone access off (2 = force deny)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy") as k:
            val, _ = winreg.QueryValueEx(k, "LetAppsAccessMicrophone")
        out["policy"] = int(val)
        if int(val) == 2:
            out["blocked"] = True
            out["which"].append("policy")
    except (OSError, ValueError, TypeError):
        pass
    return out


def endpoint_status() -> dict:
    """The default recording device as Windows sees it: name, muted, level (0..1)."""
    out: Dict[str, object] = {}
    if not IS_WINDOWS:
        return out
    co = None
    try:
        import comtypes  # type: ignore
        co = comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            co = None
        from pycaw.pycaw import AudioUtilities  # type: ignore
        dev = AudioUtilities.GetMicrophone()
        if dev is None:
            out["present"] = False
            return out
        out["present"] = True
        ev = getattr(dev, "EndpointVolume", None)
        if ev is None:
            from ctypes import cast, POINTER  # type: ignore
            from comtypes import CLSCTX_ALL  # type: ignore
            from pycaw.pycaw import IAudioEndpointVolume  # type: ignore
            ev = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                      POINTER(IAudioEndpointVolume))
        out["muted"] = bool(ev.GetMute())
        out["volume"] = round(float(ev.GetMasterVolumeLevelScalar()), 2)
        try:
            name = getattr(dev, "FriendlyName", None)
            if name:
                out["name"] = str(name)
        except Exception:
            pass
    except Exception as e:
        out["error"] = repr(e)[:200]
    finally:
        if co is not None:
            try:
                co.CoUninitialize()
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- #
# the stream
# --------------------------------------------------------------------------- #
class MicStream:
    """Opens the best working microphone and delivers 20 ms 16 kHz mono int16 frames to
    sink(frame) from the audio thread."""

    def __init__(self, sd, np, sink: Callable[[bytes], None], device: str = "default",
                 log: Optional[Callable[[str], None]] = None):
        self.sd, self.np, self.sink = sd, np, sink
        self.device_pref = device or "default"
        self.log = log or (lambda m: None)
        self.stream = None
        self.info: dict = {}              # the open device
        self.error = ""                   # why the last open() failed ('' while open)
        self.reason = ""                  # explain_error(error)
        self.attempts: List[str] = []     # what the last open() tried
        self.bad: Dict[int, str] = {}     # device index -> why it was abandoned
        self.pref_missing = False         # the device chosen in Settings is not connected
        self.callbacks = 0
        self.overflows = 0
        self.last_data = 0.0
        self.opened_at = 0.0
        self.cb_error = ""
        self._lock = threading.RLock()
        self._conv: Optional[Converter] = None
        self._framer = Framer()
        self._logged: set = set()

    # ------------------------------------------------------------ helpers #
    @property
    def is_open(self) -> bool:
        return self.stream is not None

    @property
    def locked_to_choice(self) -> bool:
        """The user chose a device in Settings: never wander off to another one."""
        return _lc(self.device_pref) not in ("", "default", "auto") and not self.pref_missing

    def _say(self, msg: str):
        if msg not in self._logged:
            self._logged.add(msg)
            self.log(msg)

    def devices(self) -> List[dict]:
        return list_input_devices(self.sd)

    def candidates(self) -> List[dict]:
        devs = self.devices()
        self.pref_missing = False
        if not devs:   # enumeration unavailable: let PortAudio pick its default
            return [{"index": None, "name": "default input", "hostapi": "", "channels": 2,
                     "rate": 0.0, "default": True, "api_default": True, "loopback": False}]
        out: List[dict] = []

        def add(d):
            if all(d["index"] != o["index"] for o in out):
                out.append(d)

        pref = self.device_pref.strip()
        if _lc(pref) not in ("", "default", "auto"):
            for d in sorted(devs, key=lambda d: hostapi_rank(d["hostapi"])):
                if names_match(d["name"], pref):
                    add(d)
            if out:
                return out
            self.pref_missing = True                     # fall back to the default, and say so
        for d in devs:
            if d["default"]:
                add(d)
        default_names = [d["name"] for d in out]
        # the default microphone through the other audio systems, then other systems' defaults
        for d in sorted(devs, key=lambda d: hostapi_rank(d["hostapi"])):
            if any(names_match(d["name"], n) for n in default_names) and not d["loopback"]:
                add(d)
        for d in sorted(devs, key=lambda d: hostapi_rank(d["hostapi"])):
            if d["api_default"] and not d["loopback"]:
                add(d)
        rest = [d for d in devs if not d["loopback"] and all(d["index"] != o["index"] for o in out)]
        rest.sort(key=lambda d: (-mic_score(d["name"]), hostapi_rank(d["hostapi"])))
        for d in rest:
            add(d)
        return out

    @staticmethod
    def formats(d: dict) -> List[tuple]:
        rates = [SR]
        if d.get("rate"):
            rates.append(int(round(d["rate"])))
        rates += [48000, 44100]
        chans = [1] + ([2] if int(d.get("channels") or 1) >= 2 else [])
        out, seen = [], set()
        for r in rates:
            for c in chans:
                if (r, c) not in seen:
                    seen.add((r, c))
                    out.append((r, c))
        return out

    def has_alternatives(self) -> bool:
        if self.locked_to_choice:
            return False
        cur = self.info.get("index")
        return any(d["index"] != cur and d["index"] not in self.bad for d in self.candidates())

    # -------------------------------------------------------------- open #
    def open(self, exclude=()) -> bool:
        """Open the first candidate/format that works. Returns True when open."""
        with self._lock:
            self._close_locked()
            self.attempts, errors = [], []
            cands = self.candidates()
            usable = [d for d in cands if d["index"] not in exclude and d["index"] not in self.bad]
            if not usable:                                # everything was abandoned: start over
                self.bad.clear()
                usable = [d for d in cands if d["index"] not in exclude] or cands
            for d in usable[:10]:
                for rate, ch in self.formats(d):
                    err = self._try(d, rate, ch)
                    label = f"{d['name']} [{d['hostapi'] or 'default'}] {rate} Hz {ch}ch"
                    if err is None:
                        self.attempts.append(label + ": ok")
                        self.error = self.reason = ""
                        note = "" if (rate, ch) == (SR, 1) else " (converted to 16 kHz mono)"
                        self._say(f"microphone: {d['name']} via {d['hostapi'] or 'the default audio system'}, "
                                  f"{rate} Hz, {ch} channel(s){note}")      # once per device, not per reopen
                        if self.pref_missing:
                            self._say(f"microphone: the chosen device '{self.device_pref}' is not connected; "
                                      f"using the default instead")
                        return True
                    self.attempts.append(f"{label}: {err}")
                    errors.append(err)
                    if "querying device" in err.lower():
                        break                             # no such device: other formats won't help
            self.error = errors[0] if errors else "no input device"
            self.reason = explain_error(self.error)
            self._say(f"microphone could not be opened: {self.reason} ({self.error})")
            return False

    def _try(self, d: dict, rate: int, ch: int) -> Optional[str]:
        sd = self.sd
        kw = dict(samplerate=rate, blocksize=max(1, int(round(rate * 0.02))), dtype="int16",
                  channels=ch, callback=self._callback)
        if d.get("index") is not None:
            kw["device"] = d["index"]
        if "wasapi" in _lc(d.get("hostapi")) and hasattr(sd, "WasapiSettings"):
            try:
                kw["extra_settings"] = sd.WasapiSettings(auto_convert=True)
            except Exception:
                pass
        self._conv = Converter(self.np, rate, ch)
        self._framer.reset()
        stream = None
        try:
            stream = sd.RawInputStream(**kw)
            self.callbacks = 0
            self.opened_at = time.monotonic()
            self.last_data = 0.0
            stream.start()
        except Exception as e:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            return str(e).strip() or type(e).__name__
        self.stream = stream
        self.info = {"index": d.get("index"), "name": d.get("name"), "hostapi": d.get("hostapi"),
                     "rate": rate, "channels": ch, "converted": (rate, ch) != (SR, 1),
                     "default": bool(d.get("default"))}
        return None

    def _callback(self, indata, frames, time_info, status):
        try:
            self.callbacks += 1
            self.last_data = time.monotonic()
            if status:
                self.overflows += 1
            pcm = self._conv.process(indata) if self._conv is not None else bytes(indata)
            for frame in self._framer.push(pcm):
                self.sink(frame)
        except Exception as e:          # an exception here would silently stop the stream
            self.cb_error = repr(e)[:200]

    # ------------------------------------------------------------- close #
    def _close_locked(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close_locked()

    def abandon(self, reason: str) -> None:
        """Give up on the open device (no data, only silence) so open() tries the next one."""
        idx = self.info.get("index")
        if idx is not None:
            self.bad[idx] = reason
        self._say(f"microphone: leaving {self.info.get('name')} ({reason})")
        self.close()

    def stalled(self, seconds: float) -> bool:
        """Open, but no audio has arrived for `seconds` (device unplugged or broken)."""
        if self.stream is None:
            return False
        since = self.last_data or self.opened_at
        return bool(since) and time.monotonic() - since > seconds

    def describe(self) -> dict:
        return {"open": self.is_open, "device": self.info.get("name"),
                "hostapi": self.info.get("hostapi"), "rate": self.info.get("rate"),
                "channels": self.info.get("channels"), "converted": self.info.get("converted"),
                "chosen": self.device_pref, "chosen_missing": self.pref_missing,
                "error": self.error or None, "reason": self.reason or None,
                "abandoned": {str(k): v for k, v in self.bad.items()} or None,
                "attempts": self.attempts[-12:], "callback_error": self.cb_error or None}
