from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import ingest_queued, sync_results
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, read_json, stable_key
from windows_execution_foundation.codex_local_job import execute
from windows_execution_foundation.foundation_failure_slack import _message
from windows_execution_foundation.lanes import claim_work_identity
from windows_execution_foundation.work_identity import validate_v3_fields


class WorkIdentityV3Acceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared-resource-locks"
        self.fields = {
            "request_id": "request_id", "requested_host": "requested_host",
            "action_json": "action_json", "action_type": "action_type", "state": "state",
            "schema": "schema", "finished": "finished", "resolved_host": "resolved_host",
            "heartbeat": "heartbeat", "error_code": "error_code", "started": "started",
            "work_number": "work_number", "dedup_key": "dedup_key",
            "evidence": "evidence", "evidence_ref": "evidence_ref",
        }

    def foundation(self, lane: str = "A") -> ExecutionFoundation:
        state = self.root / ("state" if lane == "A" else "state-b")
        return ExecutionFoundation({
            "state_dir": str(state), "lane_id": lane,
            "shared_resource_dir": str(self.shared),
            "codex_local_job": {"default_workdir": str(self.root), "approved_roots": [str(self.root)]},
            "actions": {"codex-local-job": {
                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                "allowed_args": [], "append_airtable_record_id": True,
                "append_idempotency_key": True, "timeout_seconds": 5,
                "requires_network": False, "requires_remote": False,
            }},
        })

    def config(self, lane: str = "A", cutoff: str | None = None) -> dict:
        config = {
            "schema_version": "EF_v0.1", "device_id": "tantan-mf-a1c6659aabef611c",
            "lane_id": lane, "action_map": {"CODEX_LOCAL_JOB": "codex-local-job"},
            "fields": dict(self.fields),
        }
        if cutoff:
            config["codex_local_job_v3_enforcement_cutoff"] = cutoff
        return config

    def record(self, request_id: str, action_id: str, *, lane: str = "A",
               work_number: str = "20260926-161800", state: str = "EF_QUEUED",
               target: str = "foundation", execution_unit: str = "phase-1",
               stage: str = "foundation-intake", schema: str = "codex_local_job_mailbox_v3",
               resume: str | None = None, spec_text: str = "Verify v3", created: str | None = None,
               include_dedup: bool = True) -> dict:
        now = datetime.now(UTC)
        request = {
            "request_id": request_id,
            "task_name": "Foundation v3 acceptance",
            "task_instruction": "Implement and verify the bounded Foundation change.",
            "verification_spec": {"version": 1, "criteria": spec_text,
                                  "required_files": [{"path": "evidence.json", "kind": "json"}]},
            "requested_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        }
        if schema in {"codex_local_job_mailbox_v2", "codex_local_job_mailbox_v3"}:
            request["work_number"] = work_number
        if schema == "codex_local_job_mailbox_v3":
            request.update({
                "display_name_ja": "Foundation受付・重複実行防止",
                "stage_key": stage,
                "completion_scope": "STEP",
                "work_identity": {
                    "project_id": "foundation-project", "scope_key": "repo",
                    "target_key": target, "execution_unit": execution_unit,
                    "acceptance_version": "v0.3",
                },
                "lane": lane, "resource_scope": "repo", "resource_key": "foundation",
            })
            if resume:
                request["resume_of_action_id"] = resume
        envelope = {"schema_version": schema, "host_device_id": self.config(lane)["device_id"],
                    "action_type": "CODEX_LOCAL_JOB", "request": request}
        fields = {
            "request_id": request_id, "requested_host": self.config(lane)["device_id"],
            "action_json": json.dumps(envelope, ensure_ascii=False),
            "action_type": "CODEX_LOCAL_JOB", "state": state, "schema": "EF_v0.1",
        }
        if "work_number" in request:
            fields["work_number"] = work_number
        if schema == "codex_local_job_mailbox_v3" and include_dedup:
            fields["dedup_key"] = validate_v3_fields(request)["work_identity_key"]
        result = {"id": action_id, "fields": fields}
        if created:
            result["createdTime"] = created
        return result

    def history(self, record: dict, state: str, *, terminal: str | None = None) -> dict:
        copied = json.loads(json.dumps(record))
        copied["fields"]["state"] = state
        if terminal:
            copied["fields"]["evidence"] = json.dumps({"terminal_status": terminal})
            copied["fields"]["evidence_ref"] = "fixture-result-ref"
        return copied

    def ingest(self, foundation, config, record, history=None):
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
            result = ingest_queued(foundation, config, [record], identity_records=history or [])
        return result, patch

    def test_00_work_identity_key_is_independent_of_display_name(self):
        first = self.record("display-one", "action-display-one")
        request = json.loads(first["fields"]["action_json"])["request"]
        first_key = validate_v3_fields(request)["work_identity_key"]
        request["display_name_ja"] = "別の表示名"
        second_key = validate_v3_fields(request)["work_identity_key"]
        self.assertEqual(first_key, second_key)

    def test_01_same_identity_lane_race_admits_exactly_one(self):
        a, b = self.foundation("A"), self.foundation("B")
        ra = self.record("request-a", "action-a", lane="A", include_dedup=False)
        rb = self.record("request-b", "action-b", lane="B", include_dedup=False)
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda args: ingest_queued(*args, identity_records=[]),
                    [(a, self.config("A"), [ra]), (b, self.config("B"), [rb])],
                ))
        self.assertEqual(sum(item["enqueued"] for item in results), 1)
        self.assertEqual(sum(item["rejected"] for item in results), 1)
        self.assertEqual(len(list(a.pending.glob("*.json"))) + len(list(b.pending.glob("*.json"))), 1)

    def test_02_active_duplicate_returns_existing_metadata(self):
        foundation = self.foundation()
        old = self.history(self.record("old", "action-old"), "EF_RUNNING")
        new = self.record("new", "action-new", work_number="20260926-161801")
        result, patch = self.ingest(foundation, self.config(), new, [old])
        self.assertEqual(result["rejected"], 1)
        fields = patch.call_args.args[2]
        evidence = json.loads(fields["evidence"])
        self.assertEqual(fields["error_code"], "DUPLICATE_WORK_ACTIVE")
        self.assertEqual((evidence["existing_action_id"], evidence["existing_work_number"]),
                         ("action-old", "20260926-161800"))
        self.assertEqual(evidence["codex_launch_count"], 0)

    def test_03_completed_same_spec_is_noop(self):
        old = self.history(self.record("old", "action-old"), "EF_SUCCEEDED", terminal="VERIFIED")
        result, patch = self.ingest(self.foundation(), self.config(),
                                    self.record("new", "action-new"), [old])
        self.assertEqual(result["rejected"], 1)
        evidence = json.loads(patch.call_args.args[2]["evidence"])
        self.assertEqual(evidence["reason_code"], "WORK_ALREADY_COMPLETE")
        self.assertEqual(evidence["existing_action_id"], "action-old")

    def test_04_failed_without_resume_is_noop(self):
        old = self.history(self.record("old", "action-old"), "EF_FAILED", terminal="FAILED")
        result, patch = self.ingest(self.foundation(), self.config(),
                                    self.record("new", "action-new"), [old])
        self.assertEqual((result["enqueued"], result["rejected"]), (0, 1))
        self.assertEqual(patch.call_args.args[2]["error_code"], "WORK_REQUIRES_RESUME")

    def test_05_valid_resume_is_candidate(self):
        old = self.history(self.record("old", "action-old"), "EF_FAILED", terminal="FAILED")
        result, _ = self.ingest(self.foundation(), self.config(),
                                self.record("new", "action-new", resume="action-old"), [old])
        self.assertEqual(result["enqueued"], 1)

    def test_06_same_identity_spec_conflict_fails_closed(self):
        old = self.history(self.record("old", "action-old", spec_text="Old spec"), "EF_FAILED", terminal="FAILED")
        result, patch = self.ingest(self.foundation(), self.config(),
                                    self.record("new", "action-new", spec_text="New spec", resume="action-old"), [old])
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "WORK_IDENTITY_SPEC_CONFLICT")

    def test_07_same_project_different_identity_is_allowed(self):
        foundation = self.foundation()
        first, _ = self.ingest(foundation, self.config(), self.record("one", "action-one", target="one"))
        second, _ = self.ingest(foundation, self.config(), self.record("two", "action-two", target="two"))
        self.assertEqual((first["enqueued"], second["enqueued"]), (1, 1))

    def test_08_same_work_number_different_stage_unit_is_allowed(self):
        foundation = self.foundation()
        first, _ = self.ingest(foundation, self.config(),
                               self.record("one", "action-one", stage="step-one", execution_unit="unit-one"))
        second, _ = self.ingest(foundation, self.config(),
                                self.record("two", "action-two", stage="step-two", execution_unit="unit-two"))
        self.assertEqual((first["enqueued"], second["enqueued"]), (1, 1))

    def test_09_v2_work_number_gate_non_regression(self):
        record = self.record("v2", "action-v2", schema="codex_local_job_mailbox_v2")
        result, _ = self.ingest(self.foundation(), self.config(), record)
        self.assertEqual(result["enqueued"], 1)

    def test_10_cutoff_unset_keeps_v1_v2_compatible(self):
        for schema in ("codex_local_job_mailbox_v1", "codex_local_job_mailbox_v2"):
            with self.subTest(schema=schema):
                result, _ = self.ingest(self.foundation(), self.config(),
                                        self.record(schema, "action-" + schema, schema=schema))
                self.assertEqual(result["enqueued"], 1)

    def test_11_post_cutoff_legacy_is_rejected_before_enqueue(self):
        cutoff = "2026-09-26T00:00:00Z"
        record = self.record("legacy", "action-legacy", schema="codex_local_job_mailbox_v2",
                             created="2026-09-26T00:00:01Z")
        result, patch = self.ingest(self.foundation(), self.config(cutoff=cutoff), record)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "WORK_IDENTITY_V3_REQUIRED")

    def test_12_missing_or_wrong_v3_contract_is_rejected(self):
        cases = (("work_number", "WORK_NUMBER_REQUIRED"),
                 ("display_name_ja", "DISPLAY_NAME_JA_REQUIRED"),
                 ("work_identity", "WORK_IDENTITY_INVALID"))
        for field, code in cases:
            with self.subTest(field=field):
                record = self.record("bad-" + field, "action-" + field)
                envelope = json.loads(record["fields"]["action_json"])
                envelope["request"].pop(field)
                record["fields"]["action_json"] = json.dumps(envelope)
                if field == "work_number":
                    record["fields"].pop("work_number")
                result, patch = self.ingest(self.foundation(), self.config(), record)
                self.assertEqual(result["rejected"], 1)
                self.assertEqual(patch.call_args.args[2]["error_code"], code)

    def test_13_same_request_replay_adds_no_job(self):
        foundation = self.foundation()
        record = self.record("replay", "action-replay")
        first, _ = self.ingest(foundation, self.config(), record)
        second, _ = self.ingest(foundation, self.config(), record)
        self.assertEqual((first["enqueued"], second["noop"]), (1, 1))
        self.assertEqual(len(list(foundation.pending.glob("*.json"))), 1)

    def failed_v3_fixture(self, request_id: str = "failure-v3"):
        foundation = self.foundation()
        record = self.record(request_id, "action-failed", state="EF_RUNNING")
        request = json.loads(record["fields"]["action_json"])["request"]
        contract = validate_v3_fields(request)
        key = stable_key(request_id)
        atomic_write_json(foundation.failed / f"{key}.json", {
            "action": "codex-local-job", "attempt": 1, "last_error": "UNMAPPED_CAUSE",
            "metadata": {
                "airtable_record_id": record["id"], "local_exec_action_type": "CODEX_LOCAL_JOB",
                "mailbox_schema": "codex_local_job_mailbox_v3",
                "work_number": request["work_number"], "display_name_ja": request["display_name_ja"],
                "stage_key": request["stage_key"], "completion_scope": request["completion_scope"],
                "work_identity_key": contract["work_identity_key"],
            },
        })
        return foundation, self.config(), record, key

    def test_14_foundation_direct_failure_slack_is_japanese_and_numbered(self):
        foundation, config, record, _ = self.failed_v3_fixture()
        notifier = mock.Mock(return_value={"state": "SENT"})
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            sync_results(foundation, config, [record], failure_notifier=notifier)
        message = _message(notifier.call_args.args[0])
        for expected in ("作業番号: 20260926-161800", "作業: Foundation受付・重複実行防止",
                         "工程:", "結果:", "次:", "たんたんの対応:", "詳細:"):
            self.assertIn(expected, message)

    def test_15_unknown_reason_is_not_creatively_translated(self):
        payload = {
            "work_id": "work", "action_type": "CODEX_LOCAL_JOB", "route": "codex-local-job",
            "terminal_state": "EF_FAILED", "error_code": "NEVER_SEEN_CODE", "attempt_count": 1,
            "host": "M720S", "work_number": "20260926-161800", "display_name_ja": "テスト作業",
            "stage_key": "unknown-stage", "completion_scope": "STEP",
        }
        message = _message(payload)
        self.assertIn("原因未分類（コード: NEVER_SEEN_CODE）", message)

    def test_16_notification_failure_never_requeues_business_job(self):
        foundation, config, record, key = self.failed_v3_fixture("notify-failure")
        notifier = mock.Mock(side_effect=RuntimeError("offline failure"))
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            sync_results(foundation, config, [record], failure_notifier=notifier)
            sync_results(foundation, config, [record], failure_notifier=notifier)
        self.assertEqual(notifier.call_count, 1)
        self.assertFalse((foundation.pending / f"{key}.json").exists())
        self.assertEqual(read_json(foundation.failed / f"{key}.json")
                         ["foundation_failure_notification"]["state"], "FAILED")

    def test_17_result_and_receipt_preserve_completion_metadata(self):
        foundation = self.foundation()
        config = self.config()
        record = self.record("metadata", "action-metadata")
        bridge = self.root / "bridge"
        bridge.mkdir()
        configuration = {
            **foundation.config,
            "local_exec_airtable": config,
            "codex_local_job": {
                "bridge_root": str(bridge), "default_workdir": str(self.root),
                "approved_roots": [str(self.root)], "default_model": "gpt-5.6-luna",
                "default_reasoning": "medium", "approved_models": ["gpt-5.6-luna"],
                "approved_reasoning": ["medium"], "codex_timeout_seconds": 60,
                "verifier_timeout_seconds": 60, "run_timeout_seconds": 150,
            },
        }
        config_path = self.root / "v3-config.json"
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            accepted = ingest_queued(foundation, config, [record], identity_records=[])
        self.assertEqual(accepted["enqueued"], 1)
        self.assertIsNotNone(foundation._claim())
        record["fields"]["state"] = "EF_RUNNING"
        job_id = "codex-local-" + stable_key("metadata")
        status_path = bridge / "runtime" / "jobs" / job_id / "status.json"

        def fake_run(*_args, **kwargs):
            environment = kwargs["env"]
            self.assertEqual(environment["TANTAN_COMPLETION_SCOPE"], "STEP")
            self.assertEqual(environment["TANTAN_WORK_NUMBER"], "20260926-161800")
            atomic_write_json(status_path, {
                "status": "VERIFIED", "reason_code": "VERIFIED",
                "codex": {"launch_count": 1, "exit_code": 0},
                "verifier": {"launch_count": 1, "exit_code": 0},
                "notification": {"state": "SENT", "attempt_count": 1},
            })
            return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "job_id": job_id}), "")

        output = self.root / "v3-result.json"
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
                mock.patch("windows_execution_foundation.codex_local_job.subprocess.run", side_effect=fake_run):
            result = execute(config_path, record["id"], "metadata", output)
        persisted = json.loads(output.read_text(encoding="utf-8"))
        for field in ("work_number", "work_identity_key", "display_name_ja", "stage_key", "completion_scope"):
            self.assertEqual(result[field], persisted[field])
        self.assertEqual(result["completion_scope"], "STEP")

        receipt_foundation = ExecutionFoundation({
            "state_dir": str(self.root / "receipt-state"),
            "actions": {"fixture": {"command": [sys.executable, "-c", "pass"],
                                       "allowed_args": [], "timeout_seconds": 5,
                                       "requires_network": False, "requires_remote": False}},
        })
        receipt_foundation.enqueue(action="fixture", idempotency_key="receipt-v3",
                                   metadata={field: result[field] for field in (
                                       "work_number", "work_identity_key", "display_name_ja",
                                       "stage_key", "completion_scope")})
        receipt = receipt_foundation.work_once()
        self.assertEqual(receipt["completion_scope"], "STEP")
        self.assertEqual(receipt["work_identity_key"], result["work_identity_key"])

    def test_18_pre_cutoff_legacy_remains_compatible(self):
        cutoff = "2026-09-26T00:00:00Z"
        record = self.record("old-v2", "action-old-v2", schema="codex_local_job_mailbox_v2",
                             created="2026-09-25T23:59:59Z")
        result, _ = self.ingest(self.foundation(), self.config(cutoff=cutoff), record)
        self.assertEqual(result["enqueued"], 1)

    def test_19_configured_cutoff_without_source_timestamp_fails_closed(self):
        cutoff = "2026-09-26T00:00:00Z"
        record = self.record("unknown-created", "action-unknown", schema="codex_local_job_mailbox_v2")
        result, patch = self.ingest(self.foundation(), self.config(cutoff=cutoff), record)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(patch.call_args.args[2]["error_code"], "LEGACY_CUTOFF_SOURCE_UNAVAILABLE")

    def _claim_fixture(self, terminal_state: str | None, *, history_action: str = "action-old",
                       history_spec: str = "Verify v3"):
        foundation = self.foundation()
        old = self.record("old", "action-old")
        old_request = json.loads(old["fields"]["action_json"])["request"]
        old_contract = validate_v3_fields(old_request)
        registry_path = self.shared / "work-identities" / f"{stable_key(old_contract['work_identity_key'])}.json"
        atomic_write_json(registry_path, {
            **old_contract, "action_id": "action-old", "request_id": "old",
            "work_number": old_request["work_number"], "identity_state": "ACTIVE",
        })
        history = []
        if terminal_state:
            prior = self.record("old", history_action, spec_text=history_spec)
            prior_request = json.loads(prior["fields"]["action_json"])["request"]
            history.append({
                **validate_v3_fields(prior_request), "action_id": history_action,
                "request_id": "old", "work_number": prior_request["work_number"],
                "identity_state": terminal_state, "result_ref": "fixture-result-ref",
            })
        resumed = self.record("resume", "action-resume", resume="action-old")
        resumed_request = json.loads(resumed["fields"]["action_json"])["request"]
        candidate = {
            **validate_v3_fields(resumed_request), "action_id": "action-resume",
            "request_id": "resume", "work_number": resumed_request["work_number"],
        }
        return foundation, registry_path, candidate, history

    def test_20_stale_active_exact_failed_reconciles_then_allows_resume(self):
        foundation, path, candidate, history = self._claim_fixture("FAILED")
        writes = []
        real_write = atomic_write_json

        def capture(target, content):
            writes.append(json.loads(json.dumps(content)))
            real_write(target, content)

        with mock.patch("windows_execution_foundation.lanes.atomic_write_json", side_effect=capture):
            decision = claim_work_identity(foundation, candidate, history)
        self.assertTrue(decision["allowed"])
        self.assertEqual(writes[0]["identity_state"], "FAILED")
        self.assertEqual(read_json(path)["action_id"], "action-resume")
        self.assertEqual(read_json(path)["identity_state"], "ACTIVE")

    def test_21_stale_active_exact_verified_is_preserved_complete(self):
        foundation, path, candidate, history = self._claim_fixture("VERIFIED")
        decision = claim_work_identity(foundation, candidate, history)
        self.assertEqual(decision["code"], "WORK_ALREADY_COMPLETE")
        self.assertEqual(read_json(path)["identity_state"], "VERIFIED")
        self.assertEqual(read_json(path)["result_ref"], "fixture-result-ref")

    def test_22_true_active_without_terminal_history_stays_duplicate(self):
        foundation, path, candidate, history = self._claim_fixture(None)
        decision = claim_work_identity(foundation, candidate, history)
        self.assertEqual(decision["code"], "DUPLICATE_WORK_ACTIVE")
        self.assertEqual(read_json(path)["identity_state"], "ACTIVE")

    def test_23_exact_action_spec_conflict_is_not_reconciled(self):
        foundation, path, candidate, history = self._claim_fixture("FAILED", history_spec="Changed spec")
        decision = claim_work_identity(foundation, candidate, history)
        self.assertEqual(decision["code"], "WORK_IDENTITY_SPEC_CONFLICT")
        self.assertEqual(read_json(path)["identity_state"], "ACTIVE")

    def test_24_wrong_action_terminal_history_does_not_reconcile(self):
        foundation, path, candidate, history = self._claim_fixture("FAILED", history_action="action-other")
        decision = claim_work_identity(foundation, candidate, history)
        self.assertEqual(decision["code"], "DUPLICATE_WORK_ACTIVE")
        self.assertEqual(read_json(path)["identity_state"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
