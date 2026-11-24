"""
dev/files.py -- opening what you send: what a file really is, what is inside it, and the tools
to change it.

  detect(path)          the real type from the content (magic bytes), not just the extension
  inspect(path)         details + a plain summary for the coding model:
                          archives (zip/7z/rar/tar/gz, and zip-based docx/xlsx/pptx/jar/apk/epub)
                          Windows programs (.exe/.dll): 32/64-bit, console or window, .NET, PyInstaller,
                          Electron, Java launcher, Go/Rust/Delphi/AutoIt, installers (Inno, NSIS, MSI),
                          packers (UPX), version information
                          GBA ROMs: header (title, game code, maker, version, checksum)
                          images, audio/video, documents, text and code
  extract / repack      archives, in the same format they came in
  pyinstaller_extract   recovers the files of a PyInstaller-built .exe (Python bytecode + data)
  asar_extract / pack   Electron app archives
  gba_header / gba_fix_header      read and repair a cartridge header (title, code, checksum)
  ips_apply / ips_create / bps_apply / bps_create   ROM patches
  tiles_to_png / png_to_tiles      4-bit GBA tiles <-> an editable PNG

Compiled native code cannot be turned back into its original source; JARVIS says so and
works with what the format allows (resources, byte patches, a rewrite from what it observes).
Refused whatever is asked: removing copy protection, licence checks or anti-cheat.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
import zlib
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")

# ------------------------------------------------------------------ detection
_MAGIC = [
    (0, b"%PDF", "pdf"), (0, b"\x89PNG\r\n\x1a\n", "png"), (0, b"\xff\xd8\xff", "jpg"), (0, b"GIF8", "gif"),
    (0, b"BM", "bmp"), (0, b"\x00\x00\x01\x00", "ico"), (0, b"7z\xbc\xaf\x27\x1c", "7z"), (0, b"Rar!\x1a\x07", "rar"),
    (0, b"\x1f\x8b", "gz"), (0, b"BZh", "bz2"), (0, b"\xfd7zXZ\x00", "xz"), (0, b"\x7fELF", "elf"),
    (0, b"\xcf\xfa\xed\xfe", "macho"), (0, b"\xca\xfe\xba\xbe", "class-or-macho"), (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"), (0, b"ID3", "mp3"), (0, b"\x1a\x45\xdf\xa3", "mkv"), (0, b"glTF", "glb"),
    (0, b"wOFF", "woff"), (0, b"wOF2", "woff2"), (0, b"OTTO", "otf"), (0, b"\x00\x01\x00\x00\x00", "ttf"),
    (0, b"SQLite format 3\x00", "sqlite"), (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"), (0, b"NES\x1a", "nes"),
    (0, b"MThd", "midi"), (0, b"8BPS", "psd"), (0, b"\x00asm", "wasm"), (0, b"PATCH", "ips"), (0, b"BPS1", "bps"),
]
_TEXT_EXT = {".txt", ".md", ".json", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".xml", ".html", ".htm",
             ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
             ".rs", ".rb", ".php", ".lua", ".sh", ".bat", ".ps1", ".csv", ".tsv", ".sql", ".svg", ".log", ".gd",
             ".kt", ".swift", ".dart", ".vue", ".svelte", ".reg", ".srt", ".vtt", ".tex", ".rst", ".properties"}
_ALIASES = {"jpeg": "jpg", "htm": "html", "yml": "yaml", "tif": "tiff", "dll": "exe", "mid": "midi", "markdown": "md",
            "tgz": "gz", "text": "txt"}


def _head(path: str, n: int = 4096) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def detect(path: str) -> dict:
    """{"kind", "ext", "bytes", "text"}: kind is the real format ("zip", "docx", "exe", "gba", "png", ...)."""
    ext = os.path.splitext(path)[1].lower()
    if os.path.isdir(path):
        return {"kind": "folder", "ext": "", "bytes": 0, "text": False}
    size = os.path.getsize(path)
    head = _head(path, 4096)
    kind = None
    for off, magic, k in _MAGIC:
        if head[off:off + len(magic)] == magic:
            kind = k
            break
    if head[:4] == b"RIFF" and len(head) >= 12:
        kind = {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(head[8:12], "riff")
    elif head[4:8] == b"ftyp":
        brand = head[8:12]
        kind = "heic" if brand in (b"heic", b"heix", b"mif1") else ("m4a" if brand.startswith(b"M4A") else "mp4")
    elif head[:2] == b"MZ":
        kind = "exe"
    elif len(head) >= 262 and head[257:262] == b"ustar":
        kind = "tar"
    elif head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        kind = "mp3"
    elif head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        kind = _zip_kind(path)
    if kind == "class-or-macho":
        kind = "class" if ext == ".class" else "macho"
    if kind == "ole":
        kind = {".doc": "doc", ".xls": "xls", ".ppt": "ppt", ".msi": "msi"}.get(ext, "ole")
    if kind is None and ext == ".gba" or (kind is None and size >= 0xC0 and head[0xB2:0xB3] == b"\x96" and ext in (".gba", ".agb", ".bin", ".mb")):
        kind = "gba"
    if kind is None and ext in (".nds",):
        kind = "nds"
    text = False
    if kind is None:
        if b"\x00" not in head[:2048]:
            try:
                head.decode("utf-8")
                text = True
            except UnicodeDecodeError:
                try:
                    head[:-4].decode("utf-8")
                    text = True
                except UnicodeDecodeError:
                    text = ext in _TEXT_EXT
        kind = (ext.lstrip(".") or "txt") if text else (ext.lstrip(".") or "binary")
        if text and kind in ("htm",):
            kind = "html"
    return {"kind": kind, "ext": ext, "bytes": size, "text": text}


def _zip_kind(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "mimetype" in names:
                mt = z.read("mimetype").decode("ascii", "replace").strip()
                return {"application/epub+zip": "epub", "application/vnd.oasis.opendocument.text": "odt",
                        "application/vnd.oasis.opendocument.spreadsheet": "ods",
                        "application/vnd.oasis.opendocument.presentation": "odp"}.get(mt, "zip")
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return "docx"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx"
                if any(n.startswith("ppt/") for n in names):
                    return "pptx"
            if "AndroidManifest.xml" in names:
                return "apk"
            if "META-INF/MANIFEST.MF" in names and any(n.endswith(".class") for n in names):
                return "jar"
    except zipfile.BadZipFile:
        return "zip-damaged"
    return "zip"


def format_matches(info: dict, fmt: str) -> bool:
    want = _ALIASES.get(fmt.lower().lstrip("."), fmt.lower().lstrip("."))
    got = _ALIASES.get(str(info.get("kind", "")).lower(), str(info.get("kind", "")).lower())
    if want == got:
        return True
    if want in ("txt", "csv", "json", "ini", "md", "html", "xml", "yaml", "toml", "svg", "py", "js", "css"):
        return bool(info.get("text")) and (got == want or info.get("ext", "").lstrip(".") in (want, fmt.lower()))
    if want == "zip" and got in ("docx", "xlsx", "pptx", "jar", "apk", "epub", "odt", "ods", "odp"):
        return True
    if want == "mp4" and got in ("m4a",):
        return False
    return False


# ------------------------------------------------------------------ inspection
def inspect(path: str) -> dict:
    """Everything worth knowing about a file, with a plain summary for the coding model."""
    info = detect(path)
    k = info["kind"]
    name = os.path.basename(path)
    details: dict = {}
    try:
        if k in ("zip", "docx", "xlsx", "pptx", "jar", "apk", "epub", "odt", "ods", "odp"):
            details = _zip_details(path)
        elif k in ("tar", "gz", "bz2", "xz"):
            details = _tar_details(path)
        elif k in ("7z", "rar"):
            details = _sevenzip_list(path)
        elif k == "exe":
            details = pe_info(path)
        elif k == "gba":
            details = gba_header(path)
        elif k in ("png", "jpg", "gif", "bmp", "webp", "ico", "tiff"):
            details = _image_details(path)
        elif k == "folder":
            details = _folder_details(path)
        elif info["text"]:
            details = _text_details(path)
    except Exception as e:
        details = {"error": f"{type(e).__name__}: {e}"}
    info["details"] = details
    info["summary"] = _summary(name, info)
    return info


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def _zip_details(path: str) -> dict:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        return {"files": len(infos), "unpacked_bytes": sum(i.file_size for i in infos),
                "names": [i.filename for i in infos[:60]], "encrypted": any(i.flag_bits & 0x1 for i in infos)}


def _tar_details(path: str) -> dict:
    try:
        with tarfile.open(path) as t:
            members = t.getmembers()
            return {"files": len(members), "names": [m.name for m in members[:60]],
                    "unpacked_bytes": sum(m.size for m in members)}
    except tarfile.ReadError:
        return {"files": 1, "note": "a single compressed file (not a tar archive)"}


def sevenzip() -> Optional[str]:
    for name in ("7z", "7za", "7zz"):
        p = shutil.which(name)
        if p:
            return p
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            p = os.path.join(base, "7-Zip", "7z.exe") if base else ""
            if p and os.path.exists(p):
                return p
    return None


def _sevenzip_list(path: str) -> dict:
    exe = sevenzip()
    if not exe:
        return {"note": "7-Zip is needed to look inside (JARVIS can install it)"}
    r = subprocess.run([exe, "l", "-ba", path], capture_output=True, text=True, timeout=120)
    names = [ln[53:].strip() for ln in r.stdout.splitlines() if len(ln) > 53]
    return {"files": len(names), "names": names[:60]}


def _image_details(path: str) -> dict:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"width": im.width, "height": im.height, "mode": im.mode, "frames": getattr(im, "n_frames", 1)}
    except Exception as e:
        return {"note": f"could not open the image: {e}"}


def _folder_details(path: str) -> dict:
    files = []
    for base, dirs, fnames in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for f in fnames:
            files.append(os.path.relpath(os.path.join(base, f), path).replace("\\", "/"))
            if len(files) >= 300:
                break
    return {"files": len(files), "names": files[:80]}


def _text_details(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read(200_000)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return {"lines": len(lines), "preview": "\n".join(lines[:25])[:2000]}


def _summary(name: str, info: dict) -> str:
    d, k = info.get("details") or {}, info["kind"]
    parts = [f"{name}: {k.upper()} file, {_human(info['bytes'])}"]
    if d.get("error"):
        parts.append(f"(could not read it fully: {d['error']})")
    if k == "exe":
        parts.append(d.get("description", ""))
        if d.get("recoverable"):
            parts.append("Source can be recovered: " + d["recoverable"])
        elif d.get("native"):
            parts.append("Native machine code: the original source cannot be recovered; it can be inspected, its "
                         "resources and bytes patched, or the program rewritten from its observed behaviour.")
    elif k == "gba":
        if d.get("ok") is not None:
            parts.append(f"GBA cartridge '{d.get('title')}' code {d.get('code')} maker {d.get('maker')} "
                         f"v{d.get('version')}; header checksum {'OK' if d.get('ok') else 'WRONG'}.")
    elif "names" in d:
        listed = ", ".join(d["names"][:25])
        parts.append(f"{d.get('files')} files inside: {listed}" + (" ..." if d.get("files", 0) > 25 else ""))
        if d.get("encrypted"):
            parts.append("Some entries are password-protected.")
    elif "width" in d:
        parts.append(f"{d['width']}x{d['height']} {d.get('mode', '')}")
    elif "lines" in d:
        parts.append(f"{d['lines']} lines; it begins:\n{d['preview'][:800]}")
    return " ".join(p for p in parts if p)


# ------------------------------------------------------------------ Windows programs (PE)
_MACHINE = {0x14c: "x86 (32-bit)", 0x8664: "x64 (64-bit)", 0xaa64: "ARM64", 0x1c4: "ARM"}
_SUBSYS = {2: "window (GUI)", 3: "console", 9: "Windows CE", 10: "EFI", 16: "boot"}


def pe_info(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read(min(os.path.getsize(path), 64 * 1024 * 1024))
    if data[:2] != b"MZ" or len(data) < 0x40:
        return {"error": "not a Windows program"}
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\x00\x00":
        return {"description": "an old DOS program (no PE header)", "native": True}
    machine, nsect, stamp, _, _, opt_size, chars = struct.unpack_from("<HHIIIHH", data, pe + 4)
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    is64 = magic == 0x20B
    subsystem = struct.unpack_from("<H", data, opt + 68)[0]
    dd_off = opt + (112 if is64 else 96)
    ndd = struct.unpack_from("<I", data, dd_off - 4)[0]
    dirs = [struct.unpack_from("<II", data, dd_off + 8 * i) for i in range(min(ndd, 16))]
    sec_off = opt + opt_size
    sections = []
    for i in range(nsect):
        o = sec_off + 40 * i
        nm = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, o + 8)
        sections.append({"name": nm, "size": rsize, "offset": rptr})
    dll = bool(chars & 0x2000)
    dotnet = len(dirs) > 14 and dirs[14][0] != 0
    info = {"machine": _MACHINE.get(machine, hex(machine)), "subsystem": _SUBSYS.get(subsystem, str(subsystem)),
            "dll": dll, "dotnet": dotnet, "sections": [s["name"] for s in sections], "timestamp": stamp}
    blob = data
    tail = data[-4096:] if len(data) > 4096 else data
    found: List[str] = []
    if dotnet:
        found.append(".NET")
    if b"MEI\x0c\x0b\x0a\x0b\x0e" in _tail(path, 8192) or b"pyi-runtime-tmpdir" in blob or b"PyInstaller" in blob:
        found.append("PyInstaller (Python)")
    if b"Electron" in blob and (b"app.asar" in blob or b"electron.asar" in blob):
        found.append("Electron")
    if b"launch4j" in blob.lower() or (b"PK\x03\x04" in tail and b"META-INF" in blob):
        found.append("Java (wrapped jar)")
    if b"Go build ID" in blob or b"runtime.gopanic" in blob:
        found.append("Go")
    if b"rust_panic" in blob or b"/rustc/" in blob:
        found.append("Rust")
    if b"Borland" in blob or b"Embarcadero" in blob or any(s["name"] in ("CODE", "DATA") for s in sections):
        found.append("Delphi")
    if b"AU3!" in blob or b"AutoIt" in blob:
        found.append("AutoIt script")
    if b"Inno Setup" in blob:
        found.append("Inno Setup installer")
    if b"Nullsoft" in blob or b"NSIS" in blob:
        found.append("NSIS installer")
    if any(s["name"].startswith("UPX") for s in sections):
        found.append("UPX-packed")
    if b"Qt5Core" in blob or b"Qt6Core" in blob:
        found.append("Qt")
    if b"UnityPlayer" in blob or b"UnityEngine" in blob:
        found.append("Unity game")
    if b"Godot" in blob:
        found.append("Godot")
    info["built_with"] = found
    info["version"] = _version_strings(blob)
    rec = ""
    if dotnet:
        rec = ".NET: decompile to C# with ILSpy (ilspycmd), change, and rebuild with the .NET SDK"
    elif "PyInstaller (Python)" in found:
        rec = "PyInstaller: extract the bundled Python files (jarvis-tool pyinstaller-extract), recover or rewrite the scripts, rebuild with PyInstaller"
    elif "Electron" in found:
        rec = "Electron: the app's JavaScript is in resources/app.asar next to the .exe (jarvis-tool asar-extract)"
    elif "Java (wrapped jar)" in found:
        rec = "Java: the jar inside can be extracted and decompiled (CFR)"
    elif "AutoIt script" in found:
        rec = "AutoIt: the script can usually be extracted with an AutoIt decompiler"
    info["recoverable"] = rec
    info["native"] = not rec and not any(x.endswith("installer") for x in found)
    bits = "64-bit" if is64 else "32-bit"
    kind = "library (DLL)" if dll else f"{info['subsystem']} program"
    info["description"] = (f"A {bits} Windows {kind} for {info['machine']}" +
                           (f", built with {', '.join(found)}" if found else "") +
                           (f"; product: {info['version'].get('ProductName')}" if info["version"].get("ProductName") else "") + ".")
    return info


def _tail(path: str, n: int) -> bytes:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - n))
        return fh.read(n)


def _version_strings(blob: bytes) -> Dict[str, str]:
    out = {}
    for key in ("CompanyName", "FileDescription", "FileVersion", "ProductName", "ProductVersion", "OriginalFilename",
                "LegalCopyright"):
        pat = key.encode("utf-16-le") + b"\x00\x00"
        i = blob.find(pat)
        if i < 0:
            continue
        j = i + len(pat)
        while j < len(blob) and blob[j:j + 2] == b"\x00\x00":
            j += 2
        end = blob.find(b"\x00\x00", j)
        while end > 0 and (end - j) % 2:
            end = blob.find(b"\x00\x00", end + 1)
        if end > j:
            val = blob[j:end].decode("utf-16-le", "replace").strip()
            if 0 < len(val) < 200:
                out[key] = val
    return out


# ------------------------------------------------------------------ archives
def extract(path: str, dest: str) -> List[str]:
    """Unpack an archive into dest; returns the files written."""
    os.makedirs(dest, exist_ok=True)
    k = detect(path)["kind"]
    if k in ("zip", "docx", "xlsx", "pptx", "jar", "apk", "epub", "odt", "ods", "odp"):
        with zipfile.ZipFile(path) as z:
            for m in z.infolist():
                target = os.path.realpath(os.path.join(dest, m.filename))
                if not target.startswith(os.path.realpath(dest)):
                    raise ValueError(f"unsafe path in archive: {m.filename}")
            z.extractall(dest)
    elif k in ("tar", "gz", "bz2", "xz"):
        try:
            with tarfile.open(path) as t:
                safe = [m for m in t.getmembers() if not (m.name.startswith("/") or ".." in m.name.split("/"))
                        and not m.issym() and not m.islnk()]
                t.extractall(dest, members=safe)
        except tarfile.ReadError:
            import gzip, bz2, lzma
            opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[k]
            out = os.path.join(dest, os.path.splitext(os.path.basename(path))[0])
            with opener(path, "rb") as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
    elif k in ("7z", "rar", "msi", "exe"):
        exe = sevenzip()
        if not exe:
            raise FileNotFoundError("7-Zip is needed to extract this (JARVIS can install it)")
        r = subprocess.run([exe, "x", "-y", f"-o{dest}", path], capture_output=True, text=True, timeout=1800)
        if r.returncode not in (0, 1):
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    else:
        raise ValueError(f"{os.path.basename(path)} is not an archive JARVIS can extract ({k})")
    files = []
    for base, _d, fnames in os.walk(dest):
        for f in fnames:
            files.append(os.path.join(base, f))
    return files


def repack(src_dir: str, dest: str, fmt: str = "") -> str:
    """Pack a folder as zip / tar / tar.gz / 7z (the format from dest's extension if fmt is empty)."""
    fmt = (fmt or "").lower() or ("tar.gz" if dest.lower().endswith((".tar.gz", ".tgz")) else
                                   os.path.splitext(dest)[1].lower().lstrip("."))
    if fmt in ("zip", "docx", "xlsx", "pptx", "jar", "apk", "epub"):
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            if fmt == "epub" and os.path.exists(os.path.join(src_dir, "mimetype")):
                z.write(os.path.join(src_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
            for base, _d, fnames in os.walk(src_dir):
                for f in sorted(fnames):
                    full = os.path.join(base, f)
                    rel = os.path.relpath(full, src_dir).replace("\\", "/")
                    if fmt == "epub" and rel == "mimetype":
                        continue
                    z.write(full, rel)
    elif fmt in ("tar", "tar.gz", "tgz", "gz", "bz2", "xz"):
        mode = {"tar": "w", "tar.gz": "w:gz", "tgz": "w:gz", "gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}[fmt]
        with tarfile.open(dest, mode) as t:
            for entry in sorted(os.listdir(src_dir)):
                t.add(os.path.join(src_dir, entry), entry)
    elif fmt == "7z":
        exe = sevenzip()
        if not exe:
            raise FileNotFoundError("7-Zip is needed to make a .7z archive")
        r = subprocess.run([exe, "a", "-y", dest, os.path.join(src_dir, "*")], capture_output=True, text=True,
                           timeout=1800)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    else:
        raise ValueError(f"cannot pack as {fmt}")
    return dest


# ------------------------------------------------------------------ PyInstaller
_MEI = b"MEI\x0c\x0b\x0a\x0b\x0e"


def pyinstaller_extract(path: str, dest: str) -> dict:
    """Unpack the CArchive of a PyInstaller-built executable (the bundled files and the main
    scripts' bytecode). Returns {"files": [...], "scripts": [...], "python": "3.x"}."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        search = min(size, 1024 * 1024)
        fh.seek(size - search)
        tail = fh.read(search)
        i = tail.rfind(_MEI)
        if i < 0:
            raise ValueError("not a PyInstaller program")
        cookie_pos = size - search + i
        fh.seek(cookie_pos)
        cookie = fh.read(88)
        if len(cookie) >= 88:                       # PyInstaller >= 2.1: name of the Python DLL at the end
            magic, pkg_len, toc_off, toc_len, pyver = struct.unpack("!8sIIii", cookie[:24])
            cookie_size = 88
        else:
            magic, pkg_len, toc_off, toc_len, pyver = struct.unpack("!8sIIii", cookie[:24])
            cookie_size = 24
        overlay = cookie_pos + cookie_size - pkg_len
        fh.seek(overlay + toc_off)
        toc = fh.read(toc_len)
        files, scripts = [], []
        p = 0
        os.makedirs(dest, exist_ok=True)
        while p < len(toc):
            (entry_size,) = struct.unpack("!i", toc[p:p + 4])
            pos, csize, usize, cflag, typ = struct.unpack("!IIIBc", toc[p + 4:p + 18])
            name = toc[p + 18:p + entry_size].rstrip(b"\x00").decode("utf-8", "replace")
            p += entry_size
            fh.seek(overlay + pos)
            blob = fh.read(csize)
            if cflag:
                try:
                    blob = zlib.decompress(blob)
                except zlib.error:
                    pass
            safe = name.replace("..", "_").lstrip("/\\") or "unnamed"
            if typ == b"s":
                safe += ".pyc.code"                   # a marshalled code object (the main scripts)
                scripts.append(safe)
            out = os.path.join(dest, safe)
            os.makedirs(os.path.dirname(out) or dest, exist_ok=True)
            with open(out, "wb") as o:
                o.write(blob)
            files.append(safe)
    ver = f"{pyver // 100}.{pyver % 100}" if pyver >= 100 else f"{pyver // 10}.{pyver % 10}"
    return {"files": files, "scripts": scripts, "python": ver}


# ------------------------------------------------------------------ Electron .asar
def asar_extract(path: str, dest: str) -> List[str]:
    with open(path, "rb") as fh:
        data = fh.read()
    header_size = struct.unpack_from("<I", data, 4)[0]
    json_len = struct.unpack_from("<I", data, 12)[0]
    header = json.loads(data[16:16 + json_len].decode("utf-8"))
    base = 8 + header_size
    out: List[str] = []

    def walk(node: dict, rel: str):
        for name, entry in (node.get("files") or {}).items():
            child = os.path.join(rel, name)
            if "files" in entry:
                walk(entry, child)
            elif entry.get("unpacked"):
                continue                               # lives in app.asar.unpacked next to the archive
            else:
                off, size = int(entry["offset"]), int(entry["size"])
                target = os.path.join(dest, child)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as o:
                    o.write(data[base + off: base + off + size])
                out.append(child.replace("\\", "/"))
    walk(header, "")
    return out


def asar_pack(src_dir: str, dest: str) -> str:
    chunks: List[bytes] = []
    offset = 0

    def build(folder: str) -> dict:
        nonlocal offset
        node: Dict[str, dict] = {}
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if os.path.isdir(full):
                node[name] = {"files": build(full)}
            else:
                with open(full, "rb") as fh:
                    blob = fh.read()
                node[name] = {"size": len(blob), "offset": str(offset)}
                chunks.append(blob)
                offset += len(blob)
        return node
    header = json.dumps({"files": build(src_dir)}, separators=(",", ":")).encode("utf-8")
    padded = header + b"\x00" * ((4 - len(header) % 4) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(header)) + padded       # Chromium pickle: payload size, string
    with open(dest, "wb") as fh:
        fh.write(struct.pack("<II", 4, len(pickle)))
        fh.write(pickle)
        for c in chunks:
            fh.write(c)
    return dest


# ------------------------------------------------------------------ GBA
def _gba_complement(rom: bytes) -> int:
    chk = 0
    for b in rom[0xA0:0xBD]:
        chk = (chk - b) & 0xFF
    return (chk - 0x19) & 0xFF


def gba_header(path: str) -> dict:
    with open(path, "rb") as fh:
        rom = fh.read(0x200)
    if len(rom) < 0xC0:
        return {"ok": False, "problem": "too small to be a GBA cartridge (no 192-byte header)"}
    title = rom[0xA0:0xAC].rstrip(b"\x00").decode("ascii", "replace")
    code = rom[0xAC:0xB0].decode("ascii", "replace")
    maker = rom[0xB0:0xB2].decode("ascii", "replace")
    fixed, version, stored = rom[0xB2], rom[0xBC], rom[0xBD]
    computed = _gba_complement(rom)
    problems = []
    if fixed != 0x96:
        problems.append("the fixed byte at 0xB2 must be 0x96")
    if stored != computed:
        problems.append(f"header checksum is 0x{stored:02X}, should be 0x{computed:02X}")
    return {"ok": not problems, "title": title, "code": code, "maker": maker, "version": version,
            "checksum": stored, "computed": computed, "bytes": os.path.getsize(path),
            "problem": "; ".join(problems)}


def gba_fix_header(path: str, title: Optional[str] = None, code: Optional[str] = None,
                   maker: Optional[str] = None, version: Optional[int] = None) -> dict:
    """Set title (12 chars), game code (4), maker (2), version, the fixed byte, and recompute
    the complement checksum -- what a cartridge needs to boot. (The Nintendo logo area is left
    as it is: JARVIS does not ship that bitmap; emulators run ROMs without it.)"""
    with open(path, "r+b") as fh:
        rom = bytearray(fh.read())
        if len(rom) < 0xC0:
            rom.extend(b"\x00" * (0xC0 - len(rom)))
        if title is not None:
            rom[0xA0:0xAC] = title.upper().encode("ascii", "replace")[:12].ljust(12, b"\x00")
        if code is not None:
            rom[0xAC:0xB0] = code.upper().encode("ascii", "replace")[:4].ljust(4, b"0")
        if maker is not None:
            rom[0xB0:0xB2] = maker.upper().encode("ascii", "replace")[:2].ljust(2, b"0")
        if version is not None:
            rom[0xBC] = int(version) & 0xFF
        rom[0xB2] = 0x96
        rom[0xBD] = _gba_complement(rom)
        fh.seek(0)
        fh.write(rom)
        fh.truncate()
    return gba_header(path)


def gba_screenshot(rom: str, out_png: str, frames: int = 300) -> dict:
    """Run the ROM in mGBA for a few seconds and save what it shows (when mGBA is installed)."""
    exe = shutil.which("mgba-qt") or shutil.which("mgba")
    if not exe and IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            p = os.path.join(base, "mGBA", "mGBA.exe") if base else ""
            if p and os.path.exists(p):
                exe = p
    if not exe:
        return {"path": None, "skipped": "mGBA is not installed"}
    script = out_png + ".lua"
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(f"local n = 0\ncallbacks:add('frame', function()\n n = n + 1\n if n == {frames} then "
                 f"emu:screenshot('{out_png.replace(chr(92), '/')}') os.exit(0) end\nend)\n")
    try:
        subprocess.run([exe, "--script", script, rom], capture_output=True, timeout=60)
    except Exception as e:
        return {"path": None, "skipped": f"mGBA could not run it: {e}"}
    if not os.path.exists(out_png):
        return {"path": None, "skipped": "mGBA did not save a picture"}
    blank = None
    try:
        from PIL import Image, ImageStat
        with Image.open(out_png) as im:
            blank = max(ImageStat.Stat(im.convert("RGB")).stddev) < 2.0
    except Exception:
        pass
    return {"path": out_png, "blank": blank}


# ------------------------------------------------------------------ patches
def ips_apply(rom: bytes, patch: bytes) -> bytes:
    if patch[:5] != b"PATCH":
        raise ValueError("not an IPS patch")
    out = bytearray(rom)
    p = 5
    while p + 3 <= len(patch):
        if patch[p:p + 3] == b"EOF":
            p += 3
            if p + 3 <= len(patch):                    # optional truncation
                out = out[:int.from_bytes(patch[p:p + 3], "big")]
            break
        off = int.from_bytes(patch[p:p + 3], "big")
        size = int.from_bytes(patch[p + 3:p + 5], "big")
        p += 5
        if size == 0:                                   # run-length record
            run = int.from_bytes(patch[p:p + 2], "big")
            val = patch[p + 2]
            p += 3
            data = bytes([val]) * run
        else:
            data = patch[p:p + size]
            p += size
        if off + len(data) > len(out):
            out.extend(b"\x00" * (off + len(data) - len(out)))
        out[off:off + len(data)] = data
    return bytes(out)


def ips_create(original: bytes, modified: bytes) -> bytes:
    if len(modified) > 0x1000000:
        raise ValueError("IPS patches can only address the first 16 MB (use BPS)")
    out = bytearray(b"PATCH")
    i, n = 0, len(modified)
    while i < n:
        if i < len(original) and original[i] == modified[i]:
            i += 1
            continue
        start = i
        while i < n and (i >= len(original) or original[i] != modified[i]) and i - start < 0xFFFF:
            i += 1
        if start == 0x454F46:                           # "EOF" as an offset would end the patch early
            start -= 1
        chunk = modified[start:i]
        out += start.to_bytes(3, "big") + len(chunk).to_bytes(2, "big") + chunk
    out += b"EOF"
    if len(modified) < len(original):
        out += len(modified).to_bytes(3, "big")
    return bytes(out)


def _bps_read(data: bytes, p: int) -> Tuple[int, int]:
    value, shift = 0, 1
    while True:
        x = data[p]
        p += 1
        value += (x & 0x7F) * shift
        if x & 0x80:
            return value, p
        shift <<= 7
        value += shift


def _bps_write(n: int) -> bytes:
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        n -= 1


def bps_apply(source: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"BPS1":
        raise ValueError("not a BPS patch")
    src_crc, tgt_crc, patch_crc = struct.unpack("<III", patch[-12:])
    if zlib.crc32(patch[:-4]) & 0xFFFFFFFF != patch_crc:
        raise ValueError("the patch file is damaged (checksum)")
    if zlib.crc32(source) & 0xFFFFFFFF != src_crc:
        raise ValueError("this patch is for a different file (source checksum does not match)")
    p = 4
    src_size, p = _bps_read(patch, p)
    tgt_size, p = _bps_read(patch, p)
    meta, p = _bps_read(patch, p)
    p += meta
    out = bytearray()
    src_rel = tgt_rel = 0
    end = len(patch) - 12
    while p < end:
        data, p = _bps_read(patch, p)
        cmd, length = data & 3, (data >> 2) + 1
        if cmd == 0:                                   # SourceRead
            out += source[len(out):len(out) + length]
        elif cmd == 1:                                 # TargetRead
            out += patch[p:p + length]
            p += length
        else:
            off, p = _bps_read(patch, p)
            off = (-1 if off & 1 else 1) * (off >> 1)
            if cmd == 2:                               # SourceCopy
                src_rel += off
                out += source[src_rel:src_rel + length]
                src_rel += length
            else:                                      # TargetCopy (may overlap)
                tgt_rel += off
                for _ in range(length):
                    out.append(out[tgt_rel])
                    tgt_rel += 1
    if len(out) != tgt_size or zlib.crc32(bytes(out)) & 0xFFFFFFFF != tgt_crc:
        raise ValueError("the patched result is not what the patch expects")
    return bytes(out)


def bps_create(source: bytes, target: bytes) -> bytes:
    """A simple, valid BPS patch: unchanged runs are read from the source, the rest is stored."""
    out = bytearray(b"BPS1") + _bps_write(len(source)) + _bps_write(len(target)) + _bps_write(0)
    i, n = 0, len(target)
    while i < n:
        same = 0
        while i + same < n and i + same < len(source) and source[i + same] == target[i + same]:
            same += 1
        if same >= 4 or (same and i + same == n):
            out += _bps_write(((same - 1) << 2) | 0)
            i += same
            continue
        start = i
        while i < n:
            run = 0
            while i + run < n and i + run < len(source) and source[i + run] == target[i + run] and run < 4:
                run += 1
            if run >= 4:
                break
            i += max(1, run)
        chunk = target[start:i]
        out += _bps_write(((len(chunk) - 1) << 2) | 1) + chunk
    out += struct.pack("<II", zlib.crc32(source) & 0xFFFFFFFF, zlib.crc32(target) & 0xFFFFFFFF)
    out += struct.pack("<I", zlib.crc32(bytes(out)) & 0xFFFFFFFF)
    return bytes(out)


# ------------------------------------------------------------------ 4bpp tiles
def tiles_to_png(data: bytes, out_png: str, per_row: int = 16, palette: Optional[List[Tuple[int, int, int]]] = None) -> str:
    """GBA 4-bit tiles (8x8, 32 bytes each) -> an indexed PNG to edit in any paint program."""
    from PIL import Image
    n = len(data) // 32
    rows = max(1, (n + per_row - 1) // per_row)
    img = Image.new("P", (per_row * 8, rows * 8), 0)
    pal = palette or [(i * 17, i * 17, i * 17) for i in range(16)]
    flat = [c for rgb in pal for c in rgb] + [0] * (768 - 3 * len(pal))
    img.putpalette(flat)
    px = img.load()
    for t in range(n):
        tx, ty = (t % per_row) * 8, (t // per_row) * 8
        for y in range(8):
            for x in range(4):
                b = data[t * 32 + y * 4 + x]
                px[tx + x * 2, ty + y] = b & 0x0F
                px[tx + x * 2 + 1, ty + y] = b >> 4
    img.save(out_png)
    return out_png


def png_to_tiles(png: str) -> bytes:
    from PIL import Image
    with Image.open(png) as im:
        if im.mode != "P":
            im = im.convert("L").point(lambda v: v // 17).convert("P")
        w, h = im.size
        px = im.load()
        out = bytearray()
        for ty in range(0, h - h % 8, 8):
            for tx in range(0, w - w % 8, 8):
                for y in range(8):
                    for x in range(0, 8, 2):
                        out.append((px[tx + x, ty + y] & 0x0F) | ((px[tx + x + 1, ty + y] & 0x0F) << 4))
        return bytes(out)


# ------------------------------------------------------------------ refusals
PROTECTION = re.compile(r"\b(crack|keygen|serial key|licen[cs]e (check|key|bypass)|activation|drm|denuvo|"
                        r"anti-?cheat|bypass (the )?(protection|license|licence|activation|paywall)|remove (the )?"
                        r"(protection|watermark|licen[cs]e)|pirat|warez|cheat engine|aimbot|wallhack|"
                        r"ransomware|keylogger|steal (passwords|credentials|cookies)|malware|virus|trojan|botnet|"
                        r"disable (antivirus|defender)|undetectable)\b", re.IGNORECASE)


def refused(request: str) -> Optional[str]:
    """Why JARVIS will not do a request (copy protection, licence checks, cheats, malware) -- or None."""
    m = PROTECTION.search(request or "")
    if not m:
        return None
    return ("I won't do that part, sir: removing copy protection, licence or activation checks, anti-cheat, or "
            "building malware or tools to break into systems. I'm glad to change, fix or improve the program in "
            "every other way.")
