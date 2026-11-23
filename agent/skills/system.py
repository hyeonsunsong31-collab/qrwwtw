"""System skills (requirement 20): info, volume, mic mute, power, battery."""
from __future__ import annotations

import re

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


class _com:
    """COM must be initialised on every thread that uses it; skills run on worker threads."""

    def __enter__(self):
        self.on = False
        try:
            import comtypes  # type: ignore
            comtypes.CoInitialize()
            self.on = True
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.on:
            try:
                import comtypes  # type: ignore
                comtypes.CoUninitialize()
            except Exception:
                pass
        return False


def _endpoint_volume(device):
    """IAudioEndpointVolume for a pycaw device, across pycaw versions: newer releases wrap
    the device and expose `.EndpointVolume`; older ones return the raw IMMDevice."""
    ev = getattr(device, "EndpointVolume", None)
    if ev is not None:
        return ev
    from ctypes import cast, POINTER  # type: ignore
    from comtypes import CLSCTX_ALL  # type: ignore
    from pycaw.pycaw import IAudioEndpointVolume  # type: ignore
    iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def set_volume(level=None, **_) -> dict:
    """level: 0-100, "+10" / "-10", "up" / "down" (10 points), "mute" / "unmute"."""
    raw = str(level if level is not None else "").strip().lower().rstrip("%").replace("percent", "").strip()
    rel, mute, lvl = None, None, None
    if raw in ("up", "louder", "+", "increase", "raise", "올려", "키워"):
        rel = 10
    elif raw in ("down", "quieter", "softer", "-", "decrease", "lower", "내려", "줄여"):
        rel = -10
    elif raw in ("mute", "off", "silent", "silence", "음소거"):
        mute = True
    elif raw in ("unmute", "on"):
        mute = False
    elif re.fullmatch(r"[+-]\s*\d{1,3}", raw):
        rel = int(raw.replace(" ", ""))
    else:
        try:
            lvl = max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return fail("What volume level, sir? A number from 0 to 100, or up, down or mute.",
                        f"unknown volume level {level!r}")
    if not IS_WINDOWS:
        if mute is not None:
            return ok(f"(Dev) Sound would be {'muted' if mute else 'unmuted'}, sir.", {"muted": mute,
                      "speak_ko": f"(개발) 소리를 {'끕니다' if mute else '켭니다'}, sir."})
        if rel is not None:
            return ok(f"(Dev) Volume would go {'up' if rel > 0 else 'down'} by {abs(rel)}, sir.", {"change": rel})
        return ok(f"(Dev) Volume would be set to {lvl} percent, sir.", {"level": lvl})
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        with _com():
            ev = _endpoint_volume(AudioUtilities.GetSpeakers())
            if mute is not None:
                ev.SetMute(1 if mute else 0, None)
                return ok("Sound muted, sir." if mute else "Sound back on, sir.",
                          {"muted": mute, "speak_ko": "소리를 껐습니다, sir." if mute else "소리를 다시 켰습니다, sir."})
            if rel is not None:
                cur = int(round(ev.GetMasterVolumeLevelScalar() * 100))
                lvl = max(0, min(100, cur + rel))
                if rel > 0:
                    ev.SetMute(0, None)               # turning it up also unmutes
            ev.SetMasterVolumeLevelScalar(lvl / 100.0, None)
        return ok(f"Volume at {lvl} percent, sir.", {"level": lvl, "speak_ko": f"볼륨을 {lvl}퍼센트로 맞췄습니다, sir."})
    except Exception as e:
        return fail("I couldn't adjust the volume, sir.", str(e))


_BRIGHT_GET = ("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
               "Select-Object -First 1).CurrentBrightness")
_BRIGHT_SET = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | "
               "ForEach-Object {{ Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness "
               "-Arguments @{{Timeout=1; Brightness={0}}} | Out-Null }}; 'ok'")


def set_brightness(level=None, **_) -> dict:
    """level: 0-100, "+10" / "-10", "up" / "down". Built-in (laptop) screens; external monitors
    keep their own buttons."""
    raw = str(level if level is not None else "").strip().lower().rstrip("%").replace("percent", "").strip()
    rel, lvl = None, None
    if raw in ("up", "brighter", "+", "increase", "raise"):
        rel = 15
    elif raw in ("down", "dimmer", "darker", "-", "decrease", "lower", "dim"):
        rel = -15
    elif re.fullmatch(r"[+-]\s*\d{1,3}", raw):
        rel = int(raw.replace(" ", ""))
    else:
        try:
            lvl = max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return fail("What brightness, sir? A number from 0 to 100, or up or down.", f"unknown level {level!r}")
    if not IS_WINDOWS:
        return ok(f"(Dev) Brightness would be {'changed by ' + str(rel) if rel is not None else 'set to ' + str(lvl)}, sir.",
                  {"level": lvl, "change": rel})
    try:
        if rel is not None:
            cur = int(float(powershell(_BRIGHT_GET, timeout=15) or 50))
            lvl = max(0, min(100, cur + rel))
        out = powershell(_BRIGHT_SET.format(lvl), timeout=15)
        if "ok" not in out:
            raise RuntimeError(out[:200])
        return ok(f"Brightness at {lvl} percent, sir.", {"level": lvl, "speak_ko": f"화면 밝기를 {lvl}퍼센트로 맞췄습니다, sir."})
    except Exception as e:
        return fail("This display doesn't let Windows change its brightness, sir; an external monitor uses its "
                    "own buttons.", f"brightness: {e}")


def _set_mic_mute(mute: bool) -> dict:
    if not IS_WINDOWS:
        return ok(f"(Dev) Microphone would be {'muted' if mute else 'unmuted'}, sir.",
                  {"muted": mute})
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        with _com():
            _endpoint_volume(AudioUtilities.GetMicrophone()).SetMute(1 if mute else 0, None)
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


def open_mic_settings(**_) -> dict:
    """Windows' microphone privacy page: where 'Let desktop apps access your microphone' is."""
    if not IS_WINDOWS:
        return ok("(Dev) Would open the microphone privacy settings, sir.", {"page": "privacy-microphone"})
    try:
        import os
        os.startfile("ms-settings:privacy-microphone")  # type: ignore[attr-defined]
        return ok("Opening the microphone privacy settings, sir. Turn on 'Microphone access' and "
                  "'Let desktop apps access your microphone'.", {"page": "privacy-microphone"})
    except Exception as e:
        return fail("I couldn't open the microphone settings, sir.", str(e))


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
