"""
agent/tools_spec.py -- what the language model may DO on the PC, described so it actually does it.

Before v1.5.0 the model was told to write <action name="...">{...}</action> after its sentence.
Small local models mostly wrote the sentence ("Right away, sir -- opening Notepad.") and left
the action out, so nothing happened: measured with qwen2.5:3b, 1 command in 15 was carried out.
Native tool calling (Ollama `tools`, which Qwen 2.5 is trained for) with real parameter schemas
carried out 15 of 15. So:

  * llm_tools()        the tool definitions sent with every request (when the model supports it)
  * acting_section()   the system-prompt section for either mode (tools, or <action> tags for
                       models without tool support -- with the tag shown INSIDE the example reply)
  * resolve_call()     a call from the model -> (registry skill, clean arguments): tool-name
                       aliases ("launch_app" -> open_app), argument aliases ("search_term" ->
                       query), plain-text arguments, numbers, placeholder user folders
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

S = {"type": "string"}


def _p(desc: str, enum: Optional[List[str]] = None) -> dict:
    d = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


# name -> (description, {param: schema}, required)
SPECS: Dict[str, Tuple[str, Dict[str, dict], List[str]]] = {
    "open_app": ("Open (launch) an application, game, Windows tool or well-known website by its name.",
                 {"app": _p("the name, e.g. Notepad, Spotify, Calculator, Steam, YouTube")}, ["app"]),
    "close_app": ("Close a running application.", {"app": _p("the application name")}, ["app"]),
    "open_url": ("Open a web address in the browser.", {"url": _p("e.g. youtube.com/feed/trending")}, ["url"]),
    "web_search": ("Search the web in the browser. Only when the user asks to search or wants live "
                   "information (news, weather, prices, scores).", {"query": _p("what to search for")}, ["query"]),
    "play_music": ("Play music: a song, an artist, an album or a style.",
                   {"query": _p("e.g. Bohemian Rhapsody, Adele, relaxing jazz")}, ["query"]),
    "stop_music": ("Stop the music JARVIS is playing.", {}, []),
    "media_control": ("Control the media that is playing in any app.",
                      {"action": _p("what to do", ["next", "previous", "pause", "resume"])}, ["action"]),
    "set_volume": ("Set the speaker volume.", {"level": _p("0-100, or up, down, mute, unmute")}, ["level"]),
    "set_brightness": ("Set the screen brightness.", {"level": _p("0-100, or up, down")}, ["level"]),
    "mute_mic": ("Mute the microphone.", {}, []),
    "unmute_mic": ("Unmute the microphone.", {}, []),
    "time_now": ("Tell the current time and date.", {}, []),
    "set_timer": ("Start a timer, or set a reminder.",
                  {"duration": _p("how long, e.g. 5 minutes (a timer, or 'remind me in ...')"),
                   "at": _p("a clock time, e.g. 5 pm (for 'remind me at ...')"),
                   "what": _p("what to remind the user about (empty for a plain timer)")}, []),
    "list_timers": ("Say which timers and reminders are running.", {}, []),
    "cancel_timer": ("Cancel a timer or reminder.", {"what": _p("which one, or 'all'")}, []),
    "file_search": ("Find files on this PC by (part of) their name.", {"query": _p("part of the file name")}, ["query"]),
    "open_folder": ("Show a folder in File Explorer.",
                    {"folder": _p("downloads, documents, desktop, pictures, music, videos, recycle bin, or a path")},
                    ["folder"]),
    "make_folder": ("Create a new, empty folder (not for notes: use file_write for those).", {"name": _p("the folder name"),
                                             "where": _p("desktop, documents, downloads, pictures, music or videos "
                                                         "(default: desktop)")}, ["name"]),
    "file_write": ("Write a text file -- a note, a list, a letter -- with the given content (saved on the desktop "
                   "unless a full path is given).",
                   {"path": _p("a file name (saved on the desktop) or a full path"), "content": _p("the text")},
                   ["path", "content"]),
    "file_read": ("Read a text file and report what it says.", {"path": _p("the file path")}, ["path"]),
    "type_text": ("Type text into the app the user is working in (a letter, a message, a note).",
                  {"text": _p("exactly what to type")}, ["text"]),
    "press_keys": ("Press a key combination in the app the user is working in.",
                   {"keys": _p("e.g. ctrl+s, ctrl+c, alt+tab, enter")}, ["keys"]),
    "window_focus": ("Bring a window to the front.", {"title": _p("part of the window title or app name")}, ["title"]),
    "window_minimize": ("Minimise a window ('' = the current one).", {"title": _p("window title, or ''")}, []),
    "window_maximize": ("Maximise a window ('' = the current one).", {"title": _p("window title, or ''")}, []),
    "show_desktop": ("Minimise everything and show the desktop.", {}, []),
    "screenshot": ("Take a screenshot.", {}, []),
    "system_info": ("Report CPU, memory, disk and battery use.", {}, []),
    "list_devices": ("List connected devices.", {}, []),
    "settings": ("Open a Windows Settings page (Bluetooth, Wi-Fi, display, sound, updates, ...).",
                 {"page": _p("e.g. bluetooth, network-wifi, display, sound, windowsupdate, nightlight; '' for the main page")},
                 []),
    "power": ("Shut down, restart or put the PC to sleep.",
              {"action": _p("what to do", ["shutdown", "restart", "sleep"])}, ["action"]),
    "lock": ("Lock the PC.", {}, []),
    "empty_recycle_bin": ("Empty the Recycle Bin.", {}, []),
    "run_shell": ("Run a PowerShell command and show its output. Only when the user asks to run a command, or wants "
                  "something only a command can do (network, processes, system details) and no other tool fits. "
                  "Never for arithmetic or questions you can answer yourself.",
                  {"command": _p("the PowerShell command")}, ["command"]),
    "open_space3d": ("Open the 3D project space.", {}, []),
    "close_space3d": ("Close the 3D project space.", {}, []),
    "self_remember": ("Remember something the user tells you about themselves or how they like things done.",
                      {"fact": _p("the fact, in the user's words")}, ["fact"]),
    "sleep_assistant": ("Stand by until the user wakes you again.", {}, []),
}

TOOL_ALIASES = {
    "launch_app": "open_app", "start_app": "open_app", "open_application": "open_app", "run_app": "open_app",
    "launch": "open_app", "open": "open_app", "start": "open_app", "open_program": "open_app", "launch_program": "open_app",
    "close": "close_app", "kill_app": "close_app", "quit_app": "close_app", "terminate_app": "close_app",
    "close_application": "close_app", "exit_app": "close_app",
    "search": "web_search", "google": "web_search", "search_web": "web_search", "internet_search": "web_search",
    "search_internet": "web_search", "browse": "open_url", "open_website": "open_url", "navigate": "open_url",
    "go_to_url": "open_url", "open_link": "open_url", "visit": "open_url",
    "play": "play_music", "play_song": "play_music", "play_track": "play_music",
    "volume": "set_volume", "change_volume": "set_volume", "adjust_volume": "set_volume",
    "brightness": "set_brightness", "change_brightness": "set_brightness", "adjust_brightness": "set_brightness",
    "shell": "run_shell", "powershell": "run_shell", "cmd": "run_shell", "run_command": "run_shell",
    "execute": "run_shell", "command": "run_shell", "terminal": "run_shell", "execute_command": "run_shell",
    "type": "type_text", "write_text": "type_text", "keyboard_type": "type_text",
    "hotkey": "press_keys", "keys": "press_keys", "press": "press_keys", "press_key": "press_keys",
    "take_screenshot": "screenshot", "capture_screen": "screenshot",
    "timer": "set_timer", "start_timer": "set_timer", "reminder": "set_timer", "set_reminder": "set_timer",
    "remind": "set_timer", "remind_me": "set_timer", "alarm": "set_timer", "set_alarm": "set_timer",
    "time": "time_now", "get_time": "time_now", "current_time": "time_now", "date": "time_now", "get_date": "time_now",
    "create_folder": "make_folder", "mkdir": "make_folder", "new_folder": "make_folder", "make_directory": "make_folder",
    "lock_screen": "lock", "lock_pc": "lock", "lock_computer": "lock",
    "system_status": "system_info", "get_system_info": "system_info", "battery": "system_info",
    "find_file": "file_search", "search_files": "file_search", "find_files": "file_search",
    "read_file": "file_read", "open_file": "file_read", "write_file": "file_write", "create_file": "file_write",
    "save_file": "file_write", "minimize_window": "window_minimize", "maximize_window": "window_maximize",
    "focus_window": "window_focus", "switch_window": "window_focus", "switch_to": "window_focus",
    "open_settings": "settings", "windows_settings": "settings", "show_folder": "open_folder",
    "explore_folder": "open_folder", "next_track": "media_control", "pause_music": "media_control",
    "resume_music": "media_control", "mute": "set_volume", "unmute": "set_volume",
    "remember": "self_remember", "shutdown": "power", "restart": "power", "reboot": "power",
}
_IMPLIED = {"shutdown": {"action": "shutdown"}, "restart": {"action": "restart"}, "reboot": {"action": "restart"},
            "next_track": {"action": "next"}, "pause_music": {"action": "pause"}, "resume_music": {"action": "resume"},
            "mute": {"level": "mute"}, "unmute": {"level": "unmute"}}

ARG_ALIASES = {
    "open_app": {"app_name": "app", "name": "app", "application": "app", "program": "app", "application_name": "app",
                 "target": "app", "title": "app"},
    "close_app": {"app_name": "app", "name": "app", "application": "app", "program": "app", "process": "app"},
    "web_search": {"q": "query", "search_term": "query", "term": "query", "text": "query", "keywords": "query",
                   "search": "query", "topic": "query"},
    "file_search": {"search_term": "query", "name": "query", "filename": "query", "file_name": "query",
                    "pattern": "query", "term": "query", "q": "query", "keyword": "query"},
    "set_volume": {"volume": "level", "value": "level", "percent": "level", "percentage": "level", "amount": "level"},
    "set_brightness": {"brightness": "level", "value": "level", "percent": "level", "percentage": "level"},
    "type_text": {"content": "text", "message": "text", "string": "text", "body": "text"},
    "run_shell": {"cmd": "command", "script": "command", "powershell": "command", "code": "command"},
    "open_url": {"link": "url", "address": "url", "website": "url", "site": "url", "uri": "url"},
    "play_music": {"song": "query", "track": "query", "artist": "query", "music": "query", "title": "query",
                   "name": "query", "search": "query", "genre": "query"},
    "media_control": {"command": "action", "control": "action", "operation": "action"},
    "power": {"mode": "action", "type": "action", "operation": "action"},
    "set_timer": {"time": "at", "message": "what", "text": "what", "reminder": "what", "task": "what",
                  "label": "what", "name": "what", "length": "duration", "delay": "duration", "in": "duration"},
    "window_focus": {"window": "title", "name": "title", "app": "title", "window_title": "title"},
    "window_minimize": {"window": "title", "name": "title", "app": "title"},
    "window_maximize": {"window": "title", "name": "title", "app": "title"},
    "make_folder": {"folder": "name", "folder_name": "name", "dirname": "name", "directory": "name",
                    "location": "where", "parent": "where", "in": "where", "folder_path": "path"},
    "open_folder": {"path": "folder", "name": "folder", "location": "folder", "directory": "folder"},
    "settings": {"setting": "page", "section": "page", "name": "page"},
    "file_write": {"file": "path", "filename": "path", "name": "path", "file_name": "path", "text": "content",
                   "body": "content", "data": "content"},
    "file_read": {"file": "path", "filename": "path", "name": "path"},
    "press_keys": {"key": "keys", "combination": "keys", "shortcut": "keys", "hotkey": "keys"},
    "self_remember": {"text": "fact", "memory": "fact", "note": "fact", "info": "fact"},
    "cancel_timer": {"name": "what", "label": "what", "timer": "what"},
}


def llm_tools(registry, forbidden=()) -> List[dict]:
    """Ollama tool definitions for the skills that exist (and the model may use)."""
    out = []
    for name, (desc, props, required) in SPECS.items():
        if name not in registry or name in forbidden:
            continue
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}})
    return out


def resolve_call(name: str, args, registry) -> Tuple[Optional[str], Dict[str, str]]:
    """A call as the model wrote it -> (skill name in the registry or None, clean arguments)."""
    raw = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    skill = raw if raw in registry else TOOL_ALIASES.get(raw)
    if skill not in registry:
        return None, {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip().startswith("{") else {"_text": args}
        except ValueError:
            args = {"_text": args}
    args = dict(args or {})
    clean: Dict[str, str] = dict(_IMPLIED.get(raw, {}))
    spec = SPECS.get(skill)
    params = spec[1] if spec else {}
    aliases = ARG_ALIASES.get(skill, {})
    for k, v in args.items():
        key = str(k).strip()
        key = aliases.get(key.lower(), key)
        if v is None or (isinstance(v, str) and not v.strip()) and key not in ("title",):
            continue
        if isinstance(v, bool):
            v = "yes" if v else "no"
        elif isinstance(v, (int, float)):
            v = str(int(v)) if float(v).is_integer() else str(v)
        elif isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        clean[key] = str(v).strip() if key not in ("text", "content") else str(v)
    text = clean.pop("_text", None)
    if text is not None:
        required = spec[2] if spec else []
        first = (required or list(params) or [None])[0]
        if first and first not in clean:
            clean[first] = text.strip()
    if params:
        clean = {k: v for k, v in clean.items() if k in params}
    if skill == "set_timer":
        for unit in ("minutes", "seconds", "hours"):
            if unit in args and "duration" not in clean:
                clean["duration"] = f"{args[unit]} {unit}"
    if skill == "media_control" and clean.get("action"):
        clean["action"] = {"skip": "next", "forward": "next", "back": "previous", "prev": "previous",
                           "play": "resume", "stop": "pause", "toggle": "pause"}.get(clean["action"].lower(),
                                                                                    clean["action"].lower())
    if skill == "power" and clean.get("action"):
        a = clean["action"].lower()
        clean["action"] = "shutdown" if a in ("shut down", "shut_down", "off", "poweroff", "power off") else \
            "restart" if a in ("reboot", "restart") else a
    if skill == "make_folder" and "path" in args and "name" not in clean:
        clean = {"path": str(args["path"])}
    if skill in ("file_write", "file_read", "file_delete", "list_dir", "make_folder") and clean.get("path"):
        try:                                   # a made-up "C:\\Users\\YourUsername\\..." -> the real folder
            from .skills.files import user_path
            clean["path"] = str(user_path(clean["path"]))
        except Exception:
            pass
    return skill, clean


def _tag_line(name: str) -> str:
    desc, props, _ = SPECS[name]
    args = ", ".join(f'"{k}": "…"' for k in props)
    return f"  {name} {{{args}}}" if args else f"  {name} {{}}"


def acting_section(mode: str, lang: str, registry, forbidden=()) -> str:
    """The system prompt's section on acting, for native tools ("tools") or action tags ("tags")."""
    ko = lang == "ko"
    if mode == "tools":
        if ko:
            return ("PC 조작(도구)\n"
                    "주어진 도구로 이 PC를 직접 조작할 수 있습니다. 사용자가 PC에서 무언가를 해 달라고 하면(열기, 닫기, 재생, "
                    "검색, 설정, 입력, 찾기, 만들기, 켜기/끄기, 종료 등) 알맞은 도구를 호출하세요(여러 단계가 필요하면 순서대로 "
                    "여러 개). 도구를 호출하지 않았다면 했다고 말하지 마세요.\n"
                    "사용자가 대화를 하거나 스스로 답할 수 있는 질문(지식, 계산, 농담, 조언, 의견)을 하면 도구 없이 자연스러운 "
                    "문장으로 답하세요. 말로 하는 답에는 JSON이나 코드, 도구 문법을 쓰지 마세요.\n"
                    "run_shell은 다른 도구로 안 될 때만 쓰고, JARVIS 자신의 권한이나 설정을 바꾸는 데는 절대 쓰지 마세요.\n\n")
        return ("ACTING ON THE PC (TOOLS)\n"
                "You can act on this PC with the tools you have been given. When the user asks you to do something on "
                "the PC (open, close, play, search, set, type, find, create, turn on or off, shut down...), call the "
                "matching tool, several in order if the request needs several. Describing an action does nothing: "
                "only a tool call does it, so never say you have done something unless you called a tool for it.\n"
                "When the user is chatting, or asks something you can answer yourself (facts, arithmetic, jokes, "
                "advice, opinions), just answer it in plain spoken sentences and call no tool -- work out sums "
                "yourself. Never write JSON, code or tool syntax in a spoken reply.\n"
                "Use run_shell only when no other tool fits, and never to change JARVIS's own permissions or "
                "settings.\n\n")
    lines = "\n".join(_tag_line(n) for n in SPECS if n in registry and n not in forbidden)
    if ko:
        return ("PC 조작(도구)\n"
                "PC에서 무언가를 하려면 답의 끝에 다음 형식의 동작 줄을 정확히 한 줄 쓰세요:\n"
                '  <action name="도구">{"인자": "값"}</action>\n'
                "무언가를 한다고 말하는 답에는 반드시 이 동작 줄이 있어야 합니다. 동작 줄이 없으면 아무 일도 일어나지 않습니다. 예:\n"
                "  사용자: 메모장 열어 줘\n"
                '  당신: 바로 열겠습니다, sir. <action name="open_app">{"app": "Notepad"}</action>\n'
                f"도구와 인자:\n{lines}\n"
                "대화만 하거나 스스로 답할 수 있는 질문에는 동작 줄 없이 평소처럼 답하세요.\n\n")
    return ("ACTING ON THE PC (TOOLS)\n"
            "To act on the PC, end your reply with exactly ONE action line in this format:\n"
            '  <action name="TOOL">{"argument": "value"}</action>\n'
            "A reply that says you are doing something MUST contain that action line -- without it nothing happens. "
            "Example:\n"
            "  User: open notepad\n"
            '  You: Right away, sir. <action name="open_app">{"app": "Notepad"}</action>\n'
            f"Tools and their arguments:\n{lines}\n"
            "When the user is only chatting, or asks something you can answer yourself, reply normally without an "
            "action line.\n\n")


