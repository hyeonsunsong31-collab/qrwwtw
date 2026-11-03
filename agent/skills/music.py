"""
Music from the web (requirement 19): play a requested song by voice or text.

Primary path (fully local playback): resolve the query to an audio stream with yt-dlp,
then play it with mpv or ffplay in the background. Fallback: open a web player in the
default browser. stop_music() stops local playback.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.parse

from .base import ok, fail

_player_proc = None
_lock = threading.Lock()


def _have(exe: str) -> bool:
    return shutil.which(exe) is not None


def _resolve_stream(query: str):
    """Return (title, stream_url) using yt-dlp, or (None, None) if unavailable."""
    if not _have("yt-dlp"):
        return None, None
    try:
        # ytsearch1 picks the top match; -g prints the direct media URL; --print title
        cp = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "--no-playlist", "--default-search", "ytsearch1",
             "--print", "title", "-g", query],
            capture_output=True, text=True, timeout=45)
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
    global _player_proc
    title, url = _resolve_stream(q)
    if url and (_have("mpv") or _have("ffplay")):
        player = ["mpv", "--no-video", "--really-quiet", url] if _have("mpv") \
            else ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", url]
        try:
            with _lock:
                stop_music()
                _player_proc = subprocess.Popen(player, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
            return ok(f"Now playing {title or q}, sir.",
                      {"title": title or q, "mode": "stream"})
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


def stop_music(**_) -> dict:
    global _player_proc
    with _lock:
        if _player_proc and _player_proc.poll() is None:
            try:
                _player_proc.terminate()
            except Exception:
                pass
        was = _player_proc is not None
        _player_proc = None
    return ok("Music stopped, sir." if was else "Nothing was playing, sir.")
