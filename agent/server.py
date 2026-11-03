"""
agent/server.py
==============
The local command bus (requirements 15, 16): a single WebSocket that every surface —
the holographic overlay, the voice engine, any tab — connects to. Voice input and text
input travel the exact same path, so every feature works by voice OR text, on any screen.

  ws://127.0.0.1:8765/ws        bidirectional event stream (JSON messages)
  GET /health                   liveness
  GET /config                   client-safe config subset
  GET /neural/points            downsampled 3D point cloud of the live brain mesh (viz)
  GET /history?key=&n=          recent records for the overlay History panel

Message types are documented in docs/ARCHITECTURE.md. The server owns:
  * ConnectionManager  — broadcast to all surfaces,
  * permission asker   — round-trips a holographic confirm dialog and waits,
  * interrupt handling — a new command or an explicit interrupt cancels the in-flight
                         reply and stops speech (requirement 13),
  * optional VoiceEngine — wake word / STT / British TTS, run in-process when audio
                         libraries are present (requirement 3, 7, 8, 21).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Dict, Optional, Set

from core.control_layer import ControlLayer
from core.brain_mesh import BrainMesh
from core.scale import resolve_scale
from recorder import Recorder
from .orchestrator import Orchestrator
from .permissions import PermissionManager, PermissionRequest
from .llm_ollama import OllamaLLM, MockLLM

CONFIG_PATH = os.environ.get("JARVIS_CONFIG", "config/jarvis.config.json")


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class ConnectionManager:
    def __init__(self):
        self.active: Set = set()

    async def connect(self, ws):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws):
        self.active.discard(ws)

    async def broadcast(self, event: dict):
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class JarvisServer:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_config()
        self.manager = ConnectionManager()
        self.recorder = Recorder()

        scale = resolve_scale(self.config.get("neural_core", {}).get("active_neurons", "auto"))
        self.mesh = BrainMesh(scale)
        self.control = ControlLayer(mesh=self.mesh)

        self.permissions = PermissionManager(recorder=self.recorder, asker=self._ask_user)
        self.llm = self._make_llm()

        self.orch = Orchestrator(self.config, self.llm, recorder=self.recorder,
                                 control=self.control, permissions=self.permissions,
                                 emit=self._emit)

        self._pending_perms: Dict[str, asyncio.Future] = {}
        self._current: Optional[asyncio.Task] = None
        self.voice = None  # set by attach_voice()

    # ------------------------------------------------------------- setup #
    def _make_llm(self):
        llm_cfg = self.config.get("llm", {})
        llm = OllamaLLM(host=llm_cfg.get("host", "http://127.0.0.1:11434"),
                        model=llm_cfg.get("model", "auto"),
                        candidates=llm_cfg.get("model_candidates", {}))
        if llm.health():
            return llm
        print("[jarvis] Ollama not reachable — using offline mock replies. "
              "Run INSTALL_JARVIS.bat / start Ollama for full conversation.")
        return MockLLM()

    # --------------------------------------------------------- emit/voice #
    async def _emit(self, event: dict):
        await self.manager.broadcast(event)
        # route spoken text to the in-process British voice, if available
        if event.get("type") == "say" and self.voice is not None:
            try:
                await self.voice.speak(event.get("text", ""), event.get("lang", "en"))
            except Exception:
                pass

    def attach_voice(self, engine):
        self.voice = engine

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
            await self.manager.broadcast({"type": "state", "state": "listening"})

    async def wake(self):
        await self.orch.handle_wake()

    # --------------------------------------------------- inbound dispatch #
    async def on_message(self, msg: dict):
        t = msg.get("type")
        if t == "command":
            await self.submit_command(msg.get("text", ""), msg.get("source", "text"))
        elif t == "wake":
            await self.wake()
        elif t == "interrupt":
            await self.interrupt(stop_speech=True)
        elif t == "feedback":
            await self.orch.handle_feedback(msg.get("signals", {}))
        elif t == "permission_response":
            fut = self._pending_perms.get(msg.get("request_id"))
            if fut and not fut.done():
                fut.set_result(bool(msg.get("granted")))
        elif t == "ui":  # overlay telling us about a 3D action done client-side
            self.recorder.log_command(f"[ui] {msg.get('action')}", source="ui")
        elif t == "ping":
            await self.manager.broadcast({"type": "pong"})


# --------------------------------------------------------------------------- #
# FastAPI app factory (imported lazily so the module is testable without web deps)
# --------------------------------------------------------------------------- #
def create_app(server: Optional[JarvisServer] = None):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    srv = server or JarvisServer()
    app = FastAPI(title="JARVIS", version="1.0.0")

    @app.on_event("startup")
    async def _startup():
        # start the voice engine in-process if audio libraries are available
        try:
            from voice.engine import VoiceEngine
            ve = VoiceEngine(srv.config,
                             on_wake=srv.wake,
                             on_command=lambda text: srv.submit_command(text, source="voice"),
                             on_interrupt=lambda: srv.interrupt(stop_speech=True))
            if ve.available:
                srv.attach_voice(ve)
                asyncio.create_task(ve.run())
                print("[jarvis] voice engine online (wake word + STT + British TTS)")
            else:
                print("[jarvis] voice engine offline (audio libs missing) — text still works")
        except Exception as e:
            print(f"[jarvis] voice engine not started: {e!r}")

    @app.get("/health")
    async def health():
        return {"ok": True, "llm": getattr(srv.llm, "model", "?"),
                "neurons_active": srv.mesh.N, "neurons_nominal": srv.mesh.scale.nominal_neurons,
                "voice": bool(srv.voice)}

    @app.get("/config")
    async def config():
        c = srv.config
        return {"identity": c.get("identity", {}), "language": c.get("language", {}),
                "overlay": c.get("overlay", {}), "space3d": c.get("space3d", {})}

    @app.get("/neural/points")
    async def neural_points(max_points: int = 3000):
        return srv.mesh.sample_points(max_points=max_points)

    @app.get("/history")
    async def history(key: str = "conversation", n: int = 50):
        try:
            return JSONResponse(srv.recorder.recent(key, n))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await srv.manager.connect(ws)
        await ws.send_text(json.dumps({"type": "hello", "config": await config(),
                                       "state": "idle"}))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await srv.on_message(msg)
        except WebSocketDisconnect:
            srv.manager.disconnect(ws)
        except Exception:
            srv.manager.disconnect(ws)

    app.state.server = srv
    return app


def main():
    import uvicorn
    cfg = load_config()
    srv = JarvisServer(cfg)
    app = create_app(srv)
    s = cfg.get("server", {})
    print(f"[jarvis] command bus on ws://{s.get('host','127.0.0.1')}:{s.get('port',8765)}/ws")
    uvicorn.run(app, host=s.get("host", "127.0.0.1"), port=int(s.get("port", 8765)),
                log_level="warning")


if __name__ == "__main__":
    main()
