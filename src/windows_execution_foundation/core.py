from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .reservation_pause import action_paused, parse_pause_notice

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FoundationError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_timestamp(value: Any) -> tuple[str, datetime | None]:
    """Classify a timestamp. A value without an offset is not treated as UTC or JST."""
    if value is None or value == "":
        return "absent", None
    if not isinstance(value, str):
        return "unreadable", None
    try:
        parsed = parse_time(value)
    except (TypeError, ValueError):
        return "unreadable", None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return "naive", None
    return "aware", parsed


def invalid_timestamp_record(source: str, record_id: Any, field: str, value: Any) -> dict[str, Any] | None:
    kind, _parsed = classify_timestamp(value)
    if kind == "naive":
        reason = "時差のない時刻"
    elif kind == "unreadable":
        reason = "読めない時刻"
    else:
        return None
    return {
        "category": "不正な記録",
        "source": source,
        "id": record_id,
        "field": field,
        "reason": reason,
        "value": value if isinstance(value, str) else None,
    }


def stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise FoundationError("SINGLE_WORKER_LOCKED") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise FoundationError("SINGLE_WORKER_LOCKED") from exc
        acquired = True
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} at={iso_now()}".encode())
        handle.flush()
        yield
    finally:
        try:
            if not acquired:
                pass
            elif os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class ExecutionFoundation:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root = Path(config["state_dir"]).resolve()
        self.pending = self.root / "spool" / "pending"
        self.inflight = self.root / "spool" / "inflight"
        self.done = self.root / "spool" / "done"
        self.failed = self.root / "spool" / "failed"
        self.receipts = self.root / "receipts"
        self.heartbeats = self.root / "heartbeats"
        for path in (self.pending, self.inflight, self.done, self.failed, self.receipts, self.heartbeats):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_path(cls, path: Path, lane: str = "A") -> "ExecutionFoundation":
        from .lanes import load_config
        return cls(load_config(path, lane))

    def receipt_path(self, key_hash: str) -> Path:
        return self.receipts / f"{key_hash}.json"

    def enqueue(self, *, action: str, idempotency_key: str, args: list[str] | None = None,
                metadata: dict[str, Any] | None = None,
                not_before_epoch: float = 0) -> dict[str, Any]:
        if action_paused(action):
            return {"status": "PAUSED_BY_OWNER", "action": action}
        if action not in self.config.get("actions", {}):
            raise FoundationError(f"ACTION_NOT_ALLOWED:{action}")
        key_hash = stable_key(idempotency_key)
        receipt = read_json(self.receipt_path(key_hash))
        if receipt:
            return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
        for directory in (self.pending, self.inflight):
            existing = read_json(directory / f"{key_hash}.json")
            if existing:
                return {"status": "NOOP_QUEUED", "dedup_key": key_hash, "job_id": existing["job_id"]}
        job = {
            "schema_version": "1.0",
            "job_id": uuid.uuid4().hex,
            "action": action,
            "args": list(args or []),
            "idempotency_key": idempotency_key,
            "dedup_key": key_hash,
            "attempt": 0,
            "created_at": iso_now(),
            "not_before_epoch": not_before_epoch,
            "metadata": metadata or {},
        }
        with exclusive_lock(self.root / ".spool.lock"):
            receipt = read_json(self.receipt_path(key_hash))
            if receipt:
                return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
            if (self.pending / f"{key_hash}.json").exists() or (self.inflight / f"{key_hash}.json").exists():
                return {"status": "NOOP_QUEUED", "dedup_key": key_hash}
            failed = read_json(self.failed / f"{key_hash}.json", {})
            old_lease = failed.get("resource_lease") or {}
            shared = self.config.get("shared_resource_dir")
            if old_lease and shared:
                active = Path(shared) / "active" / (str(old_lease.get("token", "MISSING")) + ".json")
                if active.exists():
                    # A rescue must first prove the old owner/children dead and
                    # release the exact lease. Requeue never force-releases it.
                    raise FoundationError("FAILED_REQUEUE_RESOURCE_LEASE_ACTIVE")
            atomic_write_json(self.pending / f"{key_hash}.json", job)
        return {"status": "ENQUEUED", "dedup_key": key_hash, "job_id": job["job_id"]}

    def heartbeat(self, name: str, status: str, **extra: Any) -> dict[str, Any]:
        value = {"name": name, "status": status, "observed_at": iso_now(), "pid": os.getpid(), **extra}
        atomic_write_json(self.heartbeats / f"{name}.json", value)
        return value

    def _network_ready(self) -> bool:
        if os.environ.get("WEF_FAULT_NETWORK_DOWN") == "1":
            return False
        url = self.config.get("network_probe_url")
        if not url:
            return True
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "WindowsExecutionFoundation/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 500
        except Exception:
            return False

    def _inflight_stale_limit(self, job: dict[str, Any]) -> int:
        base = int(self.config.get("inflight_stale_seconds", 900))
        grace = int(self.config.get("inflight_stale_grace_seconds", 300))
        action = self.config.get("actions", {}).get(str(job.get("action") or ""), {})
        timeout = int(action.get("timeout_seconds", 0) or 0) if isinstance(action, dict) else 0
        return max(base, timeout + grace)

    def recover_stale(self) -> int:
        now = datetime.now(UTC)
        recovered = 0
        with exclusive_lock(self.root / ".spool.lock"):
            for path in self.inflight.glob("*.json"):
                job = read_json(path, {})
                if action_paused(job.get("action")):
                    continue
                maximum = self._inflight_stale_limit(job)
                claimed = job.get("claimed_at")
                if not claimed:
                    age = maximum + 1
                else:
                    age = (now - parse_time(claimed)).total_seconds()
                if age > maximum:
                    job["recovered_at"] = iso_now()
                    job["recovered_after_seconds"] = int(age)
                    job.pop("claimed_at", None)
                    atomic_write_json(self.pending / path.name, job)
                    path.unlink(missing_ok=True)
                    recovered += 1
        return recovered

    def _claim(self) -> tuple[Path, dict[str, Any]] | None:
        from .lanes import acquire_resource
        now = time.time()
        self.resource_wait = False
        with exclusive_lock(self.root / ".spool.lock"):
            for source in sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime):
                job = read_json(source, {})
                if action_paused(job.get("action")):
                    continue
                if float(job.get("not_before_epoch", 0)) > now:
                    continue
                if self.receipt_path(job["dedup_key"]).exists():
                    source.unlink(missing_ok=True)
                    continue
                try:
                    lease = acquire_resource(self, job)
                except FoundationError as exc:
                    if str(exc) != "SINGLE_WORKER_LOCKED":
                        raise
                    lease = None
                if lease is None:
                    self.resource_wait = True
                    continue
                if action_paused(job.get("action")):
                    # This exact lease was just acquired; no child or claim exists.
                    from .lanes import release_resource
                    try:
                        release_resource(self, lease)
                    except Exception:
                        self.resource_wait = True  # Preserve foreign/uncertain lease.
                    continue
                job["resource_lease"] = lease
                destination = self.inflight / source.name
                job["claimed_at"] = iso_now()
                job["worker_pid"] = os.getpid()
                atomic_write_json(destination, job)
                source.unlink(missing_ok=True)
                return destination, job
        return None

    def _command(self, job: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        action = self.config["actions"][job["action"]]
        supplied = list(job.get("args", []))
        allowed = set(action.get("allowed_args", []))
        if any(arg not in allowed for arg in supplied):
            raise FoundationError("ARG_NOT_ALLOWED")
        command = [str(item) for item in action["command"]] + supplied
        if action.get("append_airtable_record_id"):
            record_id = str((job.get("metadata") or {}).get("airtable_record_id") or "").strip()
            if not record_id:
                raise FoundationError("AIRTABLE_RECORD_ID_REQUIRED")
            command += ["--record-id", record_id]
        if action.get("append_idempotency_key"):
            command += ["--request-id", str(job["idempotency_key"])]
        return command, action

    def _external_receipt(self, action: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any] | None:
        source = action.get("receipt_source")
        if not source and action.get("receipt_source_dir") and job:
            source = str(Path(action["receipt_source_dir"]) / f"{job['dedup_key']}.json")
        if not source:
            return None
        path = Path(source)
        if not path.exists():
            return None
        value = read_json(path, {})
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "content": value}

    def _fail_requeue_closed(self, path: Path, job: dict[str, Any], error: str,
                             original_error: str) -> dict[str, Any]:
        job["last_error"] = error
        job["requeue_error"] = original_error
        job["last_failed_at"] = iso_now()
        persisted = {key: value for key, value in job.items()
                     if key != "_resource_release_attempted"}
        atomic_write_json(self.failed / path.name, persisted)
        path.unlink(missing_ok=True)
        self.heartbeat("worker", "IDLE", last_dedup_key=job["dedup_key"])
        return {"status": "FAILED_REQUEUE_RELEASE", "attempt": int(job.get("attempt", 0)),
                "error": error, "dedup_key": job["dedup_key"]}

    def _release_before_pending(self, path: Path, job: dict[str, Any], *,
                                safe_to_release: bool, original_error: str) -> dict[str, Any] | None:
        if not safe_to_release:
            return self._fail_requeue_closed(
                path, job, f"RESOURCE_RELEASE_UNSAFE:{original_error}", original_error)
        job["_resource_release_attempted"] = True
        try:
            from .lanes import release_resource
            release_resource(self, job.get("resource_lease") or {})
        except Exception as exc:
            return self._fail_requeue_closed(
                path, job, f"RESOURCE_RELEASE_FAILED:{type(exc).__name__}:{exc}", original_error)
        # A pending job is unowned. It must acquire a fresh token at its next claim.
        job.pop("resource_lease", None)
        job.pop("claimed_at", None)
        job.pop("worker_pid", None)
        job.pop("_resource_release_attempted", None)
        return None

    def _defer_external(self, path: Path, job: dict[str, Any], action: dict[str, Any],
                        *, safe_to_release: bool) -> dict[str, Any]:
        delay = max(1, int(action.get("external_poll_seconds", 60)))
        job["not_before_epoch"] = time.time() + delay
        job["external_wait_started_at"] = job.get("external_wait_started_at") or iso_now()
        failed = self._release_before_pending(
            path, job, safe_to_release=safe_to_release, original_error="EXTERNAL_RESULT_PENDING")
        if failed:
            return failed
        atomic_write_json(self.pending / path.name, job)
        path.unlink(missing_ok=True)
        self.heartbeat("worker", "IDLE", last_dedup_key=job["dedup_key"])
        return {
            "status": "DEFERRED_EXTERNAL",
            "retry_in_seconds": delay,
            "dedup_key": job["dedup_key"],
        }

    def _fail_final(self, path: Path, job: dict[str, Any], error: str,
                    external: dict[str, Any] | None = None) -> dict[str, Any]:
        job["attempt"] = int(job.get("attempt", 0)) + 1
        job["last_error"] = error
        job["last_failed_at"] = iso_now()
        if external is not None:
            job["terminal_external_receipt"] = external
        atomic_write_json(self.failed / path.name, job)
        path.unlink(missing_ok=True)
        self.heartbeat("worker", "IDLE", last_dedup_key=job["dedup_key"])
        return {"status": "FAILED_FINAL", "attempt": job["attempt"], "error": error,
                "dedup_key": job["dedup_key"]}

    def _retry(self, path: Path, job: dict[str, Any], error: str,
               *, safe_to_release: bool) -> dict[str, Any]:
        attempt = int(job.get("attempt", 0)) + 1
        job["attempt"] = attempt
        job["last_error"] = error
        job["last_failed_at"] = iso_now()
        maximum = int(self.config.get("max_attempts", 5))
        if attempt >= maximum:
            atomic_write_json(self.failed / path.name, job)
            path.unlink(missing_ok=True)
            return {"status": "FAILED_FINAL", "attempt": attempt, "error": error, "dedup_key": job["dedup_key"]}
        delay = int(self.config.get("retry_base_seconds", 15)) * (2 ** (attempt - 1))
        job["not_before_epoch"] = time.time() + delay
        failed = self._release_before_pending(
            path, job, safe_to_release=safe_to_release, original_error=error)
        if failed:
            return failed
        atomic_write_json(self.pending / path.name, job)
        path.unlink(missing_ok=True)
        return {"status": "RETRY_SPOOLED", "attempt": attempt, "retry_in_seconds": delay,
                "error": error, "dedup_key": job["dedup_key"]}

    def _hold_reservation(self, job: dict[str, Any], *, no_child_confirmed: bool) -> dict[str, Any]:
        # Never rewrite/remove pending, inflight, or receipt on cancellation.
        released = False
        job["_resource_release_attempted"] = True  # In-memory only; prevent double release.
        if no_child_confirmed is True:
            from .lanes import release_resource
            try:
                release_resource(self, job.get("resource_lease") or {})
                released = True
            except Exception:
                pass  # No force release: existing owner/token contract remains intact.
        return {"status": "PAUSED_BY_OWNER", "action": job["action"],
                "resource_release_safe": released}

    def work_once(self) -> dict[str, Any]:
        try:
            with exclusive_lock(self.root / ".worker.lock"):
                self.heartbeat("worker", "RUNNING")
                claimed = self._claim()
                if claimed is None:
                    self.heartbeat("worker", "IDLE")
                    return {"status": "RESOURCE_WAIT" if self.resource_wait else "IDLE"}
                path, job = claimed
                launch_stage = "UNKNOWN"
                safe_to_release = False
                try:
                    # The worker owns the exact lease and no child launch has
                    # been attempted in this try block yet.
                    launch_stage = "PRELAUNCH_CONFIRMED"
                    safe_to_release = True
                    if action_paused(job.get("action")):
                        return self._hold_reservation(job, no_child_confirmed=True)
                    command, action = self._command(job)
                    if action.get("requires_remote"):
                        raise FoundationError("REMOTE_DEPENDENCY_FORBIDDEN")
                    if action.get("requires_network") and not self._network_ready():
                        return self._retry(path, job, "NETWORK_UNAVAILABLE",
                                           safe_to_release=safe_to_release)
                    if os.environ.get("WEF_FAULT_WORKER_CRASH") == "1":
                        raise FoundationError("INJECTED_WORKER_CRASH")
                    # subprocess.run may raise after creating a process. From
                    # this point, absence of a returned result is ambiguous.
                    launch_stage = "SPAWN_OUTCOME_UNKNOWN"
                    safe_to_release = False
                    completed = subprocess.run(
                        command,
                        cwd=action.get("working_directory"),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=int(action.get("timeout_seconds", 900)),
                        check=False,
                        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    launch_stage = "SPAWN_RETURNED"
                    if (job.get("action") == "cityheaven-reservation-schedule"
                            and completed.returncode == 30):
                        safe_to_release = False
                        notice = parse_pause_notice(completed.stdout, "parent")
                        return self._hold_reservation(
                            job, no_child_confirmed=notice is not None and notice["no_child_confirmed"] is True)
                    external = self._external_receipt(action, job)
                    content = (external or {}).get("content") or {}
                    safe_to_release = job["action"] != "codex-local-job" or (
                        completed.returncode == 0 and content.get("request_id") == job["idempotency_key"]
                        and content.get("resource_release_safe") is True)
                    if not self.config.get("shared_resource_dir"):
                        safe_to_release = True
                    if completed.returncode != 0:
                        error = f"EXIT_{completed.returncode}"
                        if not action.get("redact_subprocess_output"):
                            error += f":{completed.stderr[-1000:]}"
                        if completed.returncode in set(action.get("terminal_failure_exit_codes", [])):
                            return self._fail_final(path, job, error, external)
                        return self._retry(path, job, error, safe_to_release=safe_to_release)
                    if action.get("external_async") and external is None:
                        return self._defer_external(path, job, action,
                                                    safe_to_release=safe_to_release)
                    if action.get("receipt_source") and external is None:
                        return self._retry(path, job, "EXTERNAL_RECEIPT_MISSING",
                                           safe_to_release=safe_to_release)
                    if action.get("receipt_source_dir") and external is None:
                        return self._retry(path, job, "EXTERNAL_RECEIPT_MISSING",
                                           safe_to_release=safe_to_release)
                    if action.get("receipt_request_id_field"):
                        content = external.get("content") if external else None
                        if not isinstance(content, dict) or content.get(action["receipt_request_id_field"]) != job["idempotency_key"]:
                            return self._retry(path, job, "EXTERNAL_RECEIPT_REQUEST_MISMATCH",
                                               safe_to_release=safe_to_release)
                    receipt = {
                        "schema_version": "1.0",
                        "lane_id": self.config.get("lane_id", "A"),
                        "status": "SUCCESS",
                        "job_id": job["job_id"],
                        "action": job["action"],
                        "idempotency_key": job["idempotency_key"],
                        "dedup_key": job["dedup_key"],
                        **({"work_number": job["metadata"]["work_number"]}
                           if (job.get("metadata") or {}).get("work_number") else {}),
                        **({key: job["metadata"][key] for key in (
                            "work_identity_key", "display_name_ja", "stage_key", "completion_scope",
                            "existing_action_id", "existing_work_number",
                        ) if (job.get("metadata") or {}).get(key) is not None}),
                        "completed_at": iso_now(),
                        "attempt": int(job.get("attempt", 0)) + 1,
                        "exit_code": completed.returncode,
                        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
                        "external_receipt": external,
                    }
                    atomic_write_json(self.receipt_path(job["dedup_key"]), receipt)
                    atomic_write_json(self.done / path.name, {**job, "receipt": receipt})
                    path.unlink(missing_ok=True)
                    self.heartbeat("worker", "IDLE", last_dedup_key=job["dedup_key"])
                    return receipt
                except subprocess.TimeoutExpired as exc:
                    return self._retry(path, job, f"TIMEOUT:{exc.timeout}",
                                       safe_to_release=False)
                except Exception as exc:
                    return self._retry(path, job, f"{type(exc).__name__}:{exc}",
                                       safe_to_release=(
                                           launch_stage == "PRELAUNCH_CONFIRMED"
                                           and safe_to_release
                                       ))
                finally:
                    if (launch_stage in {"PRELAUNCH_CONFIRMED", "SPAWN_RETURNED"}
                            and safe_to_release
                            and not job.get("_resource_release_attempted")):
                        from .lanes import release_resource
                        release_resource(self, job.get("resource_lease") or {})
        except FoundationError as exc:
            if str(exc) == "SINGLE_WORKER_LOCKED":
                return {"status": "NOOP_SINGLE_WORKER"}
            raise

    def watchdog(self, *, restart: bool = True) -> dict[str, Any]:
        worker = read_json(self.heartbeats / "worker.json", {})
        maximum = int(self.config.get("worker_heartbeat_max_age_seconds", 900))
        stale = True
        heartbeat_age = None
        if worker.get("observed_at"):
            heartbeat_age = (datetime.now(UTC) - parse_time(worker["observed_at"])).total_seconds()
            stale = heartbeat_age > maximum

        recovered = 0
        restart_result = "NOT_NEEDED"
        worker_lock_held = False
        status = "HEALTHY"

        if stale:
            try:
                with exclusive_lock(self.root / ".worker.lock"):
                    recovered = self.recover_stale()
            except FoundationError as exc:
                if str(exc) != "SINGLE_WORKER_LOCKED":
                    raise
                worker_lock_held = True
                status = "WORKER_BUSY_STALE_HEARTBEAT"
                restart_result = "DEFERRED_LOCK_HELD"
            else:
                status = "WORKER_STALE"
                if restart and os.name == "nt" and self.config.get("worker_task_name"):
                    result = subprocess.run(
                        ["schtasks.exe", "/run", "/tn", self.config["worker_task_name"]],
                        capture_output=True,
                        text=True,
                        check=False,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    restart_result = "TRIGGERED" if result.returncode == 0 else f"FAILED:{result.returncode}"

        value = self.heartbeat(
            "watchdog",
            status,
            recovered_inflight=recovered,
            restart_result=restart_result,
            worker_lock_held=worker_lock_held,
        )
        value["worker_stale"] = stale
        try:
            from .lane_monitor import watchdog_alert
            alerts = watchdog_alert(self, value, heartbeat_age)
            if alerts:
                value["alerts"] = alerts
        except Exception as exc:  # Alerting must never break the watchdog.
            value["alert_error"] = type(exc).__name__
        return value

    def _eligible_since(self, job: dict[str, Any], not_before: float) -> datetime | None:
        kind, created = classify_timestamp(job.get("created_at"))
        if kind != "aware" or created is None:
            return None
        if not_before > 0:
            ready = datetime.fromtimestamp(not_before, UTC)
            return ready if ready > created else created
        return created

    def _queue_delay(self, pending: list[Path]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Deadline check for pending receipts. Bad clocks become attention items, not exceptions."""
        now = datetime.now(UTC)
        allowed = int(self.config.get("pending_allowed_delay_seconds", 900))
        oldest: datetime | None = None
        overdue: list[Any] = []
        unprocessable_pending: list[str] = []
        invalid_records: list[dict[str, Any]] = []
        eligible = 0
        ineligible = 0
        for path in pending:
            try:
                job = read_json(path, {})
            except (json.JSONDecodeError, OSError, UnicodeError):
                unprocessable_pending.append(path.stem)
                continue
            if not isinstance(job, dict):
                unprocessable_pending.append(path.stem)
                continue
            try:
                not_before = float(job.get("not_before_epoch") or 0)
            except (TypeError, ValueError):
                unprocessable_pending.append(path.stem)
                continue
            record_id = job.get("execution_id") or job.get("job_id") or path.stem
            invalid = invalid_timestamp_record("pending", record_id, "created_at", job.get("created_at"))
            if invalid is not None:
                invalid_records.append(invalid)
                continue
            if not_before > time.time():
                ineligible += 1
                continue
            eligible += 1
            try:
                start = self._eligible_since(job, not_before)
                if start is None:
                    overdue.append(record_id)
                    continue
                if oldest is None or start < oldest:
                    oldest = start
                if (now - start).total_seconds() > allowed:
                    overdue.append(record_id)
            except (TypeError, ValueError, OverflowError, OSError):
                invalid_records.append(
                    {
                        "category": "不正な記録",
                        "source": "pending",
                        "id": record_id,
                        "field": "created_at",
                        "reason": "時刻の比較に失敗",
                        "value": job.get("created_at") if isinstance(job.get("created_at"), str) else None,
                    }
                )
        queue_status = "PASS" if not overdue and not unprocessable_pending else "ATTENTION"
        oldest_wait = 0.0 if oldest is None else (now - oldest).total_seconds()
        queue_delay = {
            "status": queue_status,
            "pending_count": len(pending),
            "eligible_pending_count": eligible,
            "ineligible_pending_count": ineligible,
            "oldest_waiting_at": (
                oldest.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if oldest else None
            ),
            "oldest_wait_seconds": oldest_wait,
            "allowed_delay_seconds": allowed,
            "overdue_count": len(overdue),
            "unprocessable_pending": unprocessable_pending,
        }
        return queue_delay, invalid_records

    def audit(self) -> dict[str, Any]:
        pending = list(self.pending.glob("*.json"))
        inflight = list(self.inflight.glob("*.json"))
        done = list(self.done.glob("*.json"))
        failed = list(self.failed.glob("*.json"))
        receipts = list(self.receipts.glob("*.json"))
        keys = [p.stem for p in receipts]
        duplicate_receipts = len(keys) - len(set(keys))
        queue_delay, invalid_records = self._queue_delay(pending)
        structural_ok = not inflight and not failed and duplicate_receipts == 0
        status = (
            "PASS"
            if structural_ok and queue_delay["status"] == "PASS" and not invalid_records
            else "ATTENTION"
        )
        result = {
            "status": status,
            "observed_at": iso_now(),
            "counts": {"pending": len(pending), "inflight": len(inflight), "done": len(done),
                       "failed": len(failed), "receipts": len(receipts)},
            "duplicate_receipts": duplicate_receipts,
            "receipt_coverage": 1.0 if not done else min(1.0, len(receipts) / len(done)),
            "queue_delay": queue_delay,
            "invalid_records": invalid_records,
            "remote_required_actions": [name for name, action in self.config.get("actions", {}).items()
                                        if action.get("requires_remote")],
        }
        atomic_write_json(self.root / "audit-last.json", result)
        return result
