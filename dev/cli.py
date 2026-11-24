"""
dev/cli.py -- `jarvis-tool`: JARVIS's file utilities as commands the coding model can run.

  jarvis-tool inspect <file>                 what a file is and what is inside
  jarvis-tool extract <archive> <folder>     unpack zip / 7z / rar / tar / gz
  jarvis-tool repack <folder> <archive>      pack as zip / tar.gz / 7z (from the extension)
  jarvis-tool pyinstaller-extract <exe> <folder>
  jarvis-tool asar-extract <app.asar> <folder>   |   asar-pack <folder> <app.asar>
  jarvis-tool gba-info <rom>                 the cartridge header
  jarvis-tool gba-fix <rom> [--title T] [--code C] [--maker M] [--version N]
  jarvis-tool ips-apply <rom> <patch> <out>  |   ips-create <original> <modified> <patch>
  jarvis-tool bps-apply <rom> <patch> <out>  |   bps-create <original> <modified> <patch>
  jarvis-tool tiles-export <rom> <offset> <count> <out.png>    4-bit tiles -> PNG
  jarvis-tool tiles-import <rom> <offset> <in.png>             PNG -> tiles, written back
  jarvis-tool convert <input> <output>       change a file's format (see dev/convert.py)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import files


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="jarvis-tool", description="JARVIS's developer file utilities")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, args in (("inspect", ["file"]), ("extract", ["archive", "folder"]), ("repack", ["folder", "archive"]),
                       ("pyinstaller-extract", ["exe", "folder"]), ("asar-extract", ["asar", "folder"]),
                       ("asar-pack", ["folder", "asar"]), ("gba-info", ["rom"]),
                       ("ips-apply", ["rom", "patch", "out"]), ("ips-create", ["original", "modified", "patch"]),
                       ("bps-apply", ["rom", "patch", "out"]), ("bps-create", ["original", "modified", "patch"]),
                       ("tiles-export", ["rom", "offset", "count", "out"]), ("tiles-import", ["rom", "offset", "png"]),
                       ("convert", ["input", "output"])):
        sp = sub.add_parser(name)
        for a in args:
            sp.add_argument(a)
    fix = sub.add_parser("gba-fix")
    fix.add_argument("rom")
    fix.add_argument("--title")
    fix.add_argument("--code")
    fix.add_argument("--maker")
    fix.add_argument("--version", type=int)
    a = p.parse_args(argv)
    try:
        if a.cmd == "inspect":
            info = files.inspect(a.file)
            print(info["summary"])
            print(json.dumps(info.get("details") or {}, indent=1, default=str)[:6000])
        elif a.cmd == "extract":
            out = files.extract(a.archive, a.folder)
            print(f"extracted {len(out)} files into {a.folder}")
        elif a.cmd == "repack":
            print("wrote", files.repack(a.folder, a.archive))
        elif a.cmd == "pyinstaller-extract":
            r = files.pyinstaller_extract(a.exe, a.folder)
            print(f"{len(r['files'])} files (Python {r['python']}); main scripts: {', '.join(r['scripts'])}")
        elif a.cmd == "asar-extract":
            print(f"extracted {len(files.asar_extract(a.asar, a.folder))} files")
        elif a.cmd == "asar-pack":
            print("wrote", files.asar_pack(a.folder, a.asar))
        elif a.cmd == "gba-info":
            print(json.dumps(files.gba_header(a.rom), indent=1))
        elif a.cmd == "gba-fix":
            h = files.gba_fix_header(a.rom, a.title, a.code, a.maker, a.version)
            print(json.dumps(h, indent=1))
            return 0 if h.get("ok") else 1
        elif a.cmd in ("ips-apply", "bps-apply"):
            fn = files.ips_apply if a.cmd == "ips-apply" else files.bps_apply
            out = fn(open(a.rom, "rb").read(), open(a.patch, "rb").read())
            open(a.out, "wb").write(out)
            print(f"wrote {a.out} ({len(out)} bytes)")
        elif a.cmd in ("ips-create", "bps-create"):
            fn = files.ips_create if a.cmd == "ips-create" else files.bps_create
            patch = fn(open(a.original, "rb").read(), open(a.modified, "rb").read())
            open(a.patch, "wb").write(patch)
            print(f"wrote {a.patch} ({len(patch)} bytes)")
        elif a.cmd == "tiles-export":
            off, n = int(a.offset, 0), int(a.count, 0)
            data = open(a.rom, "rb").read()[off:off + 32 * n]
            print("wrote", files.tiles_to_png(data, a.out))
        elif a.cmd == "tiles-import":
            off = int(a.offset, 0)
            tiles = files.png_to_tiles(a.png)
            rom = bytearray(open(a.rom, "rb").read())
            rom[off:off + len(tiles)] = tiles
            open(a.rom, "wb").write(rom)
            print(f"wrote {len(tiles) // 32} tiles at 0x{off:X}")
        elif a.cmd == "convert":
            from . import convert
            r = convert.convert(a.input, a.output)
            print(r.get("message") or r)
            return 0 if r.get("ok") else 1
    except Exception as e:
        print(f"jarvis-tool {a.cmd}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
