"""
dev/convert.py -- changing a file's format ("convert this .png to .ico", ".ini to .exe").

  images     png jpg bmp gif webp ico tiff tga ppm -> png jpg bmp gif webp ico tiff pdf     (Pillow)
  data       json <-> yaml, csv, xlsx, ini, toml, xml                                       (stdlib, PyYAML, openpyxl)
  documents  md html docx odt epub rst tex ... (Pandoc); office -> pdf (LibreOffice);
             html / md -> pdf (the browser engine JARVIS already has)
  media      any audio / video -> any audio / video, video -> gif                          (FFmpeg)
  archives   zip <-> tar.gz / tar / 7z                                                     (stdlib, 7-Zip)
  3D         obj stl ply glb gltf off                                                       (trimesh)
  fonts      ttf otf <-> woff woff2                                                         (fontTools)
  -> exe     .py: built with PyInstaller. Scripts (.bat .cmd .ps1 .js .vbs): an executable that runs
             the script inside it. Anything else (.ini .json .txt ...): an executable built AROUND the
             file -- a window that shows it, lets you copy it and save it back out.

A pair not listed here is handed to the coding loop, which writes a converter and checks that
the output really is the new format. Tools that are missing are installed only after you agree.
"""
from __future__ import annotations

import configparser
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, Optional

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
IMAGE_IN = {"png", "jpg", "jpeg", "bmp", "gif", "webp", "ico", "tif", "tiff", "tga", "ppm", "pgm", "pcx", "dds", "icns"}
IMAGE_OUT = {"png", "jpg", "jpeg", "bmp", "gif", "webp", "ico", "tif", "tiff", "pdf", "ppm", "tga", "icns"}
DATA = {"json", "yaml", "yml", "csv", "tsv", "xlsx", "ini", "toml", "xml"}
DOCS = {"md", "markdown", "html", "htm", "docx", "odt", "epub", "rst", "tex", "latex", "rtf", "txt", "org", "textile"}
OFFICE = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf"}
MEDIA = {"mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma", "mp4", "mkv", "webm", "mov", "avi", "wmv", "flv",
         "m4v", "mpg", "mpeg", "3gp", "gif"}
ARCHIVES = {"zip", "tar", "gz", "tgz", "7z", "bz2", "xz"}
MESH = {"obj", "stl", "ply", "glb", "gltf", "off"}
FONTS = {"ttf", "otf", "woff", "woff2"}
SCRIPTS = {"bat", "cmd", "ps1", "js", "vbs", "sh"}
TOOLS_DIR = os.path.join(os.path.expanduser("~"), ".jarvis_tools")
AskFn = Callable[..., bool]


def _ext(path: str) -> str:
    p = path.lower()
    if p.endswith(".tar.gz"):
        return "tgz"
    return os.path.splitext(p)[1].lstrip(".")


def route(src: str, dest: str) -> Optional[str]:
    """Which converter handles src -> dest (None: the coding loop builds one)."""
    a, b = _ext(src), _ext(dest)
    if b in ("exe", "") and (b == "exe" or dest.endswith(".bin")):
        return "exe"
    if a in IMAGE_IN and b in IMAGE_OUT:
        return "image"
    if a in ("html", "htm", "md", "markdown") and b == "pdf":
        return "browser_pdf"
    if a in DATA and b in DATA:
        return "data"
    if a in OFFICE and b == "pdf":
        return "office"
    if a in MEDIA and b in MEDIA and not (a == "gif" and b in IMAGE_OUT):
        return "media"
    if a in ARCHIVES and b in ARCHIVES:
        return "archive"
    if a in MESH and b in MESH:
        return "mesh"
    if a in FONTS and b in FONTS:
        return "font"
    if a in DOCS and b in DOCS:
        return "pandoc"
    if a in ("txt", "csv", "json", "md") and b == "pdf":
        return "browser_pdf"
    return None


