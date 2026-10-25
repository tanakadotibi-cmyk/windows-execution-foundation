"""2026-09-26 lane monitoring: dead-owner reclaim, live-owner retention, RESOURCE_WAIT timer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation import lane_monitor
from windows_execution_foundation.cli import work_cycle
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, stable_key
from windows_execution_foundation.lanes import acquire_resource, release_resource


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc.wait(timeout=20)
    return proc.pid


class LaneMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shared = self.root / "shared"
        self.a = self.foundation("A")
        self.b = self.foundation("B")
        patcher = mock.patch.object(lane_monitor, "send_text", return_value={"state": "SENT"})
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def foundation(self, lane):
        return ExecutionFoundation({
            "lane_id": lane, "state_dir": str(self.root / lane),
            "shared_resource_dir": str(self.shared),
            "actions": {"fixture": {"command": [sys.executable, "-c", "print('ok')"], "timeout_seconds": 5}},
        })

    def job(self, foundation, request, key="repo:x"):
        foundation.enqueue(action="fixture", idempotency_key=request,
                           metadata={"resource": {"key": key, "workdir": None}})
        return json.loads((foundation.pending / (stable_key(request) + ".json")).read_text())

    def rewrite_lease(self, lease, **changes):
        changed = {**lease, **changes}
        atomic_write_json(self.shared / "active" / (lease["token"] + ".json"), changed)
        return changed

    # --- reclaim -----------------------------------------------------------
    def test_dead_owner_lease_is_reclaimed_and_alerted(self):
        lease = acquire_resource(self.a, self.job(self.a, "crashed"))
        self.rewrite_lease(lease, worker_pid=dead_pid())
        new = acquire_resource(self.b, self.job(self.b, "next"))
        self.assertTrue(new)
        self.assertFalse((self.shared / "active" / (lease["token"] + ".json")).exists())
        released = json.loads((self.shared / "released" / (lease["token"] + ".json")).read_text())
        self.assertEqual(released["released_reason"], "OWNER_DEAD_RECLAIMED")
        self.assertEqual(released["reclaim"]["owner_probe"]["owner"], "DEAD")
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("OWNER_DEAD_RECLAIMED", self.send.call_args[0][0])
        release_resource(self.b, new)

    def test_live_owner_is_never_reclaimed_even_when_old(self):
        lease = acquire_resource(self.a, self.job(self.a, "running"))
        # Age is irrelevant: owner (this python process) is alive and predates the lease.
        verdict = lane_monitor.assess_owner(lease)
        self.assertEqual(verdict["owner"], "LIVE", verdict)
        self.assertIsNone(acquire_resource(self.b, self.job(self.b, "waiting")))
        self.assertTrue((self.shared / "active" / (lease["token"] + ".json")).exists())
        self.assertFalse(list((self.shared / "released").glob("*.json")) if (self.shared / "released").exists() else [])
        self.send.assert_not_called()
        release_resource(self.a, lease)

    def test_reused_pid_created_after_lease_is_treated_as_dead(self):
        lease = acquire_resource(self.a, self.job(self.a, "reused"))
        changed = self.rewrite_lease(lease, acquired_at="2001-01-01T00:00:00.000Z")
        verdict = lane_monitor.assess_owner(changed)
        self.assertEqual((verdict["owner"], verdict["reason"]), ("DEAD", "PID_REUSED_AFTER_ACQUIRE"))

    def test_dead_owner_with_live_child_is_not_reclaimed(self):
        lease = acquire_resource(self.a, self.job(self.a, "orphan"))
        self.rewrite_lease(lease, worker_pid=dead_pid())
        with mock.patch.object(lane_monitor, "live_children", return_value=[{"pid": 1, "image": "codex.exe"}]):
            self.assertIsNone(acquire_resource(self.b, self.job(self.b, "blocked")))
        self.assertTrue((self.shared / "active" / (lease["token"] + ".json")).exists())

    def test_dead_owner_lease_retained_by_failed_job_is_not_reclaimed(self):
        self.job(self.a, "failclosed")
        path, job = self.a._claim()
        self.a._retry(path, job, "CHILD_TERMINATION_UNCONFIRMED", safe_to_release=False)
        lease = job["resource_lease"]
        self.rewrite_lease(lease, worker_pid=dead_pid())
        self.assertIsNone(acquire_resource(self.b, self.job(self.b, "blocked")))
        verdict = lane_monitor.reclaim_verdict(self.shared, json.loads(
            (self.shared / "active" / (lease["token"] + ".json")).read_text()))
        self.assertEqual(verdict.get("blocked_by"), "FAILED_JOB_RETAINS_LEASE")

    def test_probe_error_fails_closed(self):
        lease = acquire_resource(self.a, self.job(self.a, "probe"))
        self.rewrite_lease(lease, worker_pid=dead_pid())
        with mock.patch.object(lane_monitor, "process_info", side_effect=OSError("boom")):
            self.assertIsNone(acquire_resource(self.b, self.job(self.b, "blocked")))

    # --- RESOURCE_WAIT timer --------------------------------------------------
    def test_resource_wait_alerts_after_15_minutes_then_hourly_and_clears(self):
        wait = [{"status": "RESOURCE_WAIT"}]
        t0 = 1_000_000.0
        lane_monitor.observe_cycle(self.b, wait, now=t0)
        lane_monitor.observe_cycle(self.b, wait, now=t0 + 10 * 60)
        self.send.assert_not_called()
        lane_monitor.observe_cycle(self.b, [{"status": "NOOP_SINGLE_WORKER"}], now=t0 + 12 * 60)
        state = lane_monitor.observe_cycle(self.b, wait, now=t0 + 16 * 60)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("RESOURCE_WAIT", self.send.call_args[0][0])
        self.assertEqual(state["since_epoch"], t0)
        lane_monitor.observe_cycle(self.b, wait, now=t0 + 40 * 60)
        self.assertEqual(self.send.call_count, 1)
        lane_monitor.observe_cycle(self.b, wait, now=t0 + 77 * 60)
        self.assertEqual(self.send.call_count, 2)
        lane_monitor.observe_cycle(self.b, [{"status": "IDLE"}], now=t0 + 80 * 60)
        self.assertEqual(self.send.call_count, 3)  # one resolved message
        cleared = json.loads((self.b.root / "monitoring" / "resource-wait.json").read_text())
        self.assertNotIn("since_epoch", cleared)
        lane_monitor.observe_cycle(self.b, wait, now=t0 + 81 * 60)
        lane_monitor.observe_cycle(self.b, wait, now=t0 + 90 * 60)
        self.assertEqual(self.send.call_count, 3)  # new timer started, <15 min

    def test_short_wait_clears_silently(self):
        lane_monitor.observe_cycle(self.b, [{"status": "RESOURCE_WAIT"}], now=100.0)
        lane_monitor.observe_cycle(self.b, [{"status": "IDLE"}], now=200.0)
        self.send.assert_not_called()

    def test_work_cycle_records_resource_wait_state(self):
        lease = acquire_resource(self.a, self.job(self.a, "holder"))
        self.job(self.b, "waiter")
        self.assertEqual(work_cycle(self.b, 1)["status"], "RESOURCE_WAIT")
        state = json.loads((self.b.root / "monitoring" / "resource-wait.json").read_text())
        self.assertIn("since_epoch", state)
        release_resource(self.a, lease)
        self.assertEqual(work_cycle(self.b, 1)["status"], "SUCCESS")
        state = json.loads((self.b.root / "monitoring" / "resource-wait.json").read_text())
        self.assertNotIn("since_epoch", state)

    # --- watchdog ---------------------------------------------------------
    def test_watchdog_alert_dedupes_per_condition(self):
        value = {"status": "WORKER_STALE", "restart_result": "FAILED:1"}
        first = lane_monitor.watchdog_alert(self.a, value, 2000, now=5000.0)
        self.assertEqual(set(first), {"WORKER_STALE", "RESTART_FAILED"})
        self.assertIsNone(lane_monitor.watchdog_alert(self.a, value, 2300, now=5000.0 + 1800))
        again = lane_monitor.watchdog_alert(self.a, value, 6000, now=5000.0 + 3700)
        self.assertEqual(set(again), {"WORKER_STALE", "RESTART_FAILED"})
        self.assertEqual(self.send.call_count, 4)

    def test_busy_heartbeat_alerts_only_beyond_action_timeout(self):
        busy = {"status": "WORKER_BUSY_STALE_HEARTBEAT", "restart_result": "DEFERRED_LOCK_HELD"}
        self.assertIsNone(lane_monitor.watchdog_alert(self.a, busy, 850, now=1.0))
        self.assertEqual(lane_monitor.watchdog_alert(self.a, busy, 950, now=2.0),
                         {"WORKER_BUSY_STALE_HEARTBEAT": "SENT"})

    def test_watchdog_healthy_is_silent(self):
        self.assertEqual(self.a.watchdog(restart=False)["status"], "WORKER_STALE")
        self.a.heartbeat("worker", "IDLE")
        result = self.a.watchdog(restart=False)
        self.assertEqual(result["status"], "HEALTHY")
        self.assertNotIn("alerts", result)


class SendTextSafetyTests(unittest.TestCase):
    def test_disabled_without_enabled_flag_or_with_env_kill_switch(self):
        self.assertEqual(lane_monitor.send_text("x", {})["state"], "DISABLED")
        with mock.patch.dict(os.environ, {"WEF_DISABLE_MONITOR_SLACK": "1"}):
            self.assertEqual(lane_monitor.send_text("x", {"foundation_failure_slack": {"enabled": True}})["state"],
                             "DISABLED")


if __name__ == "__main__":
    unittest.main()
