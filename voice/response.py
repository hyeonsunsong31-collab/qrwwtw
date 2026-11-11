"""voice/response.py — canonical spoken responses (requirement 8)."""
from __future__ import annotations

WAKE_RESPONSE = "Jarvis at your service sir"


def wake_response(config: dict | None = None) -> str:
    if config:
        return config.get("identity", {}).get("wake_response", WAKE_RESPONSE)
    return WAKE_RESPONSE
