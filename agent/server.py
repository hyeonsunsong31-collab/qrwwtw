"""
agent/server.py
==============
The local command bus (requirements 15, 16): one WebSocket that every surface -- the
holographic overlay, the in-process voice engine, any tab -- uses. Voice and text input
travel the exact same path, so every feature works by voice OR text, on any screen.

  ws://127.0.0.1:8765/ws?token=...   bidirectional event stream (JSON messages)
  GET /health                        public liveness probe ({"ok": true} only)
  GET /status        (token)         LLM / voice / neural-mesh status, connected overlays
  POST /announce     (token)         ask the overlay to show itself (START_JARVIS.bat)
  GET /neural/points (token)         downsampled 3D point cloud of the live brain mesh
  GET /history       (token)         recent records for the History view

ACCESS CONTROL
  The bus binds to 127.0.0.1 only, and every connection must present the per-session
  token the server writes to data/session.token at start-up (a fresh random value each
  run). The Electron overlay reads that file through its preload script. Web pages are
  refused twice over: they can't read the token file, and handshakes carrying an
  http(s) Origin are rejected outright. This keeps the assistant's actions reachable
  only by the user's own JARVIS processes.

NOTE: no `from __future__ import annotations` here -- FastAPI must see the real Request /
WebSocket classes (imported inside create_app), not unresolvable annotation strings.
"""
import asyncio
import base64
import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional, Set

from core.control_layer import ControlLayer
from core.brain_mesh import BrainMesh
from core.scale import resolve_scale
from recorder import Recorder
from . import settings as settings_mod
from .orchestrator import Orchestrator
from .permissions import PermissionManager
from .llm_ollama import ResilientLLM

VERSION = "1.3.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rootpath(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


CONFIG_PATH = os.environ.get("JARVIS_CONFIG", rootpath("config", "jarvis.config.json"))
PERMISSIONS_PATH = os.environ.get("JARVIS_PERMISSIONS", rootpath("config", "permissions.json"))
DATA_DIR = os.environ.get("JARVIS_DATA", rootpath("data"))
MAX_PROJECT_BYTES = 8 * 1024 * 1024      # serialized 3D project JSON
MAX_EXPORT_BYTES = 64 * 1024 * 1024      # exported .glb model


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_session_token(path: str) -> str:
    """Write a fresh random token readable only by the current user; return it."""
    token = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(token)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return token


def origin_allowed(origin: Optional[str]) -> bool:
    """The overlay (file://) and native clients (no Origin) are allowed; web pages are not."""
    if not origin:
        return True
    o = origin.strip().lower()
    return not (o.startswith("http://") or o.startswith("https://"))


def token_matches(given: Optional[str], expected: str) -> bool:
    return bool(given) and hmac.compare_digest(str(given), expected)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9 _.-]+")


def safe_filename(name: str, ext: str) -> str:
    base = os.path.basename(str(name or "model")).strip() or "model"
    base = _SAFE_NAME.sub("_", base)[:80].strip(" .") or "model"
    if not base.lower().endswith(ext):
        base = os.path.splitext(base)[0] + ext
    return base


class ConnectionManager:
    def __init__(self):
        self.active: Set = set()

    def add(self, ws):
        self.active.add(ws)

    def discard(self, ws):
        self.active.discard(ws)

    async def send(self, ws, event: dict):
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            self.discard(ws)

    async def broadcast(self, event: dict):
        for ws in list(self.active):
            await self.send(ws, event)


