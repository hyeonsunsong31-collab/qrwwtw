"""
agent/nlu.py
===========
Language detection + flexible intent parsing (requirements 2, 12, 14).

* detect_language(): Hangul -> Korean, otherwise English (the two supported languages).
  Drives automatic input/output language switching (requirement 2).
* parse_intent(): maps free-form English OR Korean phrasing to a structured intent with
  arguments and a confidence. Deliberately permissive (synonyms, word order, filler words)
  for high command flexibility (requirement 14). Rules are ordered most-specific first;
  anything unmatched falls through to 'chat', which the LLM handles (and the LLM can still
  trigger tools through <action> tags).
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
    return "ko" if text and _HANGUL.search(text) else "en"


@dataclass
class Intent:
    name: str
    args: Dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    lang: str = "en"


SHAPES = {
    "cube": "box", "box": "box", "큐브": "box", "상자": "box",
    "sphere": "sphere", "ball": "sphere", "구": "sphere", "공": "sphere",
    "torus": "torus", "ring": "torus", "donut": "torus", "토러스": "torus", "링": "torus",
    "cone": "cone", "원뿔": "cone", "cylinder": "cylinder", "원기둥": "cylinder",
    "icosahedron": "icosa", "gem": "icosa",
}


def _rules():
    return [
        # ---------------- the self core (core/self_model.py) ----------------
        ("self_forget", [
            r"\b(forget|erase|wipe|clear|reset)\b.*\b(what|everything|all)\b.*\b(learn(ed|t)|know|remember(ed)?|taught)\b",
            r"\b(forget|erase|wipe|clear|reset)\b.*\b(your|the)\s+(memory|memories|self[- ]?model)\b",
            r"(배운|학습한|기억한|기억해\s*둔)\s*(것|내용).*(잊어|지워|삭제|초기화)",
            r"(기억|메모리).*(초기화|모두\s*지워|전부\s*지워|다\s*지워)",
        ]),
        ("self_remember", [
            r"^(please\s+)?remember\s+(that\s+)?(?P<fact>.+)$",
            r"^(from now on|in future|going forward)[, ]+(?P<fact>.+)$",
            r"^(?P<fact>(call me (?!a\b|an\b|the\b|back\b|later\b|when\b)|my name is |i am called ).+)$",
            r"^(?P<fact>.+?)\s*(라고|이라고|을|를)?\s*기억해\s*(줘|둬|주세요|두세요)?$",
            r"^(?P<fact>내\s*이름은\s*.+)$",
            r"^(?P<fact>(나를|저를)\s*.+(이?라고)\s*불러.*)$",
            r"^(앞으로는?|이제부터)\s*(?P<fact>.+)$",
        ]),
        ("self_describe", [
            r"\bwho\s+are\s+you\b", r"^what\s+are\s+you\??$", r"\btell me about yourself\b",
            r"\b(introduce|describe)\s+yourself\b",
            r"\bwhat('s| is)\s+your\s+(purpose|goal|directive|mission|prime directive)\b",
            r"\bwhat\s+(have|did)\s+you\s+learn(ed|t)?\b",
            r"\bwhat\s+do\s+you\s+(know|remember)\s+about\s+(me|yourself)\b",
            r"\bare\s+you\s+loyal\b", r"\bwho\s+do\s+you\s+serve\b",
            r"(너는|넌|당신은|자비스는?)\s*(누구|뭐야|무엇)", r"자기\s*소개",
            r"(너의?|네|당신의?|자비스의?)\s*(목적|목표|사명|지침)",
            r"(뭘|무엇을|뭐를)\s*(배웠|학습했|기억하)", r"나에\s*대해\s*(뭘|무엇을|뭐를)?\s*(알고|기억)",
        ]),
        # ---------------- 3D project space (requirements 5, 17, 18) ----------------
        ("open_space3d", [
            r"\b(open|launch|start|show|bring up|enter)\b.*\b(3d|three.?d|project space|modell?ing|workspace)\b",
            r"\b(3d|three.?d)\b.*\b(space|project|workspace)\b",
            r"(3d|프로젝트)\s*(공간|작업)?.*(열어|열기|시작|띄워|보여)",
            r"(모델링|작업\s*공간).*(열어|시작|띄워)",
        ]),
        ("close_space3d", [
            r"\b(close|exit|leave|hide)\b.*\b(3d|three.?d|project space|workspace)\b",
            r"(3d|프로젝트|작업)\s*(공간)?.*(닫아|닫기|종료|나가)",
        ]),
        ("clone_object", [
            r"\b(clone|duplicate)\b",
            r"\bcopy\b.*\b(object|this one|cube|sphere|model|selection)\b",
            r"(복제|클론)",
        ]),
        ("toggle_gesture", [
            r"\b(turn|switch)\s+(on|off)\b.*\b(gesture|hand|camera)\b",
            r"\b(gesture|hand tracking|camera control)s?\b.*\b(on|off)\b",
            r"(손동작|제스처|카메라\s*감지).*(켜|꺼|끄기|켜기|비활성|활성)",
        ]),
        ("add_object", [
            r"\b(add|create|spawn|make)\b\s+(a\s+|an\s+|another\s+|new\s+)*(?P<shape>cube|box|sphere|ball|torus|ring|donut|cone|cylinder|icosahedron|gem|object|shape)s?\b",
            r"(?P<shape>큐브|상자|구|공|토러스|링|원뿔|원기둥|오브젝트|도형)\s*(하나|를|을)?\s*(추가|만들어|생성)",
        ]),
        ("delete_object", [
            r"\b(delete|remove)\b\s+(the\s+|this\s+)?(selected\s+)?(object|cube|box|sphere|shape|torus|cone|cylinder)\b",
            r"(오브젝트|객체|도형).*(삭제|지워)",
        ]),
        ("save_project", [r"\bsave\b.*\b(project|scene|workspace)\b", r"(프로젝트|씬|작업\s*공간).*(저장)"]),
        ("export_model", [r"\bexport\b.*\b(model|scene|project|glb|it|this)\b", r"(모델|씬|프로젝트).*(내보내|익스포트)"]),
        ("neural_view", [r"\b(show|display|open|visuali[sz]e|toggle|hide)\b.*\b(neural|brain|neurons?|mesh)\b",
                         r"(신경망|뇌|뉴런).*(보여|띄워|시각화|숨겨)"]),
        ("wallpaper_off", [r"\b(stop|exit|leave|turn off|disable|end)\b.*\bwallpaper\b",
                           r"\bwallpaper\b.*\boff\b",
                           r"(배경\s*화면|바탕\s*화면).*(해제|끄|꺼|그만|원래대로)"]),
        ("wallpaper_on", [r"\b(use|set|make|turn)\b.*\b(as\s+(my\s+|the\s+)?|into\s+(my\s+|the\s+)?)?wallpaper\b",
                          r"\bwallpaper\s+mode\b",
                          r"(배경\s*화면|바탕\s*화면)\s*(으로\s*(사용|설정|써|해)|모드)"]),
        ("open_jarvis_settings", [r"\b(jarvis|your|assistant)\s+settings\b", r"\bsettings\s+panel\b",
                                  r"(자비스|비서)\s*설정"]),
        # ---------------- music (requirement 19) ----------------
        ("stop_music", [r"\b(stop|pause)\b.*\b(music|song|track|playback)\b",
                        r"(음악|노래|재생).*(멈춰|정지|중지|꺼)"]),
        ("play_music", [r"\bplay\b\s+(?P<query>.+?)(?:\s+(?:for me|please|now))?$",
                        r"\b(put on|play me|play some)\b\s+(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(노래|음악|곡)?\s*(틀어|재생|들려|플레이)\s*(줘|주라|해)?$"]),
        # ---------------- microphone / volume / media ----------------
        ("mute_mic", [r"\b(mute|turn off)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(음소거|꺼|끄기)"]),
        ("unmute_mic", [r"\b(unmute|turn on)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(켜|음소거\s*해제)"]),
        ("set_volume", [r"\b(set|turn)\b.*\bvolume\b.*?(?P<level>\d{1,3})\s*%?",
                        r"볼륨.*?(?P<level>\d{1,3})\s*(%|퍼센트)?"]),
        ("media_control", [r"^(?P<action>next|previous|prev|skip|pause|resume|play|back)\s*(track|song)?$",
                           r"^(다음|이전|일시정지|정지|재생)\s*(곡|트랙)?$"]),
        # ---------------- system info / devices / Windows settings ----------------
        ("system_info", [r"\b(system|pc|computer|cpu|memory|ram|disk|battery)\b.*\b(info|status|usage|how much|state)\b",
                         r"(시스템|pc|컴퓨터|cpu|메모리|램|디스크|배터리).*(상태|정보|사용량|얼마)"]),
        ("list_devices", [r"\b(list|show|what)\b.*\bdevices?\b", r"(장치|디바이스|기기).*(목록|보여|뭐)"]),
        ("settings", [r"\b(open|change|go to)\b.*\bsettings?\b", r"설정.*(열어|변경|바꿔|가)"]),
        # ---------------- files (requirement 20) ----------------
        ("file_search", [r"\b(find|search|locate|look for)\b\s+(?P<query>.+?)\s+(file|files|document)s?$",
                         r"\b(find|search)\b.*\bfiles?\b.*\b(named|called|about)\b\s+(?P<query>.+)$",
                         r"(?P<query>.+?)\s*(파일|문서).*(찾아|검색)\s*(줘|)?$"]),
        ("file_read", [r"\b(read|open|show)\b\s+(the\s+)?file\s+(?P<path>.+)$",
                       r"파일\s+(?P<path>.+?)\s*(읽어|열어|보여)\s*(줘|)?$"]),
        ("file_write", [r"\b(write|create|save|make)\b\s+(a\s+)?(new\s+)?file\s+(?P<path>\S+)",
                        r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(만들어|생성|저장)\s*(줘|)?$"]),
        ("file_delete", [r"\b(delete|remove|trash)\b\s+(the\s+)?file\s+(?P<path>\S+)",
                         r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(삭제|지워)\s*(줘|)?$"]),
        # ---------------- browser / web (requirement 20) ----------------
        ("open_url", [r"\b(open|go to|visit|browse to)\b\s+(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)$",
                      r"(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)\s*(열어|접속|가)\s*(줘|)?$"]),
        ("web_search", [r"\b(search|google|look up)\b\s+(for\s+)?(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(검색|찾아)\s*(해|줘|봐)\s*$"]),
        # ---------------- windows, keyboard, screen (requirement 20) ----------------
        ("screenshot", [r"\b(take|capture|grab)\b.*\b(screenshot|screen ?shot|screen)\b", r"\bscreenshot\b",
                        r"(스크린\s*샷|화면\s*(캡처|캡쳐))"]),
        ("type_text", [r"^(type|write out)\b\s+(?P<text>.+)$",
                       r"(?P<text>.+?)\s*(라고|이라고)\s*(입력|타이핑)\s*(해|해줘|해\s*줘)?$"]),
        ("press_keys", [r"^press\b\s+(?P<keys>[\w+ -]+)$", r"(?P<keys>[A-Za-z0-9+ -]+?)\s*(키를?|단축키를?)\s*(눌러|입력)"]),
        ("show_desktop", [r"\b(show|go to)\s+(the\s+)?desktop\b", r"(minimi[sz]e)\s+(all|everything)\b",
                          r"바탕\s*화면\s*(보여|으로\s*가)"]),
        ("window_minimize", [r"\bminimi[sz]e\b\s*(the\s+)?(?P<title>.*?)(\s+window)?$", r"(?P<title>.*?)\s*창?\s*최소화"]),
        ("window_maximize", [r"\bmaximi[sz]e\b\s*(the\s+)?(?P<title>.*?)(\s+window)?$", r"(?P<title>.*?)\s*창?\s*최대화"]),
        ("window_focus", [r"\b(switch to|focus( on)?|bring up)\b\s+(the\s+)?(?P<title>[\w .+-]+?)(\s+window)?$",
                          r"(?P<title>[\w .+-]+?)\s*(창|윈도우)\s*(으로|로)?\s*(전환|이동)"]),
        # ---------------- apps (requirement 20) ----------------
        ("open_app", [r"\b(open|launch|start|run)\b\s+(?P<app>[\w .+-]+?)(?:\s+(?:app|application|program|please|now))?$",
                      r"(?P<app>[\w .+-]+?)\s*(앱|프로그램|어플)?\s*(열어|실행|켜)\s*(줘|주라|)?$"]),
        ("close_app", [r"\b(close|quit|kill|exit)\b\s+(?P<app>[\w .+-]+?)(?:\s+(?:app|program))?$",
                       r"(?P<app>[\w .+-]+?)\s*(앱|프로그램)?\s*(닫아|종료|꺼)\s*(줘|)?$"]),
        # ---------------- assistant control ----------------
        ("sleep_assistant", [r"\b(go to sleep|stand down|that'?s all|sleep now|dismiss)\b",
                             r"(그만\s*쉬어|쉬어|자러\s*가|대기\s*모드)"]),
    ]


_COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in pats]) for name, pats in _rules()]
_STOPWORDS_APP = {"the", "my", "me", "please", "now", "a", "an", "up", "로", "을", "를", "좀"}
_NO_ARG_INTENTS = {"mute_mic", "unmute_mic", "stop_music", "open_space3d", "close_space3d",
                   "clone_object", "system_info", "sleep_assistant", "delete_object",
                   "save_project", "export_model", "neural_view", "wallpaper_on", "wallpaper_off",
                   "open_jarvis_settings", "screenshot", "show_desktop", "toggle_gesture",
                   "list_devices", "settings", "window_minimize", "window_maximize",
                   "self_describe", "self_forget"}


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
            args = {k: v.strip() for k, v in (m.groupdict() or {}).items() if v and v.strip()}
            if "app" in args:
                args["app"] = _clean_app(args["app"])
            if "query" in args:
                args["query"] = re.sub(r"^(for|about|the)\s+", "", args["query"].strip(" ?."),
                                       flags=re.IGNORECASE)
            if "shape" in args:
                shape = SHAPES.get(args["shape"].lower())
                if shape:
                    args["shape"] = shape
                else:
                    args.pop("shape")
            if "text" in args and name == "type_text":
                # keep the user's original casing for what gets typed
                mt = pat.search(t)
                if mt and mt.groupdict().get("text"):
                    args["text"] = mt.group("text").strip()
            if name == "self_remember":
                # keep the user's own words and casing for what is to be remembered
                mt = pat.search(t)
                if mt and mt.groupdict().get("fact"):
                    args["fact"] = mt.group("fact").strip()
            if name == "self_describe":
                if re.search(r"learn|know|remember|배웠|학습|알고|기억", tl):
                    args["about"] = "learned"
                elif re.search(r"purpose|goal|directive|mission|loyal|serve|목적|목표|사명|지침", tl):
                    args["about"] = "purpose"
                else:
                    args["about"] = "self"
            if name == "toggle_gesture":
                args["state"] = "off" if re.search(r"\boff\b|꺼|끄기|비활성", tl) else "on"
            conf = 0.8 if args or name in _NO_ARG_INTENTS else 0.6
            return Intent(name=name, args=args, confidence=conf, lang=lang)

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
