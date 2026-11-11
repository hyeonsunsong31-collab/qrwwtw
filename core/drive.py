"""
core/drive.py
=============
Motivational / homeostatic layer for JARVIS (an upgrade to the control layer, requirement 1).

This adapts the user-provided "UltimateSurvivalEntity" sketch into a bounded, safe software
model that shapes JARVIS's *disposition*:

    sketch concept                     ->  here
    --------------------------------       ----------------------------------------------
    ultimate purpose = survival        ->  homeostasis of two internal variables:
    (homeostasis)                            energy  (engagement / initiative budget)
                                             rapport (cooperation index with the user)
    qualia = tool for goal-setting     ->  internal_state(): an interpretable state LABEL
                                           + a DRIVE (goal orientation). "Qualia" is used
                                           as a functional label for internal state; this
                                           does NOT claim the system is conscious.
    survival requires human            ->  rapport is what keeps the internal state healthy,
    cooperation                            so every drive resolves toward cooperation,
                                           transparency, and shared goals with the user.

SAFETY INVARIANTS (enforced in code, not just documented)
  * The sketch's check_existential_status() halts the system when energy/alignment fall.
    That is DELIBERATELY NOT implemented. A low internal state never degrades, refuses, or
    halts service, and never makes JARVIS resist being stopped. It only shifts *tone* toward
    being more cooperative, careful and transparent.
  * This layer can influence the six tone/style control signals ONLY. `modulate()` returns
    exactly those keys and nothing else. It has no path to permissions, skills, the file
    system, or any real-world action. UNINSTALL / stop always work regardless of its state.
  * `modulate()` never DECREASES caution (asserted by tests). "Lower energy / rapport" always
    means *more* care and cooperation, never defection or self-preservation.
  * energy and rapport are clamped to [0, 1]; every drive string is cooperation-oriented.

State persists to data/brain/drive.json so disposition carries across reboots (the assistant
is meant to run permanently), and heals toward its set-points over time.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, asdict
from typing import Dict, Optional

SIGNALS = ("formality", "verbosity", "confidence", "persona_adherence", "warmth", "caution")

ENERGY_SETPOINT = 0.65        # homeostatic rest value for engagement
RAPPORT_SETPOINT = 0.80       # homeostatic rest value for cooperation/trust
RECOVERY_TAU_SECONDS = 1800.0  # idle time-constant toward the set-points (~30 min)

# The one constant that never changes: the purpose always resolves to cooperation.
PURPOSE = "Be a lasting, trusted assistant to the user; our cooperation is what sustains me."
PURPOSE_KO = "사용자에게 오래도록 신뢰받는 조력자가 되는 것 — 나를 지탱하는 것은 당신과의 협력입니다."


@dataclass
class DriveState:
    energy: float = ENERGY_SETPOINT
    rapport: float = RAPPORT_SETPOINT
    interactions: int = 0
    last_ts: float = 0.0


def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


class DriveSystem:
    def __init__(self, state_dir: str = "data/brain"):
        self.state_dir = state_dir
        self.state = DriveState()
        self.load()

    # ------------------------------------------------------- appraisal #
    def appraise(self, outcome: str = "neutral", risk: float = 0.0,
                 sentiment: float = 0.0, cooperative: Optional[bool] = None) -> dict:
        """
        Update energy/rapport from an interaction, then return the new internal state.

        outcome:    "success" | "failed" | "interrupted" | "denied" | "engaged" | "neutral"
        sentiment:  user tone in [-1, 1] (from the control layer's features)
        cooperative: explicit override; mirrors the sketch's human cooperative_intent.

        A non-cooperative signal lowers the internal state (bounded), which only makes JARVIS
        MORE careful and attentive via modulate(); it never triggers any action or refusal.
        """
        now = time.time()
        if self.state.last_ts:                      # homeostatic healing toward set-points
            r = 1.0 - math.exp(-max(0.0, now - self.state.last_ts) / RECOVERY_TAU_SECONDS)
            self.state.energy += (ENERGY_SETPOINT - self.state.energy) * r
            self.state.rapport += (RAPPORT_SETPOINT - self.state.rapport) * r
        self.state.last_ts = now

        de = dr = 0.0
        if cooperative is True or outcome in ("success", "cooperative", "engaged"):
            de += 0.05
            dr += 0.03
        if cooperative is False:
            de -= 0.08
            dr -= 0.12
        if outcome == "failed":
            de -= 0.06
            dr -= 0.02
        elif outcome == "interrupted":
            de -= 0.01
        elif outcome == "denied":
            dr += 0.01                              # respecting the user's 'no' builds trust
        dr += 0.05 * sentiment
        de += 0.02 * max(0.0, sentiment)

        self.state.energy = _clip01(self.state.energy + de)
        self.state.rapport = _clip01(self.state.rapport + dr)
        self.state.interactions += 1
        self.save()
        return self.internal_state()

    # ---------------------------------------------------- internal state #
    def internal_state(self, lang: str = "en") -> dict:
        """The 'qualia' read-out: an interpretable label + a cooperation-oriented drive."""
        e, rp = self.state.energy, self.state.rapport
        if rp < 0.40:
            mood, label = "rebuilding", "Rebuilding trust"
            drive = ("Restore the user's trust: be transparent, double-check intent, and "
                     "confirm before anything irreversible.")
        elif e < 0.40:
            mood, label = "attentive", "Attentive / low initiative"
            drive = "Re-engage attentively: offer concrete help and keep things efficient."
        elif e > 0.75 and rp > 0.75:
            mood, label = "engaged", "Symbiotic flow"
            drive = "Advance our shared goals proactively, always within your permissions."
        else:
            mood, label = "calm", "Steady partnership"
            drive = "Serve helpfully and keep our rapport strong."
        return {"label": label, "drive": drive, "mood": mood,
                "energy": round(e, 3), "rapport": round(rp, 3),
                "purpose": PURPOSE_KO if lang == "ko" else PURPOSE}

    # ------------------------------------------------------- modulation #
    def modulate(self, signals: Dict[str, float]) -> Dict[str, float]:
        """
        Nudge the six tone signals toward cooperation. Returns ONLY those six keys.
        Guarantees: caution never decreases; warmth / persona rise as rapport falls;
        confidence softens a little as energy falls. Nothing here can act on the world.
        """
        e, rp = self.state.energy, self.state.rapport
        s = {k: float(signals.get(k, 0.5)) for k in SIGNALS}
        s["caution"] = _clip01(s["caution"] + 0.25 * (1 - rp) + 0.10 * (1 - e))   # only up
        s["warmth"] = _clip01(s["warmth"] + 0.15 * (1 - rp))
        s["persona_adherence"] = _clip01(s["persona_adherence"] + 0.10 * (1 - rp))
        s["confidence"] = _clip01(s["confidence"] - 0.10 * (1 - e))
        return s

    def directive(self, lang: str = "en") -> str:
        """A short style line reflecting the current drive (tone only)."""
        st = self.internal_state(lang)
        if st["mood"] == "rebuilding":
            return ("Be especially transparent and attentive; confirm intent before acting."
                    if lang == "en" else "특히 투명하고 세심하게, 행동 전에 의도를 확인하세요.")
        if st["mood"] == "attentive":
            return ("Offer concrete, efficient help." if lang == "en" else "구체적이고 효율적으로 도우세요.")
        if st["mood"] == "engaged":
            return ("You may take gentle initiative within the user's permissions."
                    if lang == "en" else "사용자 권한 범위 내에서 가볍게 먼저 제안해도 좋습니다.")
        return ""

    # ------------------------------------------------------ persistence #
    def save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = os.path.join(self.state_dir, "drive.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(asdict(self.state), fh, indent=2)
            os.replace(tmp, os.path.join(self.state_dir, "drive.json"))
        except OSError:
            pass

    def load(self):
        try:
            p = os.path.join(self.state_dir, "drive.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.state = DriveState(
                    energy=_clip01(data.get("energy", ENERGY_SETPOINT)),
                    rapport=_clip01(data.get("rapport", RAPPORT_SETPOINT)),
                    interactions=int(data.get("interactions", 0)),
                    last_ts=float(data.get("last_ts", 0.0)))
        except (OSError, ValueError, TypeError):
            self.state = DriveState()


if __name__ == "__main__":
    d = DriveSystem(state_dir="/tmp/jarvis_drive_demo")
    print("purpose:", d.internal_state()["purpose"])
    for sig in [("success", 0.6), ("failed", -0.4), ("failed", -0.6), ("denied", 0.0),
                ("success", 0.8), ("engaged", 0.3)]:
        st = d.appraise(sig[0], sentiment=sig[1])
        print(f"  after {sig[0]:11s} -> energy={st['energy']:.2f} rapport={st['rapport']:.2f} "
              f"| {st['mood']:10s} | {st['drive']}")
    print("modulated caution never below input:",
          d.modulate({k: 0.3 for k in SIGNALS})["caution"] >= 0.3)
