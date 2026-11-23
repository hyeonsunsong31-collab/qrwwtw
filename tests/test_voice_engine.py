"""
tests/test_voice_engine.py -- exercises the voice state machine with fake audio devices.

    python tests/test_voice_engine.py        (or: python -m pytest tests/)

No microphone, speakers or models are needed: sounddevice, the wake detector, Whisper and
the speaker are replaced with fakes so the real engine logic (queueing, capture timing,
barge-in rules, mute, microphone health) runs end to end. Frames carry realistic audio: a
quiet noise floor, louder "speech", or the exact digital silence of a muted microphone.
"""
import asyncio
import os
import sys
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- fakes ----
class FakeStream:
    opened = 0
    fail_with = None                 # set to an exception message to make opening fail

    def __init__(self, **kw):
        if FakeStream.fail_with:
            raise RuntimeError(FakeStream.fail_with)
        self.callback = kw["callback"]
        self.kw = kw
        FakeStream.opened += 1

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


fake_sd = types.ModuleType("sounddevice")
fake_sd.RawInputStream = FakeStream
fake_sd.play = lambda *a, **k: None
fake_sd.wait = lambda *a, **k: None
fake_sd.stop = lambda *a, **k: None
sys.modules["sounddevice"] = fake_sd

import voice.engine as engine_mod  # noqa: E402
from voice.engine import VoiceEngine  # noqa: E402

# the tests feed frames in bursts; a real stream never pauses, so only the stall test uses 2.5 s
engine_mod.STALL_S = 30.0

_rng = np.random.default_rng(7)
SILENCE = bytes(640)                                      # a muted / privacy-blocked microphone


def noise_frame(level=30.0) -> bytes:                     # about -60 dBFS: a quiet room
    return (_rng.standard_normal(320) * level).astype(np.int16).tobytes()


def speech_frame() -> bytes:                              # about -14 dBFS: someone talking
    t = np.arange(320) / 16000.0
    x = 4000 * np.sin(2 * np.pi * 220 * t) + 2000 * np.sin(2 * np.pi * 660 * t) + _rng.standard_normal(320) * 300
    return x.astype(np.int16).tobytes()


class FakeDetector:
    knows_keywords = True

    def __init__(self):
        self.queue = []          # results handed out by the next process() calls
        self.calls = 0
        self.fail = False

    def say(self, text, final=False):
        self.queue.append((text, final))

    def process(self, audio):
        self.calls += 1
        if self.fail:
            raise ValueError("recogniser exploded")
        return self.queue.pop(0) if self.queue else None

    def reset(self):
        pass


class FakeVAD:
    """Speech = loud frame (like webrtcvad on these synthetic frames)."""
    available = True

    def __init__(self):
        self.fed = 0

    def is_speech(self, frame):
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(x * x))) > 600.0

    def reset(self):
        self.fed = 0

    def feed(self, frame):
        self.fed += 1
        return self.is_speech(frame) and self.fed > 3


class FakeSpeaker:
    available = True

    def __init__(self, seconds=0.05):
        self.seconds, self.said, self.stopped, self.rate = seconds, [], 0, 1.0
        self._stop = False

    def speak(self, text, lang="en"):
        self.said.append(text)
        self._stop = False
        t0 = time.time()
        while time.time() - t0 < self.seconds and not self._stop:
            time.sleep(0.005)

    def stop(self):
        self._stop = True
        self.stopped += 1


class FakeSTT:
    def __init__(self, text, available=True, state="ready"):
        self.text, self.calls, self.available, self.state = text, 0, available, state
        self.error = "" if available else "the speech model could not be downloaded"
        self.verify_text = None           # what transcribe_detail returns for wake checks

    def transcribe_detail(self, audio, sr=16000):
        self.calls += 1
        text = self.verify_text if self.verify_text is not None else self.text
        return {"text": text, "lang": "en", "dropped": None if text else "no speech", "seconds": 0.01}

    def transcribe(self, audio, sr=16000):
        r = self.transcribe_detail(audio, sr)
        return r["text"], r["lang"]


CONFIG = {"voice": {"barge_in": {"mode": "keyword", "keywords": ["stop", "wait", "hold on", "jarvis"]},
                    "wakeword": {"lenient": False}, "tts": {}}}


