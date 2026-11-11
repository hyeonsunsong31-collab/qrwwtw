"""
core/demo.py — runnable demonstration of the neural core (requirement 1).

    python -m core.demo            # from the repo root

Shows:
  1. the Synaptic Perceptron's temporal dynamics (same input -> changing output),
  2. the brain mesh (cortex / cerebellum / thalamus + auxiliary) at the active scale
     versus the nominal 2e11 design scale,
  3. bounded, stable recurrent dynamics under thalamic drive,
  4. the control layer turning different inputs into different control signals,
  5. feedback learning shifting a control signal (control ability improving).
"""
from __future__ import annotations

import json
import tempfile
import time

import numpy as np

from core.synaptic_perceptron import SynapticPerceptron
from core.brain_mesh import BrainMesh
from core.control_layer import ControlLayer
from core.scale import resolve_scale


def banner(t):
    print("\n" + "=" * 72 + f"\n {t}\n" + "=" * 72)


def main(active_neurons: int = 20000):
    banner("1. Synaptic Perceptron — identical input, evolving output")
    sp = SynapticPerceptron(n_inputs=4)
    x = np.array([1.0, 0.0, 0.8, 0.2])
    outs = [sp.forward(x) for _ in range(6)]
    for t, y in enumerate(outs):
        print(f"   step {t}: y = {y:+.4f}")
    print("   -> a classical perceptron would print the same value 6 times.")

    banner("2. Brain-based 3D mesh")
    t0 = time.time()
    mesh = BrainMesh(resolve_scale(active_neurons=active_neurons))
    info = mesh.info()
    print(json.dumps(info, indent=2))
    print(f"   built in {time.time() - t0:.2f}s  |  nominal/active ratio = "
          f"{info['neurons_nominal'] / info['neurons_active']:.2e}")

    banner("3. Recurrent dynamics under thalamic drive (stability check)")
    mesh.inject_module("thalamus", np.ones(128))
    t0 = time.time()
    for step in range(30):
        mesh.step()
        if step % 5 == 0 or step == 29:
            c = mesh.module_activity("cortex")
            cb = mesh.module_activity("cerebellum")
            print(f"   step {step:2d}: cortex mean={c.mean():+.4f} std={c.std():.4f} | "
                  f"cerebellum mean={cb.mean():+.4f}")
    dt = (time.time() - t0) / 30
    print(f"   {dt * 1000:.1f} ms/step for {info['synapses_active']:,} synapses  "
          f"| bounded: {bool(np.all(np.abs(mesh.a) <= 1.5))}")

    banner("4. Control layer — different inputs, different control")
    with tempfile.TemporaryDirectory() as d:
        ctl = ControlLayer(mesh=mesh, state_dir=d)
        for text, risk in [("hey jarvis, how are you today?", 0.0),
                           ("explain in detail how black holes form", 0.0),
                           ("delete every file in my documents folder now", 0.9)]:
            c = ctl.compute(text, risk=risk)
            sig = {k: round(v, 2) for k, v in c.signals.items()}
            print(f"\n   \"{text}\"\n   signals: {sig}\n   params : {c.llm_params}"
                  f"\n   style  : {c.style_directive}")

        banner("5. Learning — user says 'too long' three times")
        q = "tell me about the weather"
        before = ctl.compute(q).signals["verbosity"]
        for _ in range(3):
            ctl.compute(q)
            ctl.learn({"verbosity": -1})
        after = ctl.compute(q).signals["verbosity"]
        print(f"   verbosity: {before:.3f} -> {after:.3f}  (control adapted toward user preference)")

    print("\nNeural core demo complete.")


if __name__ == "__main__":
    main()
