from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.airtable_local_exec import ingest_queued
from windows_execution_foundation.cityheaven_reservation import (
    ACTION_SCHEMA,
    ACTION_TYPE,
    CityHeavenReservationError,
    TaskScheduler,
    schedule,
    validate_reservation_record,
)
from windows_execution_foundation.cityheaven_policy import (
    CANONICAL_EVENT_ID,
    CANONICAL_VERSION,
    EVENT_FIELD_ID,
    POLICY_SCHEMA,
    POLICY_BASE_ID,
    POLICY_TABLE_ID,
    VERIFICATION_FIELD_ID,
    VERSION_FIELD_ID,
    CityHeavenPolicyError,
    load_canonical_policy,
    validate_policy,
)
from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, stable_key


class FakeScheduler:
    def __init__(self):
        self.tasks: set[str] = set()
        self.created = 0
        self.deleted = 0

    def exists(self, task_name: str) -> bool:
        return task_name in self.tasks

    def create(self, task_name: str, xml_path: Path) -> None:
        self.created += 1
        self.tasks.add(task_name)
        self.xml = xml_path.read_text(encoding="utf-16")

    def delete(self, task_name: str) -> bool:
        self.deleted += 1
        self.tasks.discard(task_name)
        return True


class CityHeavenReservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fields = {
            "request_id": "request_id", "action_json": "action_json",
            "local_exec_payload": "local_exec_payload",
            "requested_host": "requested_host", "resolved_host": "resolved_host",
            "schema": "schema", "action_type": "action_type", "state": "state",
            "error_code": "error_code", "finished": "finished", "started": "started",
            "heartbeat": "heartbeat", "evidence": "evidence", "evidence_ref": "evidence_ref",
            "due_date": "due_date",
        }
        self.local = {
            "schema_version": "EF_v0.1", "device_id": "tantan-mf-a1c6659aabef611c",
            "action_map": {ACTION_TYPE: "cityheaven-reservation-schedule"},
            "fields": self.fields,
        }
        self.policy = {
            "schema_version": POLICY_SCHEMA,
            "event_id": CANONICAL_EVENT_ID,
            "version": CANONICAL_VERSION,
            "rules": {
                "message_send_enabled": False,
                "course_authority": "CITYHEAVEN_RESERVATION_DETAIL",
                "payload_course_is_hint_only": True,
                "unknown_course_must_fail_closed": True,
                "exact_message_match_required": True,
            },
            "template_a": {
                "match_type": "course_exact", "course_exact": "A course", "text": "canonical",
            },
            "template_b": {
                "match_type": "fallback_after_source_backed_course", "text": "fallback",
            },
        }
        self.foundation = ExecutionFoundation({
            "state_dir": str(self.root / "state"),
            "actions": {"cityheaven-reservation-schedule": {
                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                "allowed_args": [], "append_idempotency_key": True,
                "timeout_seconds": 5, "requires_network": False, "requires_remote": False,
            }},
        })

    def record(self, *, request_id: str = "reservation-hmac-1", state: str = "EF_QUEUED",
               host: str = "tantan-mf-a1c6659aabef611c", due: datetime | None = None,
               changes: dict | None = None) -> dict:
        due = due or datetime.now(UTC) + timedelta(hours=1)
        action = {
            "schema_version": ACTION_SCHEMA, "reservation_key": request_id,
            "reservation_at": due.isoformat(), "cast": "CAST-A", "course": "COURSE-A",
        }
        action.update(changes or {})
        return {"id": "rec1", "fields": {
            "request_id": request_id, "requested_host": host, "schema": "EF_v0.1",
            "action_type": ACTION_TYPE, "state": state, "due_date": due.isoformat(),
            "action_json": json.dumps(action),
        }}

    def write_foundation_claim(self, record: dict, request_id: str = "reservation-hmac-1") -> str:
        _, request_hash = validate_reservation_record(
            self.local, record["fields"], request_id,
            expected_state="EF_RUNNING", allow_past=True,
        )
        atomic_write_json(self.foundation.inflight / f"{stable_key(request_id)}.json", {
            "action": "cityheaven-reservation-schedule",
            "idempotency_key": request_id,
            "metadata": {
                "local_exec_action_type": ACTION_TYPE,
                "request_hash": request_hash,
            },
        })
        return request_hash

    def test_valid_contract_enqueues_and_preserves_request_hash(self):
        due = datetime.now(UTC) + timedelta(hours=1)
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
            result = ingest_queued(self.foundation, self.local, [self.record(due=due)])
        self.assertEqual(result, {"enqueued": 1, "noop": 0, "rejected": 0})
        job = json.loads(next(self.foundation.pending.glob("*.json")).read_text())
        self.assertEqual(job["metadata"]["local_exec_action_type"], ACTION_TYPE)
        self.assertEqual(len(job["metadata"]["request_hash"]), 64)
        self.assertAlmostEqual(job["not_before_epoch"], due.timestamp(), places=3)
        self.assertEqual(patch.call_args.args[2]["state"], "EF_RUNNING")

    def test_local_exec_payload_is_preferred_and_human_action_is_preserved(self):
        record = self.record()
        payload = record["fields"]["action_json"]
        record["fields"]["action_json"] = "CityHeaven予約メール受信｜10分後確認待ち"
        record["fields"]["local_exec_payload"] = payload

        action, first_hash = validate_reservation_record(
            self.local, record["fields"], "reservation-hmac-1"
        )
        record["fields"]["action_json"] = "人間向け表示を変更しても機械payloadは不変"
        _, second_hash = validate_reservation_record(
            self.local, record["fields"], "reservation-hmac-1"
        )

        self.assertEqual(action["reservation_key"], "reservation-hmac-1")
        self.assertEqual(first_hash, second_hash)

    def test_empty_or_missing_local_exec_payload_falls_back_to_action(self):
        for payload in (None, "", "   "):
            with self.subTest(payload=payload):
                record = self.record()
                if payload is not None:
                    record["fields"]["local_exec_payload"] = payload
                action, request_hash = validate_reservation_record(
                    self.local, record["fields"], "reservation-hmac-1"
                )
                self.assertEqual(action["reservation_key"], "reservation-hmac-1")
                self.assertEqual(len(request_hash), 64)

    def test_pii_guard_applies_to_local_exec_payload(self):
        record = self.record(changes={"phone": "09000000000"})
        record["fields"]["local_exec_payload"] = record["fields"]["action_json"]
        record["fields"]["action_json"] = "CityHeaven予約メール受信｜10分後確認待ち"
        with self.assertRaisesRegex(CityHeavenReservationError, "PII_KEY_FORBIDDEN"):
            validate_reservation_record(self.local, record["fields"], "reservation-hmac-1")

    def test_enqueue_default_not_before_and_claim_due_boundary(self):
        ordinary = ExecutionFoundation({
            "state_dir": str(self.root / "ordinary-state"),
            "actions": {"ordinary": {"command": [sys.executable, "-c", "pass"]}},
        })
        ordinary.enqueue(action="ordinary", idempotency_key="ordinary-1")
        ordinary_job = json.loads(next(ordinary.pending.glob("*.json")).read_text())
        self.assertEqual(ordinary_job["not_before_epoch"], 0)

        due = datetime.now(UTC) + timedelta(hours=1)
        with mock.patch("windows_execution_foundation.airtable_local_exec._patch"):
            ingest_queued(self.foundation, self.local, [self.record(due=due)])
        with mock.patch("windows_execution_foundation.core.time.time", return_value=due.timestamp() - 0.001):
            self.assertIsNone(self.foundation._claim())
        with mock.patch("windows_execution_foundation.core.time.time", return_value=due.timestamp()):
            claimed = self.foundation._claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[1]["idempotency_key"], "reservation-hmac-1")

    def test_unknown_key_and_pii_key_fail_closed(self):
        for changes, code in [({"unknown": "x"}, "REQUEST_FIELDS_INVALID"),
                              ({"phone": "09000000000"}, "PII_KEY_FORBIDDEN"),
                              ({"customerName": "PRIVATE"}, "PII_KEY_FORBIDDEN")]:
            with self.subTest(code=code), mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
                result = ingest_queued(self.foundation, self.local, [self.record(changes=changes)])
                self.assertEqual(result["rejected"], 1)
                self.assertEqual(patch.call_args.args[2]["error_code"], code)

    def test_wrong_host_and_past_due_rejected(self):
        cases = [
            (self.record(host="wrong-host"), "WRONG_HOST"),
            (self.record(due=datetime.now(UTC) - timedelta(minutes=1)), "DUE_DATE_PAST"),
        ]
        for record, code in cases:
            with self.subTest(code=code), mock.patch("windows_execution_foundation.airtable_local_exec._patch") as patch:
                result = ingest_queued(self.foundation, self.local, [record])
                self.assertEqual(result["rejected"], 1)
                self.assertEqual(patch.call_args.args[2]["error_code"], code)

    def test_due_date_and_reservation_at_are_independent(self):
        due = datetime.now(UTC) + timedelta(hours=2)
        for reservation_at in (due - timedelta(hours=1), due + timedelta(hours=1)):
            with self.subTest(reservation_at=reservation_at):
                record = self.record(due=due, changes={"reservation_at": reservation_at.isoformat()})
                action, request_hash = validate_reservation_record(
                    self.local, record["fields"], "reservation-hmac-1"
                )
                self.assertEqual(action["reservation_at"], reservation_at.isoformat(timespec="seconds").replace("+00:00", "Z"))
                self.assertEqual(len(request_hash), 64)

    def test_timezone_invalid_reservation_at_fails_closed(self):
        record = self.record(changes={"reservation_at": "2026-09-27T13:00:00"})
        with self.assertRaisesRegex(CityHeavenReservationError, "RESERVATION_AT_INVALID"):
            validate_reservation_record(self.local, record["fields"], "reservation-hmac-1")

    def test_same_request_executes_directly_once_without_task_scheduler(self):
        due = datetime.now(UTC) - timedelta(minutes=1)
        reservation_at = due + timedelta(days=1, hours=2)
        record = self.record(
            state="EF_RUNNING", due=due, changes={"reservation_at": reservation_at.isoformat()}
        )
        config = {
            "python_executable": sys.executable,
            "state_dir": str(self.root / "state"),
            "actions": {"cityheaven-reservation-schedule": {
                "receipt_source_dir": str(self.root / "receipts"),
            }},
            "local_exec_airtable": self.local,
            "cityheaven_reservation": {
                "reader_path": str(self.root / "reader.js"), "node_executable": "node.exe",
                "task_user_id": "S-1-5-21-1-2-3-1001",
            },
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.write_foundation_claim(record)
        scheduler = FakeScheduler()
        execution_count = 0

        def one_shot(command, **kwargs):
            nonlocal execution_count
            execution_count += 1
            manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            transport = json.loads(kwargs["input"])
            self.assertEqual(transport["schema_version"], "cityheaven_policy_transport_v1")
            self.assertEqual(transport["policy"], self.policy)
            self.assertNotIn("policy", manifest)
            self.assertNotIn("credential", kwargs["input"].lower())
            self.assertNotIn("token", kwargs["input"].lower())
            atomic_write_json(Path(manifest["receipt_path"]), {
                "schema_version": "cityheaven_reservation_result_v1",
                "reservation_key": manifest["reservation_key"],
                "request_hash": manifest["request_hash"],
                "terminal_status": "SUCCEEDED",
                "reason_code": "OBSERVED_READ_ONLY",
                "reservation_at": manifest["reservation_at"],
                "cast": manifest["cast"],
                "course": manifest["course"],
                "external_change_count": 0,
                "chat_open_count": 0,
                "customer_send_count": 0,
            })
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("windows_execution_foundation.airtable_local_exec.records_by_request_id", return_value=[record]), \
                mock.patch("windows_execution_foundation.cityheaven_reservation._prefetch_canonical_policy", return_value=self.policy), \
                mock.patch("windows_execution_foundation.cityheaven_reservation.subprocess.run", side_effect=one_shot), \
                mock.patch.object(TaskScheduler, "create", side_effect=AssertionError("must not create task")):
            first = schedule(config_path, "reservation-hmac-1", scheduler=scheduler)
            second = schedule(config_path, "reservation-hmac-1", scheduler=scheduler)
        self.assertEqual(first["status"], "DIRECT_EXECUTED")
        self.assertEqual(second["status"], "TERMINAL_RECEIPT")
        self.assertEqual(execution_count, 1)
        self.assertEqual(scheduler.created, 0)
        manifest = json.loads(next((self.root / "state" / "cityheaven-reservations" / "jobs").glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["reservation_at"], reservation_at.isoformat(timespec="seconds").replace("+00:00", "Z"))
        self.assertNotIn("task_name", manifest)
        receipt = json.loads((self.root / "receipts" / f"{stable_key('reservation-hmac-1')}.json").read_text())
        self.assertEqual(receipt["task_cleanup_status"], "NOT_APPLICABLE_FOUNDATION_QUEUE")

    def test_schedule_at_claim_time_does_not_reject_past_due(self):
        record = self.record(state="EF_RUNNING", due=datetime.now(UTC) - timedelta(seconds=1))
        config = {
            "python_executable": sys.executable, "state_dir": str(self.root / "state"),
            "actions": {"cityheaven-reservation-schedule": {"receipt_source_dir": str(self.root / "receipts")}},
            "local_exec_airtable": self.local,
            "cityheaven_reservation": {"reader_path": "reader.js", "node_executable": "node.exe",
                                         "task_user_id": "S-1-5-21-1-2-3-1001"},
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.write_foundation_claim(record)
        def one_shot(command, **kwargs):
            manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            atomic_write_json(Path(manifest["receipt_path"]), {
                "schema_version": "cityheaven_reservation_result_v1",
                "reservation_key": manifest["reservation_key"], "request_hash": manifest["request_hash"],
                "terminal_status": "SUCCEEDED", "reason_code": "OBSERVED_READ_ONLY",
            })
            return subprocess.CompletedProcess(command, 0, "", "")
        with mock.patch("windows_execution_foundation.airtable_local_exec.records_by_request_id", return_value=[record]), \
                mock.patch("windows_execution_foundation.cityheaven_reservation._prefetch_canonical_policy", return_value=self.policy), \
                mock.patch("windows_execution_foundation.cityheaven_reservation.subprocess.run", side_effect=one_shot):
            result = schedule(config_path, "reservation-hmac-1")
        self.assertEqual(result["terminal_status"], "SUCCEEDED")

    def test_async_foundation_waits_for_terminal_receipt(self):
        receipt_dir = self.root / "async-receipts"
        foundation = ExecutionFoundation({
            "state_dir": str(self.root / "async-state"), "retry_base_seconds": 0,
            "actions": {"async": {
                "command": [sys.executable, "-c", "raise SystemExit(0)"], "allowed_args": [],
                "timeout_seconds": 5, "requires_network": False, "requires_remote": False,
                "receipt_source_dir": str(receipt_dir), "external_async": True,
                "external_poll_seconds": 1, "receipt_request_id_field": "reservation_key",
            }},
        })
        foundation.enqueue(action="async", idempotency_key="req-1")
        deferred = foundation.work_once()
        self.assertEqual(deferred["status"], "DEFERRED_EXTERNAL")
        pending = next(foundation.pending.glob("*.json"))
        job = json.loads(pending.read_text())
        job["not_before_epoch"] = 0
        atomic_write_json(pending, job)
        atomic_write_json(receipt_dir / f"{stable_key('req-1')}.json", {
            "schema_version": "cityheaven_reservation_result_v1", "reservation_key": "req-1",
            "terminal_status": "SUCCEEDED",
        })
        completed = foundation.work_once()
        self.assertEqual(completed["status"], "SUCCESS")

    def test_one_shot_candidate_zero_one_multiple_and_no_pii_receipt(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        cases = [
            ([], "FAILED", "CANDIDATE_NOT_FOUND"),
            ([{"startTime": "13:00", "cast": "CAST-A", "status": "OTHER", "rsvId": "opaque-1",
               "customerName": "PRIVATE NAME", "phone": "09000000000"}], "SUCCEEDED", "OBSERVED_READ_ONLY"),
            ([{"startTime": "13:00", "cast": "CAST-A", "status": "仮予約", "rsvId": "opaque-1"},
              {"startTime": "13:00", "cast": "CAST-A", "status": "取消", "rsvId": "opaque-2"}],
             "FAILED", "CANDIDATE_AMBIGUOUS"),
        ]
        for index, (rows, terminal, reason) in enumerate(cases):
            with self.subTest(index=index):
                case = self.root / f"case-{index}"
                case.mkdir()
                reader = case / "reader.js"
                reader.write_text(
                    "class BrowserReader { async ensure(){} async city(){ return " + json.dumps(rows, ensure_ascii=False) + "; } }\n"
                    "module.exports={BrowserReader};\n", encoding="utf-8")
                receipt = case / "receipt.json"
                manifest = case / "manifest.json"
                atomic_write_json(manifest, {
                    "reservation_key": "reservation-hmac-1", "reservation_at": "2026-09-27T04:00:00Z",
                    "cast": "CAST-A", "course": "COURSE-A", "request_hash": "a" * 64,
                    "receipt_path": str(receipt), "reader_path": str(reader),
                })
                completed = subprocess.run(["node", str(bridge), str(manifest)], capture_output=True,
                                           text=True, encoding="utf-8", check=False)
                value = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertEqual(value["terminal_status"], terminal)
                self.assertEqual(value["reason_code"], reason)
                serialized = json.dumps(value, ensure_ascii=False).lower()
                for forbidden in ("customername", "phone", "private name", "09000000000", "opaque-1", "opaque-2"):
                    self.assertNotIn(forbidden, serialized)
                self.assertEqual(completed.returncode, 0 if terminal == "SUCCEEDED" else 20)

    def test_v20_policy_validation_requires_exact_caller_version_and_send_disabled(self):
        policy = self.policy
        self.assertIs(validate_policy(policy, CANONICAL_VERSION), policy)
        for mutation, required, code in [
            ({"schema_version": "wrong"}, CANONICAL_VERSION, "POLICY_SCHEMA_MISMATCH"),
            ({"version": "wrong"}, CANONICAL_VERSION, "POLICY_IDENTITY_MISMATCH"),
            ({}, "wrong-caller-version", "POLICY_IDENTITY_MISMATCH"),
            ({"rules": {**policy["rules"], "message_send_enabled": True}}, CANONICAL_VERSION,
             "POLICY_SEND_NOT_DISABLED"),
            ({"rules": {**policy["rules"], "message_send_enabled": "yes"}}, CANONICAL_VERSION,
             "POLICY_SEND_NOT_DISABLED"),
        ]:
            with self.subTest(code=code):
                changed = {**policy, **mutation}
                with self.assertRaisesRegex(CityHeavenPolicyError, code):
                    validate_policy(changed, required)

    def test_v02_parent_reader_exact_identity_and_fail_closed_boundaries(self):
        calls = []

        def response(fields):
            def request(config, method, **kwargs):
                calls.append((config, method, kwargs))
                return {"records": [{"id": "recFixtureOnly", "fields": fields}]}
            return request

        exact_fields = {
            EVENT_FIELD_ID: CANONICAL_EVENT_ID,
            VERSION_FIELD_ID: CANONICAL_VERSION,
            VERIFICATION_FIELD_ID: json.dumps(self.policy),
        }
        self.assertEqual(load_canonical_policy(self.local, request=response(exact_fields)), self.policy)
        config, method, kwargs = calls.pop()
        self.assertEqual((config["base_id"], config["table_id"], method),
                         (POLICY_BASE_ID, POLICY_TABLE_ID, "GET"))
        formula = kwargs["params"]["filterByFormula"]
        self.assertIn(CANONICAL_EVENT_ID, formula)
        self.assertIn(CANONICAL_VERSION, formula)
        self.assertEqual(kwargs["params"]["fields[]"],
                         [EVENT_FIELD_ID, VERSION_FIELD_ID, VERIFICATION_FIELD_ID])

        cases = [
            ({**exact_fields, EVENT_FIELD_ID: "wrong-event"}, "POLICY_IDENTITY_MISMATCH"),
            ({**exact_fields, VERSION_FIELD_ID: "wrong-version"}, "POLICY_IDENTITY_MISMATCH"),
            ({**exact_fields, VERIFICATION_FIELD_ID: "{"}, "POLICY_JSON_INVALID"),
            ({**exact_fields, VERIFICATION_FIELD_ID: json.dumps({**self.policy, "schema_version": "wrong"})},
             "POLICY_SCHEMA_MISMATCH"),
            ({**exact_fields, VERIFICATION_FIELD_ID: json.dumps({**self.policy, "event_id": "wrong"})},
             "POLICY_IDENTITY_MISMATCH"),
        ]
        for fields, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(CityHeavenPolicyError, code):
                load_canonical_policy(self.local, request=response(fields))

    def test_v02_child_accepts_only_valid_parent_policy(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        harness = r"""
const core = require(process.argv[1]);
const policy = JSON.parse(process.argv[2]);
const manifest = {policy_version: policy.version};
const results = [];
for (const [name, value] of [
  ['valid', policy],
  ['wrong_event', {...policy, event_id:'wrong'}],
  ['wrong_version', {...policy, version:'wrong'}],
  ['wrong_schema', {...policy, schema_version:'wrong'}],
  ['missing', null],
]) {
  try { core.loadCanonicalPolicy(manifest, value); results.push([name, 'PASS']); }
  catch (error) { results.push([name, error.message]); }
}
process.stdout.write(JSON.stringify(results));
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(bridge), json.dumps(self.policy)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [
            ["valid", "PASS"],
            ["wrong_event", "POLICY_IDENTITY_MISMATCH"],
            ["wrong_version", "POLICY_IDENTITY_MISMATCH"],
            ["wrong_schema", "POLICY_SCHEMA_MISMATCH"],
            ["missing", "POLICY_JSON_INVALID"],
        ])

    def test_v20_course_authority_and_single_message_matching(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        script = r"""
const core = require(process.argv[1]);
const policy = {template_a:{course_exact:'A course',text:'line one\nline two'},template_b:{text:'fallback'}};
let unknown = null;
try { core.chooseTemplate(' UNKNOWN ', policy); } catch (error) { unknown = error.message; }
const exactA = core.chooseTemplate('A course', policy).key;
const knownB = core.chooseTemplate('known course', policy).key;
const exact = core.decideMessage([{text:'<p>line one</p><p>line two</p>'}], policy.template_a.text);
const split = core.decideMessage([{text:'line one'}, {text:'line two'}], policy.template_a.text);
console.log(JSON.stringify({unknown, exactA, knownB, exact, split}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(bridge)], capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["unknown"], "COURSE_SOURCE_MISSING")
        self.assertEqual(value["exactA"], "A")
        self.assertEqual(value["knownB"], "B")
        self.assertEqual(value["exact"]["messageState"], "DONE_NO_SEND")
        self.assertFalse(value["exact"]["wouldSend"])
        self.assertEqual(value["split"]["messageState"], "DONE_NO_SEND")
        self.assertFalse(value["split"]["wouldSend"])

    def test_one_shot_candidate_match_uses_reservation_at(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        case = self.root / "reservation-at-match"
        case.mkdir()
        reader = case / "reader.js"
        rows = [
            {"startTime": "13:00", "cast": "CAST-A", "status": "OTHER", "rsvId": "reservation-target"},
            {"startTime": "12:10", "cast": "CAST-A", "status": "OTHER", "rsvId": "due-date-decoy"},
        ]
        reader.write_text(
            "class BrowserReader { async ensure(){} async city(){ return "
            + json.dumps(rows, ensure_ascii=False)
            + "; } }\nmodule.exports={BrowserReader};\n",
            encoding="utf-8",
        )
        receipt = case / "receipt.json"
        manifest = case / "manifest.json"
        atomic_write_json(manifest, {
            "reservation_key": "reservation-hmac-1",
            "reservation_at": "2026-09-27T04:00:00Z",
            "cast": "CAST-A", "course": "COURSE-A", "request_hash": "a" * 64,
            "receipt_path": str(receipt), "reader_path": str(reader),
        })
        completed = subprocess.run(
            ["node", str(bridge), str(manifest)], capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        value = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(value["candidate_count"], 1)
        self.assertEqual(value["reason_code"], "OBSERVED_READ_ONLY")

    def test_v28_primes_existing_city_page_before_city_read(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        case = self.root / "v28-city-page-prime"
        case.mkdir()
        reader_path = case / "reader.js"
        reader_path.write_text("class BrowserReader {}\nmodule.exports={BrowserReader};\n", encoding="utf-8")
        receipt = case / "receipt.json"
        manifest = case / "manifest.json"
        atomic_write_json(manifest, {
            "reservation_key": "reservation-hmac-v28", "reservation_at": "2026-09-30T04:00:00Z",
            "cast": "CAST-A", "course": "COURSE-A", "request_hash": "b" * 64,
            "receipt_path": str(receipt), "reader_path": str(reader_path),
        })
        harness = r"""
const core = require(process.argv[1]);
const manifest = process.argv[2];
const calls = [];
let dedicated = false;
const reader = {
  async ensure(kind) { calls.push(`ensure:${kind}`); },
  async read(kind, fn, date) {
    calls.push(`read:${kind}:${date}`);
    const prior = { location: global.location, history: global.history };
    global.location = { origin: 'https://pro-manager.cityheaven.net', pathname: '/DisplayChat', search: '' };
    global.history = { state: null, replaceState(_state, _title, url) {
      const parsed = new URL(url, global.location.origin);
      global.location.pathname = parsed.pathname; global.location.search = parsed.search; dedicated = true;
    }};
    try { return fn(date); } finally { global.location = prior.location; global.history = prior.history; }
  },
  async city(date) {
    calls.push(`city:${date}`);
    if (!dedicated) throw new Error('CITY_DEDICATED_PAGE_UNAVAILABLE');
    return [];
  },
};
(async () => {
  const code = await core.run(manifest, { reader });
  process.stdout.write(JSON.stringify({code, calls}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(bridge), str(manifest)], capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["code"], 20)
        self.assertEqual(value["calls"], [
            "ensure:city", "read:city:2026/09/30", "city:2026/09/30",
        ])
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["reason_code"], "CANDIDATE_NOT_FOUND")

    def test_v35_passes_exact_browser_runtime_and_keeps_legacy_reader_compatible(self):
        source = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation"
        bridge = source / "cityheaven_reservation_once.js"
        case = self.root / "v35-runtime-argument"
        case.mkdir()
        capture = case / "captured-runtime.json"
        reader = case / "capturing-reader.js"
        reader.write_text(
            "const fs=require('node:fs');\n"
            "class BrowserReader { constructor(signal, rawConfig) { "
            "fs.writeFileSync(rawConfig.capture_path, JSON.stringify(rawConfig)); } "
            "async ensure(){} async city(){ return [{startTime:'13:00',cast:'CAST-A',status:'OTHER',rsvId:'opaque'}]; } }\n"
            "module.exports={BrowserReader};\n", encoding="utf-8",
        )
        runtime = {"browser": "CHROME", "cdp_port": 9227,
                   "capture_path": str(capture), "marker": {"exact": [1, "two"]}}

        def manifest(name, reader_path, browser_runtime_marker=True):
            receipt = case / f"{name}.receipt.json"
            path = case / f"{name}.manifest.json"
            value = {
                "reservation_key": f"synthetic-{name}", "reservation_at": "2026-09-27T04:00:00Z",
                "cast": "CAST-A", "course": "COURSE-A", "request_hash": "a" * 64,
                "receipt_path": str(receipt), "reader_path": str(reader_path),
            }
            if browser_runtime_marker:
                value["browser_runtime"] = runtime
            atomic_write_json(path, value)
            return path, receipt

        runtime_manifest, runtime_receipt = manifest("runtime", reader)
        completed = subprocess.run(["node", str(bridge), str(runtime_manifest)], capture_output=True,
                                   text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(capture.read_text(encoding="utf-8")), runtime)
        runtime_result = json.loads(runtime_receipt.read_text(encoding="utf-8"))
        self.assertFalse(runtime_result["send_enabled"])
        self.assertEqual(runtime_result["customer_send_count"], 0)
        self.assertEqual(runtime_result["reservation_mutation_count"], 0)
        self.assertEqual(runtime_result["external_change_count"], 0)

        legacy = case / "legacy-reader.js"
        legacy.write_text(
            "class BrowserReader { constructor(signal) { this.signal=signal; } "
            "async ensure(){} async city(){ return []; } }\nmodule.exports={BrowserReader};\n",
            encoding="utf-8",
        )
        legacy_manifest, legacy_receipt = manifest("legacy", legacy, False)
        completed = subprocess.run(["node", str(bridge), str(legacy_manifest)], capture_output=True,
                                   text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 20, completed.stderr)
        self.assertEqual(json.loads(legacy_receipt.read_text(encoding="utf-8"))["reason_code"],
                         "CANDIDATE_NOT_FOUND")

        runtime_reader = source / "cityheaven_reservation_runtime_reader.js"
        for name, marker in (("missing", False), ("invalid", True)):
            with self.subTest(runtime_config=name):
                invalid_manifest, invalid_receipt = manifest(name, runtime_reader, marker)
                completed = subprocess.run(["node", str(bridge), str(invalid_manifest)], capture_output=True,
                                           text=True, encoding="utf-8", check=False)
                self.assertEqual(completed.returncode, 30)
                invalid_result = json.loads(invalid_receipt.read_text(encoding="utf-8"))
                self.assertEqual(invalid_result["reason_code"], "CHROME_RUNTIME_CONFIG_INVALID")
                self.assertFalse(invalid_result["send_enabled"])
                self.assertEqual(invalid_result["customer_send_count"], 0)
                self.assertEqual(invalid_result["reservation_mutation_count"], 0)
                self.assertEqual(invalid_result["external_change_count"], 0)

    def test_v24_post_confirmed_readonly_integration_contract(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        harness = r"""
const fs = require('node:fs');
const path = require('node:path');
const core = require(process.argv[1]);
const root = process.argv[2];
const readerPath = path.join(root, 'synthetic-reader.js');
fs.writeFileSync(readerPath, "class BrowserReader {}\nmodule.exports={BrowserReader};\n", 'utf8');
const policy = {
  version: 'cityheaven_ab_message_v1_20260923',
  template_a: { course_exact: 'A course', text: 'canonical A' },
  template_b: { text: 'canonical B' },
};
const confirmed = '\u78ba\u5b9a';
const baseRow = { startTime: '13:00', cast: 'CAST-A', status: confirmed, rsvId: 'PRIVATE-RSV-ID',
  customerName: 'PRIVATE CUSTOMER', phone: '09000000000' };
const scenarios = [
  { name: 'candidate_zero', rows: [], terminal: 'FAILED', reason: 'CANDIDATE_NOT_FOUND',
    detail: 0, chatCandidates: 0, history: 0 },
  { name: 'candidate_multiple', rows: [baseRow, {...baseRow, rsvId: 'PRIVATE-RSV-ID-2'}],
    terminal: 'FAILED', reason: 'CANDIDATE_AMBIGUOUS', detail: 0, chatCandidates: 0, history: 0 },
  { name: 'non_confirmed', rows: [{...baseRow, status: 'OTHER'}], terminal: 'SUCCEEDED',
    reason: 'OBSERVED_READ_ONLY', detail: 0, chatCandidates: 0, history: 0 },
  { name: 'a_already_sent', rows: [baseRow], course: 'A course', chats: [{unread:false}],
    messages: [{text:'canonical A',sender_role:'staff',rsvId:'PRIVATE-RSV-ID'}], terminal: 'SUCCEEDED', reason: 'DONE_NO_SEND',
    state: 'PRESENT', wouldSend: false, matches: 1, chatRead: 1, history: 1, template: 'A' },
  { name: 'b_not_sent', rows: [baseRow], course: 'known non-A', chats: [{unread:false}],
    messages: [{text:'some prior text'}], terminal: 'SUCCEEDED', reason: 'SEND_DISABLED',
    state: 'MISSING', wouldSend: false, matches: 0, chatRead: 1, history: 1, template: 'B' },
  { name: 'unknown_course', rows: [baseRow], course: 'UNKNOWN', chats: [{unread:false}], messages: [],
    terminal: 'FAILED', reason: 'COURSE_SOURCE_MISSING', state: 'UNDETERMINED', wouldSend: false,
    chatCandidates: 0, history: 0 },
  { name: 'chat_zero', rows: [baseRow], course: 'A course', chats: [], terminal: 'FAILED',
    reason: 'CHAT_MATCH_UNDETERMINED', state: 'FAIL_CLOSED', wouldSend: false, history: 0 },
  { name: 'chat_multiple', rows: [baseRow], course: 'A course', chats: [{unread:false},{unread:false}],
    terminal: 'FAILED', reason: 'CHAT_MATCH_UNDETERMINED', state: 'FAIL_CLOSED', wouldSend: false, history: 0 },
  { name: 'unread_risk', rows: [baseRow], course: 'A course', chats: [{unread:true}], messages: [],
    terminal: 'FAILED', reason: 'CHAT_UNREAD_SIDE_EFFECT_RISK', state: 'FAIL_CLOSED', wouldSend: false, history: 0 },
];
(async () => {
  const results = [];
  for (const scenario of scenarios) {
    const receiptPath = path.join(root, `${scenario.name}.receipt.json`);
    const manifestPath = path.join(root, `${scenario.name}.manifest.json`);
    fs.writeFileSync(manifestPath, JSON.stringify({
      reservation_key: `synthetic-${scenario.name}`, reservation_at: '2026-09-27T04:00:00Z',
      cast: 'CAST-A', course: 'PAYLOAD UNKNOWN HINT', request_hash: 'a'.repeat(64),
      receipt_path: receiptPath, reader_path: readerPath,
    }), 'utf8');
    const counts = { ensure: 0, city: 0, detail: 0, chatCandidates: 0, history: 0, save: 0 };
    const reader = {
      async ensure() { counts.ensure++; },
      async city() { counts.city++; return scenario.rows; },
    };
    class Adapter {
      async reservationDetail() { counts.detail++; return { course: scenario.course }; }
      async chatCandidates() { counts.chatCandidates++; return scenario.chats; }
      async chatHistory() { counts.history++; return {messages:scenario.messages,complete:true,authenticated:true}; }
      async save() { counts.save++; throw new Error('SEND_FORBIDDEN'); }
      async send() { counts.save++; throw new Error('SEND_FORBIDDEN'); }
      async deliver() { counts.save++; throw new Error('SEND_FORBIDDEN'); }
    }
    const code = await core.run(manifestPath, { reader, Adapter, loadPolicy: async () => policy,
      now: () => new Date('2026-09-26T03:00:00Z') });
    results.push({ scenario, code, counts, receipt: JSON.parse(fs.readFileSync(receiptPath, 'utf8')) });
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(bridge), str(self.root)], capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        results = json.loads(completed.stdout)
        required_fields = {
            "schema_version", "reservation_key", "request_hash", "terminal_status", "reason_code",
            "observed_at", "reservation_at", "cast", "course", "candidate_count", "observed_status",
            "reservation_ref", "course_source", "policy_version", "template_key", "chat_match_count",
            "message_match_count", "message_state", "would_send", "send_enabled", "chat_read_count",
            "external_change_count", "reservation_mutation_count", "chat_open_count",
            "customer_send_count", "task_cleanup_status",
            "staff_notification_eligible", "required_ab_state", "time_gate_state",
        }
        for result in results:
            with self.subTest(scenario=result["scenario"]["name"]):
                scenario, counts, receipt = result["scenario"], result["counts"], result["receipt"]
                self.assertEqual(receipt["terminal_status"], scenario["terminal"])
                self.assertEqual(receipt["reason_code"], scenario["reason"])
                self.assertEqual(counts["detail"], scenario.get("detail", 1))
                self.assertEqual(counts["chatCandidates"], scenario.get("chatCandidates", 1))
                self.assertEqual(counts["history"], scenario.get("history", 0))
                self.assertEqual(counts["save"], 0)
                self.assertEqual(receipt["message_state"], scenario.get("state", "UNDETERMINED"))
                self.assertEqual(receipt["would_send"], scenario.get("wouldSend", False))
                self.assertEqual(receipt["message_match_count"], scenario.get("matches", 0))
                self.assertEqual(receipt["chat_read_count"], scenario.get("chatRead", 0))
                self.assertFalse(receipt["send_enabled"])
                self.assertEqual(receipt["customer_send_count"], 0)
                self.assertEqual(receipt["reservation_mutation_count"], 0)
                self.assertEqual(receipt["external_change_count"], 0)
                self.assertEqual(set(receipt), required_fields)
                if counts["detail"] == 1:
                    self.assertEqual(receipt["course_source"], "RESERVATION_DETAIL")
                if "template" in scenario:
                    self.assertEqual(receipt["template_key"], scenario["template"])
                    self.assertEqual(receipt["policy_version"], CANONICAL_VERSION)
                serialized = json.dumps(receipt, ensure_ascii=False).lower()
                for forbidden in ("private customer", "09000000000", "private-rsv-id", "canonical a", "canonical b"):
                    self.assertNotIn(forbidden, serialized)

    def test_confirmed_time_gate_precedes_detail_policy_chat_and_notifier(self):
        bridge = Path(__file__).resolve().parents[1] / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        harness = r"""
const fs = require('node:fs');
const path = require('node:path');
const core = require(process.argv[1]);
const root = process.argv[2];
const readerPath = path.join(root, 'gate-order-reader.js');
fs.writeFileSync(readerPath, "class BrowserReader {}\nmodule.exports={BrowserReader};\n", 'utf8');
const confirmed = '\u78ba\u5b9a';
const policy = { version: 'fixture', template_a: { course_exact: 'A', text: 'A' },
  template_b: { text: 'B' } };
const scenarios = [
  { name: 'same_day', reservationAt: '2026-10-05T08:00:00Z', now: '2026-10-05T06:30:00Z',
    code: 0, terminal: 'SUCCEEDED', reason: 'RESERVATION_WITHIN_30_MINUTES', gate: 'EXCLUDED' },
  { name: 'under_30_minutes', reservationAt: '2026-10-05T15:00:00Z', now: '2026-10-05T14:30:01Z',
    code: 0, terminal: 'SUCCEEDED', reason: 'RESERVATION_WITHIN_30_MINUTES', gate: 'EXCLUDED' },
  { name: 'eligible_chat_fail_closed', reservationAt: '2026-10-05T15:00:00Z', now: '2026-10-05T14:30:00Z',
    code: 20, terminal: 'FAILED', reason: 'CHAT_MATCH_UNDETERMINED', gate: 'ELIGIBLE',
    detail: 1, policy: 1, chatCandidates: 1 },
  { name: 'unknown_checked_at', reservationAt: '2026-10-05T15:00:00Z', now: 'unknown',
    code: 20, terminal: 'FAILED', reason: 'RESERVATION_TIME_UNKNOWN', gate: 'UNKNOWN' },
];
(async () => {
  const results = [];
  for (const scenario of scenarios) {
    const receiptPath = path.join(root, `${scenario.name}.gate-order.receipt.json`);
    const manifestPath = path.join(root, `${scenario.name}.gate-order.manifest.json`);
    fs.writeFileSync(manifestPath, JSON.stringify({ reservation_key: `gate-${scenario.name}`,
      reservation_at: scenario.reservationAt, cast: 'CAST-A', course: 'hint', request_hash: 'c'.repeat(64),
      receipt_path: receiptPath, reader_path: readerPath, staff_notify_enabled: true,
      send_state_path: path.join(root, `${scenario.name}.send-state.json`) }), 'utf8');
    const startTime = core.jstParts(scenario.reservationAt).time;
    const counts = { detail: 0, policy: 0, chatCandidates: 0, history: 0, notifier: 0 };
    const reader = { async ensure() {}, async city() { return [{ startTime, cast: 'CAST-A',
      status: confirmed, rsvId: 'fixture-reservation' }]; } };
    const adapter = { async reservationDetail() { counts.detail++; return { course: 'A' }; },
      async chatCandidates() { counts.chatCandidates++; return []; },
      async chatHistory() { counts.history++; return { messages: [], complete: true, authenticated: true }; } };
    const code = await core.run(manifestPath, { reader, adapter,
      loadPolicy: async () => { counts.policy++; return policy; }, now: () => scenario.now,
      notifyStaff: async () => { counts.notifier++; throw new Error('NOTIFIER_MUST_NOT_RUN'); } });
    results.push({ scenario, code, counts, receipt: JSON.parse(fs.readFileSync(receiptPath, 'utf8')) });
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(bridge), str(self.root)], capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for result in json.loads(completed.stdout):
            with self.subTest(scenario=result["scenario"]["name"]):
                scenario, counts, receipt = result["scenario"], result["counts"], result["receipt"]
                self.assertEqual(result["code"], scenario["code"])
                self.assertEqual(receipt["terminal_status"], scenario["terminal"])
                self.assertEqual(receipt["reason_code"], scenario["reason"])
                self.assertEqual(receipt["time_gate_state"], scenario["gate"])
                self.assertEqual(counts["detail"], scenario.get("detail", 0))
                self.assertEqual(counts["policy"], scenario.get("policy", 0))
                self.assertEqual(counts["chatCandidates"], scenario.get("chatCandidates", 0))
                self.assertEqual(counts["history"], 0)
                self.assertEqual(counts["notifier"], 0)
                self.assertFalse(receipt["would_send"])
                self.assertFalse(receipt["send_enabled"])
                self.assertEqual(receipt["customer_send_count"], 0)
                self.assertEqual(receipt["reservation_mutation_count"], 0)
                self.assertEqual(receipt["external_change_count"], 0)


if __name__ == "__main__":
    unittest.main()
