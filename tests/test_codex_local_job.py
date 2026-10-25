from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import LocalExecError, ingest_queued, validate_codex_request
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, stable_key
from windows_execution_foundation.codex_local_job import (
    RESUME_CONTEXT_FIELDS,
    _project_failure_evidence,
    compose_resume_instruction,
    execute,
    read_resume_context,
)


class CodexLocalJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.foundation = ExecutionFoundation({
            "state_dir": str(Path(self.temporary.name) / "state"),
            "actions": {"codex-local-job": {
                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                "allowed_args": [], "append_airtable_record_id": True,
                "append_idempotency_key": True,
                "timeout_seconds": 5, "requires_network": False, "requires_remote": False,
            }},
        })
        self.config = {
            "schema_version": "EF_v0.1", "device_id": "tantan-mf-a1c6659aabef611c",
            "action_map": {"CODEX_LOCAL_JOB": "codex-local-job"},
            "fields": {"request_id": "request_id", "requested_host": "requested_host",
                       "action_json": "action_json", "local_exec_payload": "local_exec_payload",
                       "action_type": "action_type", "state": "state",
                       "schema": "schema",
                       "finished": "finished", "resolved_host": "resolved_host",
                       "heartbeat": "heartbeat", "error_code": "error_code", "started": "started",
                       "work_number": "work_number"},
        }

    def record(self, request_id="request-1", host="tantan-mf-a1c6659aabef611c", expired=False,
               schema="codex_local_job_mailbox_v1", work_number=None, airtable_work_number=None):
        now = datetime.now(UTC)
        request = {
            "request_id": request_id, "task_name": "Harmless canary",
            "task_instruction": "Create a harmless JSON canary file.",
            "verification_spec": {"version": 1, "criteria": "Check canary", "required_files": [{"path": "canary.json", "kind": "json"}]},
            "requested_at": (now - timedelta(hours=2 if expired else 1)).isoformat(),
            "expires_at": (now - timedelta(hours=1) if expired else now + timedelta(hours=1)).isoformat(),
        }
        if work_number is not None:
            request["work_number"] = work_number
        envelope = {"schema_version": schema, "host_device_id": self.config["device_id"],
                    "action_type": "CODEX_LOCAL_JOB", "request": request}
        fields = {"request_id": request_id, "requested_host": host,
                "schema": "EF_v0.1",
                "action_json": json.dumps(envelope), "action_type": "CODEX_LOCAL_JOB", "state": "EF_QUEUED"}
        if airtable_work_number is not None:
            fields["work_number"] = airtable_work_number
        return {"id": "rec1", "fields": fields}

    def execution_fixture(self, request_id="request-1", record_id="recA"):
        root = Path(self.temporary.name)
        bridge = root / ("bridge-" + request_id)
        bridge.mkdir(exist_ok=True)
        (self.foundation.root / "local-exec-v01").mkdir(parents=True, exist_ok=True)
        configuration = {
            "state_dir": str(self.foundation.root),
            "local_exec_airtable": self.config,
            "codex_local_job": {
                "bridge_root": str(bridge),
                "default_workdir": str(root), "approved_roots": [str(root)],
                "default_model": "gpt-5.6-luna", "default_reasoning": "medium",
                "approved_models": ["gpt-5.6-luna"], "approved_reasoning": ["medium"],
                "codex_timeout_seconds": 60, "verifier_timeout_seconds": 60,
                "run_timeout_seconds": 150,
            },
        }
        config_path = root / ("config-" + request_id + ".json")
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        record = self.record(request_id=request_id)
        record["id"] = record_id
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            ingest_queued(self.foundation, self.config, [record])
        record["fields"]["state"] = "EF_RUNNING"
        return config_path, bridge, record

    def resume_execution_fixture(self, request_id="request-resume", record_id="recResume",
                                 resume_id="recPrior"):
        config_path, bridge, record = self.execution_fixture(request_id, record_id)
        envelope = json.loads(record["fields"]["action_json"])
        request = envelope["request"]
        envelope["schema_version"] = "codex_local_job_mailbox_v3"
        request.update({
            "work_number": "20260930-120000-01",
            "display_name_ja": "再開テスト", "stage_key": "resume-test", "completion_scope": "STEP",
            "work_identity": {"project_id": "p", "scope_key": "s", "target_key": "t",
                              "execution_unit": "u", "acceptance_version": "v1"},
            "resume_of_action_id": resume_id,
        })
        record["fields"]["action_json"] = json.dumps(envelope, ensure_ascii=False)
        record["fields"]["work_number"] = request["work_number"]
        from windows_execution_foundation.work_identity import result_metadata, validate_v3_fields
        request, request_hash = validate_codex_request(self.config, record["fields"], request_id, replay=True)
        contract = validate_v3_fields(request)
        key_hash = stable_key(request_id)
        atomic_write_json(self.foundation.inflight / f"{key_hash}.json", {
            "metadata": {"airtable_record_id": record_id, "request_hash": request_hash,
                         "work_number": request["work_number"],
                         **result_metadata({**request, **contract}),
                         "verification_spec_hash": contract["verification_spec_hash"]},
        })
        return config_path, bridge, record

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_wrong_host_rejected_before_enqueue(self, patch):
        result = ingest_queued(self.foundation, self.config, [self.record(host="wrong-host")])
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "WRONG_HOST")
        self.assertFalse(list(self.foundation.pending.glob("*.json")))

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_expired_request_rejected_before_enqueue(self, patch):
        result = ingest_queued(self.foundation, self.config, [self.record(expired=True)])
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "REQUEST_EXPIRED")
        self.assertFalse(list(self.foundation.pending.glob("*.json")))

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_duplicate_request_is_one_foundation_job(self, patch):
        record = self.record()
        first = ingest_queued(self.foundation, self.config, [record])
        second = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual((first["enqueued"], second["noop"]), (1, 1))
        self.assertEqual(len(list(self.foundation.pending.glob("*.json"))), 1)
        job = json.loads(next(self.foundation.pending.glob("*.json")).read_text())
        command, _ = self.foundation._command(job)
        self.assertEqual(command[-4:], ["--record-id", "rec1", "--request-id", "request-1"])
        self.assertEqual(job["metadata"]["airtable_record_id"], "rec1")
        self.assertTrue(job["metadata"]["request_hash"])
        self.assertEqual(job["not_before_epoch"], 0)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_codex_enqueue_keeps_airtable_queued_without_start_fields(self, patch):
        record = self.record()
        result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result["enqueued"], 1)
        job = json.loads(next(self.foundation.pending.glob("*.json")).read_text())
        self.assertEqual(job["attempt"], 0)
        self.assertEqual(record["fields"]["state"], "EF_QUEUED")
        self.assertNotIn("started", record["fields"])
        self.assertNotIn("heartbeat", record["fields"])
        patch.assert_not_called()

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_codex_noop_receipt_does_not_regress_terminal_truth(self, patch):
        record = self.record()
        _, request_hash = validate_codex_request(self.config, record["fields"], "request-1")
        atomic_write_json(self.foundation.receipt_path(stable_key("request-1")), {
            "status": "SUCCESS",
            "external_receipt": {"content": {"request_hash": request_hash}},
        })
        result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result["noop"], 1)
        patch.assert_not_called()

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_two_records_keep_distinct_record_ids_hashes_and_commands(self, patch):
        record_a = self.record(request_id="request-a")
        record_a["id"] = "recA"
        record_b = self.record(request_id="request-b")
        record_b["id"] = "recB"
        result = ingest_queued(self.foundation, self.config, [record_a, record_b])
        self.assertEqual(result["enqueued"], 2)
        jobs = [json.loads(path.read_text()) for path in self.foundation.pending.glob("*.json")]
        by_request = {job["idempotency_key"]: job for job in jobs}
        self.assertNotEqual(by_request["request-a"]["metadata"]["request_hash"],
                            by_request["request-b"]["metadata"]["request_hash"])
        for request_id, record_id in (("request-a", "recA"), ("request-b", "recB")):
            job = by_request[request_id]
            self.assertEqual(job["metadata"]["airtable_record_id"], record_id)
            command, _ = self.foundation._command(job)
            self.assertEqual(command[-4:], ["--record-id", record_id, "--request-id", request_id])

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_changed_payload_with_same_request_id_rejected(self, patch):
        record = self.record()
        ingest_queued(self.foundation, self.config, [record])
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["task_instruction"] = "Different task"
        record["fields"]["action_json"] = json.dumps(envelope)
        result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "REQUEST_ID_INPUT_MISMATCH")

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_v2_matching_work_number_passes_and_is_claimed(self, patch):
        number = "20260925-224228"
        record = self.record(schema="codex_local_job_mailbox_v2", work_number=number,
                             airtable_work_number=number)
        result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result, {"enqueued": 1, "noop": 0, "rejected": 0})
        job = json.loads(next(self.foundation.pending.glob("*.json")).read_text())
        self.assertEqual(job["metadata"]["work_number"], number)
        patch.assert_not_called()

    def test_v2_work_number_gate_rejects_before_enqueue(self):
        cases = [
            ("missing", None, None, "WORK_NUMBER_REQUIRED"),
            ("field-empty", "20260925-224228", None, "WORK_NUMBER_REQUIRED"),
            ("mismatch", "20260925-224228", "20260925-224229", "WORK_NUMBER_MISMATCH"),
            ("whitespace-mismatch", "20260925-224228", " 20260925-224228 ", "WORK_NUMBER_MISMATCH"),
            ("invalid", "2026-09-25-224228", "2026-09-25-224228", "WORK_NUMBER_INVALID"),
        ]
        for name, request_number, field_number, code in cases:
            with self.subTest(name=name), mock.patch(
                "windows_execution_foundation.airtable_local_exec._patch"
            ) as patch:
                record = self.record(request_id="request-" + name,
                                     schema="codex_local_job_mailbox_v2",
                                     work_number=request_number,
                                     airtable_work_number=field_number)
                result = ingest_queued(self.foundation, self.config, [record])
                self.assertEqual(result["rejected"], 1)
                self.assertEqual(patch.call_args.args[2]["error_code"], code)
        self.assertFalse(list(self.foundation.pending.glob("*.json")))

    def test_v2_accepts_both_lanes_and_suffix_form(self):
        for lane, number in (("A", "20260925-224228"), ("B", "20260925-224228-01")):
            with self.subTest(lane=lane):
                config = {**self.config, "lane_id": lane}
                record = self.record(request_id="request-" + lane,
                                     schema="codex_local_job_mailbox_v2",
                                     work_number=number, airtable_work_number=number)
                envelope = json.loads(record["fields"]["action_json"])
                envelope["request"]["lane"] = lane
                record["fields"]["action_json"] = json.dumps(envelope)
                request, _ = validate_codex_request(config, record["fields"], "request-" + lane)
                self.assertEqual(request["work_number"], number)

    @mock.patch("windows_execution_foundation.airtable_local_exec._patch")
    def test_v2_replay_same_number_adds_no_job_and_changed_number_rejects(self, patch):
        number = "20260925-224228"
        record = self.record(schema="codex_local_job_mailbox_v2", work_number=number,
                             airtable_work_number=number)
        first = ingest_queued(self.foundation, self.config, [record])
        second = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual((first["enqueued"], second["noop"]), (1, 1))
        self.assertEqual(len(list(self.foundation.pending.glob("*.json"))), 1)
        record["fields"]["work_number"] = "20260925-224229"
        changed = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(changed["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "WORK_NUMBER_MISMATCH")
        self.assertEqual(len(list(self.foundation.pending.glob("*.json"))), 1)

    def test_v2_result_and_foundation_receipt_preserve_work_number(self):
        number = "20260925-224228"
        config_path, bridge, record = self.execution_fixture("request-v2-result", "recV2")
        envelope = json.loads(record["fields"]["action_json"])
        envelope["schema_version"] = "codex_local_job_mailbox_v2"
        envelope["request"]["work_number"] = number
        record["fields"]["work_number"] = number
        record["fields"]["action_json"] = json.dumps(envelope)
        # Rebuild the claim using the v2 hash.
        for path in self.foundation.pending.glob("*.json"):
            path.unlink()
        record["fields"]["state"] = "EF_QUEUED"
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            ingest_queued(self.foundation, self.config, [record])
        record["fields"]["state"] = "EF_RUNNING"
        job_id = "codex-local-" + stable_key("request-v2-result")
        status_path = bridge / "runtime" / "jobs" / job_id / "status.json"

        def fake_run(*_args, **_kwargs):
            atomic_write_json(status_path, {
                "status": "VERIFIED", "reason_code": "VERIFIED",
                "codex": {"launch_count": 1, "exit_code": 0},
                "verifier": {"launch_count": 1, "exit_code": 0},
                "notification": {"state": "SENT", "attempt_count": 1, "http_status": 200},
            })
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

        output = Path(self.temporary.name) / "v2-result.json"
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
            result = execute(config_path, "recV2", "request-v2-result", output)
        self.assertEqual(result["work_number"], number)

        receipt_foundation = ExecutionFoundation({
            "state_dir": str(Path(self.temporary.name) / "receipt-state"),
            "actions": {"fixture": {"command": [sys.executable, "-c", "pass"],
                                      "allowed_args": [], "timeout_seconds": 5,
                                      "requires_network": False, "requires_remote": False}},
        })
        receipt_foundation.enqueue(action="fixture", idempotency_key="receipt-v2",
                                   metadata={"work_number": number})
        receipt = receipt_foundation.work_once()
        self.assertEqual(receipt["work_number"], number)

    def test_v2_number_change_after_claim_never_launches(self):
        number = "20260925-224228"
        config_path, _bridge, record = self.execution_fixture("request-v2-change", "recV2Change")
        envelope = json.loads(record["fields"]["action_json"])
        envelope["schema_version"] = "codex_local_job_mailbox_v2"
        envelope["request"]["work_number"] = number
        record["fields"]["work_number"] = number
        record["fields"]["action_json"] = json.dumps(envelope)
        for path in self.foundation.pending.glob("*.json"):
            path.unlink()
        record["fields"]["state"] = "EF_QUEUED"
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            ingest_queued(self.foundation, self.config, [record])
        record["fields"]["state"] = "EF_RUNNING"
        record["fields"]["work_number"] = "20260925-224229"
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["work_number"] = "20260925-224229"
        record["fields"]["action_json"] = json.dumps(envelope)
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
             mock.patch("windows_execution_foundation.codex_local_job._patch") as patch, \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run") as run:
            with self.assertRaisesRegex(LocalExecError, "WORK_NUMBER_MISMATCH"):
                execute(config_path, "recV2Change", "request-v2-change",
                        Path(self.temporary.name) / "never.json")
        run.assert_not_called()
        self.assertEqual(patch.call_args.args[2]["state"], "EF_REJECTED")
        self.assertEqual(patch.call_args.args[2]["error_code"], "WORK_NUMBER_MISMATCH")

    def test_unknown_action_still_denied(self):
        record = self.record()
        record["fields"]["action_type"] = "RUN_SHELL"
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
            result = ingest_queued(self.foundation, self.config, [record])
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "ALLOWLIST_DENIED")

    def test_replay_allows_expired_immutable_request(self):
        record = self.record(expired=True)
        request, _ = validate_codex_request(self.config, record["fields"], "request-1", replay=True)
        self.assertEqual(request["request_id"], "request-1")
        with self.assertRaisesRegex(LocalExecError, "REQUEST_EXPIRED"):
            validate_codex_request(self.config, record["fields"], "request-1")

    def test_codex_local_job_ignores_local_exec_payload(self):
        record = self.record()
        baseline, baseline_hash = validate_codex_request(
            self.config, record["fields"], "request-1"
        )
        record["fields"]["local_exec_payload"] = "not codex json"
        request, request_hash = validate_codex_request(
            self.config, record["fields"], "request-1"
        )
        self.assertEqual(request, baseline)
        self.assertEqual(request_hash, baseline_hash)

    def test_receipt_request_mismatch_fails_closed(self):
        action = self.foundation.config["actions"]["codex-local-job"]
        source = Path(self.temporary.name) / "external.json"
        atomic_write_json(source, {"request_id": "wrong"})
        action["receipt_source"] = str(source)
        action["receipt_request_id_field"] = "request_id"
        self.foundation.enqueue(action="codex-local-job", idempotency_key="request-1")
        result = self.foundation.work_once()
        self.assertEqual(result["status"], "RETRY_SPOOLED")
        self.assertFalse(self.foundation.receipt_path(stable_key("request-1")).exists())

    def test_execute_reads_only_supplied_record_and_ignores_changed_other_record(self):
        config_path, bridge, record_a = self.execution_fixture("request-a", "recA")
        other_record = self.record(request_id="request-b")
        other_record["id"] = "recB"
        other_record["fields"]["request_id"] = "overwritten-b"
        job_id = "codex-local-" + stable_key("request-a")
        status_path = bridge / "runtime" / "jobs" / job_id / "status.json"

        def fetch(_config, method, *, record_id=None, **_kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(record_id, "recA")
            return record_a

        def fake_run(*_args, **_kwargs):
            atomic_write_json(status_path, {
                "status": "VERIFIED", "reason_code": "VERIFIED",
                "codex": {"launch_count": 1}, "verifier": {"launch_count": 1},
                "notification": {"state": "SENT", "attempt_count": 1, "http_status": 200},
            })
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

        output = Path(self.temporary.name) / "record-a-result.json"
        with mock.patch("windows_execution_foundation.codex_local_job._request", side_effect=fetch), \
             mock.patch("windows_execution_foundation.codex_local_job._patch") as patch, \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
            result = execute(config_path, "recA", "request-a", output)
        self.assertEqual(result["terminal_status"], "VERIFIED")
        self.assertEqual(other_record["fields"]["request_id"], "overwritten-b")
        patch.assert_not_called()

    def test_queued_execute_patches_running_once_immediately_before_launch(self):
        config_path, bridge, record = self.execution_fixture("request-queued", "recQueued")
        record["fields"]["state"] = "EF_QUEUED"
        job_id = "codex-local-" + stable_key("request-queued")
        status_path = bridge / "runtime" / "jobs" / job_id / "status.json"

        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
             mock.patch("windows_execution_foundation.codex_local_job._patch") as patch:
            def fake_run(*_args, **_kwargs):
                self.assertEqual(patch.call_count, 1)
                self.assertEqual(
                    _kwargs["env"]["TANTAN_EXECUTION_HOST_DEVICE_ID"],
                    self.config["device_id"],
                )
                running = patch.call_args.args[2]
                self.assertEqual(running["state"], "EF_RUNNING")
                self.assertEqual(running["resolved_host"], self.config["device_id"])
                self.assertTrue(running["started"])
                self.assertTrue(running["heartbeat"])
                self.assertEqual(running["error_code"], "")
                atomic_write_json(status_path, {
                    "status": "VERIFIED", "reason_code": "VERIFIED",
                    "codex": {"launch_count": 1}, "verifier": {"launch_count": 1},
                    "notification": {"state": "SENT", "attempt_count": 1, "http_status": 200},
                })
                return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

            with mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
                execute(config_path, "recQueued", "request-queued", Path(self.temporary.name) / "queued.json")
        self.assertEqual(patch.call_count, 1)

    def test_execute_fails_closed_if_its_own_envelope_changes(self):
        config_path, _bridge, record = self.execution_fixture("request-change", "recChange")
        record["fields"]["state"] = "EF_QUEUED"
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"]["task_instruction"] = "Changed after ingest"
        record["fields"]["action_json"] = json.dumps(envelope)
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
             mock.patch("windows_execution_foundation.codex_local_job._patch") as patch, \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "REQUEST_INPUT_CHANGED"):
                execute(config_path, "recChange", "request-change", Path(self.temporary.name) / "changed.json")
        run.assert_not_called()
        patch.assert_not_called()

    def test_execute_route_fields_fail_closed(self):
        changes = {
            "schema": "EF_v9",
            "requested_host": "wrong-host",
            "action_type": "MINKUE_PUBLIC_FETCH",
            "state": "EF_SUCCEEDED",
            "request_id": "wrong-request",
        }
        for field, bad_value in changes.items():
            with self.subTest(field=field):
                request_id = "request-route-" + field
                config_path, _bridge, record = self.execution_fixture(request_id, "rec-" + field)
                record["fields"][field] = bad_value
                with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
                     mock.patch("windows_execution_foundation.codex_local_job.subprocess.run") as run:
                    with self.assertRaisesRegex(ValueError, "REQUEST_NOT_CURRENT"):
                        execute(config_path, "rec-" + field, request_id, Path(self.temporary.name) / (field + ".json"))
                run.assert_not_called()

    def test_first_failure_and_bridge_evidence_survive_foundation_result_sync(self):
        from windows_execution_foundation.airtable_local_exec import sync_results
        fixture = json.loads((Path(__file__).parent / "fixtures" / "cti_bridge_artifact_return_v01.json").read_text(encoding="utf-8"))
        scenarios = (
            ("BASELINE_RECEIPT_RUNTIME_PERMISSION_DENIED", "FAILED", "BASELINE_RECEIPT_RUNTIME"),
            ("TEST_COVERAGE_INCOMPLETE", "FAILED", "TEST_COVERAGE"),
            ("RECOVERY_BUDGET_EXHAUSTED", "STOPPED", "RECOVERY_BUDGET"),
        )
        for index, (reason, terminal_status, boundary) in enumerate(scenarios):
            with self.subTest(reason=reason, terminal_status=terminal_status):
                request_id, record_id = f"failure-{index}", f"recFailure{index}"
                config_path, bridge, record = self.execution_fixture(request_id, record_id)
                job_id = "codex-local-" + stable_key(request_id)
                extras = {
                    "execution_status": terminal_status, "first_failed_boundary": boundary,
                    "verification_reason": "FAILURE_EVIDENCE_VERIFIED" if index else "TEST_EVIDENCE_INVALID",
                    "secondary_reason": "EVIDENCE_MISSING" if index else None,
                }

                def fake_run(*_args, **kwargs):
                    self.assertEqual(kwargs["env"]["TANTAN_COMPLETION_REQUEST_ID"], request_id)
                    jobdir = bridge / "runtime" / "jobs" / job_id
                    artifact = {
                        "schema_version": "bridge_failure_evidence_v1", "source": "BRIDGE",
                        "request_id": request_id, "job_id": job_id,
                        "execution_status": extras["execution_status"],
                        "first_failed_boundary": extras["first_failed_boundary"],
                        "reason_code": reason, "fixture_secret": fixture["secret_marker"],
                    }
                    atomic_write_json(jobdir / "bridge-failure-evidence.json", artifact)
                    atomic_write_json(jobdir / "status.json", {
                        "job_id": job_id, "request_id": request_id,
                        "status": terminal_status, "reason_code": reason, **extras,
                        "failure_evidence": {"path": "bridge-failure-evidence.json", "source": "BRIDGE", "verifier_eligible": True},
                        "codex": {"launch_count": 1, "exit_code": 0},
                        "verifier": {"launch_count": 1, "exit_code": 0},
                        "notification": {"state": "NOT_CONFIGURED", "attempt_count": 0},
                    })
                    return subprocess.CompletedProcess([], 1, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

                output = Path(self.temporary.name) / f"failure-result-{index}.json"
                with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
                     mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run) as launch:
                    result = execute(config_path, record_id, request_id, output)
                launch.assert_called_once()
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual(result["terminal_status"], terminal_status)
                self.assertIn(result["terminal_status"], fixture["positive_expectations"]["terminal_statuses"])
                self.assertEqual({key: result[key] for key in extras}, extras)
                projected = result["failure_evidence"]
                artifact_path = bridge / "runtime" / "jobs" / job_id / "bridge-failure-evidence.json"
                self.assertEqual(projected["path"], str(artifact_path.resolve()))
                self.assertEqual(projected["sha256"], hashlib.sha256(artifact_path.read_bytes()).hexdigest())
                self.assertEqual(projected["locator_status"], fixture["positive_expectations"]["locator_status"])
                self.assertEqual(projected["job_id"], job_id)
                self.assertEqual(projected["request_id"], request_id)
                self.assertNotIn(fixture["secret_marker"], json.dumps(result))
                self.assertTrue(Path(projected["path"]).is_absolute())
                self.assertRegex(projected["sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(set(fixture["positive_expectations"]["legacy_keys"]).issubset(projected))
                atomic_write_json(self.foundation.receipt_path(stable_key(request_id)), {
                    "status": "SUCCESS", "external_receipt": {"content": result},
                })
                config = {**self.config, "fields": {**self.config["fields"], "evidence": "evidence", "evidence_ref": "evidence_ref"}}
                notifier = mock.Mock()
                with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
                    counts = sync_results(self.foundation, config, [record], failure_notifier=notifier)
                self.assertEqual(counts["failed"], 1)
                notifier.assert_not_called()
                self.assertEqual(notifier.call_count, fixture["positive_expectations"]["notification_added_count"])
                fields = patch.call_args.args[2]
                self.assertEqual(fields["error_code"], reason)
                self.assertEqual(fields["state"], "EF_FAILED")
                passed = json.loads(fields["evidence"])
                self.assertEqual({key: passed[key] for key in extras}, extras)
                self.assertEqual(passed["failure_evidence"], projected)
                self.assertEqual(passed["failure_evidence"]["source"], "BRIDGE")
                self.assertTrue(passed["failure_evidence"]["verifier_eligible"])

    def test_failure_evidence_locator_failures_return_unknown(self):
        root = Path(self.temporary.name) / "locator-cases"
        request_id = "fixture-request"
        fixture_data = json.loads((Path(__file__).parent / "fixtures" / "cti_bridge_artifact_return_v01.json").read_text(encoding="utf-8"))
        expected_failures = fixture_data["negative_expectations"]

        def fixture(case, *, locator="bridge-failure-evidence.json", artifact_job=None,
                    artifact=True, expected_hash=None, frozen_work=None):
            job_id = "codex-local-" + case
            jobdir = root / job_id
            status_path = jobdir / "status.json"
            status = {
                "job_id": job_id, "request_id": request_id, "status": "FAILED",
                "reason_code": "FIXTURE_FAILURE", "execution_status": "FAILED",
                "first_failed_boundary": "FIXTURE_BOUNDARY",
                "failure_evidence": {
                    "path": locator, "source": "BRIDGE", "verifier_eligible": True,
                    **({"sha256": expected_hash} if expected_hash is not None else {}),
                },
                **({"work_metadata": frozen_work} if frozen_work is not None else {}),
            }
            atomic_write_json(status_path, status)
            if artifact:
                atomic_write_json(jobdir / "bridge-failure-evidence.json", {
                    "schema_version": "bridge_failure_evidence_v1", "source": "BRIDGE",
                    "request_id": request_id, "job_id": artifact_job or job_id,
                    "reason_code": "FIXTURE_FAILURE", "execution_status": "FAILED",
                    "first_failed_boundary": "FIXTURE_BOUNDARY",
                })
            return status_path, status, job_id

        cases = []
        cases.append(("relative", *fixture("relative", locator="unproven/bridge-failure-evidence.json"), expected_failures["unproven_relative"]))
        cases.append(("wrong-job", *fixture("wrong-job", artifact_job="codex-local-other"), expected_failures["wrong_job"]))
        cases.append(("wrong-hash", *fixture("wrong-hash", expected_hash="0" * 64), expected_failures["wrong_hash"]))
        cases.append(("missing", *fixture("missing", artifact=False), expected_failures["missing_artifact"]))
        for label, status_path, status, job_id, expected in cases:
            with self.subTest(case=label):
                result = _project_failure_evidence(
                    status_path, status, job_id=job_id, request_id=request_id, work_metadata={},
                )
                self.assertEqual((result["path"], result["sha256"]), (fixture_data["unknown_locator"], fixture_data["unknown_locator"]))
                self.assertEqual(result["locator_status"], expected)

        status_path, status, job_id = fixture("permission")
        with mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError("fixture denied")):
            result = _project_failure_evidence(
                status_path, status, job_id=job_id, request_id=request_id, work_metadata={},
            )
        self.assertEqual((result["path"], result["sha256"]), (fixture_data["unknown_locator"], fixture_data["unknown_locator"]))
        self.assertEqual(result["locator_status"], expected_failures["permission_denied"])

    def test_completion_terminal_mapping_including_verifier_failure(self):
        for terminal in ("VERIFIED", "VERIFY_FAILED"):
            with self.subTest(terminal=terminal):
                request_id = "request-" + terminal.lower()
                record_id = "rec-" + terminal.lower()
                root = Path(self.temporary.name)
                bridge = root / "bridge"
                bridge.mkdir(exist_ok=True)
                (root / "state" / "local-exec-v01").mkdir(parents=True, exist_ok=True)
                configuration = {
                    "state_dir": str(root / "state"),
                    "local_exec_airtable": {**self.config, "device_id": "tantan-mf-a1c6659aabef611c"},
                    "codex_local_job": {
                        "bridge_root": str(bridge),
                        "default_workdir": str(root), "approved_roots": [str(root)],
                        "default_model": "gpt-5.6-luna", "default_reasoning": "medium",
                        "approved_models": ["gpt-5.6-luna"], "approved_reasoning": ["medium"],
                        "codex_timeout_seconds": 60, "verifier_timeout_seconds": 60,
                        "run_timeout_seconds": 150,
                    },
                }
                config_path = root / "config.json"
                config_path.write_text(json.dumps(configuration), encoding="utf-8")
                record = self.record(request_id=request_id)
                record["id"] = record_id
                # Reuse the same request snapshot so the ingested request hash matches.
                ingest_record = json.loads(json.dumps(record))
                record["fields"]["state"] = "EF_RUNNING"
                with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
                    ingest_queued(self.foundation, self.config, [ingest_record])
                job_id = "codex-local-" + stable_key(request_id)
                status_path = bridge / "runtime" / "jobs" / job_id / "status.json"

                def fake_run(*_args, **_kwargs):
                    atomic_write_json(status_path, {
                        "status": terminal, "reason_code": terminal,
                        "codex": {"launch_count": 1}, "verifier": {"launch_count": 1},
                        "notification": {"state": "SENT", "attempt_count": 1, "http_status": 200},
                    })
                    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id, "status": terminal}), stderr="")

                output = root / (terminal + ".json")
                with mock.patch("windows_execution_foundation.codex_local_job._request", return_value={"fields": record["fields"]}), \
                     mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
                    result = execute(config_path, record_id, request_id, output)
                self.assertEqual(result["terminal_status"], terminal)
                self.assertEqual(result["slack_attempt_count"], 1)
                self.assertEqual(result["slack_http_status"], 200)
                self.assertEqual(json.loads(output.read_text())["terminal_status"], terminal)

    def test_resume_exact_read_allowlist_and_deterministic_hypothesis_context(self):
        fixture = {
            "id": "recPrior",
            "fields": {
                "Action ID": "act-prior", "Request ID": "request-prior", "作業番号": "20260929-090000-01",
                "Local Exec State": "EF_FAILED", "Local Exec Error Code": "UnauthorizedAccess",
                "Autonomous Evidence": json.dumps({"first_failed_boundary": "PROMPT_FILE_READ", "detail": "denied"}),
                "Terminal Evidence": "failed before launch",
                "問題Fingerprint": "UNAUTHORIZED_ACCESS_PROMPT",
                "03｜問題": "ACL may deny the runtime input directory.",
                "03｜推奨修理": "Confirm access at the exact boundary before retry.",
                "Not Allowed": "Bearer should-never-enter",
            },
        }
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=fixture) as request:
            context = read_resume_context(self.config, "recPrior")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["record_id"], "recPrior")
        self.assertNotIn("params", request.call_args.kwargs)
        first = compose_resume_instruction(context, "Continue safely.")
        second = compose_resume_instruction(context, "Continue safely.")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("PREVIOUS_FAILURE_CONTEXT_V1\n"))
        self.assertIn('first_failed_boundary: "PROMPT_FILE_READ"', first)
        self.assertIn('problem_fingerprint: "UNAUTHORIZED_ACCESS_PROMPT"', first)
        self.assertIn("DIAGNOSTIC_HYPOTHESIS_NOT_CONFIRMED_FACT", first)
        self.assertIn("PRIOR_FAILURE_EVIDENCE_OVER_DIAGNOSTIC_HYPOTHESIS", first)
        self.assertNotIn("Not Allowed", first)
        self.assertNotIn("should-never-enter", first)

    def test_resume_record_id_and_failure_context_fail_closed(self):
        cases = (
            ("action-id", "act-prior", None),
            ("missing-id", "", None),
            ("wrong-record", "recPrior", {"id": "recOther", "fields": {"Local Exec Error Code": "FAILED"}}),
            ("missing-fields", "recPrior", {"id": "recPrior"}),
            ("no-failure-context", "recPrior", {"id": "recPrior", "fields": {"Local Exec State": "EF_FAILED"}}),
        )
        for name, record_id, response in cases:
            with self.subTest(name=name), mock.patch(
                "windows_execution_foundation.codex_local_job._request", return_value=response
            ) as request:
                with self.assertRaisesRegex(LocalExecError, "RESUME_CONTEXT_READBACK_FAILED"):
                    read_resume_context(self.config, record_id)
                if record_id.startswith("rec"):
                    request.assert_called_once_with(self.config, "GET", record_id=record_id)
                else:
                    request.assert_not_called()

    def test_resume_without_healer_still_injects_failure_and_redacts_pii_secrets(self):
        fixture = {
            "id": "recPrior",
            "fields": {
                "Local Exec State": "EF_FAILED", "Local Exec Error Code": "EVIDENCE_MISSING",
                "Autonomous Evidence": "token=topsecret owner@example.com 090-1234-5678",
            },
        }
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=fixture):
            prompt = compose_resume_instruction(read_resume_context(self.config, "recPrior"), "Original.")
        self.assertIn("HEALER_DIAGNOSIS_NOT_AVAILABLE", prompt)
        self.assertIn("EVIDENCE_MISSING", prompt)
        self.assertNotIn("topsecret", prompt)
        self.assertNotIn("owner@example.com", prompt)
        self.assertNotIn("090-1234-5678", prompt)

    def test_resume_readback_failure_never_launches_codex_and_writes_existing_result_contract(self):
        config_path, _bridge, current = self.resume_execution_fixture()
        output = Path(self.temporary.name) / "resume-failed.json"
        calls = 0

        def fetch(_config, _method, *, record_id=None, **_kwargs):
            nonlocal calls
            calls += 1
            if record_id == "recResume":
                return current
            raise LocalExecError("AIRTABLE_HTTP_403")

        with mock.patch("windows_execution_foundation.codex_local_job._request", side_effect=fetch), \
             mock.patch("windows_execution_foundation.codex_local_job._patch") as patch, \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run") as launch:
            result = execute(config_path, "recResume", "request-resume", output)
        launch.assert_not_called()
        self.assertEqual(calls, 2)
        self.assertEqual(result["reason_code"], "RESUME_CONTEXT_READBACK_FAILED")
        self.assertEqual(result["codex_launch_count"], 0)
        self.assertEqual(result["first_failed_boundary"], "RESUME_CONTEXT_READBACK")
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
        self.assertEqual(patch.call_args.args[2]["error_code"], "RESUME_CONTEXT_READBACK_FAILED")
        self.assertNotIn("resume_context", result)

    def test_resume_job_reads_referenced_row_once_and_injects_before_launch(self):
        config_path, bridge, current = self.resume_execution_fixture(
            "request-resume-ok", "recResumeOk", "recPriorOk")
        prior = {
            "id": "recPriorOk",
            "fields": {
                "Request ID": "request-prior", "作業番号": "20260929-090000-01",
                "Local Exec State": "EF_FAILED", "Local Exec Error Code": "UnauthorizedAccess",
                "Autonomous Evidence": json.dumps({"first_failed_boundary": "PROMPT_FILE_READ"}),
                "問題Fingerprint": "ACL", "03｜問題": "Access may be denied.",
                "03｜推奨修理": "Read back the exact ACL boundary.",
            },
        }
        job_id = "codex-local-" + stable_key("request-resume-ok")
        seen = []
        captured = {}

        def fetch(_config, _method, *, record_id=None, **kwargs):
            seen.append((record_id, kwargs.get("params")))
            return current if record_id == "recResumeOk" else prior

        def fake_run(command, **_kwargs):
            captured["prompt"] = Path(command[command.index("-PromptFile") + 1]).read_text(encoding="utf-8")
            atomic_write_json(bridge / "runtime" / "jobs" / job_id / "status.json", {
                "status": "VERIFIED", "reason_code": "VERIFIED",
                "codex": {"launch_count": 1}, "verifier": {"launch_count": 1},
                "notification": {"state": "NOT_CONFIGURED", "attempt_count": 0},
            })
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

        with mock.patch("windows_execution_foundation.codex_local_job._request", side_effect=fetch), \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
            execute(config_path, "recResumeOk", "request-resume-ok", Path(self.temporary.name) / "resume-ok.json")
        self.assertEqual([record_id for record_id, _ in seen], ["recResumeOk", "recPriorOk"])
        self.assertIsNone(seen[1][1])
        self.assertTrue(captured["prompt"].startswith("PREVIOUS_FAILURE_CONTEXT_V1\n"))
        self.assertTrue(captured["prompt"].endswith("Create a harmless JSON canary file."))

    def test_normal_job_keeps_prompt_byte_identical_and_has_no_extra_read(self):
        config_path, bridge, record = self.execution_fixture("request-normal", "recNormal")
        job_id = "codex-local-" + stable_key("request-normal")
        captured = {}

        def fake_run(command, **_kwargs):
            prompt_path = Path(command[command.index("-PromptFile") + 1])
            captured["prompt"] = prompt_path.read_text(encoding="utf-8")
            atomic_write_json(bridge / "runtime" / "jobs" / job_id / "status.json", {
                "status": "VERIFIED", "reason_code": "VERIFIED",
                "codex": {"launch_count": 1}, "verifier": {"launch_count": 1},
                "notification": {"state": "NOT_CONFIGURED", "attempt_count": 0},
            })
            return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job_id": job_id}), stderr="")

        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record) as request, \
             mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
            execute(config_path, "recNormal", "request-normal", Path(self.temporary.name) / "normal.json")
        request.assert_called_once()
        self.assertEqual(captured["prompt"], "Create a harmless JSON canary file.")


if __name__ == "__main__":
    unittest.main()