def convert(src: str, dest: str, ask: Optional[AskFn] = None, log: Optional[Callable[[str], None]] = None) -> dict:
    """{"ok", "path", "message", "tool"} -- or {"ok": False, "fallback": True} for the coding loop."""
    ask = ask or (lambda *a, **k: False)
    log = log or (lambda m: None)
    if not os.path.exists(src):
        return {"ok": False, "message": f"{src} does not exist"}
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    how = route(src, dest)
    if how is None:
        return {"ok": False, "fallback": True,
                "message": f"no ready-made converter from .{_ext(src)} to .{_ext(dest)}: the coding loop will build one"}
    fn = {"image": _image, "data": _data, "browser_pdf": _browser_pdf, "office": _office, "media": _media,
          "archive": _archive, "mesh": _mesh, "font": _font, "pandoc": _pandoc, "exe": _exe}[how]
    tried = set()
    while True:
        try:
            msg = fn(src, dest, ask, log)
            if not os.path.exists(dest) or (os.path.isfile(dest) and os.path.getsize(dest) == 0):
                return {"ok": False, "message": f"the {how} converter produced no output", "tool": how}
            return {"ok": True, "path": dest, "tool": how, "message": msg or f"converted to {os.path.basename(dest)}"}
        except _NeedsTool as e:
            # the missing piece is installed (you are asked first), then the conversion runs again
            if e.tool in tried:
                return {"ok": False, "tool": how, "message": str(e), "missing": e.tool}
            tried.add(e.tool)
            done = _install_missing(e.tool, ask, log)
            if done is not True:
                return {"ok": False, "tool": how, "message": f"{e} -- {done}", "missing": e.tool}
        except Exception as e:
            return {"ok": False, "tool": how, "message": f"{how} conversion failed: {type(e).__name__}: {e}",
                    "fallback": how in ("data", "pandoc")}


PIP_TOOLS = {"pyyaml": "pyyaml", "openpyxl": "openpyxl", "trimesh": "trimesh", "fonttools": "fonttools"}


def _install_missing(tool: str, ask: AskFn, log: Callable[[str], None]):
    """True when the tool is now there; otherwise what happened, in words."""
    if tool in PIP_TOOLS:
        pkg = PIP_TOOLS[tool]
        if not ask("install_software", f"Install the Python package {pkg} into JARVIS (for this conversion)", risk=0.4):
            return f"installing {pkg} was not allowed"
        log(f"installing {pkg}")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg], capture_output=True, text=True,
                           timeout=1800, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return f"installing {pkg} failed: {(r.stderr or r.stdout).strip()[-300:]}"
        import importlib
        importlib.invalidate_caches()
        return True
    from .toolchains import install
    said = install(tool, ask, log)
    return True if said.startswith("installed") or said.endswith("already installed") else said


class _NeedsTool(RuntimeError):
    def __init__(self, tool: str, msg: str):
        super().__init__(msg)
        self.tool = tool


# ------------------------------------------------------------------ images
def _image(src, dest, ask, log):
    from PIL import Image
    b = _ext(dest)
    with Image.open(src) as im:
        im.load()
        if b in ("jpg", "jpeg", "bmp", "pdf") and im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode not in ("RGB", "RGBA", "L", "P"):
            im = im.convert("RGBA")
        if b == "ico":
            sizes = [s for s in ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))
                     if s[0] <= max(im.size)] or [(32, 32)]
            im.convert("RGBA").save(dest, format="ICO", sizes=sizes)
            return f"made an icon with {len(sizes)} sizes ({sizes[0][0]} to {sizes[-1][0]} px)"
        fmt = {"jpg": "JPEG", "jpeg": "JPEG", "tif": "TIFF", "tiff": "TIFF"}.get(b, b.upper())
        im.save(dest, format=fmt, **({"quality": 92} if fmt == "JPEG" else {}))
    return None


# ------------------------------------------------------------------ data
def _load_data(src: str):
    a = _ext(src)
    text = None if a == "xlsx" else open(src, encoding="utf-8-sig").read()
    if a == "json":
        return json.loads(text)
    if a in ("yaml", "yml"):
        try:
            import yaml
        except ImportError:
            raise _NeedsTool("pyyaml", "reading YAML needs the PyYAML package")
        return yaml.safe_load(text)
    if a in ("csv", "tsv"):
        rows = list(csv.DictReader(io.StringIO(text), delimiter="\t" if a == "tsv" else ","))
        return rows
    if a == "ini":
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read_string(text if text.lstrip().startswith("[") else "[DEFAULT]\n" + text)
        out = {s: dict(cp.items(s, raw=True)) for s in cp.sections()}
        if cp.defaults():
            out = {"DEFAULT": dict(cp.defaults()), **out} if out else dict(cp.defaults())
        return out
    if a == "toml":
        import tomllib
        return tomllib.loads(text)
    if a == "xml":
        import xml.etree.ElementTree as ET
        return {ET.fromstring(text).tag: _xml_to_obj(ET.fromstring(text))}
    if a == "xlsx":
        try:
            import openpyxl
        except ImportError:
            raise _NeedsTool("openpyxl", "reading .xlsx needs the openpyxl package")
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        head = [str(h) if h is not None else f"col{i + 1}" for i, h in enumerate(rows[0])]
        return [{head[i]: v for i, v in enumerate(r) if i < len(head)} for r in rows[1:]]
    raise ValueError(f"cannot read .{a}")


