"""
tests/test_server_e2e.py -- runs the REAL command-bus server (uvicorn) and talks to it the
way the overlay does: HTTP + token-authenticated WebSocket.

    python tests/test_server_e2e.py      (needs fastapi, uvicorn, httpx, websockets)

Covers: access control (token, web-origin refusal), commands -> UI events, streamed chat
replies, the permission round-trip, 3D save/load/export, settings, neural points, history.
Uses temporary copies of the config and a temporary data folder; no audio, no Ollama.
"""
import asyncio
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import httpx
    import uvicorn
    import websockets
    from agent.server import JarvisServer, create_app
except Exception as e:  # pragma: no cover
    print(f"SKIP: web dependencies missing ({e})")
    sys.exit(0)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Harness:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis_e2e_")
        self.cfg = os.path.join(self.tmp, "jarvis.config.json")
        self.perms = os.path.join(self.tmp, "permissions.json")
        shutil.copy(os.path.join(ROOT, "config", "permissions.json"), self.perms)
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
        cfg["neural_core"]["active_neurons"] = 20000          # keep the test fast
        cfg["llm"]["host"] = "http://127.0.0.1:9"             # no Ollama: offline fallback
        json.dump(cfg, open(self.cfg, "w", encoding="utf-8"))
        self.data = os.path.join(self.tmp, "data")
        self.srv = JarvisServer(config_path=self.cfg, permissions_path=self.perms, data_dir=self.data)
        self.port = free_port()
        app = create_app(self.srv, start_voice=False)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="error", ws_max_size=8 * 1024 * 1024))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def ws_url(self, token=None):
        return f"ws://127.0.0.1:{self.port}/ws" + (f"?token={token}" if token else "")


async def recv_until(ws, pred, timeout=15.0):
    """Collect messages until pred(msg) is true; return the list (incl. the matching one)."""
    got = []
    end = time.time() + timeout
    while time.time() < end:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
        msg = json.loads(raw)
        got.append(msg)
        if pred(msg):
            return got
    raise AssertionError(f"timed out; got types {[m.get('type') for m in got]}")


async def expect_rejected(url, **kw):
    try:
        async with websockets.connect(url, **kw) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)
    except Exception:
        return True
    raise AssertionError(f"connection should have been refused: {url} {kw}")


