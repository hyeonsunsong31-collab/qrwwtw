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
from typing import Awaitable, Callable, List, Optional, Tuple

from core.control_layer import ControlLayer
from recorder import Recorder
from .nlu import parse_intent, detect_language, parse_action_tags, strip_action_tags
from .permissions import PermissionManager, PermissionRequest
from .replies_ko import localize
from .skills import REGISTRY, selfcore

EmitFn = Callable[[dict], Awaitable[None]]

DIRECT_SKILL_MIN_CONF = 0.6
_BOUNDARY = re.compile(r"[.!?。！？…][\"')\]]*\s+|\n+")
_DANGLING_TAG = re.compile(r"<action.*$", re.S)


def _load_prompt(lang: str) -> str:
    here = os.path.dirname(__file__)
    fn = os.path.join(here, "prompts", f"system_{'ko' if lang == 'ko' else 'en'}.txt")
    try:
        with open(fn, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "You are JARVIS, a courteous British butler AI."


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


def display_text(raw: str) -> str:
    """What the chat bubble shows mid-stream: no action tags, complete or half-written."""
    return _DANGLING_TAG.sub("", strip_action_tags(raw)).strip()


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
        self.awake = True
        self._last_denied = False
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

        cid = self.recorder.log_command(text, source=source, lang=lang)
        self.recorder.log_conversation("user", text, lang=lang)
        self.history.append({"role": "user", "text": text})
        await self._emit({"type": "transcript", "role": "user", "text": text, "lang": lang})
        await self._emit({"type": "state", "state": "thinking", "lang": lang})

        if self.selfmodel is not None:
            self.selfmodel.enabled = self._self_learning()      # the Settings switch applies at once
        intent = parse_intent(text, lang=lang)
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
        # update the drive layer's disposition from how this interaction went
        drive_state = await self._run_blocking(self.control.note_outcome, outcome, risk, sentiment)
        await self._learn(intent.name, lang, outcome, rid)
        await self._emit({"type": "control", "state": drive_state, "self": self.self_summary()})

        self.history.append({"role": "assistant", "text": reply})
        self.history[:] = self.history[-24:]
        self.recorder.log_conversation("assistant", reply, lang=lang, run_id=rid)
        await self._emit({"type": "transcript", "role": "assistant", "text": reply, "lang": lang,
                          "msg_id": rid, "final": True})
        if to_say.strip():
            await self._emit({"type": "say", "text": to_say.strip(), "lang": lang})

        await self._follow_ups(intent.name, lang, rid)
        self.recorder.checkpoint({"last_command": text, "intent": intent.name,
                                  "history_len": len(self.history)})
        await self._emit({"type": "state", "state": "idle"})

    # ----------------------------------------------------------- skill run #
    async def _do_skill(self, name, args, text, lang, rid):
        skill = REGISTRY[name]
        self.recorder.run_step(rid, f"intent '{name}' args={args}")

        granted = await self.permissions.check(PermissionRequest(
            action=f"{name} {json.dumps(args, ensure_ascii=False)}",
            category=skill.category, detail=text,
            risk=self.permissions.risk_of(skill.category, text)))
        if not granted:
            self.recorder.run_step(rid, "permission denied", status="denied")
            self._last_denied = True
            return (("I'll hold off on that, sir - permission wasn't granted."
                     if lang == "en" else "그 작업은 보류하겠습니다, sir - 권한이 없습니다."), lang)

        result = await self._run_blocking(skill.handler, args)
        data = result.get("data") or {}
        self.recorder.run_step(rid, f"result ok={result.get('ok')}",
                               status="ok" if result.get("ok") else "error", data=data)
        # every file a skill produced is kept in the results archive (requirement 2)
        for path in data.get("generated_files", []) or []:
            try:
                self.recorder.register_file(path, run_id=rid, note=f"generated by {name}")
            except Exception:
                pass
        # UI-affecting intents (3D space etc.) tell the overlay what to do
        if data.get("ui"):
            await self._emit({"type": "ui", "action": data["ui"], "args": data})
        if not result.get("ok") and result.get("error"):
            self.recorder.log_explanation("error", str(result["error"]), run_id=rid)
        return localize(name, result, lang) or "Done, sir.", lang

    # ------------------------------------------------------------- chat/LLM #
    async def _do_chat(self, text, lang, control, rid) -> Tuple[str, str]:
        """Return (full reply for the transcript, text that still needs to be spoken)."""
        system = _load_prompt(lang)
        if self.selfmodel is not None:
            system += "\n\n" + self.selfmodel.prompt_context(lang)
        if control.style_directive:
            system += "\n\nSTYLE: " + control.style_directive
        history = list(self.history[:-1])  # the current user turn is passed separately

        if not hasattr(self.llm, "chat_stream"):
            raw = await self._run_blocking(self.llm.chat, system, text, history, control.llm_params)
            prose, action_replies = await self._finish_llm(raw, text, lang, rid)
            reply = (prose + " " + action_replies).strip()
            return reply, reply

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        done = object()

        def producer():
            try:
                for piece in self.llm.chat_stream(system, text, history, control.llm_params,
                                                  stop_event=stop):
                    loop.call_soon_threadsafe(queue.put_nowait, piece)
                    if stop.is_set():
                        break
            except Exception as e:  # surfaced to the consumer below
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        loop.run_in_executor(None, producer)
        max_sentences = 2 if control.signals.get("verbosity", 0.5) < 0.2 else None
        full, buf, said = "", "", 0
        muted = False
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, Exception):
                    raise item
                full += item
                await self._emit({"type": "transcript_delta", "role": "assistant",
                                  "text": display_text(full), "lang": lang, "msg_id": rid})
                if muted:
                    continue
                buf += item
                if "<action" in buf:            # stop speaking where an action tag begins
                    head = buf.split("<action", 1)[0].strip()
                    if head:
                        await self._emit({"type": "say", "text": head, "lang": lang})
                    buf, muted = "", True
                    continue
                sentences, buf = pop_sentences(buf)
                for s in sentences:
                    await self._emit({"type": "say", "text": s, "lang": lang})
                    said += 1
                    if max_sentences and said >= max_sentences:
                        # stop TALKING, but keep reading: an action tag usually comes last
                        muted = True
                        break
        finally:
            stop.set()

        if not muted and buf.strip():
            await self._emit({"type": "say", "text": buf.strip(), "lang": lang})
        prose, action_replies = await self._finish_llm(full, text, lang, rid,
                                                       max_sentences=max_sentences)
        reply = (prose + " " + action_replies).strip()
        return reply, action_replies

    async def _finish_llm(self, raw: str, text: str, lang: str, rid: str,
                          max_sentences: Optional[int] = None) -> Tuple[str, str]:
        """Execute any <action> tags the model emitted (requirement 14); return
        (prose without tags, replies produced by the actions)."""
        replies = []
        for act in parse_action_tags(raw):
            if act.name in REGISTRY:
                self.recorder.run_step(rid, f"LLM-requested action '{act.name}'")
                act_reply, _ = await self._do_skill(act.name, act.args, text, lang, rid)
                replies.append(act_reply)
        prose = strip_action_tags(raw).strip()
        if max_sentences:
            kept = pop_sentences(prose + " ")[0][:max_sentences]
            prose = " ".join(kept) or prose
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
    "_default": _fu(["Anything else, sir?", "Repeat that", "Adjust my settings"],
                    ["You can say 'wake up Jarvis' any time, sir."],
                    ["더 필요하신 것이 있나요, sir?", "다시 말하기", "설정 변경"],
                    ["언제든 'wake up Jarvis'라고 부르시면 됩니다, sir."]),
}
