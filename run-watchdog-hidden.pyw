from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
CONFIG = ROOT / "local" / "config.json"
LOG_DIR = ROOT / "local" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG = LOG_DIR / f"foundation-watchdog-hidden-{datetime.now():%Y%m%d}.log"

sys.path.insert(0, str(SRC))
sys.argv = ["windows_execution_foundation", "--config", str(CONFIG), "watchdog"]

with LOG.open("a", encoding="utf-8", newline="\n") as handle:
    with contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
        print(f"--- {datetime.now().isoformat()} ---")
        from windows_execution_foundation.cli import main
        exit_code = main()
        import json
        _lanes = json.loads(CONFIG.read_text(encoding="utf-8")).get("lanes", {})
        if _lanes.get("enabled") and "B" in _lanes.get("watchdog_lanes", ["A", "B"]):
            sys.argv = ["windows_execution_foundation", "--config", str(CONFIG), "--lane", "B", "watchdog"]
            exit_code = max(exit_code, main())
        raise SystemExit(exit_code)
