"""
tests/test_voice_parts.py -- the v1.4.2 voice pieces, one by one (no microphone or models needed).

    python tests/test_voice_parts.py

Covers: format conversion and device choice (voice/mic.py), Whisper loading order and what
it drops as noise (voice/stt.py), judging a near-miss wake (voice/wakeword.py), the
checksum-pinned repair download (voice/models.py), the "check the microphone" report
(agent/skills/voicecheck.py), the microphone setting, and how the server turns voice events
into toasts and spoken explanations (agent/server.py).
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# =========================================================================== mic
def _tone(rate, ch=1, secs=1.0, f=1000.0, amp=8000):
    t = np.arange(int(rate * secs)) / rate
    x = (amp * np.sin(2 * np.pi * f * t)).astype(np.int16)
    return np.repeat(x[:, None], ch, axis=1).reshape(-1) if ch > 1 else x


def test_conversion_to_16k_mono_keeps_pitch_and_level():
    from voice.mic import Converter, Framer
    for rate, ch in ((48000, 2), (44100, 1), (16000, 1), (8000, 1)):
        c, fr = Converter(np, rate, ch), Framer()
        x = _tone(rate, ch, 1.0)
        frames = []
        for i in range(0, len(x), 777 * ch):                  # odd block sizes, like real callbacks
            frames += fr.push(c.process(x[i:i + 777 * ch].tobytes()))
        y = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32)
        assert all(len(f) == 640 for f in frames)
        assert abs(len(y) / 16000 - 1.0) < 0.03, (rate, len(y))
        mid = y[2000:-2000]
        peak = np.argmax(np.abs(np.fft.rfft(mid * np.hanning(len(mid))))) * 16000 / len(mid)
        assert abs(peak - 1000) < 5, (rate, peak)
        assert 0.9 < np.sqrt(np.mean(mid ** 2)) / (8000 / np.sqrt(2)) < 1.05, rate


def test_level_meter_tells_digital_silence_from_a_quiet_room():
    from voice.mic import LevelMeter
    m = LevelMeter(np)
    for _ in range(160):
        m.update(bytes(640))
    assert m.zero_run == 160 and m.silent_seconds >= 3.0
    quiet = (np.random.default_rng(0).standard_normal(320) * 20).astype(np.int16).tobytes()
    m.update(quiet)
    assert m.zero_run == 0 and -75 < m.db < -55


def _fake_sd(devices, default=0, reject=lambda kw: None):
    opened = []

    class Stream:
        def __init__(self, **kw):
            err = reject(kw)
            if err:
                raise RuntimeError(err)
            self.kw, self.callback = kw, kw["callback"]
            opened.append(kw)

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    sd = types.SimpleNamespace(
        RawInputStream=Stream,
        query_devices=lambda: devices,
        query_hostapis=lambda *a: ([{"name": "MME", "default_input_device": default},
                                    {"name": "Windows WASAPI", "default_input_device": 3}] if not a
                                   else {"name": "MME", "default_input_device": default}),
        default=types.SimpleNamespace(device=[-1, -1], hostapi=0),
        WasapiSettings=lambda **kw: ("wasapi", kw))
    return sd, opened


DEVICES = [
    {"index": 0, "name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100.0},
    {"index": 1, "name": "마이크 배열(Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000.0},
    {"index": 2, "name": "스테레오 믹스(Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000.0},
    {"index": 3, "name": "마이크 배열(Realtek(R) Audio)", "hostapi": 1, "max_input_channels": 4, "default_samplerate": 48000.0},
    {"index": 4, "name": "Headset Microphone (USB Audio Device)", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 16000.0},
    {"index": 5, "name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 0, "default_samplerate": 48000.0},
]


def test_device_order_default_first_never_loopback_and_choice_respected():
    from voice.mic import MicStream, device_choices
    sd, _ = _fake_sd(DEVICES, default=1)
    m = MicStream(sd, np, lambda f: None)
    order = [d["index"] for d in m.candidates()]
    assert order[0] == 1, order                        # the Windows default input first
    assert 3 in order and order.index(3) < order.index(4), order   # same mic via WASAPI before others
    assert 2 not in order and 5 not in order, "loopback / output devices are never chosen"
    m.device_pref = "Headset Microphone"
    assert [d["index"] for d in m.candidates()] == [4] and m.locked_to_choice
    m.device_pref = "A microphone that is not plugged in"
    assert m.candidates()[0]["index"] == 1 and m.pref_missing and not m.locked_to_choice
    names = device_choices(sd)
    assert names[0] == "default" and "스테레오 믹스(Realtek(R) Audio)" in names[-2:]
    assert names.count("마이크 배열(Realtek(R) Audio)") == 1, names


def test_open_falls_back_to_the_devices_own_format_and_converts():
    from voice.mic import MicStream
    sd, opened = _fake_sd(DEVICES, default=1,
                          reject=lambda kw: "Invalid sample rate [PaErrorCode -9997]"
                          if kw["samplerate"] != 48000 else
                          ("Invalid number of channels" if kw["channels"] != 2 else None))
    got = []
    m = MicStream(sd, np, got.append)
    assert m.open(), m.describe()
    assert m.info["rate"] == 48000 and m.info["channels"] == 2 and m.info["converted"]
    assert any("16000 Hz 1ch: Invalid sample rate" in a for a in m.attempts)
    x = _tone(48000, 2, 0.5)
    for i in range(0, len(x), 1920):
        m.stream.callback(x[i:i + 1920].tobytes(), 960, None, None)
    assert 23 <= len(got) <= 25 and all(len(f) == 640 for f in got), len(got)   # ~0.5 s of 20 ms frames


def test_no_device_is_explained():
    from voice.mic import MicStream
    sd, _ = _fake_sd([], reject=lambda kw: "Error querying device -1")
    m = MicStream(sd, np, lambda f: None, log=lambda s: None)
    assert not m.open()
    assert "no microphone was found" in m.reason


# =========================================================================== wake words
def test_wake_judgement():
    from voice.wakeword import wake_verdict, loose_wake, wake_candidate, heard_wake
    assert heard_wake("wake up jarvis") and not heard_wake("jarvis") and heard_wake("jarvis", lenient=True)
    for said in ("Wake up, Jarvis.", "Jarvis, wake up!", "Vaku up, Jarvis", "Weckup Jarvis", "wake up jervis",
                 "자비스, 일어나.", "일어나 자비스", "Jarvis, get up.", "Back up, Jarvis"):
        assert wake_verdict(said) == "wake", said
    for said in ("Jarvis.", "Hey Jarvis", "Vacuum, Jarvis"):
        assert wake_verdict(said) == "name", said
    for said in ("The service was great.", "I am nervous", "Wake up early tomorrow", "Harvest"):
        assert wake_verdict(said) == "", said
    assert loose_wake("[unk] up jarvis") and loose_wake("jarvis wake up") and not loose_wake("[unk] jarvis")
    assert wake_candidate("wake up") and wake_candidate("[unk] jarvis") and not wake_candidate("[unk]")


# =========================================================================== whisper
class _Seg:
    def __init__(self, text, no_speech=0.05, logprob=-0.2, cr=1.2):
        self.text, self.no_speech_prob, self.avg_logprob, self.compression_ratio = text, no_speech, logprob, cr


class _Info:
    def __init__(self, language, probs=None):
        self.language, self.all_language_probs = language, probs or [(language, 0.9)]


class _Model:
    def __init__(self, script):
        self.script, self.langs = script, []

    def transcribe(self, audio, **opts):
        self.langs.append(opts.get("language"))
        segs, info = self.script(opts.get("language"))
        return iter(segs), info


def _transcriber(script):
    from voice.stt import Transcriber
    t = Transcriber.__new__(Transcriber)
    import threading
    t.language, t.cuda, t.available, t.model = None, False, True, _Model(script)
    t._run_lock, t.error, t.last = threading.Lock(), "", {}
    return t


def test_whisper_language_is_limited_to_english_and_korean():
    def script(lang):
        if lang is None:
            return [_Seg("Agor y notepad")], _Info("cy", [("cy", 0.4), ("en", 0.35), ("ko", 0.05)])
        return [_Seg("Open notepad.")], _Info(lang)
    t = _transcriber(script)
    r = t.transcribe_detail(np.zeros(16000, np.float32))
    assert r["text"] == "Open notepad." and r["lang"] == "en" and t.model.langs == [None, "en"], (r, t.model.langs)


def test_whisper_noise_guesses_are_dropped_but_real_thanks_kept():
    t = _transcriber(lambda lang: ([_Seg("Thank you.", no_speech=0.55, logprob=-0.9)], _Info("en")))
    r = t.transcribe_detail(np.zeros(16000, np.float32))
    assert r["text"] == "" and "noise" in r["dropped"], r
    t = _transcriber(lambda lang: ([_Seg("Thank you.", no_speech=0.02, logprob=-0.15)], _Info("en")))
    assert t.transcribe_detail(np.zeros(16000, np.float32))["text"] == "Thank you."
    t = _transcriber(lambda lang: ([_Seg("blah", no_speech=0.9, logprob=-1.5)], _Info("en")))
    r = t.transcribe_detail(np.zeros(16000, np.float32))
    assert r["text"] == "" and r["dropped"], r


def test_whisper_loads_from_the_jarvis_folder_then_cache_then_downloads():
    import voice.stt as stt
    calls = []

    class Missing(Exception):
        pass

    Missing.__name__ = "LocalEntryNotFoundError"

    class WhisperModel:
        def __init__(self, name, device="cpu", compute_type="int8", download_root=None,
                     local_files_only=False, cpu_threads=0):
            calls.append((device, download_root, local_files_only))
            if local_files_only:
                raise Missing("Cannot find an appropriate cached snapshot folder ... outgoing traffic has been disabled")

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = WhisperModel
    saved = (sys.modules.get("faster_whisper"), stt.cuda_available)
    sys.modules["faster_whisper"], stt.cuda_available = fake, (lambda: False)
    root = tempfile.mkdtemp()
    try:
        t = stt.Transcriber("small", root=root, lazy=True, log=lambda m: None)
        assert t.load() and t.state == "ready" and t.source == "download"
        folder = os.path.join(root, "models", "whisper")
        assert calls == [("cpu", folder, True), ("cpu", None, True), ("cpu", folder, False)], calls
    finally:
        if saved[0] is not None:
            sys.modules["faster_whisper"] = saved[0]
        else:
            sys.modules.pop("faster_whisper", None)
        stt.cuda_available = saved[1]
        shutil.rmtree(root, ignore_errors=True)


def test_a_gpu_that_fails_its_test_is_not_used():
    import voice.stt as stt
    calls = []

    class WhisperModel:
        def __init__(self, name, device="cpu", **kw):
            calls.append((name, device))

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = WhisperModel
    saved = (sys.modules.get("faster_whisper"), stt.cuda_available, stt.cuda_usable)
    sys.modules["faster_whisper"] = fake
    stt.cuda_available = lambda: True
    stt.cuda_usable = lambda *a, **k: (False, "exit code 3221226505: Could not locate cudnn_ops64_9.dll")
    try:
        t = stt.Transcriber("auto", root=tempfile.gettempdir(), lazy=True, log=lambda m: None)
        assert t.load() and not t.cuda and "cudnn" in t.cuda_note, t.describe()
        assert calls[0] == ("large-v3-turbo", "cuda") and calls[-1][1] == "cpu" and not calls[-1][0].startswith("large")
    finally:
        if saved[0] is not None:
            sys.modules["faster_whisper"] = saved[0]
        else:
            sys.modules.pop("faster_whisper", None)
        stt.cuda_available, stt.cuda_usable = saved[1], saved[2]


def test_load_errors_are_explained():
    from voice.stt import explain_error
    assert "Visual C++" in explain_error(ImportError("DLL load failed while importing _ext: The specified module could not be found."))
    assert "internet" in explain_error(OSError("Max retries exceeded with url (connection refused)"))


# =========================================================================== repair download
def _model_zip(path, extra=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("vosk-model-small-en-us-0.15/conf/model.conf", "--sample-frequency=16000\n")
        z.writestr("vosk-model-small-en-us-0.15/am/final.mdl", b"\x00" * 64)
        for name in extra:
            z.writestr(name, "evil")
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_missing_wake_model_is_repaired_with_a_pinned_checksum():
    from voice.models import ensure_vosk_model
    tmp = tempfile.mkdtemp()
    try:
        zp = os.path.join(tmp, "src", "model.zip")
        os.makedirs(os.path.dirname(zp))
        digest = _model_zip(zp, extra=("../evil.txt", "vosk-model-small-en-us-0.15/../../evil2.txt"))
        dest = os.path.join(tmp, "jarvis", "models", "vosk-small-en")
        url = "file:///" + zp.replace(os.sep, "/").lstrip("/")
        ok, detail = ensure_vosk_model(dest, log=lambda m: None, url=url, sha256="0" * 64)
        assert not ok and "checksum" in detail and not os.path.exists(dest)
        ok, detail = ensure_vosk_model(dest, log=lambda m: None, url=url, sha256=digest)
        assert ok, detail
        assert os.path.isfile(os.path.join(dest, "conf", "model.conf"))
        for root_, _dirs, files in os.walk(tmp):
            assert "evil.txt" not in files and "evil2.txt" not in files, root_
        assert ensure_vosk_model(dest, log=lambda m: None, url="file:///nonexistent", sha256=digest) == (True, "present")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== report + settings
def test_voice_report_names_the_problem_and_the_fix():
    import time
    from agent.skills.voicecheck import summarise, check, set_provider
    good = {"engine": "online", "listening": True, "problems": [],
            "mic": {"open": True, "device": "마이크 배열(Realtek(R) Audio)", "rate": 48000, "converted": True,
                    "level_db": -55.0, "privacy": {"blocked": False}, "endpoint": {"muted": False, "volume": 0.7}},
            "wake": {"ready": True, "engine": "vosk"}, "stt": {"state": "ready", "model": "small"},
            "last_heard": {"text": "wake up jarvis", "at": time.time() - 30}}
    en, ko, fixes, action = summarise(good)
    assert "Everything looks right" in en and "모두 정상" in ko and not fixes and action is None
    bad = json.loads(json.dumps(good))
    bad["problems"] = [{"code": "privacy", "blocking": True, "action": "ms-settings:privacy-microphone",
                        "message": "Windows is blocking desktop apps from using the microphone.",
                        "fix": "Turn on 'Let desktop apps access your microphone'.",
                        "message_ko": "Windows가 막고 있습니다.", "fix_ko": "켜 주세요."}]
    en, ko, fixes, action = summarise(bad)
    assert "blocking" in en and "Everything looks right" not in en and action == "ms-settings:privacy-microphone"
    muted = json.loads(json.dumps(good))
    muted["mic"]["endpoint"] = {"muted": True}
    assert "muted" in summarise(muted)[0]
    assert "isn't running" in summarise({"engine": "loading", "reason": "starting up"})[0]
    set_provider(lambda: good)
    try:
        r = check()
        assert r["ok"] and "listening through" in r["speak"] and r["data"]["speak_ko"]
    finally:
        set_provider(None)


def test_microphone_setting_offers_the_connected_devices():
    from agent import settings as st
    tmp = tempfile.mkdtemp()
    try:
        cfg, perms = os.path.join(tmp, "c.json"), os.path.join(tmp, "p.json")
        shutil.copy(os.path.join(ROOT, "config", "jarvis.config.json"), cfg)
        shutil.copy(os.path.join(ROOT, "config", "permissions.json"), perms)
        got = st.get_settings(cfg, perms, options={"voice.input_device": ["default", "마이크 배열(Realtek(R) Audio)"]})
        assert got["schema"]["voice.input_device"]["options"][1] == "마이크 배열(Realtek(R) Audio)"
        assert got["values"]["voice.input_device"] == "default"
        res = st.apply_settings({"voice.input_device": "마이크 배열(Realtek(R) Audio)"}, cfg, perms)
        assert res["applied"] and not res["restart_required"], res
        res = st.apply_settings({"voice.input_device": "bad\x07name"}, cfg, perms)
        assert "voice.input_device" in res["rejected"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_intents_for_the_microphone():
    from agent.nlu import parse_intent
    for text in ("check the microphone", "can you hear me", "why can't you hear me", "마이크 확인해 줘",
                 "내 말 들려?", "음성 인식이 안 돼"):
        assert parse_intent(text).name == "voice_status", text
    assert parse_intent("open microphone privacy settings").name == "mic_settings"
    assert parse_intent("마이크 설정 열어 줘").name == "mic_settings"
    assert parse_intent("mute the microphone").name == "mute_mic"
    assert parse_intent("마이크 켜 줘").name == "unmute_mic"


def test_start_window_reports_the_voice():
    from boot.launcher import voice_lines
    ok_status = {"voice_state": "online", "voice_detail": {
        "problems": [], "mic": {"open": True, "device": "Microphone (USB)"},
        "wake": {"ready": True}, "stt": {"state": "ready"}}}
    lines = voice_lines(ok_status)
    assert ("ok", "Microphone: Microphone (USB) - listening.") in lines and all(k == "ok" for k, _ in lines), lines
    bad = json.loads(json.dumps(ok_status))
    bad["voice_detail"]["problems"] = [{"code": "privacy", "blocking": True, "message": "Windows is blocking it.",
                                        "fix": "Turn it on."}]
    bad["voice_detail"]["stt"] = {"state": "downloading"}
    lines = voice_lines(bad)
    assert lines[0] == ("bad", "Windows is blocking it. Turn it on.") and any("TEST_MICROPHONE" in t for _, t in lines)
    assert any("downloading" in t for _, t in lines)
    assert voice_lines({"voice_state": "failed", "voice_error": "boom"})[0][0] == "bad"
    assert "turned off" in voice_lines({"voice_state": "off"})[0][1]


# =========================================================================== server
def _server():
    from agent.server import JarvisServer
    tmp = tempfile.mkdtemp(prefix="jarvis_voice_")
    cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    cfg["neural_core"]["active_neurons"] = 20000
    cfg["llm"]["host"] = "http://127.0.0.1:9"
    cp, pp = os.path.join(tmp, "c.json"), os.path.join(tmp, "p.json")
    json.dump(cfg, open(cp, "w", encoding="utf-8"))
    shutil.copy(os.path.join(ROOT, "config", "permissions.json"), pp)
    srv = JarvisServer(config_path=cp, permissions_path=pp, data_dir=os.path.join(tmp, "data"))
    sent = []

    async def broadcast(ev):
        sent.append(ev)

    srv.manager.broadcast = broadcast
    srv.manager.active = {object()}
    return srv, sent, tmp


def test_server_explains_voice_problems_once_and_unheard_commands():
    srv, sent, tmp = _server()
    try:
        said = lambda: [e["text"] for e in sent if e.get("type") == "say"]  # noqa: E731
        problem = {"code": "privacy", "blocking": True, "action": "ms-settings:privacy-microphone",
                   "message": "Windows is blocking desktop apps from using the microphone, so I can't hear you, sir.",
                   "fix": "Open Settings > Privacy & security > Microphone.", "message_ko": "막고 있습니다.",
                   "fix_ko": "켜 주세요."}

        async def run():
            await srv.on_voice_event({"type": "voice_problem", "problem": problem, "active": True})
            await srv.on_voice_event({"type": "voice_problem", "problem": problem, "active": True})
            assert len(said()) == 1 and "blocking" in said()[0], said()       # said once, not twice
            assert any(e.get("type") == "voice_problem" for e in sent)        # and shown on the HUD
            await srv.on_voice_event({"type": "voice_unheard", "reason": "no_speech", "kind": "ptt",
                                      "voiced_frames": 0})
            assert said()[-1] == "I didn't hear anything, sir."
            hint = [e for e in sent if e.get("type") == "voice_hint"][-1]
            assert hint["code"] == "unheard" and hint["reason"] == "no_speech", hint      # the HUD toast
            n = len(said())
            await srv.on_voice_event({"type": "voice_unheard", "reason": "no_speech", "kind": "wake",
                                      "voiced_frames": 0})
            assert len(said()) == n, "silence after the wake reply means 'never mind': no reply"
            await srv.on_voice_event({"type": "voice_unheard", "reason": "empty", "kind": "wake"})
            assert said()[-1] == "Sorry, sir, I didn't catch that."
            await srv.on_voice_event({"type": "mic", "level": 0.3})
            assert sent[-1]["type"] == "mic"
            srv.orch.last_lang = "ko"
            await srv.on_voice_event({"type": "voice_unheard", "reason": "too_short", "kind": "ptt"})
            assert said()[-1].startswith("죄송합니다")
            srv.voice_state = "loading"
            out = []

            async def send(ws, ev):
                out.append(ev)

            srv.manager.send = send
            await srv.listen(ws=object())
            assert out and out[0]["type"] == "listen_unavailable" and "starting up" in out[0]["reason"]
            st = srv.status()
            assert "voice_detail" in st and "voice_tts" in st and st["version"] == "1.5.0"

        asyncio.run(run())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception:
            import traceback
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} voice-part tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
