"""
tests/test_dev_files.py -- opening what you send and changing formats (v1.5.0).

    python tests/test_dev_files.py

Real-type detection; Windows programs (a synthetic PE: 64-bit, console/window, .NET, UPX); a
PyInstaller archive (synthetic CArchive) unpacked; GBA header repair and checksum; IPS and BPS
patches round-trip; 4-bit tiles <-> PNG; Electron .asar; archives (and a zip-slip archive refused);
conversions (image -> icon, data formats, Markdown -> HTML / PDF) and their routing; refusals.
"""
import io
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dev import files, convert  # noqa: E402


def _tmp():
    return tempfile.mkdtemp(prefix="jarvis_filetest_")


def _pe(machine=0x8664, subsystem=3, dotnet=False, sections=(".text", ".rsrc"), extra=b""):
    """A minimal (not runnable) PE image with the fields JARVIS reads."""
    dos = bytearray(b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x80))
    dos += b"\x00" * (0x80 - len(dos))
    coff = struct.pack("<HHIIIHH", machine, len(sections), 0x5F000000, 0, 0, 240, 0x0022)
    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20B)                  # PE32+
    struct.pack_into("<H", opt, 68, subsystem)
    struct.pack_into("<I", opt, 108, 16)                   # number of data directories
    if dotnet:
        struct.pack_into("<II", opt, 112 + 8 * 14, 0x2000, 0x48)
    sect = b"".join(n.encode().ljust(8, b"\x00") + struct.pack("<IIII", 0x1000, 0x1000 * (i + 1), 0x200, 0x400) + b"\x00" * 16
                    for i, n in enumerate(sections))
    return bytes(dos) + b"PE\x00\x00" + coff + bytes(opt) + sect + b"\x00" * 512 + extra


def test_the_real_type_is_detected():
    t = _tmp()
    try:
        from PIL import Image
        Image.new("RGB", (8, 8)).save(os.path.join(t, "photo.jpg"))
        os.rename(os.path.join(t, "photo.jpg"), os.path.join(t, "photo.dat"))       # wrong extension
        with zipfile.ZipFile(os.path.join(t, "report.zip"), "w") as z:
            z.writestr("[Content_Types].xml", "<x/>")
            z.writestr("xl/workbook.xml", "<w/>")
        open(os.path.join(t, "settings.ini"), "w").write("[a]\nb=1\n")
        open(os.path.join(t, "app.exe"), "wb").write(_pe())
        cases = {"photo.dat": "jpg", "report.zip": "xlsx", "settings.ini": "ini", "app.exe": "exe"}
        for name, kind in cases.items():
            assert files.detect(os.path.join(t, name))["kind"] == kind, (name, files.detect(os.path.join(t, name)))
        assert files.format_matches(files.detect(os.path.join(t, "report.zip")), "zip")
        assert not files.format_matches(files.detect(os.path.join(t, "photo.dat")), "png")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_windows_programs_are_analysed():
    t = _tmp()
    try:
        p = os.path.join(t, "tool.exe")
        open(p, "wb").write(_pe(dotnet=True, subsystem=2))
        info = files.pe_info(p)
        assert info["dotnet"] and info["machine"].startswith("x64") and "window" in info["subsystem"], info
        assert ".NET" in info["recoverable"] and "decompile" in info["recoverable"]
        open(p, "wb").write(_pe(sections=("UPX0", "UPX1")))
        info = files.pe_info(p)
        assert "UPX-packed" in info["built_with"] and info["native"], info
        assert "cannot be recovered" in files.inspect(p)["summary"]
        open(p, "wb").write(_pe(extra=b"Go build ID: abc runtime.gopanic"))
        assert "Go" in files.pe_info(p)["built_with"]
    finally:
        shutil.rmtree(t, ignore_errors=True)


