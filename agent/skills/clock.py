"""
Clock skills (v1.5.0): the time and date, timers and reminders.

  "what time is it" / "지금 몇 시야"                      -> time_now()
  "set a timer for 5 minutes" / "5분 타이머"              -> set_timer(duration="5 minutes")
  "remind me to call mom in 10 minutes"                   -> set_timer(duration=..., what="call mom")
  "remind me to stretch at 5 pm"                          -> set_timer(at="5 pm", what="stretch")
  "what timers do I have" / "cancel the timer"            -> list_timers() / cancel_timer()

Timers live in data/timers.json, so they survive a restart of JARVIS (one that came due while
JARVIS was not running is announced as soon as it is back). When a timer is due, JARVIS says it
out loud and shows it in the chat (the server registers how, with set_notifier()).
"""
from __future__ import annotations

import heapq
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from .base import ok, fail

_NOTIFY: Optional[Callable[[dict], None]] = None
_PATH: Optional[str] = None
_LOCK = threading.RLock()
_TIMERS: List[dict] = []
_WAKE = threading.Event()
_THREAD: Optional[threading.Thread] = None

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
        "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "couple": 2,
        "few": 3}
_UNIT = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "초": 1,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "분": 60,
         "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "시간": 3600}


# ------------------------------------------------------------------ parsing
def parse_duration(text: str) -> Optional[int]:
    """'5 minutes', '1 hour and 30 minutes', 'half an hour', '90 seconds', '1시간 30분' -> seconds."""
    t = (text or "").lower().strip()
    if not t:
        return None
    if re.search(r"half an? hour|30분", t) and not re.search(r"\d+\s*(hours?|시간)", t):
        if re.fullmatch(r"(a\s+)?half an? hour|30분", t):
            return 1800
    t = t.replace("half an hour", "30 minutes").replace("half a minute", "30 seconds")
    t = re.sub(r"(\d)\s*-\s*(minute|second|hour)", r"\1 \2", t)
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?|[a-z-]+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh]\b|초|분|시간)", t):
        n = float(num) if re.match(r"\d", num) else _NUM.get(num)
        if n is None:
            continue
        total += n * _UNIT.get(unit, _UNIT.get(unit.rstrip("s"), 60))
        found = True
    if not found:
        m = re.fullmatch(r"(\d+)", t)
        if m:
            return int(m.group(1)) * 60                       # "a 5 timer": minutes
        return None
    return int(round(total)) if total > 0 else None


