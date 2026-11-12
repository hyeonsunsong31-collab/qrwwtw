"""
boot/overlay_runtime.py -- make sure the holographic overlay can start. No Node.js needed.

The overlay needs three things under node_modules/:

    electron/dist/<electron binary>     the Electron runtime (about 110 MB download)
    three/                              three.js, for the 3D project space
    @mediapipe/tasks-vision/            hand tracking, for gesture control

`npm install` provides them, but it is the most fragile step of an install: it needs
Node.js, a working PATH, and a post-install script that silently leaves Electron's binary
out when its download hiccups -- and then JARVIS runs with nothing on screen. This module
fetches exactly the same files itself and verifies them the way npm does:

  * versions, URLs and SHA-512 integrity values are read from package-lock.json,
  * the Electron binary's SHA-256 is read from the electron package's own checksums.json.

Nothing is trusted that is not pinned by those two files. It is used by the installer, by
START_JARVIS.bat (repair before start) and by the supervisor (repair at logon).

    python boot/overlay_runtime.py            install whatever is missing
    python boot/overlay_runtime.py --check    only report; exit code 1 if something is missing
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform as _platform
import shutil
import stat
import sys
import tarfile
import time
import urllib.request
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PACKAGES = {                      # lock-file key -> a file that proves the package is usable
    "node_modules/electron": "checksums.json",
    "node_modules/three": os.path.join("build", "three.module.js"),
    "node_modules/@mediapipe/tasks-vision": "vision_bundle.mjs",
}
ALLOWED_PREFIXES = ("https://registry.npmjs.org/", "https://github.com/electron/electron/releases/download/")
DEFAULT_MIRROR = "https://github.com/electron/electron/releases/download/"
MIN_FREE_BYTES = 700 * 1024 * 1024

Log = Callable[[str], None]


class RuntimeProblem(Exception):
    """A step failed in a way worth showing to the user verbatim."""


# ------------------------------------------------------------------ platform
def electron_platform(platform: Optional[str] = None, arch: Optional[str] = None) -> Tuple[str, str, str]:
    """(platform, arch, executable path inside dist) in Electron's naming."""
    plat = platform or ("win32" if sys.platform.startswith("win") else
                        "darwin" if sys.platform == "darwin" else "linux")
    m = (arch or _platform.machine() or "").lower()
    arch_name = {"amd64": "x64", "x86_64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64",
                 "x86": "ia32", "i386": "ia32", "i686": "ia32", "ia32": "ia32", "armv7l": "armv7l"}.get(m, "x64")
    exe = {"win32": "electron.exe", "darwin": "Electron.app/Contents/MacOS/Electron"}.get(plat, "electron")
    return plat, arch_name, exe


# ------------------------------------------------------------------ lock file
def read_lock(root: str = ROOT) -> Dict[str, dict]:
    path = os.path.join(root, "package-lock.json")
    try:
        with open(path, encoding="utf-8") as fh:
            packages = json.load(fh).get("packages", {})
    except (OSError, ValueError) as e:
        raise RuntimeProblem(f"package-lock.json is missing or unreadable ({e}); extract the JARVIS folder again")
    out = {}
    for key in PACKAGES:
        entry = packages.get(key) or {}
        if not all(entry.get(k) for k in ("version", "resolved", "integrity")):
            raise RuntimeProblem(f"package-lock.json has no complete entry for {key}")
        out[key] = entry
    return out


def _installed_version(pkg_dir: str) -> str:
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version", ""))
    except (OSError, ValueError):
        return ""


def _dist_ok(root: str, version: str, exe_rel: str) -> bool:
    base = os.path.join(root, "node_modules", "electron")
    try:
        with open(os.path.join(base, "dist", "version"), encoding="utf-8") as fh:
            if fh.read().strip().lstrip("v") != version:
                return False
        with open(os.path.join(base, "path.txt"), encoding="utf-8") as fh:
            if fh.read().strip() != exe_rel:
                return False
    except OSError:
        return False
    return os.path.isfile(os.path.join(base, "dist", *exe_rel.split("/")))


def problems(root: str = ROOT, platform: Optional[str] = None, arch: Optional[str] = None) -> List[str]:
    """Human-readable list of what is missing; empty when the overlay can start."""
    try:
        lock = read_lock(root)
    except RuntimeProblem as e:
        return [str(e)]
    out = []
    for key, sentinel in PACKAGES.items():
        pkg_dir = os.path.join(root, *key.split("/"))
        name = key[len("node_modules/"):]
        if _installed_version(pkg_dir) != lock[key]["version"] or not os.path.isfile(os.path.join(pkg_dir, sentinel)):
            out.append(f"{name} {lock[key]['version']} is not installed")
    _plat, _arch, exe_rel = electron_platform(platform, arch)
    if not _dist_ok(root, lock["node_modules/electron"]["version"], exe_rel):
        out.append(f"the Electron {lock['node_modules/electron']['version']} runtime (electron/dist) is not installed")
    return out


