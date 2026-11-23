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
_ACTION_TAG = re.compile(r"<action\b([^>]*?)(/>|>(.*?)</action\s*>)", re.DOTALL | re.IGNORECASE)


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


# "3D" as speech recognition writes it: 3D, 3-D, 3 D, three D, three-dimensional
_3D = r"(?:\b3\s*-?\s*d\b|\bthree\s*-?\s*d\b|\b(?:3|three)[- ]dimensional\b)"
# commands about things INSIDE the 3D space (objects, gestures, neural view, wallpaper, saving)
# belong to their own intents, not to opening or closing the space
_NOT_3D_SUB = (r"(?!.*\b(gestures?|hand tracking|hands?|camera|neural|brain|neurons?|mesh|wallpaper|objects?|"
               r"cube|box|sphere|ball|torus|ring|donut|cone|cylinder|icosahedron|gem|shapes?|save|export|"
               r"clone|duplicate|add|delete|remove)\b)")


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
        # ---------------- clock: time, timers, reminders (v1.5.0) ----------------
        ("set_timer", [
            r"\bremind me\b(\s+to)?\s+(?P<what>.+?)\s+(in|after)\s+(?P<duration>(an?|\d+|one|two|three|four|five|ten|fifteen|twenty|thirty|half an?)\b.*)$",
            r"\bremind me\b\s+(in|after)\s+(?P<duration>.+?)\s+(to|that)\s+(?P<what>.+)$",
            r"\bremind me\b(\s+to)?\s+(?P<what>.+?)\s+at\s+(?P<at>(\d{1,2}([:.]\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?|noon|midnight)(\s*(today|tonight|tomorrow))?)$",
            r"\bremind me\b\s+at\s+(?P<at>\d{1,2}([:.]\d{2})?\s*(am|pm)?)\s+(to|that)\s+(?P<what>.+)$",
            r"\b(set|start|make|create)\b\s+(an?\s+)?(timer|alarm|countdown)\s+(for|of)\s+(?P<duration>.+?)(\s+(called|named|for)\s+(?P<what>.+))?$",
            r"\b(set|start|make|create)\b\s+(an?\s+)?(?P<duration>(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty|ninety|half an?)[\w -]*?)\s+(timer|alarm|countdown)\b",
            r"^(?P<duration>\d+\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)(\s+(and\s+)?\d+\s*(seconds?|minutes?|mins?))?)\s+timer$",
            r"\b(wake me( up)?|alert me|tell me)\b\s+(in|after)\s+(?P<duration>.+)$",
            r"(?P<duration>\d+\s*(시간|분|초)(\s*\d+\s*(분|초))?)\s*(뒤|후)에?\s*(?P<what>.*?)\s*(알려|깨워|말해)\s*(줘|주세요)?$",
            r"(?P<duration>\d+\s*(시간|분|초)(\s*\d+\s*(분|초))?)\s*(짜리\s*)?(타이머|알람)",
            r"(타이머|알람)\s*(?P<duration>\d+\s*(시간|분|초)(\s*\d+\s*(분|초))?)",
        ]),
        ("cancel_timer", [r"\b(cancel|stop|delete|remove|clear|turn off)\b\s+(the\s+|my\s+|that\s+|all\s+(the\s+|my\s+)?)?(?P<what>timers?|reminders?|alarms?|countdown)\b",
                          r"(타이머|알림|알람)\s*(을|를)?\s*(취소|꺼|삭제|멈춰)"]),
        ("list_timers", [r"\b(what|which|any|how many)\b.*\b(timers?|reminders?|alarms?)\b",
                         r"\bhow (much|long)\b.*\b(left|remaining)\b.*\b(timer|reminder)?\b",
                         r"\b(timers?|reminders?)\s+(status|left)\b", r"(타이머|알림|알람).*(남았|확인|뭐|있어)"]),
        ("time_now", [
            r"^what('?s| is)?\s+(the\s+)?(time|date|day)(\s+(is it|now|today|right now))?$",
            r"\bwhat time is it\b", r"\bwhat('?s| is) (today'?s|the current) (date|time|day)\b",
            r"\bwhat day (of the week )?is (it|today)\b", r"^(the\s+)?(time|date)( now| please)?$",
            r"\b(tell me|do you know) (the )?(time|date)\b",
            r"(지금\s*)?몇\s*시(야|예요|에요|인가요|니)?$", r"(지금|현재)\s*시간", r"오늘\s*(날짜|며칠|몇\s*일|무슨\s*요일)",
        ]),
        # ---------------- the PC itself: power and lock (before "close <app>") ----------------
        ("power", [
            r"\b(?P<shutdown>shut ?down|turn off|power off|switch off|power down)\b.*\b(computer|pc|laptop|system|machine|windows|everything)\b",
            r"^(?P<shutdown2>shut ?down|power off)$",
            r"\b(?P<restart>restart|reboot)\b.*\b(computer|pc|laptop|system|machine|windows)\b|^(?P<restart2>restart|reboot)$",
            r"\b(put|send)\b.*\b(computer|pc|laptop|system|machine)\b.*\bto sleep\b|\b(?P<sleep>sleep|hibernate|suspend)\b\s+(the\s+|my\s+)?(computer|pc|laptop|machine)\b",
            r"(컴퓨터|피시|pc|노트북|윈도우).*(?P<restart_ko>재시작|다시\s*시작|재부팅)",
            r"(컴퓨터|피시|pc|노트북).*(?P<sleep_ko>절전|잠자기|최대\s*절전)",
            r"(컴퓨터|피시|pc|노트북|윈도우).*(?P<shutdown_ko>꺼|끄|종료|전원)",
        ]),
        ("lock", [
            r"\block\b\s+(the\s+|my\s+|this\s+)?(computer|pc|screen|laptop|workstation|windows|machine|it|up)\b",
            r"^lock$", r"(컴퓨터|화면|피시|pc).*(잠가|잠궈|잠금)",
        ]),
        # ---------------- 3D project space (requirements 5, 17, 18) ----------------
        # Closing comes FIRST: "close the 3D space" also mentions "3D space", and used to be
        # read as "open the 3D space" (v1.5.0 fix). Speech recognition writes 3D, 3-D, 3 D or
        # "three D". Commands about objects, gestures, the neural view or the wallpaper inside
        # the 3D space go to their own intents. ("workspace" now means a developer workspace.)
        ("close_space3d", [
            rf"^{_NOT_3D_SUB}.*\b(close|exit|leave|hide|quit|shut|turn off|switch off|dismiss|end|stop|"
            rf"get out of|go out of|minimi[sz]e|put away)\b.*({_3D}|\bproject space\b|\bholo(gram|graphic)? space\b|"
            rf"\bmodell?ing (space|mode|view)\b)",
            rf"^{_NOT_3D_SUB}.*({_3D}|\bproject space\b).*\b(off|away|closed?)\s*[.!]?$",
            r"(3\s*d|쓰리\s*디|3\s*차원|프로젝트|홀로그램)\s*(공간|모드|화면|뷰)?.*(닫|종료|나가|꺼|끄|끝내|그만)",
        ]),
        ("open_space3d", [
            rf"^{_NOT_3D_SUB}.*\b(open|launch|start|show|bring up|enter|go (in)?to|switch to|turn on)\b.*"
            rf"({_3D}|\bproject space\b|\bmodell?ing\b)",
            rf"^{_NOT_3D_SUB}.*{_3D}.*\b(space|project|mode|view|room|scene|world|editor)\b",
            r"(3\s*d|쓰리\s*디|3\s*차원|프로젝트)\s*(공간|모드|화면)?.*(열어|열기|시작|띄워|보여|켜)",
            r"(모델링).*(열어|시작|띄워)",
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
        ("save_project", [r"\bsave\b.*\b(project|scene)\b", r"(프로젝트|씬|3\s*d\s*공간).*(저장)"]),
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
        ("media_control", [
            r"^(?P<action>skip|next)(\s+(this|the|that))?(\s+(one|song|track|tune))?$",
            r"^(play\s+|go\s+to\s+)?(the\s+)?(?P<action>next)\s+(song|track|one|tune)$",
            r"^(play\s+|go\s+)?(the\s+)?(?P<action>previous|last)\s+(song|track|one)(\s+again)?$",
            r"^(go\s+)?(?P<action>back)\s+(a|one)\s+(song|track)$",
            r"^(?P<action>resume|continue)(\s+(the|my))?\s+(music|song|playback|playing)$",
            r"(?P<action_ko>다음|이전)\s*(곡|노래|트랙)",
        ]),
        ("stop_music", [r"\b(stop|pause)\b.*\b(music|song|track|playback)\b",
                        r"(음악|노래|재생).*(멈춰|정지|중지|꺼)"]),
        ("play_music", [r"^(?!.*\b(next|previous|last)\s+(song|track)\b)(?!.*\bplay\s?list\s+of\s+commands\b).*\bplay\b\s+(?P<query>.+?)(?:\s+(?:for me|please|now))?$",
                        r"\b(put on|play me|play some)\b\s+(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(노래|음악|곡)?\s*(틀어|재생|플레이)\s*(줘|주라|해)?$",
                        r"(?P<query>.+?)\s*(노래|음악|곡)?\s*들려\s*(줘|주라|주세요)$"]),
        # ---------------- microphone / volume / media ----------------
        # ---------------- shell commands (v1.4.3) ----------------
        ("shell_permission", [
            r"\b(allow|enable|permit|let)\b.*\b(shell|powershell|terminal|command line|cmd)\b\s*(commands?|access|runs?)?",
            r"\b(?P<level_ask>always ask|ask me)\b.*\b(before|for)\b.*\b(shell|powershell|terminal)\b",
            r"\b(?P<level_deny>block|disable|forbid|deny|disallow|turn off)\b.*\b(shell|powershell|terminal)\b",
            r"(셸|쉘|파워셸|파워쉘|터미널|명령어?\s*실행).*(?P<level_ko>허용|허락|켜|차단|막아|금지|물어)"]),
        ("run_shell_elevated", [
            r"^(please\s+)?(run|execute)\s+(this\s+)?(command\s+)?as\s+(an?\s+)?admin(istrator)?\s*[:,-]?\s*(?P<command>.+)$",
            r"^(please\s+)?(run|execute)\s+(the\s+command\s+)?(?P<command>.+?)\s+as\s+(an?\s+)?admin(istrator)?$",
            r"^(admin|elevated|administrator)\s+(powershell|shell|cmd|command|terminal)\s*[:>]\s*(?P<command>.+)$",
            r"^관리자\s*(권한)?\s*(으로|로)\s*(?P<command>.+?)\s*(명령어?\s*)?(을|를)?\s*(실행|돌려)\s*(해|해줘|해\s*줘|줘)?$"]),
        ("run_shell", [
            r"^(please\s+)?(run|execute)\s+(the\s+|this\s+)?(shell\s+|powershell\s+|cmd\s+|terminal\s+)?command\s*[:,-]?\s*(?P<command>.+)$",
            r"^(in|on|using)\s+(the\s+)?(powershell|shell|terminal|command prompt|cmd)\s*[,:]?\s*(run\s+|execute\s+)?(?P<command>.+)$",
            r"^(powershell|shell|cmd|terminal|ps)\s*[:>]\s*(?P<command>.+)$",
            r"^[$>]\s*(?P<command>\S.*)$",
            r"^(?P<command>(ping|tracert|nslookup|ipconfig|netstat|systeminfo|tasklist|whoami|hostname|getmac|pathping)\b.*?)"
            r"(\s+for\s+me)?(\s+(and|then)\s+(show|tell|give)\s+me\s+(the\s+)?(result|results|output))?$",
            r"^(run|execute)\s+(?P<command>(ipconfig|ping|netstat|tracert|nslookup|systeminfo|tasklist|taskkill|whoami|hostname|getmac|"
            r"winget|python|py|node|npm|npx|pip|git|dotnet|java|sfc|chkdsk|wmic|dir|ls|cd|echo|get-\w+|set-\w+|curl|wsl|choco|"
            r"cls|ver|arp|route|pathping|driverquery|powercfg|gpupdate|cleanmgr)\b.*)$",
            r"^(셸|쉘|터미널|파워셸|파워쉘|명령\s*프롬프트|cmd)\s*(에서|으로|로)\s*(?P<command>.+?)\s*(을|를)?\s*(실행|돌려)\s*(해|해줘|해\s*줘|줘)?$",
            r"^(?P<command>.+?)\s*(명령어?|커맨드)\s*(을|를)?\s*(실행|돌려)\s*(해|해줘|해\s*줘|줘)?$"]),
        # ---------------- the language engine (Ollama) ----------------
        ("llm_install", [r"\b(install|set up|setup|download|get)\b.*\b(language engine|ollama)\b",
                         r"(언어\s*엔진|올라마|ollama).*(설치|깔아|받아)"]),
        ("llm_status", [r"\b(?P<restart>restart|reconnect|reboot|start up|start|turn on)\b.*\b(language engine|language model|ollama|llm)\b",
                        r"\b(check|test|diagnose|troubleshoot|fix|repair)\b.*\b(language engine|language model|ollama|llm|ai engine)\b",
                        r"\b(language engine|language model|ollama|llm)\b.*\b(status|offline|not working|isn'?t working|down|problem|connect)",
                        r"\bis (the |your )?(language engine|language model|ollama|llm)\b.*\b(running|working|online|up|ok)\b",
                        r"\bwhy (are|is) (you|jarvis|the language engine|your language engine)\b.*\b(offline|not working|unavailable|disconnected)\b",
                        r"\bwhy (can'?t|cannot|won'?t) you (think|talk|chat|answer properly|connect)\b",
                        r"(언어\s*엔진|언어\s*모델|올라마|ollama|llm).*(?P<restart_ko>다시\s*시작|재시작|켜)",
                        r"(언어\s*엔진|언어\s*모델|올라마|ollama|llm).*(확인|점검|상태|안\s*돼|안돼|안\s*되|연결|문제|오프라인|고쳐)",
                        r"왜.*(오프라인|연결이?\s*안|연결되지\s*않|제대로\s*대답)"]),
        ("mic_settings", [r"\b(open|show|go to)\b.*\b(mic|microphone)\b.*\b(privacy|permission|access|settings?)\b",
                          r"\b(mic|microphone)\s+(privacy|permission|access)\b",
                          r"마이크.*(개인\s*정보|권한|설정).*(열어|보여|가|띄워)"]),
        ("voice_status", [r"\b(check|test|diagnose|troubleshoot|fix)\b.*\b(mic|microphone|voice|hearing|audio input|wake ?word)\b",
                          r"\bcan you hear me\b", r"\bare you (listening|hearing me)\b",
                          r"\b(mic|microphone|voice|wake ?word)\s+(status|check|test|problems?)\b",
                          r"\bwhy (can'?t|cannot|don'?t|do not|won'?t|aren'?t) you (hear|listen|respond|wake)\b",
                          r"\b(voice|wake ?word|microphone|mic)\b.*\b(not working|isn'?t working|doesn'?t work|does not work)\b",
                          r"마이크.*(확인|테스트|점검|상태|안\s*돼|안돼|안\s*되|작동|문제)",
                          r"(내\s*)?(말|목소리).*(들려|들리|들립)",
                          r"(음성\s*인식|음성|호출어|웨이크).*(확인|테스트|점검|안\s*돼|안돼|안\s*되|상태|문제)"]),
        ("mute_mic", [r"\b(mute|turn off)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(음소거|꺼|끄기)"]),
        ("unmute_mic", [r"\b(unmute|turn on)\b.*\b(mic|microphone)\b", r"(마이크|마이크로폰).*(켜|음소거\s*해제)"]),
        ("set_volume", [r"\b(set|turn|change|put)\b.*\b(volume|sound)\b.*?(?P<level>\d{1,3})\s*(%|percent)?",
                        r"\bvolume\b\s*(to|at)?\s*(?P<level>\d{1,3})\s*(%|percent)?$",
                        r"볼륨.*?(?P<level>\d{1,3})\s*(%|퍼센트)?",
                        r"^(turn|crank|bring|pump)\s+(it|the volume|the sound|the music|that)\s+(?P<level_up>up)(\s+(a bit|a little|more))?$",
                        r"^(turn|bring)\s+(it|the volume|the sound|the music|that)\s+(?P<level_down>down)(\s+(a bit|a little))?$",
                        r"\b(volume|sound)\s+(?P<level_up2>up)\b|\b(?P<level_up4>increase|raise|boost)\s+(the\s+)?(volume|sound)\b|^(?P<level_up3>louder)(\s+please)?$",
                        r"\b(volume|sound)\s+(?P<level_down2>down)\b|\b(?P<level_down4>decrease|lower|reduce)\s+(the\s+)?(volume|sound)\b|^(?P<level_down3>quieter|softer)(\s+please)?$",
                        r"^(it'?s|this is|that'?s|the (music|sound|volume) is)\s+(way\s+|much\s+|a (bit|little)\s+|really\s+|so\s+)?(?P<level_down5>too loud|loud|deafening)\b",
                        r"^(it'?s|this is|that'?s|the (music|sound|volume) is)\s+(way\s+|much\s+|a (bit|little)\s+|really\s+|so\s+)?(?P<level_up5>too quiet|too soft|too low)\b|\bi (?P<level_up6>can'?t hear) (it|anything|the (music|sound|video))\b",
                        r"\b(volume|sound)\b.*\b(?P<level_down6>way down|down a bit|lower|softer|quieter)\b|\b(volume|sound)\b.*\b(?P<level_up7>way up|up a bit|higher|louder)\b",
                        r"^(?P<level_unmute>unmute)(\s+(the\s+)?(sound|audio|volume|speakers?|pc|computer|music))?$",
                        r"^(?P<level_mute>mute)(\s+(the\s+)?(sound|audio|volume|speakers?|pc|computer|music|everything))?$",
                        r"(볼륨|소리|음량).*(?P<level_up_ko>올려|키워|높여|크게)",
                        r"(볼륨|소리|음량).*(?P<level_down_ko>내려|줄여|낮춰|작게)",
                        r"^(?P<level_mute_ko>음소거)(\s*(해|해\s*줘|해줘))?$|(소리|스피커)\s*(?P<level_mute_ko2>음소거|꺼)"]),
        ("set_brightness", [r"\bbrightness\b.*?(?P<level>\d{1,3})\s*(%|percent)?",
                            r"\b(?P<level_up3>increase|raise|turn up|boost|up)\b.*\bbrightness\b|\bbrightness\s+(?P<level_up>up)\b|\b(?P<level_up2>brighter)\b|\b(?P<level_up4>brighten)\b",
                            r"\b(?P<level_down3>decrease|lower|turn down|reduce|dim)\b.*\b(brightness|screen)\b|\bbrightness\s+(?P<level_down>down)\b|\b(?P<level_down2>dimmer|darker)\b",
                            r"^(it'?s|the screen is|my screen is|this is)\s+(way\s+|much\s+|a (bit|little)\s+|really\s+|so\s+)?(?P<level_down4>too bright)\b",
                            r"^(it'?s|the screen is|my screen is|this is)\s+(way\s+|much\s+|a (bit|little)\s+|really\s+|so\s+)?(?P<level_up5>too dark|too dim)\b",
                            r"\bbrightness\b.*\b(?P<level_down5>way down|down a bit|lower|darker|dimmer)\b|\bbrightness\b.*\b(?P<level_up6>way up|up a bit|higher|brighter)\b",
                            r"밝기.*?(?P<level>\d{1,3})\s*(%|퍼센트)?",
                            r"(밝기|화면).*(?P<level_up_ko>올려|높여|밝게)", r"(밝기|화면).*(?P<level_down_ko>내려|낮춰|어둡게)"]),
        ("media_control", [r"^(?P<action>next|previous|prev|skip|pause|resume|play|back)\s*(track|song)?$",
                           r"^(다음|이전|일시정지|정지|재생)\s*(곡|트랙)?$"]),
        # ---------------- system info / devices / Windows settings ----------------
        ("system_info", [r"\b(system|pc|computer|cpu|memory|ram|disk|battery)\b.*\b(info|status|usage|how much|state|level|left|percentage)\b",
                         r"\bhow much (battery|memory|ram|disk|storage|space|cpu)\b", r"\bhow('?s| is) (my |the )?(battery|cpu|memory|pc|computer)\b",
                         r"(시스템|pc|컴퓨터|cpu|메모리|램|디스크|배터리).*(상태|정보|사용량|얼마)"]),
        ("list_devices", [r"\b(list|show|what)\b.*\bdevices?\b", r"(장치|디바이스|기기).*(목록|보여|뭐)"]),
        ("settings", [r"\b(turn|switch)\s+(on|off)\s+(the\s+|my\s+)?(?P<page>bluetooth|wi-?fi|wireless|airplane mode|flight mode|night light|hotspot|mobile hotspot|dark mode|light mode|focus assist|do not disturb)\b",
                      r"\b(?P<page2>bluetooth|wi-?fi|airplane mode|night light|hotspot|display|sound|network|update|windows update|battery|power|privacy|apps|personali[sz]ation|wallpaper|background|mouse|keyboard|language|time|date) settings\b",
                      r"\b(open|change|go to)\b.*\bsettings?\b", r"(블루투스|와이파이|비행기\s*모드|야간\s*모드)\s*(?P<page_ko>켜|꺼|설정)", r"설정.*(열어|변경|바꿔|가)"]),
        ("empty_recycle_bin", [r"\b(empty|clear|clean)\b\s+(out\s+)?(the\s+|my\s+)?(recycle bin|trash|bin)\b",
                               r"휴지통\s*(을|를)?\s*(비워|비우기|정리)"]),
        # ---------------- folders (v1.5.0) ----------------
        ("make_folder", [
            r"^(?!.*\b(and|then)\s+(then\s+)?(write|type|put|save|create|make|add|copy|move|open)\b)"
            r".*\b(make|create|add)\b\s+(a\s+)?(new\s+)?(folder|directory)\b(\s+(on|in|inside|under)\s+(my\s+|the\s+)?(?P<where>desktop|documents|downloads|pictures|music|videos|home( folder)?))?(\s+(called|named))?\s+(?P<name>[\w .,'()-]+?)(\s+(on|in)\s+(my\s+|the\s+)?(?P<where2>desktop|documents|downloads|pictures|music|videos))?$",
            r"((?P<where_ko>바탕\s*화면|문서|다운로드|사진|음악|동영상)\s*(에|에다가?)\s*)?(?P<name>[\w .-]+?)\s*(이라는|라는)?\s*(새\s*)?폴더\s*(를|을)?\s*(만들어|생성)",
        ]),
        ("open_folder", [
            r"^(open|go to|bring up)\s+(me\s+)?(my\s+|the\s+)?(?P<folder>downloads?|documents?|desktop|pictures|photos|music|videos|home|recycle bin|trash|this pc|my computer)(\s+folder)?$",
            r"^show\s+(me\s+)?(my\s+|the\s+)?(?P<folder>downloads?|documents?|desktop|pictures|photos|music|videos|home)\s+folder$",
            r"^show\s+(me\s+)?(my\s+|the\s+)?(?P<folder>downloads|documents|pictures|photos|recycle bin|trash)$",
            r"^(open|show)\s+(the\s+)?(folder|directory)\s+(?P<path>.+)$",
            r"(?P<folder_ko>다운로드|문서|바탕\s*화면|사진|음악|동영상|휴지통)\s*(폴더)?\s*(을|를)?\s*(열어|보여)",
        ]),
        ("open_app", [r"^(open|launch|start|show)\s+(the\s+|my\s+)?(?P<app>file explorer|windows explorer|explorer|file manager)$",
                      r"(?P<app_ko>파일\s*탐색기|탐색기)\s*(을|를)?\s*(열어|실행|켜)"]),
        # ---------------- files (requirement 20) ----------------
        ("file_search", [r"\b(find|search|locate|look for)\b\s+(?P<query>.+?)\s+(file|files|document)s?$",
                         r"\b(find|search)\b.*\bfiles?\b.*\b(named|called|about)\b\s+(?P<query>.+)$",
                         r"(?P<query>.+?)\s*(파일|문서).*(찾아|검색)\s*(줘|)?$"]),
        ("file_read", [r"\b(read|open|show)\b\s+(the\s+)?file\s+(called\s+|named\s+)?(?P<path>\S*[./\\]\S+|\S+\.\w{1,5})$",
                       r"파일\s+(?P<path>.+?)\s*(읽어|열어|보여)\s*(줘|)?$"]),
        ("file_write", [r"^(?!.*\b(and|then)\s+(then\s+)?(open|type|send|copy|move|email)\b).*\b(write|create|save|make)\b\s+(a\s+)?(new\s+)?(text\s+)?file\s+(called\s+|named\s+)?(?P<path>[^\s,]+)(\s+(with|containing|that says|saying)\s+(?P<content>.+))?$",
                        r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(만들어|생성|저장)\s*(줘|)?$"]),
        ("file_delete", [r"\b(delete|remove|trash)\b\s+(the\s+)?file\s+(called\s+|named\s+)?(?P<path>\S+)",
                         r"파일\s+(?P<path>\S+)\s*(을|를)?\s*(삭제|지워)\s*(줘|)?$"]),
        # ---------------- browser / web (requirement 20) ----------------
        ("open_url", [r"\b(open|go to|visit|browse to)\b\s+(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)$",
                      r"(?P<url>(https?://)?[\w.-]+\.\w{2,}(/\S*)?)\s*(열어|접속|가)\s*(줘|)?$"]),
        ("web_search", [r"\b(search|google|look up)\b\s+(for\s+)?(?P<query>.+)$",
                        r"(?P<query>.+?)\s*(검색|찾아)\s*(해|줘|봐)\s*$"]),
        # ---------------- windows, keyboard, screen (requirement 20) ----------------
        ("press_keys", [r"^(?P<k_copy>copy)(\s+(that|this|it|the selection))?$", r"^(?P<k_paste>paste)(\s+(it|that|this))?$",
                        r"^(?P<k_cut>cut)(\s+(it|that|this))?$", r"^(?P<k_undo>undo)(\s+(that|it|this))?$",
                        r"^(?P<k_redo>redo)(\s+(that|it))?$", r"^(?P<k_selectall>select all)$",
                        r"^(?P<k_save>save)(\s+(it|this|that|the file|my work|the document|changes))?$",
                        r"^(?P<k_newtab>(open\s+)?(a\s+)?new tab)$", r"^(?P<k_closetab>close (this|the|that) tab)$",
                        r"^(?P<k_refresh>refresh|reload)(\s+(the|this)\s+page)?$",
                        r"^(?P<k_copy_ko>복사)\s*(해|해\s*줘)?$", r"^(?P<k_paste_ko>붙여\s*넣기|붙여\s*넣어)\s*(줘)?$",
                        r"^(?P<k_undo_ko>실행\s*취소)\s*(해|해\s*줘)?$", r"^(?P<k_save_ko>저장)\s*(해|해\s*줘)?$",
                        r"^(?P<k_selectall_ko>전체\s*선택|모두\s*선택)\s*(해|해\s*줘)?$"]),
        ("screenshot", [r"\b(take|capture|grab)\b.*\b(screenshot|screen ?shot|screen)\b", r"\bscreenshot\b",
                        r"(스크린\s*샷|화면\s*(캡처|캡쳐))"]),
        ("type_text", [r"^(type|write out)\b\s+(?P<text>.+)$",
                       r"^(jot|write|put|type)\s+(down\s+)?['\"“]?(?P<text>.+?)['\"”]?\s+(in|into|on)\s+(the|this|that|my)\s+(open|current|active|focused)?\s*(window|app|document|editor|notepad)$",
                       r"(?P<text>.+?)\s*(라고|이라고)\s*(입력|타이핑)\s*(해|해줘|해\s*줘)?$"]),
        ("press_keys", [r"^press\b\s+(?P<keys>[\w+ -]+)$", r"(?P<keys>[A-Za-z0-9+ -]+?)\s*(키를?|단축키를?)\s*(눌러|입력)"]),
        ("show_desktop", [r"\b(show|go to)\s+(the\s+)?desktop\b", r"(minimi[sz]e)\s+(all|everything)\b",
                          r"바탕\s*화면\s*(보여|으로\s*가)"]),
        ("window_minimize", [r"\bminimi[sz]e\b\s*(the\s+)?(?P<title>.*?)(\s+window)?$", r"(?P<title>.*?)\s*창?\s*최소화"]),
        ("window_maximize", [r"\bmaximi[sz]e\b\s*(the\s+)?(?P<title>.*?)(\s+window)?$", r"(?P<title>.*?)\s*창?\s*최대화"]),
        ("window_focus", [r"\b(switch to|focus( on)?|bring up)\b\s+(the\s+)?(?P<title>[\w .+-]+?)(\s+window)?$",
                          r"(?P<title>[\w .+-]+?)\s*(창|윈도우)\s*(으로|로)?\s*(전환|이동)"]),
        # ---------------- apps (requirement 20) ----------------
        # "open Word and write a letter" is several steps: the language model plans those
        ("open_app", [r"^(?!.*\band\s+(then\s+)?(write|type|search|play|tell|send|make|create|find|go|show|read|put|draft|start|open|close)\b)"
                      r"\b(open|launch|start|run|fire up|bring up|load)\b\s+(up\s+)?(?P<app>[\w .+'&-]+?)(?:\s+(?:app|application|program|game))?$",
                      r"(?P<app>[\w .+-]+?)\s*(앱|프로그램|어플|게임)?\s*(을|를)?\s*(열어|실행|켜)\s*(줘|주라|해|해\s*줘|줄래|주세요|봐)?$"]),
        ("close_app", [r"\b(close|quit|kill|exit|shut)\b\s+(down\s+)?(?P<app>[\w .+'&-]+?)(?:\s+(?:app|application|program|game))?$",
                       r"(?P<app>[\w .+-]+?)\s*(앱|프로그램|어플|게임)?\s*(을|를)?\s*(닫아|종료|꺼)\s*(줘|해|해\s*줘|줄래|주세요)?$"]),
        # ---------------- assistant control ----------------
        ("sleep_assistant", [r"\b(go to sleep|stand down|that'?s all|sleep now|dismiss)\b",
                             r"(그만\s*쉬어|쉬어|자러\s*가|대기\s*모드)"]),
    ]


_COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in pats]) for name, pats in _rules()]
_STOPWORDS_APP = {"the", "my", "me", "please", "now", "a", "an", "up", "로", "을", "를", "좀"}
_NO_ARG_INTENTS = {"mute_mic", "unmute_mic", "voice_status", "mic_settings", "llm_status", "llm_install",
                   "time_now", "lock", "power", "open_folder", "empty_recycle_bin", "list_timers", "cancel_timer",
                   "shell_permission",
                   "stop_music", "open_space3d", "close_space3d",
                   "clone_object", "system_info", "sleep_assistant", "delete_object",
                   "save_project", "export_model", "neural_view", "wallpaper_on", "wallpaper_off",
                   "open_jarvis_settings", "screenshot", "show_desktop", "toggle_gesture",
                   "list_devices", "settings", "window_minimize", "window_maximize",
                   "self_describe", "self_forget"}


