"""
agent/orchestrator.py
====================
The brain-stem of JARVIS: turns a command (from voice OR text, same path — requirement 15)
into actions + a spoken/written reply, recording everything (requirement 2) and letting the
neural control layer shape the output (requirement 1).

Pipeline per command:
  detect language ─► log ─► parse intent ─► neural control signals ─► run skill or LLM
  ─► permission-gate any PC action ─► postprocess ─► speak/transcribe ─► options+suggestions
  ─► checkpoint.

Transport-agnostic: the server injects `emit` (push an event to the overlay) and the
permission `asker` (round-trip a confirm dialog). This class has no socket code, so it is
unit-testable with a mock LLM and mock emit (see scripts/mock_run.py, tests/).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable, List, Optional

from core.control_layer import ControlLayer
from recorder import Recorder
from .nlu import parse_intent, detect_language, parse_action_tags, strip_action_tags
from .permissions import PermissionManager, PermissionRequest
from .skills import REGISTRY

EmitFn = Callable[[dict], Awaitable[None]]

DIRECT_SKILL_MIN_CONF = 0.6
UI_INTENTS = {"open_space3d", "close_space3d", "clone_object", "toggle_gesture"}


def _load_prompt(lang: str) -> str:
    here = os.path.dirname(__file__)
    fn = os.path.join(here, "prompts", f"system_{'ko' if lang == 'ko' else 'en'}.txt")
    try:
        with open(fn, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "You are JARVIS, a courteous British butler AI."


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

    # --------------------------------------------------------------- utils #
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

        intent = parse_intent(text, lang=lang)
        skill = REGISTRY.get(intent.name)
        risk = self.permissions.risk_of(skill.category, text) if skill else 0.0

        control = await self._run_blocking(self.control.compute, text, lang, risk)
        await self._emit({"type": "control", "signals": control.signals})

        rid = self.recorder.start_run(cid, intent.name,
                                      description=text,
                                      eta_seconds=self.recorder.estimate_eta(skill.eta) if skill else None)
        try:
            if skill and intent.name != "chat" and intent.confidence >= DIRECT_SKILL_MIN_CONF:
                reply, lang = await self._do_skill(intent.name, intent.args, text, lang, rid)
            else:
                reply = await self._do_chat(text, lang, control, rid)
            reply = self.control.postprocess(reply, control)
            self.recorder.end_run(rid, "success", summary=reply[:200])
        except Exception as e:  # never crash the bus
            self.recorder.end_run(rid, "failed", error=repr(e))
            reply = ("My apologies, sir — something went wrong carrying that out."
                     if lang == "en" else "죄송합니다, sir — 수행 중 문제가 발생했습니다.")
            self.recorder.log_explanation("error", repr(e), run_id=rid)

        self.history.append({"role": "assistant", "text": reply})
        self.history[:] = self.history[-24:]
        self.recorder.log_conversation("assistant", reply, lang=lang, run_id=rid)
        await self._emit({"type": "transcript", "role": "assistant", "text": reply, "lang": lang})
        await self._emit({"type": "say", "text": reply, "lang": lang})

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
            return (("I'll hold off on that, sir — permission wasn't granted."
                     if lang == "en" else "그 작업은 보류하겠습니다, sir — 권한이 없습니다."), lang)

        result = await self._run_blocking(skill.handler, args)
        self.recorder.run_step(rid, f"result ok={result.get('ok')}",
                               status="ok" if result.get("ok") else "error",
                               data=result.get("data"))
        # UI-affecting intents (3D space etc.) tell the overlay what to do
        if name in UI_INTENTS or (result.get("data") or {}).get("ui"):
            await self._emit({"type": "ui", "action": (result["data"].get("ui") or name),
                              "args": result.get("data", {})})
        if not result.get("ok") and result.get("error"):
            self.recorder.log_explanation("error", str(result["error"]), run_id=rid)
        return result.get("speak", "Done, sir."), lang

    # ------------------------------------------------------------- chat/LLM #
    async def _do_chat(self, text, lang, control, rid):
        system = _load_prompt(lang)
        if control.style_directive:
            system += "\n\nSTYLE: " + control.style_directive
        try:
            raw = await self._run_blocking(self.llm.chat, system, text, list(self.history),
                                           control.llm_params)
        except Exception as e:
            self.recorder.log_explanation("error", f"LLM unavailable: {e!r}", run_id=rid)
            return ("I'm afraid my language engine is offline, sir — please ensure Ollama is "
                    "running." if lang == "en" else
                    "언어 엔진이 오프라인입니다, sir — Ollama가 실행 중인지 확인해 주세요.")

        # the model may request PC actions via <action .../> tags (requirement 14)
        actions = parse_action_tags(raw)
        spoken = strip_action_tags(raw)
        for act in actions:
            if act.name in REGISTRY:
                self.recorder.run_step(rid, f"LLM-requested action '{act.name}'")
                act_reply, _ = await self._do_skill(act.name, act.args, text, lang, rid)
                spoken = (spoken + " " + act_reply).strip()
        return spoken or ("Very good, sir." if lang == "en" else "알겠습니다, sir.")

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
    "system_info": _fu(["Watch it live", "Free up memory", "Check the battery"],
                       ["I can alert you if CPU or memory runs high, sir."],
                       ["실시간 보기", "메모리 정리", "배터리 확인"],
                       ["CPU나 메모리가 높아지면 알려드릴 수 있습니다, sir."]),
    "_default": _fu(["Anything else, sir?", "Repeat that", "Adjust my settings"],
                    ["You can say 'wake up Jarvis' any time, sir."],
                    ["더 필요하신 것이 있나요, sir?", "다시 말하기", "설정 변경"],
                    ["언제든 'wake up Jarvis'라고 부르시면 됩니다, sir."]),
}
