"""
Application control (requirement 20: run any installed app).

Launching resolves the spoken name in this order:
  1. built-in Windows tools and settings URIs (always present: notepad, calc, settings...),
  2. an index of every Start Menu shortcut (all users + current user) and every Store/UWP
     app reported by `Get-StartApps`, matched fuzzily (exact > prefix > substring > close
     spelling), so "open spotify", "open VS code" or "메모장 열어줘" find the real app,
  3. otherwise a polite "couldn't find it" (no error pop-ups).
Closing terminates matching processes gracefully (psutil), falling back to a kill only if
they don't exit.

SPEED (v1.4): the index (Start Menu walk + `Get-StartApps`, 1-3 s) is built in the
background at start-up, and a stale index is used at once while a fresh one is built behind
it, so "open ..." never waits for PowerShell.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import threading
import re
import time
from typing import List, Optional, Tuple

from .base import ok, fail, powershell, IS_WINDOWS, IS_MAC

# Spoken name -> built-in executable / URI that exists on every Windows install.
SYSTEM_TOOLS = {
    "notepad": "notepad", "메모장": "notepad",
    "calculator": "calc", "calc": "calc", "계산기": "calc",
    "paint": "mspaint", "그림판": "mspaint",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "탐색기": "explorer", "파일 탐색기": "explorer",
    "cmd": "cmd", "command prompt": "cmd", "명령 프롬프트": "cmd",
    "powershell": "powershell", "task manager": "taskmgr", "작업 관리자": "taskmgr",
    "control panel": "control", "제어판": "control",
    "settings": "ms-settings:", "설정": "ms-settings:",
    "camera": "microsoft.windows.camera:", "카메라": "microsoft.windows.camera:",
    "terminal": "wt", "windows terminal": "wt", "터미널": "wt",
    "노트패드": "notepad", "note pad": "notepad", "snipping tool": "snippingtool", "캡처 도구": "snippingtool",
    "registry editor": "regedit", "regedit": "regedit", "device manager": "devmgmt.msc", "장치 관리자": "devmgmt.msc",
    "disk management": "diskmgmt.msc", "services": "services.msc", "event viewer": "eventvwr.msc",
    "character map": "charmap", "magnifier": "magnify", "on-screen keyboard": "osk", "화상 키보드": "osk",
    "wordpad": "write", "sound settings": "ms-settings:sound", "display settings": "ms-settings:display",
    "this pc": "explorer", "my computer": "explorer", "내 pc": "explorer",
    "microsoft store": "ms-windows-store:", "store": "ms-windows-store:", "스토어": "ms-windows-store:",
    "clock": "ms-clock:", "alarms": "ms-clock:", "시계": "ms-clock:", "photos": "ms-photos:", "사진": "ms-photos:",
    "mail": "outlookmail:", "calendar": "outlookcal:", "weather": "bingweather:", "날씨": "bingweather:",
    "maps": "bingmaps:", "지도": "bingmaps:", "xbox": "xbox:", "edge": "microsoft-edge:",
}
# Spoken name -> website, when no installed app has that name ("open YouTube")
WEBSITES = {
    "youtube": "https://www.youtube.com", "유튜브": "https://www.youtube.com", "youtube music": "https://music.youtube.com",
    "google": "https://www.google.com", "구글": "https://www.google.com", "gmail": "https://mail.google.com",
    "지메일": "https://mail.google.com", "google drive": "https://drive.google.com", "google maps": "https://maps.google.com",
    "google translate": "https://translate.google.com", "naver": "https://www.naver.com", "네이버": "https://www.naver.com",
    "papago": "https://papago.naver.com", "파파고": "https://papago.naver.com", "daum": "https://www.daum.net",
    "facebook": "https://www.facebook.com", "페이스북": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "인스타그램": "https://www.instagram.com", "인스타": "https://www.instagram.com",
    "twitter": "https://x.com", "트위터": "https://x.com", "reddit": "https://www.reddit.com", "레딧": "https://www.reddit.com",
    "netflix": "https://www.netflix.com", "넷플릭스": "https://www.netflix.com", "amazon": "https://www.amazon.com",
    "github": "https://github.com", "깃허브": "https://github.com", "wikipedia": "https://www.wikipedia.org",
    "위키백과": "https://ko.wikipedia.org", "chatgpt": "https://chatgpt.com", "linkedin": "https://www.linkedin.com",
    "twitch": "https://www.twitch.tv", "트위치": "https://www.twitch.tv", "coupang": "https://www.coupang.com",
    "쿠팡": "https://www.coupang.com", "tiktok": "https://www.tiktok.com", "틱톡": "https://www.tiktok.com",
    "pinterest": "https://www.pinterest.com", "outlook web": "https://outlook.live.com",
}
_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+(/\S*)?$")
# Spoken name -> how the app appears in the Start Menu (fuzzy-matched afterwards).
SYNONYMS = {
    "chrome": "google chrome", "크롬": "google chrome",
    "edge": "microsoft edge", "엣지": "microsoft edge",
    "word": "word", "워드": "word", "excel": "excel", "엑셀": "excel",
    "powerpoint": "powerpoint", "파워포인트": "powerpoint", "outlook": "outlook",
    "vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
    "teams": "microsoft teams", "spotify": "spotify", "discord": "discord", "steam": "steam",
    "kakaotalk": "kakaotalk", "카카오톡": "카카오톡", "카톡": "카카오톡",
}
# Spoken name -> process image name, for closing.
PROCESS_NAMES = {
    "chrome": "chrome", "크롬": "chrome", "edge": "msedge", "엣지": "msedge",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
    "notepad": "notepad", "메모장": "notepad", "calculator": "calculatorapp", "계산기": "calculatorapp",
    "spotify": "spotify", "vscode": "code", "vs code": "code", "discord": "discord",
    "teams": "ms-teams", "카카오톡": "kakaotalk", "kakaotalk": "kakaotalk", "카톡": "kakaotalk",
    "노트패드": "notepad", "그림판": "mspaint", "paint": "mspaint", "task manager": "taskmgr", "작업 관리자": "taskmgr",
    "explorer": "explorer", "file explorer": "explorer", "탐색기": "explorer", "steam": "steam", "스팀": "steam",
    "워드": "winword", "엑셀": "excel", "파워포인트": "powerpnt", "zoom": "zoom", "줌": "zoom", "obs": "obs64",
    "vlc": "vlc", "photoshop": "photoshop", "포토샵": "photoshop", "edge browser": "msedge", "firefox": "firefox",
    "파이어폭스": "firefox", "whale": "whale", "웨일": "whale", "terminal": "windowsterminal",
}

_INDEX = {"t": 0.0, "items": []}   # items: (display name, kind 'lnk'|'aumid', target)
_INDEX_TTL = 3600


def _start_menu_dirs() -> List[str]:
    dirs = []
    for env, sub in (("ProgramData", r"Microsoft\Windows\Start Menu\Programs"),
                     ("APPDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base and os.path.isdir(os.path.join(base, sub)):
            dirs.append(os.path.join(base, sub))
    return dirs


_INDEX_LOCK = threading.Lock()
_REFRESHING = threading.Event()


def build_index(force: bool = False) -> List[Tuple[str, str, str]]:
    if not IS_WINDOWS:
        return []
    if not force and _INDEX["items"]:
        if time.time() - _INDEX["t"] >= _INDEX_TTL:
            refresh_index_async()            # answer now with the old index, refresh behind it
        return _INDEX["items"]
    with _INDEX_LOCK:                        # one build at a time; a waiting caller reuses it
        if not force and _INDEX["items"]:
            return _INDEX["items"]
        items = _scan_apps()
        _INDEX.update(t=time.time(), items=items)
        return items


def refresh_index_async() -> None:
    if not IS_WINDOWS or _REFRESHING.is_set():
        return
    _REFRESHING.set()

    def run():
        try:
            with _INDEX_LOCK:
                items = _scan_apps()
                _INDEX.update(t=time.time(), items=items)
        except Exception:
            pass
        finally:
            _REFRESHING.clear()

    threading.Thread(target=run, daemon=True, name="jarvis-app-index").start()


def _scan_apps() -> List[Tuple[str, str, str]]:
    items, seen = [], set()
    for d in _start_menu_dirs():
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                name = f[:-4]
                low = name.lower()
                if "uninstall" in low or "제거" in low or low in seen:
                    continue
                seen.add(low)
                items.append((name, "lnk", os.path.join(root, f)))
    try:  # Store / UWP apps (Calculator, Spotify from the Store, etc.)
        out = powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress", timeout=25)
        data = json.loads(out) if out else []
        if isinstance(data, dict):
            data = [data]
        for app in data:
            name, aumid = app.get("Name"), app.get("AppID")
            if name and aumid and name.lower() not in seen:
                seen.add(name.lower())
                items.append((name, "aumid", aumid))
    except Exception:
        pass
    return items


def best_match(query: str, items: List[Tuple[str, str, str]]) -> Optional[Tuple[str, str, str]]:
    q = (query or "").strip().lower()
    if not q or not items:
        return None
    exact = [it for it in items if it[0].lower() == q]
    if exact:
        return exact[0]
    prefix = [it for it in items if it[0].lower().startswith(q)]
    if prefix:
        return min(prefix, key=lambda it: len(it[0]))
    contains = [it for it in items if q in it[0].lower()]
    if contains:
        return min(contains, key=lambda it: len(it[0]))
    names = [it[0].lower() for it in items]
    close = difflib.get_close_matches(q, names, n=1, cutoff=0.72)
    return items[names.index(close[0])] if close else None


def launch_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application, sir?")
    key = name.lower()
    if IS_WINDOWS:
        try:
            tool = SYSTEM_TOOLS.get(key)
            if tool:
                if tool.endswith(":"):
                    os.startfile(tool)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([tool], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                     if tool in ("cmd", "powershell") else 0)
                return ok(f"Opening {name}, sir.", {"app": name, "target": tool})
            match = best_match(SYNONYMS.get(key, key), build_index())
            if match:
                disp, kind, target = match
                if kind == "lnk":
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
                return ok(f"Opening {disp}, sir.", {"app": disp, "kind": kind})
            site = _website(key)
            if site:
                return _open_site(name, site)
            return fail(f"I couldn't find an app called {name} on this PC, sir.",
                        error=f"no Start Menu or Store app matched '{name}'")
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))

    # non-Windows (development / testing)
    if IS_MAC:
        try:
            subprocess.run(["open", "-a", name], capture_output=True, timeout=10)
            return ok(f"Opening {name}, sir.", {"app": name})
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))
    exe = shutil.which(key) or shutil.which(key.replace(" ", "-"))
    if not exe:
        site = _website(key)
        if site:
            return _open_site(name, site)
        return fail(f"I can't find {name} on this system, sir.", error=f"executable not found: {name}")
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok(f"Opening {name}, sir.", {"app": name, "target": exe})
    except Exception as e:
        return fail(f"I couldn't open {name}, sir.", str(e))


def _website(key: str) -> Optional[str]:
    k = key.strip().lower()
    k = re.sub(r"\s+(website|site|web ?page|page|dot com)$", "", k)
    if k in WEBSITES:
        return WEBSITES[k]
    if _DOMAIN.match(k):
        return "https://" + k
    return None


def _open_site(name: str, url: str) -> dict:
    from .browser import open_url
    r = open_url(url)
    if r.get("ok"):
        r = dict(r)
        r["speak"] = f"Opening {name}, sir."
        r["data"] = dict(r.get("data") or {}, url=url, speak_ko=f"{name}을(를) 엽니다, sir.")
    return r


def close_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application should I close, sir?")
    try:
        import psutil  # type: ignore
    except Exception:
        return fail("Closing apps needs the psutil library, sir.", "psutil missing")
    want = PROCESS_NAMES.get(name.lower(), name.lower().replace(" ", ""))
    procs = []
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info.get("name") or "").lower()
        base = pname[:-4] if pname.endswith(".exe") else pname
        if base == want or (len(want) >= 4 and want in base):
            if p.pid != os.getpid():
                procs.append(p)
    if not procs:
        return fail(f"{name} doesn't appear to be running, sir.")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=4)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    return ok(f"Closed {name}, sir.", {"app": name, "processes": len(procs)})