# ------------------------------------------------------------------ download
def _check_url(url: str) -> None:
    mirror = os.environ.get("ELECTRON_MIRROR", "")
    if url.startswith(ALLOWED_PREFIXES) or (mirror and url.startswith(mirror)):
        return
    raise RuntimeProblem(f"refusing to download from an unexpected address: {url}")


def _digest_matches(h, expected: str) -> bool:
    """expected is 'sha512-<base64>' (npm integrity) or a hex SHA-256 (Electron checksums)."""
    if expected.startswith("sha512-"):
        return base64.b64encode(h.digest()).decode() == expected[len("sha512-"):]
    return h.hexdigest().lower() == expected.lower()


def _new_hash(expected: str):
    return hashlib.sha512() if expected.startswith("sha512-") else hashlib.sha256()


def file_matches(path: str, expected: str) -> bool:
    h = _new_hash(expected)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return _digest_matches(h, expected)


def download(url: str, dest: str, expected: str, log: Log, tries: int = 3) -> str:
    """Download url to dest unless a verified copy is already there. Returns dest."""
    _check_url(url)
    if os.path.isfile(dest) and file_matches(dest, expected):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    name = os.path.basename(dest)
    part = f"{dest}.{os.getpid()}.part"
    last = ""
    for attempt in range(1, tries + 1):
        h = _new_hash(expected)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "jarvis-overlay-runtime"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0)
                size_txt = f" ({total / 1048576:.0f} MB)" if total > 5 * 1048576 else ""
                log(f"downloading {name}{size_txt} ...")
                done, next_mark = 0, 0.2
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total > 20 * 1048576 and done / total >= next_mark:
                        log(f"  {name}: {int(next_mark * 100)}%")
                        next_mark += 0.2
            if total and done != total:
                raise OSError(f"connection closed early ({done} of {total} bytes)")
            if not _digest_matches(h, expected):
                raise RuntimeProblem(f"{name} failed its integrity check (corrupted or altered download)")
            os.replace(part, dest)
            return dest
        except Exception as e:                     # network, disk, integrity
            last = str(e) if isinstance(e, RuntimeProblem) else f"{type(e).__name__}: {e}"
            try:
                os.remove(part)
            except OSError:
                pass
            if attempt < tries:
                log(f"  download of {name} failed ({last}); retrying ({attempt + 1} of {tries})")
                time.sleep(3 * attempt)
    raise RuntimeProblem(f"could not download {name} from {url.split('/')[2]}: {last}")


# ------------------------------------------------------------------ extraction
def _safe_join(base: str, rel: str) -> Optional[str]:
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None
    target = os.path.normpath(os.path.join(base, *rel.split("/")))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def extract_tgz(archive: str, dest: str) -> int:
    """Extract an npm tarball, dropping its top-level folder (as npm does). Files only."""
    count = 0
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf:
            parts = member.name.replace("\\", "/").split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            target = _safe_join(dest, parts[1])
            if target is None:
                raise RuntimeProblem(f"unsafe path in {os.path.basename(archive)}: {member.name}")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
            # links and special files are never part of these packages: ignore them
    return count


