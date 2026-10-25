from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import ExecutionFoundation

# Zero means the outcome is complete or there was nothing to do.
# Every other operator-visible state has its own code so a caller can tell
# a retry, an unknown effect, a final failure, an audit failure, and a dead
# worker apart. Unknown statuses fail closed with 1.
EXIT_CODES: dict[str, int] = {
    "SUCCESS": 0,
    "ENQUEUED": 0,
    "NOOP_RECEIPT": 0,
    "NOOP_QUEUED": 0,
    "IDLE": 0,
    "HEALTHY": 0,
    "PASS": 0,
    "NOOP_SINGLE_WORKER": 0,
    "RETRY_SPOOLED": 10,
    "RESULT_UNKNOWN": 11,
    "FAILED_FINAL": 12,
    "ATTENTION": 13,
    "WORKER_STALE": 14,
    "WORKER_UNRESPONSIVE": 15,
    "DROPPED_BEFORE_LOCAL_RECEIPT": 16,
    "NOOP_UNKNOWN": 17,
}


def exit_code_for(result: dict[str, Any]) -> int:
    status = str(result.get("status") or "")
    if status not in EXIT_CODES:
        return 1
    return EXIT_CODES[status]


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
    return exit_code_for(result)
