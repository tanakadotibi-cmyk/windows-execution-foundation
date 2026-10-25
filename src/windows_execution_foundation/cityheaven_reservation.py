from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .core import CREATE_NO_WINDOW, atomic_write_json, iso_now, read_json, stable_key

from .reservation_pause import paused, pause_notice, parse_pause_notice, ReservationPauseExit

ACTION_TYPE = "CITYHEAVEN_RESERVATION_SCHEDULE"
ACTION_SCHEMA = "cityheaven_reservation_schedule_v1"
RESULT_SCHEMA = "cityheaven_reservation_result_v1"
ACTION_KEYS = {"schema_version", "reservation_key", "reservation_at", "cast", "course"}
PII_KEYS = {
    "customer", "customername", "customer_name", "name", "phone", "telephone",
    "email", "mail", "body", "message", "text", "content", "address",
}
TERMINAL_EXIT = 20
RETRY_EXIT = 30


class CityHeavenReservationError(RuntimeError):
    pass


def _choice(value: Any) -> str:
    return str(value.get("name") or "") if isinstance(value, dict) else str(value or "")


def _instant(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CityHeavenReservationError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CityHeavenReservationError(code) from exc
    if parsed.tzinfo is None:
        raise CityHeavenReservationError(code)
    return parsed.astimezone(UTC)


def _safe_text(value: Any, code: str, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise CityHeavenReservationError(code)
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise CityHeavenReservationError(code)
    return text


def validate_reservation_record(config: dict[str, Any], values: dict[str, Any], request_id: str,
                                *, expected_state: str = "EF_QUEUED", allow_past: bool = False,
                                now: datetime | None = None) -> tuple[dict[str, str], str]:
    field = dict(config["fields"])
    if str(values.get(field["schema"]) or "") != config["schema_version"]:
        raise CityHeavenReservationError("LOCAL_EXEC_SCHEMA_INVALID")
    if str(values.get(field["requested_host"]) or "") != config["device_id"]:
        raise CityHeavenReservationError("WRONG_HOST")
    if str(values.get(field["action_type"]) or "") != ACTION_TYPE:
        raise CityHeavenReservationError("REQUEST_ROUTE_INVALID")
    if _choice(values.get(field["state"])) != expected_state:
        raise CityHeavenReservationError("REQUEST_STATE_INVALID")
    payload_field = field.get("local_exec_payload")
    payload_value = values.get(payload_field) if payload_field else None
    raw_action = payload_value if str(payload_value or "").strip() else values.get(field["action_json"])
    try:
        action = json.loads(str(raw_action or ""))
    except (ValueError, TypeError) as exc:
        raise CityHeavenReservationError("REQUEST_JSON_INVALID") from exc
    if not isinstance(action, dict):
        raise CityHeavenReservationError("REQUEST_JSON_INVALID")
    normalized_keys = {str(key).replace("-", "_").lower() for key in action}
    if normalized_keys & PII_KEYS:
        raise CityHeavenReservationError("PII_KEY_FORBIDDEN")
    if set(action) != ACTION_KEYS:
        raise CityHeavenReservationError("REQUEST_FIELDS_INVALID")
    if action.get("schema_version") != ACTION_SCHEMA:
        raise CityHeavenReservationError("REQUEST_SCHEMA_INVALID")
    request_id = _safe_text(request_id, "REQUEST_ID_REQUIRED", 256)
    reservation_key = _safe_text(action.get("reservation_key"), "RESERVATION_KEY_INVALID", 256)
    if reservation_key != request_id:
        raise CityHeavenReservationError("REQUEST_ID_MISMATCH")
    reservation_at = _instant(action.get("reservation_at"), "RESERVATION_AT_INVALID")
    due_at = _instant(values.get(field["due_date"]), "DUE_DATE_INVALID")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not allow_past and due_at <= current:
        raise CityHeavenReservationError("DUE_DATE_PAST")
    result = {
        "schema_version": ACTION_SCHEMA,
        "reservation_key": reservation_key,
        "reservation_at": reservation_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cast": _safe_text(action.get("cast"), "CAST_INVALID", 120),
        "course": _safe_text(action.get("course"), "COURSE_INVALID", 240),
    }
    canonical = json.dumps(
        {"action": result, "due_at": due_at.isoformat(timespec="seconds").replace("+00:00", "Z")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return result, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TaskScheduler:
    def __init__(self, executable: str = "schtasks.exe"):
        self.executable = executable

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.executable, *arguments], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def exists(self, task_name: str) -> bool:
        return self._run(["/Query", "/TN", task_name]).returncode == 0

    def create(self, task_name: str, xml_path: Path) -> None:
        result = self._run(["/Create", "/TN", task_name, "/XML", str(xml_path), "/F"])
        if result.returncode != 0:
            raise CityHeavenReservationError("TASK_REGISTER_FAILED")

    def delete(self, task_name: str) -> bool:
        return self._run(["/Delete", "/TN", task_name, "/F"]).returncode == 0


def _task_name(reservation_key: str) -> str:
    return "ExecutionFoundation-CityHeaven-" + stable_key(reservation_key)[:16]


def _command_line(config_path: Path, manifest_path: Path) -> tuple[str, str, str]:
    root = Path(__file__).resolve().parents[2]
    launcher = root / "run-cityheaven-reservation.py"
    configured = Path(json.loads(config_path.read_text(encoding="utf-8"))["python_executable"])
    pythonw = configured.with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else configured
    arguments = f'"{launcher}" --config "{config_path}" run-once --manifest "{manifest_path}"'
    return str(executable), arguments, str(root)


def _task_xml(config_path: Path, manifest_path: Path, due_at: datetime,
              user_id: str, description: str) -> str:
    executable, arguments, working_directory = _command_line(config_path, manifest_path)
    local_due = due_at.astimezone()
    start = local_due.isoformat(timespec="seconds")
    end = local_due.replace(microsecond=0)
    end = datetime.fromtimestamp(end.timestamp() + 7 * 24 * 60 * 60, tz=end.tzinfo).isoformat(timespec="seconds")
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers><TimeTrigger><StartBoundary>{escape(start)}</StartBoundary><EndBoundary>{escape(end)}</EndBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user_id)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT5M</ExecutionTimeLimit><DeleteExpiredTaskAfter>PT1M</DeleteExpiredTaskAfter><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(working_directory)}</WorkingDirectory></Exec></Actions>
</Task>'''


def _paths(configuration: dict[str, Any], request_id: str) -> tuple[Path, Path, Path]:
    key_hash = stable_key(request_id)
    action = configuration["actions"]["cityheaven-reservation-schedule"]
    receipt_dir = Path(action["receipt_source_dir"])
    job_dir = Path(configuration["state_dir"]) / "cityheaven-reservations" / "jobs" / key_hash
    return job_dir, job_dir / "manifest.json", receipt_dir / f"{key_hash}.json"


def _validate_foundation_claim(configuration: dict[str, Any], request_id: str,
                               request_hash: str) -> None:
    key_hash = stable_key(request_id)
    claim_path = Path(configuration["state_dir"]) / "spool" / "inflight" / f"{key_hash}.json"
    claim = read_json(claim_path)
    metadata = claim.get("metadata") if isinstance(claim, dict) else None
    if (
        not isinstance(claim, dict)
        or claim.get("action") != "cityheaven-reservation-schedule"
        or claim.get("idempotency_key") != request_id
        or not isinstance(metadata, dict)
        or metadata.get("local_exec_action_type") != ACTION_TYPE
    ):
        raise CityHeavenReservationError("FOUNDATION_CLAIM_INVALID")
    if metadata.get("request_hash") != request_hash:
        raise CityHeavenReservationError("REQUEST_ID_INPUT_MISMATCH")


def _failure_receipt(request_id: str, request_hash: str | None, code: str,
                     action: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "reservation_key": request_id,
        "request_hash": request_hash or "UNKNOWN",
        "terminal_status": "FAILED",
        "reason_code": code,
        "observed_at": iso_now(),
        "reservation_at": (action or {}).get("reservation_at", "UNKNOWN"),
        "cast": (action or {}).get("cast", "UNKNOWN"),
        "course": (action or {}).get("course", "UNKNOWN"),
        "candidate_count": None,
        "observed_status": None,
        "reservation_ref": None,
        "course_source": None,
        "policy_version": None,
        "template_key": None,
        "chat_match_count": 0,
        "message_match_count": 0,
        "message_state": "UNDETERMINED",
        "would_send": False,
        "send_enabled": False,
        "chat_read_count": 0,
        "external_change_count": 0,
        "reservation_mutation_count": 0,
        "chat_open_count": 0,
        "customer_send_count": 0,
        "task_cleanup_status": "NOT_APPLICABLE_FOUNDATION_QUEUE",
    }


def _prefetch_canonical_policy(configuration: dict[str, Any]) -> dict[str, Any]:
    """Read the canonical policy through the parent Foundation Airtable route."""
    from .cityheaven_policy import CityHeavenPolicyError, load_canonical_policy

    try:
        return load_canonical_policy(configuration["local_exec_airtable"])
    except CityHeavenPolicyError as exc:
        raise CityHeavenReservationError(str(exc)) from exc
    except Exception as exc:
        raise CityHeavenReservationError("POLICY_LOAD_FAILED") from exc


def schedule(config_path: Path, request_id: str, *, scheduler: TaskScheduler | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """Execute a claimed Foundation reservation job directly.

    ``scheduler`` remains in the signature for source compatibility only.  The
    production Foundation path deliberately never creates a per-reservation
    Windows Scheduled Task.
    """
    if paused():
        return {"status": "PAUSED_BY_OWNER", "terminal_status": None, "pause_notice": pause_notice(True, "PYTHON_BEFORE_NODE")}
    from .airtable_local_exec import records_by_request_id

    configuration = json.loads(config_path.read_text(encoding="utf-8"))
    local_config = configuration["local_exec_airtable"]
    records = records_by_request_id(local_config, request_id)
    if len(records) != 1:
        raise CityHeavenReservationError("REQUEST_NOT_UNIQUE")
    record_fields = records[0].get("fields") or {}
    action, request_hash = validate_reservation_record(
        local_config, record_fields, request_id,
        expected_state="EF_RUNNING", allow_past=True, now=now,
    )
    _validate_foundation_claim(configuration, request_id, request_hash)
    job_dir, manifest_path, receipt_path = _paths(configuration, request_id)
    receipt = read_json(receipt_path)
    if receipt:
        if receipt.get("request_hash") != request_hash or receipt.get("reservation_key") != request_id:
            raise CityHeavenReservationError("REQUEST_ID_INPUT_MISMATCH")
        terminal = receipt.get("terminal_status")
        if terminal not in {"SUCCEEDED", "FAILED"}:
            raise CityHeavenReservationError("RECEIPT_INVALID")
        return {"status": "TERMINAL_RECEIPT", "terminal_status": terminal,
                "request_hash": request_hash}
    city_config = configuration["cityheaven_reservation"]
    from .cityheaven_policy import CANONICAL_VERSION

    foundation_root = Path(__file__).resolve().parents[2]
    manifest = {
        **action,
        "request_hash": request_hash,
        "receipt_path": str(receipt_path),
        "reader_path": str(Path(city_config.get("runtime_reader_path") or city_config["reader_path"])),
        "node_executable": str(Path(city_config.get("node_executable") or "node.exe")),
        "python_executable": str(Path(configuration.get("python_executable") or sys.executable)),
        "config_path": str(config_path.resolve()),
        "foundation_root": str(foundation_root),
        "foundation_src": str(foundation_root / "src"),
        "policy_version": CANONICAL_VERSION,
        "send_state_path": str(job_dir / "send-state.json"),
        "staff_notify_enabled": True,
    }
    if city_config.get("runtime_reader_path"):
        manifest["browser_runtime"] = {
            "browser": "CHROME",
            "chrome_executable": str(Path(city_config["chrome_executable"])),
            "profile_path": str(Path(city_config["dedicated_profile_path"])),
            "cdp_port": int(city_config["cdp_port"]),
            "base_reader_path": str(Path(city_config["reader_path"])),
            "startup_lock_path": str(Path(configuration["state_dir"]) / "runtime-locks" / "cityheaven-reservation-chrome.lock"),
            "start_url": "https://newmanager.cityheaven.net/",
        }
    existing = read_json(manifest_path)
    if existing and existing != manifest:
        raise CityHeavenReservationError("REQUEST_ID_INPUT_MISMATCH")
    policy = _prefetch_canonical_policy(configuration)
    job_dir.mkdir(parents=True, exist_ok=True)
    if not existing:
        atomic_write_json(manifest_path, manifest)
    exit_code = run_once(config_path, manifest_path, foundation_queue=True, policy=policy)
    if exit_code == RETRY_EXIT:
        return {"status": "PAUSED_BY_OWNER", "terminal_status": None, "exit_code": RETRY_EXIT,
                "pause_notice": exit_code.notice if isinstance(exit_code, ReservationPauseExit)
                else pause_notice(False, "UNKNOWN_CHILD_STATE")}
    terminal_receipt = read_json(receipt_path, {})
    return {
        "status": "DIRECT_EXECUTED",
        "terminal_status": terminal_receipt.get("terminal_status", "FAILED"),
        "request_hash": request_hash,
        "exit_code": exit_code,
    }


def run_once(config_path: Path, manifest_path: Path, *, scheduler: TaskScheduler | None = None,
             foundation_queue: bool = False, policy: dict[str, Any] | None = None) -> int:
    if paused():
        return ReservationPauseExit(pause_notice(True, "PYTHON_BEFORE_NODE"))
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        return TERMINAL_EXIT
    receipt_path = Path(str(manifest.get("receipt_path") or ""))
    task_name = str(manifest.get("task_name") or "")
    bridge = Path(__file__).with_name("cityheaven_reservation_once.js")
    node = str(manifest.get("node_executable") or "node.exe")
    exit_code = RETRY_EXIT
    held = False
    cleanup = "NOT_APPLICABLE_FOUNDATION_QUEUE" if foundation_queue else "DELETE_FAILED"
    try:
        if policy is None:
            configuration = json.loads(config_path.read_text(encoding="utf-8"))
            policy = _prefetch_canonical_policy(configuration)
        transport = json.dumps(
            {"schema_version": "cityheaven_policy_transport_v1", "policy": policy},
            ensure_ascii=False, separators=(",", ":"),
        )
        completed = subprocess.run(
            [node, str(bridge), str(manifest_path)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
            input=transport,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        notice = parse_pause_notice(completed.stdout, "node") if completed.returncode == RETRY_EXIT else None
        if completed.returncode == RETRY_EXIT and (paused() or notice is not None):
            held = True
            return ReservationPauseExit(notice or pause_notice(False, "UNKNOWN_CHILD_STATE"))
        exit_code = 0 if completed.returncode == 0 else TERMINAL_EXIT
        if not receipt_path.exists():
            action = {key: str(manifest.get(key) or "UNKNOWN") for key in ("reservation_at", "cast", "course")}
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                receipt_path,
                _failure_receipt(str(manifest.get("reservation_key") or "UNKNOWN"),
                                 str(manifest.get("request_hash") or "UNKNOWN"),
                                 "CITY_READ_FAILED", action),
            )
            exit_code = TERMINAL_EXIT
    except (OSError, subprocess.TimeoutExpired, CityHeavenReservationError) as exc:
        action = {key: str(manifest.get(key) or "UNKNOWN") for key in ("reservation_at", "cast", "course")}
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        code = str(exc) if isinstance(exc, CityHeavenReservationError) else "CITY_READ_FAILED"
        atomic_write_json(
            receipt_path,
            _failure_receipt(str(manifest.get("reservation_key") or "UNKNOWN"),
                             str(manifest.get("request_hash") or "UNKNOWN"),
                             code, action),
        )
        exit_code = TERMINAL_EXIT
    finally:
        if not held:
            if not foundation_queue:
                scheduler = scheduler or TaskScheduler()
                if task_name and scheduler.delete(task_name):
                    cleanup = "DELETED"
            receipt = read_json(receipt_path)
            if isinstance(receipt, dict):
                receipt["task_cleanup_status"] = cleanup
                atomic_write_json(receipt_path, receipt)
            if not foundation_queue:
                try:
                    shutil.rmtree(manifest_path.parent)
                except OSError:
                    pass
    if foundation_queue:
        return exit_code
    return exit_code if cleanup == "DELETED" else TERMINAL_EXIT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    schedule_parser = commands.add_parser("schedule")
    schedule_parser.add_argument("--request-id", required=True)
    run_parser = commands.add_parser("run-once")
    run_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run-once":
        code = run_once(args.config, args.manifest)
        if code == RETRY_EXIT:
            print(json.dumps(code.notice if isinstance(code, ReservationPauseExit)
                             else pause_notice(False, "UNKNOWN_CHILD_STATE")))
        return code
    try:
        result = schedule(args.config, args.request_id)
    except CityHeavenReservationError as exc:
        code = str(exc)
        if code == "TASK_REGISTER_FAILED":
            return RETRY_EXIT
        try:
            configuration = json.loads(args.config.read_text(encoding="utf-8"))
            _, _, receipt_path = _paths(configuration, args.request_id)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(receipt_path, _failure_receipt(args.request_id, None, code))
        except Exception:
            pass
        return TERMINAL_EXIT
    if result.get("status") == "PAUSED_BY_OWNER":
        print(json.dumps(result.get("pause_notice") or pause_notice(False, "UNKNOWN_CHILD_STATE")))
        return RETRY_EXIT
    if result.get("terminal_status") == "FAILED":
        return TERMINAL_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
