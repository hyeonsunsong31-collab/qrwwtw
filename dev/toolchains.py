"""
dev/toolchains.py -- the compilers and runtimes the developer can use, found or installed on demand.

detect() lists what this PC already has (for the coding model's instructions). install(name)
installs one with winget (Windows' own package manager, free) -- only after you agree, through
the same permission prompt as everything else ("install_software"). The coding model asks for a
tool with the INSTALL directive / install_tool call instead of improvising install commands.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# name: (commands that prove it is there, winget id, what it is)
TOOLS: Dict[str, tuple] = {
    "python": (["python --version", "py --version"], "Python.Python.3.12", "Python 3"),
    "node": (["node --version"], "OpenJS.NodeJS.LTS", "Node.js LTS (and npm)"),
    "git": (["git --version"], "Git.Git", "Git"),
    "dotnet": (["dotnet --version"], "Microsoft.DotNet.SDK.8", ".NET 8 SDK (C#)"),
    "java": (["java -version"], "Microsoft.OpenJDK.21", "Java 21 (OpenJDK)"),
    "rust": (["cargo --version"], "Rustlang.Rustup", "Rust (rustup, cargo)"),
    "go": (["go version"], "GoLang.Go", "Go"),
    "gcc": (["gcc --version"], "BrechtSanders.WinLibs.POSIX.UCRT", "GCC / G++ (MinGW)"),
    "cmake": (["cmake --version"], "Kitware.CMake", "CMake"),
    "arm-none-eabi-gcc": (["arm-none-eabi-gcc --version"], "Arm.GnuArmEmbeddedToolchain", "ARM GCC (Game Boy Advance)"),
    "mgba": (["mgba --version", "mgba-qt --version"], "JeffreyPfau.mGBA", "mGBA (GBA emulator)"),
    "7zip": (["7z i"], "7zip.7zip", "7-Zip"),
    "pandoc": (["pandoc --version"], "JohnMacFarlane.Pandoc", "Pandoc (documents)"),
    "ffmpeg": (["ffmpeg -version"], "Gyan.FFmpeg", "FFmpeg (audio/video)"),
    "libreoffice": (["soffice --version"], "TheDocumentFoundation.LibreOffice", "LibreOffice (Office files)"),
    "godot": (["godot --version"], "GodotEngine.GodotEngine", "Godot game engine"),
    "make": (["make --version"], "ezwinports.make", "GNU make"),
}
EXTRA = {   # installed with a language's own package manager
    "ilspycmd": ("dotnet tool install -g ilspycmd", ".NET decompiler (ILSpy)"),
    "pyinstaller": ("python -m pip install pyinstaller", "PyInstaller (Python -> .exe)"),
}


def _ok(cmd: str) -> bool:
    exe = cmd.split()[0]
    if not shutil.which(exe):
        return False
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return r.returncode == 0 or exe in ("java",)
    except Exception:
        return False


def detect(names: Optional[List[str]] = None) -> Dict[str, bool]:
    out = {}
    for name in names or TOOLS:
        cmds = TOOLS.get(name, ([name + " --version"],))[0]
        out[name] = any(_ok(c) for c in cmds)
    return out


def summary() -> str:
    have = [n for n, ok in detect(["python", "node", "git", "dotnet", "java", "rust", "go", "gcc",
                                   "arm-none-eabi-gcc", "ffmpeg", "pandoc"]).items() if ok]
    return ", ".join(have) or "python"


def refresh_path() -> None:
    """After an install, pick up the new PATH entries without restarting JARVIS (Windows)."""
    if not IS_WINDOWS:
        return
    try:
        import winreg
        parts = []
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, r"Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    parts.append(os.path.expandvars(val))
            except OSError:
                pass
        if parts:
            current = os.environ.get("PATH", "").split(os.pathsep)
            for p in os.pathsep.join(parts).split(os.pathsep):
                if p and p not in current:
                    current.append(p)
            os.environ["PATH"] = os.pathsep.join(current)
    except Exception:
        pass


def install(name: str, ask: Callable[..., bool], log: Optional[Callable[[str], None]] = None) -> str:
    """Install one tool (asks first). Returns what happened, in words."""
    log = log or (lambda m: None)
    key = name.strip().lower()
    key = {"nodejs": "node", "npm": "node", "c#": "dotnet", ".net": "dotnet", "csharp": "dotnet", "jdk": "java",
           "cargo": "rust", "rustc": "rust", "golang": "go", "g++": "gcc", "mingw": "gcc", "7z": "7zip",
           "devkitarm": "arm-none-eabi-gcc", "gba": "arm-none-eabi-gcc", "office": "libreoffice"}.get(key, key)
    if key in TOOLS and detect([key]).get(key):
        return f"{key} is already installed"
    if key in EXTRA:
        cmd, what = EXTRA[key]
        if not ask("install_software", f"Install {what}: {cmd}", risk=0.6):
            return f"installing {what} was not allowed"
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800, creationflags=NO_WINDOW)
        refresh_path()
        return f"installed {what}" if r.returncode == 0 else f"installing {what} failed: {(r.stderr or r.stdout)[-300:]}"
    if key not in TOOLS:
        return (f"I don't know a package called {name!r}; known: {', '.join(sorted(TOOLS))}. Install packages for "
                f"the project with RUN (pip / npm) instead")
    _cmds, wid, what = TOOLS[key]
    if not IS_WINDOWS:
        return f"{what} is not installed on this machine (JARVIS installs it with winget on Windows)"
    winget = shutil.which("winget")
    if not winget:
        return "winget (Windows' App Installer) is missing; install 'App Installer' from the Microsoft Store"
    if not ask("install_software", f"Install {what} with winget ({wid})", risk=0.6):
        return f"installing {what} was not allowed"
    log(f"installing {what} with winget")
    r = subprocess.run([winget, "install", "--id", wid, "-e", "--silent", "--accept-package-agreements",
                        "--accept-source-agreements", "--disable-interactivity"], capture_output=True, text=True,
                       timeout=3600, creationflags=NO_WINDOW)
    refresh_path()
    if r.returncode == 0 or re.search(r"already installed|No newer package", r.stdout or "", re.I):
        return f"installed {what}" + ("" if detect([key]).get(key) else " (a new terminal may be needed to see it)")
    return f"installing {what} failed (winget exit {r.returncode}): {(r.stdout or r.stderr)[-300:]}"
