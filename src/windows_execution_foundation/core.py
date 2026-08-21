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


class FoundationError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        self.receipts = self.root / "receipts"
        self.heartbeats = self.root / "heartbeats"
        for path in (self.pending, self.inflight, self.done, self.failed, self.receipts, self.heartbeats):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_path(cls, path: Path) -> "ExecutionFoundation":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def receipt_path(self, key_hash: str) -> Path:
        return self.receipts / f"{key_hash}.json"

    def enqueue(self, *, action: str, idempotency_key: str, args: list[str] | None = None,
                metadata: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "not_before_epoch": 0,
            "metadata": metadata or {},
        }
        with exclusive_lock(self.root / ".spool.lock"):
            receipt = read_json(self.receipt_path(key_hash))
            if receipt:
                return {"status": "NOOP_RECEIPT", "dedup_key": key_hash, "receipt": receipt}
            if (self.pending / f"{key_hash}.json").exists() or (self.inflight / f"{key_hash}.json").exists():
                return {"status": "NOOP_QUEUED", "dedup_key": key_hash}
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

    def recover_stale(self) -> int:
        maximum = int(self.config.get("inflight_stale_seconds", 300))
        now = datetime.now(UTC)
        recovered = 0
        with exclusive_lock(self.root / ".spool.lock"):
            for path in self.inflight.glob("*.json"):
                job = read_json(path, {})
                claimed = job.get("claimed_at")
                if not claimed:
                    age = maximum + 1
                else:
                    age = (now - parse_time(claimed)).total_seconds()
                if age > maximum:
                    job["recovered_at"] = iso_now()
                    job.pop("claimed_at", None)
                    atomic_write_json(self.pending / path.name, job)
                    path.unlink(missing_ok=True)
                    recovered += 1
        return recovered

    def _claim(self) -> tuple[Path, dict[str, Any]] | None:
        now = time.time()
        with exclusive_lock(self.root / ".spool.lock"):
            for source in sorted(self.pending.glob("*.json"), key=lambda p: p.stat().st_mtime):
                job = read_json(source, {})
                if float(job.get("not_before_epoch", 0)) > now:
                    continue
                if self.receipt_path(job["dedup_key"]).exists():
                    source.unlink(missing_ok=True)
                    continue
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
        return command, action

    def _external_receipt(self, action: dict[str, Any]) -> dict[str, Any] | None:
        source = action.get("receipt_source")
        if not source:
            return None
        path = Path(source)
        if not path.exists():
            return None
        value = read_json(path, {})
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "content": value}

    def _retry(self, path: Path, job: dict[str, Any], error: str) -> dict[str, Any]:
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
        atomic_write_json(self.pending / path.name, job)
        path.unlink(missing_ok=True)
        return {"status": "RETRY_SPOOLED", "attempt": attempt, "retry_in_seconds": delay,
                "error": error, "dedup_key": job["dedup_key"]}

    def work_once(self) -> dict[str, Any]:
        try:
            with exclusive_lock(self.root / ".worker.lock"):
                self.heartbeat("worker", "RUNNING")
                claimed = self._claim()
                if claimed is None:
                    self.heartbeat("worker", "IDLE")
                    return {"status": "IDLE"}
                path, job = claimed
                try:
                    command, action = self._command(job)
                    if action.get("requires_remote"):
                        raise FoundationError("REMOTE_DEPENDENCY_FORBIDDEN")
                    if action.get("requires_network") and not self._network_ready():
                        return self._retry(path, job, "NETWORK_UNAVAILABLE")
                    if os.environ.get("WEF_FAULT_WORKER_CRASH") == "1":
                        raise FoundationError("INJECTED_WORKER_CRASH")
                    completed = subprocess.run(
                        command,
                        cwd=action.get("working_directory"),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=int(action.get("timeout_seconds", 900)),
                        check=False,
                    )
                    if completed.returncode != 0:
                        return self._retry(path, job, f"EXIT_{completed.returncode}:{completed.stderr[-1000:]}")
                    external = self._external_receipt(action)
                    if action.get("receipt_source") and external is None:
                        return self._retry(path, job, "EXTERNAL_RECEIPT_MISSING")
                    receipt = {
                        "schema_version": "1.0",
                        "status": "SUCCESS",
                        "job_id": job["job_id"],
                        "action": job["action"],
                        "idempotency_key": job["idempotency_key"],
                        "dedup_key": job["dedup_key"],
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
                    return self._retry(path, job, f"TIMEOUT:{exc.timeout}")
                except Exception as exc:
                    return self._retry(path, job, f"{type(exc).__name__}:{exc}")
        except FoundationError as exc:
            if str(exc) == "SINGLE_WORKER_LOCKED":
                return {"status": "NOOP_SINGLE_WORKER"}
            raise

    def watchdog(self, *, restart: bool = True) -> dict[str, Any]:
        recovered = self.recover_stale()
        worker = read_json(self.heartbeats / "worker.json", {})
        maximum = int(self.config.get("worker_heartbeat_max_age_seconds", 120))
        stale = True
        if worker.get("observed_at"):
            stale = (datetime.now(UTC) - parse_time(worker["observed_at"])).total_seconds() > maximum
        restart_result = "NOT_NEEDED"
        if stale and restart and os.name == "nt" and self.config.get("worker_task_name"):
            result = subprocess.run(["schtasks.exe", "/run", "/tn", self.config["worker_task_name"]],
                                    capture_output=True, text=True, check=False)
            restart_result = "TRIGGERED" if result.returncode == 0 else f"FAILED:{result.returncode}"
        value = self.heartbeat("watchdog", "HEALTHY" if not stale else "WORKER_STALE",
                               recovered_inflight=recovered, restart_result=restart_result)
        value["worker_stale"] = stale
        return value

    def audit(self) -> dict[str, Any]:
        pending = list(self.pending.glob("*.json"))
        inflight = list(self.inflight.glob("*.json"))
        done = list(self.done.glob("*.json"))
        failed = list(self.failed.glob("*.json"))
        receipts = list(self.receipts.glob("*.json"))
        keys = [p.stem for p in receipts]
        duplicate_receipts = len(keys) - len(set(keys))
        result = {
            "status": "PASS" if not inflight and not failed and duplicate_receipts == 0 else "ATTENTION",
            "observed_at": iso_now(),
            "counts": {"pending": len(pending), "inflight": len(inflight), "done": len(done),
                       "failed": len(failed), "receipts": len(receipts)},
            "duplicate_receipts": duplicate_receipts,
            "receipt_coverage": 1.0 if not done else min(1.0, len(receipts) / len(done)),
            "remote_required_actions": [name for name, action in self.config.get("actions", {}).items()
                                        if action.get("requires_remote")],
        }
        atomic_write_json(self.root / "audit-last.json", result)
        return result
