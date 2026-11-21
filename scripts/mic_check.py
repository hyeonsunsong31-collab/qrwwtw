"""
scripts/mic_check.py -- a guided microphone check (TEST_MICROPHONE.bat), v1.4.2.

  1. lists the microphones Windows offers (marking the default one),
  2. reads Windows' microphone privacy switch and the microphone's mute and input level,
  3. opens the microphone exactly the way JARVIS does and shows a live level for a few
     seconds while you say "wake up Jarvis",
  4. runs what it heard through JARVIS's wake-word listener (Vosk) and Whisper, and
  5. says exactly what to fix. A copy of the report is saved to data/logs/mic_check.txt.

It works while JARVIS is running (Windows lets several programs share a microphone).

    python scripts/mic_check.py           (TEST_MICROPHONE.bat)
    python scripts/mic_check.py --quick   (no recording prompt; the installer uses this)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LINES = []


def out(text: str = "") -> None:
    LINES.append(text)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)


def level_bar(db: float, width: int = 40) -> str:
    n = max(0, min(width, int(round((db + 70.0) / 60.0 * width))))
    return "[" + "#" * n + "." * (width - n) + "]"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS microphone check")
    ap.add_argument("--quick", action="store_true", help="no recording prompt")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args(argv)
    problems, good = [], []
    out("JARVIS microphone check")
    out("=" * 60)
    try:
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore
    except Exception as e:
        out(f"[!!] The audio packages are missing ({e}). Run INSTALL_JARVIS.bat.")
        return 1
    from voice.mic import (MicStream, LevelMeter, list_input_devices, privacy_status, endpoint_status,
                           mic_score)
    try:
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    chosen = str(cfg.get("voice", {}).get("input_device", "default") or "default")

    # 1) devices
    devs = list_input_devices(sd)
    out("\nMicrophones Windows offers:")
    if not devs:
        out("   (none)")
    for d in devs:
        tag = " <- default" if d["default"] else ""
        kind = " (loopback - never used)" if d["loopback"] else ""
        out(f"   {d['name']}  [{d['hostapi']}, {int(d['rate'])} Hz, {d['channels']} ch]{tag}{kind}")
    if chosen.lower() not in ("default", "auto", ""):
        out(f"   JARVIS Settings > Microphone: '{chosen}'")
    real = [d for d in devs if not d["loopback"]]
    if not real:
        problems.append("No microphone was found. Plug one in, or enable one in Settings > System > Sound > "
                        "Input (and in Device Manager if it is disabled).")

    # 2) Windows settings
    priv = privacy_status()
    ep = endpoint_status()
    out("\nWindows settings:")
    if priv.get("which") or any(k in priv for k in ("device", "apps", "desktop_apps")):
        out(f"   Microphone access: device={priv.get('device', '?')}, apps={priv.get('apps', '?')}, "
            f"desktop apps={priv.get('desktop_apps', '?')}")
    else:
        out("   Microphone access: (not readable here)")
    if priv.get("blocked"):
        problems.append("Windows is blocking microphone access for desktop apps. Open Settings > Privacy & security > "
                        "Microphone and turn on 'Microphone access' and 'Let desktop apps access your microphone'.")
    if ep:
        if "muted" in ep:
            out(f"   Default microphone: {'MUTED' if ep.get('muted') else 'not muted'}, input volume "
                f"{int(round(100 * (ep.get('volume') or 0)))}%")
        if ep.get("muted"):
            problems.append("The default microphone is muted in Windows. Unmute it in Settings > System > Sound > "
                            "Input (or type 'unmute the microphone' in the JARVIS HUD).")
        elif isinstance(ep.get("volume"), (int, float)) and ep["volume"] < 0.15:
            problems.append(f"The microphone's input volume is only {int(ep['volume'] * 100)}%. Raise it in "
                            "Settings > System > Sound > Input.")

    # 3) record
    frames = []
    mic = MicStream(sd, np, frames.append, device=chosen, log=lambda m: out("   " + m))
    out("\nOpening the microphone the way JARVIS does:")
    if not mic.open():
        problems.append(f"The microphone could not be opened: {mic.reason} ({mic.error}).")
        for a in mic.attempts[-6:]:
            out(f"   tried {a}")
    else:
        secs = 2.0 if args.quick else max(2.0, args.seconds)
        if not args.quick:
            out(f'\n   >>> Now say "wake up Jarvis" (recording {secs:.0f} seconds) <<<\n')
        meter = LevelMeter(np)
        t0, seen, loud = time.time(), 0, -100.0
        while time.time() - t0 < secs:
            time.sleep(0.1)
            while seen < len(frames):
                meter.update(frames[seen])
                seen += 1
            loud = max(loud, meter.db)
            if not args.quick:
                try:
                    sys.stdout.write(f"\r   level {meter.db:6.1f} dB {level_bar(meter.db)}   ")
                    sys.stdout.flush()
                except Exception:
                    pass
        mic.close()
        if not args.quick:
            out("")
        n = len(frames)
        zeros = meter.zero_run
        out(f"   received {n} frames ({n * 0.02:.1f} s), loudest {meter.loudest_db:.0f} dB, room {meter.floor_db:.0f} dB")
        if n == 0:
            problems.append(f"'{mic.info.get('name')}' opened but sent no sound at all. Pick another microphone "
                            "in Settings > System > Sound > Input, or in JARVIS Settings > Microphone.")
        elif zeros >= min(n, 100):
            if not priv.get("blocked") and not ep.get("muted"):
                problems.append(f"'{mic.info.get('name')}' sends pure digital silence: it is muted, switched off, "
                                "or blocked by Windows. Check Settings > Privacy & security > Microphone and "
                                "Settings > System > Sound > Input.")
        else:
            good.append(f"Sound arrives from '{mic.info.get('name')}' ({mic.info.get('hostapi')}, "
                        f"{mic.info.get('rate')} Hz).")
            if not args.quick and meter.loudest_db < meter.floor_db + 10:
                problems.append("I could not hear you speak above the room noise. Speak closer to the microphone, "
                                "raise its input volume, or choose another microphone.")

        # 4) recognisers
        if frames and not args.quick and zeros < min(n, 100):
            audio = b"".join(frames)
            try:
                from voice.wakeword import make_detector_ex, heard_wake, loose_wake, wake_verdict
                det, info = make_detector_ex(cfg, ROOT)
                if det is None:
                    problems.append(f"The wake-word listener is unavailable: {info.get('error')}. "
                                    "JARVIS downloads its model by itself when it starts; or run INSTALL_JARVIS.bat.")
                else:
                    finals = []
                    for i in range(0, len(audio), 6400):
                        r = det.process(audio[i:i + 6400])
                        if r and r[1]:
                            finals.append(r[0])
                    heard = " | ".join(finals) or "(nothing)"
                    out(f"\n   Wake listener ({info.get('engine')}) heard: {heard}")
                    if any(heard_wake(f) or loose_wake(f) for f in finals):
                        good.append('The wake listener recognised "wake up Jarvis".')
                    else:
                        try:
                            from voice.stt import Transcriber
                            out("   Loading Whisper to double-check (a few seconds)...")
                            s = cfg.get("voice", {}).get("stt", {})
                            tr = Transcriber(s.get("model", "auto"), s.get("language", "auto"), root=ROOT)
                            if tr.available:
                                a = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
                                said = tr.transcribe_detail(a).get("text", "")
                                out(f"   Whisper heard: {said or '(nothing)'}")
                                v = wake_verdict(said)
                                if v == "wake":
                                    good.append("Whisper recognised the wake phrase: JARVIS will wake (it double-checks "
                                                "near misses with Whisper).")
                                elif v == "name":
                                    problems.append("Only 'Jarvis' was recognised. Say the whole phrase, 'wake up Jarvis', "
                                                    "or say 'Jarvis' twice.")
                                else:
                                    problems.append('Neither recogniser heard "wake up Jarvis". Speak a little slower and '
                                                    "closer to the microphone, then run this check again.")
                            else:
                                problems.append(f"Whisper could not be loaded: {tr.error}")
                        except Exception as e:
                            problems.append(f"Whisper check failed: {e!r}")
            except Exception as e:
                problems.append(f"Wake-word check failed: {e!r}")

    # 5) verdict
    out("\n" + "=" * 60)
    for g in good:
        out("[ok] " + g)
    for p in problems:
        out("[!!] " + p)
    if not problems:
        out('Everything looks right. Say "wake up Jarvis" to JARVIS.')
    try:
        logs = os.path.join(os.environ.get("JARVIS_DATA") or os.path.join(ROOT, "data"), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "mic_check.txt"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + "\n".join(LINES) + "\n")
        out("\nReport saved to data\\logs\\mic_check.txt")
    except OSError:
        pass
    return 0 if not problems else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")      # Korean / Western consoles alike
    except Exception:
        pass
    sys.exit(main())
