"""Shared helpers for skills. Skills are plain functions returning a result dict:
   {"ok": bool, "speak": str, "data": dict, "error": str|None}
Each skill degrades gracefully when a platform-specific library is missing, so the whole
system imports and runs on Linux (for testing) and works fully on Windows."""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Optional

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def ok(speak: str, data: Optional[dict] = None) -> dict:
    return {"ok": True, "speak": speak, "data": data or {}, "error": None}


def fail(speak: str, error: Optional[str] = None) -> dict:
    return {"ok": False, "speak": speak, "data": {}, "error": error or speak}


def not_on_this_os(what: str) -> dict:
    return fail(f"{what} is available when JARVIS runs on Windows, sir.",
                error=f"{what} requires Windows (current platform: {sys.platform})")


def run(cmd, timeout: float = 20.0, shell: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)


def powershell(script: str, timeout: float = 20.0) -> str:
    """Run a PowerShell snippet and return stdout (Windows)."""
    if not IS_WINDOWS:
        raise OSError("PowerShell is Windows-only")
    cp = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)
    return (cp.stdout or cp.stderr).strip()
