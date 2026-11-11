"""
tests/test_core.py — fast unit tests runnable with pytest OR directly:
    python -m pytest tests/           # if pytest is installed
    python tests/test_core.py         # plain runner (no pytest needed)
Covers the parts that must hold regardless of OS/optional libs.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.synaptic_perceptron import SynapticPerceptron
from core.brain_mesh import BrainMesh
from core.scale import resolve_scale, NOMINAL_NEURONS
from core.control_layer import ControlLayer
from agent.nlu import parse_intent, detect_language, parse_action_tags
from recorder import Recorder


def test_synapse_makes_output_history_dependent():
    sp = SynapticPerceptron(n_inputs=4)
    x = np.array([1.0, 0.2, 0.8, 0.0])
    outs = [sp.forward(x) for _ in range(5)]
    # identical input must NOT give identical output (that's the synapse at work)
    assert len(set(round(o, 6) for o in outs)) > 1


def test_mesh_is_bounded_and_scaled():
    mesh = BrainMesh(resolve_scale(active_neurons=20000))
    mesh.inject_module("thalamus", np.ones(64))
    mesh.run(25)
    assert np.all(np.abs(mesh.a) <= 1.5)           # stable, bounded
    assert mesh.scale.nominal_neurons == NOMINAL_NEURONS
    assert 20000 * 0.5 <= mesh.N <= 20000 * 1.5    # active neurons near requested
    for m in ("cortex", "cerebellum", "thalamus", "brainstem", "hippocampus", "basal_ganglia"):
        assert m in mesh.module_range


def test_control_layer_flags_risky_commands():
    with tempfile.TemporaryDirectory() as d:
        ctl = ControlLayer(mesh=BrainMesh(resolve_scale(active_neurons=20000)), state_dir=d)
        safe = ctl.compute("how are you today?", risk=0.0).signals["caution"]
        risky = ctl.compute("delete everything in my documents now", risk=0.9).signals["caution"]
        assert risky > safe and risky > 0.5


def test_control_layer_learns():
    with tempfile.TemporaryDirectory() as d:
        ctl = ControlLayer(mesh=BrainMesh(resolve_scale(active_neurons=20000)), state_dir=d)
        before = ctl.compute("tell me about the weather").signals["verbosity"]
        for _ in range(3):
            ctl.compute("tell me about the weather"); ctl.learn({"verbosity": -1})
        after = ctl.compute("tell me about the weather").signals["verbosity"]
        assert after < before


def test_nlu_bilingual_intents():
    cases = {
        "play some jazz": "play_music", "open notepad": "open_app",
        "open the 3d project space": "open_space3d", "clone the object": "clone_object",
        "mute the microphone": "mute_mic", "노래 틀어줘": "play_music",
        "3d 공간 열어줘": "open_space3d", "메모장 열어줘": "open_app",
    }
    for text, want in cases.items():
        assert parse_intent(text).name == want, f"{text!r} -> {parse_intent(text).name}"
    assert detect_language("안녕하세요") == "ko"
    assert detect_language("hello there") == "en"


def test_nlu_action_tags():
    tags = parse_action_tags('Right away, sir. <action name="open_app">{"app":"chrome"}</action>')
    assert tags and tags[0].name == "open_app" and tags[0].args["app"] == "chrome"


def test_recorder_writes_every_category():
    with tempfile.TemporaryDirectory() as d:
        r = Recorder(d)
        cid = r.log_command("hi", source="voice")
        rid = r.start_run(cid, "chat")
        r.log_conversation("user", "hi"); r.log_explanation("answer", "because")
        r.log_options(["a"]); r.log_suggestions(["b"]); r.end_run(rid, "success")
        r.checkpoint({"k": 1}); r.audit("x", "read_files", "allow")
        for key in ("commands", "conversation", "options", "suggestions", "expl_answers", "expl_process"):
            assert r.recent(key, 5), f"no records for {key}"
        assert os.path.exists(os.path.join(d, "checkpoints", "latest.json"))
        assert os.path.exists(os.path.join(d, "audit", "audit.log"))


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn(); print(f"  PASS {fn.__name__}"); passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
