from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from windows_execution_foundation.cli import exit_code_for, main
from windows_execution_foundation.core import (
    ExecutionFoundation,
    atomic_write_json,
    iso_now,
    process_identity,
    read_json,
)


ROOT = Path(__file__).resolve().parents[1]
CORRECT_HELPER = """import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(n))
receipt = Path(os.environ["WEF_RECEIPT_PATH"])
receipt.parent.mkdir(parents=True, exist_ok=True)
generated = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
receipt.write_text(json.dumps({
    "execution_id": os.environ["WEF_EXECUTION_ID"],
    "target": os.environ["WEF_ACTION"],
    "status": "PUBLISHED_VERIFIED",
    "generated_at": generated,
    "site_article_id": "53957062",
}))
"""
COUNT_ONLY_HELPER = """from pathlib import Path
import sys
counter = Path(sys.argv[1])
n = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(n))
"""


class Issue107Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.counter = self.root / "counter.txt"
        self.receipt_source = self.root / "external.json"
        self.helper = self.root / "helper.py"
        self.helper.write_text(CORRECT_HELPER, encoding="utf-8")
        self.config = {
            "state_dir": str(self.root / "state"),
            "max_attempts": 4,
            "retry_base_seconds": 0,
            "inflight_stale_seconds": 0,
            "worker_heartbeat_max_age_seconds": 120,
            "pending_allowed_delay_seconds": 900,
            "actions": {
                "publish-verify": {
                    "command": [sys.executable, str(self.helper), str(self.counter), str(self.receipt_source)],
                    "allowed_args": [],
                    "timeout_seconds": 20,
                    "requires_network": True,
                    "requires_remote": False,
                    "receipt_source": str(self.receipt_source),
                }
            },
        }
        self.foundation = ExecutionFoundation(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_config(self) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        return path

    def _run(self) -> dict:
        return self.foundation.work_once()

    def _counter(self) -> int:
        if not self.counter.exists():
            return 0
        return int(self.counter.read_text(encoding="utf-8"))

    def test_timing_roles_keep_120_300_and_900_distinct(self):
        foundation = ExecutionFoundation({"state_dir": str(self.root / "policy"), "actions": {}})
        policy = foundation.timing_policy()
        self.assertEqual(120, policy["heartbeat_max_age_seconds"])
        self.assertEqual(300, policy["ownership_lease_seconds"])
        self.assertEqual(900, policy["default_child_timeout_seconds"])
        self.assertLess(policy["heartbeat_interval_seconds"], policy["heartbeat_max_age_seconds"])
        self.assertGreaterEqual(policy["ownership_lease_seconds"], policy["heartbeat_max_age_seconds"])
        self.assertGreater(policy["default_child_timeout_seconds"], policy["ownership_lease_seconds"])
        self.assertEqual(["owner_not_alive", "ownership_deadline_passed"], policy["reclaim_requires"])
        short = ExecutionFoundation(
            {
                "state_dir": str(self.root / "policy-short"),
                "worker_heartbeat_max_age_seconds": 120,
                "inflight_stale_seconds": 10,
                "actions": {},
            }
        )
        self.assertEqual(120, short.timing_policy()["ownership_lease_seconds"])

    def test_legacy_shared_receipt_is_not_success_and_is_not_resent(self):
        self.receipt_source.write_text(
            json.dumps(
                {
                    "status": "PUBLISHED_VERIFIED",
                    "execution_id": "someone-else",
                    "target": "publish-verify",
                    "generated_at": iso_now(),
                    "site_article_id": "999",
                }
            ),
            encoding="utf-8",
        )
        self.helper.write_text(COUNT_ONLY_HELPER, encoding="utf-8")
        self.foundation.enqueue(action="publish-verify", idempotency_key="legacy")
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertFalse(result["resend"])
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        self.assertEqual(1, self._counter())
        again = self._run()
        self.assertEqual("IDLE", again["status"])
        self.assertEqual(1, self._counter())
        replay = self.foundation.enqueue(action="publish-verify", idempotency_key="legacy")
        self.assertEqual("NOOP_UNKNOWN", replay["status"])
        self.assertFalse(replay["resend"])
        self.assertEqual(1, self._counter())

    def test_receipt_for_another_execution_is_unknown(self):
        self.helper.write_text(
            CORRECT_HELPER.replace('os.environ["WEF_EXECUTION_ID"]', '"other-execution"'),
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="other-exec")
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertIn("EXECUTION_ID", result["reason"])
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        self.assertEqual("IDLE", self._run()["status"])
        self.assertEqual(1, self._counter())

    def test_receipt_for_another_target_is_unknown(self):
        self.helper.write_text(
            CORRECT_HELPER.replace('os.environ["WEF_ACTION"]', '"other-action"'),
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="other-target")
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertIn("TARGET", result["reason"])
        self.assertEqual(1, self._counter())
        self.assertEqual("IDLE", self._run()["status"])
        self.assertEqual(1, self._counter())

    def test_receipt_written_before_start_is_unknown(self):
        self.helper.write_text(COUNT_ONLY_HELPER, encoding="utf-8")
        queued = self.foundation.enqueue(action="publish-verify", idempotency_key="old-file")
        receipt = self.foundation.external_receipt_path(
            self.config["actions"]["publish-verify"], queued["execution_id"]
        )
        assert receipt is not None
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "execution_id": queued["execution_id"],
                    "target": "publish-verify",
                    "status": "PUBLISHED_VERIFIED",
                    "generated_at": iso_now(),
                    "site_article_id": "111",
                }
            ),
            encoding="utf-8",
        )
        aged = time.time() - 30
        os.utime(receipt, (aged, aged))
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertIn("FILE_MTIME_BEFORE_START", result["reason"])
        self.assertNotEqual(self.receipt_source, receipt)
        self.assertEqual("IDLE", self._run()["status"])
        self.assertEqual(1, self._counter())

    def test_matching_receipt_from_a_dead_owner_is_adopted_without_a_second_run(self):
        queued = self.foundation.enqueue(action="publish-verify", idempotency_key="already-applied")
        pending = self.foundation.pending / f"{queued['dedup_key']}.json"
        job = read_json(pending)
        job["external_started_at"] = "2020-01-01T00:00:00.000Z"
        job["worker_pid"] = 2**30
        job["owner_proc_start"] = "gone"
        job["lease_until"] = "2000-01-01T00:00:00.000Z"
        job["phase"] = "EXTERNAL_EFFECT_STARTED"
        receipt = self.foundation.external_receipt_path(
            self.config["actions"]["publish-verify"], job["execution_id"]
        )
        assert receipt is not None
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "execution_id": job["execution_id"],
                    "target": "publish-verify",
                    "status": "PUBLISHED_VERIFIED",
                    "generated_at": "2020-01-01T00:00:01.000Z",
                    "site_article_id": "53957062",
                }
            ),
            encoding="utf-8",
        )
        inflight = self.foundation.inflight / pending.name
        atomic_write_json(inflight, job)
        pending.unlink()
        adopted = self._run()
        self.assertEqual("SUCCESS", adopted["status"])
        self.assertEqual("53957062", adopted["external_receipt"]["content"]["site_article_id"])
        self.assertEqual(0, self._counter())
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        replay = self.foundation.enqueue(action="publish-verify", idempotency_key="already-applied")
        self.assertEqual("NOOP_RECEIPT", replay["status"])

    def test_crash_after_external_effect_does_not_run_twice(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="crash-window")
        with mock.patch.dict(os.environ, {"WEF_FAULT_DROP_BEFORE_LOCAL_RECEIPT": "1"}):
            dropped = self._run()
        self.assertEqual("DROPPED_BEFORE_LOCAL_RECEIPT", dropped["status"])
        self.assertFalse(dropped["resend"])
        self.assertEqual(1, self._counter())
        path = next(self.foundation.inflight.glob("*.json"))
        job = read_json(path)
        self.assertTrue(job.get("external_started_at"))
        job["worker_pid"] = 2**30
        job["owner_proc_start"] = "gone"
        job["lease_until"] = "2000-01-01T00:00:00.000Z"
        atomic_write_json(path, job)
        adopted = self._run()
        self.assertEqual("SUCCESS", adopted["status"])
        self.assertEqual(1, self._counter())
        self.assertEqual(
            "NOOP_RECEIPT",
            self.foundation.enqueue(action="publish-verify", idempotency_key="crash-window")["status"],
        )

    def test_missing_receipt_after_start_is_not_requeued(self):
        self.helper.write_text(COUNT_ONLY_HELPER, encoding="utf-8")
        queued = self.foundation.enqueue(action="publish-verify", idempotency_key="ambiguous")
        pending = self.foundation.pending / f"{queued['dedup_key']}.json"
        job = read_json(pending)
        job["external_started_at"] = iso_now()
        job["worker_pid"] = 2**30
        job["owner_proc_start"] = "gone"
        job["lease_until"] = "2000-01-01T00:00:00.000Z"
        inflight = self.foundation.inflight / pending.name
        atomic_write_json(inflight, job)
        pending.unlink()
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertFalse(result["resend"])
        self.assertEqual(0, self._counter())
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        self.assertEqual(1, len(list(self.foundation.unknown.glob("*.json"))))

    def test_explicit_not_applied_may_retry_once(self):
        self.helper.write_text(
            """import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(n))
receipt = Path(os.environ["WEF_RECEIPT_PATH"])
receipt.parent.mkdir(parents=True, exist_ok=True)
generated = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
status = "NOT_APPLIED" if n == 1 else "PUBLISHED_VERIFIED"
body = {
    "execution_id": os.environ["WEF_EXECUTION_ID"],
    "target": os.environ["WEF_ACTION"],
    "status": status,
    "generated_at": generated,
}
if n > 1:
    body["site_article_id"] = "53957062"
receipt.write_text(json.dumps(body))
if n == 1:
    raise SystemExit(2)
""",
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="not-applied")
        first = self._run()
        self.assertEqual("RETRY_SPOOLED", first["status"])
        second = self._run()
        self.assertEqual("SUCCESS", second["status"])
        self.assertEqual(2, self._counter())

    def test_not_applied_stops_at_max_attempts_without_unknown_resend(self):
        self.foundation.config["max_attempts"] = 1
        self.helper.write_text(
            """import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(n))
receipt = Path(os.environ["WEF_RECEIPT_PATH"])
receipt.parent.mkdir(parents=True, exist_ok=True)
receipt.write_text(json.dumps({
    "execution_id": os.environ["WEF_EXECUTION_ID"],
    "target": os.environ["WEF_ACTION"],
    "status": "NOT_APPLIED",
    "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
}))
raise SystemExit(2)
""",
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="final-not-applied")
        result = self._run()
        self.assertEqual("FAILED_FINAL", result["status"])
        self.assertEqual(12, exit_code_for(result))
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        self.assertEqual(1, len(list(self.foundation.failed.glob("*.json"))))

    def test_timeout_without_a_matching_receipt_is_unknown(self):
        self.foundation.config["actions"]["publish-verify"]["timeout_seconds"] = 1
        self.helper.write_text(
            """import sys, time
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(n))
time.sleep(5)
""",
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="timeout")
        result = self._run()
        self.assertEqual("RESULT_UNKNOWN", result["status"])
        self.assertEqual("TIMEOUT_EFFECT_UNKNOWN", result["reason"])
        self.assertFalse(result["resend"])
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))
        self.assertEqual(1, self._counter())
        self.assertEqual("IDLE", self._run()["status"])
        self.assertEqual(1, self._counter())

    def test_command_failure_without_external_receipt_protocol_retries(self):
        self.foundation.config["actions"]["local-only"] = {
            "command": [sys.executable, "-c", "import sys; sys.exit(3)"],
            "allowed_args": [],
            "timeout_seconds": 20,
            "requires_network": False,
            "requires_remote": False,
        }
        self.foundation.enqueue(action="local-only", idempotency_key="local-fail")
        result = self.foundation.work_once()
        self.assertEqual("RETRY_SPOOLED", result["status"])
        self.assertEqual(10, exit_code_for(result))

    def test_live_owner_is_not_reclaimed_after_the_lease(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="live-owner")
        claimed = self.foundation._claim()
        assert claimed is not None
        path, job = claimed
        job["claimed_at"] = "2000-01-01T00:00:00.000Z"
        job["heartbeat_at"] = "2000-01-01T00:00:00.000Z"
        job["lease_until"] = "2000-01-01T00:00:00.000Z"
        job["worker_pid"] = os.getpid()
        job["owner_proc_start"] = process_identity(os.getpid())
        atomic_write_json(path, job)
        self.assertEqual(0, self.foundation.recover_stale())
        self.assertEqual(1, len(list(self.foundation.inflight.glob("*.json"))))
        self.assertEqual([], list(self.foundation.pending.glob("*.json")))

    def test_reused_pid_with_a_different_start_token_can_be_reclaimed(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="pid-reuse")
        claimed = self.foundation._claim()
        assert claimed is not None
        path, job = claimed
        job["worker_pid"] = os.getpid()
        job["owner_proc_start"] = "not-this-process"
        job["lease_until"] = "2000-01-01T00:00:00.000Z"
        atomic_write_json(path, job)
        self.assertEqual(1, self.foundation.recover_stale())
        self.assertEqual(0, self._counter())
        self.assertEqual(1, len(list(self.foundation.pending.glob("*.json"))))

    def test_dead_owner_inside_the_lease_is_left_alone(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="lease-open")
        claimed = self.foundation._claim()
        assert claimed is not None
        path, job = claimed
        job["worker_pid"] = 2**30
        job["owner_proc_start"] = "gone"
        job["lease_until"] = "2099-01-01T00:00:00.000Z"
        atomic_write_json(path, job)
        self.assertEqual(0, self.foundation.recover_stale())
        self.assertEqual(1, len(list(self.foundation.inflight.glob("*.json"))))

    def test_running_owner_is_not_stolen(self):
        release = self.root / "release"
        self.helper.write_text(
            "import json, os, sys, time\n"
            "from datetime import UTC, datetime\n"
            "from pathlib import Path\n"
            f"release = Path({str(release)!r})\n"
            "counter = Path(sys.argv[1])\n"
            "n = int(counter.read_text() if counter.exists() else '0') + 1\n"
            "counter.write_text(str(n))\n"
            "for _ in range(200):\n"
            "    if release.exists():\n"
            "        break\n"
            "    time.sleep(0.05)\n"
            "else:\n"
            "    raise SystemExit('release timeout')\n"
            "receipt = Path(os.environ['WEF_RECEIPT_PATH'])\n"
            "receipt.parent.mkdir(parents=True, exist_ok=True)\n"
            "generated = datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')\n"
            "receipt.write_text(json.dumps({\n"
            "    'execution_id': os.environ['WEF_EXECUTION_ID'],\n"
            "    'target': os.environ['WEF_ACTION'],\n"
            "    'status': 'PUBLISHED_VERIFIED',\n"
            "    'generated_at': generated,\n"
            "}))\n",
            encoding="utf-8",
        )
        self.foundation.enqueue(action="publish-verify", idempotency_key="in-progress")
        holder: dict[str, dict] = {}

        def run() -> None:
            holder["result"] = self.foundation.work_once()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            for _ in range(100):
                if list(self.foundation.inflight.glob("*.json")):
                    break
                time.sleep(0.05)
            self.assertEqual(1, len(list(self.foundation.inflight.glob("*.json"))))
            self.assertEqual(0, self.foundation.recover_stale())
            self.assertEqual(1, len(list(self.foundation.inflight.glob("*.json"))))
            release.write_text("go", encoding="utf-8")
            thread.join(10)
        finally:
            release.write_text("go", encoding="utf-8")
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual("SUCCESS", holder["result"]["status"])
        self.assertEqual(1, self._counter())

    def test_watchdog_checks_liveness_before_calling_the_worker_dead(self):
        fresh = ExecutionFoundation({"state_dir": str(self.root / "watch"), "actions": {}})
        self.assertEqual("WORKER_STALE", fresh.watchdog(restart=False)["status"])
        fresh.heartbeat("worker", "RUNNING")
        self.assertEqual("HEALTHY", fresh.watchdog(restart=False)["status"])
        self.assertEqual(0, exit_code_for({"status": "HEALTHY"}))

        heartbeat_path = fresh.heartbeats / "worker.json"
        stale = read_json(heartbeat_path)
        stale["observed_at"] = "2000-01-01T00:00:00.000Z"
        atomic_write_json(heartbeat_path, stale)
        unresponsive = fresh.watchdog(restart=True)
        self.assertEqual("WORKER_UNRESPONSIVE", unresponsive["status"])
        self.assertEqual("SKIPPED_OWNER_ALIVE", unresponsive["restart_result"])
        self.assertFalse(unresponsive["worker_stale"])
        self.assertEqual(15, exit_code_for(unresponsive))

        stale["pid"] = 2**30
        stale["proc_start"] = "gone"
        stale["observed_at"] = "2000-01-01T00:00:00.000Z"
        atomic_write_json(heartbeat_path, stale)
        dead = fresh.watchdog(restart=True)
        self.assertEqual("WORKER_STALE", dead["status"])
        self.assertTrue(dead["worker_stale"])
        self.assertEqual("NOT_NEEDED", dead["restart_result"])
        self.assertEqual(14, exit_code_for(dead))

    def test_queue_delay_is_judged_apart_from_the_pending_count(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="fresh")
        fresh = self.foundation.audit()
        self.assertEqual("PASS", fresh["status"])
        self.assertEqual(1, fresh["queue_delay"]["pending_count"])
        self.assertEqual("PASS", fresh["queue_delay"]["status"])
        self.assertLessEqual(fresh["queue_delay"]["oldest_wait_seconds"], fresh["queue_delay"]["allowed_delay_seconds"])

        path = next(self.foundation.pending.glob("*.json"))
        job = read_json(path)
        job["created_at"] = "2000-01-01T00:00:00.000Z"
        job["not_before_epoch"] = time.time() + 3600
        atomic_write_json(path, job)
        waiting = self.foundation.audit()
        self.assertEqual("PASS", waiting["queue_delay"]["status"])
        self.assertEqual(1, waiting["queue_delay"]["ineligible_pending_count"])
        self.assertIsNone(waiting["queue_delay"]["oldest_waiting_at"])

        job["not_before_epoch"] = 0
        atomic_write_json(path, job)
        overdue = self.foundation.audit()
        self.assertEqual("ATTENTION", overdue["status"])
        self.assertEqual("ATTENTION", overdue["queue_delay"]["status"])
        self.assertEqual("PASS", overdue["identifier_reconciliation"]["status"])
        self.assertIsNotNone(overdue["queue_delay"]["oldest_waiting_at"])
        self.assertGreater(overdue["queue_delay"]["oldest_wait_seconds"], overdue["queue_delay"]["allowed_delay_seconds"])
        self.assertEqual(13, exit_code_for(overdue))

    def test_done_jobs_are_matched_to_receipts_by_execution_id(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="match")
        self.assertEqual("SUCCESS", self._run()["status"])
        audit = self.foundation.audit()
        self.assertEqual("PASS", audit["identifier_reconciliation"]["status"])
        self.assertEqual(1.0, audit["receipt_coverage"])

        execution_id = next(self.foundation.execution_receipts.glob("*.json")).stem
        receipt_path = self.foundation.execution_receipt_path(execution_id)
        receipt = read_json(receipt_path)
        receipt["execution_id"] = "someone-else"
        atomic_write_json(receipt_path, receipt)
        broken = self.foundation.audit()
        self.assertEqual("ATTENTION", broken["status"])
        self.assertEqual("ATTENTION", broken["identifier_reconciliation"]["status"])
        self.assertEqual(0, broken["identifier_reconciliation"]["matched"])
        self.assertEqual(0.0, broken["receipt_coverage"])

    def test_orphan_receipt_fails_the_identifier_check(self):
        atomic_write_json(
            self.foundation.execution_receipt_path("orphan-exec"),
            {"status": "SUCCESS", "execution_id": "orphan-exec", "job_id": "orphan-exec"},
        )
        audit = self.foundation.audit()
        self.assertEqual("ATTENTION", audit["identifier_reconciliation"]["status"])
        self.assertIn("orphan-exec", audit["identifier_reconciliation"]["orphan_execution_ids"])
        self.assertEqual("ATTENTION", audit["status"])

    def test_exit_codes_differ_by_state(self):
        statuses = [
            "RETRY_SPOOLED",
            "RESULT_UNKNOWN",
            "FAILED_FINAL",
            "ATTENTION",
            "WORKER_STALE",
            "WORKER_UNRESPONSIVE",
            "DROPPED_BEFORE_LOCAL_RECEIPT",
            "NOOP_UNKNOWN",
        ]
        codes = [exit_code_for({"status": status}) for status in statuses]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(code != 0 for code in codes))
        self.assertEqual(0, exit_code_for({"status": "SUCCESS"}))
        self.assertEqual(0, exit_code_for({"status": "PASS"}))
        self.assertEqual(1, exit_code_for({"status": "SOMETHING_NEW"}))
        self.assertNotEqual(exit_code_for({"status": "FAILED_FINAL"}), exit_code_for({"status": "ATTENTION"}))

    def test_cli_exit_codes_follow_the_audit_state(self):
        config_path = self._write_config()
        buffer = io.StringIO()
        with mock.patch("sys.argv", ["wef", "--config", str(config_path), "work-once"]):
            with redirect_stdout(buffer):
                idle = main()
        self.assertEqual(0, idle)
        self.assertEqual("IDLE", json.loads(buffer.getvalue())["status"])

        buffer = io.StringIO()
        with mock.patch(
            "sys.argv",
            ["wef", "--config", str(config_path), "enqueue", "--action", "publish-verify", "--idempotency-key", "cli"],
        ):
            with redirect_stdout(buffer):
                queued = main()
        self.assertEqual(0, queued)
        self.assertEqual("ENQUEUED", json.loads(buffer.getvalue())["status"])

        path = next(self.foundation.pending.glob("*.json"))
        job = read_json(path)
        job["created_at"] = "2000-01-01T00:00:00.000Z"
        atomic_write_json(path, job)
        buffer = io.StringIO()
        with mock.patch("sys.argv", ["wef", "--config", str(config_path), "audit"]):
            with redirect_stdout(buffer):
                attention = main()
        self.assertEqual(13, attention)
        payload = json.loads(buffer.getvalue())
        self.assertEqual("ATTENTION", payload["status"])
        self.assertEqual("ATTENTION", payload["queue_delay"]["status"])

    def test_schedule_record_does_not_change_the_one_minute_definition(self):
        script = (ROOT / "install-windows.ps1").read_text(encoding="utf-8")
        lines = script.splitlines()
        record = json.loads((ROOT / "docs" / "schedule-discrepancy-record.json").read_text(encoding="utf-8"))
        definition = record["repository_definition"]
        self.assertEqual(lines[definition["worker_trigger_line"] - 1], definition["worker_trigger_text"])
        self.assertEqual(lines[definition["watchdog_trigger_line"] - 1], definition["watchdog_trigger_text"])
        self.assertEqual(1, definition["repetition_interval_minutes"])
        self.assertIn("New-TimeSpan -Minutes 1", definition["worker_trigger_text"])
        self.assertNotIn("Minutes 5", script)
        self.assertEqual(5, record["machine_report"]["reported_repetition_interval_minutes"])
        self.assertFalse(record["applied_to_machine"])
        self.assertFalse(record["install_script_modified"])
        procedure = (ROOT / "docs" / "windows-schedule-reconciliation.md").read_text(encoding="utf-8")
        self.assertIn("照合", procedure)
        self.assertIn("変えません", procedure)
        self.assertIn("Get-ScheduledTask", procedure)
        self.assertIn("schtasks /Change", procedure)


if __name__ == "__main__":
    unittest.main()