def make_engine(events, speak_seconds=0.05, stt_text="open notepad", config=CONFIG):
    async def on_wake():
        events.append("wake")
        eng.enqueue("Jarvis at your service sir")
        eng.begin_capture(after_speech=True)

    async def on_command(text):
        events.append(("command", text))

    async def on_interrupt():
        events.append("interrupt")

    async def on_state(s):
        events.append(("state", s))

    async def on_event(ev):
        events.append(("event", ev))

    eng = VoiceEngine(config, on_wake, on_command, on_interrupt, on_state, on_event=on_event,
                      log=lambda m: events.append(("log", m)))
    assert eng.available, "engine should be available with the fake sounddevice"
    eng.detector, eng.vad = FakeDetector(), FakeVAD()
    eng.speaker, eng.stt = FakeSpeaker(speak_seconds), FakeSTT(stt_text)
    eng.tts_info = {"state": "ready", "backend": "fake"}
    return eng


async def feed(eng, n, kind="noise", delay=0.002):
    for _ in range(n):
        while eng._stream is None:
            await asyncio.sleep(0.005)
        frame = {"noise": noise_frame, "speech": speech_frame}.get(kind, lambda: SILENCE)()
        eng._stream.callback(frame, 320, None, None)
        await asyncio.sleep(delay)


async def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def events_of(ev, typ):
    return [e[1] for e in ev if isinstance(e, tuple) and e[0] == "event" and e[1].get("type") == typ]


async def settle(eng, frames=40):
    """Give the noise-floor estimate something to work with."""
    await feed(eng, frames)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------- tests ----
