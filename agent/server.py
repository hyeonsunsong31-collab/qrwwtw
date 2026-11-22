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

START-UP (v1.4.1): the command bus claims its port first -- the configured one, or the next
free one if another program holds it (8765 is a popular default) -- and records the port,
its process id and its start-up phase in data/runtime.json, which the HUD, the launcher and
the supervisor read. Nothing on the event loop waits for the network or for model loading,
and the heavy warm-ups (speech, then the language model) run one after another in the
background so they do not fight over the CPU.

LANGUAGE ENGINE (v1.4.3): the brain keeps Ollama working by itself -- it starts it (or moves
it off a blocked port), downloads a missing model while using one that is installed, and
steps down to a smaller model when memory is short (agent/llm_ollama.py, agent/ollama_service.py).
/status reports llm_state / llm_problem / llm_note / llm_progress, the HUD and the launcher
show them, and JARVIS says what is wrong and how to fix it when the engine cannot be used.

VOICE (v1.4.2): the voice engine starts with the microphone and the wake word, and loads the
British voice and Whisper in the background. What it hears -- the microphone level, problems
(muted, blocked by Windows privacy, no device), near misses and commands it could not make
out -- arrives through on_voice_event(): the HUD gets a level meter and toasts, and JARVIS
says what is wrong and how to fix it instead of staying silent. /status carries the full
voice report ("voice_detail"), which the launcher and "check the microphone" read too.

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
import socket
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional, Set

_T_IMPORTS = time.time()          # before the heavy imports below (for the start-up timing line)

from core.control_layer import ControlLayer
from core.brain_mesh import build_mesh
from recorder import Recorder
from . import settings as settings_mod
from .orchestrator import Orchestrator, ACK
from .permissions import PermissionManager
from .llm_ollama import ResilientLLM, keep_alive_value
from .ollama_service import is_loopback

VERSION = "1.4.3"
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


PHASES = {
    "starting": "starting up",
    "neural-mesh": "building the neural mesh",
    "agent": "loading the agent (skills, memory, language-model client)",
    "listening": "ready",
    "stopped": "stopped",
    "failed": "failed",
}