class JarvisServer:
    def __init__(self, config: Optional[dict] = None, data_dir: str = DATA_DIR,
                 config_path: str = CONFIG_PATH, permissions_path: str = PERMISSIONS_PATH):
        self.config_path = config_path
        self.permissions_path = permissions_path
        self.config = config or load_config(config_path)
        self.data_dir = data_dir
        self.manager = ConnectionManager()
        self.recorder = Recorder(data_dir)

        scale = resolve_scale(self.config.get("neural_core", {}).get("active_neurons", "auto"))
        self.mesh = BrainMesh(scale)
        self.control = ControlLayer(mesh=self.mesh, state_dir=os.path.join(data_dir, "brain"))
        self.permissions = PermissionManager(config_path=permissions_path,
                                             recorder=self.recorder, asker=self._ask_user)
        llm_cfg = self.config.get("llm", {})
        self.llm = ResilientLLM(host=llm_cfg.get("host", "http://127.0.0.1:11434"),
                                model=llm_cfg.get("model", "auto"),
                                candidates=llm_cfg.get("model_candidates", {}))
        self.orch = Orchestrator(self.config, self.llm, recorder=self.recorder,
                                 control=self.control, permissions=self.permissions,
                                 emit=self._emit)
        self.token = write_session_token(os.path.join(data_dir, "session.token"))
        self._pending_perms: Dict[str, asyncio.Future] = {}
        self._current: Optional[asyncio.Task] = None
        self.voice = None  # set by attach_voice()
        self.voice_state = "off"          # off | loading | online | unavailable | failed
        self.started_at = time.time()

    # ------------------------------------------------------------- status #
    def status(self) -> dict:
        return {"ok": True, "version": VERSION, "pid": os.getpid(),
                "uptime": round(time.time() - self.started_at, 1),
                "overlay_clients": len(self.manager.active),
                "llm": self.llm.model_name(), "llm_online": self.llm.online(),
                "voice": bool(self.voice), "voice_state": self.voice_state,
                "listening": bool(self.voice and self.voice.listening),
                "neurons_active": int(self.mesh.N),
                "neurons_nominal": int(self.mesh.scale.nominal_neurons),
                "synapses_active": int(self.mesh.E)}

    def client_config(self) -> dict:
        c = self.config
        return {"identity": c.get("identity", {}), "language": c.get("language", {}),
                "overlay": c.get("overlay", {}), "space3d": c.get("space3d", {}),
                "barge_in": c.get("voice", {}).get("barge_in", {}).get("mode", "keyword")}

    # --------------------------------------------------------- emit/voice #
    async def _emit(self, event: dict):
        await self.manager.broadcast(event)
        # queue spoken text for the in-process British voice (non-blocking, so a streamed
        # reply keeps generating while earlier sentences are spoken)
        if event.get("type") == "say" and self.voice is not None:
            try:
                self.voice.enqueue(event.get("text", ""), event.get("lang", "en"))
            except Exception:
                pass

    async def on_voice_state(self, state: str):
        await self.manager.broadcast({"type": "state", "state": state, "source": "voice"})

    def attach_voice(self, engine):
        self.voice = engine
        self.voice_state = "online"

    async def announce(self, reason: str = "launcher") -> int:
        """Ask every connected overlay to show its 'JARVIS online' banner. Returns how many."""
        await self.manager.broadcast({"type": "ui", "action": "announce", "args": {"reason": reason}})
        return len(self.manager.active)

    async def notify(self, text: str, lang: str = "en"):
        """Speak + show a short assistant line that isn't the reply to a command."""
        self.recorder.log_conversation("assistant", text, lang=lang)
        await self._emit({"type": "transcript", "role": "assistant", "text": text, "lang": lang})
        await self._emit({"type": "say", "text": text, "lang": lang})

    # ------------------------------------------------- permission asker #
    async def _ask_user(self, request: dict) -> bool:
        rid = "perm_" + uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_perms[rid] = fut
        await self.manager.broadcast({"type": "permission_request", "request_id": rid, **request})
        try:
            return await asyncio.wait_for(fut, timeout=90)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_perms.pop(rid, None)

    # ------------------------------------------------- command handling #
    async def submit_command(self, text: str, source: str = "text"):
        # a new command interrupts any in-flight reply (requirement 13)
        await self.interrupt(stop_speech=True)
        self._current = asyncio.create_task(self.orch.handle_command(text, source=source))

    async def interrupt(self, stop_speech: bool = True):
        if self._current and not self._current.done():
            self._current.cancel()
            try:
                await self._current
            except (asyncio.CancelledError, Exception):
                pass
        if stop_speech:
            if self.voice is not None:
                try:
                    self.voice.stop_speaking()
                except Exception:
                    pass
            await self.manager.broadcast({"type": "stop_tts"})

    async def wake(self):
        """Voice wake word, WAKE button and Ctrl+Alt+J all land here (requirements 7-9)."""
        await self.orch.handle_wake()
        if self.voice is not None:
            # listen for the command right after "Jarvis at your service sir" is spoken
            self.voice.begin_capture(after_speech=True)

    async def listen(self, ws=None):
        """Push-to-talk from the overlay mic button: capture one command with Whisper."""
        if self.voice is None:
            if ws is not None:
                await self.manager.send(ws, {"type": "listen_unavailable"})
            return
        await self.interrupt(stop_speech=True)
        self.voice.begin_capture(after_speech=False)

    # ------------------------------------------------------- 3D projects #
    def _space3d_dir(self) -> str:
        d = os.path.join(self.data_dir, "space3d")
        os.makedirs(d, exist_ok=True)
        return d

    async def save_project(self, project, ws=None):
        blob = json.dumps(project, ensure_ascii=False)
        if not isinstance(project, dict) or len(blob.encode("utf-8")) > MAX_PROJECT_BYTES:
            return await self.manager.send(ws, {"type": "error", "message": "invalid project"})
        d = self._space3d_dir()
        latest = os.path.join(d, "project.json")
        with open(latest + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(blob)
        os.replace(latest + ".tmp", latest)
        stored = self.recorder.register_file(latest, copy=True, note="3D project save")
        await self.manager.send(ws, {"type": "space3d_saved", "path": stored})
        await self.notify("Project saved, sir.")

    async def load_project(self, ws):
        p = os.path.join(self._space3d_dir(), "project.json")
        project = None
        try:
            with open(p, encoding="utf-8") as fh:
                project = json.load(fh)
        except (OSError, ValueError):
            pass
        await self.manager.send(ws, {"type": "space3d_project", "project": project})

    async def export_model(self, filename: str, data_b64: str, ws=None):
        try:
            raw = base64.b64decode(data_b64 or "", validate=True)
        except (ValueError, TypeError):
            return await self.manager.send(ws, {"type": "error", "message": "invalid model data"})
        if not raw or len(raw) > MAX_EXPORT_BYTES or raw[:4] != b"glTF":
            return await self.manager.send(ws, {"type": "error", "message": "not a .glb model"})
        day = os.path.join(self.data_dir, "results", "files", datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(day, exist_ok=True)
        path = os.path.join(day, f"{datetime.now().strftime('%H%M%S')}_{safe_filename(filename, '.glb')}")
        with open(path, "wb") as fh:
            fh.write(raw)
        self.recorder.register_file(path, copy=False, note="3D model export (.glb)")
        await self.manager.send(ws, {"type": "space3d_exported", "path": path})
        await self.notify(f"Model exported to {os.path.basename(path)}, sir.")

    # ---------------------------------------------------------- settings #
    def get_settings(self) -> dict:
        return settings_mod.get_settings(self.config_path, self.permissions_path)

    async def set_settings(self, changes: dict, ws=None):
        result = settings_mod.apply_settings(changes, self.config_path, self.permissions_path)
        self.config = load_config(self.config_path)
        self.orch.config = self.config
        self.orch.default_lang = self.config.get("language", {}).get("default", "en")
        if self.orch.selfmodel is not None:
            self.orch.selfmodel.enabled = self.orch._self_learning()
        self.permissions.reload()
        if self.voice is not None:
            try:
                self.voice.reconfigure(self.config)
            except Exception:
                pass
        self.recorder.audit("settings", "settings", "applied",
                            detail={"applied": result["applied"], "rejected": result["rejected"]})
        await self.manager.send(ws, {"type": "settings_result", **result})
        await self.manager.send(ws, {"type": "settings", **self.get_settings()})

    # --------------------------------------------------- inbound dispatch #
    async def on_message(self, msg: dict, ws=None):
        t = msg.get("type")
        if t == "command":
            await self.submit_command(msg.get("text", ""), msg.get("source", "text"))
        elif t == "wake":
            await self.wake()
        elif t == "listen":
            await self.listen(ws)
        elif t == "interrupt":
            await self.interrupt(stop_speech=True)
        elif t == "feedback":
            await self.orch.handle_feedback(msg.get("signals", {}))
        elif t == "permission_response":
            fut = self._pending_perms.get(msg.get("request_id"))
            if fut and not fut.done():
                fut.set_result(bool(msg.get("granted")))
        elif t == "ui":
            action = msg.get("action")
            if action == "ai_listen" and self.voice is not None:
                enabled = bool((msg.get("args") or {}).get("enabled", True))
                self.voice.set_listening(enabled)
                await self.manager.broadcast({"type": "state", "state": "idle" if enabled else "muted"})
            self.recorder.log_command(f"[ui] {action}", source="ui")
        elif t == "neural_points":
            n = max(200, min(6000, int(msg.get("max_points", 3000))))
            await self.manager.send(ws, {"type": "neural_points", **self.mesh.sample_points(n)})
        elif t == "space3d_save":
            await self.save_project(msg.get("project"), ws)
        elif t == "space3d_load":
            await self.load_project(ws)
        elif t == "space3d_export":
            await self.export_model(msg.get("filename", "model.glb"), msg.get("data_b64", ""), ws)
        elif t == "get_settings":
            await self.manager.send(ws, {"type": "settings", **self.get_settings()})
        elif t == "set_settings":
            await self.set_settings(msg.get("changes") or {}, ws)
        elif t == "get_history":
            key = msg.get("key", "conversation")
            try:
                recs = self.recorder.recent(key, max(1, min(500, int(msg.get("n", 50)))))
            except Exception:
                recs = []
            await self.manager.send(ws, {"type": "history", "key": key, "records": recs})
        elif t == "ping":
            await self.manager.send(ws, {"type": "pong"})


# --------------------------------------------------------------------------- #
# FastAPI app factory (web deps imported lazily so the module is testable without them)
# --------------------------------------------------------------------------- #
def create_app(server: Optional[JarvisServer] = None, start_voice: bool = True):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
    from fastapi.responses import JSONResponse

    srv = server or JarvisServer()

    async def start_voice_engine():
        """Load the speech models off the event loop. They can take from seconds to minutes
        (first run, slow disk), and the command bus must not wait for them: the overlay
        connects -- and typed commands work -- as soon as the server is up."""
        srv.voice_state = "loading"
        try:
            from voice.engine import VoiceEngine
            ve = await asyncio.to_thread(
                lambda: VoiceEngine(srv.config, on_wake=srv.wake,
                                    on_command=lambda text: srv.submit_command(text, source="voice"),
                                    on_interrupt=lambda: srv.interrupt(stop_speech=True),
                                    on_state=srv.on_voice_state, root=ROOT))
            if ve.available:
                srv.attach_voice(ve)
                asyncio.create_task(ve.run())
                print("[jarvis] voice engine online (wake word + STT + British TTS)", flush=True)
            else:
                srv.voice_state = "unavailable"
                print("[jarvis] voice engine offline (audio libraries missing) - text still works", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            srv.voice_state = "failed"
            print(f"[jarvis] voice engine not started: {e!r}", flush=True)
        await srv.manager.broadcast({"type": "status", "status": srv.status()})

    @asynccontextmanager
    async def lifespan(app):
        voice_task = None
        if start_voice and srv.config.get("voice", {}).get("enabled", True):
            voice_task = asyncio.create_task(start_voice_engine())
        print(f"[jarvis] ready ({time.time() - srv.started_at:.1f}s)", flush=True)
        yield
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
        if srv.voice is not None:
            srv.voice.stop()

    app = FastAPI(title="JARVIS", version=VERSION, lifespan=lifespan)

    def require_token(request: Request):
        given = request.headers.get("x-jarvis-token") or request.query_params.get("token")
        if not token_matches(given, srv.token):
            raise HTTPException(status_code=401, detail="missing or invalid token")

    @app.get("/health")
    async def health():
        return {"ok": True, "version": VERSION}

    @app.get("/status")
    async def status(request: Request):
        require_token(request)
        return srv.status()

    @app.post("/announce")
    async def announce(request: Request):
        require_token(request)
        return {"ok": True, "clients": await srv.announce("launcher")}

    @app.get("/neural/points")
    async def neural_points(request: Request, max_points: int = 3000):
        require_token(request)
        return srv.mesh.sample_points(max_points=max(200, min(6000, max_points)))

    @app.get("/history")
    async def history(request: Request, key: str = "conversation", n: int = 50):
        require_token(request)
        try:
            return JSONResponse(srv.recorder.recent(key, max(1, min(500, n))))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin")
        if not origin_allowed(origin) or not token_matches(ws.query_params.get("token"), srv.token):
            srv.recorder.audit("ws_connect", "security", "rejected", detail={"origin": origin})
            await ws.close(code=1008)  # policy violation; handshake is refused
            return
        await ws.accept()
        srv.manager.add(ws)
        await srv.manager.send(ws, {"type": "hello", "config": srv.client_config(),
                                    "status": srv.status(), "state": "idle",
                                    "self": srv.orch.self_summary()})
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await srv.on_message(msg, ws)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            srv.manager.discard(ws)

    app.state.server = srv
    return app


def _ensure_stdio():
    """pythonw.exe and detached starts give a process no stdout/stderr at all (None).
    uvicorn's logging set-up calls sys.stdout.isatty() and would crash before the server
    ever starts, without a trace. Give such a process the null device instead."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main():
    _ensure_stdio()
    import uvicorn
    print(f"[jarvis] backend {VERSION} starting (pid {os.getpid()}, python {sys.version.split()[0]})", flush=True)
    cfg = load_config()
    srv = JarvisServer(cfg)
    app = create_app(srv)
    s = cfg.get("server", {})
    host = s.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("[jarvis] refusing to bind the command bus to a non-loopback address; using 127.0.0.1")
        host = "127.0.0.1"
    port = int(s.get("port", 8765))
    print(f"[jarvis] command bus on ws://{host}:{port}/ws (token in data/session.token)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning", use_colors=False,
                ws_max_size=MAX_EXPORT_BYTES * 2)


if __name__ == "__main__":
    main()