def _clean_app(name: str) -> str:
    name = name.strip().strip(".").strip()
    parts = [w for w in name.split() if w.lower() not in _STOPWORDS_APP]
    return " ".join(parts) if parts else name


# ------------------------------------------------------------------ normalisation (v1.5.0)
# Spoken commands arrive from speech recognition with punctuation and polite wrappers
# ("Can you open Notepad?", "Jarvis, could you please turn it up for me."): the rules are
# matched against the bare command first, then against the text as it was said.
_PUNCT_TAIL = re.compile(r"[\s.!?。！？…,;:~]+$")
_QUOTES = "\"'“”‘’`"
_WAKE_LEAD = re.compile(r"^(?:(?:hey|ok(?:ay)?|hi|yo|so)[\s,]+)?(?:jarvis|자비스(?:야|님)?)\s*[,!.:]?\s*", re.I)
_POLITE_LEAD = re.compile(
    r"^(?:(?:please|kindly|just|quickly|now|then|also|and|ok(?:ay)?|alright|so)[\s,]+|go ahead and\s+|"
    r"(?:can|could|would|will)\s+(?:you|u)\s+(?:please\s+|kindly\s+|just\s+|quickly\s+)?|"
    r"i\s+(?:want|need|would like|'d like|wanna|wish)\s+(?:you\s+)?to\s+|i\s+need\s+you\s+to\s+|"
    r"(?:let'?s|lets)\s+(?=open|close|play|start|launch|search|go|turn|set|take|make|create|find)|"
    r"(?:try to|try and)\s+)", re.I)
