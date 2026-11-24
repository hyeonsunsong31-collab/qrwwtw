"""
agent/orchestrator.py
====================
Turns a command (voice OR text -- same path, requirement 15) into actions plus a spoken and
written reply, recording everything (requirement 2) and letting the neural control layer
shape the output (requirement 1).

Pipeline per command:
  detect language -> log -> parse intent -> neural control signals -> skill or LLM
  -> permission gate for any PC action -> reply -> options + suggestions -> checkpoint.

After every command the self core (core/self_model.py) learns from how it went, and its
fixed directive plus what it has learned are part of the language model's instructions.

SPEED (v1.4): the language model reuses its cache of a prompt prefix it has already read,
so everything that changes per turn (the neural style directive, recent-mistake notes) rides
on the user message as a short [Host note], while the system prompt and the history stay
byte-identical between turns; the history is trimmed in large steps, not one turn at a time.
The first clause of a long opening sentence is spoken without waiting for its full stop,
slow skills are acknowledged at once, and the reply is shown and spoken before bookkeeping.

LLM replies are STREAMED: each finished sentence is spoken as soon as it exists, so JARVIS
starts talking within a moment instead of after the whole answer is generated (requirement
11). A new command or a barge-in cancels the task; the run is recorded as interrupted.

Transport-agnostic: the server injects `emit` and the permission `asker`; this class has no
socket code and is unit-tested with a mock LLM (scripts/mock_run.py, tests/).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Awaitable, Callable, List, Optional, Tuple

from core.control_layer import ControlLayer
from recorder import Recorder
from .nlu import parse_intent, detect_language, parse_action_tags, strip_action_tags, contextual_intent
from .permissions import PermissionManager, PermissionRequest
from .replies_ko import localize
from .skills import REGISTRY, LLM_FORBIDDEN, selfcore
from .tools_spec import SPECS, CLAIM as _CLAIM, PSEUDO_CALL as _PSEUDO, acting_section, llm_tools, resolve_call
from .llm_ollama import ToolsUnsupported

EmitFn = Callable[[dict], Awaitable[None]]

DIRECT_SKILL_MIN_CONF = 0.6
_BOUNDARY = re.compile(r"[.!?。！？…][\"')\]]*\s+|\n+")
_DANGLING_TAG = re.compile(r"<action.*$", re.S)
_CLAUSE = re.compile(r"[,;:]\s+|\s[-–—]\s+")

# conversation context for the language model: trimmed in large steps so the kept part is
# an unchanged prefix (cached by the model) for several turns in a row
# (an action turn takes 4 messages since v1.4.4: request, tool call, tool result, reply -- the
# character budget is what bounds the size)
HISTORY_MAX_MESSAGES = 24
HISTORY_MAX_CHARS = 6000
HISTORY_KEEP_MESSAGES = 12
DELTA_INTERVAL = 0.04          # at most ~25 chat-bubble updates a second while streaming

# skills that can take a few seconds: say something at once, then the result
ACK_INTENTS = {"play_music", "file_search", "read_page"}
ACK = {"en": "Right away, sir.", "ko": "바로 하겠습니다, sir."}
WARMING = {"en": "One moment, sir. My language engine is still warming up.",
           "ko": "잠시만요, sir. 언어 엔진이 아직 준비 중입니다."}

_PROMPTS: dict = {}


def _load_prompt(lang: str) -> str:
    key = "ko" if lang == "ko" else "en"
    if key not in _PROMPTS:
        fn = os.path.join(os.path.dirname(__file__), "prompts", f"system_{key}.txt")
        try:
            with open(fn, encoding="utf-8") as fh:
                _PROMPTS[key] = fh.read().strip()
        except OSError:
            return "You are JARVIS, a courteous British butler AI."
    return _PROMPTS[key]


def pop_sentences(buf: str, min_len: int = 14) -> Tuple[List[str], str]:
    """Split complete sentences off the front of `buf`; return (sentences, remainder).
    Very short fragments ("Sir.", "Mr.") are merged with what follows."""
    out: List[str] = []
    start = 0
    for m in _BOUNDARY.finditer(buf):
        candidate = buf[start:m.end()].strip()
        if len(candidate) >= min_len:
            out.append(candidate)
            start = m.end()
    return out, buf[start:]


def pop_first_clause(buf: str, min_len: int = 24, trigger: int = 60, hard: int = 180) -> Tuple[Optional[str], str]:
    """Before the first sentence of a reply is complete, take its first clause once the
    buffer is long enough, so a long opening sentence does not hold back the first word.
    Returns (clause or None, remainder)."""
    if len(buf) < trigger:
        return None, buf
    for m in _CLAUSE.finditer(buf):
        if m.end() >= min_len:
            return buf[:m.end()].strip(), buf[m.end():]
    if len(buf) >= hard:                              # no pause at all: break at a word
        cut = buf.rfind(" ", min_len, hard)
        if cut > 0:
            return buf[:cut].strip(), buf[cut + 1:]
    return None, buf


def display_text(raw: str) -> str:
    """What the chat bubble shows mid-stream: no action tags, complete or half-written."""
    return _DANGLING_TAG.sub("", strip_action_tags(raw)).strip()


# skills whose reply carries information (always spoken, even when the model already talked)
INFO_SKILLS = {"time_now", "system_info", "file_search", "file_read", "list_timers", "list_devices", "list_windows",
               "now_playing", "run_shell", "run_shell_elevated", "llm_status", "voice_status", "self_describe",
               "list_dir", "set_timer"}
_REQUEST = re.compile(
    r"^(please\s+)?(can|could|would|will)\s+(you|u)\b|^(please\s+|jarvis[, ]+)?(open|close|launch|start|play|stop|pause|"
    r"search|find|look up|set|turn|switch|increase|decrease|raise|lower|mute|unmute|lock|shut|restart|reboot|create|"
    r"make|type|write|send|take|show|go|navigate|minimi[sz]e|maximi[sz]e|run|execute|remind|delete|remove|empty|"
    r"install|download|copy|paste|save|undo|check|put|bring|move|resume|skip|enable|disable|change|adjust|clear|"
    r"jot|note|write down|ping|show me|give me|get me|let me|let'?s|pull up|fire up|kill|quit|exit|load|fetch)\b|"
    r"\b(i want you to|i need you to|i'?d like you to|please|i need|i want|i'?d like|can i get|too loud|too quiet|"
    r"too bright|too dark|can'?t hear)\b|"
    r"(줘|해|해\s*줘|주세요|줄래|해\s*봐|하자|켜|꺼|열어|닫아)\s*[?.!]*$", re.IGNORECASE)


# what JARVIS asks before an action that needs your permission (spoken and shown)
_ASK_EN = {
    "open_app": "open {app}", "close_app": "close {app}", "type_text": "type \"{text}\"", "press_keys": "press {keys}",
    "file_write": "write the file {path}", "file_delete": "delete {path}", "make_folder": "create the folder {name}",
    "empty_recycle_bin": "empty the Recycle Bin", "lock": "lock the PC", "settings": "open Windows Settings",
    "run_shell": "run this PowerShell command: {command}", "run_shell_elevated": "run this command as administrator: {command}",
    "mute_mic": "mute your microphone", "unmute_mic": "unmute your microphone", "control_devices": "change that device",
    "window_focus": "switch to {title}", "screenshot": "take a screenshot", "browser_form_fill": "fill in that form",
    "llm_install": "install the language engine", "shell_permission": "change the shell permission",
    "self_remember": "remember \"{fact}\"", "play_music": "play {query}", "web_search": "search the web for {query}",
    "open_url": "open {url}", "set_volume": "set the volume to {level}", "set_brightness": "set the brightness to {level}",
    "file_search": "search your files for {query}", "open_folder": "open {folder}", "file_read": "read {path}",
    "window_minimize": "minimise that window", "window_maximize": "maximise that window", "show_desktop": "show the desktop",
    "media_control": "{action} the media", "set_timer": "set that timer", "list_dir": "list {path}",
}
_ASK_KO = {
    "open_app": "{app}을(를) 열까요", "close_app": "{app}을(를) 닫을까요", "type_text": "\"{text}\"라고 입력할까요",
    "press_keys": "{keys}를 누를까요", "file_write": "{path} 파일을 쓸까요", "file_delete": "{path}을(를) 삭제할까요",
    "make_folder": "{name} 폴더를 만들까요", "empty_recycle_bin": "휴지통을 비울까요", "lock": "PC를 잠글까요",
    "settings": "Windows 설정을 열까요", "run_shell": "이 PowerShell 명령을 실행할까요: {command}",
    "run_shell_elevated": "이 명령을 관리자 권한으로 실행할까요: {command}", "mute_mic": "마이크를 끌까요",
    "unmute_mic": "마이크를 켤까요", "screenshot": "스크린샷을 찍을까요", "llm_install": "언어 엔진을 설치할까요",
    "self_remember": "\"{fact}\"을(를) 기억할까요", "play_music": "{query}을(를) 재생할까요",
    "web_search": "{query}을(를) 웹에서 검색할까요", "open_url": "{url}을(를) 열까요", "set_volume": "볼륨을 {level}(으)로 할까요",
    "file_search": "{query} 파일을 찾을까요", "open_folder": "{folder}을(를) 열까요",
}
_POWER_EN = {"shutdown": "shut down the PC", "restart": "restart the PC", "sleep": "put the PC to sleep"}
_POWER_KO = {"shutdown": "PC를 끌까요", "restart": "PC를 다시 시작할까요", "sleep": "PC를 절전 모드로 할까요"}


def describe_action(name: str, args: dict, lang: str = "en") -> str:
    """'Shall I close Spotify, sir?' -- the permission question in plain words."""
    a = {k: (str(v)[:80] + ("…" if len(str(v)) > 80 else "")) for k, v in (args or {}).items()}
    ko = lang == "ko"
    if name == "power":
        act = a.get("action", "shutdown")
        return (_POWER_KO.get(act, "그렇게 할까요") + ", sir?") if ko else f"Shall I {_POWER_EN.get(act, act + ' the PC')}, sir?"
    tmpl = (_ASK_KO if ko else _ASK_EN).get(name)
    try:
        body = tmpl.format_map(_Blank(a)) if tmpl else ""
    except Exception:
        body = ""
    if ko:
        return f"{body or '그렇게 할까요'}, sir?"
    return f"Shall I {body or name.replace('_', ' ')}, sir?"


class _Blank(dict):
    def __missing__(self, key):
        return "it"


def _looks_like_request(text: str) -> bool:
    return bool(_REQUEST.search((text or "").strip()))


_MULTI_STEP = re.compile(r"\b(and|then|after that|afterwards|also)\b.*\b(write|type|search|play|open|send|make|"
                         r"create|find|go|put|save|close|set|draft|read|tell|turn)\b|그리고|다음에|하고\s|해서", re.IGNORECASE)


def _collect_calls(llm, system: str, history: list, params: dict, tools: list) -> list:
    """One more model turn after tool results: just the further tool calls it makes."""
    calls: list = []
    try:
        for kind, payload in llm.chat_stream_events(system, "Continue with the next step of my request, if any.",
                                                    history, params, tools):
            if kind == "tools":
                calls.extend(payload or [])
    except Exception:
        return []
    return calls


def _needs_rescue(prose: str, text: str) -> bool:
    """The model described an action instead of doing it: a pseudo-call written as text, or an
    action claim in reply to something that was a request."""
    if _PSEUDO.search(prose or ""):
        return True
    return bool(_CLAIM.search(prose or "")) and _looks_like_request(text)


def _wait_for_new_window(timeout: float = 8.0) -> None:
    """After launching an app, wait (a little) for its window before typing into it."""
    try:
        from .skills.windows import _gw, _own        # Windows only
        gw = _gw()
        start = gw.getActiveWindow()
        t0 = time.time()
        while time.time() - t0 < timeout:
            now = gw.getActiveWindow()
            if now is not None and not _own(now) and (start is None or now.title != start.title):
                time.sleep(0.6)
                return
            time.sleep(0.3)
    except Exception:
        time.sleep(min(timeout, 2.5))


class Orchestrator:
    def __init__(self, config: dict, llm, recorder: Optional[Recorder] = None,
                 control: Optional[ControlLayer] = None,
                 permissions: Optional[PermissionManager] = None,
                 emit: Optional[EmitFn] = None):
        self.config = config
        self.llm = llm
        self.recorder = recorder or Recorder()
        self.control = control or ControlLayer()
        self.permissions = permissions or PermissionManager(recorder=self.recorder)
        self.emit = emit or (lambda ev: asyncio.sleep(0))
        self.history: List[dict] = []
        self.default_lang = config.get("language", {}).get("default", "en")
        self.last_lang = None             # the language of the latest command (spoken notices follow it)
        self.awake = True
        self._last_denied = False
        self._last_skill_ok = True
        # what the HUD shows right now ({"space3d": True} while the 3D space is open): a bare
        # "close it" / "exit" then means the 3D space (nlu.contextual_intent)
        self.ui_context: dict = {}
        # exactly what the language model was sent and produced, turn by turn (see SPEED)
        self.llm_history: List[dict] = []
        # the self core: fixed directive + the self-model learned from experience
        self.selfmodel = getattr(self.control, "selfmodel", None)
        if self.selfmodel is not None:
            self.selfmodel.enabled = self._self_learning()
            selfcore.bind(self.selfmodel)

    # --------------------------------------------------------------- utils #
    def _self_learning(self) -> bool:
        return bool(self.config.get("self_model", {}).get("enabled", True))

    def self_summary(self) -> Optional[dict]:
        return self.selfmodel.summary() if self.selfmodel is not None else None

    # ------------------------------------------------- prompt for the LLM #
    def system_prompt(self, lang: str, mode: Optional[str] = None) -> str:
        """Everything in the language model's instructions that does not change from turn to
        turn, so the model can reuse its cache of it on every request. mode: "tools" (native
        tool calling), "tags" (<action> lines) or "plain" (conversation only); by default the
        one this model uses."""
        mode = mode or ("tools" if self._tools_mode() else "tags")
        system = _load_prompt(lang) + "\n\n"
        if mode != "plain":
            system += acting_section(mode, lang, REGISTRY, LLM_FORBIDDEN)
        if self.selfmodel is not None:
            system += self.selfmodel.prompt_context(lang)
        return system.rstrip()

    def _turn_note(self, control, lang: str) -> str:
        """What changes per turn, appended to the user message instead of the system prompt."""
        parts = [control.style_directive] if control.style_directive else []
        if self.selfmodel is not None:
            careful = self.selfmodel.careful_note(lang)
            if careful:
                parts.append(careful)
        return ("[Host note - not from the user: " + " ".join(parts) + "]") if parts else ""

    def _llm_live(self) -> bool:
        try:
            return self.llm.model_name() != "offline-fallback"
        except Exception:
            return True

    def _remember_llm_turn(self, user_msg: str, assistant_msg: str, calls: Optional[list] = None):
        """calls: [(tool name, arguments, what it reported)] -- kept as a real tool-call exchange,
        so the model sees that actions are done by calling tools, never by describing them."""
        if calls:
            self.llm_history += [{"role": "user", "text": user_msg},
                                 {"role": "assistant", "text": "", "calls": [{"name": n, "arguments": a} for n, a, _ in calls]}]
            self.llm_history += [{"role": "tool", "text": r or "done", "name": n} for n, _, r in calls]
            self.llm_history.append({"role": "assistant", "text": assistant_msg})
        else:
            self.llm_history += [{"role": "user", "text": user_msg},
                                 {"role": "assistant", "text": assistant_msg}]
        h = self.llm_history
        if len(h) <= HISTORY_MAX_MESSAGES and sum(len(m["text"]) for m in h) <= HISTORY_MAX_CHARS:
            return
        keep = h[-HISTORY_KEEP_MESSAGES:]
        while len(keep) > 2 and sum(len(m["text"]) for m in keep) > HISTORY_MAX_CHARS // 2:
            keep = keep[2:]
        while keep and keep[0]["role"] != "user":
            keep = keep[1:]
        self.llm_history = keep

    def _history(self, mode: str) -> List[dict]:
        """The conversation as this kind of model reads it: tool calls stay tool calls for a
        tool-calling model; for the others they become the action lines they would write."""
        if mode == "tools":
            return list(self.llm_history)
        out: List[dict] = []
        for m in self.llm_history:
            if m["role"] == "tool":
                continue
            if m.get("calls"):
                tags = " ".join(f'<action name="{c["name"]}">{json.dumps(c.get("arguments") or {}, ensure_ascii=False)}</action>'
                                for c in m["calls"])
                out.append({"role": "assistant", "text": ((m.get("text") or "") + " " + tags).strip()})
                continue
            if out and out[-1]["role"] == m["role"] == "assistant":
                out[-1] = {"role": "assistant", "text": (out[-1]["text"] + " " + m["text"]).strip()}
                continue
            out.append({"role": m["role"], "text": m["text"]})
        return out

    async def _learn(self, intent_name: str, lang: str, outcome: str, rid: Optional[str] = None):
        """Self-education step: the self-model integrates how this request went."""
        if self.selfmodel is None or intent_name == "self_forget":
            return
        try:
            note = await self._run_blocking(self.selfmodel.observe, intent_name, lang, outcome)
            lesson = (note or {}).get("lesson")
            if lesson:
                self.recorder.log_explanation(
                    "process", f"Self-model: repaired my model of '{lesson['about']}' after it "
                               f"{lesson['outcome']}; I will take more care with it.", run_id=rid)
        except Exception:
            pass                      # learning must never get in the way of answering

    async def _emit(self, event: dict):
        try:
            await self.emit(event)
        except Exception:
            pass

    async def _run_blocking(self, fn, *a, **kw):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    # ---------------------------------------------------------------- wake #
    async def handle_wake(self):
        self.awake = True
        resp = self.config.get("identity", {}).get("wake_response", "Jarvis at your service sir")
        await self._emit({"type": "state", "state": "listening"})
        await self._emit({"type": "wake_response", "text": resp, "lang": "en"})
        await self._emit({"type": "say", "text": resp, "lang": "en"})
        self.recorder.log_conversation("assistant", resp, lang="en")
        # the user reaching out strengthens rapport (drive layer, requirement 1)
        state = await self._run_blocking(self.control.note_outcome, "engaged")
        await self._emit({"type": "control", "state": state})

    async def handle_feedback(self, signals: dict):
        """User tunes behaviour (e.g. 'too long'): the mesh learns (requirement 1)."""
        await self._run_blocking(self.control.learn, signals)
        await self._emit({"type": "state", "state": "idle"})

    # ------------------------------------------------------------- command #
    async def handle_command(self, text: str, source: str = "text"):
        text = (text or "").strip()
        if not text:
            return
        lang = detect_language(text)
        if self.config.get("language", {}).get("auto_switch", True) is False:
            lang = self.default_lang
        self.last_lang = lang

        cid = self.recorder.log_command(text, source=source, lang=lang)
        self.recorder.log_conversation("user", text, lang=lang)
        self.history.append({"role": "user", "text": text})
        await self._emit({"type": "transcript", "role": "user", "text": text, "lang": lang})
        await self._emit({"type": "state", "state": "thinking", "lang": lang})

        if self.selfmodel is not None:
            self.selfmodel.enabled = self._self_learning()      # the Settings switch applies at once
        intent = contextual_intent(parse_intent(text, lang=lang), text, self.ui_context)
        skill = REGISTRY.get(intent.name)
        risk = self.permissions.risk_of(skill.category, text) if skill else 0.0
        self._last_denied = False
        control = await self._run_blocking(self.control.compute, text, lang, risk)
        sentiment = self.control.last_features.sentiment if self.control.last_features else 0.0
        await self._emit({"type": "control", "signals": control.signals,
                          "state": control.internal_state})

        rid = self.recorder.start_run(
            cid, intent.name, description=text,
            eta_seconds=self.recorder.estimate_eta(skill.eta) if skill else None)
        outcome = "success"
        try:
            if skill and intent.name != "chat" and intent.confidence >= DIRECT_SKILL_MIN_CONF:
                reply, lang = await self._do_skill(intent.name, intent.args, text, lang, rid)
                reply = self.control.postprocess(reply, control)
                to_say = reply
                # context for later conversation: what was asked, the tool that did it, its result
                self._remember_llm_turn(text, reply, calls=[(intent.name, dict(intent.args or {}), reply)])
            else:
                reply, to_say = await self._do_chat(text, lang, control, rid)
            self.recorder.end_run(rid, "success", summary=reply[:200])
            outcome = "denied" if self._last_denied else "success"
        except asyncio.CancelledError:
            self.recorder.end_run(rid, "cancelled", summary="interrupted by the user")
            await self._run_blocking(self.control.note_outcome, "interrupted", risk, sentiment)
            await self._learn(intent.name, lang, "interrupted", rid)
            raise
        except Exception as e:  # never crash the bus
            self.recorder.end_run(rid, "failed", error=repr(e))
            reply = ("My apologies, sir - something went wrong carrying that out."
                     if lang == "en" else "죄송합니다, sir - 수행 중 문제가 발생했습니다.")
            to_say = reply
            outcome = "failed"
        # show and speak the reply first; the bookkeeping below must not delay it
        self.history.append({"role": "assistant", "text": reply})
        self.history[:] = self.history[-24:]
        await self._emit({"type": "transcript", "role": "assistant", "text": reply, "lang": lang,
                          "msg_id": rid, "final": True})
        if to_say.strip():
            await self._emit({"type": "say", "text": to_say.strip(), "lang": lang})
        self.recorder.log_conversation("assistant", reply, lang=lang, run_id=rid)

        # update the drive layer's disposition from how this interaction went
        drive_state = await self._run_blocking(self.control.note_outcome, outcome, risk, sentiment)
        await self._learn(intent.name, lang, outcome, rid)
        await self._emit({"type": "control", "state": drive_state, "self": self.self_summary()})

        await self._follow_ups(intent.name, lang, rid)
        self.recorder.checkpoint({"last_command": text, "intent": intent.name,
                                  "history_len": len(self.history)})
        await self._emit({"type": "state", "state": "idle"})

    # ----------------------------------------------------------- skill run #
    async def _do_skill(self, name, args, text, lang, rid, ack: bool = True):
        skill = REGISTRY[name]
        self.recorder.run_step(rid, f"intent '{name}' args={args}")

        pre = {}
        if getattr(skill, "preflight", None) is not None:
            try:
                pre = skill.preflight(args or {}, text) or {}
            except Exception:
                pre = {"force_ask": True}
        detail = pre.get("detail") or text
        granted = await self.permissions.check(PermissionRequest(
            action=f"{name} {json.dumps(args, ensure_ascii=False)}",
            category=skill.category, detail=detail,
            risk=pre.get("risk") or self.permissions.risk_of(skill.category, detail),
            force_ask=bool(pre.get("force_ask")), keywords=pre.get("keywords", True),
            question=describe_action(name, args or {}, lang), lang=lang))
        if not granted:
            self.recorder.run_step(rid, "permission denied", status="denied")
            self._last_denied = True
            self._last_skill_ok = False
            return (("I'll hold off on that, sir - permission wasn't granted."
                     if lang == "en" else "그 작업은 보류하겠습니다, sir - 권한이 없습니다."), lang)

        if ack and name in ACK_INTENTS:
            # these can take seconds (finding a song, searching the disk): answer at once
            await self._emit({"type": "say", "text": ACK["ko" if lang == "ko" else "en"], "lang": lang})
        result = await self._run_blocking(skill.handler, args)
        self._last_skill_ok = bool(result.get("ok"))
        data = result.get("data") or {}
        self.recorder.run_step(rid, f"result ok={result.get('ok')}",
                               status="ok" if result.get("ok") else "error", data=data)
        # every file a skill produced is kept in the results archive (requirement 2)
        for path in data.get("generated_files", []) or []:
            try:
                self.recorder.register_file(path, run_id=rid, note=f"generated by {name}")
            except Exception:
                pass
        for path in data.get("temp_files", []) or []:        # copied into the records above
            try:
                os.remove(path)
            except OSError:
                pass
        # UI-affecting intents (3D space etc.) tell the overlay what to do
        if data.get("ui"):
            if data["ui"] in ("open_space3d", "close_space3d"):
                self.ui_context["space3d"] = data["ui"] == "open_space3d"
            await self._emit({"type": "ui", "action": data["ui"], "args": data})
        if data.get("show"):
            # e.g. a shell command's output: shown in the chat, not read aloud
            await self._emit({"type": "transcript", "role": "assistant", "text": str(data["show"]),
                              "lang": lang, "kind": "output"})
        if not result.get("ok") and result.get("error"):
            self.recorder.log_explanation("error", str(result["error"]), run_id=rid)
        return localize(name, result, lang) or "Done, sir.", lang

    # ------------------------------------------------------------- chat/LLM #
    def _tools_mode(self) -> bool:
        fn = getattr(self.llm, "supports_tools", None)
        try:
            return bool(fn and fn() and hasattr(self.llm, "chat_stream_events"))
        except Exception:
            return False

    def _tools(self) -> list:
        return llm_tools(REGISTRY, LLM_FORBIDDEN)

    async def _stream(self, make_iter, lang, rid, control):
        """Run a blocking reply iterator in a thread and speak each finished sentence at once.
        Items are text pieces, ("text", piece) or ("tools", [calls]).
        Returns (full text, tool calls, muted, unspoken remainder, max_sentences)."""
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = object()

        def producer():
            try:
                for item in make_iter(stop):
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                    if stop.is_set():
                        break
            except BaseException as e:  # surfaced to the consumer below
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        loop.run_in_executor(None, producer)
        max_sentences = 2 if control.signals.get("verbosity", 0.5) < 0.2 else None
        full, buf, said = "", "", 0
        muted = False
        last_delta = 0.0
        calls: list = []
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                kind, piece = item if isinstance(item, tuple) else ("text", item)
                if kind == "tools":
                    calls.extend(piece or [])
                    continue
                full += piece
                now = time.monotonic()
                if now - last_delta >= DELTA_INTERVAL:
                    last_delta = now
                    await self._emit({"type": "transcript_delta", "role": "assistant",
                                      "text": display_text(full), "lang": lang, "msg_id": rid})
                if muted:
                    continue
                buf += piece
                if "<action" in buf:            # stop speaking where an action tag begins
                    head = buf.split("<action", 1)[0].strip()
                    if head:
                        await self._emit({"type": "say", "text": head, "lang": lang})
                    buf, muted = "", True
                    continue
                sentences, buf = pop_sentences(buf)
                if not sentences and said == 0:
                    clause, buf = pop_first_clause(buf)
                    if clause:
                        sentences = [clause]
                for sent in sentences:
                    await self._emit({"type": "say", "text": sent, "lang": lang})
                    said += 1
                    if max_sentences and said >= max_sentences:
                        # stop TALKING, but keep reading: an action usually comes last
                        muted = True
                        break
        finally:
            stop.set()
        if full:
            await self._emit({"type": "transcript_delta", "role": "assistant",
                              "text": display_text(full), "lang": lang, "msg_id": rid})
        return full, calls, muted, buf, max_sentences

    async def _do_chat(self, text, lang, control, rid) -> Tuple[str, str]:
        """Return (full reply for the transcript, text that still needs to be spoken)."""
        if getattr(self.llm, "state", "ready") == "loading":
            await self._emit({"type": "say", "text": WARMING["ko" if lang == "ko" else "en"], "lang": lang})
        if self._tools_mode():
            try:
                return await self._do_chat_tools(text, lang, control, rid)
            except ToolsUnsupported:
                pass                    # marked on the model: this and later replies use action lines
        return await self._do_chat_tags(text, lang, control, rid)

    def _user_msg(self, text, control, lang) -> str:
        note = self._turn_note(control, lang)
        return f"{text}\n\n{note}" if note else text

    async def _do_chat_tools(self, text, lang, control, rid) -> Tuple[str, str]:
        """Native tool calling: the model calls skills directly (v1.4.4)."""
        system = self.system_prompt(lang)
        user_msg = self._user_msg(text, control, lang)
        history = self._history("tools")
        tools = self._tools()
        params = dict(control.llm_params or {})
        params["num_predict"] = max(int(params.get("num_predict") or 300), 800)   # room for a letter to type
        params["temperature"] = min(float(params.get("temperature", 0.7)), 0.5)   # steadier choices
        full, calls, muted, rest, max_s = await self._stream(
            lambda stop: self.llm.chat_stream_events(system, user_msg, history, params, tools, stop_event=stop),
            lang, rid, control)
        prose = display_text(full)
        if not calls and not prose:
            # nothing usable came back with tools offered (a half-written tool call): plain reply
            self.recorder.run_step(rid, "empty reply with tools offered; answering without them")
            return await self._do_chat_tags(text, lang, control, rid, plain=True)
        if not muted and rest.strip() and not calls:
            await self._emit({"type": "say", "text": strip_action_tags(rest).strip(), "lang": lang})
        replies: List[str] = []
        done: list = []
        if calls:
            replies, done = await self._run_calls(calls, text, lang, rid, spoken_prose=bool(prose))
            # a request with several steps ("open Word and write a letter"): give the model what
            # happened and let it take the next step (up to 3 rounds)
            rounds = 1
            while done and rounds < 3 and _MULTI_STEP.search(text) and self._last_skill_ok:
                rounds += 1
                step_hist = history + [{"role": "user", "text": user_msg},
                                       {"role": "assistant", "text": prose,
                                        "calls": [{"name": n, "arguments": a} for n, a, _ in done]}] + \
                            [{"role": "tool", "text": r or "done", "name": n} for n, _, r in done]
                more_calls = await self._run_blocking(_collect_calls, self.llm, system, step_hist, params, tools)
                fresh = [c for c in more_calls if (c.get("name"), c.get("arguments")) not in
                         [(n, a) for n, a, _ in done]]
                if not fresh:
                    break
                more, did = await self._run_calls(fresh, text, lang, rid)
                replies += more
                done += did
        elif _needs_rescue(prose, text):
            replies, done = await self._rescue(text, lang, rid)
            if done:
                prose = ""                       # the description of the action is replaced by its result
        reply = (prose + " " + " ".join(replies)).strip() or ("Very good, sir." if lang != "ko" else "알겠습니다, sir.")
        if self._llm_live():
            self._remember_llm_turn(user_msg, reply, calls=done or None)
        to_say = " ".join(r for r in replies if r).strip()
        if calls and not prose and not to_say:
            to_say = reply
        return reply, to_say

    async def _do_chat_tags(self, text, lang, control, rid, plain: bool = False) -> Tuple[str, str]:
        """Models without tool calling: the reply ends with an <action> line (or, plain=True,
        a conversational reply without any tool instructions)."""
        system = self.system_prompt(lang, mode="plain" if plain else "tags")
        user_msg = self._user_msg(text, control, lang)
        history = self._history("tags")

        if not hasattr(self.llm, "chat_stream"):
            raw = await self._run_blocking(self.llm.chat, system, user_msg, history, control.llm_params)
            if self._llm_live():
                self._remember_llm_turn(user_msg, raw)
            prose, action_replies = await self._finish_llm(raw, text, lang, rid, allow_rescue=not plain)
            reply = (prose + " " + action_replies).strip()
            return reply, reply

        full, _calls, muted, rest, max_s = await self._stream(
            lambda stop: self.llm.chat_stream(system, user_msg, history, control.llm_params, stop_event=stop),
            lang, rid, control)
        if not muted and rest.strip():
            await self._emit({"type": "say", "text": rest.strip(), "lang": lang})
        if self._llm_live():
            self._remember_llm_turn(user_msg, full)
        prose, action_replies = await self._finish_llm(full, text, lang, rid, max_sentences=max_s,
                                                       allow_rescue=not plain)
        reply = (prose + " " + action_replies).strip()
        return reply, action_replies

    async def _run_calls(self, calls: list, text: str, lang: str, rid: str, spoken_prose: bool = False) -> List[str]:
        """Carry out the model's tool calls, through the same permission gate as every command."""
        replies: List[str] = []
        done: list = []
        prev = None
        for call in calls[:6]:
            name, args = resolve_call(call.get("name"), call.get("arguments"), REGISTRY)
            if name is None:
                self.recorder.run_step(rid, f"the model asked for an unknown tool {call.get('name')!r}", status="error")
                continue
            if name in LLM_FORBIDDEN:
                self.recorder.run_step(rid, f"refused LLM-requested action '{name}' (user-only)", status="denied")
                continue
            if prev == "open_app" and name in ("type_text", "press_keys"):
                await self._run_blocking(_wait_for_new_window, 8.0)        # let the app open first
            self.recorder.run_step(rid, f"LLM tool call '{name}' {json.dumps(args, ensure_ascii=False)[:300]}")
            reply, _ = await self._do_skill(name, args, text, lang, rid, ack=False)
            ok = self._last_skill_ok
            if not spoken_prose or not ok or name in INFO_SKILLS:
                replies.append(reply)
            done.append((name, args, reply))
            prev = name
        return replies, done

    async def _rescue(self, text: str, lang: str, rid: str):
        """The reply SAID it was doing something but did not do it: ask the model, in a strict
        JSON form, which single tool the user asked for -- and carry that out."""
        chat_json = getattr(self.llm, "chat_json", None)
        if chat_json is None:
            return [], []
        names = [n for n in SPECS if n in REGISTRY and n not in LLM_FORBIDDEN]
        schema = {"type": "object", "properties": {"tool": {"type": "string", "enum": names + ["none"]},
                                                   "args": {"type": "object"}}, "required": ["tool", "args"]}
        lines = "\n".join(f"- {n}: {SPECS[n][0]} args: {', '.join(SPECS[n][1]) or 'none'}" for n in names)
        system = ("You turn a user's request to a PC assistant into ONE tool call. Answer with JSON only: "
                  '{"tool": <name or "none">, "args": {...}}. Use "none" if the user did not ask for an action.\n'
                  f"Tools:\n{lines}")
        try:
            out = await self._run_blocking(chat_json, system, text, schema)
        except Exception:
            return [], []
        tool = str((out or {}).get("tool") or "none")
        if tool == "none":
            return [], []
        name, args = resolve_call(tool, (out or {}).get("args") or {}, REGISTRY)
        if name is None or name in LLM_FORBIDDEN:
            return [], []
        self.recorder.run_step(rid, f"the reply described an action without doing it; carrying out '{name}' {args}")
        reply, _ = await self._do_skill(name, args, text, lang, rid, ack=False)
        return [reply], [(name, args, reply)]

    async def _finish_llm(self, raw: str, text: str, lang: str, rid: str,
                          max_sentences: Optional[int] = None, allow_rescue: bool = True) -> Tuple[str, str]:
        """Execute any <action> lines the model wrote (requirement 14); return
        (prose without tags, replies produced by the actions)."""
        replies = []
        acted = False
        for act in parse_action_tags(raw):
            name, args = resolve_call(act.name, act.args, REGISTRY)
            if name is None:
                continue
            if name in LLM_FORBIDDEN:
                self.recorder.run_step(rid, f"refused LLM-requested action '{name}' (user-only)",
                                       status="denied")
                continue
            self.recorder.run_step(rid, f"LLM-requested action '{name}'")
            act_reply, _ = await self._do_skill(name, args, text, lang, rid, ack=False)
            replies.append(act_reply)
            acted = True
        prose = strip_action_tags(raw).strip()
        if max_sentences:
            kept = pop_sentences(prose + " ")[0][:max_sentences]
            prose = " ".join(kept) or prose
        if allow_rescue and not acted and self._llm_live() and _needs_rescue(prose, text):
            more, did = await self._rescue(text, lang, rid)
            replies += more
            if did:
                prose = ""
        if not prose and not replies:
            prose = "Very good, sir." if lang == "en" else "알겠습니다, sir."
        return prose, " ".join(replies).strip()

    # ---------------------------------------------- options + suggestions #
    async def _follow_ups(self, intent: str, lang: str, rid: str):
        """Requirement 2: (3) options for further adjustment + (4) recommendations for them."""
        options, suggestions = _FOLLOWUPS.get(intent, _FOLLOWUPS["_default"])(lang)
        self.recorder.log_options(options, run_id=rid)
        self.recorder.log_suggestions(suggestions, run_id=rid)
        await self._emit({"type": "options", "options": options, "suggestions": suggestions})


