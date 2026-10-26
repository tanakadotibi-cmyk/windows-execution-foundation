"""LocalExecError reasons stay in the cycle output, the error record, and the exit payload."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import LocalExecError
from windows_execution_foundation.cli import _finish, work_cycle
from windows_execution_foundation.core import ExecutionFoundation


class CliReasonCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.foundation = ExecutionFoundation({
            "state_dir": str(Path(self.tmp.name) / "state"),
            "actions": {},
            "local_exec_airtable": {"enabled": True},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_exec_error_keeps_reason_through_output_record_and_exit(self):
        with mock.patch(
            "windows_execution_foundation.cli.run_before_work",
            side_effect=LocalExecError("REQUEST_EXPIRED"),
        ), mock.patch(
            "windows_execution_foundation.cli.run_after_work",
            side_effect=LocalExecError("AIRTABLE_HTTP_403"),
        ), mock.patch.object(self.foundation, "work_once", return_value={"status": "IDLE"}):
            result = work_cycle(self.foundation, 1)
        self.assertEqual(result["status"], "IDLE")
        self.assertEqual(result["local_exec"]["before"]["error_type"], "LocalExecError")
        self.assertEqual(result["local_exec"]["before"]["reason_code"], "REQUEST_EXPIRED")
        self.assertEqual(result["local_exec"]["after"]["reason_code"], "AIRTABLE_HTTP_403")
        self.assertEqual(result["local_exec_reason_code"], "REQUEST_EXPIRED")
        recorded = json.loads(
            (self.foundation.root / "monitoring" / "local-exec-error.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["reason_code"] for item in recorded["phases"]],
            ["REQUEST_EXPIRED", "AIRTABLE_HTTP_403"],
        )
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = _finish(result)
        printed = buffer.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("REQUEST_EXPIRED", printed)
        self.assertIn("AIRTABLE_HTTP_403", printed)
        self.assertIn("LocalExecError", printed)

    def test_other_exceptions_still_keep_only_the_error_type(self):
        with mock.patch(
            "windows_execution_foundation.cli.run_before_work",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "windows_execution_foundation.cli.run_after_work",
            return_value={"status": "PASS"},
        ), mock.patch.object(self.foundation, "work_once", return_value={"status": "IDLE"}):
            result = work_cycle(self.foundation, 1)
        self.assertEqual(result["local_exec"]["before"], {"status": "ERROR", "error_type": "RuntimeError"})
        self.assertNotIn("local_exec_reason_code", result)
        self.assertFalse((self.foundation.root / "monitoring" / "local-exec-error.json").exists())
        buffer = StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(_finish(result), 0)

    def test_record_write_failure_still_prints_json_and_exits_2(self):
        with mock.patch(
            "windows_execution_foundation.cli.run_before_work",
            side_effect=LocalExecError("REQUEST_EXPIRED"),
        ), mock.patch(
            "windows_execution_foundation.cli.run_after_work",
            return_value={"status": "PASS"},
        ), mock.patch.object(self.foundation, "work_once", return_value={"status": "IDLE"}), mock.patch(
            "windows_execution_foundation.cli.atomic_write_json",
            side_effect=OSError("monitoring directory is not writable"),
        ):
            result = work_cycle(self.foundation, 1)
        self.assertEqual(result["local_exec"]["before"]["reason_code"], "REQUEST_EXPIRED")
        self.assertEqual(result["local_exec_reason_code"], "REQUEST_EXPIRED")
        self.assertEqual(result["local_exec_record_write_error"], "OSError")
        self.assertNotIn("monitoring directory", json.dumps(result))
        self.assertFalse((self.foundation.root / "monitoring" / "local-exec-error.json").exists())
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = _finish(result)
        printed = json.loads(buffer.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(printed["local_exec_reason_code"], "REQUEST_EXPIRED")
        self.assertEqual(printed["local_exec"]["before"]["reason_code"], "REQUEST_EXPIRED")
        self.assertEqual(printed["local_exec_record_write_error"], "OSError")
        self.assertNotIn("monitoring directory", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
