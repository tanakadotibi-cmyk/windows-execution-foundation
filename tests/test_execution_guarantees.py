"""Evidence identity, reflection-before-retry, audit matching, and live-owner deadlines."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.cli import _finish
from windows_execution_foundation.core import (
    RESULT_UNKNOWN,
    ExecutionFoundation,
    atomic_write_json,
    stable_key,
)


def _completed(command, code=0, **_kwargs):
    import subprocess
    return subprocess.CompletedProcess(command, code, "", "")


class GuaranteeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.receipt_dir = self.root / "external"
        self.receipt_dir.mkdir()

    def foundation(self, **config):
        base = {
            "state_dir": str(self.root / "state"),
            "max_attempts": 4,
            "retry_base_seconds": 0,
            "inflight_stale_seconds": 0,
            "inflight_stale_grace_seconds": 300,
            "worker_heartbeat_max_age_seconds": 120,
            "worker_heartbeat_interval_seconds": 30,
            "actions": {
                "publish-verify": {
                    "command": ["python", "-c", "pass"],
                    "allowed_args": [],
                    "timeout_seconds": 20,
                    "requires_network": False,
                    "requires_remote": False,
                    "receipt_source_dir": str(self.receipt_dir),
                }
            },
        }
        base.update(config)
        return ExecutionFoundation(base)

    def inflight(self, foundation, key, **fields):
        dedup = stable_key(key)
        job = {
            "schema_version": "1.0",
            "job_id": "job-" + key,
            "execution_id": "exec-" + key,
            "execution_started_at": "2020-01-01T00:00:00Z",
            "action": "publish-verify",
            "idempotency_key": key,
            "dedup_key": dedup,
            "attempt": 0,
            "claimed_at": "2000-01-01T00:00:00Z",
            "worker_pid": 2_000_000_000,
            "created_at": "2000-01-01T00:00:00Z",
            "metadata": {},
        }
        job.update(fields)
        path = foundation.inflight / f"{dedup}.json"
        atomic_write_json(path, job)
        return path, job


class EvidenceIdentityTests(GuaranteeTest):
    def test_old_success_file_is_unknown_and_is_not_resent(self):
        foundation = self.foundation()
        foundation.enqueue(action="publish-verify", idempotency_key="old-evidence")
        dedup = stable_key("old-evidence")
        atomic_write_json(self.receipt_dir / f"{dedup}.json", {
            "status": "PUBLISHED_VERIFIED",
            "idempotency_key": "old-evidence",
            "dedup_key": dedup,
            "completed_at": "1999-01-01T00:00:00Z",
        })
        with mock.patch("windows_execution_foundation.core.subprocess.run", side_effect=_completed) as run:
            result = foundation.work_once()
            self.assertEqual(RESULT_UNKNOWN, result["status"])
            self.assertFalse(result["resend"])
            self.assertEqual(1, run.call_count)
            again = foundation.work_once()
        self.assertEqual("IDLE", again["status"])
        self.assertEqual(1, run.call_count)
        self.assertFalse(list(foundation.pending.glob("*.json")))
        replay = foundation.enqueue(action="publish-verify", idempotency_key="old-evidence")
        self.assertEqual(RESULT_UNKNOWN, replay["status"])
        self.assertFalse(replay["resend"])

    def test_other_jobs_evidence_is_unknown_and_is_not_resent(self):
        foundation = self.foundation()
        foundation.enqueue(action="publish-verify", idempotency_key="job-a")
        dedup = stable_key("job-a")

        def write_other(command, **_kwargs):
            atomic_write_json(self.receipt_dir / f"{dedup}.json", {
                "status": "PUBLISHED_VERIFIED",
                "reservation_key": "job-b",
                "execution_id": "exec-other",
                "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            })
            return _completed(command)

        with mock.patch("windows_execution_foundation.core.subprocess.run", side_effect=write_other) as run:
            result = foundation.work_once()
        self.assertEqual(RESULT_UNKNOWN, result["status"])
        self.assertEqual("EVIDENCE_MISMATCH", result["reason"])
        self.assertEqual(1, run.call_count)
        self.assertFalse(foundation.receipt_path(dedup).exists())
        self.assertFalse(list(foundation.pending.glob("*.json")))

    def test_evidence_after_start_for_this_execution_is_success(self):
        foundation = self.foundation()
        foundation.enqueue(action="publish-verify", idempotency_key="job-ok")

        def write_own(command, **_kwargs):
            inflight = next(foundation.inflight.glob("*.json"))
            job = atomic_read(inflight)
            atomic_write_json(self.receipt_dir / f"{job['execution_id']}.json", {
                "status": "PUBLISHED_VERIFIED",
                "execution_id": job["execution_id"],
                "idempotency_key": job["idempotency_key"],
                "dedup_key": job["dedup_key"],
                "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            })
            return _completed(command)

        with mock.patch("windows_execution_foundation.core.subprocess.run", side_effect=write_own):
            result = foundation.work_once()
        self.assertEqual("SUCCESS", result["status"])
        self.assertTrue(result["execution_id"])
        self.assertTrue((foundation.receipts / "executions" / f"{result['execution_id']}.json").exists())
        replay = foundation.enqueue(action="publish-verify", idempotency_key="job-ok")
        self.assertEqual("NOOP_RECEIPT", replay["status"])

    def test_uncommitted_receipt_file_is_not_a_success(self):
        foundation = self.foundation()
        dedup = stable_key("stale-receipt")
        atomic_write_json(foundation.receipt_path(dedup), {
            "status": "FAILED",
            "idempotency_key": "someone-else",
            "dedup_key": dedup,
            "action": "publish-verify",
        })
        result = foundation.enqueue(action="publish-verify", idempotency_key="stale-receipt")
        self.assertEqual(RESULT_UNKNOWN, result["status"])
        self.assertFalse(list(foundation.pending.glob("*.json")))


class ReflectionRetryTests(GuaranteeTest):
    def test_matching_destination_is_adopted_instead_of_rerun(self):
        foundation = self.foundation()
        _path, job = self.inflight(foundation, "reflected", spawn_attempted=True,
                                   execution_started_at="2020-01-01T00:00:00Z")
        atomic_write_json(self.receipt_dir / f"{job['execution_id']}.json", {
            "status": "PUBLISHED_VERIFIED",
            "execution_id": job["execution_id"],
            "idempotency_key": job["idempotency_key"],
            "dedup_key": job["dedup_key"],
            "completed_at": "2024-01-01T00:00:00Z",
        })
        with mock.patch("windows_execution_foundation.core.subprocess.run") as run:
            self.assertEqual(1, foundation.recover_stale())
            self.assertFalse(run.called)
        self.assertFalse(list(foundation.pending.glob("*.json")))
        self.assertFalse(list(foundation.inflight.glob("*.json")))
        receipt = atomic_read(foundation.receipt_path(job["dedup_key"]))
        self.assertEqual("SUCCESS", receipt["status"])
        self.assertEqual(job["execution_id"], receipt["execution_id"])
        self.assertTrue(receipt["adopted_from_evidence"])

    def test_unknown_reflection_is_not_requeued_as_unexecuted(self):
        foundation = self.foundation()
        _path, job = self.inflight(foundation, "unknown-effect", spawn_attempted=True)
        self.assertEqual(1, foundation.recover_stale())
        self.assertFalse(list(foundation.pending.glob("*.json")))
        failed = atomic_read(foundation.failed / f"{job['dedup_key']}.json")
        self.assertEqual(RESULT_UNKNOWN, failed["status"])
        self.assertEqual("UNKNOWN", failed["result_state"])
        replay = foundation.enqueue(action="publish-verify", idempotency_key="unknown-effect")
        self.assertEqual(RESULT_UNKNOWN, replay["status"])
        self.assertFalse(list(foundation.pending.glob("*.json")))

    def test_unspawned_dead_owner_can_be_requeued(self):
        foundation = self.foundation()
        self.inflight(foundation, "never-started")
        self.assertEqual(1, foundation.recover_stale())
        self.assertEqual(1, len(list(foundation.pending.glob("*.json"))))
        self.assertFalse(list(foundation.failed.glob("*.json")))


class AuditMatchTests(GuaranteeTest):
    def _pending(self, foundation, execution_id, created_at):
        atomic_write_json(foundation.pending / f"{execution_id}.json", {
            "execution_id": execution_id,
            "job_id": execution_id,
            "action": "publish-verify",
            "dedup_key": execution_id,
            "idempotency_key": execution_id,
            "created_at": created_at,
            "not_before_epoch": 0,
        })

    def test_pending_count_does_not_fail_when_oldest_wait_is_inside_the_delay(self):
        foundation = self.foundation(pending_allowed_delay_seconds=900)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for index in range(4):
            self._pending(foundation, f"fresh-{index}", now)
        audit = foundation.audit()
        self.assertEqual("PASS", audit["status"])
        self.assertEqual(4, audit["queue_delay"]["pending_count"])
        self.assertFalse(audit["queue_delay"]["pending_count_affects_status"])
        self.assertEqual("PASS", audit["queue_delay"]["status"])
        self.assertEqual("最古の待機が許容遅延以内", audit["queue_delay"]["pass_criterion"])

    def test_oldest_wait_beyond_allowed_delay_is_attention(self):
        foundation = self.foundation(pending_allowed_delay_seconds=900)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._pending(foundation, "fresh", now)
        self._pending(foundation, "old", "2000-01-01T00:00:00Z")
        audit = foundation.audit()
        self.assertEqual("ATTENTION", audit["status"])
        self.assertEqual("ATTENTION", audit["queue_delay"]["status"])
        self.assertEqual(1, audit["queue_delay"]["overdue_count"])
        self.assertTrue(str(audit["queue_delay"]["oldest_waiting_at"]).startswith("2000-01-01"))
        self.assertGreater(audit["queue_delay"]["pending_count"], audit["queue_delay"]["overdue_count"])

    def test_done_and_receipt_are_matched_by_execution_id(self):
        foundation = self.foundation()
        done = {
            "execution_id": "exec-done",
            "job_id": "job-done",
            "action": "publish-verify",
            "idempotency_key": "same-key",
            "dedup_key": "dedup-same",
        }
        atomic_write_json(foundation.done / "dedup-same.json", done)
        atomic_write_json(foundation.receipt_path("dedup-same"), {
            "status": "SUCCESS",
            "execution_id": "exec-other",
            "action": "publish-verify",
            "idempotency_key": "same-key",
            "dedup_key": "dedup-same",
        })
        mismatched = foundation.audit()
        self.assertEqual("ATTENTION", mismatched["status"])
        self.assertEqual(["exec-done"], mismatched["completion_evidence"]["unmatched_done"])
        self.assertEqual(0.0, mismatched["receipt_coverage"])
        atomic_write_json(foundation.receipt_path("dedup-same"), {
            "status": "SUCCESS",
            "execution_id": "exec-done",
            "action": "publish-verify",
            "idempotency_key": "same-key",
            "dedup_key": "dedup-same",
        })
        matched = foundation.audit()
        self.assertEqual("PASS", matched["status"])
        self.assertEqual(1.0, matched["receipt_coverage"])
        self.assertEqual([], matched["completion_evidence"]["unmatched_done"])


class OwnershipDeadlineTests(GuaranteeTest):
    def test_fresh_heartbeat_keeps_a_living_owner(self):
        foundation = self.foundation(inflight_stale_seconds=0, inflight_stale_grace_seconds=0)
        foundation.config["actions"]["publish-verify"]["timeout_seconds"] = 1
        self.inflight(
            foundation,
            "living",
            worker_pid=os.getpid(),
            owner_heartbeat_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            claimed_at="2000-01-01T00:00:00Z",
        )
        self.assertEqual(0, foundation.recover_stale())
        self.assertEqual(1, len(list(foundation.inflight.glob("*.json"))))

    def test_reclaim_waits_for_the_child_maximum(self):
        foundation = self.foundation(
            inflight_stale_seconds=300,
            inflight_stale_grace_seconds=0,
        )
        foundation.config["actions"]["publish-verify"]["timeout_seconds"] = 900
        self.assertGreaterEqual(foundation._ownership_deadline_seconds({"action": "publish-verify"}), 900)
        recent = (datetime.now(UTC) - timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
        self.inflight(foundation, "still-inside-child-max", claimed_at=recent)
        self.assertEqual(0, foundation.recover_stale())
        self.assertEqual(1, len(list(foundation.inflight.glob("*.json"))))
        self.inflight(foundation, "past-child-max", claimed_at="2000-01-01T00:00:00Z")
        self.assertEqual(1, foundation.recover_stale())
        self.assertTrue((foundation.pending / f"{stable_key('past-child-max')}.json").exists())
        self.assertTrue((foundation.inflight / f"{stable_key('still-inside-child-max')}.json").exists())

    def test_owner_pulse_refreshes_the_running_timestamp(self):
        foundation = self.foundation()
        foundation.enqueue(action="publish-verify", idempotency_key="pulse")
        seen = {}

        def observe(command, **_kwargs):
            heartbeat = atomic_read(foundation.heartbeats / "worker.json")
            inflight = atomic_read(next(foundation.inflight.glob("*.json")))
            seen["heartbeat"] = heartbeat
            seen["job"] = inflight
            return _completed(command)

        with mock.patch("windows_execution_foundation.core.subprocess.run", side_effect=observe):
            foundation.work_once()
        self.assertEqual("RUNNING", seen["heartbeat"]["status"])
        self.assertEqual(seen["job"]["execution_id"], seen["heartbeat"]["execution_id"])
        self.assertTrue(seen["job"]["owner_heartbeat_at"])
        self.assertEqual(os.getpid(), seen["job"]["worker_pid"])

    def test_watchdog_does_not_treat_a_living_process_as_stale(self):
        foundation = self.foundation(worker_heartbeat_max_age_seconds=120)
        self.inflight(foundation, "busy", worker_pid=os.getpid(), claimed_at="2000-01-01T00:00:00Z")
        atomic_write_json(foundation.heartbeats / "worker.json", {
            "name": "worker",
            "status": "RUNNING",
            "observed_at": "2000-01-01T00:00:00Z",
            "pid": os.getpid(),
        })
        result = foundation.watchdog(restart=False)
        self.assertEqual("HEALTHY", result["status"])
        self.assertFalse(result["worker_stale"])
        self.assertEqual(0, result["recovered_inflight"])
        self.assertTrue((foundation.inflight / f"{stable_key('busy')}.json").exists())

        atomic_write_json(foundation.heartbeats / "worker.json", {
            "name": "worker",
            "status": "RUNNING",
            "observed_at": "2000-01-01T00:00:00Z",
            "pid": 2_000_000_000,
        })
        stale = foundation.watchdog(restart=False)
        self.assertEqual("WORKER_STALE", stale["status"])
        self.assertTrue(stale["worker_stale"])


class ExitCodeTests(unittest.TestCase):
    def test_exit_codes_follow_the_result_state(self):
        cases = {
            "SUCCESS": 0,
            "IDLE": 0,
            "HEALTHY": 0,
            "PASS": 0,
            "RETRY_SPOOLED": 0,
            "DEFERRED_EXTERNAL": 0,
            RESULT_UNKNOWN: 1,
            "FAILED_FINAL": 2,
            "FAILED_REQUEUE_RELEASE": 2,
            "ATTENTION": 2,
            "WORKER_STALE": 3,
            "WORKER_BUSY_STALE_HEARTBEAT": 3,
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                self.assertEqual(code, _finish({"status": status}))
        self.assertEqual(2, _finish({"status": "IDLE", "local_exec_reason_code": "REQUEST_EXPIRED"}))


def atomic_read(path: Path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