_POLITE_TAIL = re.compile(
    r"(?:[\s,]+(?:please|for me|right now|now|thanks|thank you|jarvis|if you can|if you could|would you|"
    r"will you|could you|can you|asap|real quick|quickly|sir|okay|ok|for me please|pls))+$", re.I)
_KO_LEAD = re.compile(r"^(?:자비스(?:야|님)?|저기|혹시)\s*[,!.]?\s*")
_KO_TAIL = [
    (re.compile(r"\s*(줄래요?|주시겠어요|주실래요|주시겠습니까|줄\s*수\s*있(어|나|니|을까|겠어)요?|주겠니|주라|주렴)$"), " 줘"),
    (re.compile(r"\s*(해\s*주세요|해\s*줄래요?|해\s*주시겠어요|해\s*줄\s*수\s*있어요?)$"), " 해 줘"),
    (re.compile(r"\s*(주세요|줘요)$"), " 줘"),
]


def normalize_command(text: str) -> str:
    """The bare command, original casing kept: no wake word, polite wrapper or end punctuation."""
    t = (text or "").strip().strip(_QUOTES).strip()
    t = _PUNCT_TAIL.sub("", t)
    prev = None
    while prev != t:
        prev = t
        t = _WAKE_LEAD.sub("", t).strip()
        t = _KO_LEAD.sub("", t).strip()
        t = _POLITE_LEAD.sub("", t).strip()
        t = _POLITE_TAIL.sub("", t).strip()
        t = _PUNCT_TAIL.sub("", t).strip().strip(_QUOTES).strip()
    if _HANGUL.search(t):
        for pat, rep in _KO_TAIL:
            t = pat.sub(rep, t).strip()
    return t or (text or "").strip()


