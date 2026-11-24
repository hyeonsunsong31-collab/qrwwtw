"""
dev/serve.py -- links for web products.

  local   http://127.0.0.1:PORT        always; only this PC can open it
  lan     http://192.168.x.y:PORT      "share it on my network": phones and PCs on your Wi-Fi
  public  https://xxxx.trycloudflare.com   "share it publicly": a free Cloudflare quick tunnel
          (no account, no cost; the cloudflared program is downloaded once, after you agree)

A static site (HTML/CSS/JS, or a build folder) is served by JARVIS itself; a project with its
own server (Node/Express, Flask, ...) is started with PORT / HOST set. Sharing can be stopped
at any time ("stop sharing"), and everything stops when JARVIS does.
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Callable, Dict, Optional

from . import procs

IS_WINDOWS = sys.platform.startswith("win")
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
                      ".json": "application/json", ".svg": "image/svg+xml", ".webp": "image/webp",
                      ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".gba": "application/octet-stream"}

    def log_message(self, *a):            # quiet
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        # single-page apps: an unknown path without a file extension gets index.html
        path = self.translate_path(self.path)
        if not os.path.exists(path) and "." not in os.path.basename(self.path.split("?")[0]):
            index = os.path.join(self.directory, "index.html")
            if os.path.exists(index):
                self.path = "/index.html"
        return super().send_head()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class StaticServer:
    """Serve a folder over HTTP from a background thread."""

    def __init__(self, root: str, host: str = "127.0.0.1", port: int = 0):
        self.root = os.path.abspath(root)
        self.host = host
        self.port = port
        self._srv: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "StaticServer":
        handler = functools.partial(_Handler, directory=self.root)
        self._srv = _Server((self.host, self.port), handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, name="jarvis-static", daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        if self._srv is not None:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None


class _Product:
    def __init__(self):
        self.static: Optional[StaticServer] = None
        self.proc: Optional[procs.Background] = None
        self.port = 0
        self.lan = False
        self.tunnel: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self.spec: dict = {}


class Links:
    """One running web product per workspace, with its local / network / public links."""

    def __init__(self, data_dir: str, log: Optional[Callable[[str], None]] = None):
        self.data_dir = data_dir
        self.bin_dir = os.path.join(data_dir, "bin")
        self.log = log or (lambda m: None)
        self._items: Dict[str, _Product] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ serve
    def serve(self, wid: str, root: Optional[str] = None, command: Optional[str] = None,
              cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, port: int = 0,
              url_path: str = "/", lan: bool = False) -> dict:
        """Start (or restart) a workspace's product; returns its links."""
        with self._lock:
            old = self._items.get(wid)
            keep_public = bool(old and old.public_url)
            self.stop(wid)
            item = _Product()
            item.spec = {"root": root, "command": command, "cwd": cwd, "env": env or {}, "port": port,
                         "url_path": url_path or "/"}
            host = "0.0.0.0" if lan else "127.0.0.1"
            if command:
                item.port = port or procs.free_port()
                e = dict(env or {})
                e.update({"PORT": str(item.port), "HOST": host})
                item.proc = procs.Background(command, cwd or root or ".", env=e,
                                             log_path=os.path.join(self.data_dir, "logs", f"product-{wid}.log")).start()
                if not procs.wait_port(item.port, timeout=90, alive=item.proc.alive):
                    out = item.proc.output(30)
                    item.proc.stop()
                    raise RuntimeError(f"the product's server did not start on port {item.port}: {out[-600:]}")
            else:
                item.static = StaticServer(root or ".", host=host).start()
                item.port = item.static.port
            item.lan = lan
            self._items[wid] = item
        links = self.links(wid)
        if keep_public:
            try:
                self.share_public(wid)
                links = self.links(wid)
            except Exception as e:
                self.log(f"could not restore the public link: {e}")
        return links

    def links(self, wid: str) -> dict:
        item = self._items.get(wid)
        if item is None:
            return {}
        path = item.spec.get("url_path") or "/"
        out = {"local": f"http://127.0.0.1:{item.port}{path}"}
        if item.lan:
            ip = procs.lan_ip()
            if ip:
                out["lan"] = f"http://{ip}:{item.port}{path}"
        if item.public_url:
            out["public"] = item.public_url.rstrip("/") + path
        return out

    def running(self, wid: str) -> bool:
        item = self._items.get(wid)
        if item is None:
            return False
        if item.proc is not None:
            return item.proc.alive()
        return item.static is not None

    # ------------------------------------------------------------ sharing
    def share_lan(self, wid: str) -> dict:
        item = self._items.get(wid)
        if item is None:
            raise KeyError("nothing is being served for this workspace")
        if not item.lan:
            s = item.spec
            self.serve(wid, s.get("root"), s.get("command"), s.get("cwd"), s.get("env"), item.port, s.get("url_path"), lan=True)
        return self.links(wid)

    def cloudflared(self) -> Optional[str]:
        found = shutil.which("cloudflared")
        if found:
            return found
        local = os.path.join(self.bin_dir, "cloudflared.exe" if IS_WINDOWS else "cloudflared")
        return local if os.path.exists(local) else None

    def download_cloudflared(self) -> str:
        """Fetch Cloudflare's cloudflared (one file, ~40 MB) into data/bin. The caller asks first."""
        os.makedirs(self.bin_dir, exist_ok=True)
        if IS_WINDOWS:
            name, dest = "cloudflared-windows-amd64.exe", os.path.join(self.bin_dir, "cloudflared.exe")
        elif sys.platform == "darwin":
            raise RuntimeError("install cloudflared with: brew install cloudflared")
        else:
            name, dest = "cloudflared-linux-amd64", os.path.join(self.bin_dir, "cloudflared")
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{name}"
        self.log(f"downloading cloudflared from {url}")
        tmp = dest + ".part"
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        os.replace(tmp, dest)
        if not IS_WINDOWS:
            os.chmod(dest, 0o755)
        return dest

    def share_public(self, wid: str, timeout: float = 60.0) -> dict:
        item = self._items.get(wid)
        if item is None:
            raise KeyError("nothing is being served for this workspace")
        if item.public_url and item.tunnel is not None and item.tunnel.poll() is None:
            return self.links(wid)
        exe = self.cloudflared()
        if not exe:
            raise FileNotFoundError("cloudflared")
        flags = 0x08000000 if IS_WINDOWS else 0
        item.tunnel = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{item.port}"],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        found: Dict[str, str] = {}

        def read():
            assert item.tunnel is not None and item.tunnel.stdout is not None
            for raw in iter(item.tunnel.stdout.readline, b""):
                m = TUNNEL_URL.search(raw.decode("utf-8", errors="replace"))
                if m and "url" not in found:
                    found["url"] = m.group(0)
        threading.Thread(target=read, daemon=True).start()
        end = time.time() + timeout
        while time.time() < end and "url" not in found and item.tunnel.poll() is None:
            time.sleep(0.2)
        if "url" not in found:
            self._kill_tunnel(item)
            raise RuntimeError("the public link could not be created (is the internet reachable?)")
        item.public_url = found["url"]
        return self.links(wid)

    def _kill_tunnel(self, item: _Product) -> None:
        if item.tunnel is not None:
            try:
                item.tunnel.kill()
                item.tunnel.wait(timeout=5)
            except Exception:
                pass
        item.tunnel, item.public_url = None, None

    def stop_sharing(self, wid: str) -> dict:
        item = self._items.get(wid)
        if item is None:
            return {}
        self._kill_tunnel(item)
        if item.lan:
            s = item.spec
            self.serve(wid, s.get("root"), s.get("command"), s.get("cwd"), s.get("env"), item.port, s.get("url_path"), lan=False)
        return self.links(wid)

    # ------------------------------------------------------------ stop
    def stop(self, wid: str) -> None:
        with self._lock:
            item = self._items.pop(wid, None)
        if item is None:
            return
        self._kill_tunnel(item)
        if item.static is not None:
            item.static.stop()
        if item.proc is not None:
            item.proc.stop()

    def stop_all(self) -> None:
        for wid in list(self._items):
            self.stop(wid)
