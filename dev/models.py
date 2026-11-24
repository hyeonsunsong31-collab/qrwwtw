"""
dev/models.py -- the coding model: the largest Qwen2.5-Coder this PC can hold (your choice).

Memory needed = the model's size x 1.15 + its working memory for the context (the KV cache,
which grows with the context length). It must fit in the graphics card's free memory plus the
free RAM, keeping a reserve for Windows, JARVIS and the program being built. Ollama splits a
model between the graphics card and the processor when it has to, so both count -- a model
mostly on the processor is slower, but the choice you made is "largest that fits".

    qwen2.5-coder:32b  ~20 GB    qwen2.5-coder:14b  ~9 GB    qwen2.5-coder:7b  ~4.7 GB
    qwen2.5-coder:3b   ~1.9 GB   qwen2.5-coder:1.5b ~1 GB
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")
# name, download GB, layers, KV heads (head size 128) -- for the context's memory
CODERS: List[Tuple[str, float, int, int]] = [
    ("qwen2.5-coder:32b", 19.9, 64, 8),
    ("qwen2.5-coder:14b", 9.0, 48, 8),
    ("qwen2.5-coder:7b", 4.7, 28, 4),
    ("qwen2.5-coder:3b", 1.9, 36, 2),
    ("qwen2.5-coder:1.5b", 1.0, 28, 2),
]
RESERVE_GB = 3.0


def kv_gb(layers: int, kv_heads: int, ctx: int) -> float:
    return 2 * layers * kv_heads * 128 * 2 * ctx / 1e9          # K and V, fp16


def need_gb(name: str, ctx: int) -> float:
    for n, gb, layers, kvh in CODERS:
        if n == name:
            return gb * 1.15 + kv_gb(layers, kvh, ctx)
    return 6.0


def memory() -> Dict[str, float]:
    """RAM and graphics memory in GB (graphics: NVIDIA via nvidia-smi; 0 when unknown)."""
    out = {"ram_total": 0.0, "ram_available": 0.0, "vram_total": 0.0, "vram_free": 0.0, "gpu": ""}
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["ram_total"], out["ram_available"] = vm.total / 1e9, vm.available / 1e9
    except Exception:
        pass
    smi = shutil.which("nvidia-smi")
    if not smi and IS_WINDOWS:
        cand = r"C:\Windows\System32\nvidia-smi.exe"
        smi = cand if __import__("os").path.exists(cand) else None
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10,
                               creationflags=0x08000000 if IS_WINDOWS else 0)
            first = (r.stdout or "").strip().splitlines()[0]
            name, total, free = [x.strip() for x in first.split(",")]
            out["gpu"], out["vram_total"], out["vram_free"] = name, float(total) / 1024, float(free) / 1024
        except Exception:
            pass
    return out


def pick(mem: Optional[Dict[str, float]] = None, reserve_gb: float = RESERVE_GB,
         prefer_ctx: int = 16384) -> Tuple[str, int, str]:
    """(model, context tokens, why) -- the largest coder that fits, with the longest context that fits."""
    mem = mem or memory()
    budget = mem.get("vram_free", 0.0) + mem.get("ram_available", 0.0) - reserve_gb
    for ctx in (prefer_ctx, 8192):
        for name, gb, _layers, _kvh in CODERS:
            need = need_gb(name, ctx)
            if need <= budget:
                where = ("on the graphics card" if mem.get("vram_free", 0) >= need else
                         "split between the graphics card and the processor" if mem.get("vram_free", 0) > 1 else
                         "on the processor")
                return name, ctx, (f"{name} needs about {need:.1f} GB with a {ctx // 1024}K context; "
                                   f"{budget:.1f} GB is free ({where})")
    name = CODERS[-1][0]
    return name, 4096, f"memory is tight ({max(budget, 0):.1f} GB free): using the smallest coder, {name}"


def installed_coder(names: List[str]) -> Optional[str]:
    """The largest qwen2.5-coder already downloaded, if any."""
    have = {n.split("@")[0].lower() for n in names}
    for name, *_ in CODERS:
        if name in have or name + ":latest" in have:
            return name
    return None