_KEYS = {"copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x", "undo": "ctrl+z", "redo": "ctrl+y",
         "selectall": "ctrl+a", "save": "ctrl+s", "newtab": "ctrl+t", "closetab": "ctrl+w", "refresh": "f5"}
_FOLDERS_KO = {"다운로드": "downloads", "문서": "documents", "바탕화면": "desktop", "바탕 화면": "desktop", "사진": "pictures",
               "음악": "music", "동영상": "videos", "휴지통": "recycle bin"}
_THIS_WINDOW = {"this", "this window", "the window", "current window", "the current window", "active window",
                "the active window", "it", "this one", "that", "that window"}


def _postprocess(name: str, args: Dict[str, str], tl: str, t: str, pat) -> Optional[Dict[str, str]]:
    """Turn a rule's capture groups into the skill's arguments (None: the rule does not apply)."""
    if "app" in args or "app_ko" in args:
        app = args.pop("app_ko", None) or args.get("app", "")
        # "open paint for me old bean" -> paint: the name ends where the filler starts
        app = re.split(r"\s+(?:for\s+(?:me|us|now|a (?:bit|moment|sec(?:ond)?))|please|now|right now|quickly|asap|"
                       r"thanks|thank you|sir|old (?:bean|chap|boy|friend)|mate|buddy|real quick)\b", app,
                       maxsplit=1, flags=re.IGNORECASE)[0]
        args["app"] = _clean_app(app)
        if name == "open_app" and args["app"] in ("파일 탐색기", "탐색기", "file explorer", "windows explorer", "file manager"):
            args["app"] = "explorer"
        if not args["app"] or args["app"] in ("it", "this", "that", "them", "everything", "all"):
            return None if name in ("open_app", "close_app") else args
    if "query" in args:
        q = args["query"].strip(" ?.")
        q = re.sub(r"^(for|about|the)\s+", "", q, flags=re.IGNORECASE)
        q = re.sub(r"^(the\s+)?(web|internet|net|online|google|youtube)\s+(for|about)\s+", "", q, flags=re.IGNORECASE)
        q = re.sub(r"\s+(on|in)\s+(the\s+)?(web|internet|google)$", "", q, flags=re.IGNORECASE)
        q = re.sub(r"^(my|the|a|an|some)\s+(?=\S)", "", q.strip(), flags=re.IGNORECASE) if name == "file_search" else q
        args["query"] = q.strip()
        if not args["query"]:
            return None
    if "shape" in args:
        shape = SHAPES.get(args["shape"].lower())
        if shape:
            args["shape"] = shape
        else:
            args.pop("shape")
    if name in ("type_text", "self_remember", "run_shell", "run_shell_elevated", "file_write", "file_read",
                "file_delete", "make_folder", "open_folder", "set_timer"):
        mt = pat.search(t)                      # keep the user's own casing
        if mt:
            for key, val in (mt.groupdict() or {}).items():
                if val and val.strip() and key in args:
                    args[key] = val.strip()
    if name == "self_describe":
        if re.search(r"learn|know|remember|배웠|학습|알고|기억", tl):
            args["about"] = "learned"
        elif re.search(r"purpose|goal|directive|mission|loyal|serve|목적|목표|사명|지침", tl):
            args["about"] = "purpose"
        else:
            args["about"] = "self"
    if name == "shell_permission":
        admin = bool(re.search(r"\badmin(istrator)?\b|\belevated\b|관리자", tl))
        ko_level = args.pop("level_ko", None)
        if args.pop("level_deny", None) or (ko_level and re.search(r"차단|막아|금지", ko_level)):
            level = "deny"
        elif args.pop("level_ask", None) or (ko_level and "물어" in ko_level):
            level = "ask"
        else:
            level = "allow"
        return {"level": level, "scope": "admin" if admin else "normal"}
    if name == "llm_status":
        restart = args.pop("restart", None) or args.pop("restart_ko", None)
        return {"restart": "yes"} if restart else {}
    if name == "toggle_gesture":
        args["state"] = "off" if re.search(r"\boff\b|꺼|끄기|비활성", tl) else "on"
    if name == "power":
        keys = set(args)
        action = ("restart" if keys & {"restart", "restart2", "restart_ko"} else
                  "sleep" if keys & {"sleep", "sleep_ko"} or "to sleep" in tl else "shutdown")
        return {"action": action}
    if name in ("set_volume", "set_brightness"):
        if args.get("level"):
            return {"level": args["level"]}
        keys = " ".join(args)
        for word in ("unmute", "mute", "up", "down"):
            if f"level_{word}" in keys:
                return {"level": word}
        return None
    if name == "press_keys" and not args.get("keys"):
        for key in args:
            if key.startswith("k_"):
                return {"keys": _KEYS[key[2:].replace("_ko", "")]}
        return None
    if name == "media_control":
        a = (args.get("action") or args.get("action_ko") or "").lower()
        a = {"skip": "next", "last": "previous", "back": "previous", "continue": "resume", "다음": "next",
             "이전": "previous"}.get(a, a)
        return {"action": a} if a else None
    if name == "make_folder":
        where = args.get("where") or args.get("where2") or _FOLDERS_KO.get(re.sub(r"\s+", "", args.get("where_ko", "")), "")
        nm = (args.get("name") or "").strip(" .'\"")
        if not nm:
            return None
        return {"name": nm, "where": where.replace(" folder", "") if where else ""}
    if name == "open_folder":
        if args.get("path"):
            return {"folder": args["path"]}
        f = args.get("folder") or _FOLDERS_KO.get(re.sub(r"\s+", "", args.get("folder_ko", "")), "")
        f = {"download": "downloads", "document": "documents", "photos": "pictures", "trash": "recycle bin",
             "my computer": "this pc"}.get(f, f)
        return {"folder": f} if f else None
    if name == "settings":
        page = (args.get("page") or args.get("page2") or "").lower()
        ko = args.get("page_ko")
        if ko or re.search(r"블루투스|와이파이|비행기|야간", tl):
            page = ("bluetooth" if "블루투스" in tl else "wifi" if "와이파이" in tl else
                    "airplane mode" if "비행기" in tl else "night light")
        pages = {"bluetooth": "bluetooth", "wifi": "network-wifi", "wi-fi": "network-wifi", "wireless": "network-wifi",
                 "airplane mode": "network-airplanemode", "flight mode": "network-airplanemode",
                 "night light": "nightlight", "hotspot": "network-mobilehotspot", "mobile hotspot": "network-mobilehotspot",
                 "dark mode": "colors", "light mode": "colors", "focus assist": "quiethours", "do not disturb": "quiethours",
                 "display": "display", "sound": "sound", "network": "network", "update": "windowsupdate",
                 "windows update": "windowsupdate", "battery": "batterysaver", "power": "powersleep",
                 "privacy": "privacy", "apps": "appsfeatures", "personalization": "personalization",
                 "personalisation": "personalization", "wallpaper": "personalization-background",
                 "background": "personalization-background", "mouse": "mousetouchpad", "keyboard": "typing",
                 "language": "regionlanguage", "time": "dateandtime", "date": "dateandtime"}
        return {"page": pages.get(page, "")} if page else {}
    if name == "set_timer":
        out = {k: v for k, v in args.items() if k in ("duration", "at", "what") and v}
        return out or None
    if name == "cancel_timer":
        return {"what": "all"} if re.search(r"\ball\b|모두|전부", tl) else {}
    if name == "run_shell" and args.get("command"):
        first, _, rest = args["command"].partition(" ")
        if first.lower() in ("ping", "tracert", "nslookup", "ipconfig", "netstat", "systeminfo", "tasklist", "whoami",
                             "hostname", "getmac", "pathping"):
            args["command"] = (first.lower() + (" " + rest if rest else "")).strip()
    if name in ("window_minimize", "window_maximize", "window_focus") and "title" in args:
        title = args["title"].strip(" .")
        args["title"] = "" if title.lower() in _THIS_WINDOW else title
        if name == "window_focus" and not args["title"]:
            return None
    if name == "file_write" and args.get("path"):
        args["path"] = args["path"].strip(" .,'\"")
    if name in ("file_read", "file_delete") and args.get("path"):
        args["path"] = args["path"].rstrip(".,")
    return args


