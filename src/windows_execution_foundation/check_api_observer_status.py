from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from datetime import datetime

FIXED_KEYS = (
    "observer_exists",
    "running",
    "last_run_at",
    "last_result",
    "next_run_at",
    "latest_report_at",
)

TASK_PATTERN = r"(?i)(api.*meter|meter.*api|integrated.*meter|api.*usage.*observer|api.*usage.*meter)"

def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

def _tail_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 131072))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}

def _integrated_meter_snapshot() -> dict[str, Any] | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / "TantanManagementOS" / "CodexBridge" / "runtime" / "airtable-request-ledger"
    window_path = root / "collection-window.json"
    if not window_path.is_file():
        return None
    window = _read_json(window_path)
    if window.get("meter_version") != "api-meter/2":
        return None

    health_files = sorted(root.glob("health-bridge_watcher-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    health = _read_json(health_files[0]) if health_files else {}
    ledgers = sorted(root.glob("bridge-watcher-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest = _tail_json(ledgers[0]) if ledgers else {}

    health_at = health.get("timestamp")
    fresh = False
    if isinstance(health_at, str):
        try:
            observed = datetime.fromisoformat(health_at.replace("Z", "+00:00"))
            fresh = abs((datetime.now(observed.tzinfo) - observed).total_seconds()) <= 180
        except ValueError:
            fresh = False

    state = str(window.get("state") or "").upper()
    running = state == "STARTED" and fresh
    latest_at = latest.get("timestamp") if isinstance(latest.get("timestamp"), str) else "UNKNOWN"
    success = latest.get("success")
    if success is True:
        last_result: Any = "SUCCESS"
    elif success is False:
        last_result = latest.get("error_category") or latest.get("error_type") or "FAILED"
    else:
        last_result = "UNKNOWN"

    return {
        "observer_exists": True,
        "running": running,
        "last_run_at": latest_at,
        "last_result": last_result,
        "next_run_at": "CONTINUOUS" if running else "UNKNOWN",
        "latest_report_at": health_at if isinstance(health_at, str) else latest_at,
    }

def _task_snapshot() -> list[dict[str, Any]]:
    script = (
        "$ErrorActionPreference='Stop';"
        "$p='" + TASK_PATTERN.replace("'", "''") + "';"
        "$r=@();"
        "Get-ScheduledTask | Where-Object {$_.TaskName -match $p} | ForEach-Object {"
        "$i=Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath;"
        "$r += [pscustomobject]@{"
        "name=$_.TaskName;state=[string]$_.State;"
        "last_run=$i.LastRunTime.ToString('o');"
        "last_result=$i.LastTaskResult;"
        "next_run=if($i.NextRunTime){$i.NextRunTime.ToString('o')}else{$null}"
        "}};"
        "$r | ConvertTo-Json -Depth 3 -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return []
    raw = completed.stdout.strip().lstrip("\ufeff")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)]

def collect() -> dict[str, Any]:
    integrated = _integrated_meter_snapshot()
    if integrated is not None:
        return integrated

    tasks = _task_snapshot()
    if not tasks:
        return {
            "observer_exists": False,
            "running": False,
            "last_run_at": "UNKNOWN",
            "last_result": "NOT_HOSTED_ON_THIS_DEVICE",
            "next_run_at": "UNKNOWN",
            "latest_report_at": "UNKNOWN",
        }
    tasks.sort(key=lambda item: str(item.get("last_run") or ""), reverse=True)
    latest = tasks[0]
    return {
        "observer_exists": True,
        "running": any(str(item.get("state") or "").lower() == "running" for item in tasks),
        "last_run_at": latest.get("last_run") or "UNKNOWN",
        "last_result": latest.get("last_result", "UNKNOWN"),
        "next_run_at": latest.get("next_run") or "UNKNOWN",
        "latest_report_at": "UNKNOWN",
    }

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    result = collect()
    if tuple(result.keys()) != FIXED_KEYS:
        raise RuntimeError("RESULT_SCHEMA_MISMATCH")
    _atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())