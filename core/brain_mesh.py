"""
core/brain_mesh.py
==================
The brain-based 3D neural mesh (requirement 1).

Instead of stacking layers of perceptrons (a classical MLP), the units are placed at
3D coordinates and wired into a sparse recurrent **mesh**, partitioned into modules that
play the functional roles of real brain structures:

  CORE
    * cerebral cortex  — high-level association & representation (large sheet)
    * cerebellum       — fine timing, error-correction, output smoothing
    * thalamus         — relay + gating hub; routes & gates signals between modules
  AUXILIARY ("부수적으로 필요한 추가 구성")
    * brainstem        — arousal / wake-state broadcast (global neuromodulation)
    * hippocampus      — episodic memory / checkpoint binding
    * basal ganglia    — action selection (inhibitory gate onto thalamus)

Connectivity has two parts:
  1. intra-module local wiring: proximity-biased in 3D (nearby units connect strongly),
  2. inter-module projections: a wiring rulebook mirroring real long-range pathways.

Every connection is a Synaptic Perceptron synapse (short-term plasticity, see
synaptic_perceptron.py), so the whole mesh has dynamics and memory. The mesh is driven
by external input (from the control layer / brainstem) and read out per module.

Representation is pure NumPy COO (rows, cols, data) + per-connection state, with
`np.bincount` for the row reductions — no dense N×N matrix is ever formed, so memory is
O(neurons × avg_degree) and the whole thing scales from 20k to millions of units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple
import numpy as np

from .synaptic_perceptron import SynapseParams, synapse_step, ACTIVATIONS
from .scale import ScaleConfig, resolve_scale

# Inter-module projection rulebook: (source, target, density, weight_mean, sign)
# density = fraction of target units that receive a projection; sign -1 = inhibitory.
PROJECTIONS: List[Tuple[str, str, float, float, int]] = [
    ("thalamus",      "cortex",       0.30, 0.9, +1),   # relay IN (sensory/driver)
    ("cortex",        "thalamus",     0.20, 0.6, +1),   # cortico-thalamic feedback
    ("cortex",        "cerebellum",   0.18, 0.5, +1),   # cortico-ponto-cerebellar
    ("cerebellum",    "thalamus",     0.18, 0.6, +1),   # deep nuclei -> thalamus
    ("thalamus",      "cerebellum",   0.08, 0.3, +1),
    ("cortex",        "basal_ganglia",0.16, 0.5, +1),   # cortico-striatal
    ("basal_ganglia", "thalamus",     0.20, 0.7, -1),   # pallido-thalamic GATE (inhib.)
    ("cortex",        "hippocampus",  0.14, 0.5, +1),   # memory encode
    ("hippocampus",   "cortex",       0.14, 0.5, +1),   # memory recall
    ("brainstem",     "thalamus",     0.10, 0.4, +1),   # arousal drive
    ("brainstem",     "cortex",       0.04, 0.3, +1),
]


@dataclass
class MeshState:
    a: np.ndarray      # neuron activations (N,)
    u: np.ndarray      # synaptic facilitation (E,)
    R: np.ndarray      # synaptic resources (E,)


class BrainMesh:
    def __init__(self, scale: ScaleConfig | None = None,
                 activation: str = "tanh",
                 params: SynapseParams | None = None,
                 seed: int = 7):
        self.scale = scale or resolve_scale()
        self.rng = np.random.default_rng(seed)
        self.activation = ACTIVATIONS[activation]
        self.p = params or SynapseParams()
        self.N = self.scale.active_neurons

        self._build_modules()
        self._place_neurons_3d()
        self._wire()
        self.reset_state()

    # ------------------------------------------------------------------ build #
    def _build_modules(self):
        self.module_sizes = self.scale.module_sizes()
        self.module_range: Dict[str, Tuple[int, int]] = {}
        start = 0
        for name, size in self.module_sizes.items():
            self.module_range[name] = (start, start + size)
            start += size
        self.N = start  # exact total after rounding
        # which module each neuron belongs to (for per-neuron parameters)
        self.module_of = np.empty(self.N, dtype=np.int16)
        for i, (name, (s, e)) in enumerate(self.module_range.items()):
            self.module_of[s:e] = i
        self.module_names = list(self.module_range.keys())

    def _place_neurons_3d(self):
        """Give each module its own box in a 3D volume so the mesh is spatial."""
        # Module boxes tile a 3x2 arrangement inside the unit cube.
        boxes = {
            "cortex":       ((0.0, 0.0, 0.5), (1.0, 1.0, 1.0)),  # outer shell (top)
            "cerebellum":   ((0.0, 0.0, 0.0), (1.0, 0.5, 0.45)),
            "thalamus":     ((0.35, 0.35, 0.35), (0.65, 0.65, 0.65)),  # central hub
            "brainstem":    ((0.4, 0.0, 0.0), (0.6, 0.35, 0.4)),
            "hippocampus":  ((0.0, 0.5, 0.0), (0.45, 1.0, 0.45)),
            "basal_ganglia":((0.55, 0.5, 0.0), (1.0, 1.0, 0.45)),
        }
        self.pos = np.zeros((self.N, 3), dtype=np.float32)
        for name, (s, e) in self.module_range.items():
            lo, hi = boxes.get(name, ((0, 0, 0), (1, 1, 1)))
            lo = np.array(lo); hi = np.array(hi)
            self.pos[s:e] = lo + (hi - lo) * self.rng.random((e - s, 3))

    def _wire(self):
        rows: List[np.ndarray] = []
        cols: List[np.ndarray] = []
        data: List[np.ndarray] = []

        k_intra = max(4, int(self.scale.avg_degree * 0.6))
        lam = 0.12  # spatial decay length for proximity bias

        # 1) intra-module proximity-biased local wiring
        for name, (s, e) in self.module_range.items():
            m = e - s
            if m < 2:
                continue
            tgt = np.repeat(np.arange(s, e), k_intra)
            src = s + self.rng.integers(0, m, size=m * k_intra)
            d = np.linalg.norm(self.pos[tgt] - self.pos[src], axis=1)
            prox = np.exp(-d / lam)                      # nearby => stronger
            sign = self.rng.choice([-1.0, 1.0], size=tgt.size, p=[0.2, 0.8])
            w = sign * prox * self.rng.normal(0.8, 0.2, size=tgt.size)
            rows.append(tgt); cols.append(src); data.append(w.astype(np.float32))

        # 2) inter-module projections (the wiring rulebook)
        for src_m, dst_m, density, wmean, sgn in PROJECTIONS:
            ss, se = self.module_range[src_m]
            ds, de = self.module_range[dst_m]
            if se - ss < 1 or de - ds < 1:
                continue
            n_conn = max(1, int(density * (de - ds) * max(2, self.scale.avg_degree // 6)))
            tgt = self.rng.integers(ds, de, size=n_conn)
            src = self.rng.integers(ss, se, size=n_conn)
            w = sgn * np.abs(self.rng.normal(wmean, wmean * 0.3, size=n_conn))
            rows.append(tgt); cols.append(src); data.append(w.astype(np.float32))

        self.rows = np.concatenate(rows).astype(np.int64)
        self.cols = np.concatenate(cols).astype(np.int64)
        self.w = np.concatenate(data).astype(np.float32)
        self.E = self.w.size
        self.gain = np.ones(self.E, dtype=np.float32)

        # per-neuron bias and small intrinsic leak
        self.bias = self.rng.normal(0.0, 0.05, size=self.N).astype(np.float32)
        # normalise incoming weights per neuron for stability (divide by sqrt(in-degree))
        indeg = np.bincount(self.rows, minlength=self.N).astype(np.float32)
        scale_in = 1.0 / np.sqrt(np.maximum(1.0, indeg))
        self.w *= scale_in[self.rows]

    # ------------------------------------------------------------------ state #
    def reset_state(self):
        self.a = np.zeros(self.N, dtype=np.float32)
        self.u = np.full(self.E, self.p.U, dtype=np.float32)
        self.R = np.ones(self.E, dtype=np.float32)
        self.ext = np.zeros(self.N, dtype=np.float32)

    def get_state(self) -> MeshState:
        return MeshState(self.a.copy(), self.u.copy(), self.R.copy())

    def set_state(self, st: MeshState):
        self.a, self.u, self.R = st.a.copy(), st.u.copy(), st.R.copy()

    # ------------------------------------------------------------------- I/O #
    def inject(self, indices, values):
        """Set external input current on specific neurons (driven by control layer)."""
        self.ext[:] = 0.0
        self.ext[np.asarray(indices)] = np.asarray(values, dtype=np.float32)

    def inject_module(self, module: str, vector):
        s, e = self.module_range[module]
        v = np.asarray(vector, dtype=np.float32)
        n = min(e - s, v.size)
        self.ext[:] = 0.0
        self.ext[s:s + n] = v[:n]

    def module_activity(self, module: str) -> np.ndarray:
        s, e = self.module_range[module]
        return self.a[s:e]

    def readout(self, module: str, dim: int, seed: int = 123) -> np.ndarray:
        """
        Fixed random linear read-out of a module's activity *pattern* to a `dim`-vector.
        Activity is z-scored first so the read-out reflects which units are active
        relative to each other (the pattern), not the raw magnitude, which is small.
        Read-out matrices are cached per (module, dim, seed).
        """
        s, e = self.module_range[module]
        act = self.a[s:e].astype(np.float64)
        sd = act.std()
        z = (act - act.mean()) / sd if sd > 1e-9 else np.zeros_like(act)
        key = (module, dim, seed)
        cache = self.__dict__.setdefault("_readout_cache", {})
        W = cache.get(key)
        if W is None:
            rng = np.random.default_rng(seed)
            W = rng.normal(0.0, 1.0 / np.sqrt(max(1, act.size)), size=(dim, act.size))
            cache[key] = W
        return np.tanh(W @ z)

    # ------------------------------------------------------------------ step #
    def step(self) -> np.ndarray:
        """Advance the whole mesh one time-step. Returns the new activation vector."""
        # brainstem arousal -> global neuromodulation in [0.5, 1.6]
        bs = self.module_activity("brainstem")
        arousal = float(0.9 + 0.7 * np.tanh(bs.mean() if bs.size else 0.0))

        # presynaptic activity per connection (rectified)
        pre = np.clip(self.a[self.cols], 0.0, None)
        transmitted, self.u, self.R = synapse_step(
            pre, self.w, self.u, self.R, self.gain, arousal, self.p)

        # sum transmitted current into each target neuron
        inp = np.bincount(self.rows, weights=transmitted, minlength=self.N).astype(np.float32)
        inp += self.bias + self.ext

        # thalamic gating: thalamus mean activity opens/closes cortical & cerebellar input
        th = self.module_activity("thalamus")
        gate = float(sigmoid_scalar(1.5 * (th.mean() if th.size else 0.0)))
        g = np.ones(self.N, dtype=np.float32)
        for mod in ("cortex", "cerebellum"):
            s, e = self.module_range[mod]
            g[s:e] = 0.4 + 0.6 * gate
        inp *= g

        # update activations with a leak (recurrent stability)
        a_new = self.activation(inp)
        self.a = (0.15 * self.a + 0.85 * a_new).astype(np.float32)
        np.clip(self.a, -1.5, 1.5, out=self.a)
        return self.a

    def run(self, steps: int) -> np.ndarray:
        for _ in range(int(steps)):
            self.step()
        return self.a

    # ---------------------------------------------------------------- export #
    def sample_points(self, max_points: int = 4000) -> dict:
        """Downsampled 3D point cloud (pos + activity + module) for the 3D viewer."""
        if self.N <= max_points:
            idx = np.arange(self.N)
        else:
            idx = self.rng.choice(self.N, size=max_points, replace=False)
        return {
            "positions": self.pos[idx].tolist(),
            "activity": self.a[idx].tolist(),
            "module": self.module_of[idx].tolist(),
            "module_names": self.module_names,
        }

    def info(self) -> dict:
        return {
            "neurons_active": int(self.N),
            "neurons_nominal": int(self.scale.nominal_neurons),
            "synapses_active": int(self.E),
            "avg_degree": round(self.E / max(1, self.N), 2),
            "modules": {k: (e - s) for k, (s, e) in self.module_range.items()},
            "ram_gb": self.scale.ram_gb,
        }


def sigmoid_scalar(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


if __name__ == "__main__":
    import json
    mesh = BrainMesh(resolve_scale(active_neurons=5000))
    print(json.dumps(mesh.info(), indent=2))
    mesh.inject_module("thalamus", np.ones(64))
    mesh.run(25)
    print("cortex mean activity after 25 steps:", round(float(mesh.module_activity("cortex").mean()), 4))
