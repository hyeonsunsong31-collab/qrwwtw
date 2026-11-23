"""
tests/test_actions.py -- commands are carried out, not just answered (v1.4.4).

    python tests/test_actions.py

Covers: everyday spoken phrasings reach the right action (speech-recognition punctuation and
polite wrappers included, English and Korean); closing the 3D space by voice ("close the 3D
space" used to OPEN it) and a bare "close it" while it is open; the language model acting
through native tool calls (and, without tool support, through tolerant action lines); a reply
that only DESCRIBES an action being rescued into the action; several-step requests; the
conversation history keeping tool calls as tool calls; spoken permission questions answered
by "yes" / "no" without cancelling the waiting action; timers and reminders; real user folders.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ------------------------------------------------------------------ parsing
def test_everyday_phrasings_reach_the_right_action():
    from agent.nlu import parse_intent
    cases = {
        "Can you open Notepad?": ("open_app", {"app": "notepad"}),
        "Could you please open notepad for me?": ("open_app", {"app": "notepad"}),
        "Jarvis, open Spotify.": ("open_app", {"app": "spotify"}),
        "fire up paint for me old bean": ("open_app", {"app": "paint"}),
        "Can you close Spotify?": ("close_app", {"app": "spotify"}),
        "Volume up.": ("set_volume", {"level": "up"}),
        "Turn it up.": ("set_volume", {"level": "up"}),
        "It's too loud.": ("set_volume", {"level": "down"}),
        "Mute.": ("set_volume", {"level": "mute"}),
        "Set the volume to 50.": ("set_volume", {"level": "50"}),
        "Play the next song.": ("media_control", {"action": "next"}),
        "Skip this song.": ("media_control", {"action": "next"}),
        "Search the web for the weather in London.": ("web_search", {"query": "the weather in london"}),
        "Go to youtube.com.": ("open_url", {"url": "youtube.com"}),
        "Open my downloads folder.": ("open_folder", {"folder": "downloads"}),
        "Show the desktop.": ("show_desktop", {}),
        "Find my resume file.": ("file_search", {"query": "resume"}),
        "Create a file called notes.txt.": ("file_write", {"path": "notes.txt"}),
        "Delete the file test.txt.": ("file_delete", {"path": "test.txt"}),
        "Shut down the computer.": ("power", {"action": "shutdown"}),
        "Restart my PC.": ("power", {"action": "restart"}),
        "Put the computer to sleep.": ("power", {"action": "sleep"}),
        "Lock the computer.": ("lock", {}),
        "Open the file explorer.": ("open_app", {"app": "explorer"}),
        "Make a new folder on my desktop called Projects.": ("make_folder", {"name": "Projects", "where": "desktop"}),
        "Run ipconfig.": ("run_shell", {"command": "ipconfig"}),
        "Ping google.com for me and show me the result.": ("run_shell", {"command": "ping google.com"}),
        "What time is it?": ("time_now", {}),
        "Set a timer for 5 minutes.": ("set_timer", {"duration": "5 minutes"}),
        "Remind me to call mom in 10 minutes.": ("set_timer", {"what": "call mom", "duration": "10 minutes"}),
        "Cancel the timer.": ("cancel_timer", {}),
        "Maximize this window.": ("window_maximize", {"title": ""}),
        "Can you increase the brightness?": ("set_brightness", {"level": "up"}),
        "Copy that.": ("press_keys", {"keys": "ctrl+c"}),
        "Save it.": ("press_keys", {"keys": "ctrl+s"}),
        "Turn on bluetooth.": ("settings", {"page": "bluetooth"}),
        "Empty the recycle bin.": ("empty_recycle_bin", {}),
        "Jot down 'call the bank' in the open window.": ("type_text", {"text": "call the bank"}),
        "Can you hear me?": ("voice_status", {}),
        "크롬 실행해": ("open_app", {"app": "크롬"}),
        "계산기 켜 줄래?": ("open_app", {"app": "계산기"}),
        "볼륨 좀 올려 줘": ("set_volume", {"level": "up"}),
        "컴퓨터 꺼 줘": ("power", {"action": "shutdown"}),
        "지금 몇 시야?": ("time_now", {}),
        "내 말 들려?": ("voice_status", {}),
        "바탕화면에 프로젝트라는 폴더 만들어 줘": ("make_folder", {"name": "프로젝트", "where": "desktop"}),
    }
    for text, (name, args) in cases.items():
        got = parse_intent(text)
        assert got.name == name and got.args == args, f"{text!r}: {got.name} {got.args}"
    # several steps go to the language model, which can do them one after another
    for text in ("Open Word and write a letter.", "Create a folder called X on my desktop and then write a note in it"):
        assert parse_intent(text).name == "chat", text


def test_the_3d_space_closes_by_voice():
    from agent.nlu import parse_intent, contextual_intent
    for text in ("close the 3D space", "Close the 3D space.", "Close the 3-D space.", "exit the 3D space",
                 "close the three D space", "quit the 3D project space", "turn off the 3D space", "3D space off",
                 "get out of the 3D space", "3D 공간 닫아 줘", "쓰리디 공간 꺼 줘", "3D 공간에서 나가"):
        assert parse_intent(text).name == "close_space3d", text
    for text in ("open the 3D space", "Open the 3-D space.", "show me the 3D space", "3D 공간 열어 줘"):
        assert parse_intent(text).name == "open_space3d", text
    # things done INSIDE the space keep their own intents; "workspace" is a developer workspace now
    assert parse_intent("add a cube to the 3D space").name == "add_object"
    assert parse_intent("turn off hand gestures in the 3D space").name == "toggle_gesture"
    assert parse_intent("save the 3D project").name == "save_project"
    assert parse_intent("open workspace alpha").name != "open_space3d"
    # with the 3D space on screen, "close it" / "exit" / "go back" close it
    for text in ("close it", "Close it.", "exit", "go back", "Go back to the desktop.", "I'm done", "닫아 줘", "나가"):
        assert contextual_intent(parse_intent(text), text, {"space3d": True}).name == "close_space3d", text
    assert contextual_intent(parse_intent("close notepad"), "close notepad", {"space3d": True}).name == "close_app"
    assert contextual_intent(parse_intent("close it"), "close it", {"space3d": False}).name != "close_space3d"


def test_action_lines_are_read_however_the_model_writes_them():
    from agent.nlu import parse_action_tags, strip_action_tags
    from agent.tools_spec import resolve_call
    from agent.skills import REGISTRY
    text = ('Right away. <action name="open_app">{"app": "Notepad"}</action> '
            "<action name='launch_app'>{'app': 'Paint'}</action> "
            '<action name="web_search" query="cheap flights"/> <action name=type_text>hello there</action>')
    acts = parse_action_tags(text)
    got = [resolve_call(a.name, a.args, REGISTRY) for a in acts]
    assert got == [("open_app", {"app": "Notepad"}), ("open_app", {"app": "Paint"}),
                   ("web_search", {"query": "cheap flights"}), ("type_text", {"text": "hello there"})], got
    assert strip_action_tags(text) == "Right away."
    assert resolve_call("volume", {"volume": 30}, REGISTRY) == ("set_volume", {"level": "30"})
    assert resolve_call("shutdown", {}, REGISTRY) == ("power", {"action": "shutdown"})
    assert resolve_call("set_timer", {"minutes": 5, "unknown": 1}, REGISTRY) == ("set_timer", {"duration": "5 minutes"})
    assert resolve_call("make_coffee", {}, REGISTRY) == (None, {})


# ------------------------------------------------------------------ the language model acting
class ToolLLM:
    """A tool-calling model: replies come from a script, one per request."""

    def __init__(self, script, tools=True, json_reply=None):
        self.script = list(script)
        self.tools = tools
        self.json_reply = json_reply or {}
        self.requests = []
        self.json_requests = []

    def model_name(self):
        return "scripted"

    def supports_tools(self):
        return self.tools

    def chat_stream_events(self, system, user, history=None, params=None, tools=None, stop_event=None):
        self.requests.append({"system": system, "user": user, "history": list(history or []), "tools": tools})
        if not self.tools:
            from agent.llm_ollama import ToolsUnsupported
            raise ToolsUnsupported("model does not support tools")
        step = self.script.pop(0) if self.script else {"text": ""}
        for piece in (step.get("text") or "").split(" "):
            if piece:
                yield ("text", piece + " ")
        if step.get("calls"):
            yield ("tools", step["calls"])

    def chat_stream(self, system, user, history=None, params=None, stop_event=None):
        self.requests.append({"system": system, "user": user, "history": list(history or []), "tools": None})
        step = self.script.pop(0) if self.script else {"text": "Very good, sir."}
        yield step.get("text", "")

    def chat(self, system, user, history=None, params=None):
        return "".join(self.chat_stream(system, user, history, params))

    def chat_json(self, system, user, schema, params=None):
        self.json_requests.append(user)
        return self.json_reply


def _orch(llm, tmp, granted=True, asked=None):
    from agent.orchestrator import Orchestrator
    from agent.permissions import PermissionManager
    from core.brain_mesh import BrainMesh
    from core.scale import resolve_scale
    from core.control_layer import ControlLayer
    from recorder import Recorder
    events, done = [], []

    async def emit(ev):
        events.append(ev)

    async def asker(req):
        (asked if asked is not None else []).append(req)
        return granted

    cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    rec = Recorder(os.path.join(tmp, "data"))
    control = ControlLayer(mesh=BrainMesh(resolve_scale(20000)), state_dir=os.path.join(tmp, "data", "brain"))
    orch = Orchestrator(cfg, llm, recorder=rec, control=control, emit=emit,
                        permissions=PermissionManager(config_path=os.path.join(ROOT, "config", "permissions.json"),
                                                      recorder=rec, asker=asker))
    real = orch._do_skill

    async def spy(name, args, text, lang, rid, ack=True):
        done.append((name, dict(args or {})))
        return await real(name, args, text, lang, rid, ack)
    orch._do_skill = spy
    return orch, events, done


def _final(events):
    return [e["text"] for e in events if e.get("type") == "transcript" and e.get("final")][-1]


def test_the_model_acts_through_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        llm = ToolLLM([{"calls": [{"name": "time_now", "arguments": {}}]},
                       {"calls": [{"name": "set_timer", "arguments": {"duration": "3 minutes"}}]},
                       {"text": "Canberra, sir."}])
        orch, ev, done = _orch(llm, tmp)
        asyncio.run(orch.handle_command("could you tell me what the clock says, old chap"))
        assert done == [("time_now", {})] and _final(ev).startswith("It's "), (done, _final(ev))
        assert llm.requests[0]["tools"] and "Describing an action does nothing" in llm.requests[0]["system"]
        asyncio.run(orch.handle_command("I'd like a heads-up once three minutes have passed"))
        assert done[-1] == ("set_timer", {"duration": "3 minutes"}) and "3 minutes" in _final(ev)
        asyncio.run(orch.handle_command("what's the capital of australia"))
        assert len(done) == 2 and "Canberra" in _final(ev)
        # the history keeps the calls as calls (the model learns that tools do things)
        hist = llm.requests[-1]["history"]
        assert any(m.get("calls") and m["calls"][0]["name"] == "time_now" for m in hist), hist
        assert any(m["role"] == "tool" and m["text"].startswith("It's ") for m in hist), hist


def test_a_described_action_is_rescued_into_the_action():
    with tempfile.TemporaryDirectory() as tmp:
        llm = ToolLLM([{"text": 'DO set_volume "down", sir.'}],
                      json_reply={"tool": "set_volume", "args": {"level": "down"}})
        orch, ev, done = _orch(llm, tmp)
        asyncio.run(orch.handle_command("the racket from these speakers is unbearable"))
        assert done == [("set_volume", {"level": "down"})] and llm.json_requests, done
        assert "DO set_volume" not in _final(ev), _final(ev)


def test_an_empty_tool_reply_falls_back_to_plain_words():
    with tempfile.TemporaryDirectory() as tmp:
        llm = ToolLLM([{"text": ""}, {"text": "Eighty-four, sir."}])
        orch, ev, done = _orch(llm, tmp)
        asyncio.run(orch.handle_command("what is twelve times seven"))
        assert "Eighty-four" in _final(ev) and not done
        assert llm.requests[-1]["tools"] is None and "ACTING ON THE PC" not in llm.requests[-1]["system"]


def test_models_without_tools_use_action_lines():
    with tempfile.TemporaryDirectory() as tmp:
        llm = ToolLLM([{"text": 'Certainly, sir. <action name="launch_app">{"app": "Paint"}</action>'}], tools=False)
        orch, ev, done = _orch(llm, tmp)
        asyncio.run(orch.handle_command("I fancy a doodle, get the drawing program going"))
        assert done == [("open_app", {"app": "Paint"})], done
        system = llm.requests[-1]["system"]
        assert '<action name="open_app">{"app": "Notepad"}</action>' in system, "the example shows the tag"


def test_several_steps_are_carried_out_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        saved = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            llm = ToolLLM([{"calls": [{"name": "make_folder", "arguments": {"name": "Reports", "where": "desktop"}}]},
                           {"calls": [{"name": "file_write", "arguments": {
                               "path": "C:\\Users\\YourUsername\\Desktop\\Reports\\hello.txt", "content": "hello"}}]}])
            asked = []
            orch, ev, done = _orch(llm, tmp, asked=asked)
            asyncio.run(orch.handle_command("make a folder called Reports on the desktop and then write hello in a note there"))
            assert [d[0] for d in done] == ["make_folder", "file_write"], done
            note = os.path.join(home, "Desktop", "Reports", "hello.txt")
            assert os.path.isfile(note) and open(note).read() == "hello", "the made-up user folder became the real one"
            assert any("hello.txt" in (q.get("question") or "") for q in asked)
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved


def test_the_model_never_changes_permissions_by_tool():
    with tempfile.TemporaryDirectory() as tmp:
        llm = ToolLLM([{"calls": [{"name": "shell_permission", "arguments": {"level": "allow"}}]}])
        orch, ev, done = _orch(llm, tmp)
        asyncio.run(orch.handle_command("please do whatever you think is best"))
        assert not done and not any(t["function"]["name"] == "shell_permission" for t in llm.requests[0]["tools"])


# ------------------------------------------------------------------ permissions asked aloud
def _server(tmp):
    from agent.server import JarvisServer
    cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    cfg["neural_core"]["active_neurons"] = 20000
    cfg["llm"]["manage_engine"] = False
    cfg["llm"]["host"] = "http://127.0.0.1:9"
    cp, pp = os.path.join(tmp, "c.json"), os.path.join(tmp, "p.json")
    json.dump(cfg, open(cp, "w", encoding="utf-8"))
    shutil.copy(os.path.join(ROOT, "config", "permissions.json"), pp)
    return JarvisServer(config_path=cp, permissions_path=pp, data_dir=os.path.join(tmp, "data"))


class FakeVoice:
    def __init__(self):
        self.captures, self.said = [], []

    def begin_capture(self, after_speech=True, kind=None):
        self.captures.append(kind)
        return True

    def enqueue(self, text, lang="en"):
        self.said.append(text)

    def tts_ready(self):
        return True

    listening = True


def test_permission_questions_are_spoken_and_answered_by_voice_or_text():
    from agent.orchestrator import describe_action
    assert describe_action("close_app", {"app": "Spotify"}) == "Shall I close Spotify, sir?"
    assert describe_action("power", {"action": "restart"}) == "Shall I restart the PC, sir?"
    assert describe_action("close_app", {"app": "메모장"}, "ko").endswith("닫을까요, sir?")
    tmp = tempfile.mkdtemp(prefix="jarvis_perm_")
    try:
        srv = _server(tmp)
        srv.voice = FakeVoice()
        sent = []

        async def broadcast(ev):
            sent.append(ev)
        srv.manager.broadcast = broadcast

        async def scenario(answer):
            sent.clear()
            srv.voice.captures.clear()
            task = asyncio.ensure_future(srv.orch.handle_command("close spotify"))
            srv._current = task
            for _ in range(200):
                if srv._pending_perms:
                    break
                await asyncio.sleep(0.01)
            assert srv._pending_perms, "JARVIS asks before closing an app"
            q = [e for e in sent if e.get("type") == "transcript" and e.get("kind") == "question"]
            assert q and q[0]["text"].startswith("Shall I close spotify, sir?"), q
            assert "Shall I close spotify, sir?" in srv.voice.said and srv.voice.captures == ["answer"]
            await srv.submit_command(answer, "voice")     # must ANSWER, not cancel the waiting action
            await asyncio.wait_for(task, 20)
            assert not task.cancelled()
            assert any(e.get("type") == "permission_resolved" for e in sent)
            return [e["text"] for e in sent if e.get("type") == "transcript" and e.get("final")][-1]

        reply = asyncio.run(scenario("Yes."))
        assert "hold off" not in reply, reply                    # granted: it went on to close it
        reply = asyncio.run(scenario("no thanks"))
        assert "hold off" in reply, reply
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ui_state_puts_commands_in_context():
    tmp = tempfile.mkdtemp(prefix="jarvis_ui_")
    try:
        srv = _server(tmp)
        asyncio.run(srv.on_message({"type": "ui_state", "state": {"space3d": True}}))
        assert srv.orch.ui_context == {"space3d": True}
        asyncio.run(srv.on_message({"type": "ui_state", "state": {"space3d": False, "evil": 1}}))
        assert srv.orch.ui_context == {"space3d": False}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ clock + folders
def test_timers_and_reminders():
    from agent.skills import clock
    assert clock.parse_duration("1 hour and 30 minutes") == 5400 and clock.parse_duration("half an hour") == 1800
    assert clock.parse_duration("1시간 30분") == 5400 and clock.parse_duration("90 seconds") == 90
    from datetime import datetime
    base = datetime(2026, 11, 23, 14, 0)
    assert clock.parse_clock("5 pm", base) == datetime(2026, 11, 23, 17, 0)
    assert clock.parse_clock("5", base) == datetime(2026, 11, 23, 17, 0)       # "at 5" said at 2 pm
    assert clock.parse_clock("9 am", base) == datetime(2026, 11, 24, 9, 0)
    assert clock.parse_clock("오후 3시 반", base) == datetime(2026, 11, 23, 15, 30)
    tmp = tempfile.mkdtemp(prefix="jarvis_timer_")
    try:
        fired = []
        clock.start(tmp, notify=lambda ev: fired.append(ev))
        clock.cancel_timer(what="all")
        assert clock.set_timer(duration="1 second")["ok"]
        r = clock.set_timer(duration="2 hours", what="check the oven")
        assert "remind you to check the oven in 2 hours" in r["speak"]
        saved = json.load(open(os.path.join(tmp, "timers.json")))
        assert len(saved) == 2, "kept on disk, so a restart keeps it"
        time.sleep(2.2)
        assert fired and fired[0]["en"].startswith("Sir, your timer for 1 second is up"), fired
        assert "check the oven" in clock.list_timers()["speak"]
        assert "Cancelled" in clock.cancel_timer(what="all")["speak"]
        assert not json.load(open(os.path.join(tmp, "timers.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_user_folders():
    from agent.skills import files
    tmp = tempfile.mkdtemp(prefix="jarvis_home_")
    saved = os.environ.get("HOME")
    os.environ["HOME"] = tmp
    try:
        assert str(files.user_path("C:\\Users\\YourUsername\\Desktop\\a.txt")) == os.path.join(tmp, "Desktop", "a.txt")
        assert str(files.user_path("notes.txt")) == os.path.join(tmp, "Desktop", "notes.txt")
        assert str(files.user_path("Documents/r.txt")) == os.path.join(tmp, "Documents", "r.txt")
        r = files.make_folder(name="Projects", where="desktop")
        assert r["ok"] and os.path.isdir(os.path.join(tmp, "Desktop", "Projects")), r
        assert "already" in files.make_folder(name="Projects", where="desktop")["speak"]
        r = files.write_file("shopping", "milk")
        assert r["ok"] and open(os.path.join(tmp, "Desktop", "shopping.txt")).read() == "milk"
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} action tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
