"""System skills (requirement 20): info, volume, mic mute, power, battery."""
from __future__ import annotations

from .base import ok, fail, powershell, run, IS_WINDOWS, not_on_this_os


def system_info(**_) -> dict:
    try:
        import psutil  # type: ignore
    except Exception:
        return fail("System metrics need the psutil library, sir.", "psutil missing")
    cpu = psutil.cpu_percent(interval=0.3)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    data = {"cpu_percent": cpu,
            "ram_percent": vm.percent,
            "ram_used_gb": round(vm.used / 1024 ** 3, 1),
            "ram_total_gb": round(vm.total / 1024 ** 3, 1),
            "disk_percent": du.percent,
            "disk_free_gb": round(du.free / 1024 ** 3, 1)}
    try:
        bat = psutil.sensors_battery()
        if bat:
            data["battery_percent"] = round(bat.percent)
            data["plugged_in"] = bat.power_plugged
    except Exception:
        pass
    speak = (f"CPU is at {cpu:.0f} percent, memory at {vm.percent:.0f} percent, "
             f"and {data['disk_free_gb']:.0f} gigabytes free on disk, sir.")
    if "battery_percent" in data:
        speak += f" Battery is at {data['battery_percent']} percent."
    return ok(speak, data)


def set_volume(level=None, **_) -> dict:
    try:
        lvl = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return fail("What volume level, sir? Give me a number from 0 to 100.")
    if not IS_WINDOWS:
        return ok(f"(Dev) Volume would be set to {lvl} percent, sir.", {"level": lvl})
    try:
        from ctypes import cast, POINTER  # type: ignore
        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        dev = AudioUtilities.GetSpeakers()
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(lvl / 100.0, None)
        return ok(f"Volume set to {lvl} percent, sir.", {"level": lvl})
    except Exception as e:
        return fail("I couldn't adjust the volume, sir.", str(e))


def _set_mic_mute(mute: bool) -> dict:
    if not IS_WINDOWS:
        return ok(f"(Dev) Microphone would be {'muted' if mute else 'unmuted'}, sir.",
                  {"muted": mute})
    try:
        from ctypes import cast, POINTER  # type: ignore
        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        mic = AudioUtilities.GetMicrophone()
        iface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMute(1 if mute else 0, None)
        return ok(f"Microphone {'muted' if mute else 'unmuted'}, sir.", {"muted": mute})
    except Exception as e:
        return fail("I couldn't change the microphone, sir.", str(e))


def mute_mic(**_) -> dict:
    return _set_mic_mute(True)


def unmute_mic(**_) -> dict:
    return _set_mic_mute(False)


def lock_workstation(**_) -> dict:
    if not IS_WINDOWS:
        return not_on_this_os("Locking the workstation")
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
        return ok("Locking the workstation, sir.")
    except Exception as e:
        return fail("I couldn't lock the workstation, sir.", str(e))


def power(action: str = "sleep", **_) -> dict:
    """action: sleep | shutdown | restart. Gated by permissions (ask) upstream."""
    if not IS_WINDOWS:
        return ok(f"(Dev) Would {action} the PC, sir.", {"action": action})
    try:
        if action == "sleep":
            run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], timeout=8)
        elif action == "shutdown":
            run(["shutdown", "/s", "/t", "20"], timeout=8)
        elif action == "restart":
            run(["shutdown", "/r", "/t", "20"], timeout=8)
        else:
            return fail(f"I don't know how to {action} the PC, sir.")
        return ok(f"Very good, sir — {action} initiated.", {"action": action})
    except Exception as e:
        return fail(f"I couldn't {action} the PC, sir.", str(e))


def open_settings(page: str = "", **_) -> dict:
    """Open Windows Settings (optionally a specific page e.g. 'bluetooth')."""
    if not IS_WINDOWS:
        return ok("(Dev) Would open system settings, sir.", {"page": page})
    try:
        import os
        os.startfile(f"ms-settings:{page}")  # type: ignore[attr-defined]
        return ok("Opening Settings, sir." + (f" ({page})" if page else ""), {"page": page})
    except Exception as e:
        return fail("I couldn't open Settings, sir.", str(e))
