from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import sync_results
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, read_json, stable_key
from windows_execution_foundation.foundation_failure_slack import (
    _message,
    _sanitized_healer_context,
    attempt_once,
    healer_eligible,
    run_foundation_healer,
)


FIELDS = {
    "request_id": "request_id",
    "requested_host": "requested_host",
    "resolved_host": "resolved_host",
    "action_json": "action_json",
    "action_type": "action_type",
    "state": "state",
    "error_code": "error_code",
    "finished": "finished",
    "heartbeat": "heartbeat",
    "evidence": "evidence",
    "evidence_ref": "evidence_ref",
}


class FoundationFinalFailureSlackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.foundation = ExecutionFoundation({
            "state_dir": str(Path(self.temporary.name) / "state"),
            "max_attempts": 2,
            "retry_base_seconds": 0,
            "actions": {
                "cityheaven-reservation-schedule": {
                    "command": [sys.executable, "-c", "raise SystemExit(1)"],
                    "allowed_args": [],
                    "timeout_seconds": 5,
                    "requires_network": False,
                    "requires_remote": False,
                },
                "codex-local-job": {
                    "command": [sys.executable, "-c", "raise SystemExit(1)"],
                    "allowed_args": [],
                    "timeout_seconds": 5,
                    "requires_network": False,
                    "requires_remote": False,
                },
            },
        })
        self.config = {
            "device_id": "test-foundation-host",
            "fields": FIELDS,
        }

    def record(self, request_id: str, action_type: str = "CITYHEAVEN_RESERVATION_SCHEDULE") -> dict:
        return {
            "id": "rec-test",
            "fields": {
                "request_id": request_id,
                "requested_host": self.config["device_id"],
                "action_type": action_type,
                "action_json": json.dumps({
                    "customer_name": "SENSITIVE CUSTOMER",
                    "phone": "090-SENSITIVE",
                    "email": "sensitive@example.invalid",
                    "body": "SENSITIVE BODY",
                    "token": "SENSITIVE TOKEN",
                }),
                "state": "EF_RUNNING",
            },
        }

    def enqueue(self, request_id: str, action: str, action_type: str) -> str:
        self.foundation.enqueue(
            action=action,
            idempotency_key=request_id,
            metadata={"local_exec_action_type": action_type},
        )
        return stable_key(request_id)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_intermediate_zero_final_one_and_replay_still_one(self, patch):
        request_id = "fixture-final-boundary"
        key = self.enqueue(request_id, "cityheaven-reservation-schedule", "CITYHEAVEN_RESERVATION_SCHEDULE")
        record = self.record(request_id)
        notifier = mock.Mock(return_value={"state": "SENT", "http_status": 200})

        self.assertEqual("RETRY_SPOOLED", self.foundation.work_once()["status"])
        sync_results(self.foundation, self.config, [record], failure_notifier=notifier)
        self.assertEqual(0, notifier.call_count)

        self.assertEqual("FAILED_FINAL", self.foundation.work_once()["status"])
        sync_results(self.foundation, self.config, [record], failure_notifier=notifier)
        self.assertEqual(1, notifier.call_count)
        sync_results(self.foundation, self.config, [record], failure_notifier=notifier)
        self.assertEqual(1, notifier.call_count)

        failed = read_json(self.foundation.failed / f"{key}.json")
        self.assertEqual("SENT", failed["foundation_failure_notification"]["state"])
        self.assertEqual(1, failed["foundation_failure_notification"]["attempt_count"])
        payload = notifier.call_args.args[0]
        self.assertEqual({
            "work_id", "action_type", "route", "terminal_state", "error_code", "attempt_count", "host",
        }, set(payload))
        self.assertEqual("EF_FAILED", payload["terminal_state"])
        self.assertEqual(2, payload["attempt_count"])
        serialized = json.dumps(payload)
        for forbidden in ("action_json", "customer_name", "SENSITIVE", "phone", "email", "body", "token"):
            self.assertNotIn(forbidden, serialized)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_codex_local_job_keeps_completion_as_notification_owner(self, patch):
        request_id = "codex-final-boundary"
        key = self.enqueue(request_id, "codex-local-job", "CODEX_LOCAL_JOB")
        (self.foundation.pending / f"{key}.json").unlink()
        atomic_write_json(self.foundation.failed / f"{key}.json", {
            "action": "codex-local-job",
            "attempt": 5,
            "last_error": "EXIT_1",
            "metadata": {"local_exec_action_type": "CODEX_LOCAL_JOB"},
        })
        notifier = mock.Mock(return_value={"state": "SENT"})
        result = sync_results(
            self.foundation,
            self.config,
            [self.record(request_id, "CODEX_LOCAL_JOB")],
            failure_notifier=notifier,
        )
        self.assertEqual(1, result["failed"])
        self.assertEqual(0, notifier.call_count)
        self.assertNotIn("foundation_failure_notification", read_json(self.foundation.failed / f"{key}.json"))

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_notification_failure_preserves_original_failure_without_requeue(self, patch):
        request_id = "notification-fails"
        key = self.enqueue(request_id, "cityheaven-reservation-schedule", "CITYHEAVEN_RESERVATION_SCHEDULE")
        failed_path = self.foundation.failed / f"{key}.json"
        (self.foundation.pending / f"{key}.json").unlink()
        atomic_write_json(failed_path, {
            "action": "cityheaven-reservation-schedule",
            "attempt": 5,
            "last_error": "EXIT_1:arbitrary subprocess output SENSITIVE",
            "metadata": {"local_exec_action_type": "CITYHEAVEN_RESERVATION_SCHEDULE"},
        })
        notifier = mock.Mock(side_effect=RuntimeError("delivery failed"))

        result = sync_results(self.foundation, self.config, [self.record(request_id)], failure_notifier=notifier)
        self.assertEqual(1, result["failed"])
        self.assertEqual("EF_FAILED", patch.call_args.args[2]["state"])
        self.assertEqual("EXIT_1", patch.call_args.args[2]["error_code"])
        self.assertEqual("EXIT_1:arbitrary subprocess output SENSITIVE", read_json(failed_path)["last_error"])
        self.assertEqual("FAILED", read_json(failed_path)["foundation_failure_notification"]["state"])
        self.assertFalse((self.foundation.pending / f"{key}.json").exists())

        sync_results(self.foundation, self.config, [self.record(request_id)], failure_notifier=notifier)
        self.assertEqual(1, notifier.call_count)

    def test_healer_eligibility_is_initially_cityheaven_only(self):
        base = {"work_id": "safe-request", "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE"}
        self.assertTrue(healer_eligible(base))
        self.assertFalse(healer_eligible({**base, "action_type": "CODEX_LOCAL_JOB"}))
        self.assertFalse(healer_eligible({**base, "action_type": "UNKNOWN_ACTION"}))

    def test_safe_external_reason_and_unsafe_fallback_are_sanitized(self):
        payload = {
            "work_id": "cityheaven|reservation|safe",
            "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE",
            "route": "cityheaven-reservation-schedule",
            "terminal_state": "EF_FAILED",
            "error_code": "LOCAL_SAFE",
            "attempt_count": 2,
            "host": "test-host",
        }
        receipt = {"terminal_external_receipt": {"content": {
            "reason_code": "EXTERNAL_SAFE", "first_failed_boundary": "BOOKING_API",
            "cast": "SECRET CAST", "course": "SECRET COURSE",
            "reservation_at": "2099-01-01", "reservation_key": "SECRET KEY",
            "customer": {"phone": "09012345678", "email": "secret@example.invalid"},
            "url": "https://example.invalid/secret", "token": "TOKEN", "cookie": "COOKIE",
            "path": r"C:\\private\\receipt.json",
        }}}
        status, log_text = _sanitized_healer_context(payload, receipt)
        self.assertEqual("EXTERNAL_SAFE", status["reason_code"])
        self.assertEqual("BOOKING_API", status["first_failed_boundary"])
        serialized = json.dumps(status) + log_text
        for forbidden in ("SECRET", "09012345678", "example.invalid", "TOKEN", "COOKIE", "C:\\\\private"):
            self.assertNotIn(forbidden, serialized)

        receipt["terminal_external_receipt"]["content"]["reason_code"] = "unsafe reason: customer@example.invalid"
        status, _ = _sanitized_healer_context(payload, receipt)
        self.assertEqual("LOCAL_SAFE", status["reason_code"])

    def test_diagnosed_standard_message_includes_ai_diagnosis(self):
        payload = {
            "work_id": "safe-request", "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE",
            "route": "cityheaven-reservation-schedule", "terminal_state": "EF_FAILED",
            "error_code": "EXIT_1", "attempt_count": 1, "host": "test-host",
            "_foundation_healer": {"state": "DIAGNOSED", "model": "gemini-test", "diagnosis": {
                "summary": "safe summary", "likely_cause": "safe cause", "suggested_action": "safe action",
            }},
        }
        message = _message(payload)
        self.assertIn("safe summary", message)
        self.assertIn("safe cause", message)
        self.assertIn("safe action", message)

    @mock.patch("windows_execution_foundation.foundation_failure_slack._CANONICAL_HEALER")
    @mock.patch("windows_execution_foundation.foundation_failure_slack.Path.is_file", return_value=True)
    @mock.patch("windows_execution_foundation.foundation_failure_slack.subprocess.run")
    def test_duplicate_healer_result_reports_zero_gemini_calls(self, run, _is_file, _canonical):
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "state": "DIAGNOSED",
                "airtable_write": {"state": "SKIPPED_DUPLICATE", "write_count": 0},
            }),
        )
        payload = {
            "work_id": "safe-request", "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE",
            "route": "cityheaven-reservation-schedule", "terminal_state": "EF_FAILED",
            "error_code": "EXIT_1", "attempt_count": 1, "host": "test-host",
        }

        result = run_foundation_healer(payload)

        self.assertEqual("DIAGNOSED", result["state"])
        self.assertEqual("SKIPPED_DUPLICATE", result["airtable_write"]["state"])
        self.assertEqual(0, result["gemini_call_count"])
        self.assertEqual(0, result["airtable_write_count"])

    def test_standard_slack_runs_healer_once_and_failure_still_notifies(self):
        failed_path = self.foundation.failed / "standard.json"
        atomic_write_json(failed_path, {"attempt": 1})
        payload = {
            "work_id": "safe-request", "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE",
            "route": "cityheaven-reservation-schedule", "terminal_state": "EF_FAILED",
            "error_code": "EXIT_1", "attempt_count": 1, "host": "test-host",
        }
        notifier = mock.Mock(return_value={"state": "SENT"})
        with mock.patch(
            "windows_execution_foundation.foundation_failure_slack.run_foundation_healer",
            return_value={"state": "RATE_LIMITED", "gemini_call_count": 1, "airtable_write_count": 0},
        ) as healer:
            self.assertTrue(attempt_once(
                self.foundation, failed_path, payload, notifier,
                standard_slack=True,
            ))
        healer.assert_called_once()
        notifier.assert_called_once()
        self.assertEqual("RATE_LIMITED", notifier.call_args.args[0]["_foundation_healer"]["state"])

    def test_custom_notifier_keeps_public_payload_and_never_runs_healer(self):
        failed_path = self.foundation.failed / "custom.json"
        atomic_write_json(failed_path, {"attempt": 1})
        payload = {
            "work_id": "safe-request", "action_type": "CITYHEAVEN_RESERVATION_SCHEDULE",
            "route": "cityheaven-reservation-schedule", "terminal_state": "EF_FAILED",
            "error_code": "EXIT_1", "attempt_count": 1, "host": "test-host",
        }
        notifier = mock.Mock(return_value={"state": "SENT"})
        with mock.patch("windows_execution_foundation.foundation_failure_slack.run_foundation_healer") as healer:
            self.assertTrue(attempt_once(self.foundation, failed_path, payload, notifier))
        healer.assert_not_called()
        self.assertEqual(payload, notifier.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
