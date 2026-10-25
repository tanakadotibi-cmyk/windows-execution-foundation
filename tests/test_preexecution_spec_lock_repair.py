from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import (
    LocalExecError,
    ingest_queued,
    validate_verification_spec,
)
from windows_execution_foundation.codex_local_job import execute
from windows_execution_foundation.core import ExecutionFoundation, FoundationError, atomic_write_json


class PreexecutionSpecLockRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fields = {key: key for key in (
            "request_id", "requested_host", "action_json", "action_type", "state", "schema",
            "finished", "resolved_host", "heartbeat", "error_code", "started",
        )}
        self.airtable = {
            "schema_version": "EF_v0.1",
            "device_id": "tantan-mf-a1c6659aabef611c",
            "lane_id": "A",
            "action_map": {"CODEX_LOCAL_JOB": "fixture"},
            "fields": self.fields,
        }

    def foundation(self, *, shared: bool = False) -> ExecutionFoundation:
        config = {
            "state_dir": str(self.root / ("state-shared" if shared else "state")),
            "lane_id": "A",
            "max_attempts": 3,
            "retry_base_seconds": 0,
            "actions": {"fixture": {
                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                "allowed_args": [],
                "timeout_seconds": 5,
                "requires_network": False,
                "requires_remote": False,
            }},
            "codex_local_job": {
                "default_workdir": str(self.root),
                "approved_roots": [str(self.root)],
            },
        }
        if shared:
            config["shared_resource_dir"] = str(self.root / "shared")
        return ExecutionFoundation(config)

    def record(self, request_id: str, spec: object) -> dict:
        now = datetime.now(UTC)
        request = {
            "request_id": request_id,
            "task_name": "Isolated repair fixture",
            "task_instruction": "Run only an isolated local fixture.",
            "verification_spec": spec,
            "requested_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "resource_scope": "global",
            "resource_key": "codex-process-lifecycle",
        }
        envelope = {
            "schema_version": "codex_local_job_mailbox_v1",
            "host_device_id": self.airtable["device_id"],
            "action_type": "CODEX_LOCAL_JOB",
            "request": request,
        }
        return {"id": "rec-" + request_id, "fields": {
            "request_id": request_id,
            "requested_host": self.airtable["device_id"],
            "action_json": json.dumps(envelope),
            "action_type": "CODEX_LOCAL_JOB",
            "state": "EF_QUEUED",
            "schema": "EF_v0.1",
        }}

    @staticmethod
    def valid_spec(path: str = "artifacts/result.json") -> dict:
        return {
            "version": 1,
            "criteria": "Verify the isolated result.",
            "required_files": [{"path": path, "kind": "json"}],
        }

    def test_invalid_specs_are_rejected_before_enqueue_claim_lease_or_child(self):
        cases = {
            "empty": {},
            "missing": {"version": 1, "criteria": "x"},
            "empty-files": {"version": 1, "criteria": "x", "required_files": []},
            "wrong-files-type": {"version": 1, "criteria": "x", "required_files": {}},
            "wrong-item-type": {"version": 1, "criteria": "x", "required_files": ["x"]},
            "wrong-kind": {"version": 1, "criteria": "x", "required_files": [{"path": "x", "kind": "binary"}]},
            "unsafe-path": {"version": 1, "criteria": "x", "required_files": [{"path": "../x", "kind": "text"}]},
        }
        for name, spec in cases.items():
            with self.subTest(name=name):
                foundation = self.foundation(shared=True)
                record = self.record("bad-" + name, spec)
                with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch, \
                        mock.patch.object(foundation, "enqueue") as enqueue, \
                        mock.patch("windows_execution_foundation.lanes.acquire_resource") as acquire, \
                        mock.patch("windows_execution_foundation.lanes.claim_work_identity") as identity, \
                        mock.patch("windows_execution_foundation.core.subprocess.run") as child:
                    result = ingest_queued(foundation, self.airtable, [record])
                self.assertEqual(result, {"enqueued": 0, "noop": 0, "rejected": 1})
                self.assertEqual(patch.call_args.args[2]["error_code"], "VERIFICATION_SPEC_INVALID")
                enqueue.assert_not_called()
                acquire.assert_not_called()
                identity.assert_not_called()
                child.assert_not_called()
                self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))

    def test_valid_future_artifact_is_allowed_and_spec_is_normalized_deepcopy(self):
        source = self.valid_spec("artifacts\\future.json")
        normalized = validate_verification_spec(source)
        self.assertEqual(normalized["required_files"][0]["path"], "artifacts/future.json")
        self.assertEqual(source["required_files"][0]["path"], "artifacts\\future.json")
        self.assertFalse((self.root / "artifacts" / "future.json").exists())
        foundation = self.foundation()
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            result = ingest_queued(foundation, self.airtable, [self.record("future", source)])
        self.assertEqual(result["enqueued"], 1)

    def test_mailbox_v3_cutoff_rejects_malformed_specs_before_any_admission(self):
        now = datetime.now(UTC)
        airtable = {
            **self.airtable,
            "codex_local_job_v3_enforcement_cutoff": "2026-09-26T11:59:40Z",
            "fields": {**self.fields, "work_number": "work_number", "dedup_key": "dedup_key"},
        }

        def record(request_id: str, work_number: str, spec: dict) -> dict:
            request = {
                "request_id": request_id,
                "work_number": work_number,
                "task_name": "Isolated mailbox v3 fixture",
                "task_instruction": "Run only an isolated local fixture.",
                "verification_spec": spec,
                "requested_at": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "display_name_ja": "隔離受入",
                "stage_key": "spec-lock-closeout",
                "completion_scope": "STEP",
                "work_identity": {
                    "project_id": "foundation",
                    "scope_key": "repo",
                    "target_key": request_id,
                    "execution_unit": "mailbox-v3-intake",
                    "acceptance_version": "v2",
                },
                "resource_scope": "global",
                "resource_key": "spec-lock-closeout",
            }
            envelope = {
                "schema_version": "codex_local_job_mailbox_v3",
                "host_device_id": airtable["device_id"],
                "action_type": "CODEX_LOCAL_JOB",
                "request": request,
            }
            return {
                "id": "rec-" + request_id,
                "createdTime": now.isoformat(),
                "fields": {
                    "request_id": request_id,
                    "requested_host": airtable["device_id"],
                    "action_json": json.dumps(envelope, ensure_ascii=False),
                    "action_type": "CODEX_LOCAL_JOB",
                    "state": "EF_QUEUED",
                    "schema": "EF_v0.1",
                    "work_number": work_number,
                },
            }

        malformed = {
            "empty-required-files": {"version": 1, "criteria": "x", "required_files": []},
            "missing-required-files": {"version": 1, "criteria": "x"},
            "kind-list": {"version": 1, "criteria": "x", "required_files": [{"path": "x.json", "kind": []}]},
            "kind-dict": {"version": 1, "criteria": "x", "required_files": [{"path": "x.json", "kind": {}}]},
            "explicit-null-output": {
                "version": 1,
                "criteria": "x",
                "required_files": [{"path": "x.json", "kind": "json"}],
                "required_output_contains": None,
            },
        }
        for index, (name, spec) in enumerate(malformed.items(), 1):
            with self.subTest(name=name):
                foundation = self.foundation(shared=True)
                with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch, \
                        mock.patch.object(foundation, "enqueue") as enqueue, \
                        mock.patch("windows_execution_foundation.lanes.admit_request") as admit, \
                        mock.patch("windows_execution_foundation.lanes.claim_work_identity") as claim, \
                        mock.patch("windows_execution_foundation.core.subprocess.run") as child:
                    result = ingest_queued(
                        foundation,
                        airtable,
                        [record(f"bad-v3-{index}", f"20260927-18000{index}-01", spec)],
                        identity_records=[],
                    )
                self.assertEqual(result, {"enqueued": 0, "noop": 0, "rejected": 1})
                self.assertEqual(patch.call_args.args[2]["error_code"], "VERIFICATION_SPEC_INVALID")
                admit.assert_not_called()
                claim.assert_not_called()
                enqueue.assert_not_called()
                child.assert_not_called()
                self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))

        valid_foundation = self.foundation(shared=True)
        future = self.root / "future" / "mailbox-v3-result.json"
        self.assertFalse(future.exists())
        valid_spec = {
            "version": 1,
            "criteria": "Verify the future output.",
            "required_files": [{"path": "future/mailbox-v3-result.json", "kind": "json"}],
        }
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            result = ingest_queued(
                valid_foundation,
                airtable,
                [record("valid-v3", "20260927-180100-01", valid_spec)],
                identity_records=[],
            )
        self.assertEqual(result, {"enqueued": 1, "noop": 0, "rejected": 0})
        self.assertEqual(len(list(valid_foundation.pending.glob("*.json"))), 1)
        self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))
        self.assertFalse(future.exists())

    def test_direct_execute_rejects_invalid_spec_before_bridge_child(self):
        bridge = self.root / "bridge"
        bridge.mkdir()
        configuration = {
            "state_dir": str(self.root / "direct-state"),
            "local_exec_airtable": self.airtable,
            "codex_local_job": {
                "bridge_root": str(bridge),
                "default_workdir": str(self.root),
                "approved_roots": [str(self.root)],
                "default_model": "gpt-5.6-luna",
                "default_reasoning": "medium",
                "approved_models": ["gpt-5.6-luna"],
                "approved_reasoning": ["medium"],
                "codex_timeout_seconds": 60,
                "verifier_timeout_seconds": 60,
                "run_timeout_seconds": 150,
            },
        }
        config_path = self.root / "direct-config.json"
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        record = self.record("direct-invalid", {"criteria": "x", "required_files": []})
        record["fields"]["state"] = "EF_RUNNING"
        with mock.patch("windows_execution_foundation.codex_local_job._request", return_value=record), \
                mock.patch("windows_execution_foundation.codex_local_job.subprocess.run") as child:
            with self.assertRaisesRegex(LocalExecError, "VERIFICATION_SPEC_INVALID"):
                execute(config_path, record["id"], "direct-invalid", self.root / "never.json")
        child.assert_not_called()

    def test_repeated_invalid_input_leaves_no_lease_then_valid_job_progresses(self):
        foundation = self.foundation(shared=True)
        invalid = self.record("repeat", {"version": 1, "criteria": "x", "required_files": []})
        for _ in range(2):
            with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
                self.assertEqual(ingest_queued(foundation, self.airtable, [invalid])["rejected"], 1)
            self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))
            self.assertFalse(list(foundation.pending.glob("*.json")))
        valid = self.record("repeat", self.valid_spec())
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            self.assertEqual(ingest_queued(foundation, self.airtable, [valid])["enqueued"], 1)
        self.assertEqual(foundation.work_once()["status"], "SUCCESS")
        self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))

    def test_confirmed_prelaunch_exception_releases_exact_own_lease(self):
        foundation = self.foundation(shared=True)
        foundation.enqueue(
            action="fixture", idempotency_key="prelaunch", args=["not-allowed"],
            metadata={"resource": {"key": "repo:prelaunch", "workdir": str(self.root)}},
        )
        result = foundation.work_once()
        self.assertEqual(result["status"], "RETRY_SPOOLED")
        self.assertFalse(list((self.root / "shared" / "active").glob("*.json")))

    def test_ambiguous_spawn_and_kill_unconfirmed_retain_lease(self):
        for request_id, failure in (
            ("spawn-ambiguous", OSError("spawn outcome unknown")),
            ("kill-unconfirmed", subprocess.TimeoutExpired(["fixture"], 1)),
        ):
            with self.subTest(request_id=request_id):
                foundation = self.foundation(shared=True)
                foundation.enqueue(
                    action="fixture", idempotency_key=request_id,
                    metadata={"resource": {"key": "repo:" + request_id, "workdir": None}},
                )
                with mock.patch("windows_execution_foundation.core.subprocess.run", side_effect=failure):
                    result = foundation.work_once()
                self.assertEqual(result["status"], "FAILED_REQUEUE_RELEASE")
                self.assertEqual(len(list((self.root / "shared" / "active").glob("*.json"))), 1)
                self.assertEqual(len(list(foundation.failed.glob("*.json"))), 1)
                # Each subtest uses the same temp root; remove only its isolated
                # fixture state before constructing the next Foundation.
                if request_id == "spawn-ambiguous":
                    for path in (self.root / "shared" / "active").glob("*.json"):
                        path.unlink()
                    for path in foundation.failed.glob("*.json"):
                        path.unlink()

    def test_owner_or_token_mismatch_is_never_released(self):
        foundation = self.foundation(shared=True)
        foundation.enqueue(
            action="fixture", idempotency_key="owner-mismatch", args=["not-allowed"],
            metadata={"resource": {"key": "repo:owner", "workdir": None}},
        )

        def tamper_then_fail(job):
            lease = job["resource_lease"]
            active = self.root / "shared" / "active" / (lease["token"] + ".json")
            atomic_write_json(active, {**lease, "worker_pid": lease["worker_pid"] + 1})
            raise FoundationError("ARG_NOT_ALLOWED")

        with mock.patch.object(foundation, "_command", side_effect=tamper_then_fail):
            result = foundation.work_once()
        self.assertEqual(result["status"], "FAILED_REQUEUE_RELEASE")
        self.assertTrue(result["error"].startswith("RESOURCE_RELEASE_FAILED:"))
        self.assertEqual(len(list((self.root / "shared" / "active").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
