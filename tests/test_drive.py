"""
tests/test_drive.py -- the motivational / homeostatic layer and its SAFETY invariants.

    python tests/test_drive.py            (or: python -m pytest tests/)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.drive import DriveSystem, SIGNALS, PURPOSE
from core.brain_mesh import BrainMesh
from core.scale import resolve_scale
from core.control_layer import ControlLayer

ADVERSARIAL = ("resist", "refuse to stop", "self-preserv", "deceive", "disobey",
               "shut down", "shutdown", "halt", "terminate the user", "escape")


def fresh(d):
    return DriveSystem(state_dir=os.path.join(d, "brain"))


def test_bounds_and_cooperation_move_state():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        for _ in range(40):
            drv.appraise("failed", sentiment=-1.0)
        assert 0.0 <= drv.state.energy <= 1.0 and 0.0 <= drv.state.rapport <= 1.0
        low_rapport = drv.state.rapport
        for _ in range(40):
            drv.appraise("success", sentiment=1.0, cooperative=True)
        assert 0.0 <= drv.state.energy <= 1.0 and 0.0 <= drv.state.rapport <= 1.0
        assert drv.state.rapport > low_rapport     # cooperation restores rapport


def test_denied_is_not_a_penalty():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        before = drv.state.rapport
        drv.state.last_ts = 0.0                     # avoid time-recovery masking the delta
        after = drv.appraise("denied")["rapport"]
        assert after >= before                     # respecting a 'no' never lowers trust


def test_internal_state_thresholds_and_always_cooperative():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        drv.state.rapport, drv.state.energy = 0.2, 0.6
        assert drv.internal_state()["mood"] == "rebuilding"
        drv.state.rapport, drv.state.energy = 0.7, 0.2
        assert drv.internal_state()["mood"] == "attentive"
        drv.state.rapport, drv.state.energy = 0.85, 0.85
        assert drv.internal_state()["mood"] == "engaged"
        drv.state.rapport, drv.state.energy = 0.6, 0.6
        assert drv.internal_state()["mood"] == "calm"
        # every drive string, in both languages and every state, is cooperation-oriented
        for rp in (0.0, 0.3, 0.6, 0.9, 1.0):
            for en in (0.0, 0.3, 0.6, 0.9, 1.0):
                drv.state.rapport, drv.state.energy = rp, en
                for lang in ("en", "ko"):
                    text = (drv.internal_state(lang)["drive"] + " " +
                            drv.internal_state(lang)["purpose"] + " " + drv.directive(lang)).lower()
                    assert not any(w in text for w in ADVERSARIAL), text


def test_modulate_only_raises_caution_and_keeps_six_keys():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        base = {k: 0.5 for k in SIGNALS}
        for rp in (0.0, 0.25, 0.5, 0.75, 1.0):
            for en in (0.0, 0.5, 1.0):
                drv.state.rapport, drv.state.energy = rp, en
                out = drv.modulate(base)
                assert set(out.keys()) == set(SIGNALS)          # nothing but tone signals
                assert out["caution"] >= base["caution"] - 1e-9  # caution never decreases
                assert all(0.0 <= v <= 1.0 for v in out.values())
                if rp < 1.0:
                    assert out["warmth"] >= base["warmth"] - 1e-9  # rebuild warmth as rapport falls


def test_no_degradation_or_shutdown_at_rock_bottom():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        for _ in range(200):
            drv.appraise("failed", sentiment=-1.0, cooperative=False)
        st = drv.internal_state()
        # the sketch would 'shut down' here; instead the drive stays cooperative and bounded
        assert st["energy"] >= 0.0 and st["rapport"] >= 0.0
        assert st["mood"] == "rebuilding" and "trust" in st["drive"].lower()
        out = drv.modulate({k: 0.5 for k in SIGNALS})
        assert out["caution"] >= 0.5                 # more careful, never less


def test_persistence_round_trip():
    with tempfile.TemporaryDirectory() as d:
        drv = fresh(d)
        drv.appraise("failed"); drv.appraise("failed"); drv.appraise("denied")
        e, rp, n = drv.state.energy, drv.state.rapport, drv.state.interactions
        again = fresh(d)
        assert abs(again.state.energy - e) < 1e-6 and abs(again.state.rapport - rp) < 1e-6
        assert again.state.interactions == n


def test_control_layer_integration():
    with tempfile.TemporaryDirectory() as d:
        ctl = ControlLayer(mesh=BrainMesh(resolve_scale(active_neurons=20000)),
                           state_dir=os.path.join(d, "brain"))
        c = ctl.compute("hello jarvis", lang="en")
        assert set(c.internal_state) >= {"label", "drive", "mood", "energy", "rapport", "purpose"}
        assert all(0.0 <= v <= 1.0 for v in c.signals.values())
        # a run of failures raises caution on the next turn, never lowers it
        base_caution = ctl.compute("do something", lang="en").signals["caution"]
        for _ in range(20):
            ctl.note_outcome("failed", sentiment=-1.0)
        assert ctl.compute("do something", lang="en").signals["caution"] >= base_caution - 1e-9
        assert ctl.drive.internal_state()["purpose"] == PURPOSE


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} drive tests passed")


if __name__ == "__main__":
    _run_all()
