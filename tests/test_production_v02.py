from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import run_before_work
from windows_execution_foundation.core import ExecutionFoundation, FoundationError


class ProductionV02Tests(unittest.TestCase):
    def make_foundation(self, timeout: int = 60) -> ExecutionFoundation:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return ExecutionFoundation({
            "state_dir": str(Path(self.tmp.name) / "state"),
            "worker_heartbeat_max_age_seconds": 900,
            "inflight_stale_seconds": 900,
            "inflight_stale_grace_seconds": 300,
            "actions": {
                "a": {
                    "command": ["noop"],
                    "allowed_args": [],
                    "timeout_seconds": timeout,
                    "requires_network": False,
                    "requires_remote": False,
                }
            },
        })

    def test_stale_limit_has_15_minute_floor(self):
        f = self.make_foundation(timeout=60)
        self.assertEqual(f._inflight_stale_limit({"action": "a"}), 900)

    def test_stale_limit_respects_timeout_plus_grace(self):
        f = self.make_foundation(timeout=1200)
        self.assertEqual(f._inflight_stale_limit({"action": "a"}), 1500)

    @mock.patch("windows_execution_foundation.airtable_local_exec.ingest_queued")
    @mock.patch("windows_execution_foundation.airtable_local_exec.sync_results")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_before_cycle_uses_one_airtable_snapshot(self, records, sync_results, ingest_queued):
        f = self.make_foundation()
        snapshot = []
        records.return_value = snapshot
        sync_results.return_value = {"succeeded": 0, "failed": 0}
        ingest_queued.return_value = {"enqueued": 0, "noop": 0, "rejected": 0}
        cfg = {"fields": {}, "action_map": {}}
        result = run_before_work(f, cfg)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(records.call_count, 1)
        sync_results.assert_called_once_with(f, cfg, snapshot)
        ingest_queued.assert_called_once_with(f, cfg, snapshot)

    def test_watchdog_defers_when_worker_lock_is_held(self):
        f = self.make_foundation()
        with mock.patch(
            "windows_execution_foundation.core.exclusive_lock",
            side_effect=FoundationError("SINGLE_WORKER_LOCKED"),
        ):
            result = f.watchdog(restart=False)
        self.assertEqual(result["status"], "WORKER_BUSY_STALE_HEARTBEAT")
        self.assertEqual(result["restart_result"], "DEFERRED_LOCK_HELD")
        self.assertTrue(result["worker_lock_held"])


if __name__ == "__main__":
    unittest.main()
