"""Application control (requirement 20): launch / close installed apps."""
from __future__ import annotations

import os
import shutil
from .base import ok, fail, run, IS_WINDOWS, IS_MAC

# Friendly name -> launch target (Windows). ms-settings: and shell: are URI handlers.
WIN_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "explorer": "explorer", "file explorer": "explorer",
    "cmd": "cmd", "command prompt": "cmd", "powershell": "powershell",
    "task manager": "taskmgr", "control panel": "control", "settings": "ms-settings:",
    "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
    "spotify": "spotify", "vscode": "code", "vs code": "code", "code": "code",
    "terminal": "wt", "camera": "microsoft.windows.camera:",
}
LINUX_ALIASES = {
    "files": "xdg-open", "browser": "xdg-open", "chrome": "google-chrome",
    "firefox": "firefox", "terminal": "x-terminal-emulator", "calculator": "gnome-calculator",
    "text editor": "gedit", "editor": "gedit",
}


def launch_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application, sir?")
    if IS_WINDOWS:
        target = WIN_ALIASES.get(name.lower(), name)
        try:
            if target.endswith(":"):  # URI handler (settings, camera, ...)
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                # 'start' resolves apps on PATH and registered App Paths
                run(["cmd", "/c", "start", "", target], timeout=10)
            return ok(f"Opening {name}, sir.", {"app": name, "target": target})
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))
    # non-Windows (dev/testing)
    target = LINUX_ALIASES.get(name.lower(), name) if not IS_MAC else name
    exe = shutil.which(target.split()[0])
    if IS_MAC:
        try:
            run(["open", "-a", name], timeout=10)
            return ok(f"Opening {name}, sir.", {"app": name})
        except Exception as e:
            return fail(f"I couldn't open {name}, sir.", str(e))
    if not exe:
        return fail(f"I can't find {name} on this system, sir.",
                    error=f"executable not found: {target}")
    try:
        import subprocess
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ok(f"Opening {name}, sir.", {"app": name, "target": exe})
    except Exception as e:
        return fail(f"I couldn't open {name}, sir.", str(e))


def close_app(app: str, **_) -> dict:
    name = (app or "").strip()
    if not name:
        return fail("Which application should I close, sir?")
    try:
        if IS_WINDOWS:
            image = WIN_ALIASES.get(name.lower(), name)
            image = image if image.lower().endswith(".exe") else image + ".exe"
            cp = run(["taskkill", "/IM", image, "/F"], timeout=10)
            okk = cp.returncode == 0
        else:
            cp = run(["pkill", "-f", name], timeout=10)
            okk = cp.returncode in (0, 1)
        return ok(f"Closed {name}, sir.", {"app": name}) if okk \
            else fail(f"{name} didn't appear to be running, sir.", cp.stderr)
    except Exception as e:
        return fail(f"I couldn't close {name}, sir.", str(e))