def parse_clock(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """'5 pm', '5:30', '17:00', 'noon', '7 tomorrow', '오후 3시' -> the next such moment."""
    now = now or datetime.now()
    t = (text or "").lower().strip().replace(".", ":").replace("a:m:", "am").replace("p:m:", "pm")
    tomorrow = "tomorrow" in t or "내일" in t
    t = re.sub(r"\b(today|tonight|tomorrow)\b|오늘|내일", "", t).strip()
    if t in ("noon", "midday", "정오"):
        h, m, ampm = 12, 0, "pm"
    elif t in ("midnight", "자정"):
        h, m, ampm = 0, 0, "am"
    else:
        ko = re.search(r"(오전|오후)?\s*(\d{1,2})\s*시\s*((\d{1,2})\s*분|반)?", t)
        if ko:
            h = int(ko.group(2))
            m = 30 if ko.group(3) == "반" else int(ko.group(4) or 0)
            ampm = {"오전": "am", "오후": "pm"}.get(ko.group(1) or "", "")
        else:
            mm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
            if not mm:
                return None
            h, m, ampm = int(mm.group(1)), int(mm.group(2) or 0), mm.group(3) or ""
    if h > 23 or m > 59:
        return None
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    cand = now.replace(hour=h % 24, minute=m, second=0, microsecond=0)
    if tomorrow:
        return cand + timedelta(days=1)
    if cand <= now:
        if not ampm and h < 12 and (cand + timedelta(hours=12)) > now:
            return cand + timedelta(hours=12)                  # "at 5" said at 2 pm: 5 pm
        cand += timedelta(days=1)
    return cand


def _say_duration(sec: int, ko: bool = False) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if ko:
        parts = [f"{h}시간" if h else "", f"{m}분" if m else "", f"{s}초" if s and not h else ""]
        return " ".join(p for p in parts if p) or "0초"
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


def _clock(dt: datetime, ko: bool = False) -> str:
    if ko:
        return f"{'오전' if dt.hour < 12 else '오후'} {dt.hour % 12 or 12}시" + (f" {dt.minute}분" if dt.minute else "")
    return dt.strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------ storage + scheduler
def start(data_dir: str, notify: Optional[Callable[[dict], None]] = None) -> None:
    """Load saved timers and start the scheduler (the server calls this once)."""
    global _PATH, _THREAD, _NOTIFY
    _PATH = os.path.join(data_dir, "timers.json")
    if notify is not None:
        _NOTIFY = notify
    try:
        with open(_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = []
    with _LOCK:
        _TIMERS[:] = [t for t in saved if isinstance(t, dict) and t.get("due")]
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_loop, name="jarvis-timers", daemon=True)
        _THREAD.start()
    _WAKE.set()


def set_notifier(fn: Optional[Callable[[dict], None]]) -> None:
    global _NOTIFY
    _NOTIFY = fn


def _save() -> None:
    if not _PATH:
        return
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_TIMERS, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _PATH)
    except OSError:
        pass


def _loop() -> None:
    while True:
        with _LOCK:
            pending = sorted(_TIMERS, key=lambda t: t["due"])
            nxt = pending[0]["due"] if pending else None
        wait = 3600.0 if nxt is None else max(0.0, nxt - time.time())
        if _WAKE.wait(timeout=min(wait, 30.0)):
            _WAKE.clear()
        now = time.time()
        due: List[dict] = []
        with _LOCK:
            for t in list(_TIMERS):
                if t["due"] <= now + 0.05:
                    due.append(t)
                    _TIMERS.remove(t)
            if due:
                _save()
        for t in due:
            _fire(t, late=now - t["due"])


def _fire(t: dict, late: float = 0.0) -> None:
    what = t.get("what") or ""
    if t.get("kind") == "reminder" or what:
        en = f"Pardon the interruption, sir: a reminder to {what}." if what else "Your reminder is due, sir."
        ko = f"말씀하신 알림입니다, sir: {what}." if what else "알림 시간이 되었습니다, sir."
    else:
        label = _say_duration(t.get("seconds") or 0)
        en = f"Sir, your timer for {label} is up."
        ko = f"sir, {_say_duration(t.get('seconds') or 0, True)} 타이머가 끝났습니다."
    if late > 120:
        en += " (It came due while I was offline.)"
        ko += " (제가 꺼져 있는 동안 시간이 지났습니다.)"
    if _NOTIFY is not None:
        try:
            _NOTIFY({"id": t["id"], "en": en, "ko": ko, "lang": t.get("lang", "en"), "timer": t})
        except Exception:
            pass


# ------------------------------------------------------------------ skills
def time_now(**_) -> dict:
    now = datetime.now()
    day = now.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    en = f"It's {_clock(now)}, sir — {now.strftime('%A')}, the {day}{suffix} of {now.strftime('%B')}."
    week_ko = "월화수목금토일"[now.weekday()]
    ko = f"지금은 {_clock(now, True)}입니다, sir. {now.month}월 {now.day}일 {week_ko}요일입니다."
    return ok(en, {"speak_ko": ko, "iso": now.isoformat(timespec="minutes")})


