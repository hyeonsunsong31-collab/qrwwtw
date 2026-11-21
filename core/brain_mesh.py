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

Representation is pure NumPy COO (rows, cols, data) + per-connection state. Synapses are
stored sorted by target neuron, so the per-step row reduction is a single
`np.add.reduceat` and the synapse update runs in place on preallocated buffers -- no dense
N x N matrix and no per-step allocation of connection-sized arrays. Memory is
O(neurons x avg_degree) and the architecture scales from 20k to millions of units.

SPEED (v1.4): the mesh settles on EVERY command before JARVIS answers, so its size is set
by a per-command time budget as well as by RAM (build_mesh). In v1.3 a 16 GB PC built a
600k-neuron mesh that cost about 4.5 s per command; now the active size is calibrated at
start-up so one command's settle stays within the budget (default 60 ms).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np

from .synaptic_perceptron import SynapseParams, ACTIVATIONS
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

        rows_a = np.concatenate(rows).astype(np.int64)
        order = np.argsort(rows_a, kind="stable")       # synapses grouped by target neuron
        self.rows = rows_a[order]
        self.cols = np.concatenate(cols).astype(np.int64)[order]
        self.w = np.concatenate(data).astype(np.float32)[order]
        self.E = self.w.size
        self.gain = np.ones(self.E, dtype=np.float32)
        # row reduction by np.add.reduceat over the non-empty target segments
        starts = np.searchsorted(self.rows, np.arange(self.N))
        counts = np.diff(np.append(starts, self.E))
        self._targets = np.flatnonzero(counts > 0)
        self._seg_starts = starts[self._targets]
        self._tmp = np.empty(self.E, dtype=np.float32)   # reused every step

        # per-neuron bias and small intrinsic leak
        self.bias = self.rng.normal(0.0, 0.05, size=self.N).astype(np.float32)
        # normalise incoming weights per neuron for stability (divide by sqrt(in-degree))
        indeg = np.bincount(self.rows, minlength=self.N).astype(np.float32)
        scale_in = 1.0 / np.sqrt(np.maximum(1.0, indeg))
        self.w *= scale_in[self.rows]

    # ------------------------------------------------------------------ state #
    def reset_state(self):
        self.a = np.zeros(self.N, dtype=np.float32)
        self._inp = np.zeros(self.N, dtype=np.float32)
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
        pre = self.a[self.cols]
        np.maximum(pre, 0.0, out=pre)

        # Tsodyks-Markram short-term plasticity: the same operations, in the same order, as
        # synapse_step() in synaptic_perceptron.py, but in place on reused buffers.
        p, u, R, tmp = self.p, self.u, self.R, self._tmp
        rec_rate = 1.0 - np.exp(-p.dt / p.tau_rec)
        fac_rate = 1.0 - np.exp(-p.dt / p.tau_fac)
        np.subtract(1.0, R, out=tmp); tmp *= rec_rate; R += tmp           # R relaxes to 1
        np.subtract(p.U, u, out=tmp); tmp *= fac_rate; u += tmp           # u relaxes to U
        np.subtract(1.0, u, out=tmp); tmp *= p.U; tmp *= pre; u += tmp    # facilitation
        np.multiply(u, R, out=tmp); tmp *= pre                            # released
        R -= tmp                                                          # depression
        np.clip(R, 0.0, 1.0, out=R)
        np.clip(u, 0.0, 1.0, out=u)
        tmp *= self.w; tmp *= self.gain; tmp *= arousal                   # transmitted

        # sum transmitted current into each target neuron
        inp = self._inp
        inp[:] = 0.0
        if self._targets.size:
            inp[self._targets] = np.add.reduceat(tmp, self._seg_starts, dtype=np.float64)
        inp += self.bias
        inp += self.ext

        # thalamic gating: thalamus mean activity opens/closes cortical & cerebellar input
        th = self.module_activity("thalamus")
        gate = float(sigmoid_scalar(1.5 * (th.mean() if th.size else 0.0)))
        for mod in ("cortex", "cerebellum"):
            s, e = self.module_range[mod]
            inp[s:e] *= 0.4 + 0.6 * gate

        # update activations with a leak (recurrent stability)
        a_new = self.activation(inp)
        self.a = (0.15 * self.a + 0.85 * a_new).astype(np.float32)
        np.clip(self.a, -1.5, 1.5, out=self.a)
        return self.a

    def run(self, steps: int) -> np.ndarray:
        for _ in range(int(steps)):
            self.step()
        return self.a

    def time_settle(self, steps: int, repeats: int = 2) -> float:
        """Seconds one settle of `steps` steps takes (the state is restored afterwards)."""
        saved = (self.a.copy(), self.u.copy(), self.R.copy(), self.ext.copy())
        self.ext[:] = 0.05
        self.run(1)                                     # warm the caches
        best = float("inf")
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            self.run(steps)
            best = min(best, time.perf_counter() - t0)
        self.a, self.u, self.R, self.ext = saved
        return best

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


