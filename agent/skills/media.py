"""Media-key control (requirement 20): play/pause, next, previous, volume keys."""
from __future__ import annotations

from .base import ok, fail, IS_WINDOWS

# Windows virtual-key codes for media keys
_VK = {"play": 0xB3, "pause": 0xB3, "playpause": 0xB3, "resume": 0xB3,
       "next": 0xB0, "previous": 0xB1, "prev": 0xB1, "back": 0xB1,
       "stop": 0xB2, "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}
_ALIAS = {"skip": "next", "forward": "next"}


def media_control(action: str = "playpause", **_) -> dict:
    act = _ALIAS.get((action or "").lower(), (action or "playpause").lower())
    vk = _VK.get(act)
    if vk is None:
        return fail(f"I don't recognise the media action '{action}', sir.")
    if not IS_WINDOWS:
        return ok(f"(Dev) Media '{act}' would be sent, sir.", {"action": act})
    try:
        import ctypes
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)            # type: ignore[attr-defined]
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)  # type: ignore[attr-defined]
        return ok(f"Done, sir — {act}.", {"action": act})
    except Exception as e:
        return fail(f"I couldn't send the media key, sir.", str(e))