async def scenario(h: Harness):
    token = h.srv.token
    assert open(os.path.join(h.data, "session.token")).read() == token

    # ---------------- HTTP access control
    async with httpx.AsyncClient() as c:
        assert (await c.get(h.base + "/health")).json() == {"ok": True, "version": "1.2.0"}
        assert (await c.get(h.base + "/status")).status_code == 401
        st = (await c.get(h.base + "/status", headers={"X-Jarvis-Token": token})).json()
        assert st["neurons_active"] >= 20000 and st["neurons_nominal"] == 200_000_000_000
        assert (await c.get(h.base + "/history?key=conversation", headers={"X-Jarvis-Token": "bad"})).status_code == 401
    print("  ok  HTTP: public /health, token-gated /status and /history")

    # ---------------- WebSocket access control
    await expect_rejected(h.ws_url())                                         # no token
    await expect_rejected(h.ws_url("wrong-token"))                            # bad token
    await expect_rejected(h.ws_url(token), origin="https://evil.example")    # web page origin
    print("  ok  WS: refused without token, with a bad token, and from a web origin")

    async with websockets.connect(h.ws_url(token), max_size=8 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and hello["status"]["neurons_active"] >= 20000
        assert hello["config"]["language"]["default"] == "en"
        print("  ok  WS: authenticated hello carries status + config")

        # ---------------- command -> UI event
        await ws.send(json.dumps({"type": "command", "text": "open the 3D project space"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "idle")
        assert any(m["type"] == "ui" and m["action"] == "open_space3d" for m in msgs)
        assert any(m["type"] == "transcript" and m.get("final") for m in msgs)
        print("  ok  command 'open the 3D project space' -> ui event + final transcript")

        # ---------------- streamed chat reply
        await ws.send(json.dumps({"type": "command", "text": "hello jarvis, how are you this evening?"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "idle")
        deltas = [m for m in msgs if m["type"] == "transcript_delta"]
        says = [m for m in msgs if m["type"] == "say"]
        final = [m for m in msgs if m["type"] == "transcript" and m.get("final")]
        assert len(deltas) >= 5 and len(says) >= 2 and final, (len(deltas), len(says))
        assert final[0]["text"].startswith("Very good, sir.")
        print(f"  ok  streamed reply: {len(deltas)} deltas, spoken in {len(says)} sentences")

        # ---------------- Korean -> Korean
        await ws.send(json.dumps({"type": "command", "text": "3D 공간 열어줘"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "idle")
        ko = [m for m in msgs if m["type"] == "say" and m.get("lang") == "ko"]
        assert ko and "3D" in ko[0]["text"], ko
        print(f"  ok  Korean command -> Korean reply: {ko[0]['text']!r}")

        # ---------------- permission round-trip (keyboard_input = ask) -> deny
        await ws.send(json.dumps({"type": "command", "text": "type hello world"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "permission_request")
        req = msgs[-1]
        assert req["category"] == "keyboard_input"
        await ws.send(json.dumps({"type": "permission_response", "request_id": req["request_id"], "granted": False}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "transcript" and m.get("final"))
        assert "hold off" in msgs[-1]["text"]
        print("  ok  permission dialog round-trip: denied -> JARVIS holds off")

        # ---------------- 3D project save / load / export
        project = {"version": 1, "objects": [{"type": "box", "color": 3727871, "position": [1, 2, 3],
                                              "rotation": [0, 0.5, 0], "scale": [1, 1, 1]}]}
        await ws.send(json.dumps({"type": "space3d_save", "project": project}))
        await recv_until(ws, lambda m: m.get("type") == "space3d_saved")
        assert json.load(open(os.path.join(h.data, "space3d", "project.json")))["objects"][0]["position"] == [1, 2, 3]
        await ws.send(json.dumps({"type": "space3d_load"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "space3d_project")
        assert msgs[-1]["project"]["objects"][0]["type"] == "box"
        glb = b"glTF" + (2).to_bytes(4, "little") + (20).to_bytes(4, "little") + b"\x00" * 8
        await ws.send(json.dumps({"type": "space3d_export", "filename": "../../my model?.glb",
                                  "data_b64": base64.b64encode(glb).decode()}))
        msgs = await recv_until(ws, lambda m: m.get("type") in ("space3d_exported", "error"))
        path = msgs[-1].get("path", "")
        assert msgs[-1]["type"] == "space3d_exported" and os.path.exists(path)
        assert path.startswith(os.path.join(h.data, "results", "files")) and path.endswith("my model_.glb")
        await ws.send(json.dumps({"type": "space3d_export", "filename": "x.glb",
                                  "data_b64": base64.b64encode(b"not a model").decode()}))
        msgs = await recv_until(ws, lambda m: m.get("type") in ("space3d_exported", "error"))
        assert msgs[-1]["type"] == "error"
        index = open(os.path.join(h.data, "results", "files", "index.jsonl"), encoding="utf-8").read()
        assert "3D model export" in index and "3D project save" in index
        print("  ok  3D: save -> load round-trip, .glb export archived (path traversal neutralised), bad data refused")

        # ---------------- settings
        await ws.send(json.dumps({"type": "get_settings"}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "settings")
        assert "voice.barge_in.mode" in msgs[-1]["schema"] and "permissions.read_files" in msgs[-1]["schema"]
        await ws.send(json.dumps({"type": "set_settings", "changes": {"language.default": "ko",
                                                                       "permissions.run_shell_elevated": "allow"}}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "settings_result")
        assert msgs[-1]["applied"] == {"language.default": "ko"}
        assert "permissions.run_shell_elevated" in msgs[-1]["rejected"]
        assert json.load(open(h.cfg, encoding="utf-8"))["language"]["default"] == "ko"
        print("  ok  settings: applied + persisted; locked permission refused")

        # ---------------- neural points + history
        await ws.send(json.dumps({"type": "neural_points", "max_points": 500}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "neural_points")
        assert len(msgs[-1]["positions"]) == 500 and len(msgs[-1]["module_names"]) == 6
        await ws.send(json.dumps({"type": "get_history", "key": "conversation", "n": 5}))
        msgs = await recv_until(ws, lambda m: m.get("type") == "history")
        assert len(msgs[-1]["records"]) == 5
        print("  ok  neural mesh points and conversation history over the bus")

    audit = open(os.path.join(h.data, "audit", "audit.log"), encoding="utf-8").read()
    assert audit.count('"ws_connect"') >= 3 and '"rejected"' in audit
    assert '"keyboard_input"' in audit and '"denied"' in audit
    print("  ok  audit log recorded refused connections and the denied permission")


def test_server_end_to_end():
    with Harness() as h:
        asyncio.run(scenario(h))


if __name__ == "__main__":
    test_server_end_to_end()
    print("\nserver end-to-end test passed")
