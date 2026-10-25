from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import (
    LocalExecError,
    _filter_formula,
    _sanitize_result,
    evaluate_preimplementation_gate,
    ingest_queued,
    sync_results,
    validate_codex_request,
)
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, stable_key
try:
    from windows_execution_foundation.outcome_efficiency import OutcomeEfficiencyEnforcer
except ModuleNotFoundError:
    OutcomeEfficiencyEnforcer = None

FIELDS = {
    "action_json": "action_json",
    "request_id": "request_id",
    "requested_host": "requested_host",
    "resolved_host": "resolved_host",
    "schema": "schema",
    "action_type": "action_type",
    "state": "state",
    "error_code": "error_code",
    "finished": "finished",
    "started": "started",
    "heartbeat": "heartbeat",
    "evidence": "evidence",
    "evidence_ref": "evidence_ref",
}

class LocalExecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = str(Path(self.tmp.name) / "state")
        self.foundation = ExecutionFoundation({
            "state_dir": state,
            "actions": {
                "check-api-observer-status": {
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "allowed_args": [],
                    "timeout_seconds": 5,
                    "requires_network": False,
                    "requires_remote": False,
                }
            },
        })
        self.config = {
            "schema_version": "EF_v0.1",
            "device_id": "tantan-3dc738bff8a2",
            "base_id": "appTEST",
            "table_id": "tblTEST",
            "action_map": {"CHECK_API_OBSERVER_STATUS": "check-api-observer-status"},
            "fields": FIELDS,
        }

    def queued(self, request_id="req-1", action_type="CHECK_API_OBSERVER_STATUS"):
        return {
            "id": "rec1",
            "fields": {
                "request_id": request_id,
                "action_type": action_type,
                "state": "EF_QUEUED",
            },
        }
    def test_filter_is_exact(self):
        formula = _filter_formula(self.config)
        self.assertIn('{Local Exec Schema}="EF_v0.1"', formula)
        self.assertIn('{Requested Host Device ID}="tantan-3dc738bff8a2"', formula)
        self.assertIn('{Local Exec State}="EF_QUEUED"', formula)
        self.assertIn('{Local Exec State}="EF_RUNNING"', formula)
        self.assertNotIn("RECORD_ID()", formula)
        self.assertNotIn("recOA3rMs01oQ4aPQ", formula)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_codex_terminal_receipt_mapping_is_terminal_and_preserves_evidence(self, patch):
        for terminal, expected_state, expected_count in (
            ("VERIFIED", "EF_SUCCEEDED", "succeeded"),
            ("FAILED", "EF_FAILED", "failed"),
            ("VERIFY_FAILED", "EF_FAILED", "failed"),
        ):
            with self.subTest(terminal=terminal):
                patch.reset_mock()
                request_id = "codex-" + terminal.lower()
                record = self.queued(request_id=request_id, action_type="CODEX_LOCAL_JOB")
                record["fields"]["requested_host"] = self.config["device_id"]
                receipt_path = self.foundation.receipt_path(stable_key(request_id))
                atomic_write_json(receipt_path, {
                    "status": "SUCCESS",
                    "external_receipt": {"content": {
                        "schema_version": "codex_local_job_result_v1",
                        "request_id": request_id,
                        "terminal_status": terminal,
                        "reason_code": "VERIFY FAILED: unsafe/value" if terminal != "VERIFIED" else None,
                        "job_id": "job-1",
                    }},
                })
                result = sync_results(self.foundation, self.config, [record])
                self.assertEqual(result[expected_count], 1)
                fields = patch.call_args.args[2]
                self.assertEqual(fields["state"], expected_state)
                self.assertIn(terminal, fields["evidence"])
                self.assertIn("foundation_receipt=", fields["evidence_ref"])
                if terminal == "VERIFIED":
                    self.assertEqual(fields["error_code"], "")
                else:
                    self.assertEqual(fields["error_code"], "VERIFY_FAILED_unsafe_value")
                self.assertFalse((self.foundation.pending / f"{stable_key(request_id)}.json").exists())

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_duplicate_ingress_is_foundation_noop(self, records, patch):
        records.return_value = [self.queued()]
        first = ingest_queued(self.foundation, self.config)
        second = ingest_queued(self.foundation, self.config)
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["noop"], 1)
        key = stable_key("req-1")
        self.assertTrue((self.foundation.pending / f"{key}.json").exists())
        self.assertEqual(len(list(self.foundation.pending.glob("*.json"))), 1)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_unknown_action_rejected_without_enqueue(self, records, patch):
        records.return_value = [self.queued(action_type="RUN_POWERSHELL")]
        result = ingest_queued(self.foundation, self.config)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(len(list(self.foundation.pending.glob("*.json"))), 0)
        self.assertEqual(patch.call_args.args[2]["state"], "EF_REJECTED")
        self.assertEqual(patch.call_args.args[2]["error_code"], "ALLOWLIST_DENIED")
    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_completed_receipt_replays_result_only(self, records, patch):
        records.return_value = [self.queued()]
        key = stable_key("req-1")
        receipt = {
            "status": "SUCCESS",
            "external_receipt": {
                "content": {
                    "observer_exists": False,
                    "running": False,
                    "last_run_at": "UNKNOWN",
                    "last_result": "UNKNOWN",
                    "next_run_at": "UNKNOWN",
                    "latest_report_at": "UNKNOWN",
                    "secret": "must-not-leak",
                }
            },
        }
        atomic_write_json(self.foundation.receipt_path(key), receipt)
        result = sync_results(self.foundation, self.config)
        self.assertEqual(result["succeeded"], 1)
        fields = patch.call_args.args[2]
        self.assertEqual(fields["state"], "EF_SUCCEEDED")
        self.assertNotIn("secret", fields["evidence"])

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_failed_spool_maps_to_failed(self, records, patch):
        records.return_value = [self.queued()]
        key = stable_key("req-1")
        atomic_write_json(self.foundation.failed / f"{key}.json", {"last_error": "TIMEOUT:60"})
        result = sync_results(self.foundation, self.config)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(patch.call_args.args[2]["state"], "EF_FAILED")
        self.assertEqual(patch.call_args.args[2]["error_code"], "TIMEOUT")
    def test_sanitize_fixed_keys_only(self):
        result = _sanitize_result({
            "observer_exists": True,
            "running": False,
            "last_run_at": None,
            "last_result": 0,
            "next_run_at": "UNKNOWN",
            "latest_report_at": "UNKNOWN",
            "token": "secret",
        })
        self.assertEqual(set(result), {
            "observer_exists", "running", "last_run_at",
            "last_result", "next_run_at", "latest_report_at",
        })
        self.assertNotIn("token", result)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_minkue_routing_keeps_immediate_foundation_enqueue(self, patch):
        self.foundation.config["actions"]["minkue-public-fetch"] = {
            "command": [sys.executable, "-c", "raise SystemExit(0)"],
            "allowed_args": [],
        }
        self.config["action_map"]["MINKUE_PUBLIC_FETCH"] = "minkue-public-fetch"
        result = ingest_queued(
            self.foundation, self.config,
            [self.queued(request_id="minkue-1", action_type="MINKUE_PUBLIC_FETCH")],
        )
        self.assertEqual(result["enqueued"], 1)
        job = json.loads(next(self.foundation.pending.glob("*.json")).read_text())
        self.assertEqual(job["action"], "minkue-public-fetch")
        self.assertEqual(job["not_before_epoch"], 0)
        running = patch.call_args.args[2]
        self.assertEqual(running["state"], "EF_RUNNING")
        self.assertTrue(running["started"])
        self.assertTrue(running["heartbeat"])

    def gate(self, **changes):
        values = {
            "existing_success_route": "REUSE",
            "reuse_decision": "EXTEND",
            "scope_mode": "MINIMUM",
            "deterministic_first": "PASS",
            "known_failure_reuse": "BLOCKED",
            "new_system_reason": "",
            "generalization_reason": "",
        }
        values.update(changes)
        return "\n".join([
            "PREIMPLEMENTATION_GATE_V1",
            *(f"{key}={values[key]}" for key in (
                "existing_success_route", "reuse_decision", "scope_mode",
                "deterministic_first", "known_failure_reuse",
                "new_system_reason", "generalization_reason",
            )),
        ])

    def codex_record(self, instruction, *, created="2026-10-01T00:00:01Z"):
        request = {
            "request_id": "codex-gate-1",
            "task_name": "gate test",
            "task_instruction": instruction,
            "verification_spec": {
                "criteria": "focused pass",
                "required_files": [{"path": "local/result.json", "kind": "json"}],
            },
            "requested_at": "2026-10-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        record = self.queued(request_id=request["request_id"], action_type="CODEX_LOCAL_JOB")
        record["createdTime"] = created
        record["fields"].update({
            "requested_host": self.config["device_id"],
            "action_json": json.dumps({
                "schema_version": "codex_local_job_mailbox_v1",
                "host_device_id": self.config["device_id"],
                "action_type": "CODEX_LOCAL_JOB",
                "request": request,
            }),
        })
        return record

    def intent_record(self, task_class="CODE_REPAIR", **changes):
        intent = {
            "goal": "Repair the focused validator",
            "task_class": task_class,
            "target": "src/windows_execution_foundation/airtable_local_exec.py",
            "success": "Focused validation passes",
            "forbidden": "No new infrastructure",
        }
        intent.update(changes)
        instruction = "\n".join(["JOB_INTENT_V1", *(f"{key}={intent[key]}" for key in intent)])
        record = self.codex_record(instruction)
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["verification_spec"] = {
            "version": 1, "criteria": "JOB_INTENT_V1", "required_files": [],
        }
        record["fields"]["action_json"] = json.dumps(envelope)
        return record

    def validate_intent(self, record):
        return validate_codex_request(
            self.config, record["fields"], record["fields"]["request_id"],
            source_created_time=record["createdTime"],
        )[0]

    def test_job_intent_three_classes_normalize_deterministically(self):
        for task_class, reuse, file_count in (
            ("READ_ONLY_CHECKPOINT", "REUSE", 1),
            ("CODE_REPAIR", "EXTEND", 2),
            ("VERIFY_ONLY", "REUSE", 1),
        ):
            with self.subTest(task_class=task_class):
                request = self.validate_intent(self.intent_record(task_class))
                self.assertIn("既にtantan-mf-a1c6659aabef611c上で実行中。端末解決しない。RDC/Remote MCP禁止。PC名をroutingに使わない。", request["task_instruction"])
                self.assertIn(f"reuse_decision={reuse}", request["task_instruction"])
                self.assertIsNone(evaluate_preimplementation_gate(request["task_instruction"]))
                files = request["verification_spec"]["required_files"]
                self.assertEqual(len(files), file_count)
                self.assertEqual(files[0]["path"], "local/codex-gate-1/summary.json")
                self.assertEqual(files[-1]["path"].endswith("proof.json"), task_class == "CODE_REPAIR")
                self.assertIn("producer自己申告だけでVERIFIEDにしない", request["verification_spec"]["criteria"])

    def test_job_intent_full_request_is_not_regenerated(self):
        record = self.codex_record("legacy full instruction")
        original = json.loads(record["fields"]["action_json"])["request"]
        request = self.validate_intent(record)
        self.assertEqual(request, original)

    def test_job_intent_missing_unknown_duplicate_and_oversize_rejected(self):
        cases = {
            "missing": "JOB_INTENT_V1\ngoal=x\ntask_class=CODE_REPAIR\ntarget=x\nsuccess=x",
            "unknown": "JOB_INTENT_V1\ngoal=x\ntask_class=CODE_REPAIR\ntarget=x\nsuccess=x\nother=x",
            "duplicate": "JOB_INTENT_V1\ngoal=x\ntask_class=CODE_REPAIR\ntarget=x\nsuccess=x\ngoal=y",
            "oversize": "JOB_INTENT_V1\ngoal=" + ("x" * 1001) + "\ntask_class=CODE_REPAIR\ntarget=x\nsuccess=x\nforbidden=x",
        }
        for name, instruction in cases.items():
            with self.subTest(name=name):
                record = self.intent_record()
                envelope = json.loads(record["fields"]["action_json"])
                envelope["request"]["task_instruction"] = instruction
                record["fields"]["action_json"] = json.dumps(envelope)
                with self.assertRaisesRegex(LocalExecError, "JOB_INTENT_"):
                    self.validate_intent(record)

    def test_job_intent_unsafe_content_rejected(self):
        cases = (
            {"goal": "use ```code```"},
            {"goal": "run PowerShell now"},
            {"goal": "inspect https://example.invalid"},
            {"goal": "token=sk-abcdefghijklmnopqrstuvwxyz"},
            {"goal": "bad\tcontrol"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(LocalExecError, "JOB_INTENT_"):
                    self.validate_intent(self.intent_record(**changes))

    def test_job_intent_path_traversal_request_id_rejected(self):
        record = self.intent_record()
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["request_id"] = "../escape"
        record["fields"]["request_id"] = "../escape"
        record["fields"]["action_json"] = json.dumps(envelope)
        with self.assertRaisesRegex(LocalExecError, "JOB_INTENT_REQUEST_ID_INVALID"):
            self.validate_intent(record)

    def test_job_intent_invalid_placeholder_rejected(self):
        record = self.intent_record()
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["verification_spec"]["criteria"] = "caller supplied"
        record["fields"]["action_json"] = json.dumps(envelope)
        with self.assertRaisesRegex(LocalExecError, "JOB_INTENT_VERIFICATION_PLACEHOLDER_INVALID"):
            self.validate_intent(record)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_job_intent_reject_occurs_before_enqueue_or_codex_launch(self, patch):
        self.config["action_map"]["CODEX_LOCAL_JOB"] = "codex-local-job"
        record = self.intent_record(goal="run cmd /c whoami")
        with mock.patch.object(self.foundation, "enqueue", wraps=self.foundation.enqueue) as enqueue:
            result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result, {"enqueued": 0, "noop": 0, "rejected": 1})
        enqueue.assert_not_called()
        self.assertEqual(list(self.foundation.pending.glob("*.json")), [])
        self.assertEqual(patch.call_args.args[2]["error_code"], "JOB_INTENT_COMMAND_FORBIDDEN")

    def test_preimplementation_gate_valid_minimum_reuse_passes(self):
        self.assertIsNone(evaluate_preimplementation_gate(self.gate()))

    def test_preimplementation_gate_missing_rejected(self):
        self.assertEqual(
            evaluate_preimplementation_gate("ordinary instruction"),
            "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID",
        )

    def test_preimplementation_gate_existing_asset_rejects_new_system(self):
        self.assertEqual(
            evaluate_preimplementation_gate(
                self.gate(existing_success_route="NONE", reuse_decision="NEW_JUSTIFIED",
                          new_system_reason="needed"),
                {"推奨再利用Asset ID": "asset_existing"},
            ),
            "SHORTEST_PATH_EXISTING_ASSET_NOT_REUSED",
        )

    def test_preimplementation_gate_new_system_reason_required(self):
        self.assertEqual(
            evaluate_preimplementation_gate(
                self.gate(existing_success_route="NONE", reuse_decision="NEW_JUSTIFIED")),
            "NEW_SYSTEM_NOT_JUSTIFIED",
        )

    def test_preimplementation_gate_generalization_reason_required(self):
        self.assertEqual(
            evaluate_preimplementation_gate(
                self.gate(scope_mode="GENERALIZATION_JUSTIFIED")),
            "GENERALIZATION_NOT_JUSTIFIED",
        )

    def test_preimplementation_gate_deterministic_first_invalid(self):
        self.assertEqual(
            evaluate_preimplementation_gate(self.gate(deterministic_first="FAIL")),
            "DETERMINISTIC_FIRST_NOT_SATISFIED",
        )

    def test_preimplementation_gate_known_failure_reuse_invalid(self):
        self.assertEqual(
            evaluate_preimplementation_gate(self.gate(known_failure_reuse="REUSE")),
            "KNOWN_FAILURE_ROUTE_REUSE_BLOCKED",
        )

    def test_preimplementation_gate_chief_of_staff_explicit_fail(self):
        self.assertEqual(
            evaluate_preimplementation_gate(
                self.gate(), {"Chief of Staff Gate": {"name": "FAIL"}}),
            "CHIEF_OF_STAFF_GATE_FAILED",
        )

    def test_preimplementation_gate_legacy_pre_activation_not_rejected(self):
        self.config["preimplementation_gate_v1_enforcement_cutoff"] = "2026-10-01T00:00:00Z"
        record = self.codex_record("legacy instruction", created="2026-09-30T23:59:59Z")
        request, request_hash = validate_codex_request(
            self.config, record["fields"], "codex-gate-1",
            source_created_time=record["createdTime"],
        )
        self.assertEqual(request["task_instruction"], "legacy instruction")
        self.assertEqual(len(request_hash), 64)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._request")
    def test_preimplementation_reject_never_enqueues_or_launches(self, request, patch):
        self.config["preimplementation_gate_v1_enforcement_cutoff"] = "2026-10-01T00:00:00Z"
        self.config["action_map"]["CODEX_LOCAL_JOB"] = "codex-local-job"
        self.foundation.config["actions"]["codex-local-job"] = {
            "command": [sys.executable, "-c", "raise SystemExit(99)"],
            "allowed_args": [],
            "timeout_seconds": 5,
            "requires_network": False,
            "requires_remote": False,
        }
        request.return_value = {"id": "rec1", "fields": {}}
        record = self.codex_record("missing gate")
        with mock.patch.object(self.foundation, "enqueue", wraps=self.foundation.enqueue) as enqueue:
            result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result, {"enqueued": 0, "noop": 0, "rejected": 1})
        enqueue.assert_not_called()
        self.assertEqual(list(self.foundation.pending.glob("*.json")), [])
        self.assertEqual(patch.call_args.args[2]["error_code"], "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID")

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    @mock.patch("windows_execution_foundation.airtable_local_exec._records")
    def test_missing_request_id_rejected(self, records, patch):
        records.return_value = [self.queued(request_id="")]
        result = ingest_queued(self.foundation, self.config)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "REQUEST_ID_REQUIRED")

    @unittest.skipIf(OutcomeEfficiencyEnforcer is None, "MAIN-only outcome_efficiency module not present")
    def test_system_idle_repeat_does_not_self_disable_worker(self):
        state = Path(self.tmp.name) / "oef"
        enforcer = OutcomeEfficiencyEnforcer(state)
        policy = {
            "classification": "EFFICIENCY",
            "operation": "DISPATCH",
            "target": "ExecutionFoundation-Worker",
            "expected_result": "ExecutionFoundation-Worker",
            "actor": "SYSTEM",
            "trigger_mode": "SCHEDULED_TIME",
            "ai_invocation_on_no_change": False,
            "allow_idle_repeat": True,
            "material_evidence_paths": [],
            "material_state_paths": [],
            "material_result_paths": [],
        }
        for _ in range(4):
            decision = enforcer.begin(policy)
            row = enforcer.complete(policy, decision, status="SUCCESS", exit_code=0)
            self.assertEqual(row["status"], "SUCCESS")
        attempts_path = state / "outcome-efficiency-first-v1" / "attempts.json"
        self.assertFalse(attempts_path.exists())

if __name__ == "__main__":
    unittest.main()
