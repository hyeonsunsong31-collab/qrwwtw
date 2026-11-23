"""
Voice self-check (v1.4.2): "check the microphone", "can you hear me", "마이크 확인", "내 말 들려?".

Type it (or say it, if speech gets through) and JARVIS explains exactly why it can or cannot
hear you: which microphone is open and how loud it is, Windows' privacy switch and mute, the
wake-word listener and Whisper, and the last thing it heard -- with the fix for each problem.
The server registers the live voice report with set_provider().
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

_PROVIDER: Optional[Callable[[], dict]] = None


def set_provider(fn: Optional[Callable[[], dict]]) -> None:
    global _PROVIDER
    _PROVIDER = fn


def _ago(ts) -> str:
    try:
        s = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''} ago"
    if s < 3600:
        return f"{s // 60} minute{'s' if s // 60 != 1 else ''} ago"
    return f"{s // 3600} hour{'s' if s // 3600 != 1 else ''} ago"


def _ago_ko(ts) -> str:
    try:
        s = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return f"{s}초 전"
    if s < 3600:
        return f"{s // 60}분 전"
    return f"{s // 3600}시간 전"


def summarise(rep: dict) -> Tuple[str, str, List[str], Optional[str]]:
    """(English, Korean, fixes, settings page to open) for a voice report."""
    en: List[str] = []
    ko: List[str] = []
    fixes: List[str] = []
    action: Optional[str] = None
    engine = rep.get("engine")
    if engine not in ("online", None) or (engine is None and not rep.get("mic")):
        reason = rep.get("engine_error") or rep.get("reason") or engine
        en.append(f"My voice isn't running, sir: {reason}.")
        ko.append(f"음성 기능이 실행되고 있지 않습니다, sir: {reason}.")
        fixes.append(str(reason))
        return " ".join(en), " ".join(ko), fixes, action

    mic = rep.get("mic") or {}
    problems = rep.get("problems") or []
    blocking = [p for p in problems if p.get("blocking")]
    others = [p for p in problems if not p.get("blocking")]
    if not rep.get("listening", True):
        en.append("You've muted my listening with the JARVIS ear button, sir; click it again and I'll hear you.")
        ko.append("자비스 귀 버튼으로 듣기를 음소거하셨습니다, sir. 다시 누르시면 듣겠습니다.")
    elif mic.get("open"):
        conv = " (converted for me)" if mic.get("converted") else ""
        en.append(f"I'm listening through '{mic.get('device')}' at {mic.get('rate')} Hz{conv}.")
        ko.append(f"'{mic.get('device')}' 마이크로 {mic.get('rate')} Hz로 듣고 있습니다.")
        lvl = mic.get("level_db")
        if isinstance(lvl, (int, float)) and not blocking:
            if lvl <= -85:
                en.append("Right now it sends pure silence.")
                ko.append("지금은 완전한 무음이 들어옵니다.")
            elif lvl < -65:
                en.append(f"The room sounds very quiet ({lvl:.0f} dB) - fine, as long as I hear you when you speak.")
                ko.append(f"주변이 매우 조용합니다({lvl:.0f} dB). 말씀하실 때 들리면 괜찮습니다.")
            else:
                en.append(f"I hear the room at {lvl:.0f} dB, which is normal.")
                ko.append(f"주변 소리가 {lvl:.0f} dB로 들리며 정상입니다.")
    else:
        why = mic.get("reason") or mic.get("error") or "unknown reason"
        en.append(f"The microphone isn't open: {why}.")
        ko.append(f"마이크가 열려 있지 않습니다: {why}.")
    for p in blocking:
        en.append(f"{p.get('message')} {p.get('fix')}")
        ko.append(f"{p.get('message_ko')} {p.get('fix_ko')}")
        fixes.append(p.get("fix") or "")
        action = action or p.get("action")
    if not blocking:
        ep = mic.get("endpoint") or {}
        if ep.get("muted"):
            en.append("Windows has the default microphone muted - type 'unmute the microphone'.")
            ko.append("Windows에서 기본 마이크가 음소거되어 있습니다 - '마이크 켜 줘'라고 입력해 주세요.")
            fixes.append("unmute the microphone")
        elif isinstance(ep.get("volume"), (int, float)) and ep["volume"] < 0.15:
            en.append(f"Its input volume is only {int(ep['volume'] * 100)} percent; raise it in Settings > System > Sound > Input.")
            ko.append(f"입력 볼륨이 {int(ep['volume'] * 100)}퍼센트뿐입니다. 설정 > 시스템 > 소리 > 입력에서 높여 주세요.")
            fixes.append("raise the input volume")
        priv = mic.get("privacy") or {}
        if priv.get("blocked"):
            en.append("Windows' microphone privacy setting is blocking desktop apps: turn on 'Let desktop apps "
                      "access your microphone' in Settings > Privacy & security > Microphone.")
            ko.append("Windows 마이크 개인 정보 설정이 데스크톱 앱을 막고 있습니다. 설정 > 개인 정보 및 보안 > "
                      "마이크에서 '데스크톱 앱이 마이크에 액세스하도록 허용'을 켜 주세요.")
            action = action or "ms-settings:privacy-microphone"
    wake = rep.get("wake") or {}
    if wake.get("ready"):
        en.append("The wake listener is ready: say 'wake up Jarvis'.")
        ko.append("호출어 인식이 준비되었습니다: 'wake up Jarvis'라고 말씀하세요.")
    else:
        err = wake.get("error") or "not loaded yet"
        en.append(f"My wake-word listener isn't available ({err}); the WAKE button and Ctrl+Alt+J still work.")
        ko.append(f"호출어 인식기를 사용할 수 없습니다({err}). 깨우기 버튼과 Ctrl+Alt+J는 작동합니다.")
    stt = rep.get("stt") or {}
    st = stt.get("state")
    if st == "ready":
        en.append(f"Speech recognition (Whisper {stt.get('model')}) is ready.")
        ko.append(f"음성 인식(Whisper {stt.get('model')})이 준비되었습니다.")
    elif st in ("loading", "downloading", "idle"):
        en.append("Speech recognition is still loading" + (" - downloading its model" if st == "downloading" else "") + ".")
        ko.append("음성 인식을 아직 불러오는 중입니다" + (" (모델 다운로드 중)" if st == "downloading" else "") + ".")
    else:
        en.append(f"Speech recognition isn't working: {stt.get('error') or st}.")
        ko.append(f"음성 인식이 작동하지 않습니다: {stt.get('error') or st}.")
    for p in others:
        if p.get("code") not in ("wake_unavailable", "stt_failed"):
            en.append(f"{p.get('message')} {p.get('fix')}")
            ko.append(f"{p.get('message_ko')} {p.get('fix_ko')}")
    heard = rep.get("last_heard") or {}
    if heard.get("text"):
        en.append(f"The last thing I heard was '{heard['text']}', {_ago(heard.get('at'))}.")
        ko.append(f"마지막으로 들은 말은 '{heard['text']}'입니다({_ago_ko(heard.get('at'))}).")
    if not blocking and wake.get("ready") and st == "ready" and mic.get("open") and rep.get("listening", True):
        en.append("Everything looks right, sir.")
        ko.append("모두 정상으로 보입니다, sir.")
    return " ".join(en), " ".join(ko), [f for f in fixes if f], action


def check(**_) -> dict:
    try:
        rep = _PROVIDER() if _PROVIDER is not None else {"engine": "off", "reason": "the voice engine isn't running"}
    except Exception as e:
        rep = {"engine": "failed", "engine_error": repr(e)[:200]}
    en, ko, fixes, action = summarise(rep or {})
    return {"ok": True, "speak": en, "data": {"report": rep, "speak_ko": ko, "fixes": fixes, "action": action},
            "error": None}