def test_wake_speaks_then_listens_then_commands():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.detector.say("wake up jarvis", False)
        await feed(eng, 3)
        assert await wait_for(lambda: eng.state == "capture"), f"never started listening: {ev}"
        assert eng.speaker.said == ["Jarvis at your service sir"]
        await feed(eng, 15)                  # the room after JARVIS speaks
        await feed(eng, 14, "speech")        # the user speaks ...
        await feed(eng, 45)                  # ... then is silent for 0.9 s
        assert await wait_for(lambda: ("command", "open notepad") in ev), ev
        assert eng.state == "idle"
        assert eng.stats["wakes"] == 1 and eng.stats["commands"] == 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_echo_right_after_jarvis_speaks_is_not_a_command():
    async def run():
        ev = []
        eng = make_engine(ev, speak_seconds=0.02)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.enqueue("Jarvis at your service sir")
        eng.begin_capture(after_speech=True)
        assert await wait_for(lambda: eng.state == "capture")
        guard = eng._cap.guard
        assert guard >= 8, f"the echo guard should cover ~0.25 s after speech, got {guard} frames"
        await feed(eng, min(guard, 8), "speech")          # JARVIS's voice ringing in the room
        await feed(eng, 60)
        assert ("command", "open notepad") not in ev and eng.stt.calls == 0, "echo was taken as a command"
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_stray_noise_never_starts_a_command():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)
        assert await wait_for(lambda: eng.state == "capture")
        for _ in range(64):                               # a click every 5th frame: never 4 of 8
            await feed(eng, 1, "speech")
            await feed(eng, 4)
        await feed(eng, 10)
        assert await wait_for(lambda: events_of(ev, "voice_unheard")), ev[-5:]
        u = events_of(ev, "voice_unheard")[0]
        assert u["reason"] == "no_speech" and u["voiced_frames"] > 0, u
        assert eng.stt.calls == 0 and not any(e[0] == "command" for e in ev if isinstance(e, tuple))
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_a_blip_is_too_short_to_be_a_command():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)
        assert await wait_for(lambda: eng.state == "capture")
        await feed(eng, 6, "speech")
        await feed(eng, 40)
        assert await wait_for(lambda: events_of(ev, "voice_unheard"))
        assert events_of(ev, "voice_unheard")[0]["reason"] == "too_short"
        assert eng.stt.calls == 0
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_unclear_speech_is_reported_not_dropped():
    async def run():
        ev = []
        eng = make_engine(ev, stt_text="")                # Whisper heard nothing usable
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)
        await feed(eng, 20, "speech")
        await feed(eng, 40)
        assert await wait_for(lambda: events_of(ev, "voice_unheard"))
        assert events_of(ev, "voice_unheard")[0]["reason"] == "empty"
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_keyword_barge_in_and_echo_immunity():
    async def run():
        ev = []
        eng = make_engine(ev, speak_seconds=2.0)
        task = asyncio.ensure_future(eng.run())
        await settle(eng, 5)
        # JARVIS says a sentence that itself contains "stop": hearing "stop" must NOT interrupt
        eng.enqueue("Please stop by the shop later, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        eng.detector.say("stop", True)
        await feed(eng, 4)
        assert "interrupt" not in ev, "echo of JARVIS's own words interrupted it"
        eng.stop_speaking()
        await wait_for(lambda: eng.state != "speaking")
        # now a sentence without the keyword: the user's "stop" interrupts and JARVIS listens
        eng.enqueue("The weather today is mild with a gentle breeze, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        eng.detector.say("stop", True)
        await feed(eng, 4)
        assert await wait_for(lambda: "interrupt" in ev), ev
        assert await wait_for(lambda: eng.state == "capture")
        assert eng.speaker.stopped >= 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_vad_barge_in_mode():
    async def run():
        ev = []
        cfg = {"voice": {"barge_in": {"mode": "vad"}, "wakeword": {}, "tts": {}}}
        eng = make_engine(ev, speak_seconds=2.0, config=cfg)
        task = asyncio.ensure_future(eng.run())
        await settle(eng, 5)
        eng.enqueue("A rather long answer, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        await feed(eng, 10, "speech")
        assert await wait_for(lambda: "interrupt" in ev), ev
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_mute_closes_microphone_and_unmute_reopens():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await feed(eng, 2)
        assert eng._stream is not None
        opened = FakeStream.opened
        eng.set_listening(False)
        assert await wait_for(lambda: eng._stream is None), "mic stayed open while muted"
        assert await wait_for(lambda: eng.state == "muted")
        eng.set_listening(True)
        assert await wait_for(lambda: eng._stream is not None)
        assert FakeStream.opened == opened + 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_push_to_talk_captures_immediately():
    async def run():
        ev = []
        eng = make_engine(ev, stt_text="what time is it")
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)          # overlay mic button
        assert await wait_for(lambda: eng.state == "capture")
        assert eng._cap.guard == 0, "nothing was spoken: no echo guard needed"
        await feed(eng, 10, "speech")
        await feed(eng, 45)
        assert await wait_for(lambda: ("command", "what time is it") in ev), ev
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_capture_waits_for_whisper_still_loading():
    async def run():
        ev = []
        eng = make_engine(ev, stt_text="open notepad")
        eng.stt.state = "loading"
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)
        await feed(eng, 12, "speech")
        await feed(eng, 40)
        assert await wait_for(lambda: any(h.get("code") == "stt_loading" for h in events_of(ev, "voice_hint")))
        assert ("command", "open notepad") not in ev
        eng.stt.state = "ready"                         # Whisper finished loading
        assert await wait_for(lambda: ("command", "open notepad") in ev, timeout=4), ev[-6:]
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_whisper_unavailable_is_reported():
    async def run():
        ev = []
        eng = make_engine(ev)
        eng.stt = FakeSTT("", available=False, state="failed")
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.begin_capture(after_speech=False)
        await feed(eng, 12, "speech")
        await feed(eng, 40)
        assert await wait_for(lambda: events_of(ev, "voice_unheard"))
        u = events_of(ev, "voice_unheard")[0]
        assert u["reason"] == "stt_unavailable" and "download" in (u["detail"] or ""), u
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_near_miss_wake_is_checked_by_whisper():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        # an accent: Vosk only catches the name; Whisper hears the whole phrase
        eng.stt.verify_text = "Wake up, Jarvis."
        eng.detector.say("[unk] jarvis", True)
        await feed(eng, 3)
        assert await wait_for(lambda: "wake" in ev), ev[-6:]
        assert eng.stats["verifications"] == 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_name_alone_hints_and_twice_wakes():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.stt.verify_text = "Jarvis."
        eng.detector.say("jarvis", True)
        await feed(eng, 3)
        assert await wait_for(lambda: any(h.get("code") == "near_miss" for h in events_of(ev, "voice_hint")))
        assert "wake" not in ev, "the name alone must not wake (unless 'lenient' is on)"
        eng.detector.say("jarvis", True)                 # "Jarvis ... Jarvis!"
        await feed(eng, 3)
        assert await wait_for(lambda: "wake" in ev), ev[-6:]
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_lookalike_words_do_not_wake():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.stt.verify_text = "The service was great."  # Vosk turned "service" into "jarvis"
        eng.detector.say("jarvis", True)
        await feed(eng, 3)
        assert await wait_for(lambda: eng.stats["verifications"] == 1)
        await asyncio.sleep(0.1)
        assert "wake" not in ev and not events_of(ev, "voice_hint")
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_wake_up_early_is_not_a_wake():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        # Vosk's grammar briefly turns "we should wake up early tomorrow" into "wake up jarvis"
        eng.stt.verify_text = "We should wake up early tomorrow."
        eng.detector.say("wake up jarvis", False)
        await feed(eng, 3)
        eng.detector.say("[unk]", False)                 # ... and changes its mind at once
        await feed(eng, 10)
        eng.detector.say("[unk] wake up [unk]", True)
        await feed(eng, 5)
        assert await wait_for(lambda: eng.stats["verifications"] == 1)
        await asyncio.sleep(0.6)
        await feed(eng, 3)
        assert "wake" not in ev, [e for e in ev if e[0] != "event"][-6:]
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_phrase_inside_longer_speech_is_checked_first():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.stt.verify_text = "Wake up, Jarvis, open notepad."
        eng.detector.say("wake up jarvis [unk] [unk]", True)
        await feed(eng, 3)
        assert await wait_for(lambda: "wake" in ev)
        assert eng.stats["verifications"] == 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_reversed_phrase_wakes_without_whisper():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        eng.detector.say("jarvis wake up", True)
        await feed(eng, 3)
        assert await wait_for(lambda: "wake" in ev)
        assert eng.stats["verifications"] == 0
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_digital_silence_is_reported_and_clears():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await feed(eng, 170, "silence")                  # what a muted microphone delivers
        assert await wait_for(lambda: eng.blocking_problem() is not None), ev[-5:]
        assert eng.blocking_problem()["code"] == "silent"
        p = [e for e in events_of(ev, "voice_problem") if e["active"]][0]["problem"]
        assert p["blocking"] and p["fix"] and p["message_ko"]
        # asking to listen now explains instead of recording silence
        assert eng.begin_capture(after_speech=False) is False
        assert await wait_for(lambda: any(u["reason"] == "mic_problem" for u in events_of(ev, "voice_unheard")))
        await feed(eng, 10)                              # sound again
        assert await wait_for(lambda: eng.blocking_problem() is None)
        assert any(not e["active"] for e in events_of(ev, "voice_problem"))
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_privacy_block_is_named():
    async def run():
        saved = engine_mod.privacy_status
        engine_mod.privacy_status = lambda: {"blocked": True, "which": ["desktop_apps"]}
        try:
            ev = []
            eng = make_engine(ev)
            task = asyncio.ensure_future(eng.run())
            await feed(eng, 170, "silence")
            assert await wait_for(lambda: eng.blocking_problem() is not None)
            p = eng.blocking_problem()
            assert p["code"] == "privacy" and p["action"] == "ms-settings:privacy-microphone", p
            assert "Let desktop apps access your microphone" in p["fix"]
            eng.stop(); task.cancel()
        finally:
            engine_mod.privacy_status = saved
    asyncio.run(run())


def test_microphone_that_cannot_open_is_reported_then_recovers():
    async def run():
        ev = []
        FakeStream.fail_with = "Error querying device -1"
        try:
            eng = make_engine(ev)
            task = asyncio.ensure_future(eng.run())
            assert await wait_for(lambda: "no_device" in eng.problems), ev[-5:]
            assert "no microphone was found" in eng.problems["no_device"]["detail"]
            FakeStream.fail_with = None                  # a microphone is plugged in
            eng._next_open = 0.0
            assert await wait_for(lambda: eng._stream is not None, timeout=4)
            assert await wait_for(lambda: "no_device" not in eng.problems)
            eng.stop(); task.cancel()
        finally:
            FakeStream.fail_with = None
    asyncio.run(run())


def test_moves_to_another_microphone_when_one_is_silent():
    async def run():
        ev = []
        sd2 = types.SimpleNamespace(
            RawInputStream=FakeStream,
            query_devices=lambda: [
                {"index": 0, "name": "Steam Streaming Microphone", "hostapi": 0, "max_input_channels": 2,
                 "default_samplerate": 48000.0},
                {"index": 1, "name": "Microphone Array (Realtek(R) Audio)", "hostapi": 0,
                 "max_input_channels": 2, "default_samplerate": 48000.0},
                {"index": 2, "name": "Stereo Mix (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 2,
                 "default_samplerate": 48000.0}],
            query_hostapis=lambda *a: ([{"name": "MME", "default_input_device": 0}] if not a
                                       else {"name": "MME", "default_input_device": 0}),
            default=types.SimpleNamespace(device=[0, -1], hostapi=0))
        eng = make_engine(ev)
        eng._sd = eng.mic.sd = sd2
        task = asyncio.ensure_future(eng.run())
        assert await wait_for(lambda: eng._stream is not None)
        assert eng._stream.kw["device"] == 0, "the default device is tried first"
        await feed(eng, 170, "silence")                  # the virtual default sends nothing
        assert await wait_for(lambda: eng._stream is not None and eng._stream.kw["device"] == 1, timeout=4), \
            eng.mic.describe()
        await feed(eng, 20)
        assert eng.blocking_problem() is None
        assert all(s != 2 for s in [eng._stream.kw["device"]]), "never the loopback device"
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_short_phrase_vosk_missed_is_still_checked_by_whisper():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng, 50)                            # quiet room
        eng.stt.verify_text = "Vaku up, Jarvis."         # a heavy accent: Vosk heard only [unk]
        await feed(eng, 50, "speech")                    # a 1-second phrase on its own ...
        await feed(eng, 30)                              # ... followed by a pause
        assert await wait_for(lambda: "wake" in ev), [e for e in ev if e[0] != "event"][-5:]
        assert eng.stats["isolated_checks"] == 1
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_long_talk_is_not_sent_to_whisper():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng, 50)
        await feed(eng, 200, "speech")                   # 4 s of someone talking: not a wake phrase
        await feed(eng, 30)
        await asyncio.sleep(0.1)
        assert eng.stats["isolated_checks"] == 0 and eng.stt.calls == 0
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_dead_stream_is_noticed_and_reopened():
    async def run():
        saved = engine_mod.STALL_S
        engine_mod.STALL_S = 0.5
        try:
            ev = []
            eng = make_engine(ev)
            task = asyncio.ensure_future(eng.run())
            await feed(eng, 5)
            opened = FakeStream.opened
            await asyncio.sleep(1.2)                     # the device delivers nothing any more
            assert await wait_for(lambda: FakeStream.opened > opened, timeout=5), "never reopened"
            assert any(e["problem"]["code"] == "no_data" for e in events_of(ev, "voice_problem") if e["active"])
            await feed(eng, 12)                          # sound again
            assert await wait_for(lambda: "no_data" not in eng.problems)
            eng.stop(); task.cancel()
        finally:
            engine_mod.STALL_S = saved
    asyncio.run(run())


def test_voice_loop_survives_an_error():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng, 5)
        eng.detector.fail = True
        await feed(eng, 5)
        assert await wait_for(lambda: eng.errors >= 1)
        eng.detector.fail = False
        eng.detector.say("wake up jarvis", False)
        await feed(eng, 5)
        assert await wait_for(lambda: "wake" in ev), "the loop died after an error"
        assert not task.done()
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_status_report_has_everything_the_hud_needs():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        await settle(eng)
        st = eng.status()
        for key in ("state", "mic", "wake", "stt", "tts", "problems", "last_heard", "stats"):
            assert key in st, key
        assert st["mic"]["open"] and st["mic"]["level_db"] < -40
        assert any(e[1].get("type") == "mic" for e in ev if isinstance(e, tuple) and e[0] == "event")
        eng.stop(); task.cancel()
    asyncio.run(run())


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} voice-engine tests passed")


if __name__ == "__main__":
    _run_all()
