"""
core/scale.py
=============
Scale manager for the brain-based 3D neural mesh (requirement 1).

HONEST NOTE ON "200 BILLION NEURONS"
------------------------------------
The design *nominally* targets ~200,000,000,000 (2e11) synaptic perceptrons wired
into a 3D mesh. That figure cannot be instantiated or trained on any consumer PC:
even at one byte per connection with a modest average degree, 2e11 neurons need
petabytes to exabytes of RAM. (For reference, data-center LLMs are ~1e11 *parameters*
trained on supercomputers, not 2e11 fully-connected neurons.)

So `nominal_neurons` is the documented design scale, and `active_neurons` is what the
mesh *actually* instantiates on this machine, chosen from available RAM and clamped to
`active_bounds`. The architecture (synapse dynamics, cortex/cerebellum/thalamus mesh,
3D sparse connectivity) is identical at any scale — only the neuron count changes.
The mesh's job is to act as a CONTROL / MODULATION layer over the LLM output, not to
be a from-scratch language model, so a feasible active size is enough to do real work.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, Tuple
import math

NOMINAL_NEURONS = 200_000_000_000  # 2e11 — documented design target (requirement 1)

# Fraction of the active mesh assigned to each brain module. Core modules
# (cerebellum, cerebral cortex, thalamus) plus auxiliary structures.
MODULE_FRACTIONS: Dict[str, float] = {
    "cortex": 0.46,        # cerebral cortex — high-level association / representation
    "cerebellum": 0.34,    # cerebellum — fine timing, error-correction, smoothing
    "thalamus": 0.08,      # thalamus — relay + gating hub between modules
    "brainstem": 0.03,     # arousal / wake-state neuromodulation
    "hippocampus": 0.06,   # episodic memory / checkpoint binding
    "basal_ganglia": 0.03, # action selection / gating
}


def _detect_ram_gb() -> float:
    """Best-effort physical-RAM detection; conservative fallback if unknown."""
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        pass
    try:  # Linux
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        pass
    try:  # POSIX
        import os
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except Exception:
        return 8.0


@dataclass
class ScaleConfig:
    """Resolved scale for a single mesh instance."""
    nominal_neurons: int = NOMINAL_NEURONS
    active_neurons: int = 100_000
    avg_degree: int = 32            # average incoming synapses per neuron (sparse)
    grid: Tuple[int, int, int] = (0, 0, 0)  # 3D lattice dimensions actually used
    module_fractions: Dict[str, float] = field(default_factory=lambda: dict(MODULE_FRACTIONS))
    ram_gb: float = 0.0

    @property
    def synapses(self) -> int:
        return self.active_neurons * self.avg_degree

    def module_sizes(self) -> Dict[str, int]:
        sizes = {m: max(1, int(self.active_neurons * f)) for m, f in self.module_fractions.items()}
        # absorb rounding drift into the cortex so the parts sum to the whole
        drift = self.active_neurons - sum(sizes.values())
        sizes["cortex"] += drift
        return sizes

    def summary(self) -> dict:
        d = asdict(self)
        d["synapses"] = self.synapses
        d["module_sizes"] = self.module_sizes()
        d["nominal_to_active_ratio"] = self.nominal_neurons / max(1, self.active_neurons)
        return d


def resolve_scale(active_neurons="auto",
                  nominal_neurons: int = NOMINAL_NEURONS,
                  avg_degree: int = 32,
                  bounds: Tuple[int, int] = (20_000, 2_000_000)) -> ScaleConfig:
    """
    Decide how many neurons to actually instantiate.

    active_neurons="auto"  -> pick from detected RAM (~40k neurons per GB of RAM,
                              each neuron costs ~avg_degree synapses * a few float32s),
                              clamped to `bounds`.
    active_neurons=<int>   -> use exactly that (still clamped to bounds for safety).
    """
    ram = _detect_ram_gb()
    lo, hi = bounds
    if active_neurons == "auto":
        # ~0.75 GB reserved for the OS/other apps; ~40k neurons per usable GB.
        usable = max(0.5, ram - 0.75)
        n = int(usable * 40_000)
    else:
        n = int(active_neurons)
    n = max(lo, min(hi, n))

    side = max(1, round(n ** (1.0 / 3.0)))
    grid = (side, side, max(1, math.ceil(n / (side * side))))

    return ScaleConfig(
        nominal_neurons=nominal_neurons,
        active_neurons=n,
        avg_degree=avg_degree,
        grid=grid,
        ram_gb=round(ram, 2),
    )


if __name__ == "__main__":
    import json
    cfg = resolve_scale()
    print(json.dumps(cfg.summary(), indent=2))
