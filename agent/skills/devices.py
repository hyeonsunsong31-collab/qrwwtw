"""Device enumeration (requirement 20): disks, network, and attached devices."""
from __future__ import annotations

from .base import ok, fail, powershell, IS_WINDOWS


def list_devices(**_) -> dict:
    data = {"disks": [], "network": [], "pnp": []}
    try:
        import psutil  # type: ignore
        for part in psutil.disk_partitions(all=False):
            data["disks"].append({"device": part.device, "mountpoint": part.mountpoint,
                                   "fstype": part.fstype})
        for name, addrs in psutil.net_if_addrs().items():
            data["network"].append({"interface": name,
                                     "addresses": [a.address for a in addrs][:3]})
    except Exception:
        pass
    # Windows: enumerate Plug-and-Play devices for a fuller "all compatible equipment" view
    if IS_WINDOWS:
        try:
            out = powershell("Get-PnpDevice -PresentOnly -Status OK | "
                             "Select-Object -First 40 FriendlyName,Class | ConvertTo-Json -Compress")
            import json
            parsed = json.loads(out) if out else []
            if isinstance(parsed, dict):
                parsed = [parsed]
            data["pnp"] = parsed
        except Exception:
            pass
    n = len(data["disks"]) + len(data["network"]) + len(data["pnp"])
    speak = (f"I can see {len(data['disks'])} drive(s), {len(data['network'])} network "
             f"interface(s)" + (f", and {len(data['pnp'])} connected device(s)" if data["pnp"] else "")
             + ", sir.") if n else "I couldn't enumerate devices, sir."
    return ok(speak, data) if n else fail(speak)
