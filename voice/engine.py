"""
voice/engine.py -- the full-duplex voice loop (requirements 3, 7, 8, 12, 13, 21).

  IDLE        listen for "wake up Jarvis"  -> on_wake()  (the server speaks the response and
                                              calls begin_capture(after_speech=True))
  CAPTURE     record until the user stops talking
  TRANSCRIBE  Whisper turns it into text -> on_command(text)
  SPEAKING    replies play from a queue, sentence by sentence; barge-in can cut them off:
                keyword (default): "stop" / "wait" / "hold on" / "Jarvis"  (immune to speaker echo)
                vad:               any speech (best with a headset)
                off:               no voice interruption
  MUTED       the AI-mic mute button closes the microphone stream entirely (privacy)

The WAKE button / Ctrl+Alt+J go through the same on_wake path, and the overlay's mic button
calls begin_capture() directly (push-to-talk), so all of them reach the identical capture ->
Whisper -> command flow.

v1.4.2 -- WHY VOICE COULD FAIL WITHOUT A WORD, AND WHAT CHANGED
  * Microphone: voice/mic.py opens whatever device and format works, moves on from a device
    that sends nothing, and recognises a muted or privacy-blocked microphone (exact digital
    silence) and the Windows setting behind it.
  * Start-up: the microphone and the wake word start at once; the British voice and Whisper
    load in the background (load_models()), so a slow or failing Whisper download no longer
    keeps the wake word -- or the whole voice -- offline.
  * Wake: the exact phrase wakes at once (a partial result must hold for 0.4 s: Vosk can
    briefly turn "wake up early" into "wake up jarvis"). Anything less tidy, and near misses -- an accent turning "wake up" or
    "Jarvis" into [unk] -- are checked by Whisper on that utterance's audio (wakeword.py), and
    so is a short phrase said on its own in a quiet moment even when Vosk made nothing of it
    (that is also how the voice still wakes if the Vosk model is missing).
  * Capture: speech must be sustained (4 of 8 frames) and above the noise floor to start a
    command; the first moments after JARVIS stops talking are ignored (its own voice is still
    ringing in the room); 300 ms before the start is kept; and a capture without real speech
    is reported ("I didn't catch that") instead of sending Whisper's guess at noise.
  * Feedback: level, problems, near misses and unheard commands go to on_event(), which the
    server turns into the HUD meter, toasts and spoken explanations.
  * Resilience: an error while handling one frame is logged and the loop carries on.

If audio libraries are missing (e.g. while testing on Linux) `available` is False and the
server simply skips the engine; typed commands still work.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

from .wakeword import (make_detector_ex, heard_wake, heard_keyword, loose_wake, wake_candidate, wake_verdict,
                       exact_wake)
from .interrupt import BargeInMonitor
from .mic import (MicStream, LevelMeter, privacy_status, endpoint_status, refresh_portaudio,
                  PRIVACY_PAGE, SOUND_PAGE)

SR = 16000
FRAME = SR * 20 // 1000          # 320 samples = 20 ms
END_SILENCE_FRAMES = 30          # default: 0.6 s of silence ends a command (voice.stt.end_silence_ms)
MAX_CAPTURE_FRAMES = 750         # 15 s cap
NO_SPEECH_FRAMES = 300           # give up after 6 s without speech
PARTIAL_KEYWORD_FRAMES = 4       # a keyword must persist this many results in a partial
MAX_BACKLOG_FRAMES = 150         # 3 s: if wake detection falls this far behind, skip ahead
BATCH_FRAMES = 10                # frames handed to the recogniser together when it lags
ONSET_WINDOW, ONSET_MIN = 8, 4   # a command starts when 4 of the last 8 frames are speech
ECHO_TAIL_S = 0.25               # ignore this long after JARVIS stops talking (room echo)
PRE_ROLL_FRAMES = 15             # keep 300 ms before the detected start
MIN_SPEECH_FRAMES = 10           # under 0.2 s of speech is a noise, not a command
ONSET_MARGIN_DB = 6.0            # speech must be this far above the noise floor to start ...
CONT_MARGIN_DB = 3.0             # ... and this far to keep a command going
SILENT_FRAMES = 150              # 3 s of digital silence: the microphone is muted / blocked
STALL_S = 2.5                    # an open stream that delivers nothing for this long is dead
LEVEL_EVERY = 10                 # HUD level report every 10 frames (5 per second)
WAKE_RING_FRAMES = 200           # the last 4 s, for checking a near-miss wake with Whisper
VERIFY_MIN_FRAMES = 150          # Whisper hears at least the last 3 s of a near miss
ISOLATED_MIN, ISOLATED_MAX = 25, 110   # a short phrase on its own (0.5 - 2.2 s) ...
ISOLATED_QUIET = 25              # ... after at least 0.5 s of quiet: how people call an assistant
SEGMENT_END = 20                 # a 0.4 s pause ends a phrase
VERIFY_EVERY_S = 2.5             # at most one Whisper check of a possible wake per 2.5 s
PROVISIONAL_S = 0.4              # a partial "wake up jarvis" must hold this long before it wakes
NAME_REPEAT_S = 10.0             # "Jarvis ... Jarvis!" within this many seconds wakes
HINT_EVERY_S = 45.0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# what keeps JARVIS from hearing, with the fix (English / Korean)
PROBLEMS: Dict[str, dict] = {
    "privacy": dict(
        blocking=True, action=PRIVACY_PAGE,
        en="Windows is blocking desktop apps from using the microphone, so I can't hear you, sir.",
        fix="Open Settings > Privacy & security > Microphone and turn on 'Microphone access' and "
            "'Let desktop apps access your microphone'. I'll start listening the moment it's allowed.",
        ko="Windows가 데스크톱 앱의 마이크 사용을 막고 있어서 말씀을 들을 수 없습니다, sir.",
        fix_ko="설정 > 개인 정보 및 보안 > 마이크에서 '마이크 액세스'와 '데스크톱 앱이 마이크에 "
               "액세스하도록 허용'을 켜 주세요. 허용되는 즉시 듣기 시작합니다."),
    "muted": dict(
        blocking=True, action=SOUND_PAGE,
        en="Your microphone is muted in Windows, sir.",
        fix="Type 'unmute the microphone', click the You button, or unmute it in Settings > System > Sound > Input.",
        ko="Windows에서 마이크가 음소거되어 있습니다, sir.",
        fix_ko="'마이크 켜 줘'라고 입력하거나 사용자 버튼을 누르거나, 설정 > 시스템 > 소리 > 입력에서 음소거를 해제해 주세요."),
    "silent": dict(
        blocking=True, action=SOUND_PAGE,
        en="Your microphone sends nothing but silence, sir.",
        fix="Check that the right microphone is the default input (Settings > System > Sound > Input), that it is "
            "plugged in and switched on, and its input volume - or choose another microphone in my Settings.",
        ko="마이크에서 아무 소리도 들어오지 않습니다, sir.",
        fix_ko="설정 > 시스템 > 소리 > 입력에서 올바른 마이크가 기본 장치인지, 연결되어 켜져 있는지, 입력 볼륨을 "
               "확인하시거나 제 설정에서 다른 마이크를 고르세요."),
    "no_device": dict(
        blocking=True, action=SOUND_PAGE,
        en="I can't find a microphone, sir.",
        fix="Plug one in, or enable one in Settings > System > Sound > Input. I'll start listening as soon as one appears.",
        ko="마이크를 찾을 수 없습니다, sir.",
        fix_ko="마이크를 연결하거나 설정 > 시스템 > 소리 > 입력에서 사용하도록 설정해 주세요. 마이크가 생기면 바로 듣기 시작합니다."),
    "open_failed": dict(
        blocking=True, action=SOUND_PAGE,
        en="I can't open the microphone, sir ({detail}).",
        fix="Close programs that might be holding it exclusively, check it in Settings > System > Sound > Input, "
            "or choose another microphone in my Settings. I'll keep trying.",
        ko="마이크를 열 수 없습니다, sir ({detail}).",
        fix_ko="마이크를 독점 사용 중인 프로그램을 닫거나, 설정 > 시스템 > 소리 > 입력을 확인하거나, 제 설정에서 다른 "
               "마이크를 골라 주세요. 계속 다시 시도하겠습니다."),
    "no_data": dict(
        blocking=True, action=SOUND_PAGE,
        en="The microphone stopped sending sound, sir.",
        fix="I'm reconnecting it; if this keeps happening, check the cable or choose another microphone in my Settings.",
        ko="마이크에서 소리가 끊겼습니다, sir.",
        fix_ko="다시 연결하는 중입니다. 계속 반복되면 케이블을 확인하거나 제 설정에서 다른 마이크를 골라 주세요."),
    "wake_unavailable": dict(
        blocking=False, action=None,
        en="My wake-word listener isn't available, sir ({detail}).",
        fix="Click WAKE or press Ctrl+Alt+J, or run INSTALL_JARVIS.bat to restore it.",
        ko="호출어 인식기를 사용할 수 없습니다, sir ({detail}).",
        fix_ko="깨우기 버튼이나 Ctrl+Alt+J를 쓰시거나, INSTALL_JARVIS.bat을 다시 실행해 주세요."),
    "stt_failed": dict(
        blocking=False, action=None,
        en="My speech recognition isn't working, sir: {detail}",
        fix="Please type your commands meanwhile.",
        ko="음성 인식이 작동하지 않습니다, sir: {detail}",
        fix_ko="그동안은 명령을 입력해 주세요."),
}


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class _Capture:
    """One spoken command being recorded."""

    def __init__(self, kind: str, guard: int):
        self.kind = kind                  # wake | ptt | barge
        self.guard = guard                # frames to ignore first (echo of JARVIS's voice)
        self.frames = 0
        self.onset = False
        self.window: deque = deque(maxlen=ONSET_WINDOW)
        self.pre: deque = deque(maxlen=PRE_ROLL_FRAMES)
        self.audio = bytearray()
        self.speech_frames = 0
        self.silence = 0
        self.voiced = 0                   # speech-like frames before the start (activity)


class VoiceEngine:
    def __init__(self, config: dict, on_wake: Callable, on_command: Callable,
                 on_interrupt: Callable, on_state: Optional[Callable] = None, root: str = ROOT,
                 on_event: Optional[Callable] = None, log: Optional[Callable[[str], None]] = None):
        self.on_wake, self.on_command = on_wake, on_command
        self.on_interrupt, self.on_state, self.on_event = on_interrupt, on_state, on_event
        self.log = log or (lambda m: print("[jarvis] voice: " + m, flush=True))
        self.root = root
        self.config = config
        self.state = "idle"
        self.listening = True
        self.current_text = ""
        self.available = False
        self.unavailable_reason = ""
        self.stopped = False
        self._pending_capture = False
        self._pending_kind = "wake"
        self._running = False
        self._loop = None
        self._frames: Optional[asyncio.Queue] = None
        self._speech: Optional[asyncio.Queue] = None
        self._speech_gen = 0           # bumped by stop_speaking(): older queued items are dropped
        self._spoke_until = 0.0        # monotonic time JARVIS last stopped talking
        # wake-word / keyword recognition runs here, not on the event loop: on a busy CPU it
        # would otherwise hold up every request the command bus serves
        self._audio_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-audio")
        self.end_silence_frames = END_SILENCE_FRAMES
        self.speaker = None            # loaded by load_models() (tests put fakes here)
        self.stt = None
        self.detector = None
        self.vad = None
        self.mic: Optional[MicStream] = None
        self.meter: Optional[LevelMeter] = None
        self.lenient = False
        self.barge_mode, self.keywords = "keyword", ["stop", "wait", "hold on", "jarvis"]
        self.wake_info: dict = {"engine": None, "ready": False, "error": "loading"}
        self.tts_info: dict = {"state": "loading"}
        self.problems: Dict[str, dict] = {}
        self.last_heard = self.last_command = self.last_unheard = self.last_wake = None
        self.errors, self.last_error = 0, ""
        self.stats = {"wakes": 0, "commands": 0, "unheard": 0, "verifications": 0, "dropped_frames": 0,
                      "isolated_checks": 0}
        self._cap: Optional[_Capture] = None
        self._kw_hits: dict = {}
        self._ring: deque = deque(maxlen=WAKE_RING_FRAMES)
        self._frame_no = 0
        self._utt_flag, self._utt_start = False, None
        self._verifying = False
        self._pending_verify = None    # a near miss heard while Whisper was busy (latest wins)
        self._provisional = None       # when a partial "wake up jarvis" was first seen
        self._last_verify_at = 0.0
        self._seg = {"active": False, "frames": 0, "silence": 0, "pre": 0}
        self._seg_hist: deque = deque(maxlen=60)       # speech / not speech, last 1.2 s (idle only)
        self._last_name_at = 0.0
        self._hint_at: Dict[str, float] = {}
        self._reopen = False
        self._next_open = 0.0
        self._open_failures = 0
        self._silence_checked = False
        self._settled = False
        self._last_recheck = 0.0
        self._last_level_frame = 0
        self._privacy = self._endpoint = None
        self._seen_errors: set = set()
        self._repairing = False
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore
            self._sd, self._np = sd, np
        except Exception as e:
            self.unavailable_reason = f"audio libraries missing ({e})"
            return
        self.vad = BargeInMonitor()
        self.meter = LevelMeter(np)
        self.mic = MicStream(sd, np, self._on_frame,
                             device=str(config.get("voice", {}).get("input_device", "default") or "default"),
                             log=self.log)
        self.reconfigure(config)
        self.available = True

    # ------------------------------------------------------------ config #
    def reconfigure(self, config: dict):
        self.config = config
        v = config.get("voice", {})
        bi = v.get("barge_in", {})
        self.barge_mode = bi.get("mode", "keyword")
        self.keywords = [k.lower() for k in bi.get("keywords", ["stop", "wait", "hold on", "jarvis"])]
        ww = v.get("wakeword", {})
        self.lenient = bool(ww.get("lenient", False))
        if self.detector is not None and hasattr(self.detector, "sensitivity"):
            self.detector.sensitivity = float(ww.get("sensitivity", 0.6))
        if getattr(self, "speaker", None) is not None and hasattr(self.speaker, "rate"):
            self.speaker.rate = float(v.get("tts", {}).get("rate", 1.0))
        try:
            ms = float(v.get("stt", {}).get("end_silence_ms", END_SILENCE_FRAMES * 20))
        except (TypeError, ValueError):
            ms = END_SILENCE_FRAMES * 20
        self.end_silence_frames = max(10, min(100, int(round(ms / 20.0))))
        dev = str(v.get("input_device", "default") or "default")
        if self.mic is not None and dev != self.mic.device_pref:
            self.mic.device_pref = dev
            self.mic.bad.clear()
            self._settled = False
            self._reopen = True
            self._next_open = 0.0

    # ------------------------------------------------------- slow loading #
    def load_models(self, phrases=()) -> dict:
        """Load what takes time, in a background thread: the wake-word model, the British voice,
        Whisper; then warm them up. Each part works the moment it is ready."""
        done = {"wake": False, "tts": False, "stt": False, "phrases": 0}
        if not self.available:
            return done
        v = self.config.get("voice", {})
        if self.detector is None:
            det, info = make_detector_ex(self.config, self.root)
            self.detector, self.wake_info = det, info
            if det is None:
                self.log(f"wake word unavailable: {info.get('error')}")
                self._problem_threadsafe("wake_unavailable", info.get("error", ""))
                self._start_repair()
            else:
                self.log(f"wake word ready ({info.get('engine')}: say '{info.get('phrase')}')")
            self._emit_threadsafe({"type": "voice_status"})
        done["wake"] = self.detector is not None
        if self.speaker is None:
            try:
                from .tts import Speaker
                tts = v.get("tts", {})
                sp = Speaker(tts.get("voice_en", "en_GB-alan-medium"), tts.get("voice_ko", ""),
                             float(tts.get("rate", 1.0)), root=self.root)
                self.speaker = sp
                self.tts_info = {"state": "ready" if sp.available else "failed",
                                 "backend": getattr(sp, "backend", "?")}
                if not sp.available:
                    self.tts_info["error"] = "no voice could be loaded (Piper voice file, Windows voices)"
            except Exception as e:
                self.tts_info = {"state": "failed", "error": repr(e)[:200]}
            self.log(f"voice: {self.tts_info}")
            self._emit_threadsafe({"type": "voice_status"})
        done["tts"] = self.tts_ready()
        if self.stt is None:
            try:
                from .stt import Transcriber
                s = v.get("stt", {})
                self.stt = Transcriber(s.get("model", "auto"), s.get("language", "auto"), root=self.root,
                                       lazy=True, log=self.log)
            except Exception as e:
                self._problem_threadsafe("stt_failed", repr(e)[:200])
        if self.stt is not None and hasattr(self.stt, "load") and getattr(self.stt, "state", "") != "ready":
            self._emit_threadsafe({"type": "voice_status"})
            if not self.stt.load():
                self._problem_threadsafe("stt_failed", getattr(self.stt, "error", "") or "could not be loaded")
            self._emit_threadsafe({"type": "voice_status"})
        done["stt"] = bool(self.stt is not None and getattr(self.stt, "available", False))
        done.update(self.warm_up(phrases))
        return done

    def warm_up(self, phrases=()) -> dict:
        """Initialise the speech models and synthesise the frequent phrases, so nothing is
        slow the first time it is used."""
        done = {"stt_warm": False, "phrases": 0}
        if not self.available:
            return done
        if self.stt is not None and hasattr(self.stt, "warm_up"):
            done["stt_warm"] = bool(self.stt.warm_up())
        if self.speaker is not None and hasattr(self.speaker, "prewarm"):
            done["phrases"] = self.speaker.prewarm(list(phrases))
        return done

    def tts_ready(self) -> bool:
        return self.speaker is not None and bool(getattr(self.speaker, "available", True))

    def _start_repair(self):
        """The Vosk wake-word model is missing: fetch it (checksum-pinned) in the background."""
        if self._repairing or not self.config.get("voice", {}).get("wakeword", {}).get("auto_download", True):
            return
        vdir = self.wake_info.get("vosk_dir") or ""
        if not vdir or os.path.isdir(os.path.join(vdir, "conf")):
            return
        self._repairing = True

        def work():
            try:
                from .models import ensure_vosk_model
                ok, detail = ensure_vosk_model(vdir, log=self.log)
                if ok:
                    det, info = make_detector_ex(self.config, self.root)
                    if det is not None:
                        self.detector, self.wake_info = det, info
                        self.log("wake word repaired: the Vosk model was downloaded")
                        self._clear_threadsafe("wake_unavailable")
                        self._emit_threadsafe({"type": "voice_status"})
            except Exception as e:
                self.log(f"wake-word repair failed: {e!r}")
            finally:
                self._repairing = False

        threading.Thread(target=work, name="jarvis-vosk-repair", daemon=True).start()

    # ------------------------------------------------------------ events #
    async def _emit(self, event: dict):
        if self.on_event is None:
            return
        try:
            await _maybe_await(self.on_event(event))
        except Exception:
            pass

    def _emit_soon(self, event: dict):
        if self.on_event is not None and self._loop is not None:
            self._loop.create_task(self._emit(event))

    def _emit_threadsafe(self, event: dict):
        loop = self._loop
        if self.on_event is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._emit(event), loop)
        except RuntimeError:
            pass

    def _problem_dict(self, code: str, detail: str = "") -> dict:
        p = PROBLEMS[code]
        fmt = lambda s: s.replace("{detail}", detail or "unknown reason")  # noqa: E731
        return {"code": code, "blocking": p["blocking"], "action": p["action"], "detail": detail or None,
                "message": fmt(p["en"]), "fix": p["fix"], "message_ko": fmt(p["ko"]), "fix_ko": p["fix_ko"],
                "since": time.time()}

    def _set_problem(self, code: str, detail: str = "") -> None:
        cur = self.problems.get(code)
        if cur is not None and (cur.get("detail") or "") == (detail or ""):
            return
        p = self._problem_dict(code, detail)
        self.problems[code] = p
        self.log(f"problem: {p['message']} {p['fix']}")
        self._emit_soon({"type": "voice_problem", "problem": p, "active": True})

    def _clear_problem(self, code: str) -> None:
        if self.problems.pop(code, None) is not None:
            self.log(f"resolved: {code}")
            self._emit_soon({"type": "voice_problem", "problem": {"code": code}, "active": False})

    def _problem_threadsafe(self, code: str, detail: str = ""):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._set_problem, code, detail)
        else:
            self.problems[code] = self._problem_dict(code, detail)

    def _clear_threadsafe(self, code: str):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._clear_problem, code)
        else:
            self.problems.pop(code, None)

    def blocking_problem(self) -> Optional[dict]:
        for code in ("privacy", "muted", "no_device", "open_failed", "silent", "no_data"):
            if code in self.problems:
                return self.problems[code]
        return None

    def _hint(self, code: str, **extra):
        now = time.time()
        if now - self._hint_at.get(code, 0.0) < HINT_EVERY_S:
            return
        self._hint_at[code] = now
        self._emit_soon({"type": "voice_hint", "code": code, **extra})

    def _note_error(self, where: str, e: BaseException):
        self.errors += 1
        self.last_error = f"{where}: {e!r}"[:300]
        key = f"{where}:{type(e).__name__}:{e}"
        if key not in self._seen_errors and len(self._seen_errors) < 50:
            self._seen_errors.add(key)
            self.log(f"error in {where} (the voice keeps running): "
                     + "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:])

    async def _set_state(self, state: str, announce: bool = True):
        if state == self.state:
            return
        self.state = state
        if announce and self.on_state:
            try:
                await _maybe_await(self.on_state(state))
            except Exception:
                pass

    # ---------------------------------------------------------- speaking #
    def enqueue(self, text: str, lang: str = "en"):
        """Queue a sentence to speak (non-blocking)."""
        if not self.available or self._speech is None or not (text or "").strip():
            return
        if self.tts_info.get("state") == "failed":
            return                      # the overlay speaks with its own voice instead
        self._speech.put_nowait((text, lang, self._speech_gen))

    def stop_speaking(self):
        if not self.available:
            return
        self._pending_capture = False
        self._speech_gen += 1
        if self._speech is not None:
            while not self._speech.empty():
                try:
                    self._speech.get_nowait()
                except asyncio.QueueEmpty:
                    break
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception:
                pass
        if self.state == "speaking":
            self._spoke_until = time.monotonic()

    async def _wait_for_speaker(self, timeout: float = 30.0) -> bool:
        t0 = time.monotonic()
        while self.speaker is None and self.tts_info.get("state") != "failed":
            if time.monotonic() - t0 > timeout:
                return False
            await asyncio.sleep(0.1)
        return self.tts_ready()

    async def _speech_worker(self):
        """Speak queued sentences in order. When the speaker supports it, the NEXT sentence is
        synthesised while the current one plays, so there is no pause between sentences."""
        pending = None                                   # (text, lang, gen, future) synthesised ahead
        while self._running:
            try:
                if pending is not None:
                    text, lang, gen, fut = pending
                    pending = None
                else:
                    text, lang, gen = await self._speech.get()
                    fut = None
                if not await self._wait_for_speaker():
                    if fut is not None:
                        fut.cancel()
                    await self._speech_finished(None)
                    continue
                pipelined = hasattr(self.speaker, "prepare") and hasattr(self.speaker, "play_prepared")
                if fut is None and pipelined:
                    fut = self._prepare(text, lang)
                if gen != self._speech_gen:                  # interrupted after it was queued
                    if fut is not None:
                        fut.cancel()
                    await self._speech_finished(pending)
                    continue
                self.current_text = text
                if self.vad is not None:
                    self.vad.reset()
                await self._set_state("speaking")
                if not pipelined:
                    await self._loop.run_in_executor(None, self.speaker.speak, text, lang)
                else:
                    try:
                        prepared = await fut
                    except Exception:
                        prepared = None
                    if gen == self._speech_gen and prepared is not None:
                        self.speaker.reset_stop()
                        play = self._loop.run_in_executor(None, self.speaker.play_prepared, prepared)
                        while not play.done():
                            if pending is None and not self._speech.empty():
                                nt, nl, ng = self._speech.get_nowait()
                                pending = (nt, nl, ng, self._prepare(nt, nl))
                            await asyncio.wait([play], timeout=0.03)
                        try:
                            play.result()
                        except Exception:
                            pass
                    if pending is None and not self._speech.empty():
                        nt, nl, ng = self._speech.get_nowait()
                        pending = (nt, nl, ng, self._prepare(nt, nl))
                self.current_text = ""
                self._spoke_until = time.monotonic()
                await self._speech_finished(pending)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._note_error("speaking", e)
                self.current_text = ""
                pending = None
                await asyncio.sleep(0.05)

    async def _speech_finished(self, pending):
        """Nothing left to say: listen for the command that was waiting, or go back to idle."""
        stale = pending is not None and pending[2] != self._speech_gen
        if (pending is None or stale) and self._speech.empty():
            if self._pending_capture:
                self._pending_capture = False
                await self._enter_capture(self._pending_kind)
            elif self.state == "speaking":
                await self._set_state("idle" if self.listening else "muted")

    def _prepare(self, text: str, lang: str):
        return self._loop.run_in_executor(None, self.speaker.prepare, text, lang)

    # ----------------------------------------------------------- capture #
    def begin_capture(self, after_speech: bool = True, kind: Optional[str] = None) -> bool:
        """Listen for one command now, or right after current speech finishes. Returns False
        (and reports why) when the microphone cannot hear anything. kind="answer": the reply to
        a question JARVIS asked (yes / no to a permission prompt), no wake phrase needed."""
        if not self.available or not self.listening:
            return False
        kind = kind or ("wake" if after_speech else "ptt")
        if self.blocking_problem() is not None:
            self._schedule(self._unheard("mic_problem", None, kind=kind))
            return False
        speaking = self.state == "speaking" or (self._speech is not None and not self._speech.empty())
        if after_speech and speaking:
            self._pending_capture, self._pending_kind = True, kind
        else:
            self._schedule(self._enter_capture(kind))
        return True

    def _schedule(self, coro):
        """Run a coroutine on the engine's loop from either that loop or another thread."""
        if self._loop is None:
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            asyncio.ensure_future(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _enter_capture(self, kind: str = "ptt"):
        left = self._spoke_until + ECHO_TAIL_S - time.monotonic()
        guard = max(0, int(round(left / 0.02))) if left > 0 else 0
        self._cap = _Capture(kind, guard)
        if self.meter is not None:
            self.meter.reset_stats()
        await self._set_state("capture")

    def set_listening(self, enabled: bool):
        """AI-mic mute (requirement 6). Muting closes the microphone stream entirely."""
        self.listening = bool(enabled)
        if not enabled:
            self._pending_capture = False
            self._cap = None
        else:
            self._next_open = 0.0
        self._schedule(self._set_state("idle" if enabled else "muted"))

    def _speechlike(self, frame: bytes, db: float, margin: float) -> bool:
        floor = self.meter.floor_db if self.meter is not None else -60.0
        if db < floor + margin:
            return False
        if self.vad is not None and getattr(self.vad, "available", False):
            return bool(self.vad.is_speech(frame))
        return db >= floor + 12.0                       # no VAD: loudness alone

    async def _capture_frame(self, frame: bytes, db: float) -> bool:
        """Feed one frame to the command being recorded. True when the capture has ended."""
        c = self._cap
        c.frames += 1
        if not c.onset:
            if c.frames <= c.guard:
                return False                            # JARVIS's own voice may still be ringing
            speech = self._speechlike(frame, db, ONSET_MARGIN_DB)
            c.window.append(speech)
            c.pre.append(frame)
            c.voiced += speech
            if sum(c.window) >= ONSET_MIN:
                c.onset = True
                c.audio = bytearray(b"".join(c.pre))
                c.speech_frames = sum(c.window)
                c.silence = 0
            elif c.frames - c.guard >= NO_SPEECH_FRAMES:
                await self._end_capture("no_speech")
                return True
            return False
        c.audio += frame
        if self._speechlike(frame, db, CONT_MARGIN_DB):
            c.speech_frames += 1
            c.silence = 0
        else:
            c.silence += 1
        if c.silence >= self.end_silence_frames or c.frames >= MAX_CAPTURE_FRAMES:
            if c.speech_frames < MIN_SPEECH_FRAMES:
                await self._end_capture("too_short")
            else:
                keep = len(c.audio) - max(0, c.silence - 10) * len(frame)   # trim trailing silence
                await self._end_capture(None, bytes(c.audio[:max(keep, len(frame))]))
            return True
        return False

    async def _end_capture(self, reason: Optional[str], audio: Optional[bytes] = None):
        c, self._cap = self._cap, None
        if self.detector:
            self.detector.reset()
        self._utt_reset()
        if audio is None:
            await self._set_state("idle")
            await self._unheard(reason or "no_speech", c)
            return
        await self._set_state("transcribing")
        asyncio.ensure_future(self._transcribe_and_dispatch(audio, c))

    async def _transcribe_and_dispatch(self, audio: bytes, c: _Capture):
        try:
            stt = self.stt
            if stt is None or getattr(stt, "state", "ready") in ("idle", "loading", "downloading"):
                self._hint("stt_loading")
                t0 = time.monotonic()
                while (self.stt is None or getattr(self.stt, "state", "ready") in ("idle", "loading", "downloading")) \
                        and time.monotonic() - t0 < 240:
                    await asyncio.sleep(0.25)
                stt = self.stt
            if stt is None or not getattr(stt, "available", True):
                if self.state == "transcribing":
                    await self._set_state("idle")
                await self._unheard("stt_unavailable", c, detail=getattr(stt, "error", "") or "not loaded")
                return
            a = self._np.frombuffer(audio, dtype=self._np.int16).astype(self._np.float32) / 32768.0
            if hasattr(stt, "transcribe_detail"):
                r = await self._loop.run_in_executor(None, stt.transcribe_detail, a, SR)
            else:
                text, lang = await self._loop.run_in_executor(None, stt.transcribe, a, SR)
                r = {"text": text, "lang": lang, "dropped": None if text.strip() else "empty"}
            text = (r.get("text") or "").strip()
            if not text:
                if self.state == "transcribing":
                    await self._set_state("idle")
                await self._unheard("empty", c, detail=r.get("error") or r.get("dropped"))
                return
            self.last_command = {"text": text, "lang": r.get("lang"), "at": time.time(),
                                 "seconds": r.get("seconds"), "kind": c.kind if c else None}
            self.last_heard = {"text": text, "final": True, "at": time.time(), "by": "whisper"}
            self.stats["commands"] += 1
            if self.state == "transcribing":
                self.state = "idle"          # quietly: the command's own states follow at once
            await _maybe_await(self.on_command(text))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._note_error("transcription", e)
            if self.state == "transcribing":
                await self._set_state("idle")

    async def _unheard(self, reason: str, c: Optional[_Capture], detail=None, kind: Optional[str] = None):
        """A capture ended without a command: tell the server, which says something useful."""
        m = self.meter
        info = {"reason": reason, "kind": kind or (c.kind if c else "ptt"), "detail": detail,
                "voiced_frames": c.voiced if c else 0,
                "loudest_db": round(m.loudest_db, 1) if m else None,
                "floor_db": round(m.floor_db, 1) if m else None,
                "problem": (self.blocking_problem() or {}).get("code")}
        self.last_unheard = dict(info, at=time.time())
        self.stats["unheard"] += 1
        self.log(f"no command heard ({reason}{': ' + str(detail) if detail else ''}; "
                 f"loudest {info['loudest_db']} dB, floor {info['floor_db']} dB)")
        await self._emit({"type": "voice_unheard", **info})

    # -------------------------------------------------------------- mic #
    @property
    def _stream(self):
        """The raw sounddevice stream (tests drive its callback directly)."""
        return self.mic.stream if self.mic is not None else None

    def _on_frame(self, frame: bytes):
        loop, q = self._loop, self._frames
        if loop is not None and q is not None:
            try:
                loop.call_soon_threadsafe(q.put_nowait, frame)
            except RuntimeError:
                pass                       # the loop has closed

    def _close_stream(self):
        if self.mic is not None:
            self.mic.close()

    async def _open_mic(self):
        now = time.monotonic()
        if now < self._next_open:
            await asyncio.sleep(0.2)
            return
        idle = self.state in ("idle", "muted") and (self._speech is None or self._speech.empty())
        if self._open_failures and self._open_failures % 2 == 0 and idle:
            # a microphone plugged in since start-up is invisible until the audio system re-scans
            await self._loop.run_in_executor(None, refresh_portaudio, self._sd)
        ok = await self._loop.run_in_executor(None, self.mic.open)
        if ok:
            self._open_failures = 0
            self.meter.reset()
            self._silence_checked = False
            for code in ("no_device", "open_failed"):
                self._clear_problem(code)              # "no_data" clears once sound actually arrives
            await self._emit({"type": "voice_status"})
            return
        self._open_failures += 1
        self._next_open = time.monotonic() + (2.0, 5.0, 10.0)[min(self._open_failures - 1, 2)]
        reason = self.mic.reason or self.mic.error
        if "no microphone was found" in reason:
            self._set_problem("no_device", reason)
            self._clear_problem("open_failed")
        else:
            self._set_problem("open_failed", reason)
            self._clear_problem("no_device")

    async def _watch(self):
        """Microphone health: dead streams, digital silence (muted / privacy-blocked)."""
        mic, meter = self.mic, self.meter
        now = time.monotonic()
        if mic.stalled(STALL_S):
            self._set_problem("no_data", mic.info.get("name") or "")
            if mic.has_alternatives() and not self._settled:
                mic.abandon("no sound arrives")
            else:
                mic.close()
                self._next_open = now + 2.0
            return
        if meter.zero_run >= SILENT_FRAMES and not self._silence_checked:
            self._silence_checked = True
            self._privacy, self._endpoint = await self._loop.run_in_executor(
                None, lambda: (privacy_status(), endpoint_status()))
            if self._privacy.get("blocked"):
                self._set_problem("privacy", ", ".join(self._privacy.get("which", [])))
            elif self._endpoint.get("muted"):
                self._set_problem("muted", str(self._endpoint.get("name") or ""))
            elif mic.has_alternatives() and not self._settled:
                mic.abandon("only silence")             # try the next microphone
            else:
                self._set_problem("silent", str(mic.info.get("name") or ""))
                if not mic.info.get("default") and not mic.locked_to_choice and not self._settled:
                    mic.bad.clear()                     # settle back on the default device
                    self._reopen = True
                self._settled = True
            self._last_recheck = now
        elif meter.zero_run == 0 and meter.frames > 5:
            if self._silence_checked:
                self._silence_checked = False
                self._settled = False
            for code in ("privacy", "muted", "silent"):
                if code in self.problems:
                    self._clear_problem(code)
        if meter.frames > 5 and "no_data" in self.problems:
            self._clear_problem("no_data")             # frames are arriving again
        # while silenced, look again every 10 s: once the switch is flipped, reopen the stream
        if self._silence_checked and now - self._last_recheck > 10.0:
            self._last_recheck = now
            priv, ep = await self._loop.run_in_executor(None, lambda: (privacy_status(), endpoint_status()))
            self._privacy, self._endpoint = priv, ep
            if "privacy" in self.problems and not priv.get("blocked"):
                self._reopen = True
            elif "silent" in self.problems:
                self._reopen = True

    # ------------------------------------------------------------- loop #
    async def run(self):
        if not self.available:
            return
        self._loop = asyncio.get_running_loop()
        self._frames, self._speech = asyncio.Queue(), asyncio.Queue()
        self._running = True
        worker = asyncio.ensure_future(self._speech_worker())
        try:
            while self._running:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._note_error("voice loop", e)
                    await asyncio.sleep(0.3)
        finally:
            worker.cancel()
            self._close_stream()

    async def _tick(self):
        # privacy: keep the microphone closed while JARVIS is muted
        if not self.listening:
            if self.mic.is_open:
                self.mic.close()
                self.meter.reset()
            await asyncio.sleep(0.2)
            return
        if self._reopen:
            self._reopen = False
            self.mic.close()
        if not self.mic.is_open:
            await self._open_mic()
            return
        if self._provisional is not None and self.state == "idle" \
                and time.monotonic() - self._provisional >= PROVISIONAL_S:
            self._provisional = None
            await self._do_wake("heard 'wake up jarvis'")
        try:
            frame = await asyncio.wait_for(self._frames.get(), timeout=0.25 if self._provisional else 0.5)
        except asyncio.TimeoutError:
            await self._watch()
            return
        frames = [frame]
        while len(frames) < BATCH_FRAMES and not self._frames.empty():
            frames.append(self._frames.get_nowait())
        if self.state == "idle" and self._frames.qsize() > MAX_BACKLOG_FRAMES:
            # recognition fell far behind (CPU busy): keep the last 0.2 s and carry on
            skipped = len(frames)
            while self._frames.qsize() > 10:
                self._frames.get_nowait()
                skipped += 1
            self.stats["dropped_frames"] += skipped
            if self.detector:
                self.detector.reset()
            self._utt_reset()
            return
        start_state = self.state
        isolated = False
        for f in frames:
            db = self.meter.update(f)
            self._ring.append(f)
            self._frame_no += 1
            if self.state == "idle":
                isolated = self._track_phrase(f, db) or isolated
            else:
                self._reset_phrase()          # a phrase is only followed while idle
                if self.state == "capture" and self._cap is not None:
                    await self._capture_frame(f, db)
        await self._watch()
        if start_state == "idle" and self.state == "idle":
            await self._idle_frames(frames, isolated)
        elif start_state == "speaking" and self.state == "speaking":
            if await self._barge_in(frames):
                self.stop_speaking()
                await _maybe_await(self.on_interrupt())
                if self.detector:
                    self.detector.reset()
                await self._enter_capture("barge")
        if self._frame_no - self._last_level_frame >= LEVEL_EVERY:
            self._last_level_frame = self._frame_no
            m = self.meter
            self._emit_soon({"type": "mic", "level": round(m.level(), 3), "db": round(m.db, 1),
                             "floor": round(m.floor_db, 1), "silent": m.zero_run >= 50,
                             "speech": bool(self._cap is not None and self._cap.onset),
                             "state": self.state})

    # ------------------------------------------------------------- wake #
    def _utt_reset(self):
        self._utt_flag, self._utt_start = False, None

    def _reset_phrase(self):
        if self._seg["active"] or self._seg_hist:
            self._seg.update(active=False, frames=0, silence=0, pre=0)
            self._seg_hist.clear()

    def _track_phrase(self, frame: bytes, db: float) -> bool:
        """Follow speech while idle. True when a short phrase said on its own just ended."""
        s, hist = self._seg, self._seg_hist
        sp = self._speechlike(frame, db, ONSET_MARGIN_DB)
        hist.append(sp)
        if not s["active"]:
            recent = list(hist)[-ONSET_WINDOW:]
            if sum(recent) >= ONSET_MIN:
                before = list(hist)[:-ONSET_WINDOW]
                quiet = 0
                for v in reversed(before):
                    if v:
                        break
                    quiet += 1
                s.update(active=True, frames=len(recent), silence=0, pre=quiet)
            return False
        s["frames"] += 1
        s["silence"] = 0 if sp else s["silence"] + 1
        if s["silence"] < SEGMENT_END:
            return False
        length = s["frames"] - s["silence"]
        s.update(active=False, frames=0, silence=0)
        return s["pre"] >= ISOLATED_QUIET and ISOLATED_MIN <= length <= ISOLATED_MAX

    def _stt_ready(self) -> bool:
        stt = self.stt
        return stt is not None and getattr(stt, "available", False) and hasattr(stt, "transcribe_detail")

    def _can_verify(self) -> bool:
        """For a short phrase Vosk made nothing of: rate-limited, as it can be any speech."""
        return (self._stt_ready() and not self._verifying
                and time.monotonic() - self._last_verify_at >= VERIFY_EVERY_S)

    def _verify_candidate(self, start, heard: str):
        """For something Vosk heard as part of the wake phrase: checked now, or right after the
        check that is running ("Jarvis ... Jarvis!" said quickly must reach Whisper twice)."""
        if not self._stt_ready():
            return
        if self._verifying:
            self._pending_verify = (self._frame_no, heard)
        else:
            self._start_verify(start, heard)

    def _start_verify(self, start, heard: str):
        # at least the last 3 s: an accent can make Vosk split the phrase in two
        # ("[unk]" ... "jarvis"), and Whisper needs to hear the "wake up" part too
        n = self._frame_no - (start or self._frame_no) + 10
        n = min(len(self._ring), max(VERIFY_MIN_FRAMES, n + 60))
        audio = b"".join(list(self._ring)[-n:])
        self._verifying = True
        self._last_verify_at = time.monotonic()
        asyncio.ensure_future(self._verify(audio, heard))

    async def _idle_frames(self, frames, isolated: bool = False):
        res = await self._detect(b"".join(frames)) if self.detector else None
        if not res:
            if isolated and self.state == "idle" and self._can_verify():
                self.stats["isolated_checks"] += 1
                self._start_verify(None, "(a short phrase)")
            return
        text, final = res
        if final and wake_candidate(text):            # noise also comes out as "hold" or "stop"
            self.last_heard = {"text": text, "final": final, "at": time.time(), "by": "wake listener"}
        if self._utt_start is None:
            self._utt_start = self._frame_no - len(frames) - 30      # ~0.6 s before the first result
        self._utt_flag = self._utt_flag or wake_candidate(text)
        if not final:
            # a partial "wake up jarvis" wakes only if it still says so 0.4 s later
            if exact_wake(text, self.lenient):
                self._provisional = self._provisional or time.monotonic()
            else:
                self._provisional = None
        if final:
            self._provisional = None
            candidate, start = self._utt_flag, self._utt_start
            self._utt_reset()
            if exact_wake(text, self.lenient):
                await self._do_wake(f"heard '{text}'")
            elif candidate and self._stt_ready():
                self._verify_candidate(start, text)       # e.g. "[unk] wake up jarvis [unk]"
            elif heard_wake(text, self.lenient) or loose_wake(text):
                await self._do_wake(f"heard '{text}'")      # Whisper not loaded yet: Vosk decides
            elif isolated and self._can_verify():
                self.stats["isolated_checks"] += 1
                self._start_verify(start, text)
        elif isolated and self._can_verify():
            self.stats["isolated_checks"] += 1
            self._start_verify(None, text)

    async def _verify(self, audio: bytes, heard: str):
        """A near miss: let Whisper listen to the utterance and decide."""
        try:
            self.stats["verifications"] += 1
            a = self._np.frombuffer(audio, dtype=self._np.int16).astype(self._np.float32) / 32768.0
            r = await self._loop.run_in_executor(None, self.stt.transcribe_detail, a, SR)
            said = (r.get("text") or "").strip()
            verdict = wake_verdict(said)
            if said:
                self.last_heard = {"text": said, "final": True, "at": time.time(), "by": "whisper"}
            if self.state != "idle" or not self.listening:
                return
            if verdict == "wake" or (verdict == "name" and self.lenient):
                await self._do_wake(f"Whisper heard '{said}'")
            elif verdict == "name":
                now = time.time()
                if now - self._last_name_at <= NAME_REPEAT_S:
                    self._last_name_at = 0.0
                    await self._do_wake(f"name said twice ('{said}')")
                else:
                    self._last_name_at = now
                    self._hint("near_miss", heard=said)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._note_error("wake check", e)
        finally:
            self._verifying = False
            pending, self._pending_verify = self._pending_verify, None
            if pending is not None and self.state == "idle" and self.listening:
                self._start_verify(None, pending[1])

    async def _do_wake(self, why: str):
        if self.detector:
            self.detector.reset()
        self._utt_reset()
        self._provisional = None
        self.stats["wakes"] += 1
        self.last_wake = {"at": time.time(), "why": why}
        self.log(f"wake ({why})")
        await _maybe_await(self.on_wake())
        if self.state == "idle" and not self._pending_capture and self.listening:
            await self._enter_capture("wake")       # nothing to say first: listen now

    async def _barge_in(self, frames) -> bool:
        if self.barge_mode == "off":
            return False
        if self.barge_mode == "vad":
            return any([self.vad.feed(f) for f in frames]) if self.vad is not None else False
        if not self.detector:
            return False
        res = await self._detect(b"".join(frames))
        if not getattr(self.detector, "knows_keywords", False):
            return bool(res and heard_wake(res[0], lenient=True))
        if not res:
            return False
        text, final = res
        kw = heard_keyword(text, self.keywords, spoken_now=self.current_text)
        if not kw:
            self._kw_hits.clear()
            return False
        if final:
            self._kw_hits.clear()
            return True
        self._kw_hits[kw] = self._kw_hits.get(kw, 0) + 1
        return self._kw_hits[kw] >= PARTIAL_KEYWORD_FRAMES

    async def _detect(self, audio: bytes):
        det = self.detector
        if not det:
            return None
        return await self._loop.run_in_executor(self._audio_pool, det.process, audio)

    # ----------------------------------------------------------- report #
    def status(self) -> dict:
        """Everything the HUD, the launcher and 'check the microphone' need to know."""
        mic = self.mic.describe() if self.mic is not None else {}
        m = self.meter
        if m is not None:
            mic.update(level_db=round(m.db, 1), floor_db=round(m.floor_db, 1),
                       silent_seconds=round(m.silent_seconds, 1))
        if self._privacy is not None:
            mic["privacy"] = self._privacy
        if self._endpoint is not None:
            mic["endpoint"] = self._endpoint
        stt = self.stt
        if stt is not None and hasattr(stt, "describe"):
            stt_info = stt.describe()
        else:
            stt_info = {"state": "ready" if stt is not None else "loading"}
        return {"state": self.state, "listening": self.listening, "mic": mic,
                "wake": dict(self.wake_info, lenient=self.lenient), "stt": stt_info,
                "tts": dict(self.tts_info), "problems": list(self.problems.values()),
                "blocking": (self.blocking_problem() or {}).get("code"),
                "last_heard": self.last_heard, "last_command": self.last_command,
                "last_unheard": self.last_unheard, "last_wake": self.last_wake,
                "errors": self.errors, "last_error": self.last_error or None, "stats": dict(self.stats)}

    def stop(self):
        self.stopped = True
        self._running = False
        if self.available:
            self.stop_speaking()
        self._close_stream()
