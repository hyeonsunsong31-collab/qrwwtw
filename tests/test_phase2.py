"""
tests/test_phase2.py -- unit tests for the installer-hardening / features release.

    python tests/test_phase2.py            (or: python -m pytest tests/)
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from agent.nlu import parse_intent
from agent.orchestrator import pop_sentences, display_text
from agent import settings as S
from agent.replies_ko import localize
from agent.server import safe_filename, origin_allowed, token_matches
from agent.skills.apps import best_match
from voice.wakeword import heard_wake, heard_keyword


def test_new_intents_en_ko():
    cases = {
        "add a cube": ("add_object", {"shape": "box"}),
        "create another sphere": ("add_object", {"shape": "sphere"}),
        "큐브 추가해줘": ("add_object", {"shape": "box"}),
        "delete the object": ("delete_object", {}),
        "오브젝트 삭제": ("delete_object", {}),
        "save the project": ("save_project", {}),
        "export the model": ("export_model", {}),
        "show the neural network": ("neural_view", {}),
        "신경망 보여줘": ("neural_view", {}),
        "use it as my wallpaper": ("wallpaper_on", {}),
        "stop the wallpaper": ("wallpaper_off", {}),
        "배경화면으로 사용해": ("wallpaper_on", {}),
        "jarvis settings": ("open_jarvis_settings", {}),
        "take a screenshot": ("screenshot", {}),
        "type Hello World": ("type_text", {"text": "Hello World"}),
        "press ctrl+s": ("press_keys", {"keys": "ctrl+s"}),
        "switch to chrome": ("window_focus", {"title": "chrome"}),
        "minimize everything": ("show_desktop", {}),
        "turn off hand gestures": ("toggle_gesture", {"state": "off"}),
        "open notepad": ("open_app", {"app": "notepad"}),
        "delete file /tmp/x.txt": ("file_delete", {"path": "/tmp/x.txt"}),
    }
    for text, (name, args) in cases.items():
        i = parse_intent(text)
        assert i.name == name, f"{text!r} -> {i.name} (want {name})"
        for k, v in args.items():
            assert i.args.get(k) == v, f"{text!r}: {k}={i.args.get(k)!r} (want {v!r})"


def test_sentence_streaming_split():
    s, rest = pop_sentences("Very good, sir. The weather is mild today. And tomorr")
    assert s == ["Very good, sir.", "The weather is mild today."] and rest == "And tomorr"
    s, rest = pop_sentences("Mr. Smith arrived at 3.15 today. Next")       # no split on "Mr." / "3.15"
    assert s == ["Mr. Smith arrived at 3.15 today."], s
    s, _ = pop_sentences("알겠습니다, sir. 바로 열겠습니다. ")
    assert len(s) == 1 or len(s) == 2
    assert display_text('Opening it now, sir. <action name="open_app">{"app":"x"}</action>') == "Opening it now, sir."
    assert display_text('Right away <action name="open_') == "Right away"   # half-written tag hidden


def _copy_configs(d):
    c, p = os.path.join(d, "c.json"), os.path.join(d, "p.json")
    shutil.copy(os.path.join(ROOT, "config", "jarvis.config.json"), c)
    shutil.copy(os.path.join(ROOT, "config", "permissions.json"), p)
    return c, p


def test_settings_validate_apply_and_locks():
    with tempfile.TemporaryDirectory() as d:
        c, p = _copy_configs(d)
        r = S.apply_settings({"language.default": "ko", "voice.tts.rate": 1.25,
                              "voice.barge_in.mode": "vad", "permissions.write_files": "allow",
                              "permissions.run_shell_elevated": "allow",     # locked
                              "voice.tts.rate_bad": 1, "llm.temperature": 9,  # unknown / out of range
                              "llm.model": "rm -rf /"},                       # bad characters
                             c, p)
        assert r["applied"] == {"language.default": "ko", "voice.tts.rate": 1.25,
                                "voice.barge_in.mode": "vad", "permissions.write_files": "allow"}
        assert set(r["rejected"]) == {"permissions.run_shell_elevated", "voice.tts.rate_bad",
                                      "llm.temperature", "llm.model"}
        cfg, perms = json.load(open(c)), json.load(open(p))
        assert cfg["language"]["default"] == "ko" and cfg["voice"]["tts"]["rate"] == 1.25
        assert perms["categories"]["write_files"] == "allow"
        assert perms["categories"]["run_shell_elevated"] == "deny"
        got = S.get_settings(c, p)
        assert got["schema"]["permissions.run_shell_elevated"]["locked"] is True


def test_korean_replies():
    ok = {"ok": True, "speak": "Opening Notepad, sir.", "data": {"app": "메모장"}}
    assert localize("open_app", ok, "ko") == "메모장을(를) 열겠습니다, sir."
    assert localize("open_app", ok, "en") == "Opening Notepad, sir."
    fail = {"ok": False, "speak": "I couldn't find it, sir.", "data": {}}
    assert localize("open_app", fail, "ko").startswith("이 PC에서")
    assert localize("something_new", fail, "ko").startswith("죄송합니다")
    info = {"ok": True, "speak": "...", "data": {"cpu_percent": 12, "ram_percent": 40, "disk_free_gb": 200}}
    assert "CPU 12퍼센트" in localize("system_info", info, "ko")


def test_server_helpers():
    assert safe_filename("../../evil name?.exe", ".glb") == "evil name_.glb"
    assert safe_filename("", ".glb") == "model.glb"
    assert origin_allowed(None) and origin_allowed("file://") and origin_allowed("null")
    assert not origin_allowed("https://example.com") and not origin_allowed("http://localhost:3000")
    assert token_matches("abc", "abc") and not token_matches("abd", "abc") and not token_matches(None, "abc")


def test_app_matching():
    items = [("Google Chrome", "lnk", "a"), ("Visual Studio Code", "lnk", "b"),
             ("Spotify", "aumid", "c"), ("메모장", "lnk", "d"), ("Microsoft Edge", "lnk", "e")]
    assert best_match("google chrome", items)[0] == "Google Chrome"
    assert best_match("visual studio code", items)[0] == "Visual Studio Code"
    assert best_match("spotfy", items)[0] == "Spotify"          # typo tolerated
    assert best_match("메모장", items)[0] == "메모장"
    assert best_match("photoshop", items) is None


def test_wake_and_keyword_rules():
    assert heard_wake("wake up jarvis") and heard_wake("[unk] wake up jarvis")
    assert not heard_wake("jarvis") and heard_wake("jarvis", lenient=True)
    assert heard_keyword("stop", ["stop", "wait"]) == "stop"
    assert heard_keyword("stop", ["stop"], spoken_now="Please stop by later") is None   # echo
    assert heard_keyword("hold on", ["hold on"]) == "hold on"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} phase-2 tests passed")


if __name__ == "__main__":
    _run_all()