def _match(tl: str, t: str, lang: str) -> Optional[Intent]:
    for name, patterns in _COMPILED:
        for pat in patterns:
            m = pat.search(tl)
            if not m:
                continue
            args = {k: v.strip() for k, v in (m.groupdict() or {}).items() if v and v.strip()}
            args = _postprocess(name, args, tl, t, pat)
            if args is None:
                continue
            conf = 0.8 if args or name in _NO_ARG_INTENTS else 0.6
            return Intent(name=name, args=args, confidence=conf, lang=lang)
    return None


def parse_intent(text: str, lang: Optional[str] = None) -> Intent:
    t = (text or "").strip()
    lang = lang or detect_language(t)
    bare = normalize_command(t)
    said = _PUNCT_TAIL.sub("", t.strip(_QUOTES)).strip()
    for cand in dict.fromkeys([bare, said, t]):
        if not cand:
            continue
        intent = _match(cand.lower(), cand, lang)
        if intent is not None:
            return intent
    return Intent(name="chat", args={"text": t}, confidence=0.3, lang=lang)


# While the 3D space fills the screen, a bare "close it" / "exit" / "go back" means: close it.
_BARE_CLOSE = re.compile(
    r"^(please\s+|ok(ay)?[, ]+|jarvis[, ]+)*(close|exit|leave|quit|hide|dismiss|shut|minimi[sz]e|end)\b"
    r"(\s+(it|this|that|here|the space|this space|the view|this view|out|now|down|please|for me|everything|"
    r"the hologram|this mode|the mode))*\s*[.!]*$"
    r"|^(please\s+|ok(ay)?[, ]+|jarvis[, ]+)*(go|get|take me|bring me)\s+(back|out)\b[\w\s,']*[.!]*$"
    r"|^(back|return)\s+to\s+(the\s+)?(desktop|normal|hud|2d|my screen)\b[\w\s']*[.!]*$"
    r"|^(i'?m|i am|we'?re|we are)\s+(done|finished)(\s+(here|with (it|this|the 3d( space)?)))?\s*[.!]*$"
    r"|^(that'?s\s+)?enough(\s+(3d|for now))?\s*[.!]*$",
    re.IGNORECASE)
