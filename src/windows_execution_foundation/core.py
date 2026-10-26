from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .reservation_pause import action_paused, parse_pause_notice

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FoundationError(RuntimeError):
    pass


# Operator-facing state. Evidence that does not prove this execution is not retried.
RESULT_UNKNOWN = "結果不明"
_SUCCESS_MARKS = frozenset({
    "SUCCESS", "SUCCEEDED", "PUBLISHED_VERIFIED", "VERIFIED", "DONE", "OK", "COMPLETED",
})
_HEARTBEAT_INTERVAL_SECONDS = 30
_CHILD_TIMEOUT_FLOOR_SECONDS = 900
# A shared receipt must name this execution. An empty or omitted value does not count.
_MAJOR_IDENTITY_FIELDS = (
    "execution_id",
    "job_id",
    "idempotency_key",
    "dedup_key",
    "request_id",
    "reservation_key",
)
# The destination itself says this attempt did not apply. Anything else after a spawn is unknown.
_NOT_APPLIED_MARKS = frozenset({
    "FAILED",
    "NOT_APPLIED",
    "NOT_PUBLISHED",
    "ROLLED_BACK",
    "REJECTED",
})


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
        self._claim_hold = None

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
        decided = self._existing_receipt_decision(action, idempotency_key, key_hash)
        if decided is not None:
            return decided
        blocked = self._unknown_reflection_block(key_hash)
        if blocked is not None:
            return blocked
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
            decided = self._existing_receipt_decision(action, idempotency_key, key_hash)
            if decided is not None:
                return decided
            blocked = self._unknown_reflection_block(key_hash)
            if blocked is not None:
                return blocked
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
                if not isinstance(job, dict) or action_paused(job.get("action")):
                    continue
                if self._job_owner_alive(job):
                    continue
                deadline = self._ownership_deadline_seconds(job)
                claimed = job.get("claimed_at")
                if not claimed:
                    age = deadline + 1
                else:
                    age = (now - parse_time(claimed)).total_seconds()
                if age <= deadline:
                    continue
                if job.get("spawn_attempted"):
                    # The command may already have reflected. Do not queue it as unexecuted.
                    action = self._action_of(job)
                    verdict, evidence = self._classify_evidence(action, job)
                    if verdict == "match":
                        self._commit_success(path, job, None, evidence)
                    else:
                        if verdict == "mismatch":
                            reason = "EVIDENCE_MISMATCH"
                        elif verdict == "unbound":
                            reason = "EVIDENCE_IDENTITY_MISSING"
                        else:
                            reason = "REFLECTION_UNCONFIRMED"
                        self._mark_unknown(path, job, reason)
                    recovered += 1
                    continue
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
        self._claim_hold = None
        with exclusive_lock(self.root / ".spool.lock"):
            for source in sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime):
                job = read_json(source, {})
                if action_paused(job.get("action")):
                    continue
                if float(job.get("not_before_epoch", 0)) > now:
                    continue
                decided = self._existing_receipt_decision(
                    str(job.get("action") or ""), str(job.get("idempotency_key") or ""), str(job.get("dedup_key") or ""))
                if decided is not None and decided.get("status") == "NOOP_RECEIPT":
                    source.unlink(missing_ok=True)
                    continue
                if decided is not None:
                    self._claim_hold = self._mark_unknown(source, job, "EVIDENCE_NOT_COMMITTED")
                    return None
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
                job["execution_id"] = str(job.get("execution_id") or uuid.uuid4().hex)
                job["execution_started_at"] = str(job.get("execution_started_at") or job["claimed_at"])
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
        verdict, evidence = self._classify_evidence(action, job or {})
        if verdict == "match":
            return evidence
        return None

    def execution_receipt_path(self, execution_id: str) -> Path:
        return self.receipts / "executions" / f"{execution_id}.json"

    def _existing_receipt_decision(self, action: str, idempotency_key: str, key_hash: str) -> dict[str, Any] | None:
        path = self.receipt_path(key_hash)
        if not path.exists():
            return None
        try:
            receipt = read_json(path)
        except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError):
            return self._unknown_result(key_hash, "EVIDENCE_UNREADABLE")
        if self._receipt_commits_success(receipt, action=action, idempotency_key=idempotency_key, key_hash=key_hash):
            return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
        return self._unknown_result(key_hash, "EVIDENCE_NOT_COMMITTED")

    def _receipt_commits_success(self, receipt: Any, *, action: str, idempotency_key: str, key_hash: str) -> bool:
        # The file name is the dedup key. Older success receipts omit the
        # identity fields; a field that is present must still name this job.
        if not isinstance(receipt, dict) or receipt.get("status") != "SUCCESS":
            return False
        expected = {"idempotency_key": idempotency_key, "dedup_key": key_hash, "action": action}
        for field, value in expected.items():
            recorded = receipt.get(field)
            if recorded not in (None, "") and recorded != value:
                return False
        return True

    def _unknown_reflection_block(self, key_hash: str) -> dict[str, Any] | None:
        path = self.failed / f"{key_hash}.json"
        if not path.exists():
            return None
        try:
            failed = read_json(path, {})
        except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError):
            return self._unknown_result(key_hash, "EVIDENCE_UNREADABLE")
        if not isinstance(failed, dict):
            return self._unknown_result(key_hash, "EVIDENCE_UNREADABLE")
        if failed.get("result_state") == "UNKNOWN" or failed.get("status") == RESULT_UNKNOWN:
            return self._unknown_result(
                key_hash, str(failed.get("last_error") or "REFLECTION_UNCONFIRMED"), failed.get("execution_id"))
        return None

    def _unknown_result(self, key_hash: str, reason: str, execution_id: Any = None) -> dict[str, Any]:
        result = {"status": RESULT_UNKNOWN, "reason": reason, "dedup_key": key_hash, "resend": False}
        if isinstance(execution_id, str) and execution_id:
            result["execution_id"] = execution_id
        return result

    def _has_reflection_destination(self, action: dict[str, Any]) -> bool:
        return bool(action.get("receipt_source") or action.get("receipt_source_dir"))

    def _evidence_candidates(self, action: dict[str, Any], job: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        execution_id = str(job.get("execution_id") or "")
        dedup = str(job.get("dedup_key") or "")
        directory = action.get("receipt_source_dir")
        if directory and execution_id:
            paths.append(Path(directory) / f"{execution_id}.json")
        if directory and dedup:
            paths.append(Path(directory) / f"{dedup}.json")
        source = action.get("receipt_source")
        if source:
            paths.append(Path(source))
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def _evidence_created_after(self, path: Path, content: Any, started_at: Any) -> bool:
        kind, started = classify_timestamp(started_at)
        if kind != "aware" or started is None:
            return False
        earliest = started - timedelta(seconds=2)
        if isinstance(content, dict):
            for field in ("completed_at", "observed_at", "created_at", "finished_at", "started_at"):
                stamp_kind, parsed = classify_timestamp(content.get(field))
                if stamp_kind == "aware" and parsed is not None:
                    return parsed >= earliest
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
        return modified >= earliest

    def _expected_identity(self, job: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "execution_id": job.get("execution_id"),
            "idempotency_key": job.get("idempotency_key"),
            "dedup_key": job.get("dedup_key"),
            "job_id": job.get("job_id"),
            "request_id": job.get("idempotency_key"),
            "reservation_key": job.get("idempotency_key"),
        }
        field = action.get("receipt_request_id_field")
        if isinstance(field, str) and field:
            expected[field] = job.get("idempotency_key")
        return expected

    def _identity_binding(self, content: Any, job: dict[str, Any], action: dict[str, Any]) -> str:
        """match, mismatch, or unbound.

        At least one major identifier must be present and equal to this job.
        A shared receipt that omits every identifier does not prove this execution.
        """
        if not isinstance(content, dict):
            return "mismatch"
        saw_major = False
        for key, want in self._expected_identity(job, action).items():
            if key not in content or content.get(key) in (None, ""):
                continue
            recorded = content.get(key)
            if want in (None, "") or recorded != want:
                return "mismatch"
            if key in _MAJOR_IDENTITY_FIELDS or key == action.get("receipt_request_id_field"):
                saw_major = True
        return "match" if saw_major else "unbound"

    def _evidence_target_matches(self, content: Any, job: dict[str, Any], action: dict[str, Any]) -> bool:
        return self._identity_binding(content, job, action) == "match"

    def _evidence_is_success(self, content: Any) -> bool:
        if not isinstance(content, dict):
            return False
        for key in ("status", "terminal_status", "acquisition_status"):
            value = content.get(key)
            if isinstance(value, str) and value in _SUCCESS_MARKS:
                return True
        return False

    def _load_evidence_file(self, path: Path) -> tuple[str, dict[str, Any] | None]:
        if not path.exists():
            return "absent", None
        try:
            content = read_json(path)
            payload = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content": content if isinstance(content, dict) else {},
            }
        except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError):
            return "unreadable", None
        return "present", payload

    def _classify_evidence(self, action: dict[str, Any], job: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Return match, mismatch, absent, or none when this action has no reflection file."""
        if not self._has_reflection_destination(action):
            return "none", None
        started = job.get("execution_started_at") or job.get("claimed_at")
        saw_file = False
        saw_unbound = False
        for path in self._evidence_candidates(action, job):
            state, payload = self._load_evidence_file(path)
            if state == "absent":
                continue
            saw_file = True
            content = (payload or {}).get("content")
            binding = self._identity_binding(content, job, action)
            created_after = self._evidence_created_after(path, content, started)
            if (
                state == "present"
                and self._evidence_is_success(content)
                and binding == "match"
                and created_after
            ):
                return "match", payload
            if state == "present" and binding == "unbound" and created_after:
                saw_unbound = True
        if not saw_file:
            return "absent", None
        if saw_unbound:
            return "unbound", None
        return "mismatch", None

    def _evidence_explicitly_not_applied(self, action: dict[str, Any], job: dict[str, Any]) -> bool:
        """True only when a bound receipt says this attempt did not apply."""
        started = job.get("execution_started_at") or job.get("claimed_at")
        for path in self._evidence_candidates(action, job):
            state, payload = self._load_evidence_file(path)
            if state != "present":
                continue
            content = (payload or {}).get("content")
            if self._identity_binding(content, job, action) != "match":
                continue
            if not self._evidence_created_after(path, content, started):
                continue
            if not isinstance(content, dict) or self._evidence_is_success(content):
                continue
            for key in ("status", "terminal_status", "acquisition_status"):
                value = content.get(key)
                if isinstance(value, str) and value in _NOT_APPLIED_MARKS:
                    return True
        return False

    def _action_of(self, job: dict[str, Any]) -> dict[str, Any]:
        action = self.config.get("actions", {}).get(str(job.get("action") or ""), {})
        return action if isinstance(action, dict) else {}

    def _action_timeout_seconds(self, job: dict[str, Any]) -> int:
        action = self._action_of(job)
        if "timeout_seconds" in action:
            return int(action.get("timeout_seconds") or 0)
        return _CHILD_TIMEOUT_FLOOR_SECONDS

    def _ownership_deadline_seconds(self, job: dict[str, Any]) -> int:
        """Reclaim no sooner than the child maximum.

        Heartbeat monitoring (often 120s) and a 300s reclaim grace must not
        expire a child that is allowed to run for up to its own timeout.
        """
        return max(self._inflight_stale_limit(job), self._action_timeout_seconds(job))

    def _heartbeat_fresh_limit(self) -> int:
        configured = int(self.config.get("worker_heartbeat_max_age_seconds", _CHILD_TIMEOUT_FLOOR_SECONDS))
        interval = max(1, int(self.config.get("worker_heartbeat_interval_seconds", _HEARTBEAT_INTERVAL_SECONDS)))
        return max(configured, interval * 2)

    def _age_seconds(self, value: Any) -> float | None:
        kind, parsed = classify_timestamp(value)
        if kind != "aware" or parsed is None:
            return None
        return (datetime.now(UTC) - parsed).total_seconds()

    def _pid_running_since(self, pid: Any, since: Any) -> bool:
        """True when pid is a live Python process that is not a reuse after `since`."""
        try:
            from .lane_monitor import _PYTHON_IMAGE, _epoch, process_info
            info = process_info(pid)
        except Exception:
            return True  # Cannot prove the owner is gone.
        if info.get("access") == "denied":
            return True
        if not info.get("alive"):
            return False
        image = os.path.basename(info.get("image") or "")
        if not _PYTHON_IMAGE.fullmatch(image):
            return False
        created = info.get("created")
        since_epoch = _epoch(since)
        if created is not None and since_epoch is not None and created > since_epoch + 2:
            return False
        return True

    def _job_owner_alive(self, job: dict[str, Any]) -> bool:
        """Living owners and their children keep the job. Age alone is not ownership."""
        pid = job.get("worker_pid")
        fresh_limit = self._heartbeat_fresh_limit()
        heartbeat_at = job.get("owner_heartbeat_at")
        heartbeat_age = self._age_seconds(heartbeat_at)
        if heartbeat_age is not None and heartbeat_age <= fresh_limit and self._pid_running_since(pid, heartbeat_at):
            return True
        worker = read_json(self.heartbeats / "worker.json", {})
        if isinstance(worker, dict) and worker.get("pid") == pid:
            observed_age = self._age_seconds(worker.get("observed_at"))
            if observed_age is not None and observed_age <= fresh_limit and self._pid_running_since(pid, worker.get("observed_at")):
                return True
        try:
            from .lane_monitor import _epoch, live_children, process_info
            info = process_info(pid)
        except Exception:
            return True
        if not info.get("alive"):
            since = _epoch(job.get("claimed_at") or job.get("execution_started_at"))
            try:
                if live_children(pid, since):
                    return True
            except Exception:
                return True
            return False
        # An alive pid proves ownership only when its start is not after the claim.
        # Without a creation time (non-Windows) a fresh heartbeat above is the proof.
        return self._pid_running_since(pid, job.get("claimed_at") or job.get("execution_started_at")) and info.get("created") is not None

    def _pulse_owner(self, job_path: Path, execution_id: str) -> None:
        try:
            self.heartbeat("worker", "RUNNING", execution_id=execution_id)
            if not job_path.exists():
                return
            current = read_json(job_path, {})
            if not isinstance(current, dict) or current.get("execution_id") != execution_id:
                return
            current["owner_heartbeat_at"] = iso_now()
            current["worker_pid"] = os.getpid()
            atomic_write_json(job_path, current)
        except Exception:
            return

    def _owner_pulse_loop(self, stop: threading.Event, job_path: Path, execution_id: str) -> None:
        interval = max(1, int(self.config.get("worker_heartbeat_interval_seconds", _HEARTBEAT_INTERVAL_SECONDS)))
        while not stop.wait(interval):
            self._pulse_owner(job_path, execution_id)

    def _persist_job(self, path: Path, job: dict[str, Any]) -> None:
        persisted = {key: value for key, value in job.items() if key != "_resource_release_attempted"}
        atomic_write_json(path, persisted)

    def _mark_spawn(self, path: Path, job: dict[str, Any]) -> None:
        job["spawn_attempted"] = True
        job["spawn_attempted_at"] = iso_now()
        self._persist_job(path, job)

    def _mark_unknown(self, path: Path, job: dict[str, Any], reason: str) -> dict[str, Any]:
        job["result_state"] = "UNKNOWN"
        job["status"] = RESULT_UNKNOWN
        job["last_error"] = reason
        job["last_failed_at"] = iso_now()
        self._persist_job(self.failed / path.name, job)
        execution_id = job.get("execution_id")
        if isinstance(execution_id, str) and execution_id:
            atomic_write_json(self.execution_receipt_path(execution_id), {
                "schema_version": "1.0",
                "status": RESULT_UNKNOWN,
                "result_state": "UNKNOWN",
                "reason": reason,
                "execution_id": execution_id,
                "idempotency_key": job.get("idempotency_key"),
                "dedup_key": job.get("dedup_key"),
                "action": job.get("action"),
                "recorded_at": iso_now(),
            })
        path.unlink(missing_ok=True)
        self.heartbeat("worker", "IDLE", last_dedup_key=job.get("dedup_key"))
        return self._unknown_result(str(job.get("dedup_key") or path.stem), reason, execution_id)

    def _commit_success(self, path: Path, job: dict[str, Any], completed: subprocess.CompletedProcess[str] | None,
                        external: dict[str, Any] | None) -> dict[str, Any]:
        stdout = completed.stdout if completed is not None else ""
        receipt = {
            "schema_version": "1.0",
            "lane_id": self.config.get("lane_id", "A"),
            "status": "SUCCESS",
            "job_id": job["job_id"],
            "execution_id": job.get("execution_id"),
            "execution_started_at": job.get("execution_started_at"),
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
            "exit_code": completed.returncode if completed is not None else None,
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "external_receipt": external,
        }
        if external is not None and completed is None:
            receipt["adopted_from_evidence"] = True
        atomic_write_json(self.receipt_path(job["dedup_key"]), receipt)
        execution_id = job.get("execution_id")
        if isinstance(execution_id, str) and execution_id:
            atomic_write_json(self.execution_receipt_path(execution_id), receipt)
        atomic_write_json(self.done / path.name, {**job, "receipt": receipt})
        path.unlink(missing_ok=True)
        self.heartbeat("worker", "IDLE", last_dedup_key=job["dedup_key"])
        return receipt

    def _release_allowed(self, job: dict[str, Any], action: dict[str, Any], returncode: int,
                         content: Any) -> bool:
        if not self.config.get("shared_resource_dir"):
            return True
        if job.get("action") != "codex-local-job":
            return True
        return (
            returncode == 0
            and isinstance(content, dict)
            and content.get("request_id") == job.get("idempotency_key")
            and content.get("resource_release_safe") is True
        )

    def _reconcile_spawned(self, path: Path, job: dict[str, Any], action: dict[str, Any]) -> dict[str, Any] | None:
        """Before another attempt, adopt proved reflection or refuse an unknown one."""
        if not job.get("spawn_attempted"):
            return None
        verdict, evidence = self._classify_evidence(action, job)
        if verdict == "match":
            return self._commit_success(path, job, None, evidence)
        if action.get("external_async") and verdict == "absent":
            # The command already ran. Poll the destination again; do not start it twice.
            return self._defer_external(path, job, action, safe_to_release=True)
        if verdict == "none" and not job.get("spawn_attempted"):
            return None
        if verdict == "none":
            # No reflection file is configured, but a previous spawn's effect is unknown.
            return self._mark_unknown(path, job, "REFLECTION_UNCONFIRMED")
        if verdict == "mismatch":
            return self._mark_unknown(path, job, "EVIDENCE_MISMATCH")
        return self._mark_unknown(path, job, "EVIDENCE_ABSENT_AFTER_SPAWN")

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
        # A confirmed exit may run again. An unfinished spawn keeps its flag on the inflight file.
        job.pop("spawn_attempted", None)
        job.pop("spawn_attempted_at", None)
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
                    held = self._claim_hold
                    self._claim_hold = None
                    self.heartbeat("worker", "IDLE")
                    if held is not None:
                        return held
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
                    prior = self._reconcile_spawned(path, job, action)
                    if prior is not None:
                        if prior.get("status") != "SUCCESS":
                            safe_to_release = False
                        else:
                            content = ((prior.get("external_receipt") or {}).get("content") or {})
                            safe_to_release = self._release_allowed(job, action, 0, content)
                        return prior
                    # subprocess.run may raise after creating a process. From
                    # this point, absence of a returned result is ambiguous.
                    launch_stage = "SPAWN_OUTCOME_UNKNOWN"
                    safe_to_release = False
                    self._mark_spawn(path, job)
                    stop_pulse = threading.Event()
                    execution_id = str(job.get("execution_id") or "")
                    self._pulse_owner(path, execution_id)
                    pulse = threading.Thread(
                        target=self._owner_pulse_loop,
                        args=(stop_pulse, path, execution_id),
                        name="wef-owner-pulse",
                        daemon=True,
                    )
                    pulse.start()
                    try:
                        completed = subprocess.run(
                            command,
                            cwd=action.get("working_directory"),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=int(action.get("timeout_seconds", _CHILD_TIMEOUT_FLOOR_SECONDS)),
                            check=False,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    finally:
                        stop_pulse.set()
                        pulse.join(timeout=2)
                    launch_stage = "SPAWN_RETURNED"
                    if (job.get("action") == "cityheaven-reservation-schedule"
                            and completed.returncode == 30):
                        safe_to_release = False
                        notice = parse_pause_notice(completed.stdout, "parent")
                        return self._hold_reservation(
                            job, no_child_confirmed=notice is not None and notice["no_child_confirmed"] is True)
                    verdict, evidence = self._classify_evidence(action, job)
                    content = (evidence or {}).get("content") or {}
                    safe_to_release = self._release_allowed(job, action, completed.returncode, content)
                    if verdict == "match":
                        return self._commit_success(path, job, completed, evidence)
                    if completed.returncode != 0:
                        # Retry only when this attempt is proved not to have applied.
                        # A missing or unbound receipt after the child exits is unknown.
                        proved_not_applied = (
                            not self._has_reflection_destination(action)
                            or self._evidence_explicitly_not_applied(action, job)
                        )
                        if not proved_not_applied:
                            safe_to_release = False
                            if verdict == "mismatch":
                                reason = "EVIDENCE_MISMATCH"
                            elif verdict == "unbound":
                                reason = "EVIDENCE_IDENTITY_MISSING"
                            else:
                                reason = "REFLECTION_UNCONFIRMED"
                            return self._mark_unknown(path, job, reason)
                        error = f"EXIT_{completed.returncode}"
                        if not action.get("redact_subprocess_output"):
                            error += f":{completed.stderr[-1000:]}"
                        observed = evidence if verdict == "match" else None
                        if completed.returncode in set(action.get("terminal_failure_exit_codes", [])):
                            return self._fail_final(path, job, error, observed)
                        return self._retry(path, job, error, safe_to_release=safe_to_release)
                    if action.get("external_async") and verdict == "absent":
                        return self._defer_external(path, job, action,
                                                    safe_to_release=safe_to_release)
                    if verdict in {"absent", "mismatch", "unbound"}:
                        safe_to_release = False
                        if verdict == "mismatch":
                            reason = "EVIDENCE_MISMATCH"
                        elif verdict == "unbound":
                            reason = "EVIDENCE_IDENTITY_MISSING"
                        else:
                            reason = "EVIDENCE_ABSENT_AFTER_SPAWN"
                        return self._mark_unknown(path, job, reason)
                    return self._commit_success(path, job, completed, None)
                except subprocess.TimeoutExpired as exc:
                    action = self._action_of(job)
                    verdict, evidence = self._classify_evidence(action, job)
                    if verdict == "match":
                        return self._commit_success(path, job, None, evidence)
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
        maximum = int(self.config.get("worker_heartbeat_max_age_seconds", _CHILD_TIMEOUT_FLOOR_SECONDS))
        stale = True
        heartbeat_age = None
        heartbeat_expired = True
        if worker.get("observed_at"):
            heartbeat_age = (datetime.now(UTC) - parse_time(worker["observed_at"])).total_seconds()
            heartbeat_expired = heartbeat_age > maximum
        # A 120s monitor window must not declare a living owner dead.
        process_alive = bool(worker.get("pid")) and self._pid_running_since(
            worker.get("pid"), worker.get("observed_at"))
        stale = heartbeat_expired and not process_alive

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
        # The waiting count is reported. Pass/fail uses the oldest eligible
        # timestamp against the allowed delay, not the count itself.
        queue_status = "PASS" if not overdue and not unprocessable_pending else "ATTENTION"
        oldest_wait = 0.0 if oldest is None else (now - oldest).total_seconds()
        queue_delay = {
            "status": queue_status,
            "pass_criterion": "最古の待機が許容遅延以内",
            "pending_count_affects_status": False,
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

    def _done_receipt_matches(self, job: dict[str, Any], receipt: Any) -> bool:
        if not isinstance(receipt, dict) or receipt.get("status") != "SUCCESS":
            return False
        dedup = job.get("dedup_key")
        if dedup and receipt.get("dedup_key") != dedup:
            return False
        idempotency_key = job.get("idempotency_key")
        if idempotency_key and receipt.get("idempotency_key") != idempotency_key:
            return False
        action = job.get("action")
        if action and receipt.get("action") not in (None, action):
            return False
        embedded = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
        done_execution = job.get("execution_id") or embedded.get("execution_id")
        receipt_execution = receipt.get("execution_id")
        if done_execution and receipt_execution and done_execution != receipt_execution:
            return False
        return True

    def _completion_evidence(self, done_paths: list[Path], receipt_paths: list[Path]) -> dict[str, Any]:
        by_stem: dict[str, Any] = {}
        execution_ids: list[str] = []
        for path in receipt_paths:
            try:
                receipt = read_json(path)
            except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError):
                receipt = None
            by_stem[path.stem] = receipt
            if isinstance(receipt, dict) and isinstance(receipt.get("execution_id"), str) and receipt.get("execution_id"):
                execution_ids.append(receipt["execution_id"])
        matched = 0
        unmatched_done: list[Any] = []
        mismatched: list[Any] = []
        for path in done_paths:
            try:
                job = read_json(path)
            except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError):
                unmatched_done.append(path.stem)
                continue
            if not isinstance(job, dict):
                unmatched_done.append(path.stem)
                continue
            dedup = str(job.get("dedup_key") or path.stem)
            receipt = by_stem.get(dedup)
            embedded = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
            identity = job.get("execution_id") or embedded.get("execution_id") or dedup
            if self._done_receipt_matches(job, receipt):
                matched += 1
            else:
                unmatched_done.append(identity)
                if receipt is not None:
                    mismatched.append(identity)
        duplicate_execution_ids = len(execution_ids) - len(set(execution_ids))
        return {
            "status": "PASS" if not unmatched_done and duplicate_execution_ids == 0 else "ATTENTION",
            "matched": matched,
            "unmatched_done": unmatched_done,
            "mismatched": mismatched,
            "duplicate_execution_ids": duplicate_execution_ids,
        }

    def audit(self) -> dict[str, Any]:
        pending = list(self.pending.glob("*.json"))
        inflight = list(self.inflight.glob("*.json"))
        done = list(self.done.glob("*.json"))
        failed = list(self.failed.glob("*.json"))
        receipts = list(self.receipts.glob("*.json"))
        keys = [p.stem for p in receipts]
        completion = self._completion_evidence(done, receipts)
        duplicate_receipts = (len(keys) - len(set(keys))) + int(completion["duplicate_execution_ids"])
        queue_delay, invalid_records = self._queue_delay(pending)
        structural_ok = not inflight and not failed and duplicate_receipts == 0
        status = (
            "PASS"
            if structural_ok and queue_delay["status"] == "PASS" and completion["status"] == "PASS"
            and not invalid_records
            else "ATTENTION"
        )
        done_count = len(done)
        result = {
            "status": status,
            "observed_at": iso_now(),
            "counts": {"pending": len(pending), "inflight": len(inflight), "done": done_count,
                       "failed": len(failed), "receipts": len(receipts)},
            "duplicate_receipts": duplicate_receipts,
            "receipt_coverage": 1.0 if not done_count else completion["matched"] / done_count,
            "completion_evidence": completion,
            "queue_delay": queue_delay,
            "invalid_records": invalid_records,
            "remote_required_actions": [name for name, action in self.config.get("actions", {}).items()
                                        if action.get("requires_remote")],
        }
        atomic_write_json(self.root / "audit-last.json", result)
        return result
