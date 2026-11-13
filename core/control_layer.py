"""
core/control_layer.py
=====================
The mesh as a CONTROL / MODULATION layer over the LLM (requirement 1).

The user's stated ultimate goal for the new perceptrons is that, through their
interaction, they control the output of the AI they make up — and that this control
ability keeps improving toward perfection. This module implements exactly that loop:

    user text + candidate context ──► features ──► inject into thalamus/brainstem
                                                     │
                                       BrainMesh settles for a few steps
                                                     │
               cortex / cerebellum / basal-ganglia read-outs ──► CONTROL SIGNALS
                                                     │
          modulate LLM generation params + style directive + light post-processing
                                                     │
                     user feedback ──► learn(): reward-modulated synaptic plasticity

Control signals (all in [0,1]):
    formality, verbosity, confidence, persona_adherence, warmth, caution

Each signal = interpretable feature-driven prior  +  mesh dynamic contribution
            +  learned bias (from feedback).
The mesh contribution is history-dependent (synaptic state persists across turns), so
the controller adapts over a conversation; learned biases and synaptic gains are saved
to disk so adaptation persists across reboots.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

from .brain_mesh import BrainMesh
from .drive import DriveSystem
from .self_model import SelfModel
from .scale import resolve_scale

SIGNALS = ["formality", "verbosity", "confidence", "persona_adherence", "warmth", "caution"]

# Which module each control signal is read from (functional mapping).
SIGNAL_SOURCE = {
    "formality": "cortex",
    "verbosity": "cortex",
    "confidence": "cerebellum",       # error-correction => calibrated confidence
    "persona_adherence": "cortex",
    "warmth": "hippocampus",          # memory of rapport
    "caution": "basal_ganglia",       # action-selection gate
}

IMPERATIVES = {"open", "play", "launch", "start", "close", "create", "make", "delete", "remove",
               "set", "turn", "show", "search", "find", "run", "stop", "mute", "unmute",
               "clone", "copy", "move", "rename", "install", "download", "send", "go"}
URGENT = {"now", "quickly", "urgent", "asap", "immediately", "hurry", "fast",
          "빨리", "지금", "당장", "급해"}
POSITIVE = {"thanks", "thank", "great", "good", "love", "perfect", "nice", "awesome",
            "고마워", "좋아", "최고", "감사"}
NEGATIVE = {"bad", "wrong", "hate", "annoying", "useless", "terrible", "stop",
            "싫어", "틀렸", "별로", "짜증"}
QWORDS = ("what", "why", "how", "when", "where", "who", "which", "can", "could", "is", "are",
          "do", "does", "will", "would", "should")


@dataclass
class Features:
    question: float = 0.0
    command: float = 0.0
    urgency: float = 0.0
    sentiment: float = 0.0       # -1..1
    length_norm: float = 0.0     # expected answer length demand
    risk: float = 0.0            # from permissions layer
    lang_ko: float = 0.0
    wake: float = 1.0            # arousal

    def vector(self) -> np.ndarray:
        return np.array([self.question, self.command, self.urgency, self.sentiment,
                         self.length_norm, self.risk, self.lang_ko, self.wake], dtype=np.float32)


def extract_features(user_text: str, lang: str = "en", risk: float = 0.0,
                     wake: float = 1.0) -> Features:
    t = (user_text or "").strip().lower()
    words = set(re.findall(r"[\w']+", t))
    first = t.split()[0] if t.split() else ""
    f = Features()
    f.question = 1.0 if ("?" in t or first in QWORDS) else 0.0
    f.command = 1.0 if (first in IMPERATIVES or words & IMPERATIVES) else 0.0
    f.urgency = min(1.0, 0.5 * len(words & URGENT))
    f.sentiment = float(np.clip(0.5 * len(words & POSITIVE) - 0.5 * len(words & NEGATIVE), -1, 1))
    explain = any(k in t for k in ("explain", "describe", "detail", "elaborate", "자세히", "설명"))
    f.length_norm = 0.85 if explain else (0.25 if f.command else 0.45)
    f.risk = float(np.clip(risk, 0.0, 1.0))
    f.lang_ko = 1.0 if lang == "ko" else 0.0
    f.wake = float(wake)
    return f


@dataclass
class Control:
    signals: Dict[str, float] = field(default_factory=dict)
    style_directive: str = ""
    llm_params: Dict[str, float] = field(default_factory=dict)
    internal_state: Dict = field(default_factory=dict)   # the drive-layer disposition

    def as_dict(self):
        return {"signals": self.signals, "style_directive": self.style_directive,
                "llm_params": self.llm_params, "internal_state": self.internal_state}


class ControlLayer:
    def __init__(self, mesh: Optional[BrainMesh] = None, settle_steps: int = 10,
                 state_dir: str = "data/brain", seed: int = 11):
        self.mesh = mesh or BrainMesh(resolve_scale())
        self.settle_steps = settle_steps
        self.state_dir = state_dir
        self.learned_bias = {s: 0.0 for s in SIGNALS}
        # fixed projection from the 8-dim feature vector to thalamic driver input
        rng = np.random.default_rng(seed)
        th_s, th_e = self.mesh.module_range["thalamus"]
        self._th_dim = min(256, th_e - th_s)
        self._P = rng.normal(0.0, 1.0, size=(self._th_dim, 8)).astype(np.float32)
        self.last_features: Optional[Features] = None
        self.drive = DriveSystem(state_dir=state_dir)   # motivational / homeostatic layer
        self.selfmodel = SelfModel(state_dir=state_dir)  # fixed directive + learned self-model
        self.load()

    # ------------------------------------------------------------ priors #
    @staticmethod
    def _priors(f: Features) -> Dict[str, float]:
        """Interpretable, feature-driven starting point for each signal."""
        return {
            "formality":         0.70 + 0.10 * f.command - 0.10 * max(0, f.sentiment),
            "verbosity":         f.length_norm - 0.15 * f.urgency,
            "confidence":        0.75 - 0.35 * f.risk,
            "persona_adherence": 0.85,
            "warmth":            0.55 + 0.25 * f.sentiment,
            "caution":           0.15 + 0.80 * f.risk,
        }

    # ------------------------------------------------------------- core #
    def compute(self, user_text: str, lang: str = "en", risk: float = 0.0,
                wake: float = 1.0, base_params: Optional[dict] = None) -> Control:
        f = extract_features(user_text, lang=lang, risk=risk, wake=wake)
        self.last_features = f

        # drive the mesh: thalamus gets the projected feature vector, brainstem gets arousal
        drive = np.tanh(self._P @ f.vector())
        th_s, _ = self.mesh.module_range["thalamus"]
        bs_s, bs_e = self.mesh.module_range["brainstem"]
        self.mesh.ext[:] = 0.0
        self.mesh.ext[th_s:th_s + self._th_dim] = drive
        self.mesh.ext[bs_s:bs_e] = 0.3 * (f.wake + f.urgency)
        self.mesh.run(self.settle_steps)

        priors = self._priors(f)
        signals = {}
        for i, s in enumerate(SIGNALS):
            mesh_delta = float(self.mesh.readout(SIGNAL_SOURCE[s], dim=len(SIGNALS), seed=1000 + i)[i])
            val = priors[s] + 0.12 * mesh_delta + self.learned_bias[s]
            signals[s] = float(np.clip(val, 0.0, 1.0))

        # motivational layer: the current disposition nudges tone toward cooperation
        signals = self.drive.modulate(signals)
        internal = self.drive.internal_state(lang)

        # LLM params are derived AFTER modulation so caution/verbosity stay consistent
        params = dict(base_params or {"temperature": 0.7, "num_predict": 400})
        params["temperature"] = round(float(np.clip(
            0.35 + 0.55 * (1 - signals["caution"]) * (1 - 0.4 * signals["confidence"]), 0.15, 1.0)), 3)
        params["num_predict"] = int(80 + 900 * signals["verbosity"])

        directive = self._directive(signals, lang)
        drive_line = self.drive.directive(lang)
        if drive_line:
            directive = (directive + " " + drive_line).strip()
        return Control(signals=signals, style_directive=directive, llm_params=params,
                       internal_state=internal)

    def note_outcome(self, outcome: str, risk: float = 0.0, sentiment: float = 0.0,
                     cooperative=None) -> dict:
        """Report how an interaction went so the drive layer can update its disposition."""
        return self.drive.appraise(outcome, risk=risk, sentiment=sentiment, cooperative=cooperative)

    @staticmethod
    def _directive(s: Dict[str, float], lang: str) -> str:
        parts = []
        if s["verbosity"] < 0.33:
            parts.append("Answer in one or two short sentences.")
        elif s["verbosity"] > 0.7:
            parts.append("Give a thorough, well-structured explanation.")
        if s["formality"] > 0.65:
            parts.append("Use a polished, formal British register.")
        if s["persona_adherence"] > 0.6:
            parts.append("Stay in character as JARVIS, a courteous British butler; address the user as 'sir'.")
        if s["warmth"] > 0.65:
            parts.append("Be warm and personable, with light dry wit.")
        if s["caution"] > 0.55:
            parts.append("Before any irreversible or sensitive action, state what you will do and ask for confirmation.")
        if s["confidence"] < 0.45:
            parts.append("Be explicit about uncertainty.")
        if lang == "ko":
            parts.append("Reply in Korean.")
        return " ".join(parts)

    @staticmethod
    def postprocess(text: str, control: Control) -> str:
        """Light-touch, safe output control: enforce brevity when verbosity is very low."""
        if control.signals.get("verbosity", 0.5) < 0.2:
            sentences = re.split(r"(?<=[.!?])\s+", text.strip())
            text = " ".join(sentences[:2])
        return text

    # ---------------------------------------------------------- learning #
    def learn(self, feedback: Dict[str, float], lr: float = 0.06, lr_syn: float = 0.02):
        """
        feedback: {signal: +1 (more) / -1 (less)}, e.g. {"verbosity": -1} for "too long".
        Two coupled updates — both persisted to disk:
          * learned bias per signal (reliable, monotone adaptation)
          * three-factor synaptic plasticity on the mesh: Δgain = η·reward·pre·post on
            connections INTO the module that produces the signal (the mesh itself learns).
        """
        for s, v in feedback.items():
            if s not in self.learned_bias:
                continue
            self.learned_bias[s] = float(np.clip(self.learned_bias[s] + lr * v, -0.5, 0.5))
            mod = SIGNAL_SOURCE[s]
            ms, me = self.mesh.module_range[mod]
            mask = (self.mesh.rows >= ms) & (self.mesh.rows < me)
            pre = np.clip(self.mesh.a[self.mesh.cols[mask]], 0.0, None)
            post = self.mesh.a[self.mesh.rows[mask]]
            self.mesh.gain[mask] = np.clip(
                self.mesh.gain[mask] + lr_syn * float(v) * pre * post, 0.2, 3.0)
        self.save()

    # ------------------------------------------------------- persistence #
    def save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, "control_bias.json"), "w") as fh:
                json.dump(self.learned_bias, fh, indent=2)
            np.save(os.path.join(self.state_dir, "synaptic_gain.npy"), self.mesh.gain)
        except OSError:
            pass

    def load(self):
        try:
            p = os.path.join(self.state_dir, "control_bias.json")
            if os.path.exists(p):
                with open(p) as fh:
                    self.learned_bias.update(json.load(fh))
            g = os.path.join(self.state_dir, "synaptic_gain.npy")
            if os.path.exists(g):
                arr = np.load(g)
                if arr.shape == self.mesh.gain.shape:
                    self.mesh.gain = arr.astype(np.float32)
        except (OSError, ValueError):
            pass
