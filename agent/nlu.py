"""
agent/nlu.py
===========
Language detection + flexible intent parsing (requirements 2, 12, 14).

* detect_language(): Hangul-aware; uses langdetect when available, else a fast heuristic.
  Drives automatic input/output language switching (requirement 2).
* parse_intent(): maps free-form English OR Korean phrasing to a structured intent with
  arguments and a confidence. Deliberately permissive (synonyms, word order, extra words)
  for high command flexibility (requirement 14). Low-confidence inputs fall through to
  'chat', which the LLM handles.
* parse_action_tags(): extracts <action name="...">{...}</action> the LLM may emit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_HANGUL = re.compile(r"[가-힣]")
_ACTION_TAG = re.compile(r'<action\s+name="([a-z_]+)"\s*>\s*(\{.*?\})?\s*</action>',
                         re.DOTALL | re.IGNORECASE)


def detect_language(text: str) -> str:
    if text and _HANGUL.search(text):
        return "ko"
    try:
        from langdetect import detect  # type: ignore
        code = detect(text)
        return "ko" if code == "ko" else "en"
    except Exception:
        return "en"


@dataclass
class Intent:
    name: str
    args: Dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    lang: str = "en"


# Each rule: intent -> list of (regex, arg-extractor group map). Bilingual keywords.
# Written permissively so natural phrasing in either language matches.
def _rules():
    return [
        # --- 3D project space (requirements 5, 17, 18) ---
        ("open_space3d", [
            r"\b(open|launch|start|show|bring up|enter)\b.*\b(3d|three.?d|project space|modell?ing|modeling|workspace)\b",
            r"\b(3d|three.?d)\b.*\b(space|project|workspace)\b.*\b(open|start|show)?\b",
            r"(3d|프로젝트)\s*(공간|작업)?.*(열어|열기|시작|띄워|보여)",
            r"(모델링|작업\s*공간).*(열어|시작|띄워)",
        ]),
        ("close_space3d", [
            r"\b(close|exit|leave|hide)\b.*\b(3d|three.?d|project space|workspace)\b",
            r"(3d|프로젝트|작업)\s*(공간)?.*(닫아|닫기|종료|나가)",
        ]),
        ("clone_object", [
            r"\b(clone|duplicate|copy)\b.*\b(object|this|it|cube|sphere|model|selection)\b",
            r"(복제|복사|클론).*(해|해줘|할래)?",
        ]),
        ("toggle_gesture", [
            r"\b(turn|switch)\s+(on|off)\b.*\b(gesture|hand|camera)\b",
            r"\b(gesture|hand tracking|camera control)\b.*\b(on|off)\b",
            r"(손동작|제스처|카메라\s*감지).*(켜|꺼|끄기|켜기|비활성|활성)",
        ]),
        # --- music (requirement 19) ---
        ("stop_music", [r"\b(stop|pause)\b.*\b(music|song|track|playback)\b",
                        r"(음악|노래|재생).*(멈춰|정지|중지|꺼)"]),
        ("play_music", [r"\bplay\b\s+(?P<query>.+?)(?:\s+(?:for me|please|now))?$",
                        r"\b(put on|play me|play some)\b\s+(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(노래|음악|곡)?\s*(틀어|재생|들려|플레이)\s*(줘|주라|해)?$"]),
        # --- apps (requirement 20) ---
        ("open_app", [r"\b(open|launch|start|run)\b\s+(?P<app>[\w .+-]+?)(?:\s+(?:app|application|program|please|now))?$",
                      r"(?P<app>[\w .+-]+?)\s*(앱|프로그램|어플)?\s*(열어|실행|켜)\s*(줘|주라|)?$"]),
        ("close_app", [r"\b(close|quit|kill|exit)\b\s+(?P<app>[\w .+-]+?)(?:\s+(?:app|program))?$",
                       r"(?P<app>[\w .+-]+?)\s*(앱|프로그램)?\s*(닫아|종료|꺼)\s*(줘|)?$"]),
        # --- mic mute (requirement 6) ---
        ("mute_mic", [r"\b(mute|turn off)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(음소거|꺼|끄기)"]),
        ("unmute_mic", [r"\b(unmute|turn on)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(켜|음소거\s*해제)"]),
        # --- volume / media ---
        ("set_volume", [r"\b(set|turn)\b.*\bvolume\b.*?(?P<level>\d{1,3})\s*%?",
                        r"볼륨.*?(?P<level>\d{1,3})\s*(%|퍼센트)?"]),
        ("media_control", [r"\b(next|previous|prev|skip|pause|resume|play|back)\b\s*(track|song)?$",
                           r"(다음|이전|일시정지|정지|재생)\s*(곡|트랙)?$"]),
        # --- system info / settings / devices ---
        ("system_info", [r"\b(system|pc|computer|cpu|memory|ram|disk|battery)\b.*\b(info|status|usage|how much|state)\b",
                         r"(시스템|pc|컴퓨터|cpu|메모리|램|디스크|배터리).*(상태|정보|사용량|얼마)"]),
        ("list_devices", [r"\b(list|show|what)\b.*\bdevices?\b", r"(장치|디바이스|기기).*(목록|보여|뭐)"]),
        ("settings", [r"\b(open|change|go to)\b.*\bsettings?\b", r"설정.*(열어|변경|바꿔|가)"]),
        # --- files (requirement 20) ---
        ("file_search", [r"\b(find|search|locate|look for)\b\s+(?P<query>.+?)\s+(file|files|document)s?$",
                         r"\b(find|search)\b.*\bfiles?\b.*\b(named|called|about)\b\s+(?P<query>.+)$",
                         r"(?P<query>.+?)\s*(파일|문서).*(찾아|검색)\s*(줘|)?$"]),
        ("file_read", [r"\b(read|open|show)\b\s+(the\s+)?file\s+(?P<path>.+)$",
                       r"파일\s+(?P<path>.+?)\s*(읽어|열어|보여)\s*(줘|)?$"]),
        ("file_write", [r"\b(write|create|save|make)\b\s+(a\s+)?(new\s+)?file\s+(?P<path>\S+)",
                        r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(만들어|생성|저장)\s*(줘|)?$"]),
        ("file_delete", [r"\b(delete|remove|trash)\b\s+(the\s+)?file\s+(?P<path>\S+)",
                         r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(삭제|지워)\s*(줘|)?$"]),
        # --- browser / web (requirement 20) ---
        ("open_url", [r"\b(open|go to|visit|browse to)\b\s+(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)$",
                      r"(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)\s*(열어|접속|가)\s*(줘|)?$"]),
        ("web_search", [r"\b(search|google|look up)\b\s+(for\s+)?(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(검색|찾아)\s*(해|줘|봐)\s*$"]),
        # --- assistant control ---
        ("sleep_assistant", [r"\b(go to sleep|stand down|that'?s all|sleep now|dismiss)\b",
                             r"(그만|쉬어|자러\s*가|대기|꺼져있어)"]),
    ]


_COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in pats]) for name, pats in _rules()]

_STOPWORDS_APP = {"the", "my", "me", "please", "now", "a", "an", "up", "로", "을", "를", "좀"}


def _clean_app(name: str) -> str:
    name = name.strip().strip(".").strip()
    parts = [w for w in name.split() if w.lower() not in _STOPWORDS_APP]
    return " ".join(parts) if parts else name


def parse_intent(text: str, lang: Optional[str] = None) -> Intent:
    t = (text or "").strip()
    lang = lang or detect_language(t)
    tl = t.lower()

    for name, patterns in _COMPILED:
        for pat in patterns:
            m = pat.search(tl)
            if not m:
                continue
            args = {k: v for k, v in (m.groupdict() or {}).items() if v}
            if "app" in args:
                args["app"] = _clean_app(args["app"])
            if "query" in args:
                # don't let 'play'/'search' swallow filler words
                args["query"] = re.sub(r"^(for|about|the)\s+", "", args["query"].strip(" ?."),
                                       flags=re.IGNORECASE)
            conf = 0.8 if args or name in ("mute_mic", "unmute_mic", "stop_music",
                                           "open_space3d", "close_space3d", "clone_object",
                                           "system_info", "sleep_assistant") else 0.6
            return Intent(name=name, args=args, confidence=conf, lang=lang)

    # greetings / short chit-chat stay as chat
    return Intent(name="chat", args={"text": t}, confidence=0.3, lang=lang)


def parse_action_tags(text: str) -> List[Intent]:
    out: List[Intent] = []
    for m in _ACTION_TAG.finditer(text or ""):
        name = m.group(1).lower()
        try:
            args = json.loads(m.group(2)) if m.group(2) else {}
        except ValueError:
            args = {}
        out.append(Intent(name=name, args={str(k): str(v) for k, v in args.items()},
                          confidence=0.9))
    return out


def strip_action_tags(text: str) -> str:
    return _ACTION_TAG.sub("", text or "").strip()
