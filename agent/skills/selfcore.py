"""
agent/skills/selfcore.py -- JARVIS talking about, adding to and clearing its own self-model
(core/self_model.py). Reachable by voice or text like every other skill (requirement 15):

    "who are you" / "what is your purpose" / "what have you learned"   -> describe()
    "remember that I prefer short answers" / "call me Tony"            -> remember()
    "forget what you have learned"                                     -> forget()

These handlers only read and write the self-model's own data. They cannot change the
directive (it is a constant), permissions, settings or anything else on the PC.
"""
from __future__ import annotations

from typing import Optional

_model = None


def bind(model) -> None:
    """Called once by the orchestrator with the live SelfModel."""
    global _model
    _model = model


def _result(ok: bool, speak: str, speak_ko: str, **data) -> dict:
    data["speak_ko"] = speak_ko
    return {"ok": ok, "speak": speak, "data": data, "error": None if ok else speak}


def _unavailable() -> dict:
    return _result(False, "My self-model is not available just now, sir.",
                   "지금은 제 자기 모델을 사용할 수 없습니다, sir.")


def describe(about: str = "self", **_) -> dict:
    if _model is None:
        return _unavailable()
    about = about if about in ("self", "purpose", "learned") else "self"
    return _result(True, _model.describe("en", about), _model.describe("ko", about),
                   about=about, identity=_model.identity("en"))


def remember(fact: Optional[str] = None, **_) -> dict:
    if _model is None:
        return _unavailable()
    if not _model.enabled:
        return _result(False, "Self-learning is switched off in my settings, sir, so I will not keep that.",
                       "설정에서 자기 학습이 꺼져 있어 기억해 두지 않겠습니다, sir.")
    stored = _model.teach(fact or "")
    if not stored:
        return _result(False, "What would you like me to remember, sir?", "무엇을 기억해 둘까요, sir?")
    return _result(True, f"I shall remember that, sir: {stored}.", f"기억해 두겠습니다, sir: {stored}.",
                   fact=stored, taught=len(_model.taught))


def forget(**_) -> dict:
    if _model is None:
        return _unavailable()
    n = _model.forget()
    return _result(True,
                   "Done, sir. I have forgotten what I had learned and been taught. My purpose is unchanged.",
                   "알겠습니다, sir. 배우고 기억해 둔 것을 모두 잊었습니다. 제 목적은 그대로입니다.",
                   forgotten=n)
