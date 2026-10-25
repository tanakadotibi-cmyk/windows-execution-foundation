"""Offline contract regression; field IDs/types observed via read-only MCP.

All values are synthetic. MCP returns the state as {id, name, color}; the
production REST reader returns fields keyed by ID and a string select value.
Neither the stopped fixture nor any production state is written by these tests.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from windows_execution_foundation import cityheaven_reservation as city
from windows_execution_foundation.core import atomic_write_json, stable_key


FIELDS = {
    "request_id": "fldD6Yj7NqqVQ78HN", "action_json": "fld7RRwgng2s04gBo",
    "local_exec_payload": "fldrVPEmdH51XxOp6",
    "requested_host": "fld7E4U94zdVwsx9a", "schema": "fldCok1E8A7PaK8mP",
    "action_type": "fldXTTVAqLLNWYL70", "state": "fldSaJbIoVRRnnKYC",
    "due_date": "fldtGFCIkwIqKZZmA",
}
REQUEST_ID = "synthetic-entry-contract-v12"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def synthetic_record():
    action = {
        "schema_version": city.ACTION_SCHEMA, "reservation_key": REQUEST_ID,
        "reservation_at": "2026-09-25T04:00:00Z", "cast": "CAST-TEST",
        "course": "COURSE-TEST",
    }
    return {"id": "recSyntheticOnly", "fields": {
        FIELDS["request_id"]: REQUEST_ID, FIELDS["requested_host"]: "test-host",
        FIELDS["schema"]: "EF_v0.1", FIELDS["action_type"]: city.ACTION_TYPE,
        FIELDS["state"]: {"id": "selSyntheticOnly", "name": "EF_RUNNING", "color": "blueLight2"},
        FIELDS["due_date"]: "2026-09-24T11:59:00Z",
        FIELDS["action_json"]: "CityHeaven予約メール受信｜10分後確認待ち",
        FIELDS["local_exec_payload"]: json.dumps(action),
    }}


class CityHeavenEntryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.record = synthetic_record()
        self.config = {
            "state_dir": str(self.root / "state"),
            "local_exec_airtable": {"fields": FIELDS, "schema_version": "EF_v0.1", "device_id": "test-host"},
            "actions": {"cityheaven-reservation-schedule": {"receipt_source_dir": str(self.root / "receipts")}},
            "cityheaven_reservation": {"reader_path": str(self.root / "never-launch.js"), "node_executable": "never-launch.exe"},
        }
        self.config_path = self.root / "config.json"
        atomic_write_json(self.config_path, self.config)
        _, self.request_hash = city.validate_reservation_record(
            self.config["local_exec_airtable"], self.record["fields"], REQUEST_ID,
            expected_state="EF_RUNNING", allow_past=True, now=NOW,
        )
        self.claim = {"action": "cityheaven-reservation-schedule", "idempotency_key": REQUEST_ID,
                      "metadata": {"local_exec_action_type": city.ACTION_TYPE, "request_hash": self.request_hash}}
        self.claim_path = self.root / "state" / "spool" / "inflight" / f"{stable_key(REQUEST_ID)}.json"
        atomic_write_json(self.claim_path, self.claim)
        self.manifest_path = self.root / "state" / "cityheaven-reservations" / "jobs" / stable_key(REQUEST_ID) / "manifest.json"
        self.receipt_path = self.root / "receipts" / f"{stable_key(REQUEST_ID)}.json"
        for target in ("subprocess.run", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("EXTERNAL_OPERATION_FORBIDDEN"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(city.TaskScheduler, "create", side_effect=AssertionError("TASK_CREATION_FORBIDDEN"))
        self.create_task = patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, terminal="SUCCEEDED"):
        def stub(config_path, manifest_path, *, foundation_queue=False, policy=None):
            self.assertTrue(foundation_queue)
            self.assertEqual(policy, {"fixture": True})
            self.assertEqual(config_path, self.config_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest), city.ACTION_KEYS | {
                "request_hash", "receipt_path", "reader_path", "node_executable",
                "python_executable", "config_path", "foundation_root", "foundation_src",
                "policy_version", "send_state_path", "staff_notify_enabled",
            })
            self.assertEqual(manifest["reservation_at"], "2026-09-25T04:00:00Z")
            atomic_write_json(self.receipt_path, {
                "reservation_key": REQUEST_ID, "request_hash": self.request_hash,
                "terminal_status": terminal, "reason_code": "MOCK_ONLY",
            })
            return 0 if terminal == "SUCCEEDED" else city.TERMINAL_EXIT
        with mock.patch("windows_execution_foundation.airtable_local_exec.records_by_request_id", return_value=[self.record]), \
                mock.patch("windows_execution_foundation.cityheaven_reservation._prefetch_canonical_policy", return_value={"fixture": True}), \
                mock.patch.object(city, "run_once", side_effect=stub) as run:
            result = city.schedule(self.config_path, REQUEST_ID, now=NOW)
        self.create_task.assert_not_called()
        return result, run.call_count

    def test_field_ids_select_object_past_due_reaches_mock_and_replays(self):
        first, count = self.invoke()
        self.assertEqual((first["status"], count), ("DIRECT_EXECUTED", 1))
        second, count = self.invoke()
        self.assertEqual((second["status"], count), ("TERMINAL_RECEIPT", 0))
        text = self.manifest_path.read_text(encoding="utf-8").lower()
        for forbidden in ("customer", "phone", "email", "task_name", "ef_running", "selsynthetic"):
            self.assertNotIn(forbidden, text)

    def test_rest_string_select_shape_also_reaches_mock(self):
        self.record["fields"][FIELDS["state"]] = "EF_RUNNING"
        self.assertEqual(self.invoke()[1], 1)

    def test_claim_identity_and_hash_validation_before_manifest(self):
        cases = [None, {}, {**self.claim, "action": "other"},
                 {**self.claim, "idempotency_key": "other"},
                 {**self.claim, "metadata": {"local_exec_action_type": "other", "request_hash": self.request_hash}},
                 {**self.claim, "metadata": {"local_exec_action_type": city.ACTION_TYPE, "request_hash": "0" * 64}}]
        for claim in cases:
            with self.subTest(claim=claim):
                self.claim_path.unlink(missing_ok=True)
                if claim is not None:
                    atomic_write_json(self.claim_path, claim)
                with self.assertRaises(city.CityHeavenReservationError) as raised:
                    self.invoke()
                self.assertIn(str(raised.exception), {"FOUNDATION_CLAIM_INVALID", "REQUEST_ID_INPUT_MISMATCH"})
                self.assertFalse(self.manifest_path.exists())

    def test_changed_input_cannot_reuse_claim(self):
        action = json.loads(self.record["fields"][FIELDS["local_exec_payload"]])
        action["course"] = "CHANGED"
        self.record["fields"][FIELDS["local_exec_payload"]] = json.dumps(action)
        with self.assertRaisesRegex(city.CityHeavenReservationError, "REQUEST_ID_INPUT_MISMATCH"):
            self.invoke()
        self.assertFalse(self.manifest_path.exists())

    def test_pii_action_key_rejected_before_manifest(self):
        action = json.loads(self.record["fields"][FIELDS["local_exec_payload"]])
        action["customer_name"] = "SYNTHETIC_SENSITIVE_SENTINEL"
        self.record["fields"][FIELDS["local_exec_payload"]] = json.dumps(action)
        with self.assertRaisesRegex(city.CityHeavenReservationError, "PII_KEY_FORBIDDEN"):
            self.invoke()
        self.assertFalse(self.manifest_path.exists())

    def test_terminal_failure_replay_does_not_relaunch(self):
        first, count = self.invoke(terminal="FAILED")
        self.assertEqual((first["exit_code"], count), (20, 1))
        second, count = self.invoke()
        self.assertEqual((second["terminal_status"], count), ("FAILED", 0))

    def test_cli_contract_rejection_is_named_terminal_not_exit_one(self):
        self.claim_path.unlink()
        with mock.patch("sys.argv", ["entry", "--config", str(self.config_path), "schedule", "--request-id", REQUEST_ID]), \
                mock.patch("windows_execution_foundation.airtable_local_exec.records_by_request_id", return_value=[self.record]), \
                mock.patch.object(city, "run_once") as run:
            self.assertEqual(city.main(), 20)
        run.assert_not_called()
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["reason_code"], "FOUNDATION_CLAIM_INVALID")
        self.assertFalse(self.manifest_path.exists())
