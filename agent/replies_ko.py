"""
agent/replies_ko.py — Korean phrasing for skill replies (requirement 2: the output language
follows the input language). Skills report structured data; when the user spoke Korean,
the orchestrator builds the spoken reply from these templates instead of the English text.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Union


def _d(r: dict, key: str, default: Any = "") -> Any:
    v = (r.get("data") or {}).get(key)
    return default if v in (None, "") else v


def _sysinfo(r: dict) -> str:
    s = (f"CPU {_d(r, 'cpu_percent', 0):.0f}퍼센트, 메모리 {_d(r, 'ram_percent', 0):.0f}퍼센트를 "
         f"사용 중이고, 디스크 여유 공간은 {_d(r, 'disk_free_gb', 0):.0f}기가바이트입니다, sir.")
    if _d(r, "battery_percent", None) is not None:
        s += f" 배터리는 {_d(r, 'battery_percent')}퍼센트입니다."
    return s


Reply = Union[str, Callable[[dict], str]]

OK: Dict[str, Reply] = {
    "open_space3d": "3D 프로젝트 공간을 띄우겠습니다, sir.",
    "close_space3d": "3D 공간을 닫겠습니다, sir.",
    "clone_object": "선택한 오브젝트를 복제하겠습니다, sir.",
    "add_object": "새 오브젝트를 추가하겠습니다, sir.",
    "delete_object": "선택한 오브젝트를 지우겠습니다, sir.",
    "save_project": "프로젝트를 저장하겠습니다, sir.",
    "export_model": "모델을 내보내겠습니다, sir.",
    "toggle_gesture": lambda r: ("손동작 감지를 끄겠습니다, sir." if _d(r, "state") == "off"
                                 else "손동작 감지를 전환하겠습니다, sir."),
    "neural_view": "제 신경망을 보여드리겠습니다, sir.",
    "wallpaper_on": "3D 공간을 바탕화면으로 설정하겠습니다, sir.",
    "wallpaper_off": "원래 바탕화면으로 되돌리겠습니다, sir.",
    "open_jarvis_settings": "제 설정 화면입니다, sir.",
    "sleep_assistant": "알겠습니다, sir. 대기하겠습니다.",
    "play_music": lambda r: (f"{_d(r, 'title')}을(를) 재생합니다, sir." if _d(r, "mode") == "stream"
                             else f"{_d(r, 'query')} 플레이어를 열겠습니다, sir."),
    "stop_music": "음악을 멈췄습니다, sir.",
    "now_playing": lambda r: (f"지금 {_d(r, 'title')}이(가) 재생 중입니다, sir." if _d(r, "title")
                              else "지금은 재생 중인 곡이 없습니다, sir."),
    "media_control": "처리했습니다, sir.",
    "open_app": lambda r: f"{_d(r, 'app', '앱')}을(를) 열겠습니다, sir.",
    "close_app": lambda r: f"{_d(r, 'app', '앱')}을(를) 닫았습니다, sir.",
    "window_focus": lambda r: f"{_d(r, 'window')} 창으로 전환했습니다, sir.",
    "window_minimize": lambda r: f"{_d(r, 'window')} 창을 최소화했습니다, sir.",
    "window_maximize": lambda r: f"{_d(r, 'window')} 창을 최대화했습니다, sir.",
    "show_desktop": "바탕화면입니다, sir.",
    "list_windows": lambda r: f"열려 있는 창이 {len(_d(r, 'windows', []))}개 있습니다, sir.",
    "type_text": "입력했습니다, sir.",
    "press_keys": lambda r: f"{'+'.join(_d(r, 'keys', []))} 키를 눌렀습니다, sir.",
    "screenshot": "스크린샷을 사진 폴더의 JARVIS 폴더에 저장했습니다, sir.",
    "mute_mic": "마이크를 음소거했습니다, sir.",
    "unmute_mic": "마이크 음소거를 해제했습니다, sir.",
    "set_volume": lambda r: f"볼륨을 {_d(r, 'level')}퍼센트로 맞췄습니다, sir.",
    "system_info": _sysinfo,
    "settings": "Windows 설정을 열겠습니다, sir.",
    "list_devices": lambda r: (f"드라이브 {len(_d(r, 'disks', []))}개, 네트워크 인터페이스 "
                               f"{len(_d(r, 'network', []))}개가 보입니다, sir."),
    "file_search": lambda r: f"'{_d(r, 'query')}'와(과) 일치하는 파일을 {_d(r, 'count', 0)}개 찾았습니다, sir.",
    "file_read": "파일 내용을 가져왔습니다, sir.",
    "file_write": "파일을 저장했습니다, sir.",
    "file_delete": "파일을 삭제했습니다, sir.",
    "open_url": lambda r: f"{_d(r, 'url')}을(를) 열겠습니다, sir.",
    "web_search": lambda r: f"'{_d(r, 'query')}'을(를) 웹에서 검색하겠습니다, sir.",
    "read_page": "페이지를 읽었습니다, sir.",
}

FAIL_DEFAULT = "죄송합니다, sir. 그 작업을 완료하지 못했습니다."
FAIL: Dict[str, Reply] = {
    "open_app": "이 PC에서 그 앱을 찾지 못했습니다, sir.",
    "close_app": "그 앱은 실행 중이 아닌 것 같습니다, sir.",
    "play_music": "음악을 재생하지 못했습니다, sir.",
    "window_focus": "그런 이름의 창이 보이지 않습니다, sir.",
    "file_search": "검색할 내용을 말씀해 주세요, sir.",
    "set_volume": "0에서 100 사이의 숫자로 볼륨을 말씀해 주세요, sir.",
}


def localize(intent: str, result: dict, lang: str) -> str:
    speak = result.get("speak", "") or ""
    if lang != "ko":
        return speak
    template = OK.get(intent) if result.get("ok") else FAIL.get(intent, FAIL_DEFAULT)
    if template is None:
        return speak
    try:
        return template(result) if callable(template) else template
    except Exception:
        return speak if result.get("ok") else FAIL_DEFAULT