def extract_zip(archive: str, dest: str) -> int:
    """Extract Electron's zip. Keeps executable bits and symlinks where the OS has them."""
    count = 0
    posix = os.name == "posix"
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = _safe_join(dest, info.filename)
            if target is None:
                raise RuntimeProblem(f"unsafe path in {os.path.basename(archive)}: {info.filename}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if posix and stat.S_ISLNK(mode):
                link = zf.read(info).decode("utf-8")
                if os.path.isabs(link) or _safe_join(os.path.dirname(target), link) is None:
                    raise RuntimeProblem(f"unsafe link in {os.path.basename(archive)}: {info.filename}")
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(link, target)
            else:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if posix and mode:
                    os.chmod(target, stat.S_IMODE(mode) or 0o644)
            count += 1
    return count


def _rmtree(path: str) -> None:
    for _ in range(5):
        if not os.path.lexists(path):
            return
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return
        except OSError:
            time.sleep(1)          # antivirus scanners briefly hold freshly written files
    if os.path.lexists(path):
        shutil.rmtree(path)        # raise the real error


def _swap_in(tmp: str, final: str) -> None:
    """Replace `final` with the fully extracted `tmp` folder."""
    _rmtree(final)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    last = None
    for _ in range(8):
        try:
            os.replace(tmp, final)
            return
        except OSError as e:
            last = e
            time.sleep(1)
    raise RuntimeProblem(f"could not put {os.path.basename(final)} in place: {last}")


# ------------------------------------------------------------------ install steps
def _install_npm_package(root: str, key: str, entry: dict, cache: str, log: Log) -> None:
    name = key[len("node_modules/"):]
    tgz = os.path.join(cache, f"{name.replace('/', '-').lstrip('@')}-{entry['version']}.tgz")
    download(entry["resolved"], tgz, entry["integrity"], log)
    final = os.path.join(root, *key.split("/"))
    tmp = f"{final}.tmp-{os.getpid()}"
    _rmtree(tmp)
    keep_dist = None
    if key == "node_modules/electron" and os.path.isdir(os.path.join(final, "dist")):
        keep_dist = f"{final}.dist-{os.getpid()}"      # never throw away a 280 MB binary for nothing
        _rmtree(keep_dist)
        os.replace(os.path.join(final, "dist"), keep_dist)
    try:
        n = extract_tgz(tgz, tmp)
        _swap_in(tmp, final)
    finally:
        _rmtree(tmp)
        if keep_dist and os.path.isdir(keep_dist):
            if os.path.isdir(final) and not os.path.exists(os.path.join(final, "dist")):
                os.replace(keep_dist, os.path.join(final, "dist"))
            else:
                _rmtree(keep_dist)
    log(f"installed {name} {entry['version']} ({n} files)")


def _install_electron_binary(root: str, version: str, cache: str, log: Log,
                             platform: Optional[str], arch: Optional[str]) -> None:
    plat, arch_name, exe_rel = electron_platform(platform, arch)
    base = os.path.join(root, "node_modules", "electron")
    filename = f"electron-v{version}-{plat}-{arch_name}.zip"
    try:
        with open(os.path.join(base, "checksums.json"), encoding="utf-8") as fh:
            sha256 = json.load(fh)[filename]
    except (OSError, ValueError, KeyError):
        raise RuntimeProblem(f"no published checksum for {filename} (unsupported processor type?)")
    mirror = os.environ.get("ELECTRON_MIRROR") or DEFAULT_MIRROR
    archive = download(f"{mirror}v{version}/{filename}", os.path.join(cache, filename), sha256, log)
    dist = os.path.join(base, "dist")
    tmp = f"{dist}.tmp-{os.getpid()}"
    _rmtree(tmp)
    try:
        log(f"unpacking {filename} ...")
        n = extract_zip(archive, tmp)
        if not os.path.isfile(os.path.join(tmp, *exe_rel.split("/"))):
            raise RuntimeProblem(f"{filename} does not contain {exe_rel}")
        _swap_in(tmp, dist)
    finally:
        _rmtree(tmp)
    typedefs = os.path.join(dist, "electron.d.ts")
    if os.path.isfile(typedefs):
        os.replace(typedefs, os.path.join(base, "electron.d.ts"))
    with open(os.path.join(base, "path.txt"), "w", encoding="utf-8") as fh:
        fh.write(exe_rel)
    log(f"installed the Electron {version} runtime for {plat}-{arch_name} ({n} files)")


def ensure(root: str = ROOT, log: Optional[Log] = None, platform: Optional[str] = None,
           arch: Optional[str] = None, cache: Optional[str] = None) -> List[str]:
    """Install whatever the overlay is missing. Returns the problems that remain ([] = ready)."""
    log = log or (lambda m: print("    " + m, flush=True))
    todo = problems(root, platform, arch)
    if not todo:
        return []
    try:
        lock = read_lock(root)
        cache = cache or os.path.join(os.environ.get("JARVIS_DATA") or os.path.join(root, "data"), "downloads")
        os.makedirs(cache, exist_ok=True)
        try:
            free = shutil.disk_usage(root).free
            if free < MIN_FREE_BYTES:
                raise RuntimeProblem(f"only {free // 1048576} MB free on this drive; the overlay runtime needs about 700 MB")
        except OSError:
            pass
        for key, sentinel in PACKAGES.items():
            pkg_dir = os.path.join(root, *key.split("/"))
            if _installed_version(pkg_dir) != lock[key]["version"] or not os.path.isfile(os.path.join(pkg_dir, sentinel)):
                _install_npm_package(root, key, lock[key], cache, log)
        version = lock["node_modules/electron"]["version"]
        if not _dist_ok(root, version, electron_platform(platform, arch)[2]):
            _install_electron_binary(root, version, cache, log, platform, arch)
    except RuntimeProblem as e:
        log(f"overlay runtime: {e}")
        return [str(e)]
    except Exception as e:                                  # disk full, permissions, ...
        log(f"overlay runtime: {type(e).__name__}: {e}")
        return [f"{type(e).__name__}: {e}"]
    return problems(root, platform, arch)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        left = problems()
        for p in left:
            print("missing: " + p)
        if not left:
            print("overlay runtime: ready")
        return 1 if left else 0
    left = ensure(log=lambda m: print("  " + m, flush=True))
    if left:
        for p in left:
            print("  [problem] " + p)
        return 1
    print("  overlay runtime: ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
