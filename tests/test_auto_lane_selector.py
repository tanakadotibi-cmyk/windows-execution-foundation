from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from windows_execution_foundation.core import ExecutionFoundation, stable_key
from windows_execution_foundation.airtable_local_exec import ingest_queued
from windows_execution_foundation.lanes import (
    admit_auto_request,
    owns_record,
    select_auto_lane,
    validate_resource,
)


class AutoLaneSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shared = self.root / "shared-resource-locks"
        common = {
            "state_dir": str(self.root),
            "shared_resource_dir": str(shared),
            "worker_heartbeat_max_age_seconds": 900,
            "lanes": {"enabled": True},
            "local_exec_airtable": {
                "action_map": {"CODEX_LOCAL_JOB": "codex-local-job"},
            },
            "actions": {"codex-local-job": {}},
        }
        self.a = ExecutionFoundation({**common, "lane_id": "A"})
        self.b = ExecutionFoundation({
            **common,
            "lane_id": "B",
            "state_dir": str(self.root / "lanes" / "B"),
        })
        self.resource = {"key": "repo:windows-execution-foundation", "workdir": str(self.root)}

    @staticmethod
    def select(**overrides):
        state = {
            "a_health": "HEALTHY", "b_health": "HEALTHY",
            "a_busy": False, "b_busy": False,
            "resource_conflict": False, "b_compatible": True,
        }
        state.update(overrides)
        return select_auto_lane(**state)

    def test_explicit_a_and_b_routing_is_unchanged(self):
        def row(lane):
            return {"action_type": "CODEX_LOCAL_JOB", "action_json": json.dumps({
                "request": {"lane": lane},
            })}

        self.assertTrue(owns_record({"lane_id": "A", "fields": {
            "action_type": "action_type", "action_json": "action_json"}}, row("A")))
        self.assertFalse(owns_record({"lane_id": "B", "fields": {
            "action_type": "action_type", "action_json": "action_json"}}, row("A")))
        self.assertTrue(owns_record({"lane_id": "B", "fields": {
            "action_type": "action_type", "action_json": "action_json"}}, row("B")))
        self.assertFalse(owns_record({"lane_id": "A", "fields": {
            "action_type": "action_type", "action_json": "action_json"}}, row("B")))

    def test_auto_a_healthy_free_selects_a(self):
        result = self.select()
        self.assertEqual((result["selected_lane"], result["selection_reason"]),
                         ("A", "A_HEALTHY_FREE"))

    def test_auto_a_busy_b_healthy_compatible_selects_b(self):
        result = self.select(a_busy=True)
        self.assertEqual(result["selected_lane"], "B")
        self.assertEqual(result["selection_reason"],
                         "A_NOT_READY_B_HEALTHY_FREE_COMPATIBLE")

    def test_auto_resource_conflict_queues_without_selection(self):
        result = self.select(resource_conflict=True)
        self.assertIsNone(result["selected_lane"])
        self.assertEqual(result["selection_reason"], "RESOURCE_CONFLICT_WAIT")

    def test_auto_a_stale_b_healthy_compatible_selects_b(self):
        result = self.select(a_health="STALE")
        self.assertEqual(result["selected_lane"], "B")

    def test_auto_both_busy_queues(self):
        result = self.select(a_busy=True, b_busy=True)
        self.assertIsNone(result["selected_lane"])
        self.assertEqual(result["selection_reason"], "BOTH_BUSY_WAIT")

    def test_auto_b_incompatible_queues_and_waits_for_a(self):
        result = self.select(a_busy=True, b_compatible=False)
        self.assertIsNone(result["selected_lane"])
        self.assertEqual(result["selection_reason"], "B_INCOMPATIBLE_WAIT_A")

    def test_simultaneous_admission_persists_exactly_one_selected_owner(self):
        observed = {
            "a_health": "HEALTHY", "b_health": "HEALTHY",
            "a_busy": False, "b_busy": False,
            "resource_conflict": False, "b_compatible": True,
        }
        with mock.patch("windows_execution_foundation.lanes.inspect_auto_lane_state",
                        return_value=observed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda foundation: admit_auto_request(
                        foundation, "same-request", "same-hash", self.resource),
                    (self.a, self.b),
                ))

        self.assertEqual({item["selected_lane"] for item in results}, {"A"})
        registry = list((self.root / "shared-resource-locks" / "requests").glob("*.json"))
        self.assertEqual(len(registry), 1)
        owner = json.loads(registry[0].read_text(encoding="utf-8"))
        self.assertEqual(owner["lane_id"], "A")
        self.assertEqual(registry[0].name, stable_key("same-request") + ".json")

    def test_replay_returns_same_selection_without_another_inspection(self):
        observed = {
            "a_health": "STALE", "b_health": "HEALTHY",
            "a_busy": False, "b_busy": False,
            "resource_conflict": False, "b_compatible": True,
        }
        with mock.patch("windows_execution_foundation.lanes.inspect_auto_lane_state",
                        return_value=observed) as inspect:
            first = admit_auto_request(self.a, "replay-request", "hash", self.resource)
            replay = admit_auto_request(self.b, "replay-request", "hash", self.resource)
        self.assertEqual(first, replay)
        self.assertEqual(replay["selected_lane"], "B")
        self.assertEqual(inspect.call_count, 1)

    def test_ingress_propagates_selection_and_replay_adds_no_job(self):
        from tests.test_codex_local_job import CodexLocalJobTests

        helper = CodexLocalJobTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        foundation = helper.foundation
        foundation.config.update({
            "lane_id": "A",
            "shared_resource_dir": str(self.root / "ingress-shared"),
            "lanes": {"enabled": True},
            "local_exec_airtable": helper.config,
            "codex_local_job": {
                "default_workdir": str(self.root),
                "approved_roots": [str(self.root)],
            },
        })
        record = helper.record(request_id="auto-ingress")
        envelope = json.loads(record["fields"]["action_json"])
        envelope["request"].update({
            "lane": "AUTO",
            "resource_scope": "repo",
            "resource_key": "windows-execution-foundation",
            "workdir_hint": str(self.root),
        })
        record["fields"]["action_json"] = json.dumps(envelope)
        observed = {
            "a_health": "HEALTHY", "b_health": "HEALTHY",
            "a_busy": False, "b_busy": False,
            "resource_conflict": False, "b_compatible": True,
        }
        with mock.patch("windows_execution_foundation.lanes.inspect_auto_lane_state",
                        return_value=observed), \
             mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            first = ingest_queued(foundation, helper.config, [record])
            replay = ingest_queued(foundation, helper.config, [record])

        self.assertEqual((first["enqueued"], replay["noop"]), (1, 1))
        jobs = list(foundation.pending.glob("*.json"))
        self.assertEqual(len(jobs), 1)
        metadata = json.loads(jobs[0].read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(metadata["selected_lane"], "A")
        self.assertEqual(metadata["selection_reason"], "A_HEALTHY_FREE")
        self.assertEqual(metadata["explicit_or_auto"], "AUTO")

    def test_invalid_lane_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "LANE_INVALID"):
            validate_resource({"lane": "C"})


if __name__ == "__main__":
    unittest.main()
