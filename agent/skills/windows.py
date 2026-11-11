"""
Window, keyboard and screen skills (requirement 20: use the features of installed apps).

  focus / minimize / maximize a window by (fuzzy) title, show the desktop,
  type text into the focused app, press a key combination, take a screenshot.

Typing and key presses act on whatever app has focus, so they sit in the 'keyboard_input'
permission category (ask by default). Screenshots are saved to Pictures\\JARVIS and a copy
is kept in the results archive via `generated_files`.
"""
from __future__ import annotations

import difflib
import os
from datetime import datetime
from pathlib import Path

from .base import ok, fail, IS_WINDOWS, not_on_this_os


def _gw():
    import pygetwindow as gw  # type: ignore  (Windows)
    return gw


def _pg():
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to abort automation
    return pyautogui


def _find_window(title: str):
    gw = _gw()
    wins = [w for w in gw.getAllWindows() if (w.title or "").strip()]
    if not wins:
        return None
    q = (title or "").strip().lower()
    if not q:
        return gw.getActiveWindow()
    for w in wins:
        if w.title.lower() == q:
            return w
    contains = [w for w in wins if q in w.title.lower()]
    if contains:
        return min(contains, key=lambda w: len(w.title))
    titles = [w.title.lower() for w in wins]
    close = difflib.get_close_matches(q, titles, n=1, cutoff=0.55)
    return wins[titles.index(close[0])] if close else None


def list_windows(**_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Window control")
    try:
        titles = [w.title for w in _gw().getAllWindows() if (w.title or "").strip()]
        return ok(f"{len(titles)} window(s) open, sir.", {"windows": titles[:60]})
    except Exception as e:
        return fail("I couldn't list the windows, sir.", str(e))


def focus_window(title: str = "", **_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Window control")
    try:
        w = _find_window(title)
        if not w:
            return fail(f"I can't see a window called {title}, sir.")
        if w.isMinimized:
            w.restore()
        w.activate()
        return ok(f"Switched to {w.title}, sir.", {"window": w.title})
    except Exception as e:
        return fail(f"I couldn't switch to {title}, sir.", str(e))


def minimize_window(title: str = "", **_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Window control")
    if (title or "").strip().lower() in ("all", "everything", "all windows"):
        return show_desktop()
    try:
        w = _find_window(title)
        if not w:
            return fail(f"I can't see a window called {title}, sir.")
        w.minimize()
        return ok(f"Minimised {w.title}, sir.", {"window": w.title})
    except Exception as e:
        return fail("I couldn't minimise that window, sir.", str(e))


def maximize_window(title: str = "", **_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Window control")
    try:
        w = _find_window(title)
        if not w:
            return fail(f"I can't see a window called {title}, sir.")
        w.maximize()
        return ok(f"Maximised {w.title}, sir.", {"window": w.title})
    except Exception as e:
        return fail("I couldn't maximise that window, sir.", str(e))


def show_desktop(**_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Showing the desktop")
    try:
        _pg().hotkey("win", "d")
        return ok("Here's the desktop, sir.")
    except Exception as e:
        return fail("I couldn't show the desktop, sir.", str(e))


def type_text(text: str = "", **_) -> dict:
    text = text or ""
    if not text:
        return fail("What should I type, sir?")
    try:
        pg = _pg()
        if text.isascii():
            pg.write(text, interval=0.01)
        else:  # Korean and other non-ASCII text: paste through the clipboard
            import pyperclip  # type: ignore
            pyperclip.copy(text)
            pg.hotkey("ctrl", "v")
        return ok("Typed it, sir.", {"chars": len(text)})
    except Exception as e:
        return fail("I couldn't type into the current window, sir.", str(e))


def press_keys(keys: str = "", **_) -> dict:
    parts = [p for p in (keys or "").lower().replace("+", " ").split() if p]
    if not parts:
        return fail("Which keys, sir?")
    try:
        pg = _pg()
        unknown = [p for p in parts if p not in pg.KEYBOARD_KEYS]
        if unknown:
            return fail(f"I don't recognise the key {unknown[0]}, sir.")
        pg.hotkey(*parts)
        return ok(f"Pressed {'+'.join(parts)}, sir.", {"keys": parts})
    except Exception as e:
        return fail("I couldn't press those keys, sir.", str(e))


def screenshot(**_) -> dict:
    try:
        img = _pg().screenshot()
    except Exception as e:
        return fail("I couldn't capture the screen, sir.", str(e))
    folder = Path.home() / "Pictures" / "JARVIS"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path(os.getcwd())
    path = folder / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        img.save(path)
    except Exception as e:
        return fail("I captured the screen but couldn't save it, sir.", str(e))
    return ok(f"Screenshot saved to {path.parent.name}/{path.name}, sir.",
              {"path": str(path), "generated_files": [str(path)]})
