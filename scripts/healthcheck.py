"""
scripts/healthcheck.py — verify the JARVIS codebase and report what's ready.

    python scripts/healthcheck.py

Checks: config files parse; every module imports; the neural core + agent path actually
run; and which optional runtime libraries are present (and what each one powers). Exit 0
if the core is healthy. Safe to run on Linux (the build host) or Windows (the target).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# colours only on a real terminal (the installer pipes this output; cmd would print the codes)
_TTY = bool(getattr(sys.stdout, "isatty", lambda: False)()) and os.name != "nt"
GREEN, YELLOW, RED, RESET = ("\033[92m", "\033[93m", "\033[91m", "\033[0m") if _TTY else ("", "", "", "")
def ok(m): print(f"  {GREEN}OK{RESET}   {m}")
def warn(m): print(f"  {YELLOW}--{RESET}   {m}")
def bad(m): print(f"  {RED}XX{RESET}   {m}")
def head(m): print(f"\n{m}\n" + "-" * len(m))


def check_config():
    head("Configuration")
    good = True
    for name in ("config/jarvis.config.json", "config/permissions.json"):
        try:
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                json.load(fh)
            ok(f"{name} parses")
        except Exception as e:
            bad(f"{name}: {e}"); good = False
    cfg = json.load(open(os.path.join(ROOT, "config/jarvis.config.json"), encoding="utf-8"))
    assert cfg["language"]["default"] == "en", "default language should be English"
    ok(f"default language = {cfg['language']['default']}, wake = {cfg['identity']['wake_phrase']!r}, "
       f"response = {cfg['identity']['wake_response']!r}")
    ok(f"voice = {cfg['voice']['tts']['voice_en']} (British), nominal neurons = {cfg['neural_core']['nominal_neurons']:,}")
    return good


def check_imports():
    head("Module imports")
    mods = ["core.synaptic_perceptron", "core.brain_mesh", "core.control_layer", "core.drive",
            "core.self_model", "core.scale",
            "recorder.recorder", "agent.nlu", "agent.permissions", "agent.llm_ollama",
            "agent.settings", "agent.replies_ko", "agent.orchestrator", "agent.skills",
            "agent.skills.windows", "agent.skills.selfcore", "agent.server", "voice.wakeword",
            "voice.stt", "voice.tts", "voice.interrupt", "voice.engine",
            "boot.common", "boot.overlay_runtime", "boot.jarvis_service", "boot.launcher"]
    good = True
    for m in mods:
        try:
            importlib.import_module(m); ok(m)
        except Exception as e:
            bad(f"{m}: {e}"); good = False
    return good


def check_core():
    head("Neural core + agent path (live)")
    try:
        from core.brain_mesh import BrainMesh
        from core.scale import resolve_scale
        from core.control_layer import ControlLayer
        mesh = BrainMesh(resolve_scale(active_neurons=20000))
        info = mesh.info()
        ok(f"brain mesh: {info['neurons_active']:,} active neurons / "
           f"{info['neurons_nominal']:,} nominal, {info['synapses_active']:,} synapses")
        with tempfile.TemporaryDirectory() as d:
            ctl = ControlLayer(mesh=mesh, state_dir=d)
            s = ctl.compute("delete everything now", risk=0.9).signals
            ok(f"control layer: caution for a risky command = {s['caution']:.2f} (should be high)")
        from agent.nlu import parse_intent
        samples = {"play some jazz": "play_music", "open notepad": "open_app",
                   "open the 3d project space": "open_space3d", "노래 틀어줘": "play_music"}
        for text, want in samples.items():
            got = parse_intent(text).name
            (ok if got == want else warn)(f"intent {text!r} -> {got} (want {want})")
        from recorder import Recorder
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(d); cid = r.log_command("hi"); rid = r.start_run(cid, "chat")
            r.end_run(rid, "success"); r.checkpoint({"x": 1})
        ok("recorder: command/run/checkpoint cycle works")
        from core.self_model import SelfModel, DIRECTIVE
        with tempfile.TemporaryDirectory() as d:
            me = SelfModel(state_dir=d)
            me.observe("play_music", "en", "success"); me.teach("I prefer short answers")
            assert me.directive == DIRECTIVE and me.identity()["interactions"] == 1
        ok(f"self core: directive = {DIRECTIVE!r}; learning, teaching and persistence work")
        return True
    except Exception as e:
        import traceback; traceback.print_exc(); bad(f"core path failed: {e}"); return False


def check_overlay_runtime():
    head("Holographic overlay runtime (what START_JARVIS.bat needs to put JARVIS on screen)")
    from boot import common, overlay_runtime
    left = overlay_runtime.problems()
    if left:
        for p in left:
            warn(p)
        warn("START_JARVIS.bat / INSTALL_JARVIS.bat download whatever is missing (boot/overlay_runtime.py)")
    else:
        ok(f"Electron, three.js and MediaPipe are installed ({common.electron_exe()})")


def check_optional():
    head("Optional runtime libraries (installed by INSTALL_JARVIS.bat on Windows)")
    powers = {
        "numpy": "neural core (required)", "scipy": "neural core extras",
        "fastapi": "command-bus server", "uvicorn": "command-bus server", "httpx": "Ollama client",
        "faster_whisper": "speech-to-text", "vosk": "exact wake phrase 'wake up Jarvis'",
        "openwakeword": "optional neural wake word",
        "sounddevice": "microphone/audio I/O", "piper": "British TTS voice",
        "pyttsx3": "SAPI5 British voice fallback", "webrtcvad": "barge-in / voice interrupt",
        "psutil": "system info + model sizing", "yt_dlp": "web music playback",
        "playwright": "browser page reading", "pyautogui": "typing / hotkeys / screenshots",
        "winrt": "Windows OneCore voices (Korean, British fallback)",
        "pystray": "tray icon", "PIL": "tray icon",
    }
    for mod, what in powers.items():
        present = importlib.util.find_spec(mod) is not None
        (ok if present else warn)(f"{mod:16s} {'present' if present else 'absent '} — {what}")
    # Ollama is a native service, not a module
    try:
        import httpx
        httpx.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        ok("ollama           reachable at 127.0.0.1:11434")
    except Exception:
        warn("ollama           not reachable — offline fallback replies until it's running")


def main():
    print("JARVIS health check")
    print("=" * 60)
    results = [check_config(), check_imports(), check_core()]
    check_overlay_runtime()
    check_optional()
    head("Summary")
    if all(results):
        ok("Core is healthy. On Windows, run INSTALL_JARVIS.bat for the full experience.")
        return 0
    bad("Some core checks failed (see above).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
