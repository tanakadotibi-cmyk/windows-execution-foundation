from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import run_autonomous_repair_controller
from windows_execution_foundation.core import ExecutionFoundation
from windows_execution_foundation.work_identity import validate_v3_fields


FIELDS = {
    "request_id": "request_id", "requested_host": "requested_host",
    "schema": "schema", "action_type": "action_type", "state": "state",
    "action_json": "action_json", "work_number": "work_number",
    "dedup_key": "dedup_key", "evidence": "evidence", "error_code": "error_code",
}


class AutonomousRepairControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
        self.foundation = ExecutionFoundation({
            "state_dir": str(root / "state"),
            "codex_local_job": {
                "default_workdir": str(root), "approved_roots": [str(root)],
            },
            "actions": {},
        })
        self.config = {
            "autonomous_repair_controller_v1_enabled": True,
            "schema_version": "EF_v0.1", "device_id": "tantan-mf-a1c6659aabef611c",
            "base_id": "appTEST", "table_id": "tblTEST", "fields": FIELDS,
        }
        self.record_id = "recParent1234567890"
        self.request = {
            "request_id": "parent-request", "work_number": "20261001-120000-01",
            "task_name": "Repair local evidence", "task_instruction": "Repair the local repo evidence files.",
            "verification_spec": {"version": 1, "criteria": "Evidence is complete",
                                  "required_files": [{"path": "evidence.json", "kind": "json"}]},
            "requested_at": "2026-10-01T03:00:00Z", "expires_at": "2026-10-01T04:00:00Z",
            "display_name_ja": "証拠修理", "stage_key": "repair", "completion_scope": "STEP",
            "work_identity": {"project_id": "p", "scope_key": "s", "target_key": "t",
                              "execution_unit": "u", "acceptance_version": "v1"},
            "lane": "A", "resource_scope": "repo", "resource_key": "fixture",
        }
        self.envelope = {"schema_version": "codex_local_job_mailbox_v3",
                         "host_device_id": self.config["device_id"],
                         "action_type": "CODEX_LOCAL_JOB", "request": self.request}

    def machine(self, *, reason="EVIDENCE_MISSING", terminal="VERIFY_FAILED", request=None):
        envelope = {**self.envelope, "request": copy.deepcopy(request or self.request)}
        return {"id": self.record_id, "fields": {
            "request_id": envelope["request"]["request_id"],
            "requested_host": self.config["device_id"], "schema": "EF_v0.1",
            "action_type": "CODEX_LOCAL_JOB", "state": "EF_FAILED",
            "action_json": json.dumps(envelope, ensure_ascii=False),
            "work_number": envelope["request"]["work_number"],
            "evidence": json.dumps({"terminal_status": terminal, "reason_code": reason,
                                    "first_failed_boundary": "EVIDENCE_VERIFY"}),
            "error_code": reason,
        }}

    def named(self, *, reason="EVIDENCE_MISSING", fingerprint="fp-same"):
        return {"id": self.record_id, "fields": {
            "Local Exec State": "EF_FAILED", "Local Exec Error Code": reason,
            "Autonomous Evidence": json.dumps({"terminal_status": "VERIFY_FAILED",
                                               "reason_code": reason,
                                               "first_failed_boundary": "EVIDENCE_VERIFY"}),
            "Terminal Evidence": "verifier failed", "問題Fingerprint": fingerprint,
            "03｜問題": "Evidence is incomplete.", "03｜推奨修理": "Complete local evidence.",
        }}

    def invoke(self, machine=None, named=None, history=None):
        machine = machine or self.machine()
        named = named or self.named()
        posts = []

        def request(_config, method, **kwargs):
            if method == "GET":
                return machine if kwargs["params"]["returnFieldsByFieldId"] == "true" else named
            posts.append(kwargs["body"])
            return {"records": [{"id": "recSuccessor1234567"}]}

        with mock.patch("windows_execution_foundation.airtable_local_exec._request", side_effect=request), \
             mock.patch("windows_execution_foundation.airtable_local_exec.records_by_work_identity_key",
                        return_value=history or []):
            result = run_autonomous_repair_controller(self.foundation, self.config, [machine])
        return result, posts

    def test_allowlisted_failure_creates_exactly_one_preserving_identity_and_spec(self):
        result, posts = self.invoke()
        self.assertEqual(result, {"created": 1, "skipped": 0, "errors": 0})
        self.assertEqual(len(posts), 1)
        fields = posts[0]["records"][0]["fields"]
        successor = json.loads(fields["action_json"])["request"]
        self.assertEqual(successor["resume_of_action_id"], self.record_id)
        self.assertEqual(successor["work_identity"], self.request["work_identity"])
        self.assertEqual(successor["verification_spec"], self.request["verification_spec"])
        self.assertNotEqual(successor["request_id"], self.request["request_id"])
        self.assertNotEqual(successor["work_number"], self.request["work_number"])
        self.assertEqual(fields["state"], "EF_QUEUED")
        self.assertEqual(fields["dedup_key"], validate_v3_fields(self.request)["work_identity_key"])

    def test_same_input_replay_and_same_fingerprint_create_no_additional_successor(self):
        first, posts = self.invoke()
        fields = posts[0]["records"][0]["fields"]
        history = [{"id": "recSuccessor1234567", "fields": fields}]
        second, second_posts = self.invoke(history=history)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second, {"created": 0, "skipped": 1, "errors": 0})
        self.assertEqual(second_posts, [])

    def test_human_unknown_verified_external_and_failed_successor_never_create(self):
        cases = []
        cases.append(("auth", self.machine(reason="AUTH_REQUIRED"), self.named(reason="AUTH_REQUIRED")))
        cases.append(("unknown", self.machine(reason="UNKNOWN"), self.named(reason="UNKNOWN")))
        cases.append(("verified", self.machine(reason="EVIDENCE_MISSING", terminal="VERIFIED"), self.named()))
        external = copy.deepcopy(self.request)
        external["task_instruction"] = "Send a customer email after repairing evidence."
        cases.append(("external", self.machine(request=external), self.named()))
        resumed = copy.deepcopy(self.request)
        resumed["resume_of_action_id"] = "recRoot123456789012"
        cases.append(("failed-successor", self.machine(request=resumed), self.named()))
        for name, machine, named in cases:
            with self.subTest(name=name):
                result, posts = self.invoke(machine=machine, named=named)
                self.assertEqual(result["created"], 0)
                self.assertEqual(posts, [])

    def test_disabled_controller_is_noop(self):
        self.config["autonomous_repair_controller_v1_enabled"] = False
        result = run_autonomous_repair_controller(self.foundation, self.config, [self.machine()])
        self.assertEqual(result, {"created": 0, "skipped": 0, "errors": 0})


if __name__ == "__main__":
    unittest.main()