def _xml_to_obj(el):
    kids = list(el)
    if not kids and not el.attrib:
        return (el.text or "").strip()
    out = {f"@{k}": v for k, v in el.attrib.items()}
    for k in kids:
        val = _xml_to_obj(k)
        if k.tag in out:
            if not isinstance(out[k.tag], list):
                out[k.tag] = [out[k.tag]]
            out[k.tag].append(val)
        else:
            out[k.tag] = val
    if (el.text or "").strip() and kids:
        out["#text"] = el.text.strip()
    return out


def _obj_to_xml(tag, obj, parent=None):
    import xml.etree.ElementTree as ET
    el = ET.Element(re.sub(r"[^\w.-]", "_", str(tag)) or "item") if parent is None else ET.SubElement(parent, re.sub(r"[^\w.-]", "_", str(tag)) or "item")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("@"):
                el.set(str(k)[1:], str(v))
            elif k == "#text":
                el.text = str(v)
            elif isinstance(v, list):
                for item in v:
                    _obj_to_xml(k, item, el)
            else:
                _obj_to_xml(k, v, el)
    elif isinstance(obj, list):
        for item in obj:
            _obj_to_xml("item", item, el)
    elif obj is not None:
        el.text = str(obj)
    return el


def _rows(obj):
    if isinstance(obj, list):
        return [r if isinstance(r, dict) else {"value": r} for r in obj]
    if isinstance(obj, dict):
        if all(isinstance(v, dict) for v in obj.values()):
            return [{"name": k, **v} for k, v in obj.items()]
        return [{"key": k, "value": v if not isinstance(v, (dict, list)) else json.dumps(v)} for k, v in obj.items()]
    return [{"value": obj}]


def _toml_dump(obj, prefix="") -> str:
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(str(v))
    lines, tables = [], []
    for k, v in (obj or {}).items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{json.dumps(k) if not re.match(r'^[A-Za-z0-9_-]+$', str(k)) else k} = {val(v)}")
    for k, v in tables:
        name = f"{prefix}.{k}" if prefix else str(k)
        lines.append(f"\n[{name}]")
        lines.append(_toml_dump(v, name))
    return "\n".join(l for l in lines if l is not None)