def _carchive(entries):
    """A synthetic PyInstaller CArchive appended to a fake executable."""
    data, toc = b"", b""
    for name, blob, typ in entries:
        comp = zlib.compress(blob)
        nm = name.encode() + b"\x00"
        nm += b"\x00" * ((16 - (18 + len(nm)) % 16) % 16)
        toc += struct.pack("!iIIIBc", 18 + len(nm), len(data), len(comp), len(blob), 1, typ) + nm
        data += comp
    cookie_len = 88
    pkg_len = len(data) + len(toc) + cookie_len
    cookie = struct.pack("!8sIIii64s", b"MEI\x0c\x0b\x0a\x0b\x0e", pkg_len, len(data), len(toc), 311, b"python311.dll")
    return _pe() + data + toc + cookie


def test_a_pyinstaller_program_is_unpacked():
    t = _tmp()
    try:
        exe = os.path.join(t, "game.exe")
        open(exe, "wb").write(_carchive([("main", b"\xe3fake-code-object", b"s"), ("data/levels.json", b'{"n": 3}', b"x")]))
        assert "PyInstaller (Python)" in files.pe_info(exe)["built_with"]
        r = files.pyinstaller_extract(exe, os.path.join(t, "out"))
        assert r["python"] == "3.11" and r["scripts"] == ["main.pyc.code"], r
        assert open(os.path.join(t, "out", "data", "levels.json")).read() == '{"n": 3}'
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_gba_header_patches_and_tiles():
    t = _tmp()
    try:
        rom = os.path.join(t, "game.gba")
        open(rom, "wb").write(os.urandom(4096))
        assert not files.gba_header(rom)["ok"]
        h = files.gba_fix_header(rom, title="Jarvis Demo", code="AJVE", maker="01", version=2)
        assert h["ok"] and h["title"] == "JARVIS DEMO" and h["code"] == "AJVE" and h["version"] == 2, h
        assert files.detect(rom)["kind"] == "gba"
        orig = bytes(os.urandom(50000))
        mod = bytearray(orig)
        for i in random.sample(range(len(mod)), 200):
            mod[i] ^= 0x5A
        mod = bytes(mod) + b"\x00" * 300
        assert files.ips_apply(orig, files.ips_create(orig, mod)) == mod
        assert files.ips_apply(orig, files.ips_create(orig, orig[:30000])) == orig[:30000]
        rle = b"PATCH" + (10).to_bytes(3, "big") + (0).to_bytes(2, "big") + (5).to_bytes(2, "big") + b"\xAA" + b"EOF"
        assert files.ips_apply(b"\x00" * 20, rle)[10:15] == b"\xAA" * 5
        bps = files.bps_create(orig, mod)
        assert files.bps_apply(orig, bps) == mod
        try:
            files.bps_apply(orig[:-1] + b"\x01", bps)
            raise AssertionError("a BPS patch for a different file must be refused")
        except ValueError:
            pass
        tiles = os.urandom(32 * 24)
        png = files.tiles_to_png(tiles, os.path.join(t, "tiles.png"), per_row=8)
        assert files.png_to_tiles(png) == tiles
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_archives_and_electron_apps():
    t = _tmp()
    try:
        src = os.path.join(t, "app")
        os.makedirs(os.path.join(src, "lib"))
        open(os.path.join(src, "main.js"), "w").write("console.log(1)")
        open(os.path.join(src, "lib", "util.js"), "w").write("module.exports = 2")
        asar = files.asar_pack(src, os.path.join(t, "app.asar"))
        assert sorted(files.asar_extract(asar, os.path.join(t, "un"))) == ["lib/util.js", "main.js"]
        for fmt in ("zip", "tar.gz"):
            arc = files.repack(src, os.path.join(t, f"app.{fmt}"))
            out = files.extract(arc, os.path.join(t, f"x_{fmt.replace('.', '_')}"))
            assert len(out) == 2, (fmt, out)
        evil = os.path.join(t, "evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escaped.txt", "x")
        try:
            files.extract(evil, os.path.join(t, "ev"))
            raise AssertionError("a path escaping the folder must be refused")
        except ValueError:
            pass
        assert not os.path.exists(os.path.join(t, "escaped.txt"))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_conversions():
    t = _tmp()
    try:
        from PIL import Image
        Image.new("RGBA", (300, 200), (10, 120, 220, 255)).save(os.path.join(t, "logo.png"))
        open(os.path.join(t, "s.ini"), "w").write("[display]\nwidth=1920\n[audio]\nvolume=70\n")
        open(os.path.join(t, "p.csv"), "w").write("name,age\nAnn,31\nBo,42\n")
        open(os.path.join(t, "d.md"), "w").write("# Title\n\nSome **bold** text.\n\n- one\n- two\n")
        r = convert.convert(os.path.join(t, "logo.png"), os.path.join(t, "logo.ico"))
        assert r["ok"] and files.detect(os.path.join(t, "logo.ico"))["kind"] == "ico" and "7 sizes" in r["message"], r
        for dst in ("s.json", "s.yaml", "s.toml", "s.xml"):
            r = convert.convert(os.path.join(t, "s.ini"), os.path.join(t, dst))
            assert r["ok"] and r["tool"] == "data", (dst, r)
        assert json.load(open(os.path.join(t, "s.json")))["display"]["width"] == "1920"
        r = convert.convert(os.path.join(t, "p.csv"), os.path.join(t, "p.json"))
        assert json.load(open(os.path.join(t, "p.json")))[1]["name"] == "Bo"
        r = convert.convert(os.path.join(t, "d.md"), os.path.join(t, "d.html"))
        assert r["ok"] and "<strong>bold</strong>" in open(os.path.join(t, "d.html")).read()
        r = convert.convert(os.path.join(t, "d.md"), os.path.join(t, "d.pdf"))
        assert r["ok"] and files.detect(os.path.join(t, "d.pdf"))["kind"] == "pdf", r
        assert convert.route("a.ini", "a.exe") == "exe" and convert.route("a.png", "a.ico") == "image"
        assert convert.route("a.mp4", "a.mp3") == "media" and convert.route("a.blend", "a.fbx") is None
        r = convert.convert(os.path.join(t, "s.ini"), os.path.join(t, "s.blend"))
        assert not r["ok"] and r.get("fallback"), "an unknown pair goes to the coding loop"
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_a_missing_converter_is_installed_then_the_conversion_runs_again():
    t = _tmp()
    real_data, real_install = convert._data, convert._install_missing
    calls = {"n": 0, "installed": []}
    try:
        open(os.path.join(t, "s.ini"), "w").write("[a]\nb=1\n")

        def data_needing_yaml(src, dest, ask, log):
            calls["n"] += 1
            if not calls["installed"]:
                raise convert._NeedsTool("pyyaml", "reading YAML needs the PyYAML package")
            return real_data(src, dest, ask, log)
        convert._data = data_needing_yaml
        convert._install_missing = lambda tool, ask, log: calls["installed"].append(tool) or True
        r = convert.convert(os.path.join(t, "s.ini"), os.path.join(t, "s.json"))
        assert r["ok"] and calls["installed"] == ["pyyaml"] and calls["n"] == 2, (r, calls)
        convert._install_missing = real_install
        said = convert._install_missing("pyyaml", lambda *a, **k: False, lambda m: None)
        assert said == "installing pyyaml was not allowed", said
        calls["installed"].clear()
        convert._install_missing = lambda tool, ask, log: "installing pyyaml was not allowed"
        r = convert.convert(os.path.join(t, "s.ini"), os.path.join(t, "s2.json"))
        assert not r["ok"] and "not allowed" in r["message"] and r["missing"] == "pyyaml", r
    finally:
        convert._data, convert._install_missing = real_data, real_install
        shutil.rmtree(t, ignore_errors=True)


def test_protection_removal_and_malware_are_refused():
    for text in ("remove the license check from this game", "crack this program", "make a keylogger",
                 "bypass the activation in setup.exe", "write ransomware", "add an aimbot to the game"):
        assert files.refused(text), text
    for text in ("add a reset button", "make the snake faster", "translate the menus to Korean",
                 "convert this ini to exe", "fix the crash when saving"):
        assert files.refused(text) is None, text


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS {fn.__name__} ({time.time() - t0:.1f}s)")
        except Exception:
            import traceback
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} developer file tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_all()
