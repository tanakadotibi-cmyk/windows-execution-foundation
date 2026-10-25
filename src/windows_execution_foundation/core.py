from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

# Time roles stay distinct. They are not one shared timeout:
#   heartbeat max age (default 120s): watchdog alert only
#   ownership lease (default 300s): refreshed while the owner lives; a job is
#     reclaimed only after this deadline AND only when that owner is gone
#   child timeout (default 900s): enforced by the owning worker on its own child
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 120
DEFAULT_OWNERSHIP_LEASE_SECONDS = 300
DEFAULT_CHILD_TIMEOUT_SECONDS = 900
DEFAULT_PENDING_ALLOWED_DELAY_SECONDS = 900
RECEIPT_FUTURE_SKEW_SECONDS = 120
FILE_MTIME_SKEW_SECONDS = 2
SUCCESS_EXTERNAL_STATUSES = frozenset({"SUCCESS", "PUBLISHED_VERIFIED"})
NOT_APPLIED_EXTERNAL_STATUSES = frozenset({"NOT_APPLIED", "NOT_PUBLISHED"})


class FoundationError(RuntimeError):
    pass


def iso_now() -> str:
    return iso_at(datetime.now(UTC))


def iso_at(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def as_time(value: Any) -> datetime | None:
    kind, parsed = classify_timestamp(value)
    if kind != "aware":
        return None
    return parsed


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


def process_identity(pid: Any) -> str | None:
    """Start token for a pid, or None when that process is not the same live owner."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start(number)
    return _linux_process_start(number)


def _linux_process_start(pid: int) -> str | None:
    path = Path(f"/proc/{pid}/stat")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = text.rfind(")")
    if marker < 0:
        return None
    fields = text[marker + 2 :].split()
    # field 3 (state) then starttime at field 22 → index 19 after comm
    if len(fields) <= 19 or fields[0] == "Z":
        return None
    return fields[19]


def _windows_process_start(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return str(value)
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
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
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} at={iso_now()}".encode())
        handle.flush()
        yield
    finally:
        try:
            if os.name == "nt":
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
        self.unknown = self.root / "spool" / "unknown"
        self.receipts = self.root / "receipts"
        self.dedup_receipts = self.receipts / "dedup"
        self.execution_receipts = self.receipts / "executions"
        self.heartbeats = self.root / "heartbeats"
        self._state_lock = threading.Lock()
        for path in (
            self.pending,
            self.inflight,
            self.done,
            self.failed,
            self.unknown,
            self.dedup_receipts,
            self.execution_receipts,
            self.heartbeats,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_path(cls, path: Path) -> "ExecutionFoundation":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def receipt_path(self, key_hash: str) -> Path:
        return self.dedup_receipts / f"{key_hash}.json"

    def execution_receipt_path(self, execution_id: str) -> Path:
        return self.execution_receipts / f"{execution_id}.json"

    def external_receipt_path(self, action: dict[str, Any], execution_id: str) -> Path | None:
        source = action.get("receipt_source")
        if not source or not execution_id:
            return None
        base = Path(source)
        # A shared file is not a reusable success flag. Each execution has its own file.
        if base.suffix.lower() == ".json":
            directory = base.parent / f"{base.stem}.executions"
        else:
            directory = base
        return directory / f"{execution_id}.json"

    def _heartbeat_max_age_seconds(self) -> int:
        return int(self.config.get("worker_heartbeat_max_age_seconds", DEFAULT_HEARTBEAT_MAX_AGE_SECONDS))

    def _lease_seconds(self) -> int:
        configured = int(self.config.get("inflight_stale_seconds", DEFAULT_OWNERSHIP_LEASE_SECONDS))
        # A lease shorter than the heartbeat window would expire a healthy owner
        # before the watchdog could tell refresh from death. Keep the lease at
        # least as long as the heartbeat alert window.
        return max(configured, self._heartbeat_max_age_seconds())

    def _heartbeat_interval_seconds(self) -> int:
        window = self._heartbeat_max_age_seconds()
        interval = max(1, window // 4)
        if window > 1 and interval >= window:
            return window - 1
        return interval

    def timing_policy(self) -> dict[str, Any]:
        return {
            "heartbeat_interval_seconds": self._heartbeat_interval_seconds(),
            "heartbeat_max_age_seconds": self._heartbeat_max_age_seconds(),
            "ownership_lease_seconds": self._lease_seconds(),
            "default_child_timeout_seconds": DEFAULT_CHILD_TIMEOUT_SECONDS,
            "pending_allowed_delay_seconds": int(
                self.config.get("pending_allowed_delay_seconds", DEFAULT_PENDING_ALLOWED_DELAY_SECONDS)
            ),
            "receipt_future_skew_seconds": RECEIPT_FUTURE_SKEW_SECONDS,
            "reclaim_requires": ["owner_not_alive", "ownership_deadline_passed"],
            "heartbeat_role": "watchdog_liveness_alert",
            "lease_role": "ownership_deadline_refreshed_while_owner_lives",
            "child_timeout_role": "enforced_by_owner_on_its_own_child",
            "action_timeout_seconds": {
                name: int(action.get("timeout_seconds", DEFAULT_CHILD_TIMEOUT_SECONDS))
                for name, action in self.config.get("actions", {}).items()
                if isinstance(action, dict)
            },
        }

    def enqueue(
        self,
        *,
        action: str,
        idempotency_key: str,
        args: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action not in self.config.get("actions", {}):
            raise FoundationError(f"ACTION_NOT_ALLOWED:{action}")
        key_hash = stable_key(idempotency_key)
        receipt = read_json(self.receipt_path(key_hash))
        if receipt:
            return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
        unknown = read_json(self.unknown / f"{key_hash}.json")
        if unknown:
            return {"status": "NOOP_UNKNOWN", "dedup_key": key_hash, "resend": False}
        for directory in (self.pending, self.inflight):
            existing = read_json(directory / f"{key_hash}.json")
            if existing:
                return {"status": "NOOP_QUEUED", "dedup_key": key_hash, "job_id": existing["job_id"]}
        execution_id = uuid.uuid4().hex
        job = {
            "schema_version": "1.0",
            "job_id": execution_id,
            "execution_id": execution_id,
            "action": action,
            "args": list(args or []),
            "idempotency_key": idempotency_key,
            "dedup_key": key_hash,
            "attempt": 0,
            "created_at": iso_now(),
            "not_before_epoch": 0,
            "metadata": metadata or {},
        }
        with exclusive_lock(self.root / ".spool.lock"):
            receipt = read_json(self.receipt_path(key_hash))
            if receipt:
                return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
            if (self.unknown / f"{key_hash}.json").exists():
                return {"status": "NOOP_UNKNOWN", "dedup_key": key_hash, "resend": False}
            if (self.pending / f"{key_hash}.json").exists() or (self.inflight / f"{key_hash}.json").exists():
                return {"status": "NOOP_QUEUED", "dedup_key": key_hash}
            atomic_write_json(self.pending / f"{key_hash}.json", job)
        return {
            "status": "ENQUEUED",
            "dedup_key": key_hash,
            "job_id": job["job_id"],
            "execution_id": execution_id,
        }

    def heartbeat(self, name: str, status: str, **extra: Any) -> dict[str, Any]:
        value = {
            "name": name,
            "status": status,
            "observed_at": iso_now(),
            "pid": os.getpid(),
            "proc_start": process_identity(os.getpid()),
            **extra,
        }
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

    def _owner_alive(self, job: dict[str, Any]) -> bool:
        expected = job.get("owner_proc_start")
        if not expected:
            return False
        current = process_identity(job.get("worker_pid"))
        return current is not None and current == str(expected)

    def _ownership_deadline_passed(self, job: dict[str, Any], now: datetime) -> bool:
        lease = as_time(job.get("lease_until"))
        if lease is not None:
            return now > lease
        heartbeat = as_time(job.get("heartbeat_at")) or as_time(job.get("claimed_at"))
        if heartbeat is None:
            return True
        return (now - heartbeat).total_seconds() > self._lease_seconds()

    def _take_ownership(self, job: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        job["claimed_at"] = iso_at(now)
        job["started_at"] = job.get("started_at") or job["claimed_at"]
        job["worker_pid"] = os.getpid()
        job["owner_proc_start"] = process_identity(os.getpid())
        job["owner_token"] = uuid.uuid4().hex
        job["heartbeat_at"] = job["claimed_at"]
        job["lease_until"] = iso_at(now + timedelta(seconds=self._lease_seconds()))

    def _refresh_ownership(self, path: Path, job: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        job["worker_pid"] = os.getpid()
        job["owner_proc_start"] = process_identity(os.getpid())
        job["heartbeat_at"] = iso_at(now)
        job["lease_until"] = iso_at(now + timedelta(seconds=self._lease_seconds()))
        if not job.get("owner_token"):
            job["owner_token"] = uuid.uuid4().hex
        atomic_write_json(path, job)

    def _release_ownership(self, job: dict[str, Any], *, clear_external: bool) -> None:
        for key in (
            "claimed_at",
            "worker_pid",
            "owner_proc_start",
            "owner_token",
            "child_pid",
            "lease_until",
            "heartbeat_at",
            "phase",
        ):
            job.pop(key, None)
        if clear_external:
            job.pop("external_started_at", None)

    def _mark_external_started(self, path: Path, job: dict[str, Any]) -> None:
        with self._state_lock:
            if not job.get("external_started_at"):
                job["external_started_at"] = iso_now()
            job["phase"] = "EXTERNAL_EFFECT_STARTED"
            self._refresh_ownership(path, job)

    def recover_stale(self) -> int:
        return self.recover_owned_deadlines()["requeued"]

    def recover_owned_deadlines(self) -> dict[str, Any]:
        try:
            with exclusive_lock(self.root / ".spool.lock"):
                return self._recover_locked()
        except FoundationError as exc:
            if str(exc) == "SINGLE_WORKER_LOCKED":
                empty = {"requeued": 0, "adopted": 0, "quarantined": 0}
                return {
                    "requeued": 0,
                    "adopted_results": [],
                    "quarantined_results": [],
                    "counts": empty,
                    "skipped": "SPOOL_LOCKED",
                }
            raise

    def _recover_locked(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        adopted: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        requeued = 0
        for path in list(self.inflight.glob("*.json")):
            try:
                job = read_json(path, {})
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(job, dict):
                continue
            # A live owner keeps the job, even when its lease timestamp has lapsed.
            # The owner enforces its own child timeout.
            if self._owner_alive(job):
                continue
            if not self._ownership_deadline_passed(job, now):
                continue
            if job.get("external_started_at"):
                action = self.config.get("actions", {}).get(job.get("action"), {})
                if isinstance(action, dict) and action.get("receipt_source"):
                    kind, external, reason = self._classify_external(action, job)
                else:
                    kind, external, reason = "MISSING", None, "EFFECT_UNKNOWN"
                if kind == "MATCH":
                    adopted.append(self._commit_success(path, job, external, None))
                    continue
                if kind == "NOT_APPLIED":
                    self._release_ownership(job, clear_external=True)
                    job["recovered_at"] = iso_now()
                    atomic_write_json(self.pending / path.name, job)
                    path.unlink(missing_ok=True)
                    requeued += 1
                    continue
                quarantined.append(self._quarantine_unknown(path, job, reason))
                continue
            self._release_ownership(job, clear_external=True)
            job["recovered_at"] = iso_now()
            atomic_write_json(self.pending / path.name, job)
            path.unlink(missing_ok=True)
            requeued += 1
        counts = {"requeued": requeued, "adopted": len(adopted), "quarantined": len(quarantined)}
        return {
            "requeued": requeued,
            "adopted_results": adopted,
            "quarantined_results": quarantined,
            "counts": counts,
        }

    def _claim(self) -> tuple[Path, dict[str, Any]] | None:
        now = time.time()
        with exclusive_lock(self.root / ".spool.lock"):
            for source in sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime):
                job = read_json(source, {})
                if not isinstance(job, dict):
                    continue
                if float(job.get("not_before_epoch", 0)) > now:
                    continue
                if self.receipt_path(job["dedup_key"]).exists() or (self.unknown / source.name).exists():
                    source.unlink(missing_ok=True)
                    continue
                destination = self.inflight / source.name
                self._take_ownership(job)
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
        return command, action

    def _child_env(self, job: dict[str, Any], action: dict[str, Any]) -> dict[str, str]:
        env = os.environ.copy()
        receipt_path = self.external_receipt_path(action, str(job.get("execution_id") or ""))
        env["WEF_EXECUTION_ID"] = str(job.get("execution_id") or "")
        env["WEF_JOB_ID"] = str(job.get("job_id") or "")
        env["WEF_ACTION"] = str(job.get("action") or "")
        env["WEF_STARTED_AT"] = str(job.get("external_started_at") or job.get("started_at") or "")
        env["WEF_RECEIPT_PATH"] = str(receipt_path) if receipt_path else ""
        if job.get("idempotency_key") is not None:
            env["WEF_IDEMPOTENCY_KEY"] = str(job["idempotency_key"])
        return env

    def _classify_external(
        self, action: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None, str]:
        path = self.external_receipt_path(action, str(job.get("execution_id") or ""))
        if path is None or not path.exists():
            return "MISSING", None, "EXTERNAL_RECEIPT_MISSING"
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError):
            return "MISMATCH", None, "EXTERNAL_RECEIPT_MISMATCH:UNREADABLE"
        if not isinstance(value, dict):
            return "MISMATCH", None, "EXTERNAL_RECEIPT_MISMATCH:NOT_OBJECT"
        reasons: list[str] = []
        if value.get("execution_id") != job.get("execution_id"):
            reasons.append("EXECUTION_ID")
        if value.get("target") != job.get("action"):
            reasons.append("TARGET")
        if "idempotency_key" in value and value.get("idempotency_key") != job.get("idempotency_key"):
            reasons.append("IDEMPOTENCY_KEY")
        started_kind, started = classify_timestamp(job.get("external_started_at"))
        generated_kind, generated = classify_timestamp(value.get("generated_at"))
        if started_kind == "naive" or generated_kind == "naive":
            reasons.append("TIME_NAIVE")
        elif started_kind != "aware" or generated_kind != "aware" or started is None or generated is None:
            reasons.append("GENERATED_AT")
        else:
            if generated < started:
                reasons.append("GENERATED_BEFORE_START")
            if generated > datetime.now(UTC) + timedelta(seconds=RECEIPT_FUTURE_SKEW_SECONDS):
                reasons.append("GENERATED_IN_FUTURE")
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            reasons.append("FILE_MTIME")
        else:
            if started_kind == "aware" and started is not None and mtime + timedelta(seconds=FILE_MTIME_SKEW_SECONDS) < started:
                reasons.append("FILE_MTIME_BEFORE_START")
        wrapped = {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": value,
            "execution_id": value.get("execution_id"),
            "target": value.get("target"),
            "generated_at": value.get("generated_at"),
        }
        if reasons:
            return "MISMATCH", wrapped, "EXTERNAL_RECEIPT_MISMATCH:" + ",".join(reasons)
        status = value.get("status")
        if status in SUCCESS_EXTERNAL_STATUSES:
            return "MATCH", wrapped, "MATCH"
        if status in NOT_APPLIED_EXTERNAL_STATUSES:
            return "NOT_APPLIED", wrapped, f"NOT_APPLIED:{status}"
        return "MISMATCH", wrapped, "EXTERNAL_RECEIPT_MISMATCH:STATUS"

    def _retry(self, path: Path, job: dict[str, Any], error: str) -> dict[str, Any]:
        attempt = int(job.get("attempt", 0)) + 1
        job["attempt"] = attempt
        job["last_error"] = error
        job["last_failed_at"] = iso_now()
        self._release_ownership(job, clear_external=True)
        maximum = int(self.config.get("max_attempts", 5))
        if attempt >= maximum:
            atomic_write_json(self.failed / path.name, job)
            path.unlink(missing_ok=True)
            return {
                "status": "FAILED_FINAL",
                "attempt": attempt,
                "error": error,
                "dedup_key": job["dedup_key"],
                "execution_id": job.get("execution_id"),
            }
        delay = int(self.config.get("retry_base_seconds", 15)) * (2 ** (attempt - 1))
        job["not_before_epoch"] = time.time() + delay
        atomic_write_json(self.pending / path.name, job)
        path.unlink(missing_ok=True)
        return {
            "status": "RETRY_SPOOLED",
            "attempt": attempt,
            "retry_in_seconds": delay,
            "error": error,
            "dedup_key": job["dedup_key"],
            "execution_id": job.get("execution_id"),
        }

    def _quarantine_unknown(self, path: Path, job: dict[str, Any], reason: str) -> dict[str, Any]:
        job["status"] = "RESULT_UNKNOWN"
        job["unknown_reason"] = reason
        job["resend"] = False
        job["unknown_at"] = iso_now()
        self._release_ownership(job, clear_external=False)
        atomic_write_json(self.unknown / path.name, job)
        if path.exists() and path.resolve() != (self.unknown / path.name).resolve():
            path.unlink(missing_ok=True)
        return {
            "status": "RESULT_UNKNOWN",
            "reason": reason,
            "resend": False,
            "dedup_key": job.get("dedup_key"),
            "execution_id": job.get("execution_id"),
            "attempt": job.get("attempt", 0),
        }

    def _commit_success(
        self,
        path: Path,
        job: dict[str, Any],
        external: dict[str, Any] | None,
        completed: subprocess.CompletedProcess[str] | None,
    ) -> dict[str, Any]:
        stdout = ""
        exit_code = None
        if completed is not None:
            stdout = completed.stdout or ""
            exit_code = completed.returncode
        receipt = {
            "schema_version": "1.0",
            "status": "SUCCESS",
            "job_id": job["job_id"],
            "execution_id": job["execution_id"],
            "action": job["action"],
            "idempotency_key": job["idempotency_key"],
            "dedup_key": job["dedup_key"],
            "completed_at": iso_now(),
            "attempt": int(job.get("attempt", 0)) + 1,
            "exit_code": exit_code,
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "external_receipt": external,
        }
        atomic_write_json(self.execution_receipt_path(job["execution_id"]), receipt)
        atomic_write_json(self.receipt_path(job["dedup_key"]), receipt)
        self._release_ownership(job, clear_external=False)
        atomic_write_json(self.done / path.name, {**job, "receipt": receipt})
        if path.exists():
            path.unlink(missing_ok=True)
        return receipt

    def _settle(
        self,
        path: Path,
        job: dict[str, Any],
        action: dict[str, Any],
        completed: subprocess.CompletedProcess[str] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if not action.get("receipt_source"):
            if completed is not None and completed.returncode == 0:
                return self._commit_success(path, job, None, completed)
            if completed is not None:
                return self._retry(path, job, error or f"EXIT_{completed.returncode}")
            return self._quarantine_unknown(path, job, error or "EFFECT_UNKNOWN")
        kind, external, reason = self._classify_external(action, job)
        if kind == "MATCH":
            return self._commit_success(path, job, external, completed)
        if kind == "NOT_APPLIED":
            return self._retry(path, job, error or reason)
        if kind == "MISSING" and error and error.startswith("TIMEOUT"):
            reason = "TIMEOUT_EFFECT_UNKNOWN"
        return self._quarantine_unknown(path, job, reason)

    def _run_command(
        self,
        path: Path,
        job: dict[str, Any],
        command: list[str],
        action: dict[str, Any],
    ) -> subprocess.CompletedProcess[str]:
        timeout = int(action.get("timeout_seconds", DEFAULT_CHILD_TIMEOUT_SECONDS))
        interval = self._heartbeat_interval_seconds()
        stop = threading.Event()

        def refresh() -> None:
            while not stop.wait(interval):
                try:
                    with self._state_lock:
                        self._refresh_ownership(path, job)
                        self.heartbeat(
                            "worker",
                            "RUNNING",
                            execution_id=job.get("execution_id"),
                            owner_token=job.get("owner_token"),
                        )
                except Exception:
                    continue

        thread = threading.Thread(target=refresh, name="wef-ownership-refresh", daemon=True)
        thread.start()
        try:
            return subprocess.run(
                command,
                cwd=action.get("working_directory"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=self._child_env(job, action),
            )
        finally:
            stop.set()
            thread.join(timeout=5)

    def _execute_claimed(self, path: Path, job: dict[str, Any]) -> dict[str, Any]:
        command, action = self._command(job)
        if action.get("requires_remote"):
            raise FoundationError("REMOTE_DEPENDENCY_FORBIDDEN")
        if action.get("requires_network") and not self._network_ready():
            return self._retry(path, job, "NETWORK_UNAVAILABLE")
        if os.environ.get("WEF_FAULT_WORKER_CRASH") == "1":
            raise FoundationError("INJECTED_WORKER_CRASH")
        self._mark_external_started(path, job)
        try:
            completed = self._run_command(path, job, command, action)
        except subprocess.TimeoutExpired as exc:
            return self._settle(path, job, action, None, error=f"TIMEOUT:{exc.timeout}")
        if os.environ.get("WEF_FAULT_DROP_BEFORE_LOCAL_RECEIPT") == "1":
            return {
                "status": "DROPPED_BEFORE_LOCAL_RECEIPT",
                "resend": False,
                "dedup_key": job["dedup_key"],
                "execution_id": job.get("execution_id"),
            }
        stderr = (completed.stderr or "")[-1000:]
        return self._settle(
            path,
            job,
            action,
            completed,
            error=f"EXIT_{completed.returncode}:{stderr}",
        )

    def work_once(self) -> dict[str, Any]:
        try:
            with exclusive_lock(self.root / ".worker.lock"):
                self.heartbeat("worker", "RUNNING")
                recovery = self.recover_owned_deadlines()
                if recovery["quarantined_results"]:
                    self.heartbeat("worker", "IDLE")
                    return {**recovery["quarantined_results"][0], "recovery": recovery["counts"]}
                if recovery["adopted_results"]:
                    result = recovery["adopted_results"][0]
                    self.heartbeat("worker", "IDLE", last_dedup_key=result.get("dedup_key"))
                    return result
                claimed = self._claim()
                if claimed is None:
                    self.heartbeat("worker", "IDLE")
                    return {"status": "IDLE"}
                path, job = claimed
                try:
                    result = self._execute_claimed(path, job)
                except Exception as exc:
                    if job.get("external_started_at"):
                        try:
                            action = self.config["actions"][job["action"]]
                            result = self._settle(
                                path,
                                job,
                                action,
                                None,
                                error=f"{type(exc).__name__}:{exc}",
                            )
                        except Exception as inner:
                            result = self._quarantine_unknown(
                                path, job, f"LOCAL_RECEIPT_FAILED:{type(inner).__name__}"
                            )
                    else:
                        result = self._retry(path, job, f"{type(exc).__name__}:{exc}")
                if result.get("status") == "SUCCESS":
                    self.heartbeat("worker", "IDLE", last_dedup_key=job.get("dedup_key"))
                else:
                    self.heartbeat("worker", "IDLE")
                return result
        except FoundationError as exc:
            if str(exc) == "SINGLE_WORKER_LOCKED":
                return {"status": "NOOP_SINGLE_WORKER"}
            raise

    def _heartbeat_owner_alive(self, worker: dict[str, Any]) -> bool:
        if not worker.get("pid") or not worker.get("proc_start"):
            return False
        return process_identity(worker.get("pid")) == str(worker.get("proc_start"))

    def watchdog(self, *, restart: bool = True) -> dict[str, Any]:
        recovery = self.recover_owned_deadlines()
        try:
            worker = read_json(self.heartbeats / "worker.json", {})
        except (json.JSONDecodeError, OSError):
            worker = {}
        if not isinstance(worker, dict):
            worker = {}
        maximum = self._heartbeat_max_age_seconds()
        age = None
        observed = as_time(worker.get("observed_at"))
        if observed is None:
            heartbeat_stale = True
        else:
            age = (datetime.now(UTC) - observed).total_seconds()
            heartbeat_stale = age > maximum
        owner_alive = self._heartbeat_owner_alive(worker)
        restart_result = "NOT_NEEDED"
        if not heartbeat_stale:
            status = "HEALTHY"
        elif owner_alive:
            # The process is still the owner. Do not start another copy over it.
            status = "WORKER_UNRESPONSIVE"
            restart_result = "SKIPPED_OWNER_ALIVE"
        else:
            status = "WORKER_STALE"
            if restart and os.name == "nt" and self.config.get("worker_task_name"):
                result = subprocess.run(
                    ["schtasks.exe", "/run", "/tn", self.config["worker_task_name"]],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                restart_result = "TRIGGERED" if result.returncode == 0 else f"FAILED:{result.returncode}"
        value = self.heartbeat(
            "watchdog",
            status,
            recovered_inflight=recovery["requeued"],
            adopted_success=recovery["counts"]["adopted"],
            quarantined_unknown=recovery["counts"]["quarantined"],
            restart_result=restart_result,
            worker_heartbeat_age_seconds=age,
            timing_policy=self.timing_policy(),
        )
        value["worker_stale"] = status == "WORKER_STALE"
        value["heartbeat_stale"] = heartbeat_stale
        value["owner_alive"] = owner_alive
        value["restart_result"] = restart_result
        return value

    def _eligible_since(self, job: dict[str, Any], not_before: float) -> datetime | None:
        kind, created = classify_timestamp(job.get("created_at"))
        if kind != "aware" or created is None:
            return None
        if not_before > 0:
            ready = datetime.fromtimestamp(not_before, UTC)
            return ready if ready > created else created
        return created

    def _receipt_matches_done(self, job: dict[str, Any], receipt: Any) -> bool:
        if not isinstance(receipt, dict) or not isinstance(job, dict):
            return False
        if receipt.get("status") != "SUCCESS":
            return False
        if receipt.get("execution_id") != job.get("execution_id"):
            return False
        if receipt.get("job_id") != job.get("job_id"):
            return False
        if receipt.get("dedup_key") != job.get("dedup_key"):
            return False
        if receipt.get("action") != job.get("action"):
            return False
        action = self.config.get("actions", {}).get(job.get("action"), {})
        if not isinstance(action, dict) or not action.get("receipt_source"):
            return True
        external = receipt.get("external_receipt")
        if not isinstance(external, dict):
            return False
        content = external.get("content")
        if not isinstance(content, dict):
            return False
        if content.get("execution_id") != job.get("execution_id"):
            return False
        if content.get("target") != job.get("action"):
            return False
        if content.get("status") not in SUCCESS_EXTERNAL_STATUSES:
            return False
        started_kind, started = classify_timestamp(job.get("external_started_at"))
        if started_kind != "aware":
            started_kind, started = classify_timestamp(job.get("started_at"))
        generated_kind, generated = classify_timestamp(content.get("generated_at"))
        if started_kind != "aware" or generated_kind != "aware" or started is None or generated is None:
            return False
        if generated < started:
            return False
        return True

    def audit(self) -> dict[str, Any]:
        pending_paths = list(self.pending.glob("*.json"))
        inflight = list(self.inflight.glob("*.json"))
        done_paths = list(self.done.glob("*.json"))
        failed = list(self.failed.glob("*.json"))
        unknown = list(self.unknown.glob("*.json"))
        execution_paths = list(self.execution_receipts.glob("*.json"))

        execution_ids: list[str] = []
        receipts_by_execution: dict[str, dict[str, Any]] = {}
        # Unreadable execution receipts cannot be matched. They stay in the
        # orphan scan so a corrupt file with no done record cannot pass.
        unreadable_execution_ids: list[str] = []
        for path in execution_paths:
            try:
                receipt = read_json(path, {})
            except (json.JSONDecodeError, OSError):
                execution_ids.append(path.stem)
                unreadable_execution_ids.append(path.stem)
                continue
            if not isinstance(receipt, dict):
                execution_ids.append(path.stem)
                unreadable_execution_ids.append(path.stem)
                continue
            execution_ids.append(str(receipt.get("execution_id") or path.stem))
            receipts_by_execution[path.stem] = receipt
        duplicate_receipts = sum(count - 1 for count in Counter(execution_ids).values() if count > 1)

        now = datetime.now(UTC)
        allowed = int(self.config.get("pending_allowed_delay_seconds", DEFAULT_PENDING_ALLOWED_DELAY_SECONDS))
        oldest: datetime | None = None
        overdue: list[Any] = []
        unprocessable_pending: list[str] = []
        invalid_records: list[dict[str, Any]] = []
        eligible = 0
        ineligible = 0
        for path in pending_paths:
            try:
                job = read_json(path, {})
            except (json.JSONDecodeError, OSError):
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
            record_id = job.get("execution_id") or path.stem
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
            except (TypeError, ValueError):
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

        mismatches: list[dict[str, Any]] = []
        matched = 0
        seen_execution: set[Any] = set()
        for path in done_paths:
            try:
                record = read_json(path, {})
            except (json.JSONDecodeError, OSError):
                mismatches.append({"execution_id": None, "dedup_key": path.stem, "reason": "UNREADABLE"})
                continue
            job = record if isinstance(record, dict) else {}
            execution_id = job.get("execution_id") or job.get("job_id")
            seen_execution.add(execution_id)
            for field in ("created_at", "started_at", "external_started_at"):
                invalid = invalid_timestamp_record("done", execution_id, field, job.get(field))
                if invalid is not None:
                    invalid_records.append(invalid)
            receipt = receipts_by_execution.get(str(execution_id))
            if isinstance(receipt, dict):
                invalid = invalid_timestamp_record("execution_receipt", execution_id, "completed_at", receipt.get("completed_at"))
                if invalid is not None:
                    invalid_records.append(invalid)
                external = receipt.get("external_receipt")
                content = external.get("content") if isinstance(external, dict) else None
                if isinstance(content, dict):
                    invalid = invalid_timestamp_record(
                        "execution_receipt", execution_id, "generated_at", content.get("generated_at")
                    )
                    if invalid is not None:
                        invalid_records.append(invalid)
            dedup_receipt: Any = {}
            if job.get("dedup_key"):
                try:
                    dedup_receipt = read_json(self.receipt_path(job["dedup_key"]), {})
                except (json.JSONDecodeError, OSError):
                    dedup_receipt = {}
            try:
                if self._receipt_matches_done(job, receipt) and self._receipt_matches_done(job, dedup_receipt):
                    matched += 1
                else:
                    mismatches.append({"execution_id": execution_id, "dedup_key": job.get("dedup_key")})
            except (TypeError, ValueError):
                mismatches.append({"execution_id": execution_id, "dedup_key": job.get("dedup_key"), "reason": "TIME_COMPARE_FAILED"})
                invalid_records.append(
                    {
                        "category": "不正な記録",
                        "source": "done",
                        "id": execution_id,
                        "field": "generated_at",
                        "reason": "時刻の比較に失敗",
                        "value": None,
                    }
                )
        orphan_candidates = list(receipts_by_execution) + [
            execution_id for execution_id in unreadable_execution_ids if execution_id not in receipts_by_execution
        ]
        orphans = [execution_id for execution_id in orphan_candidates if execution_id not in seen_execution]
        identifier_status = "PASS" if not mismatches and not orphans else "ATTENTION"
        coverage = 1.0 if not done_paths else matched / len(done_paths)
        structural_ok = not inflight and not failed and not unknown and duplicate_receipts == 0
        status = (
            "PASS"
            if structural_ok and queue_status == "PASS" and identifier_status == "PASS" and not invalid_records
            else "ATTENTION"
        )
        result = {
            "status": status,
            "observed_at": iso_now(),
            "counts": {
                "pending": len(pending_paths),
                "inflight": len(inflight),
                "done": len(done_paths),
                "failed": len(failed),
                "unknown": len(unknown),
                "receipts": len(execution_paths),
            },
            "duplicate_receipts": duplicate_receipts,
            "receipt_coverage": coverage,
            "queue_delay": {
                "status": queue_status,
                "pending_count": len(pending_paths),
                "eligible_pending_count": eligible,
                "ineligible_pending_count": ineligible,
                "oldest_waiting_at": iso_at(oldest) if oldest else None,
                "oldest_wait_seconds": oldest_wait,
                "allowed_delay_seconds": allowed,
                "overdue_count": len(overdue),
                "unprocessable_pending": unprocessable_pending,
            },
            "identifier_reconciliation": {
                "status": identifier_status,
                "done": len(done_paths),
                "matched": matched,
                "mismatches": mismatches,
                "orphan_execution_ids": orphans,
            },
            "invalid_records": invalid_records,
            "timing_policy": self.timing_policy(),
            "remote_required_actions": [
                name
                for name, action in self.config.get("actions", {}).items()
                if isinstance(action, dict) and action.get("requires_remote")
            ],
        }
        atomic_write_json(self.root / "audit-last.json", result)
        return result
