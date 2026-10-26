"""Pending-receipt deadline checks. Naive and unreadable clocks stay attention items."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json


class AuditTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.foundation = ExecutionFoundation({
            "state_dir": str(Path(self.tmp.name) / "state"),
            "actions": {},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _write_pending(self, execution_id: str, created_at: str) -> None:
        atomic_write_json(
            self.foundation.pending / f"{execution_id}.json",
            {
                "execution_id": execution_id,
                "job_id": execution_id,
                "action": "publish-verify",
                "dedup_key": execution_id,
                "created_at": created_at,
                "not_before_epoch": 0,
            },
        )

    def test_aware_timestamps_are_compared_without_guessing(self):
        self._write_pending("aware-old", "2000-01-01T00:00:00+00:00")
        self._write_pending("aware-offset", "2099-01-01T00:00:00+09:00")
        audit = self.foundation.audit()
        self.assertEqual("ATTENTION", audit["status"])
        self.assertEqual([], audit["invalid_records"])
        self.assertEqual(1, audit["queue_delay"]["overdue_count"])
        self.assertTrue(str(audit["queue_delay"]["oldest_waiting_at"]).startswith("2000-01-01"))

    def test_naive_timestamp_is_an_invalid_record_and_audit_continues(self):
        self._write_pending("naive-clock", "2026-10-26T02:00:00")
        self._write_pending("aware-old", "2000-01-01T00:00:00+00:00")
        audit = self.foundation.audit()
        self.assertEqual("ATTENTION", audit["status"])
        naive = [item for item in audit["invalid_records"] if item["id"] == "naive-clock"]
        self.assertEqual(
            [{"category": "不正な記録", "field": "created_at", "reason": "時差のない時刻", "value": "2026-10-26T02:00:00"}],
            [{key: item[key] for key in ("category", "field", "reason", "value")} for item in naive],
        )
        self.assertFalse(any(item["id"] == "aware-old" for item in audit["invalid_records"]))
        self.assertEqual(1, audit["queue_delay"]["overdue_count"])
        self.assertTrue(str(audit["queue_delay"]["oldest_waiting_at"]).startswith("2000-01-01"))

    def test_unreadable_timestamp_is_an_invalid_record_and_audit_continues(self):
        self._write_pending("bad-clock", "not-a-time")
        self._write_pending("aware-offset", "2099-01-01T00:00:00+09:00")
        audit = self.foundation.audit()
        self.assertEqual("ATTENTION", audit["status"])
        bad = [item for item in audit["invalid_records"] if item["id"] == "bad-clock"]
        self.assertEqual(
            [{"category": "不正な記録", "field": "created_at", "reason": "読めない時刻", "value": "not-a-time"}],
            [{key: item[key] for key in ("category", "field", "reason", "value")} for item in bad],
        )
        self.assertFalse(any(item["id"] == "aware-offset" for item in audit["invalid_records"]))
        self.assertEqual(0, audit["queue_delay"]["overdue_count"])
        self.assertEqual("PASS", audit["queue_delay"]["status"])


if __name__ == "__main__":
    unittest.main()
