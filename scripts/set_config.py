"""
scripts/set_config.py -- set one validated config value from the command line.

    python scripts/set_config.py llm.model qwen2.5:7b
    python scripts/set_config.py voice.tts.voice_ko ""

Used by the Windows installer: Windows PowerShell 5.1 writes JSON with a UTF-8 BOM, which
Python's json module rejects, so config edits go through the same validated code path the
Settings panel uses (agent/settings.py).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.settings import apply_settings  # noqa: E402


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__.strip())
        return 2
    key, value = argv[1], argv[2]
    result = apply_settings({key: value},
                            os.path.join(ROOT, "config", "jarvis.config.json"),
                            os.path.join(ROOT, "config", "permissions.json"))
    if key in result["rejected"]:
        print(f"rejected {key}: {result['rejected'][key]}")
        return 1
    print(f"set {key} = {result['applied'][key]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