class Runtime:
    """data/runtime.json: this backend's process id, port and start-up phase, so the HUD,
    the launcher and the supervisor know where to connect and what it is doing."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "runtime.json")
        self.body = {"pid": os.getpid(), "version": VERSION, "started": time.time(),
                     "phase": "starting", "timings": {}}

    def update(self, **fields) -> None:
        self.body.update(fields)
        self.body["updated"] = time.time()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.body, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def phase(self, name: str, **fields) -> None:
        self.body["timings"][name] = round(time.time() - self.body["started"], 2)
        self.update(phase=name, phase_text=PHASES.get(name, name), **fields)


def port_owner(port: int) -> str:
    """'name (pid N)' of the process listening on a loopback port, or ''."""
    try:
        import psutil  # type: ignore
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                try:
                    return f"{psutil.Process(c.pid).name()} (pid {c.pid})"
                except Exception:
                    return f"pid {c.pid}"
    except Exception:
        pass
    return ""


def bind_port(host: str, preferred: int, tries: int = 20):
    """Bind the command bus: the preferred port, else the next free one, else any free port.
    Returns (socket, port, note); note explains a fallback ('' when the preferred port was used).
    Binding first means a port conflict costs a second instead of a failed start-up."""
    last = None
    candidates = [preferred] + [p for p in range(preferred + 1, preferred + tries) if p != 8799] + [0]
    for port in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform.startswith("win") and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # no port hijacking
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)             # listening makes the claim exclusive on every OS
        except OSError as e:
            last = e
            sock.close()
            continue
        got = sock.getsockname()[1]
        if got == preferred:
            return sock, got, ""
        owner = port_owner(preferred)
        why = f"in use by {owner}" if owner else f"not available ({last})"
        return sock, got, f"port {preferred} is {why}; the command bus uses port {got} instead"
    raise OSError(f"no free port for the command bus near {preferred}: {last}")


# what JARVIS says when a spoken command did not come through (English, Korean)
VOICE_LINES = {
    "nothing": ("I didn't hear anything, sir.", "아무 말씀도 듣지 못했습니다, sir."),
    "unclear": ("Sorry, sir, I didn't catch that.", "죄송합니다, sir, 잘 알아듣지 못했습니다."),
    "faint": ("Sorry, sir, I could barely hear you - please speak up, or check the microphone's input volume.",
              "죄송합니다, sir, 말씀이 너무 작게 들립니다 - 조금 크게 말씀하시거나 마이크 입력 볼륨을 확인해 주세요."),
    "stt_loading": ("One moment, sir - my speech recognition is still loading.",
                    "잠시만요, sir - 음성 인식을 아직 불러오는 중입니다."),
    "stt_unavailable": ("My speech recognition isn't available, sir, so please type your command for now.",
                        "음성 인식을 사용할 수 없습니다, sir. 지금은 명령을 입력해 주세요."),
}
VOICE_STATE_TEXT = {
    "loading": "my voice is still starting up - give it a few seconds",
    "unavailable": "the audio libraries are missing - run INSTALL_JARVIS.bat",
    "failed": "the voice engine failed to start - see data/logs/backend.log",
    "off": "voice is turned off in Settings",
}


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
                 config_path: str = CONFIG_PATH, permissions_path: str = PERMISSIONS_PATH,
                 progress=None):
        progress = progress or (lambda phase: None)
        t0 = time.time()
        self.runtime: Optional[Runtime] = None
        self.port: Optional[int] = None
        self.config_path = config_path
        self.permissions_path = permissions_path
        self.config = config or load_config(config_path)
        self.data_dir = data_dir
        self.manager = ConnectionManager()
        self.recorder = Recorder(data_dir)

        nc = self.config.get("neural_core", {})
        settle = int(nc.get("settle_steps", 10))
        progress("neural-mesh")
        t_mesh = time.time()
        # sized by RAM AND by a per-command time budget: the mesh settles on every command
        self.mesh = build_mesh(nc.get("active_neurons", "auto"),
                               latency_budget_ms=float(nc.get("latency_budget_ms", 60) or 0) or None,
                               settle_steps=settle, log=lambda m: print("[jarvis] " + m, flush=True),
                               remember=os.path.join(data_dir, "brain", "mesh_size.json"))
        t_mesh = time.time() - t_mesh
        progress("agent")
        self.control = ControlLayer(mesh=self.mesh, settle_steps=settle,
                                    state_dir=os.path.join(data_dir, "brain"))
        self.permissions = PermissionManager(config_path=permissions_path,
                                             recorder=self.recorder, asker=self._ask_user)
        llm_cfg = self.config.get("llm", {})
        self.llm = ResilientLLM(host=llm_cfg.get("host", "http://127.0.0.1:11434"),
                                model=llm_cfg.get("model", "auto"),
                                candidates=llm_cfg.get("model_candidates", {}),
                                keep_alive=llm_cfg.get("keep_alive", "always"),
                                num_ctx=int(llm_cfg.get("context_tokens", 4096)),
                                data_dir=data_dir, on_model_change=self._remember_model,
                                manage=bool(llm_cfg.get("manage_engine", True)))
        self.orch = Orchestrator(self.config, self.llm, recorder=self.recorder,
                                 control=self.control, permissions=self.permissions,
                                 emit=self._emit)
        self.token = write_session_token(os.path.join(data_dir, "session.token"))
        from agent.skills import voicecheck, llmcheck, shell
        voicecheck.set_provider(self.voice_report)       # "check the microphone" reads the live report
        llmcheck.set_provider(self.llm)                  # "check the language engine" / "install ..."
        shell.set_permission_writer(self.apply_permission)   # "allow shell commands" (user only)
        self._pending_perms: Dict[str, asyncio.Future] = {}
        self._current: Optional[asyncio.Task] = None
        self.voice = None  # set by attach_voice()
        self.voice_state = "off"          # off | loading | online | unavailable | failed
        self.voice_error = ""
        self._voice_task: Optional[asyncio.Task] = None
        self._said_at: Dict[str, float] = {}     # spoken voice notices, rate-limited per kind
        self.started_at = time.time()
        self.startup = {"neural_mesh_s": round(t_mesh, 2), "init_s": round(time.time() - t0, 2)}

    # ------------------------------------------------------------- status #
    def status(self) -> dict:
        return {"ok": True, "version": VERSION, "pid": os.getpid(),
                "uptime": round(time.time() - self.started_at, 1),
                "overlay_clients": len(self.manager.active),
                "llm": self.llm.model_name(), "llm_online": self.llm.cached_online(),
                "llm_state": self.llm.state, "llm_error": self.llm.error or None,
                "llm_tokens_per_s": self.llm.stats.get("tokens_per_s"),
                "llm_problem": self.llm.problem, "llm_note": self.llm.note or None,
                "llm_progress": self.llm.progress, "llm_host": self.llm.service.base,
                "llm_version": self.llm.service.version,
                "llm_proxy_bypassed": bool(self.llm.service.proxy and is_loopback(self.llm.service.base)),
                "llm_gpu": self.llm.stats.get("gpu"), "llm_load_seconds": self.llm.stats.get("load_seconds"),
                "neural_settle_ms": getattr(self.mesh, "settle_ms", None),
                "port": self.port, "startup": self.startup,
                "voice": bool(self.voice), "voice_state": self.voice_state,
                "voice_error": self.voice_error or None,
                "voice_tts": bool(self.voice and self.voice.tts_ready()),
                "voice_detail": self._voice_detail(),
                "listening": bool(self.voice and self.voice.listening),
                "neurons_active": int(self.mesh.N),
                "neurons_nominal": int(self.mesh.scale.nominal_neurons),
                "synapses_active": int(self.mesh.E)}

    def _voice_detail(self) -> Optional[dict]:
        if self.voice is None:
            return None
        err = None
        for _ in range(3):           # read from another thread ("check the microphone"): retry a race
            try:
                return self.voice.status()
            except Exception as e:
                err = e
                time.sleep(0.01)
        return {"error": repr(err)[:200]}

    def voice_report(self) -> dict:
        """The full voice report for 'check the microphone', with the Windows settings read
        fresh (privacy switch, mute, input level)."""
        rep = {"engine": self.voice_state, "engine_error": self.voice_error or None}
        if self.voice is None:
            rep["reason"] = VOICE_STATE_TEXT.get(self.voice_state, self.voice_state)
            return rep
        rep.update(self._voice_detail() or {})
        try:
            from voice.mic import privacy_status, endpoint_status
            rep.setdefault("mic", {})["privacy"] = privacy_status()
            rep["mic"]["endpoint"] = endpoint_status()
        except Exception:
            pass
        return rep

    def input_device_choices(self):
        try:
            from voice.mic import device_choices
            sd = getattr(self.voice, "_sd", None)
            if sd is None:
                import sounddevice as sd  # type: ignore
            return device_choices(sd)
        except Exception:
            return ["default"]

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

    def apply_permission(self, category: str, level: str) -> dict:
        """Change one permission category (the user said "allow shell commands" and confirmed)."""
        result = settings_mod.apply_settings({f"permissions.{category}": level}, self.config_path,
                                             self.permissions_path)
        self.permissions.reload()
        self.recorder.audit("permission_change", category, level,
                            detail={"applied": result["applied"], "rejected": result["rejected"]})
        return result

    def _remember_model(self, model: str):
        """The language engine stepped down to a smaller model (memory): keep that choice."""
        try:
            settings_mod.apply_settings({"llm.model": model}, self.config_path, self.permissions_path)
            self.config.setdefault("llm", {})["model"] = model
            print(f"[jarvis] language model set to {model} (it fits this PC's memory)", flush=True)
        except Exception as e:
            print(f"[jarvis] could not save the language model choice: {e!r}", flush=True)

    async def on_llm_change(self, prev_state: str, prev_code: Optional[str]):
        """Say what keeps the language engine from working (once per problem per 10 minutes),
        and that it works again after it had a problem."""
        p = self.llm.problem or {}
        code = p.get("code")
        lasting = {"not_installed", "start_failed", "remote_down", "install_failed", "pull_failed",
                   "no_space", "memory", "load_failed"}
        lang = self.voice_lang()
        if code in lasting and code != prev_code and self._due("llm:" + code, 600):
            text = f"{p.get('message_ko')} {p.get('fix_ko')}" if lang == "ko" else f"{p.get('message')} {p.get('fix')}"
            try:
                self.recorder.log_explanation("error", f"language engine: {p.get('message')} {p.get('fix')}")
            except Exception:
                pass
            await self.notify(text.strip(), lang)
        elif self.llm.state == "ready" and prev_state != "ready" and self._said_at.get("llm_problem_spoken"):
            self._said_at.pop("llm_problem_spoken", None)
            await self.notify("My language engine is ready, sir." if lang != "ko" else "언어 엔진이 준비되었습니다, sir.", lang)
        if code in lasting:
            self._said_at["llm_problem_spoken"] = time.time()

    def attach_voice(self, engine):
        self.voice = engine
        self.voice_state = "online"

    def voice_lang(self) -> str:
        return getattr(self.orch, "last_lang", None) or self.orch.default_lang or "en"

    def _due(self, key: str, every: float) -> bool:
        now = time.time()
        if now - self._said_at.get(key, 0.0) < every:
            return False
        self._said_at[key] = now
        return True

    async def _say_line(self, key: str, every: float = 0.0):
        if every and not self._due("line:" + key, every):
            return
        en, ko = VOICE_LINES[key]
        lang = self.voice_lang()
        await self.notify(ko if lang == "ko" else en, lang)

    async def _say_problem(self, p: dict, every: float = 600.0):
        """Say what keeps JARVIS from hearing, and the fix (once per problem per 10 minutes)."""
        if not p or not self._due("problem:" + str(p.get("code")), every):
            return
        lang = self.voice_lang()
        text = (f"{p.get('message_ko')} {p.get('fix_ko')}" if lang == "ko"
                else f"{p.get('message')} {p.get('fix')}")
        await self.notify(text.strip(), lang)

    async def on_voice_event(self, ev: dict):
        """What the voice engine noticed -> the HUD (meter, toasts) and spoken explanations."""
        t = ev.get("type")
        if t == "mic":
            if self.manager.active:
                await self.manager.broadcast(ev)
            return
        if t == "voice_status":
            await self.manager.broadcast({"type": "status", "status": self.status()})
            return
        if t == "voice_problem":
            p = ev.get("problem") or {}
            await self.manager.broadcast(ev)
            await self.manager.broadcast({"type": "status", "status": self.status()})
            if ev.get("active"):
                try:
                    self.recorder.log_explanation("error", f"voice: {p.get('message')} {p.get('fix')}")
                except Exception:
                    pass
                if p.get("blocking"):
                    await self._say_problem(p)
            return
        if t == "voice_hint":
            await self.manager.broadcast(ev)
            if ev.get("code") == "stt_loading":
                await self._say_line("stt_loading", every=20)
            return
        if t == "voice_unheard":
            await self._voice_unheard(ev)

    async def _voice_unheard(self, ev: dict):
        reason, kind = ev.get("reason"), ev.get("kind")
        await self.manager.broadcast({**ev, "type": "voice_hint", "code": "unheard"})   # the HUD's toast
        if reason == "mic_problem":
            p = self.voice.blocking_problem() if self.voice is not None else None
            await self._say_problem(p, every=20)
        elif reason == "stt_unavailable":
            await self._say_line("stt_unavailable", every=60)
        elif reason == "no_speech":
            if (ev.get("voiced_frames") or 0) >= 3:
                await self._say_line("faint")
            elif kind == "ptt":
                await self._say_line("nothing")
            # after the wake reply, silence just means "never mind": JARVIS stands by quietly
        elif reason in ("too_short", "empty"):
            await self._say_line("unclear")

    async def supervise_voice(self, engine):
        """Run the voice loop, and start it again if it ever stops by accident."""
        crashes = []
        while True:
            try:
                await engine.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.voice_error = f"voice loop stopped: {e!r}"[:300]
                print(f"[jarvis] {self.voice_error}\n{traceback.format_exc()}", flush=True)
            if getattr(engine, "stopped", False) or self.voice is not engine:
                return
            now = time.time()
            crashes = [c for c in crashes if now - c < 600] + [now]
            if len(crashes) > 5:
                self.voice_state = "failed"
                self.voice_error = self.voice_error or "the voice loop keeps stopping"
                print("[jarvis] voice loop stopped 6 times in 10 minutes; giving up (typing still works)", flush=True)
                await self.manager.broadcast({"type": "status", "status": self.status()})
                return
            print("[jarvis] voice loop ended unexpectedly; restarting it", flush=True)
            await asyncio.sleep(2.0)

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
            # listen for the command right after "Jarvis at your service sir" is spoken; if the
            # microphone cannot hear anything, the engine reports why and JARVIS says so
            self.voice.begin_capture(after_speech=True)

    async def listen(self, ws=None):
        """Push-to-talk from the overlay mic button: capture one command with Whisper."""
        if self.voice is None:
            if ws is not None:
                await self.manager.send(ws, {"type": "listen_unavailable", "state": self.voice_state,
                                             "reason": self.voice_error or VOICE_STATE_TEXT.get(
                                                 self.voice_state, self.voice_state)})
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
        return settings_mod.get_settings(self.config_path, self.permissions_path,
                                         options={"voice.input_device": self.input_device_choices()})

    async def set_settings(self, changes: dict, ws=None):
        result = settings_mod.apply_settings(changes, self.config_path, self.permissions_path)
        self.config = load_config(self.config_path)
        self.orch.config = self.config
        self.orch.default_lang = self.config.get("language", {}).get("default", "en")
        self.llm.ollama.keep_alive = keep_alive_value(self.config.get("llm", {}).get("keep_alive", "always"))
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
        elif t == "open_settings_page":
            # the HUD's "Open settings" button on a microphone problem: only these two pages
            page = msg.get("page")
            if page in ("privacy-microphone", "sound"):
                from agent.skills import system as system_skill
                await asyncio.to_thread(system_skill.open_settings, page)
                self.recorder.log_command(f"[ui] open Windows settings: {page}", source="ui")
        elif t == "ping":
            await self.manager.send(ws, {"type": "pong"})


# --------------------------------------------------------------------------- #
# FastAPI app factory (web deps imported lazily so the module is testable without them)
# --------------------------------------------------------------------------- #
def create_app(server: Optional[JarvisServer] = None, start_voice: bool = True, warm_llm: bool = True):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
    from fastapi.responses import JSONResponse

    srv = server or JarvisServer()

    voice_done = asyncio.Event()      # the language-model warm-up waits for this (one at a time)

    async def start_voice_engine():
        """Start the voice off the event loop: the microphone and the wake word come up in a
        second or two; the British voice and Whisper load afterwards in the background (from
        seconds to minutes on a first run), and the command bus never waits for any of it."""
        srv.voice_state = "loading"
        try:
            def make():
                from voice.engine import VoiceEngine          # imported here, off the event loop
                return VoiceEngine(srv.config, on_wake=srv.wake,
                                   on_command=lambda text: srv.submit_command(text, source="voice"),
                                   on_interrupt=lambda: srv.interrupt(stop_speech=True),
                                   on_state=srv.on_voice_state, on_event=srv.on_voice_event, root=ROOT)

            ve = await asyncio.to_thread(make)
            if not ve.available:
                srv.voice_state = "unavailable"
                srv.voice_error = ve.unavailable_reason
                print(f"[jarvis] voice engine offline ({ve.unavailable_reason}) - text still works", flush=True)
                return
            srv.attach_voice(ve)
            srv._voice_task = asyncio.create_task(srv.supervise_voice(ve))
            print("[jarvis] voice engine started: microphone and wake word now, speech models loading", flush=True)
            await srv.manager.broadcast({"type": "status", "status": srv.status()})
            # warm the models up and pre-synthesise what JARVIS says most, so the wake reply
            # and the first command are as fast as every later one
            wake = srv.config.get("identity", {}).get("wake_response", "Jarvis at your service sir")
            t0 = time.time()
            done = await asyncio.to_thread(ve.load_models, [(wake, "en"), (ACK["en"], "en"),
                                                            ("Very good, sir.", "en"), (ACK["ko"], "ko")])
            st = ve.status()
            print(f"[jarvis] voice ready in {time.time() - t0:.1f}s: wake word "
                  f"{st['wake'].get('engine') or 'UNAVAILABLE'}, voice {st['tts'].get('backend') or st['tts'].get('state')}, "
                  f"speech recogniser {st['stt'].get('model')} {st['stt'].get('state')}"
                  f"{' (' + str(st['stt'].get('error')) + ')' if st['stt'].get('error') else ''}, "
                  f"{done.get('phrases', 0)} phrase(s) pre-synthesised; microphone "
                  f"{st['mic'].get('device') or 'not open: ' + str(st['mic'].get('reason'))}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            srv.voice_state = "failed"
            srv.voice_error = repr(e)[:300]
            print(f"[jarvis] voice engine not started: {e!r}\n{traceback.format_exc()}", flush=True)
        finally:
            voice_done.set()
        await srv.manager.broadcast({"type": "status", "status": srv.status()})

    async def keep_llm_warm():
        """Load the language model as soon as Ollama is up, read the system prompt into its
        cache, and load it again if Ollama restarts -- so a reply never waits for a cold model."""
        last = None
        # learn at once whether Ollama is up (for the HUD), but load the model only after the
        # speech models have loaded: on a laptop both at once take far longer than in turn
        try:
            await asyncio.to_thread(srv.llm.online)
            await srv.manager.broadcast({"type": "status", "status": srv.status()})
            await asyncio.wait_for(voice_done.wait(), timeout=45)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if warm_llm:
            from agent.skills import warm_up as warm_skills
            asyncio.get_running_loop().run_in_executor(None, warm_skills)
        while True:
            try:
                system = srv.orch.system_prompt(srv.orch.default_lang)
                prev_state, prev_code = srv.llm.state, (srv.llm.problem or {}).get("code")
                job = asyncio.ensure_future(asyncio.to_thread(srv.llm.ensure_warm, system))
                seen = None
                while not job.done():               # starting Ollama / downloading: keep the HUD posted
                    await asyncio.wait([job], timeout=2.0)
                    sig = (srv.llm.state, (srv.llm.problem or {}).get("code"),
                           int((srv.llm.progress or {}).get("percent") or 0))
                    if sig != seen and not job.done():
                        seen = sig
                        await srv.manager.broadcast({"type": "status", "status": srv.status()})
                changed = job.result()
                if srv.llm.state == "ready" and srv.llm.stats.get("warmed_at") and last != "ready":
                    st = srv.llm.stats
                    print(f"[jarvis] language model {st.get('model')} ready: loaded in {st.get('load_seconds')} s, "
                          f"{st.get('tokens_per_s')} tokens/s, {int(100 * (st.get('gpu') or 0))}% on the GPU", flush=True)
                elif srv.llm.state != last and srv.llm.state in ("error", "not_installed", "offline", "pulling"):
                    p = srv.llm.problem or {}
                    print(f"[jarvis] language engine {srv.llm.state}: {p.get('message') or srv.llm.error or ''} "
                          f"{p.get('fix') or ''}".rstrip(), flush=True)
                if changed or srv.llm.state != last:
                    last = srv.llm.state
                    await srv.manager.broadcast({"type": "status", "status": srv.status()})
                    await srv.on_llm_change(prev_state, prev_code)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[jarvis] warm-up check failed: {e!r}", flush=True)
            busy = srv.llm.state in ("pulling", "installing", "starting") or srv.llm.progress
            await asyncio.sleep(2 if busy else 60 if srv.llm.state == "ready" else 5)

    @asynccontextmanager
    async def lifespan(app):
        voice_task = None
        if start_voice and srv.config.get("voice", {}).get("enabled", True):
            voice_task = asyncio.create_task(start_voice_engine())
        else:
            voice_done.set()
        warm_task = asyncio.create_task(keep_llm_warm()) if warm_llm else None
        if srv.runtime is not None:
            srv.runtime.phase("listening")
        print(f"[jarvis] ready ({time.time() - srv.started_at:.1f}s)", flush=True)
        yield
        if srv.voice is not None:
            srv.voice.stop()
        for task in (voice_task, warm_task, srv._voice_task):
            if task is not None and not task.done():
                task.cancel()
        if srv.runtime is not None:
            srv.runtime.phase("stopped")

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


def _process_started() -> float:
    try:
        import psutil  # type: ignore
        return psutil.Process().create_time()
    except Exception:
        return _T_IMPORTS


def main():
    _ensure_stdio()
    t_main = time.time()
    print(f"[jarvis] backend {VERSION} starting (pid {os.getpid()}, python {sys.version.split()[0]})", flush=True)
    cfg = load_config()
    s = cfg.get("server", {})
    if s.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost"):
        print("[jarvis] refusing to bind the command bus to a non-loopback address; using 127.0.0.1")
    host = "127.0.0.1"                     # IPv4 loopback: what the HUD and the launcher connect to
    runtime = Runtime(DATA_DIR)
    # 1) claim a port before anything slow, so a conflict cannot stall the start-up
    try:
        sock, port, note = bind_port(host, int(s.get("port", 8765)))
    except OSError as e:
        runtime.phase("failed", error=str(e))
        print(f"[jarvis] {e}", flush=True)
        raise SystemExit(1)
    runtime.update(port=port, port_note=note or None)
    runtime.phase("starting")
    if note:
        print(f"[jarvis] {note}", flush=True)
    import uvicorn
    # 2) the brain itself
    srv = JarvisServer(cfg, progress=runtime.phase)
    srv.runtime, srv.port = runtime, port
    app = create_app(srv)
    started = _process_started()
    srv.startup.update(python_and_imports_s=round(t_main - started, 2), total_s=round(time.time() - started, 2))
    runtime.update(startup=srv.startup)
    print(f"[jarvis] start-up: python + imports {t_main - started:.1f}s, neural mesh {srv.startup['neural_mesh_s']:.1f}s, "
          f"agent {srv.startup['init_s'] - srv.startup['neural_mesh_s']:.1f}s -> serving after "
          f"{time.time() - started:.1f}s", flush=True)
    print(f"[jarvis] command bus on ws://{host}:{port}/ws (token in data/session.token)", flush=True)
    # 3) serve on the socket claimed in step 1
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", use_colors=False,
                            ws_max_size=MAX_EXPORT_BYTES * 2)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