_BARE_CLOSE_KO = re.compile(
    r"^(자비스[, ]*)?(이거|그거|이것|그것|이\s*화면|화면|여기서?)?\s*(좀\s*)?"
    r"(닫아|닫기|닫자|나가|나가기|나가자|종료|끝내|끝|그만|꺼|돌아가)\s*"
    r"(줘|주세요|줄래|요|해|해\s*줘|해\s*주세요|하자|기|자)?\s*[.!]*$")


def contextual_intent(intent: Intent, text: str, context: Optional[dict] = None) -> Intent:
    """Adjust an intent for what is on screen: with the 3D space open, a bare "close it",
    "exit" or "go back" closes the 3D space (instead of looking for an app called "it")."""
    ctx = context or {}
    t = (text or "").strip()
    if ctx.get("space3d") and intent.name != "close_space3d":
        if _BARE_CLOSE.match(t) or _BARE_CLOSE_KO.match(t):
            return Intent(name="close_space3d", args={}, confidence=0.85, lang=intent.lang)
    return intent


def _tag_args(attrs: str, body: str) -> Dict[str, str]:
    args: Dict[str, str] = {}
    for k, v1, v2, v3 in re.findall(r'([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>/]+))', attrs or ""):
        if k.lower() != "name":
            args[k] = v1 or v2 or v3
    body = (body or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```\w*\s*|\s*```$", "", body).strip()
    if body:
        obj = None
        if body.startswith("{"):
            try:
                obj = json.loads(body)
            except ValueError:
                try:
                    import ast
                    obj = ast.literal_eval(re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False",
                                                                         re.sub(r"\bnull\b", "None", body))))
                except Exception:
                    obj = None
        if isinstance(obj, dict):
            args.update({str(k): (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                         for k, v in obj.items()})
        elif not body.startswith("{"):
            args["_text"] = body                      # <action name="open_app">Notepad</action>
    return args


def parse_action_tags(text: str) -> List[Intent]:
    """<action name="TOOL">{json}</action> -- and the variations models actually write:
    single quotes, unquoted names, self-closing tags with attributes, plain-text arguments."""
    out: List[Intent] = []
    for m in _ACTION_TAG.finditer(text or ""):
        attrs, body = m.group(1) or "", m.group(3) or ""
        nm = re.search(r'\bname\s*=\s*["\']?([A-Za-z_][\w-]*)', attrs)
        if not nm:
            continue
        out.append(Intent(name=nm.group(1).lower().replace("-", "_"), args=_tag_args(attrs, body), confidence=0.9))
    return out


def strip_action_tags(text: str) -> str:
    return _ACTION_TAG.sub("", text or "").strip()


# ------------------------------------------------------------------ yes / no (v1.5.0)
# While JARVIS waits for permission ("Shall I close Spotify, sir?"), a spoken or typed answer
# answers it -- it no longer arrives as a new command that cancels the waiting action.
_YES = re.compile(
    r"^(yes|yeah|yea|yep|yup|ya|sure|ok|okay|alright|all right|go ahead|go for it|do it|proceed|confirm(ed)?|"
    r"allow(ed)?( it)?|approve(d)?|affirmative|please do|of course|certainly|absolutely|fine|grant(ed)?|"
    r"you may|correct|that'?s right|right|indeed|very well|make it so|carry on|sounds good|let'?s do it|positive)\b",
    re.IGNORECASE)
_NO = re.compile(
    r"^(no|nope|nah|nay|don'?t|do not|cancel|stop|deny|denied|never ?mind|negative|hold off|not now|wait|abort|"
    r"forget it|leave it|no thanks|no thank you|not allowed|refuse|reject(ed)?|skip it|better not)\b",
    re.IGNORECASE)
_YES_KO = re.compile(r"^(네|예|응|어|그래|좋아|좋습니다|허용|허락|해|해\s*줘|해\s*주세요|진행|확인|ㅇㅇ|괜찮아|그렇게\s*해|맞아|오케이|알겠어)")
_NO_KO = re.compile(r"^(아니|아뇨|아니요|안\s*돼|안돼|하지\s*마|취소|거부|싫어|그만|멈춰|ㄴㄴ|됐어|하지\s*말아)")


def answer_yes_no(text: str) -> Optional[bool]:
    """True / False when the utterance is a short yes or no, otherwise None."""
    t = normalize_command(text or "").strip()
    t = re.sub(r"^(jarvis|자비스)[,\s]*", "", t, flags=re.IGNORECASE)
    if not t or len(t.split()) > 5:
        return None
    if _NO.match(t) or _NO_KO.match(t):
        return False
    if _YES.match(t) or _YES_KO.match(t):
        return True
    return None
