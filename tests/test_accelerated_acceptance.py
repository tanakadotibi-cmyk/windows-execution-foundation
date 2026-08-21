from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, iso_now, read_json


class AcceleratedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.counter = self.root / "counter.txt"
        self.receipt_source = self.root / "external.json"
        self.helper = self.root / "helper.py"
        self.helper.write_text(
            "from pathlib import Path\n"
            "import json,sys\n"
            "counter=Path(sys.argv[1]); receipt=Path(sys.argv[2])\n"
            "n=int(counter.read_text() if counter.exists() else '0')+1\n"
            "counter.write_text(str(n))\n"
            "receipt.write_text(json.dumps({'status':'PUBLISHED_VERIFIED','site_article_id':'53957062'}))\n",
            encoding="utf-8",
        )
        self.config = {
            "state_dir": str(self.root / "state"),
            "max_attempts": 4,
            "retry_base_seconds": 0,
            "inflight_stale_seconds": 0,
            "worker_heartbeat_max_age_seconds": 1,
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

    def test_run_1_remote_stopped_is_not_a_dependency(self):
        queued = self.foundation.enqueue(action="publish-verify", idempotency_key="run1")
        self.assertEqual("ENQUEUED", queued["status"])
        with mock.patch.object(self.foundation, "_network_ready", return_value=True), mock.patch.dict(os.environ, {"REMOTE_AVAILABLE": "0"}):
            result = self.foundation.work_once()
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual("53957062", result["external_receipt"]["content"]["site_article_id"])

    def test_run_2_worker_chrome_crash_is_recovered(self):
        self.foundation.enqueue(action="publish-verify", idempotency_key="run2")
        claimed = self.foundation._claim()
        self.assertIsNotNone(claimed)
        path, job = claimed
        job["claimed_at"] = "2000-01-01T00:00:00Z"
        atomic_write_json(path, job)
        self.assertEqual(1, self.foundation.recover_stale())
        with mock.patch.object(self.foundation, "_network_ready", return_value=True):
            result = self.foundation.work_once()
        self.assertEqual("SUCCESS", result["status"])

    def test_run_3_network_outage_duplicate_and_noop(self):
        first = self.foundation.enqueue(action="publish-verify", idempotency_key="run3")
        duplicate = self.foundation.enqueue(action="publish-verify", idempotency_key="run3")
        self.assertEqual("ENQUEUED", first["status"])
        self.assertEqual("NOOP_QUEUED", duplicate["status"])
        with mock.patch.object(self.foundation, "_network_ready", return_value=False):
            offline = self.foundation.work_once()
        self.assertEqual("RETRY_SPOOLED", offline["status"])
        with mock.patch.object(self.foundation, "_network_ready", return_value=True):
            online = self.foundation.work_once()
        self.assertEqual("SUCCESS", online["status"])
        replay = self.foundation.enqueue(action="publish-verify", idempotency_key="run3")
        self.assertEqual("NOOP_RECEIPT", replay["status"])

    def test_final_audit_receipt_coverage_and_duplicates(self):
        for key in ("a", "b", "c"):
            self.foundation.enqueue(action="publish-verify", idempotency_key=key)
            with mock.patch.object(self.foundation, "_network_ready", return_value=True):
                self.assertEqual("SUCCESS", self.foundation.work_once()["status"])
        audit = self.foundation.audit()
        self.assertEqual("PASS", audit["status"])
        self.assertEqual(0, audit["duplicate_receipts"])
        self.assertEqual(1.0, audit["receipt_coverage"])
        self.assertEqual([], audit["remote_required_actions"])


if __name__ == "__main__":
    unittest.main()