def set_timer(duration: str = "", at: str = "", what: str = "", lang: str = "", **_) -> dict:
    what = re.sub(r"^(to|that|i (need|have) to)\s+", "", (what or "").strip(" .,!?"), flags=re.I)
    what = re.sub(r"\bmy\b", "your", what, flags=re.I)
    ko = lang == "ko" or bool(re.search(r"[가-힣]", f"{duration}{at}{what}"))
    now = time.time()
    if at:
        dt = parse_clock(at)
        if dt is None:
            return fail(f"I didn't catch the time '{at}', sir.", "unparsed time")
        due, seconds = dt.timestamp(), int(dt.timestamp() - now)
    else:
        seconds = parse_duration(duration)
        if not seconds:
            return fail("For how long, sir? Say, for example, 'a timer for 5 minutes'.",
                        f"unparsed duration {duration!r}")
        if seconds > 7 * 24 * 3600:
            return fail("That's rather long for a timer, sir: a week at most.", "too long")
        due = now + seconds
    t = {"id": uuid.uuid4().hex[:8], "due": round(due, 3), "seconds": seconds, "what": what,
         "kind": "reminder" if what or at else "timer", "created": round(now, 3), "lang": "ko" if ko else "en"}
    with _LOCK:
        _TIMERS.append(t)
        _save()
    _WAKE.set()
    when = datetime.fromtimestamp(due)
    if at:
        en = f"Very good, sir. I'll remind you to {what or 'do that'} at {_clock(when)}."
        ko_s = f"알겠습니다, sir. {_clock(when, True)}에 {what or '말씀하신 일'}을(를) 알려 드리겠습니다."
    elif what:
        en = f"Very good, sir. I'll remind you to {what} in {_say_duration(seconds)}."
        ko_s = f"알겠습니다, sir. {_say_duration(seconds, True)} 뒤에 {what}을(를) 알려 드리겠습니다."
    else:
        en = f"A timer for {_say_duration(seconds)}, starting now, sir."
        ko_s = f"{_say_duration(seconds, True)} 타이머를 시작합니다, sir."
    return ok(en, {"speak_ko": ko_s, "timer": t, "due_at": when.isoformat(timespec="seconds")})


def list_timers(**_) -> dict:
    with _LOCK:
        items = sorted(_TIMERS, key=lambda t: t["due"])
    if not items:
        return ok("You have no timers or reminders running, sir.", {"speak_ko": "설정된 타이머나 알림이 없습니다, sir.",
                                                                    "timers": []})
    now = time.time()
    en, ko = [], []
    for t in items[:5]:
        left = max(0, int(t["due"] - now))
        if t.get("what"):
            en.append(f"a reminder to {t['what']} in {_say_duration(left)}")
            ko.append(f"{_say_duration(left, True)} 뒤 '{t['what']}' 알림")
        else:
            en.append(f"a timer with {_say_duration(left)} left")
            ko.append(f"{_say_duration(left, True)} 남은 타이머")
    return ok("You have " + "; ".join(en) + ", sir.", {"speak_ko": ", ".join(ko) + "이(가) 있습니다, sir.",
                                                      "timers": items})


def cancel_timer(what: str = "", **_) -> dict:
    key = (what or "").lower().strip()
    with _LOCK:
        if not _TIMERS:
            return ok("There's no timer to cancel, sir.", {"speak_ko": "취소할 타이머가 없습니다, sir."})
        if key and key not in ("all", "everything", "timers", "reminders", "모두", "전부"):
            hit = [t for t in _TIMERS if key in (t.get("what") or "").lower()] or \
                  [max(_TIMERS, key=lambda t: t["created"])]
        elif key:
            hit = list(_TIMERS)
        else:
            hit = [max(_TIMERS, key=lambda t: t["created"])]          # the latest one
        for t in hit:
            _TIMERS.remove(t)
        _save()
    _WAKE.set()
    n = len(hit)
    return ok(f"Cancelled {'it' if n == 1 else f'{n} of them'}, sir.",
              {"speak_ko": f"{'취소했습니다' if n == 1 else f'{n}개를 취소했습니다'}, sir.", "cancelled": hit})
