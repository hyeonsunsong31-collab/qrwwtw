"""
Skill registry (requirement 15: every function reachable by voice OR text, through the
same command bus). Maps an intent name to a handler + permission category + ETA key.

handler(args: dict) -> {"ok","speak","data","error"}
"""
from __future__ import annotations

from typing import Callable, Dict, NamedTuple

from . import files, apps, system, browser, media, music, devices


class Skill(NamedTuple):
    handler: Callable[..., dict]
    category: str      # maps to config/permissions.json
    eta: str           # maps to recorder ETA keys


def _call(fn):
    # adapt handler(**args) so the orchestrator can pass a single args dict
    def wrapper(args: dict):
        return fn(**(args or {}))
    return wrapper


REGISTRY: Dict[str, Skill] = {
    # 3D space is handled by the overlay but registered so voice/text both resolve it.
    "open_space3d":   Skill(lambda args: {"ok": True, "speak": "Bringing up the 3D project space, sir.", "data": {"ui": "open_space3d"}, "error": None}, "read_system_info", "open_space3d"),
    "close_space3d":  Skill(lambda args: {"ok": True, "speak": "Closing the 3D space, sir.", "data": {"ui": "close_space3d"}, "error": None}, "read_system_info", "open_space3d"),
    "clone_object":   Skill(lambda args: {"ok": True, "speak": "Cloning the selected object, sir.", "data": {"ui": "clone_object"}, "error": None}, "read_system_info", "open_space3d"),
    "toggle_gesture": Skill(lambda args: {"ok": True, "speak": "Toggling camera gesture control, sir.", "data": {"ui": "toggle_gesture", "state": args.get("state", "")}, "error": None}, "read_system_info", "open_space3d"),

    "play_music":     Skill(_call(music.play_music), "play_music", "play_music"),
    "stop_music":     Skill(_call(music.stop_music), "media_control", "chat"),
    "media_control":  Skill(_call(media.media_control), "media_control", "chat"),

    "open_app":       Skill(_call(apps.launch_app), "launch_apps", "open_app"),
    "close_app":      Skill(_call(apps.close_app), "control_apps", "open_app"),

    "mute_mic":       Skill(_call(system.mute_mic), "control_apps", "chat"),
    "unmute_mic":     Skill(_call(system.unmute_mic), "control_apps", "chat"),
    "set_volume":     Skill(_call(system.set_volume), "media_control", "chat"),
    "system_info":    Skill(_call(system.system_info), "read_system_info", "system_info"),
    "settings":       Skill(_call(system.open_settings), "change_system_settings", "settings"),
    "power":          Skill(_call(system.power), "change_system_settings", "settings"),
    "lock":           Skill(_call(system.lock_workstation), "change_system_settings", "chat"),
    "list_devices":   Skill(_call(devices.list_devices), "read_devices", "devices"),

    "file_search":    Skill(_call(files.search_files), "read_files", "file_search"),
    "file_read":      Skill(_call(files.read_file), "read_files", "file_search"),
    "file_write":     Skill(_call(files.write_file), "write_files", "file_write"),
    "file_delete":    Skill(_call(files.delete_file), "delete_files", "file_write"),
    "list_dir":       Skill(_call(files.list_dir), "read_files", "file_search"),

    "sleep_assistant": Skill(lambda args: {"ok": True, "speak": "Very good, sir. I'll stand by.", "data": {"ui": "sleep"}, "error": None}, "read_system_info", "chat"),

    "open_url":       Skill(_call(browser.open_url), "browser_open", "browser"),
    "web_search":     Skill(_call(browser.web_search), "browser_navigate", "browser"),
    "read_page":      Skill(_call(browser.read_page), "browser_read", "browser"),
}

__all__ = ["REGISTRY", "Skill"]
