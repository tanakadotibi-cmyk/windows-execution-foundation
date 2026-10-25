from __future__ import annotations

import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
CONFIG = ROOT / "local" / "config.json"
LOG_DIR = ROOT / "local" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LANE = sys.argv[sys.argv.index("--lane") + 1] if "--lane" in sys.argv else "A"
if LANE not in ("A", "B"):
    raise ValueError("LANE_INVALID")
SUFFIX = "" if LANE == "A" else "-B"
LOG = LOG_DIR / f"foundation-worker-hidden{SUFFIX}-{datetime.now():%Y%m%d}.log"

sys.path.insert(0, str(SRC))
import json
COMMAND = "work-drain" if json.loads(CONFIG.read_text(encoding="utf-8")).get("lanes", {}).get("enabled") else "work-once"
sys.argv = ["windows_execution_foundation", "--config", str(CONFIG), "--lane", LANE, COMMAND]

with LOG.open("a", encoding="utf-8", newline="\n") as handle:
    with contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
        print(f"--- {datetime.now().isoformat()} ---")
        from windows_execution_foundation.cli import main
        raise SystemExit(main())