def _fu(en_opts, en_sugs, ko_opts, ko_sugs):
    def inner(lang):
        return (ko_opts, ko_sugs) if lang == "ko" else (en_opts, en_sugs)
    return inner


_FOLLOWUPS = {
    "play_music": _fu(["Skip this track", "Volume up / down", "Play something else"],
                      ["Say 'play' with an artist for more like this, sir."],
                      ["다음 곡", "볼륨 조절", "다른 곡 재생"],
                      ["'(가수 이름) 틀어줘'라고 하시면 비슷한 곡을 더 틀어드립니다, sir."]),
    "open_app": _fu(["Close it again", "Open another app", "Arrange the windows"],
                    ["I can open several apps at once if you list them, sir."],
                    ["다시 닫기", "다른 앱 열기", "창 정렬"],
                    ["여러 앱을 한 번에 열어드릴 수도 있습니다, sir."]),
    "open_space3d": _fu(["Add an object", "Enable hand gestures", "Use it as my wallpaper"],
                        ["Pinch with your fingers or left-click drag to move objects, sir."],
                        ["오브젝트 추가", "손동작 켜기", "바탕화면으로 사용"],
                        ["손가락으로 집거나 좌클릭 드래그로 오브젝트를 옮길 수 있습니다, sir."]),
    "export_model": _fu(["Save the project", "Open the results folder", "Export again"],
                        ["Exports are kept with every other file I create, sir."],
                        ["프로젝트 저장", "결과 폴더 열기", "다시 내보내기"],
                        ["내보낸 모델은 제가 만든 다른 파일들과 함께 보관됩니다, sir."]),
    "system_info": _fu(["Watch it live", "Free up memory", "Check the battery"],
                       ["I can alert you if CPU or memory runs high, sir."],
                       ["실시간 보기", "메모리 정리", "배터리 확인"],
                       ["CPU나 메모리가 높아지면 알려드릴 수 있습니다, sir."]),
    "llm_status": _fu(["restart the language engine", "install the language engine", "open your settings"],
                      ["Ask me again in a minute to see the progress, sir."],
                      ["언어 엔진 다시 시작해 줘", "언어 엔진 설치해 줘", "자비스 설정 열어 줘"],
                      ["잠시 뒤에 다시 물어보시면 진행 상황을 알려 드립니다, sir."]),
    "voice_status": _fu(["open microphone privacy settings", "unmute the microphone", "open your settings"],
                        ["My Settings panel lets you choose the microphone, sir."],
                        ["마이크 개인 정보 설정 열어 줘", "마이크 켜 줘", "자비스 설정 열어 줘"],
                        ["제 설정 패널에서 사용할 마이크를 고르실 수 있습니다, sir."]),
    "_default": _fu(["Anything else, sir?", "Repeat that", "Adjust my settings"],
                    ["You can say 'wake up Jarvis' any time, sir."],
                    ["더 필요하신 것이 있나요, sir?", "다시 말하기", "설정 변경"],
                    ["언제든 'wake up Jarvis'라고 부르시면 됩니다, sir."]),
}