# A reply that CLAIMS an action ("opening Notepad", "I'll set a timer") -- used to notice a
# model that described an action without performing it.
CLAIM = re.compile(
    r"\b(open(ing)?|clos(e|ing)|launch(ing)?|start(ing)?|play(ing)?|search(ing)?|set(ting)?|turn(ing)?|"
    r"increas(e|ing)|decreas(e|ing)|lower(ing)?|rais(e|ing)|mut(e|ing)|lock(ing)?|shut(ting)? down|restart(ing)?|"
    r"creat(e|ing)|mak(e|ing)|typ(e|ing)|tak(e|ing) a screenshot|navigat(e|ing)|go(ing)? to|switch(ing)?|"
    r"minimi[sz](e|ing)|maximi[sz](e|ing)|run(ning)?|remind(ing)?|timer)\b|"
    r"(열겠|열어\s*드리|닫겠|재생하겠|검색하겠|설정하겠|켜겠|끄겠|만들겠|실행하겠|열었습니다|닫았습니다|실행했습니다)",
    re.IGNORECASE)


# tool names (or "DO <tool>") written as plain text: a call that was described, not made
PSEUDO_CALL = re.compile(r"\b(DO\s+)?(" + "|".join(sorted(SPECS, key=len, reverse=True)) + r")\b(\s*[({\"']|\s+\w)",
                         re.IGNORECASE)
