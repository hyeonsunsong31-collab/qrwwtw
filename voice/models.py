"""
voice/models.py -- fetch a missing speech model, safely (v1.4.2).

The installer downloads the Vosk wake-word model, but a blocked or interrupted download left
JARVIS without its "wake up Jarvis" listener -- and only a line in the installer's output said
so. The voice engine now repairs it by itself at start-up: the archive is downloaded, its
SHA-256 checked against the value pinned here (so a tampered or truncated file is never
used), unpacked into a staging folder with every path validated, and only then moved into
place.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from typing import Callable, Tuple

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOSK_SHA256 = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
VOSK_TOP = "vosk-model-small-en-us-0.15/"
MAX_BYTES = 200 * 1024 * 1024


def ensure_vosk_model(dest_dir: str, log: Callable[[str], None] = print, url: str = VOSK_URL,
                      sha256: str = VOSK_SHA256, top: str = VOSK_TOP,
                      timeout: float = 60.0) -> Tuple[bool, str]:
    """Make sure dest_dir holds the Vosk model. Returns (ok, detail)."""
    if os.path.isdir(os.path.join(dest_dir, "conf")):
        return True, "present"
    parent = os.path.dirname(os.path.abspath(dest_dir))
    os.makedirs(parent, exist_ok=True)
    tmp_zip = os.path.join(parent, ".vosk-download.zip")
    staging = os.path.join(parent, ".vosk-staging")
    try:
        log(f"downloading the wake-word model from {url} (about 40 MB) ...")
        h, n = hashlib.sha256(), 0
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-voice-repair"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp_zip, "wb") as fh:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                n += len(chunk)
                if n > MAX_BYTES:
                    raise ValueError("the download is far larger than the model should be")
                fh.write(chunk)
                h.update(chunk)
        if h.hexdigest() != sha256:
            raise ValueError(f"checksum mismatch (got {h.hexdigest()[:16]}...): the file is damaged or not the model")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        with zipfile.ZipFile(tmp_zip) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                parts = name.split("/")
                if not name.startswith(top) or ".." in parts or name.startswith("/") or ":" in name:
                    continue                                  # outside the model folder: ignore
                rel = name[len(top):]
                if not rel:
                    continue
                target = os.path.join(staging, *[p for p in rel.split("/") if p])
                if name.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        if not os.path.isdir(os.path.join(staging, "conf")):
            raise ValueError("the archive does not contain a Vosk model")
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir)                           # an incomplete earlier attempt
        os.replace(staging, dest_dir)
        log(f"wake-word model installed in {dest_dir}")
        return True, f"downloaded ({n // (1024 * 1024)} MB)"
    except Exception as e:
        log(f"could not download the wake-word model: {e}")
        return False, str(e)[:300]
    finally:
        for path in (tmp_zip,):
            try:
                os.remove(path)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
