"""
Music from the web (requirement 19): play a requested song by voice or text.

Primary path: resolve the query to an audio stream with yt-dlp (run through this Python,
so it works without being on PATH), then play it in the background with ffplay (installed
by the setup script) or mpv. Fallback: open a web player in the default browser.
stop_music() stops local playback; the supervisor keeps yt-dlp updated weekly.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse

from .base import ok, fail, IS_WINDOWS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_player_proc = None
_now_playing = None
_lock = threading.Lock()


def find_player():
    """Return a command prefix for ffplay or mpv, or None."""
    for exe in ("ffplay", "mpv"):
        p = shutil.which(exe)
        if p:
            return exe, p
    candidates = glob.glob(os.path.join(ROOT, "tools", "ffmpeg", "**", "ffplay.exe"), recursive=True)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffplay.exe"))
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*",
                                             "**", "ffplay.exe"), recursive=True)
    for c in candidates:
        if os.path.exists(c):
            return "ffplay", c
    return None


def _resolve_stream(query: str):
    """Return (title, stream_url) using yt-dlp, or (None, None)."""
    try:
        cp = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "--no-playlist",
             "--default-search", "ytsearch1", "--print", "title", "-g", query],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        if len(lines) >= 2:
            return lines[0], lines[-1]
    except Exception:
        pass
    return None, None


def play_music(query: str, **_) -> dict:
    q = (query or "").strip()
    if not q:
        return fail("What would you like me to play, sir?")
    global _player_proc, _now_playing
    player = find_player()
    if player:
        title, url = _resolve_stream(q)
        if url:
            kind, exe = player
            cmd = ([exe, "-nodisp", "-autoexit", "-loglevel", "quiet", url] if kind == "ffplay"
                   else [exe, "--no-video", "--really-quiet", url])
            try:
                with _lock:
                    _stop_locked()
                    _player_proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0)
                    _now_playing = title or q
                return ok(f"Now playing {title or q}, sir.", {"title": title or q, "mode": "stream"})
            except Exception as e:
                return fail("I found the track but couldn't start the player, sir.", str(e))

    # fallback: open a web player
    import webbrowser
    web = "https://music.youtube.com/search?q=" + urllib.parse.quote(q)
    try:
        webbrowser.open(web, new=2)
        return ok(f"Opening a player for {q}, sir.", {"query": q, "mode": "web", "url": web})
    except Exception as e:
        return fail("I couldn't start any music, sir.", str(e))


def _stop_locked() -> bool:
    global _player_proc, _now_playing
    was = _player_proc is not None and _player_proc.poll() is None
    if was:
        try:
            _player_proc.terminate()
        except Exception:
            pass
    _player_proc, _now_playing = None, None
    return was


def stop_music(**_) -> dict:
    with _lock:
        was = _stop_locked()
    return ok("Music stopped, sir." if was else "Nothing was playing, sir.")


def now_playing(**_) -> dict:
    with _lock:
        playing = _player_proc is not None and _player_proc.poll() is None
        title = _now_playing if playing else None
    return ok(f"Now playing {title}, sir." if title else "Nothing is playing at the moment, sir.",
              {"title": title})
