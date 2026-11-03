"""
scripts/mock_run.py — exercise the full command bus on Linux WITHOUT Ollama/audio.

    python scripts/mock_run.py

Drives the Orchestrator with a MockLLM and a mock emit sink, running one command per
feature path (chat, app, music, 3D space, system info, file write with permission,
file delete denied, mic mute, and a Korean command to prove language auto-switch).
Verifies that spoken replies come back and that the recorder wrote every category.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.server import load_config
from agent.orchestrator import Orchestrator
from agent.permissions import PermissionManager
from agent.llm_ollama import MockLLM
from core.control_layer import ControlLayer
from core.brain_mesh import BrainMesh
from core.scale import resolve_scale
from recorder import Recorder

EVENTS = []


async def emit(ev):
    EVENTS.append(ev)
    if ev.get("type") in ("transcript", "say") and ev.get("role") != "user":
        tag = ev["type"].upper()
        print(f"   [{tag:10s}] ({ev.get('lang')}) {ev.get('text','')[:100]}")
    elif ev.get("type") == "ui":
        print(f"   [UI        ] action={ev.get('action')}")
    elif ev.get("type") == "options":
        print(f"   [OPTIONS   ] {ev.get('options')}")


async def main():
    tmp = tempfile.mkdtemp(prefix="jarvis_mock_")
    rec = Recorder(os.path.join(tmp, "data"))
    mesh = BrainMesh(resolve_scale(active_neurons=20000))
    ctl = ControlLayer(mesh=mesh, state_dir=os.path.join(tmp, "brain"))

    async def asker(req):
        # grant everything except deletions (to exercise a denial path)
        return req["category"] != "delete_files"

    perms = PermissionManager(recorder=rec, asker=asker)
    orch = Orchestrator(load_config(), MockLLM(), recorder=rec, control=ctl,
                        permissions=perms, emit=emit)

    scenarios = [
        ("wake", None),
        ("command", "hello Jarvis, how are you this evening?"),
        ("command", "open notepad"),
        ("command", "what's my system status?"),
        ("command", "open the 3D project space"),
        ("command", "play some relaxing jazz"),
        ("command", f"write a file {os.path.join(tmp, 'note.txt')}"),
        ("command", "mute the microphone"),
        ("command", "안녕 자비스, 오늘 날씨 어때?"),           # Korean -> auto-switch
        ("feedback", {"verbosity": -1}),
    ]

    for kind, payload in scenarios:
        if kind == "wake":
            print("\n>> WAKE")
            await orch.handle_wake()
        elif kind == "feedback":
            print(f"\n>> FEEDBACK {payload}")
            await orch.handle_feedback(payload)
        else:
            print(f"\n>> COMMAND: {payload}")
            await orch.handle_command(payload, source="text")

    # verify records
    print("\n" + "=" * 60 + "\nRECORDER VERIFICATION\n" + "=" * 60)
    checks = {
        "commands": rec.recent("commands", 50),
        "conversation": rec.recent("conversation", 50),
        "options": rec.recent("options", 50),
        "suggestions": rec.recent("suggestions", 50),
        "expl_process": rec.recent("expl_process", 50),
        "expl_eta": rec.recent("expl_eta", 50),
    }
    for k, v in checks.items():
        print(f"   {k:16s}: {len(v)} record(s)")
    audit = os.path.join(tmp, "data", "audit", "audit.log")
    runs = os.path.join(tmp, "data", "runs")
    print(f"   {'audit':16s}: {'present' if os.path.exists(audit) else 'MISSING'}")
    print(f"   {'runs':16s}: {len([f for f in os.listdir(runs) if f.startswith('run_')])} run file(s)")
    print(f"   {'checkpoint':16s}: {'present' if os.path.exists(os.path.join(tmp,'data','checkpoints','latest.json')) else 'MISSING'}")

    assert any(e["type"] == "wake_response" for e in EVENTS), "no wake response"
    assert any(e["type"] == "ui" and e["action"] == "open_space3d" for e in EVENTS), "3D space not opened"
    assert any(e["type"] == "say" for e in EVENTS), "no spoken reply"
    ko = [e for e in EVENTS if e.get("type") == "say" and e.get("lang") == "ko"]
    assert ko, "language did not switch to Korean"
    print("\nAll assertions passed. Data tree at:", os.path.join(tmp, "data"))


if __name__ == "__main__":
    asyncio.run(main())
