from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .airtable_local_exec import LocalExecError, run_after_work, run_before_work
from .core import (
    ExecutionFoundation, FoundationError, atomic_write_json, exclusive_lock, iso_now,
)

def _enabled_local_exec(foundation: ExecutionFoundation) -> dict[str, Any] | None:
    config = foundation.config.get("local_exec_airtable")
    return config if isinstance(config, dict) and config.get("enabled") is True else None

def _safe_cycle(name: str, foundation: ExecutionFoundation, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "before":
            return run_before_work(foundation, config)
        return run_after_work(foundation, config)
    except LocalExecError as exc:
        return {"status": "ERROR", "error_type": "LocalExecError", "reason_code": str(exc)}
    except Exception as exc:
        return {"status": "ERROR", "error_type": type(exc).__name__}

def _local_exec_errors(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for index, item in enumerate(outcomes):
        local_exec = item.get("local_exec") if isinstance(item, dict) else None
        if not isinstance(local_exec, dict):
            continue
        for phase in ("before", "after"):
            payload = local_exec.get(phase)
            if not isinstance(payload, dict) or payload.get("status") != "ERROR":
                continue
            if "reason_code" not in payload:
                continue
            found.append({
                "outcome_index": index,
                "phase": phase,
                "error_type": payload.get("error_type"),
                "reason_code": payload.get("reason_code"),
            })
    return found

def _record_local_exec_errors(foundation: ExecutionFoundation, errors: list[dict[str, Any]]) -> None:
    if not errors:
        return
    atomic_write_json(foundation.root / "monitoring" / "local-exec-error.json", {
        "observed_at": iso_now(),
        "phases": errors,
    })

def _apply_local_exec_reason(result: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    reason = next((item.get("reason_code") for item in errors if item.get("reason_code")), None)
    if reason:
        result["local_exec_reason_code"] = reason


def work_cycle(foundation: ExecutionFoundation, max_jobs: int = 1) -> dict[str, Any]:
    """Serialize ingestion as well as execution within a lane; drain at most two.

    Production task timeout is 40 min: two 900s actions plus bounded API calls.
    Delayed retries/resource conflicts yield to the existing five-minute trigger.
    """
    outcomes = []
    try:
        with exclusive_lock(foundation.root / ".cycle.lock"):
            config = _enabled_local_exec(foundation)
            for _ in range(max_jobs):
                before = _safe_cycle("before", foundation, config) if config else None
                result = foundation.work_once()
                after = _safe_cycle("after", foundation, config) if config else None
                if config:
                    result["local_exec"] = {"before": before, "after": after}
                outcomes.append(result)
                if result["status"] in {"IDLE", "RESOURCE_WAIT", "NOOP_SINGLE_WORKER"}:
                    break
    except FoundationError as exc:
        if str(exc) != "SINGLE_WORKER_LOCKED":
            raise
        return {"status": "NOOP_SINGLE_WORKER"}
    try:
        from .lane_monitor import observe_cycle
        observe_cycle(foundation, outcomes)  # RESOURCE_WAIT >15 min -> Slack (best-effort)
    except Exception:
        pass
    errors = _local_exec_errors(outcomes)
    _record_local_exec_errors(foundation, errors)
    if max_jobs == 1:
        _apply_local_exec_reason(outcomes[0], errors)
        return outcomes[0]
    summary = {"status": "FAILED_FINAL" if any(x["status"] == "FAILED_FINAL" for x in outcomes) else "DRAINED",
               "lane_id": foundation.config.get("lane_id", "A"), "outcomes": outcomes}
    _apply_local_exec_reason(summary, errors)
    return summary

def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Execution Foundation v1")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lane", choices=("A", "B"), default="A")
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--action", required=True)
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument("--arg", action="append", default=[])
    commands.add_parser("work-once")
    commands.add_parser("work-drain")
    watchdog = commands.add_parser("watchdog")
    watchdog.add_argument("--no-restart", action="store_true")
    commands.add_parser("audit")
    args = parser.parse_args()

    foundation = ExecutionFoundation.from_path(args.config, args.lane)
    if args.command == "enqueue":
        result = foundation.enqueue(action=args.action, idempotency_key=args.idempotency_key, args=args.arg)
    elif args.command in {"work-once", "work-drain"}:
        result = work_cycle(foundation, max_jobs=2 if args.command == "work-drain" else 1)
    elif args.command == "watchdog":
        result = foundation.watchdog(restart=not args.no_restart)
    else:
        result = foundation.audit()

    return _finish(result)

def _finish(result: dict[str, Any]) -> int:
    """Print the cycle result and exit. A local-exec reason stays in the output and the status."""
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") in {"FAILED_FINAL", "ATTENTION"} or result.get("local_exec_reason_code"):
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
