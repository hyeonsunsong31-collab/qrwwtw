"""
core/synaptic_perceptron.py
===========================
A NEW perceptron design (requirement 1).

A classical perceptron computes:      y = activation( Σ wᵢ·xᵢ + b )
It is static: the same input always yields the same output.

The **Synaptic Perceptron** inserts a stateful *synapse* between every input and the
weighted sum. Each synapse transmits a signal that depends not only on the current
input but on the *recent history* of that input, via short-term plasticity
(facilitation + depression, a discrete Tsodyks–Markram model) plus a neuromodulatory
gain. The perceptron therefore has memory and dynamics:

    transmittedᵢ = release(uᵢ, Rᵢ, xᵢ) · wᵢ · gainᵢ · neuromod
    y            = activation( Σ transmittedᵢ + b )

where (uᵢ, Rᵢ) are per-synapse facilitation/resource state that evolve every step.

DESIGN GOAL (as specified by the user): these units are NOT built for binary
classification. They are built so that, through their *interaction*, the population
can **control / modulate the output** of the AI it is part of. The synapse is what makes
that control rich: it lets upstream activity shape downstream gain over time.

Two representations are provided:
  * `SynapticPerceptron` — one readable, fully-documented unit (reference / teaching).
  * `SynapticLayer`      — a vectorised population of units (NumPy), used by the mesh.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


# ----------------------------------------------------------------------------- #
# Activation functions
# ----------------------------------------------------------------------------- #
def tanh(x):
    return np.tanh(x)


def relu(x):
    return np.maximum(0.0, x)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


ACTIVATIONS = {"tanh": tanh, "relu": relu, "sigmoid": sigmoid}


# ----------------------------------------------------------------------------- #
# Short-term plasticity (the synapse component)
# ----------------------------------------------------------------------------- #
@dataclass
class SynapseParams:
    """
    Discrete Tsodyks–Markram short-term plasticity parameters.
      U        baseline release probability / utilisation
      tau_rec  resource-recovery time constant (depression)   [steps]
      tau_fac  facilitation decay time constant               [steps]
      dt       time step
    """
    U: float = 0.45
    tau_rec: float = 18.0
    tau_fac: float = 6.0
    dt: float = 1.0


def synapse_step(a, w, u, R, gain, neuromod, p: SynapseParams):
    """
    Advance an array of synapses by one step and return (transmitted, u', R').

    a        presynaptic activity (>=0), array
    w        synaptic efficacy (signed weight), array
    u, R     facilitation / available-resource state, arrays in [0,1]
    gain     per-synapse neurotransmitter gain, array
    neuromod scalar or array broadcast — global neuromodulation (arousal/wake)

    The transmitted signal is `released = u·R·a`, consuming resources (depression)
    and raising utilisation (facilitation). Between events both relax toward rest.
    """
    rec_rate = 1.0 - np.exp(-p.dt / p.tau_rec)
    fac_rate = 1.0 - np.exp(-p.dt / p.tau_fac)

    # relaxation toward rest (R -> 1, u -> U) since the previous step
    R = R + (1.0 - R) * rec_rate
    u = u + (p.U - u) * fac_rate

    # facilitation driven by current activity, then release + depression
    u = u + p.U * (1.0 - u) * np.clip(a, 0.0, None)
    released = u * R * a
    R = R - released
    R = np.clip(R, 0.0, 1.0)
    u = np.clip(u, 0.0, 1.0)

    transmitted = released * w * gain * neuromod
    return transmitted, u, R


# ----------------------------------------------------------------------------- #
# Single unit (reference implementation)
# ----------------------------------------------------------------------------- #
class SynapticPerceptron:
    """One synaptic perceptron with `n_inputs` stateful synapses."""

    def __init__(self, n_inputs: int, activation: str = "tanh",
                 params: SynapseParams | None = None, rng: np.random.Generator | None = None):
        rng = rng or np.random.default_rng(0)
        self.n_inputs = n_inputs
        self.w = rng.normal(0.0, 1.0 / np.sqrt(max(1, n_inputs)), size=n_inputs)
        self.b = 0.0
        self.gain = np.ones(n_inputs)
        self.activation_name = activation
        self.activation = ACTIVATIONS[activation]
        self.p = params or SynapseParams()
        self.reset_state()

    def reset_state(self):
        self.u = np.full(self.n_inputs, self.p.U)
        self.R = np.ones(self.n_inputs)

    def forward(self, x, neuromod: float = 1.0):
        """x: array of presynaptic activities. Returns scalar output; updates synapses."""
        x = np.asarray(x, dtype=float)
        transmitted, self.u, self.R = synapse_step(
            x, self.w, self.u, self.R, self.gain, neuromod, self.p)
        return float(self.activation(transmitted.sum() + self.b))


# ----------------------------------------------------------------------------- #
# Vectorised population (used by brain_mesh)
# ----------------------------------------------------------------------------- #
class SynapticLayer:
    """
    A population of N synaptic perceptrons, each reading the same M-dim input vector
    through its own stateful synapses. Weights/state are dense (N×M); used for small,
    fully-connected sub-blocks. Large, sparse connectivity lives in brain_mesh.py.
    """

    def __init__(self, n_units: int, n_inputs: int, activation: str = "tanh",
                 params: SynapseParams | None = None, seed: int = 0):
        rng = np.random.default_rng(seed)
        self.N, self.M = n_units, n_inputs
        self.W = rng.normal(0.0, 1.0 / np.sqrt(max(1, n_inputs)), size=(n_units, n_inputs))
        self.b = np.zeros(n_units)
        self.gain = np.ones((n_units, n_inputs))
        self.activation = ACTIVATIONS[activation]
        self.p = params or SynapseParams()
        self.reset_state()

    def reset_state(self):
        self.u = np.full((self.N, self.M), self.p.U)
        self.R = np.ones((self.N, self.M))

    def forward(self, x, neuromod: float = 1.0):
        x = np.asarray(x, dtype=float).reshape(1, self.M)
        transmitted, self.u, self.R = synapse_step(
            x, self.W, self.u, self.R, self.gain, neuromod, self.p)
        return self.activation(transmitted.sum(axis=1) + self.b)


if __name__ == "__main__":
    # Demonstrate that identical repeated input produces a CHANGING output,
    # which a classical perceptron never does — this is the synapse at work.
    sp = SynapticPerceptron(n_inputs=4, activation="tanh")
    x = np.array([1.0, 0.0, 0.8, 0.2])
    print("Same input repeated 8× (note the drift from short-term depression):")
    for t in range(8):
        print(f"  step {t}: y = {sp.forward(x):+.4f}")
