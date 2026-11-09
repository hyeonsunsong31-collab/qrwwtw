"""
agent/settings.py
=================
Validated, hot-applied settings for the holographic Settings panel (requirement 2-(3):
options for continuous adjustment).

Only keys listed in SCHEMA (plus permission categories) can be changed from the UI, and
every value is type/range checked before it is written. `run_shell_elevated` stays locked
to "deny". Files are written atomically so a crash mid-save can't corrupt the config.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Tuple

SCHEMA: Dict[str, dict] = {
    "language.default":          {"type": "choice", "choices": ["en", "ko"], "label": "Default language"},
    "language.auto_switch":      {"type": "bool", "label": "Auto-switch language"},
    "voice.enabled":             {"type": "bool", "label": "Voice enabled", "restart": True},
    "voice.wakeword.engine":     {"type": "choice", "choices": ["vosk", "openwakeword"],
                                  "label": "Wake-word engine", "restart": True},
    "voice.wakeword.sensitivity": {"type": "float", "min": 0.3, "max": 0.95, "step": 0.05,
                                   "label": "Wake sensitivity"},
    "voice.wakeword.lenient":    {"type": "bool", "label": "Wake on 'Jarvis' alone"},
    "voice.barge_in.mode":       {"type": "choice", "choices": ["keyword", "vad", "off"],
                                  "label": "Interrupt mode"},
    "voice.tts.voice_en":        {"type": "str", "pattern": r"^[A-Za-z0-9_.-]{1,64}$",
                                  "label": "English voice (Piper)", "restart": True},
    "voice.tts.voice_ko":        {"type": "str", "pattern": r"^[A-Za-z0-9_.-]{0,64}$",
                                  "label": "Korean voice (Piper, optional)", "restart": True},
    "voice.tts.rate":            {"type": "float", "min": 0.6, "max": 1.6, "step": 0.05,
                                  "label": "Speaking rate"},
    "space3d.gesture_control":   {"type": "bool", "label": "Hand gestures when 3D opens"},
    "space3d.replace_wallpaper": {"type": "bool", "label": "Allow wallpaper mode"},
    "llm.model":                 {"type": "str", "pattern": r"^[A-Za-z0-9_.:/-]{1,80}$",
                                  "label": "Language model", "restart": True},
    "llm.temperature":           {"type": "float", "min": 0.0, "max": 1.5, "step": 0.05,
                                  "label": "Creativity"},
}

PERMISSION_CHOICES = ["allow", "ask", "deny"]
LOCKED_PERMISSIONS = {"run_shell_elevated"}


def _get(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def validate(key: str, value: Any) -> Any:
    """Return the coerced value, or raise ValueError with a human-readable reason."""
    if key.startswith("permissions."):
        cat = key.split(".", 1)[1]
        if cat in LOCKED_PERMISSIONS:
            raise ValueError("this permission is locked to 'deny'")
        if value not in PERMISSION_CHOICES:
            raise ValueError(f"must be one of {PERMISSION_CHOICES}")
        return value
    spec = SCHEMA.get(key)
    if spec is None:
        raise ValueError("not an editable setting")
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "on", "yes"):
            return True
        if str(value).lower() in ("false", "0", "off", "no"):
            return False
        raise ValueError("must be true or false")
    if t == "choice":
        if value not in spec["choices"]:
            raise ValueError(f"must be one of {spec['choices']}")
        return value
    if t == "float":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if not (spec["min"] <= v <= spec["max"]):
            raise ValueError(f"must be between {spec['min']} and {spec['max']}")
        return round(v, 3)
    if t == "str":
        v = str(value).strip()
        if not re.match(spec["pattern"], v):
            raise ValueError("contains characters that aren't allowed")
        return v
    raise ValueError("unsupported setting type")


def get_settings(config_path: str, permissions_path: str) -> dict:
    cfg = _read(config_path)
    perms = _read(permissions_path)
    schema = copy.deepcopy(SCHEMA)
    values = {k: _get(cfg, k) for k in SCHEMA}
    for cat, val in perms.get("categories", {}).items():
        key = f"permissions.{cat}"
        schema[key] = {"type": "choice", "choices": PERMISSION_CHOICES,
                       "label": cat.replace("_", " "), "group": "permissions",
                       "locked": cat in LOCKED_PERMISSIONS}
        values[key] = val
    return {"schema": schema, "values": values}


def apply_settings(changes: Dict[str, Any], config_path: str,
                   permissions_path: str) -> dict:
    cfg = _read(config_path)
    perms = _read(permissions_path)
    applied: Dict[str, Any] = {}
    rejected: Dict[str, str] = {}
    restart: List[str] = []
    for key, value in (changes or {}).items():
        try:
            v = validate(key, value)
        except ValueError as e:
            rejected[key] = str(e)
            continue
        if key.startswith("permissions."):
            perms.setdefault("categories", {})[key.split(".", 1)[1]] = v
        else:
            _set(cfg, key, v)
            if SCHEMA[key].get("restart"):
                restart.append(key)
        applied[key] = v
    if applied:
        _write_atomic(config_path, cfg)
        _write_atomic(permissions_path, perms)
    return {"applied": applied, "rejected": rejected, "restart_required": restart}
