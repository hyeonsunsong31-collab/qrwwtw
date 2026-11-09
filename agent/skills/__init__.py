"""
Skill registry (requirement 15: every function reachable by voice OR text through the same
command bus). Maps an intent name to a handler + permission category + ETA key.

handler(args: dict) -> {"ok", "speak", "data", "error"}
UI intents return data["ui"], which the orchestrator forwards to the overlay.
"""
from __future__ import annotations

from typing import Callable, Dict, NamedTuple

from . import files, apps, system, browser, media, music, devices, windows


class Skill(NamedTuple):
    handler: Callable[..., dict]
    category: str      # maps to config/permissions.json
    eta: str           # maps to recorder ETA keys


def _call(fn):
    """Adapt fn(**kwargs) so the orchestrator can pass one args dict."""
    def wrapper(args: dict):
        return fn(**{k: v for k, v in (args or {}).items() if not k.startswith("_")})
    return wrapper


def _ui(action: str, speak: str, **extra):
    """A UI intent: the overlay performs it (3D space, settings panel, ...)."""
    def handler(args: dict):
        data = {"ui": action, **extra}
        data.update({k: v for k, v in (args or {}).items() if not k.startswith("_")})
        return {"ok": True, "speak": speak, "data": data, "error": None}
    return handler


REGISTRY: Dict[str, Skill] = {
    # ---- 3D project space (overlay-side actions) ----
    "open_space3d":   Skill(_ui("open_space3d", "Bringing up the 3D project space, sir."), "read_system_info", "open_space3d"),
    "close_space3d":  Skill(_ui("close_space3d", "Closing the 3D space, sir."), "read_system_info", "open_space3d"),
    "clone_object":   Skill(_ui("clone_object", "Cloning the selected object, sir."), "read_system_info", "open_space3d"),
    "add_object":     Skill(_ui("add_object", "Adding a new object, sir."), "read_system_info", "open_space3d"),
    "delete_object":  Skill(_ui("delete_object", "Removing the selected object, sir."), "read_system_info", "open_space3d"),
    "save_project":   Skill(_ui("save_project", "Saving the project, sir."), "jarvis_data", "file_write"),
    "export_model":   Skill(_ui("export_model", "Exporting the model, sir."), "jarvis_data", "file_write"),
    "toggle_gesture": Skill(_ui("toggle_gesture", "Toggling camera gesture control, sir."), "read_system_info", "open_space3d"),
    "neural_view":    Skill(_ui("neural_view", "Showing my neural mesh, sir."), "read_system_info", "open_space3d"),
    "wallpaper_on":   Skill(_ui("wallpaper_on", "Setting the 3D space as your wallpaper, sir."), "window_control", "settings"),
    "wallpaper_off":  Skill(_ui("wallpaper_off", "Restoring your usual wallpaper, sir."), "window_control", "settings"),
    "open_jarvis_settings": Skill(_ui("open_settings_panel", "Here are my settings, sir."), "read_system_info", "chat"),
    "sleep_assistant": Skill(_ui("sleep", "Very good, sir. I'll stand by."), "read_system_info", "chat"),

    # ---- music / media ----
    "play_music":     Skill(_call(music.play_music), "play_music", "play_music"),
    "stop_music":     Skill(_call(music.stop_music), "media_control", "chat"),
    "now_playing":    Skill(_call(music.now_playing), "media_control", "chat"),
    "media_control":  Skill(_call(media.media_control), "media_control", "chat"),

    # ---- apps ----
    "open_app":       Skill(_call(apps.launch_app), "launch_apps", "open_app"),
    "close_app":      Skill(_call(apps.close_app), "control_apps", "open_app"),

    # ---- windows, keyboard, screen ----
    "window_focus":    Skill(_call(windows.focus_window), "window_control", "chat"),
    "window_minimize": Skill(_call(windows.minimize_window), "window_control", "chat"),
    "window_maximize": Skill(_call(windows.maximize_window), "window_control", "chat"),
    "show_desktop":    Skill(_call(windows.show_desktop), "window_control", "chat"),
    "list_windows":    Skill(_call(windows.list_windows), "window_control", "chat"),
    "type_text":       Skill(_call(windows.type_text), "keyboard_input", "chat"),
    "press_keys":      Skill(_call(windows.press_keys), "keyboard_input", "chat"),
    "screenshot":      Skill(_call(windows.screenshot), "screen_capture", "chat"),

    # ---- system ----
    "mute_mic":       Skill(_call(system.mute_mic), "control_apps", "chat"),
    "unmute_mic":     Skill(_call(system.unmute_mic), "control_apps", "chat"),
    "set_volume":     Skill(_call(system.set_volume), "media_control", "chat"),
    "system_info":    Skill(_call(system.system_info), "read_system_info", "system_info"),
    "settings":       Skill(_call(system.open_settings), "change_system_settings", "settings"),
    "power":          Skill(_call(system.power), "change_system_settings", "settings"),
    "lock":           Skill(_call(system.lock_workstation), "change_system_settings", "chat"),
    "list_devices":   Skill(_call(devices.list_devices), "read_devices", "devices"),

    # ---- files ----
    "file_search":    Skill(_call(files.search_files), "read_files", "file_search"),
    "file_read":      Skill(_call(files.read_file), "read_files", "file_search"),
    "file_write":     Skill(_call(files.write_file), "write_files", "file_write"),
    "file_delete":    Skill(_call(files.delete_file), "delete_files", "file_write"),
    "list_dir":       Skill(_call(files.list_dir), "read_files", "file_search"),

    # ---- browser / web ----
    "open_url":       Skill(_call(browser.open_url), "browser_open", "browser"),
    "web_search":     Skill(_call(browser.web_search), "browser_navigate", "browser"),
    "read_page":      Skill(_call(browser.read_page), "browser_read", "browser"),
}

__all__ = ["REGISTRY", "Skill"]
