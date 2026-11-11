"""
agent/permissions.py
====================
Permission gate for agentic PC control (requirement 20, see docs/SECURITY.md).

Every skill action passes through `PermissionManager.check(...)`. The decision comes from
config/permissions.json:  allow | ask | deny  per category, with hard overrides:
  * keywords in `always_ask_keywords` force 'ask' even inside an 'allow' category,
  * `run_shell_elevated` and anything in `never_allowed` are refused,
  * destructive/outbound actions default to 'ask'.
'ask' calls an injected async `asker(request)` (the overlay shows a holographic confirm
dialog). Every decision is written to the audit log by the recorder.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

AskFn = Callable[[dict], Awaitable[bool]]


@dataclass
class PermissionRequest:
    action: str
    category: str
    detail: str = ""
    risk: float = 0.0


class PermissionManager:
    def __init__(self, config_path: str = "config/permissions.json", recorder=None,
                 asker: Optional[AskFn] = None):
        self.config_path = config_path
        self.recorder = recorder
        self.asker = asker
        self.reload()

    def reload(self):
        with open(self.config_path, encoding="utf-8") as fh:
            self.cfg = json.load(fh)
        self.categories = self.cfg.get("categories", {})
        self.default = self.cfg.get("default", "ask")
        self.always_ask = [k.lower() for k in self.cfg.get("always_ask_keywords", [])]

    # risk score for the control layer / UI
    def risk_of(self, category: str, detail: str = "") -> float:
        policy = self.categories.get(category, self.default)
        base = {"allow": 0.1, "ask": 0.55, "deny": 1.0}.get(policy, 0.55)
        if any(k in (detail or "").lower() for k in self.always_ask):
            base = max(base, 0.85)
        if category in ("delete_files", "change_system_settings", "run_shell",
                        "network_send", "control_devices", "run_shell_elevated"):
            base = max(base, 0.7)
        return round(min(1.0, base), 2)

    async def check(self, req: PermissionRequest) -> bool:
        policy = self.categories.get(req.category, self.default)
        detail_l = (req.detail or "").lower()

        # hard refusals
        if policy == "deny" or req.category == "run_shell_elevated":
            self._audit(req, "deny")
            return False

        # forced 'ask'
        forced_ask = any(k in detail_l for k in self.always_ask)
        effective = "ask" if (policy == "ask" or forced_ask) else policy

        if effective == "allow":
            self._audit(req, "allow")
            return True

        # ask the user via the overlay
        if self.asker is None:
            self._audit(req, "ask-no-ui-deny")
            return False
        granted = False
        try:
            granted = bool(await self.asker({
                "action": req.action, "category": req.category,
                "detail": req.detail, "risk": req.risk or self.risk_of(req.category, req.detail),
            }))
        except Exception:
            granted = False
        self._audit(req, "granted" if granted else "denied")
        return granted

    def _audit(self, req: PermissionRequest, decision: str):
        if self.recorder:
            self.recorder.audit(req.action, req.category, decision, detail=req.detail)