def _data(src, dest, ask, log):
    obj = _load_data(src)
    b = _ext(dest)
    if b == "json":
        open(dest, "w", encoding="utf-8").write(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    elif b in ("yaml", "yml"):
        try:
            import yaml
            text = yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)
        except ImportError:
            text = _yaml_dump(obj)
        open(dest, "w", encoding="utf-8").write(text)
    elif b in ("csv", "tsv"):
        rows = _rows(obj)
        head = list(dict.fromkeys(k for r in rows for k in r))
        with open(dest, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=head, delimiter="\t" if b == "tsv" else ",")
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    elif b == "ini":
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        data = obj if isinstance(obj, dict) else {"items": {str(i): json.dumps(v) for i, v in enumerate(obj)}}
        flat = {k: v for k, v in data.items() if not isinstance(v, dict)}
        if flat:
            cp["DEFAULT"] = {k: str(v) for k, v in flat.items()}
        for k, v in data.items():
            if isinstance(v, dict):
                cp[str(k)] = {kk: (json.dumps(vv) if isinstance(vv, (dict, list)) else str(vv)) for kk, vv in v.items()}
        with open(dest, "w", encoding="utf-8") as fh:
            cp.write(fh)
    elif b == "toml":
        data = obj if isinstance(obj, dict) else {"items": obj}
        open(dest, "w", encoding="utf-8").write(_toml_dump(data).strip() + "\n")
    elif b == "xml":
        import xml.etree.ElementTree as ET
        if isinstance(obj, dict) and len(obj) == 1:
            (tag, inner), = obj.items()
        else:
            tag, inner = "data", obj
        el = _obj_to_xml(tag, inner)
        ET.indent(el)
        open(dest, "w", encoding="utf-8").write('<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(el, encoding="unicode"))
    elif b == "xlsx":
        try:
            import openpyxl
        except ImportError:
            raise _NeedsTool("openpyxl", "writing .xlsx needs the openpyxl package")
        rows = _rows(obj)
        head = list(dict.fromkeys(k for r in rows for k in r))
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(head)
        for r in rows:
            ws.append([(json.dumps(r.get(k)) if isinstance(r.get(k), (dict, list)) else r.get(k)) for k in head])
        wb.save(dest)
    else:
        raise ValueError(f"cannot write .{b}")
    return None


def _yaml_dump(obj, indent=0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:\n{_yaml_dump(v, indent + 1)}")
            else:
                out.append(f"{pad}{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, (int, float)) or isinstance(v, bool) else v}")
        return "\n".join(out)
    if isinstance(obj, list):
        return "\n".join(f"{pad}- " + (_yaml_dump(v, indent + 1).lstrip() if isinstance(v, (dict, list)) else
                                        json.dumps(v, ensure_ascii=False)) for v in obj)
    return f"{pad}{json.dumps(obj, ensure_ascii=False)}"


# ------------------------------------------------------------------ documents
def _which(*names) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    if IS_WINDOWS:
        cands = {"soffice": [r"C:\Program Files\LibreOffice\program\soffice.exe"],
                 "pandoc": [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Pandoc", "pandoc.exe"),
                            r"C:\Program Files\Pandoc\pandoc.exe"],
                 "ffmpeg": [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "ffmpeg.exe")]}
        for n in names:
            for c in cands.get(n, []):
                if c and os.path.exists(c):
                    return c
    return None


def _pandoc(src, dest, ask, log):
    exe = _which("pandoc")
    a, b = _ext(src), _ext(dest)
    if not exe:
        if a in ("md", "markdown") and b in ("html", "htm"):
            open(dest, "w", encoding="utf-8").write(markdown_to_html(open(src, encoding="utf-8").read(),
                                                                     os.path.splitext(os.path.basename(src))[0]))
            return "converted with JARVIS's built-in Markdown converter (Pandoc is not installed)"
        if a == "txt" and b in ("html", "htm"):
            import html as h
            open(dest, "w", encoding="utf-8").write(f"<!doctype html><meta charset='utf-8'><pre>{h.escape(open(src, encoding='utf-8', errors='replace').read())}</pre>")
            return None
        raise _NeedsTool("pandoc", "this conversion needs Pandoc (JARVIS can install it)")
    r = subprocess.run([exe, src, "-s", "-o", dest], capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-400:])
    return None


def markdown_to_html(md: str, title: str = "Document") -> str:
    """A small Markdown converter (headings, lists, code, emphasis, links, images, quotes)."""
    import html as h
    lines, out, in_code, in_list = md.replace("\r\n", "\n").split("\n"), [], False, None

    def inline(t: str) -> str:
        t = h.escape(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2">', t)
        t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
        return t
    for line in lines:
        if line.strip().startswith("```"):
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(h.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        lm = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", line)
        if lm:
            tag = "ol" if lm.group(1)[0].isdigit() else "ul"
            if in_list != tag:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f"<{tag}>")
                in_list = tag
            out.append(f"<li>{inline(lm.group(2))}</li>")
            continue
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif line.startswith(">"):
            out.append(f"<blockquote>{inline(line.lstrip('> '))}</blockquote>")
        elif re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            out.append("<hr>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append(f"</{in_list}>")
    body = "\n".join(out)
    return (f"<!doctype html>\n<html><head><meta charset='utf-8'><title>{h.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;line-height:1.55}"
            "pre{background:#f4f4f4;padding:.8rem;overflow:auto}code{font-family:Consolas,monospace}</style></head>"
            f"<body>\n{body}\n</body></html>\n")


def _browser_pdf(src, dest, ask, log):
    a = _ext(src)
    tmp_html = None
    if a in ("md", "markdown"):
        tmp_html = os.path.join(tempfile.mkdtemp(prefix="jarvis_pdf_"), "doc.html")
        open(tmp_html, "w", encoding="utf-8").write(markdown_to_html(open(src, encoding="utf-8").read()))
        page_src = tmp_html
    elif a in ("txt", "csv", "json"):
        import html as h
        tmp_html = os.path.join(tempfile.mkdtemp(prefix="jarvis_pdf_"), "doc.html")
        open(tmp_html, "w", encoding="utf-8").write("<!doctype html><meta charset='utf-8'><pre style='white-space:pre-wrap;"
                                                   f"font-family:Consolas,monospace'>{h.escape(open(src, encoding='utf-8', errors='replace').read())}</pre>")
        page_src = tmp_html
    else:
        page_src = src
    from playwright.sync_api import sync_playwright
    from pathlib import Path
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(Path(os.path.abspath(page_src)).as_uri(), wait_until="load")
        page.pdf(path=dest, format="A4", print_background=True, margin={"top": "15mm", "bottom": "15mm",
                                                                         "left": "12mm", "right": "12mm"})
        browser.close()
    return "printed to PDF with the built-in browser engine"


def _office(src, dest, ask, log):
    exe = _which("soffice", "libreoffice")
    if not exe:
        raise _NeedsTool("libreoffice", "converting Office documents to PDF needs LibreOffice (JARVIS can install it)")
    outdir = os.path.dirname(os.path.abspath(dest))
    r = subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", outdir, src], capture_output=True,
                       text=True, timeout=600, creationflags=NO_WINDOW)
    made = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if os.path.exists(made) and os.path.abspath(made) != os.path.abspath(dest):
        shutil.move(made, dest)
    if r.returncode != 0 and not os.path.exists(dest):
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    return None


# ------------------------------------------------------------------ media, archives, 3D, fonts
def _media(src, dest, ask, log):
    exe = _which("ffmpeg")
    if not exe:
        raise _NeedsTool("ffmpeg", "audio and video conversion needs FFmpeg (JARVIS can install it)")
    args = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if _ext(dest) == "gif":
        args += ["-vf", "fps=12,scale=640:-1:flags=lanczos"]
    if _ext(dest) in ("mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma"):
        args += ["-vn"]
    r = subprocess.run(args + [dest], capture_output=True, text=True, timeout=3600, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-400:])
    return None


