"""
scripts/llm_check.py -- a guided language-engine check (TEST_LANGUAGE_ENGINE.bat), v1.4.3.

  1. shows the settings that can get between JARVIS and its language engine (a system
     proxy, OLLAMA_HOST, a blocked port 11434),
  2. finds Ollama and starts it if it is not running (moving it to a free port if needed),
  3. lists the installed models and downloads the configured one if it is missing,
  4. asks it one question and measures the speed,
  5. says exactly what to fix. A copy of the report is saved to data/logs/llm_check.txt.

    python scripts/llm_check.py           (TEST_LANGUAGE_ENGINE.bat)
    python scripts/llm_check.py --quick   (no download, no question; the installer uses this)
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS language-engine check")
    ap.add_argument("--quick", action="store_true", help="no model download, no test question")
    args = ap.parse_args(argv)
    problems, good = [], []
    out("JARVIS language-engine check")
    out("=" * 60)
    try:
        from agent import ollama_service as svc
        from agent.llm_ollama import OllamaLLM, pick_model, classify, _norm
    except Exception as e:
        out(f"[!!] JARVIS's Python packages are incomplete ({e}). Run INSTALL_JARVIS.bat.")
        return 1
    try:
        cfg = json.load(open(os.path.join(ROOT, "config", "jarvis.config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    llm_cfg = cfg.get("llm", {})
    data = os.environ.get("JARVIS_DATA") or os.path.join(ROOT, "data")

    # 1) what can get in the way
    out("\nSettings that matter:")
    proxy = svc.system_proxy()
    out(f"   system proxy : {proxy or 'none'}" + ("  (JARVIS bypasses it for the local engine)" if proxy else ""))
    env = svc.env_ollama_host()
    out(f"   OLLAMA_HOST  : {env or 'not set (default 127.0.0.1:11434)'}")
    service = svc.OllamaService(llm_cfg.get("host", svc.DEFAULT_HOST), data_dir=data, log=lambda m: out("   " + m))
    out(f"   engine URL   : {service.base}")
    exe = svc.find_ollama()
    out(f"   Ollama       : {exe or 'NOT FOUND'}")
    if svc.is_loopback(service.base):
        st = svc.port_state(svc.port_of(service.base))
        out(f"   port {svc.port_of(service.base)}  : {st}" + (f" ({svc.port_owner(svc.port_of(service.base))})"
                                                              if st == 'in_use' else ""))

    # 2) running?
    out("\nReaching the engine:")
    if service.ensure_running():
        good.append(f"Ollama {service.version or ''} answers at {service.base}." + (f" ({service.note})" if service.note else ""))
        out(f"   answers at {service.base} (version {service.version})")
    else:
        p = service.problem or {}
        problems.append(f"{p.get('message', 'The engine does not answer.')} {p.get('fix', '')}")
        out(f"   not answering: {service.last_probe.get('error')}")
        return finish(good, problems, data)

    # 3) models
    llm = OllamaLLM(service.base, llm_cfg.get("model", "auto"), llm_cfg.get("model_candidates", {}),
                    keep_alive=llm_cfg.get("keep_alive", "always"), num_ctx=int(llm_cfg.get("context_tokens", 4096)))
    installed = llm.local_models()
    out(f"\nInstalled models: {', '.join(installed) or 'none'}")
    want = llm_cfg.get("model", "auto")
    if want == "auto":
        want = llm.resolve_model() if installed else pick_model(llm_cfg.get("model_candidates", {}))
    out(f"Model JARVIS uses: {want}")
    if _norm(want) not in {_norm(n) for n in installed}:
        if args.quick:
            problems.append(f"The language model {want} is not downloaded yet. JARVIS downloads it by itself when it "
                            "runs, or run TEST_LANGUAGE_ENGINE.bat to download it now.")
            return finish(good, problems, data)
        out(f"   downloading {want} (this can take a while) ...")
        last = [0.0]

        def progress(p):
            if time.time() - last[0] > 1.0:
                last[0] = time.time()
                try:
                    sys.stdout.write(f"\r   {p.get('percent', 0):5.1f}%  {p.get('status', '')[:40]:40}")
                    sys.stdout.flush()
                except Exception:
                    pass
        try:
            llm.pull(want, on_progress=progress)
            out("")
            good.append(f"The language model {want} was downloaded.")
        except Exception as e:
            out("")
            problems.append(f"The language model {want} could not be downloaded ({classify(e)[1]}). Check the internet "
                            "connection and the free disk space, then run this check again.")
            return finish(good, problems, data)
    else:
        good.append(f"The language model {want} is installed.")
    llm.model = want

    # 4) one question
    if not args.quick:
        out("\nAsking it one question (the first answer includes loading the model) ...")
        t0 = time.time()
        try:
            stats = llm.warm_up("You are JARVIS, a concise British butler.")
            reply = llm.chat("You are JARVIS, a concise British butler.", "Say hello in five words.", [],
                             {"temperature": 0.3, "num_predict": 30})
            out(f"   reply: {reply!r}")
            out(f"   loaded in {stats.get('load_seconds')} s, {stats.get('tokens_per_s')} tokens/s, "
                f"{int(100 * (stats.get('gpu') or 0))}% on the graphics card")
            good.append(f"The model answers ({time.time() - t0:.1f} s including loading).")
            if (stats.get("tokens_per_s") or 99) < 6:
                problems.append("Replies are slow on this PC; run INSTALL_JARVIS.bat to pick a smaller model, or set "
                                "a smaller one in JARVIS Settings > Language model.")
        except Exception as e:
            kind, msg = classify(e)
            if kind == "memory":
                problems.append(f"{want} needs more memory than is free. Close some programs or choose a smaller model "
                                "(JARVIS steps down by itself when this happens while it runs).")
            else:
                problems.append(f"The model did not answer: {msg}")
    return finish(good, problems, data)


def finish(good, problems, data) -> int:
    out("\n" + "=" * 60)
    for g in good:
        out("[ok] " + g)
    for p in problems:
        out("[!!] " + p)
    if not problems:
        out("Everything looks right: JARVIS can use its language engine.")
    try:
        logs = os.path.join(data, "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "llm_check.txt"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + "\n".join(LINES) + "\n")
        out("\nReport saved to data\\logs\\llm_check.txt")
    except OSError:
        pass
    return 0 if not problems else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
