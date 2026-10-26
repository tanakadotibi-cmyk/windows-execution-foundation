from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .airtable_local_exec import LocalExecError, run_after_work, run_before_work
from .core import (
    RESULT_UNKNOWN, ExecutionFoundation, FoundationError, atomic_write_json, exclusive_lock, iso_now,
)

# Exit codes are state-specific. A local-exec reason still forces attention (2).
_EXIT_CODES = {
    "SUCCESS": 0,
    "IDLE": 0,
    "HEALTHY": 0,
    "PASS": 0,
    "DRAINED": 0,
    "ENQUEUED": 0,
    "NOOP_RECEIPT": 0,
    "NOOP_QUEUED": 0,
    "NOOP_SINGLE_WORKER": 0,
    "RESOURCE_WAIT": 0,
    "DEFERRED_EXTERNAL": 0,
    "RETRY_SPOOLED": 0,
    "PAUSED_BY_OWNER": 0,
    RESULT_UNKNOWN: 1,
    "FAILED_FINAL": 2,
    "FAILED_REQUEUE_RELEASE": 2,
    "ATTENTION": 2,
    "WORKER_STALE": 3,
    "WORKER_BUSY_STALE_HEARTBEAT": 3,
}

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

def _record_local_exec_errors(foundation: ExecutionFoundation, errors: list[dict[str, Any]]) -> str | None:
    """Write the auxiliary record. A write failure returns the error type and does not raise."""
    if not errors:
        return None
    try:
        atomic_write_json(foundation.root / "monitoring" / "local-exec-error.json", {
            "observed_at": iso_now(),
            "phases": errors,
        })
    except Exception as exc:
        return type(exc).__name__ or "WRITE_FAILED"
    return None

def _apply_local_exec_reason(result: dict[str, Any], errors: list[dict[str, Any]], record_error: str | None) -> None:
    reason = next((item.get("reason_code") for item in errors if item.get("reason_code")), None)
    if reason:
        result["local_exec_reason_code"] = reason
    if record_error:
        result["local_exec_record_write_error"] = record_error


# A multi-job drain must not hide a closed failure or an unknown result behind DRAINED.
_DRAIN_STATUS_PRIORITY = (
    "FAILED_FINAL",
    "FAILED_REQUEUE_RELEASE",
    RESULT_UNKNOWN,
    "ATTENTION",
)


def _drain_summary_status(outcomes: list[dict[str, Any]]) -> str:
    present = {str(item.get("status") or "") for item in outcomes}
    for status in _DRAIN_STATUS_PRIORITY:
        if status in present:
            return status
    return "DRAINED"


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
                if result["status"] in {"IDLE", "RESOURCE_WAIT", "NOOP_SINGLE_WORKER", RESULT_UNKNOWN}:
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
    record_error = _record_local_exec_errors(foundation, errors)
    if max_jobs == 1:
        _apply_local_exec_reason(outcomes[0], errors, record_error)
        return outcomes[0]
    summary_status = _drain_summary_status(outcomes)
    summary = {"status": summary_status,
               "lane_id": foundation.config.get("lane_id", "A"), "outcomes": outcomes}
    _apply_local_exec_reason(summary, errors, record_error)
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
    """Print the cycle result and exit with a code for that state.

    0 is a completed, waiting, or healthy state. 1 is 結果不明 and is not resent.
    2 is a final failure, audit attention, or a local-exec reason. 3 is a worker
    whose heartbeat expired and whose process is not alive.
    """
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("local_exec_reason_code"):
        return 2
    return _EXIT_CODES.get(str(result.get("status") or ""), 2)

if __name__ == "__main__":
    raise SystemExit(main())