def _archive(src, dest, ask, log):
    from . import files
    tmp = tempfile.mkdtemp(prefix="jarvis_arc_")
    try:
        files.extract(src, tmp)
        fmt = {"tgz": "tar.gz", "gz": "tar.gz"}.get(_ext(dest), _ext(dest))
        files.repack(tmp, dest, fmt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


def _mesh(src, dest, ask, log):
    try:
        import trimesh
    except ImportError:
        raise _NeedsTool("trimesh", "3D model conversion needs the trimesh package (JARVIS can install it)")
    mesh = trimesh.load(src, force="scene" if _ext(dest) in ("glb", "gltf") else "mesh")
    mesh.export(dest)
    return None


def _font(src, dest, ask, log):
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        raise _NeedsTool("fonttools", "font conversion needs the fontTools package (JARVIS can install it)")
    f = TTFont(src)
    b = _ext(dest)
    f.flavor = b if b in ("woff", "woff2") else None
    f.save(dest)
    return None


# ------------------------------------------------------------------ -> executable
def _tool_python(name: str, packages, ask, log, module: str = "") -> str:
    """A private Python environment for a build tool (PyInstaller) in JARVIS's tools folder; installed
    once (asking first), then reused."""
    venv = os.path.join(TOOLS_DIR, f"{name}-venv")
    py = os.path.join(venv, "Scripts", "python.exe") if IS_WINDOWS else os.path.join(venv, "bin", "python")
    module = module or re.split(r"[<>=!~\[ ;]", packages[0])[0].replace("-", "_")
    if os.path.exists(py):
        r = subprocess.run([py, "-c", f"import {module}"], capture_output=True, creationflags=NO_WINDOW)
        if r.returncode == 0:
            return py
    if not ask("install_software", f"Install {', '.join(packages)} into JARVIS's tools folder ({TOOLS_DIR}) "
                                   f"to build executables", risk=0.5):
        raise _NeedsTool(name, f"building an executable needs {packages[0]}, and installing it was not allowed")
    log(f"installing {', '.join(packages)}")
    os.makedirs(TOOLS_DIR, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True, creationflags=NO_WINDOW)
    r = subprocess.run([py, "-m", "pip", "install", "-q", *packages], capture_output=True, text=True, timeout=1800,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"installing {packages[0]} failed: {r.stderr.strip()[-300:]}")
    return py


VIEWER = r'''
"""{name} -- built by JARVIS around the file "{name}". It shows the file, copies it, and saves it back out."""
import base64, os, sys

NAME = {name!r}
DATA = base64.b64decode({b64!r})


def console():
    """Where --print writes: a program built as a window (Windows) has no stdout of its own, so it
    attaches to the console it was started from."""
    if sys.stdout is not None:
        return sys.stdout
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            return open("CONOUT$", "w", encoding="utf-8", errors="replace")
    return None


def main():
    if "--print" in sys.argv:
        out = console()
        if out is not None:
            out.write(DATA.decode("utf-8", errors="replace"))
            out.flush()
        return 0
    if "--extract" in sys.argv:
        out = sys.argv[sys.argv.index("--extract") + 1] if len(sys.argv) > sys.argv.index("--extract") + 1 else NAME
        open(out, "wb").write(DATA)
        print(f"wrote {{out}}")
        return 0
    try:                                    # the window needs Tk; without it the file is printed instead
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext
        root = tk.Tk()
    except Exception:
        out = console()
        if out is not None:
            out.write(DATA.decode("utf-8", errors="replace"))
            out.flush()
        return 0
    root.title(NAME)
    root.geometry("760x520")
    text = scrolledtext.ScrolledText(root, font=("Consolas", 11))
    text.insert("1.0", DATA.decode("utf-8", errors="replace"))
    text.pack(fill="both", expand=True)
    bar = tk.Frame(root)
    bar.pack(fill="x")

    def copy():
        root.clipboard_clear()
        root.clipboard_append(text.get("1.0", "end-1c"))

    def save():
        path = filedialog.asksaveasfilename(initialfile=NAME)
        if path:
            open(path, "w", encoding="utf-8").write(text.get("1.0", "end-1c"))
            messagebox.showinfo(NAME, f"Saved {{path}}")

    tk.Button(bar, text="Copy", command=copy).pack(side="left", padx=6, pady=6)
    tk.Button(bar, text="Save as...", command=save).pack(side="left", padx=6, pady=6)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

RUNNER = r'''
"""{name} -- built by JARVIS: runs the script that is inside this program."""
import base64, os, subprocess, sys, tempfile

NAME = {name!r}
DATA = base64.b64decode({b64!r})
RUN = {run!r}


def main():
    d = tempfile.mkdtemp(prefix="jarvis_run_")
    path = os.path.join(d, NAME)
    open(path, "wb").write(DATA)
    cmd = [part.replace("{{file}}", path) for part in RUN] + sys.argv[1:]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
'''


def _exe(src, dest, ask, log):
    """Build an executable from (or around) a file with PyInstaller."""
    import base64
    a = _ext(src)
    name = os.path.basename(src)
    work = tempfile.mkdtemp(prefix="jarvis_exe_")
    if a in ("py", "pyw"):
        script, what = src, f"built {os.path.basename(dest)} from the Python script with PyInstaller"
        windowed = a == "pyw"
    else:
        b64 = base64.b64encode(open(src, "rb").read()).decode("ascii")
        script = os.path.join(work, os.path.splitext(name)[0] + "_app.py")
        if a in SCRIPTS:
            run = {"bat": ["cmd", "/c", "{file}"], "cmd": ["cmd", "/c", "{file}"],
                   "ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "{file}"],
                   "js": ["node", "{file}"], "vbs": ["cscript", "//nologo", "{file}"], "sh": ["sh", "{file}"]}[a]
            open(script, "w", encoding="utf-8").write(RUNNER.format(name=name, b64=b64, run=run))
            what = f"built {os.path.basename(dest)}: running it runs the {a} script inside it"
            windowed = False
        else:
            open(script, "w", encoding="utf-8").write(VIEWER.format(name=name, b64=b64))
            what = (f"built {os.path.basename(dest)} around {name}: it opens a window that shows the file (copy, save "
                    f"as); run it with --print to print the file or --extract to write it back out")
            windowed = True
    py = _tool_python("pyinstaller", ["pyinstaller>=6"], ask, log, module="PyInstaller")
    out_name = os.path.splitext(os.path.basename(dest))[0]
    args = [py, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--name", out_name,
            "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "build"),
            "--specpath", work]
    if windowed and IS_WINDOWS:
        args.append("--windowed")
    log("building the executable (PyInstaller, a minute or two)")
    r = subprocess.run(args + [script], capture_output=True, text=True, timeout=1800, cwd=work, creationflags=NO_WINDOW)
    built = os.path.join(work, "dist", out_name + (".exe" if IS_WINDOWS else ""))
    if r.returncode != 0 or not os.path.exists(built):
        raise RuntimeError("PyInstaller failed: " + (r.stderr or r.stdout).strip()[-500:])
    final = dest if IS_WINDOWS or not dest.lower().endswith(".exe") else dest
    shutil.move(built, final)
    if not IS_WINDOWS:
        os.chmod(final, 0o755)
        what += " (on this Linux machine it is a Linux executable; on Windows the same steps make a .exe)"
    shutil.rmtree(work, ignore_errors=True)
    return what
