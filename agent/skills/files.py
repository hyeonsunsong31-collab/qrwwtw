"""File skills (requirement 20). Cross-platform via pathlib."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from .base import ok, fail

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "AppData", "$Recycle.Bin",
             "Windows", "Program Files", "Program Files (x86)", ".cache"}


def _user_roots() -> List[Path]:
    home = Path.home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    roots = [home / n for n in names if (home / n).exists()]
    return roots or [home]


def search_files(query: str, roots: Optional[List[str]] = None, limit: int = 50) -> dict:
    q = (query or "").strip().lower()
    if not q:
        return fail("What should I search for, sir?")
    search_roots = [Path(r) for r in roots] if roots else _user_roots()
    hits: List[str] = []
    for root in search_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if q in fn.lower():
                    hits.append(str(Path(dirpath) / fn))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    speak = (f"I found {len(hits)} file(s) matching '{query}', sir."
             if hits else f"No files matching '{query}', sir.")
    return ok(speak, {"query": query, "count": len(hits), "files": hits})


def user_path(path: str, default_folder: str = "desktop") -> Path:
    """A path as the user (or a language model) said it -> a real path on this PC:
    a bare name goes on the desktop, "Documents\\x" means the real Documents folder, and a
    made-up user folder ("C:\\Users\\YourUsername\\Desktop\\x") becomes the real one."""
    raw = str(path or "").strip().strip('"').strip("'")
    p = Path(raw).expanduser()
    parts = [x for x in re.split(r"[\\/]+", raw) if x]
    from .base import IS_WINDOWS
    win_style = bool(re.match(r"^[A-Za-z]:", raw)) or "\\" in raw
    if p.is_absolute() or win_style:
        if (IS_WINDOWS or not win_style) and (p.exists() or p.parent.exists()):
            return p
        low = [x.lower() for x in parts]
        if "users" in low:
            i = low.index("users")
            rest = parts[i + 2:]                     # skip the (made-up) user name
            if rest:
                kf = known_folder(rest[0])
                if kf is not None:
                    return kf.joinpath(*rest[1:])
            return Path.home().joinpath(*rest)
        return p
    if parts:
        kf = known_folder(parts[0])
        if kf is not None and len(parts) > 1:
            return kf.joinpath(*parts[1:])
    base = known_folder(default_folder) or Path.home()
    return base.joinpath(*parts) if parts else base


def read_file(path: str, max_bytes: int = 20000) -> dict:
    p = user_path(path)
    if not p.is_file():
        return fail(f"I can't find a file at {path}, sir.")
    try:
        data = p.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        return ok(f"Here are the first {len(data)} bytes of {p.name}, sir.",
                  {"path": str(p), "text": text, "truncated": p.stat().st_size > max_bytes})
    except OSError as e:
        return fail(f"I couldn't read {p.name}, sir.", str(e))


def write_file(path: str, content: str = "") -> dict:
    p = user_path(path)
    if not p.suffix:
        p = p.with_suffix(".txt")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ok(f"I've written {p.name}, sir.", {"path": str(p), "bytes": len(content.encode()),
                                                     "generated_files": [str(p)]})
    except OSError as e:
        return fail(f"I couldn't write {path}, sir.", str(e))


def delete_file(path: str) -> dict:
    p = user_path(path)
    if not p.exists():
        return fail(f"There's nothing at {path} to delete, sir.")
    try:
        if p.is_dir():
            import shutil
            shutil.rmtree(p)
        else:
            p.unlink()
        return ok(f"I've deleted {p.name}, sir.", {"path": str(p)})
    except OSError as e:
        return fail(f"I couldn't delete {path}, sir.", str(e))


def list_dir(path: str = "~") -> dict:
    p = Path(path).expanduser() if path in ("~", "", None) else user_path(path, "home")
    if not p.is_dir():
        return fail(f"{path} isn't a folder, sir.")
    entries = sorted(os.listdir(p))[:200]
    return ok(f"{len(entries)} item(s) in {p.name or str(p)}, sir.",
              {"path": str(p), "entries": entries})


# ------------------------------------------------------------------ folders (v1.5.0)
# Windows' known folders (Desktop / Documents / ... may live in OneDrive)
_KNOWN = {"desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
          "downloads": "{374DE290-123F-4565-9164-39C4925E467B}", "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
          "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}", "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"}
_NAMES = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures",
          "music": "Music", "videos": "Videos"}
_KO_FOLDER = {"desktop": "바탕 화면", "documents": "문서", "downloads": "다운로드", "pictures": "사진", "music": "음악",
              "videos": "동영상", "home": "사용자", "recycle bin": "휴지통", "this pc": "내 PC"}


def known_folder(name: str) -> Optional[Path]:
    key = (name or "").lower().strip().replace(" folder", "")
    key = {"download": "downloads", "document": "documents", "photos": "pictures", "photo": "pictures",
           "picture": "pictures", "video": "videos", "my documents": "documents"}.get(key, key)
    if key in ("home", "user", "my home"):
        return Path.home()
    if key not in _KNOWN:
        return None
    from .base import IS_WINDOWS
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]
            u = uuid.UUID(_KNOWN[key])
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    for cand in (Path.home() / _NAMES[key], Path.home() / "OneDrive" / _NAMES[key]):
        if cand.exists():
            return cand
    return Path.home() / _NAMES[key]


_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def make_folder(name: str = "", where: str = "", path: str = "", **_) -> dict:
    """Create a folder: a name in a known place ("Projects" on the desktop) or a full path."""
    if path and not name:
        target = user_path(path)
    else:
        nm = (name or "").strip().strip(".")
        if not nm:
            return fail("What should I call the new folder, sir?")
        if _BAD_NAME.search(nm):
            nm = _BAD_NAME.sub("_", nm)
        base = known_folder(where) if where else (known_folder("desktop") or Path.home())
        target = (base or Path.home()) / nm
    try:
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return fail(f"I couldn't create that folder, sir.", str(e))
    place = target.parent.name or str(target.parent)
    if existed:
        return ok(f"A folder called {target.name} is already in {place}, sir.",
                  {"path": str(target), "speak_ko": f"{place}에 {target.name} 폴더가 이미 있습니다, sir."})
    return ok(f"I've created the folder {target.name} in {place}, sir.",
              {"path": str(target), "speak_ko": f"{place}에 {target.name} 폴더를 만들었습니다, sir."})


def open_folder(folder: str = "", **_) -> dict:
    """Show a folder in File Explorer: a known one (downloads, documents, ...) or a path."""
    from .base import IS_WINDOWS
    key = (folder or "").lower().strip()
    if not key:
        return fail("Which folder, sir?")
    shell_targets = {"recycle bin": "shell:RecycleBinFolder", "trash": "shell:RecycleBinFolder",
                     "this pc": "shell:MyComputerFolder", "my computer": "shell:MyComputerFolder"}
    label = _KO_FOLDER.get(key, folder)
    if key in shell_targets:
        if IS_WINDOWS:
            import subprocess
            subprocess.Popen(["explorer.exe", shell_targets[key]])
        return ok(f"Opening {folder}, sir.", {"folder": key, "speak_ko": f"{label}을(를) 엽니다, sir."})
    target = known_folder(key) or user_path(folder, "home")
    if not target.exists():
        return fail(f"I can't find the folder {folder}, sir.", f"not found: {target}")
    if IS_WINDOWS:
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as e:
            return fail(f"I couldn't open {folder}, sir.", str(e))
        return ok(f"Opening {target.name or folder}, sir.", {"path": str(target), "speak_ko": f"{label} 폴더를 엽니다, sir."})
    return ok(f"(Dev) Would open {target}, sir.", {"path": str(target)})


def empty_recycle_bin(**_) -> dict:
    from .base import IS_WINDOWS
    if not IS_WINDOWS:
        return ok("(Dev) The Recycle Bin would be emptied, sir.", {})
    try:
        import ctypes
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)
        if hr not in (0, -2147418113):              # E_UNEXPECTED: it was already empty
            raise OSError(f"SHEmptyRecycleBin returned {hr}")
        return ok("The Recycle Bin is empty, sir.", {"speak_ko": "휴지통을 비웠습니다, sir."})
    except Exception as e:
        return fail("I couldn't empty the Recycle Bin, sir.", str(e))