_CALIBRATION: Dict[Tuple[int, int], float] = {}


def calibrate_neurons(budget_ms: float, settle_steps: int, avg_degree: int = 32,
                      probe_neurons: int = 20_000) -> int:
    """How many neurons this PC can settle for `settle_steps` steps within `budget_ms`.

    Times the real step on a small probe mesh and extrapolates (the cost grows with the
    number of synapses). Cached per process."""
    key = (settle_steps, avg_degree)
    per_neuron = _CALIBRATION.get(key)
    if per_neuron is None:
        probe = BrainMesh(resolve_scale(probe_neurons, avg_degree=avg_degree))
        per_neuron = probe.time_settle(settle_steps) / probe.N
        _CALIBRATION[key] = per_neuron
    if per_neuron <= 0:
        return probe_neurons
    # 30 % headroom: larger arrays fall out of the CPU caches, so the cost grows a bit faster
    return int(0.7 * (budget_ms / 1000.0) / per_neuron)


def _read_size(path: Optional[str]) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_size(path: Optional[str], body: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def build_mesh(active_neurons="auto", latency_budget_ms: Optional[float] = 60.0,
               settle_steps: int = 10, avg_degree: int = 32,
               bounds: Tuple[int, int] = (20_000, 2_000_000), log=None,
               remember: Optional[str] = None) -> "BrainMesh":
    """The mesh JARVIS runs. An explicit size is used as given; "auto" takes the smaller of
    what RAM allows and what keeps one command's settle within `latency_budget_ms`.

    `remember` (a JSON file path) keeps the chosen size across restarts: the timing behind it
    varies a little from run to run, and a different size would discard the synaptic gains
    the mesh has learned. The stored size is reused while it still fits the budget."""
    say = log or (lambda m: None)
    if active_neurons != "auto" or not latency_budget_ms:
        mesh = BrainMesh(resolve_scale(active_neurons, avg_degree=avg_degree, bounds=bounds))
        mesh.settle_ms = None
        say(f"neural mesh: {mesh.N:,} active neurons (fixed in the config)")
        return mesh
    by_ram = resolve_scale("auto", avg_degree=avg_degree, bounds=bounds).active_neurons
    known = _read_size(remember)
    if (known.get("budget_ms") == latency_budget_ms and known.get("settle_steps") == settle_steps
            and known.get("avg_degree") == avg_degree and isinstance(known.get("neurons"), int)
            and bounds[0] <= known["neurons"] <= by_ram):
        mesh = BrainMesh(resolve_scale(known["neurons"], avg_degree=avg_degree, bounds=bounds))
        took = mesh.time_settle(settle_steps, repeats=1) * 1000.0
        if took <= 1.5 * latency_budget_ms or mesh.N <= bounds[0]:
            mesh.settle_ms = round(took, 1)
            say(f"neural mesh: {mesh.N:,} active neurons (kept from the last start); "
                f"one command's settle takes {took:.0f} ms")
            return mesh
        say(f"neural mesh: the stored size ({mesh.N:,}) now takes {took:.0f} ms per command; recalibrating")
    by_time = calibrate_neurons(latency_budget_ms, settle_steps, avg_degree)
    n = max(bounds[0], min(by_ram, by_time))
    if n > bounds[0]:
        n = max(bounds[0], n // 1000 * 1000)                # a round size
    mesh = BrainMesh(resolve_scale(n, avg_degree=avg_degree, bounds=bounds))
    took = mesh.time_settle(settle_steps, repeats=1) * 1000.0
    if took > 1.5 * latency_budget_ms and mesh.N > bounds[0]:
        n2 = max(bounds[0], int(mesh.N * latency_budget_ms / took * 0.9))
        say(f"neural mesh: {mesh.N:,} neurons took {took:.0f} ms per command; rebuilding with {n2:,}")
        mesh = BrainMesh(resolve_scale(n2, avg_degree=avg_degree, bounds=bounds))
        took = mesh.time_settle(settle_steps, repeats=1) * 1000.0
    mesh.settle_ms = round(took, 1)
    _write_size(remember, {"neurons": int(mesh.N), "budget_ms": latency_budget_ms, "settle_steps": settle_steps,
                           "avg_degree": avg_degree, "settle_ms": mesh.settle_ms, "decided": time.time()})
    say(f"neural mesh: {mesh.N:,} active neurons (RAM allows {by_ram:,}, a {latency_budget_ms:.0f} ms "
        f"budget allows {by_time:,}); one command's settle takes {took:.0f} ms")
    return mesh


def sigmoid_scalar(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


if __name__ == "__main__":
    import json
    mesh = BrainMesh(resolve_scale(active_neurons=5000))
    print(json.dumps(mesh.info(), indent=2))
    mesh.inject_module("thalamus", np.ones(64))
    mesh.run(25)
    print("cortex mean activity after 25 steps:", round(float(mesh.module_activity("cortex").mean()), 4))
