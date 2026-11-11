"""
tests/test_voice_engine.py -- exercises the voice state machine with fake audio devices.

    python tests/test_voice_engine.py        (or: python -m pytest tests/)

No microphone, speakers or models are needed: sounddevice, the wake detector, Whisper and
the speaker are replaced with fakes so the real engine logic (queueing, capture timing,
barge-in rules, mute) runs end to end.
"""
import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- fakes ----
class FakeStream:
    opened = 0

    def __init__(self, **kw):
        self.callback = kw["callback"]
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

from voice.engine import VoiceEngine  # noqa: E402

FRAME = bytes(640)


class FakeDetector:
    knows_keywords = True

    def __init__(self):
        self.script = {}      # frame index -> (text, final)
        self.i = 0

    def process(self, frame):
        self.i += 1
        return self.script.pop(self.i, None)

    def reset(self):
        pass


class FakeVAD:
    available = True

    def __init__(self):
        self.speech_frames = 0
        self.fed = 0

    def is_speech(self, frame):
        if self.speech_frames > 0:
            self.speech_frames -= 1
            return True
        return False

    def reset(self):
        pass

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
    def __init__(self, text):
        self.text, self.calls = text, 0

    def transcribe(self, audio, sr=16000):
        self.calls += 1
        return self.text, "en"


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

    eng = VoiceEngine(config, on_wake, on_command, on_interrupt, on_state)
    assert eng.available, "engine should be available with the fake sounddevice"
    eng.detector, eng.vad = FakeDetector(), FakeVAD()
    eng.speaker, eng.stt = FakeSpeaker(speak_seconds), FakeSTT(stt_text)
    return eng


async def feed(eng, n, delay=0.002):
    for _ in range(n):
        while eng._stream is None:
            await asyncio.sleep(0.005)
        eng._stream.callback(FRAME, 320, None, None)
        await asyncio.sleep(delay)


async def wait_for(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


# ---------------------------------------------------------------- tests ----
def test_wake_speaks_then_listens_then_commands():
    async def run():
        ev = []
        eng = make_engine(ev)
        task = asyncio.ensure_future(eng.run())
        eng.detector.script[3] = ("wake up jarvis", False)
        await feed(eng, 5)
        assert await wait_for(lambda: eng.state == "capture"), f"never started listening: {ev}"
        assert eng.speaker.said == ["Jarvis at your service sir"]
        eng.vad.speech_frames = 12          # the user speaks ...
        await feed(eng, 12 + 45)            # ... then is silent for 0.9 s
        assert await wait_for(lambda: ("command", "open notepad") in ev), ev
        assert eng.state == "idle"
        eng.stop(); task.cancel()
    asyncio.run(run())


def test_keyword_barge_in_and_echo_immunity():
    async def run():
        ev = []
        eng = make_engine(ev, speak_seconds=2.0)
        task = asyncio.ensure_future(eng.run())
        await feed(eng, 1)
        # JARVIS says a sentence that itself contains "stop": hearing "stop" must NOT interrupt
        eng.enqueue("Please stop by the shop later, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        eng.detector.script[eng.detector.i + 2] = ("stop", True)
        await feed(eng, 4)
        assert "interrupt" not in ev, "echo of JARVIS's own words interrupted it"
        eng.stop_speaking()
        await wait_for(lambda: eng.state != "speaking")
        # now a sentence without the keyword: the user's "stop" interrupts and JARVIS listens
        eng.enqueue("The weather today is mild with a gentle breeze, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        eng.detector.script[eng.detector.i + 2] = ("stop", True)
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
        await feed(eng, 1)
        eng.enqueue("A rather long answer, sir.")
        assert await wait_for(lambda: eng.state == "speaking")
        eng.vad.speech_frames = 30
        await feed(eng, 10)
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
        await feed(eng, 1)
        eng.begin_capture(after_speech=False)          # overlay mic button
        assert await wait_for(lambda: eng.state == "capture")
        eng.vad.speech_frames = 8
        await feed(eng, 8 + 45)
        assert await wait_for(lambda: ("command", "what time is it") in ev), ev
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
