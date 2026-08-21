from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import ExecutionFoundation


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Execution Foundation v1")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--action", required=True)
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument("--arg", action="append", default=[])
    commands.add_parser("work-once")
    watchdog = commands.add_parser("watchdog")
    watchdog.add_argument("--no-restart", action="store_true")
    commands.add_parser("audit")
    args = parser.parse_args()
    foundation = ExecutionFoundation.from_path(args.config)
    if args.command == "enqueue":
        result = foundation.enqueue(action=args.action, idempotency_key=args.idempotency_key, args=args.arg)
    elif args.command == "work-once":
        result = foundation.work_once()
    elif args.command == "watchdog":
        result = foundation.watchdog(restart=not args.no_restart)
    else:
        result = foundation.audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") not in {"FAILED_FINAL", "ATTENTION"} else 2
