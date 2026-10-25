from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.windows_execution_foundation import cityheaven_daily_metrics as daily
from src.windows_execution_foundation.cityheaven_daily_metrics import (
    CollectorError, INDEX_EVIDENCE, METRICS, READER_SUBPROCESS_TIMEOUT_SECONDS,
    airtable_request, fetch, metric_plan, parse_source, pii_guard, run, write_metrics,
)


HEADERS = [
    "アクセス状況 > アクセス数 > 合計", "アクセス状況 > PC", "アクセス状況 > SP",
    "マイガール数", "unused-4", "unused-5", "口コミ数", "ミテネ数", "unused-8",
    "オキニトーク > 受信人数", "オキニトーク > 送信数", "unused-11", "写メ日記投稿数",
]


def payload(values=None, casts=1):
    values = values or ["10", "2", "8", "3", "0", "0", "1", "4", "0", "5", "6", "0", "7"]
    return {"casts": [{"girl_id": str(100 + index), "display_name": f"cast-{index}", "header_paths": HEADERS, "values": values} for index in range(casts)]}


class ParserTests(unittest.TestCase):
    def test_normal_row(self):
        rows, mappings = parse_source(payload())
        self.assertEqual(rows[0]["access"], 10)
        self.assertEqual(rows[0]["diary_count"], 7)
        self.assertEqual(mappings[0]["mapping"], INDEX_EVIDENCE)

    def test_missing_field(self):
        with self.assertRaisesRegex(CollectorError, "SCHEMA_DRIFT"):
            parse_source(payload(values=["1"] * 10))

    def test_column_drift(self):
        changed = list(HEADERS); changed[7] = "別の列"
        source = payload(); source["casts"][0]["header_paths"] = changed
        with self.assertRaisesRegex(CollectorError, "SCHEMA_DRIFT"):
            parse_source(source)

    def test_unknown_is_not_zero(self):
        values = ["1"] * 13; values[3] = "---"
        with self.assertRaisesRegex(CollectorError, "SOURCE_MISSING"):
            parse_source(payload(values=values))

    def test_empty_cast_list(self):
        with self.assertRaisesRegex(CollectorError, "SOURCE_MISSING"):
            parse_source({"casts": []})

    def assert_source_label(self, index, label, metric):
        source = payload()
        source["casts"][0]["header_paths"] = list(HEADERS)
        source["casts"][0]["header_paths"][index] = label
        rows, mappings = parse_source(source)
        self.assertEqual(mappings[0]["mapping"][metric], index)
        self.assertEqual(rows[0][metric], int(source["casts"][0]["values"][index]))
        self.assertEqual(source["casts"][0]["header_paths"][index], label)

    def test_observed_total_maps_to_access(self):
        self.assert_source_label(0, "総数", "access")

    def test_observed_computer_maps_to_pc(self):
        for label in ("パソコン", "アクセス状況 > パソコン"):
            with self.subTest(label=label):
                self.assert_source_label(1, label, "pc")

    def test_observed_smartphone_maps_to_sp(self):
        for label in ("スマホ", "アクセス状況 > スマホ"):
            with self.subTest(label=label):
                self.assert_source_label(2, label, "sp")

    def test_new_source_labels_do_not_accept_unknown_paths_or_data_suffixes(self):
        for index, label in enumerate(("総数", "パソコン", "スマホ")):
            for invalid in ("未知の列", label + "予測", label + " > 60", "未知のグループ > " + label):
                with self.subTest(index=index, invalid=invalid):
                    source = payload()
                    source["casts"][0]["header_paths"] = list(HEADERS)
                    source["casts"][0]["header_paths"][index] = invalid
                    with self.assertRaisesRegex(CollectorError, "SCHEMA_DRIFT"):
                        parse_source(source)

    def test_observed_schema_still_rejects_missing_unknown_and_bad_numbers(self):
        for metric, index in INDEX_EVIDENCE.items():
            for invalid in (None, "", "UNKNOWN", "---", "-", "不明", "-1", "1.5"):
                with self.subTest(metric=metric, invalid=invalid):
                    source = payload()
                    source["casts"][0]["header_paths"] = ["総数", "アクセス状況 > パソコン", "アクセス状況 > スマホ", *HEADERS[3:]]
                    source["casts"][0]["values"][index] = invalid
                    with self.assertRaises(CollectorError):
                        parse_source(source)


class MappingAndGuardTests(unittest.TestCase):
    def test_stable_unit_metric_and_no_display_name(self):
        casts, _ = parse_source(payload())
        plan = metric_plan(casts, "2026-09-24", "receipt.json")
        self.assertEqual(len(plan), len(METRICS))
        self.assertTrue(all("cityheaven_girl:100" in row["Dedup Key"] for row in plan))
        self.assertNotIn("cast-0", json.dumps(plan))
        self.assertTrue(pii_guard(plan))

    def test_pii_guard(self):
        self.assertFalse(pii_guard({"value": "customer@example.test"}))
        self.assertFalse(pii_guard({"value": "090-1234-5678"}))

    def test_same_input_rerun_duplicate_zero(self):
        casts, _ = parse_source(payload())
        plan = metric_plan(casts, "2026-09-24", "receipt.json")
        records = []

        def fake(method, *, params=None, body=None):
            if method == "GET":
                return {"records": list(records)}
            created = []
            for item in body["records"]:
                record = {"id": f"rec{len(records)}", "fields": dict(item["fields"])}
                records.append(record); created.append(record)
            return {"records": created}

        first = write_metrics(plan, fake)
        second = write_metrics(plan, fake)
        self.assertEqual(first["write_count"], len(plan))
        self.assertEqual(second, {"write_count": 0, "readback_count": len(plan), "duplicate_count": 0, "replay_count": len(plan)})

    def test_failure_writes_receipt_not_airtable(self):
        calls = []
        with TemporaryDirectory() as directory:
            receipt = run("2026-09-24", write_airtable=True, runtime=Path(directory), fetcher=lambda _: {"casts": []}, request=lambda *a, **k: calls.append((a, k)))
            self.assertEqual(receipt["status"], "FAILED")
            self.assertEqual(receipt["airtable_write_count"], 0)
            self.assertEqual(receipt["cityheaven_fetch_count"], 1)
            self.assertFalse(calls)
            self.assertTrue((Path(directory) / "receipts" / "20260924.json").exists())

    def test_success_receipt_is_sanitized(self):
        source = payload(casts=2)
        with TemporaryDirectory() as directory:
            receipt = run("2026-09-24", write_airtable=False, runtime=Path(directory), fetcher=lambda _: source)
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["cityheaven_fetch_count"], 1)
            self.assertEqual(receipt["airtable_write_run_count"], 0)
            self.assertNotIn("raw_source_payload", receipt)
            self.assertFalse(receipt["source_payload_persisted"])
            self.assertEqual(receipt["pii_count"], 0)
            self.assertEqual(receipt["schema_status"], "PASS")
            self.assertEqual(receipt["auth_status"], "PASS")
            self.assertRegex(receipt["raw_source_sha256"], r"^[0-9a-f]{64}$")

    def test_unknown_is_counted_and_never_reaches_writer(self):
        source = payload(); source["casts"][0]["values"][3] = "UNKNOWN"
        calls = []
        with TemporaryDirectory() as directory:
            receipt = run("2026-09-24", write_airtable=True, runtime=Path(directory), fetcher=lambda _: source, request=lambda *a, **k: calls.append((a, k)))
            self.assertEqual(receipt["status"], "FAILED")
            self.assertEqual(receipt["unknown_count"], 1)
            self.assertEqual(receipt["business_write_count"], 0)
            self.assertFalse(calls)

    def test_fetch_failure_never_invokes_writer(self):
        calls = []
        def failed_fetch(_):
            raise CollectorError("BROWSER_UNAVAILABLE")
        with TemporaryDirectory() as directory:
            receipt = run("2026-09-24", write_airtable=True, runtime=Path(directory), fetcher=failed_fetch, request=lambda *a, **k: calls.append((a, k)))
            self.assertEqual(receipt["failure_code"], "BROWSER_UNAVAILABLE")
            self.assertFalse(calls)

    def test_credential_unavailable_fails_closed_without_secret_text(self):
        with mock.patch.object(daily, "_load_cti_writer", side_effect=RuntimeError("plaintext-secret-marker")):
            with self.assertRaisesRegex(CollectorError, "^AIRTABLE_WRITE_FAILED$") as caught:
                airtable_request("GET")
        self.assertNotIn("plaintext-secret-marker", str(caught.exception))

    def test_credential_unavailable_produces_sanitized_failed_receipt(self):
        with TemporaryDirectory() as directory, mock.patch.object(
            daily, "_load_cti_writer", side_effect=RuntimeError("plaintext-secret-marker")
        ):
            receipt = run("2026-09-24", write_airtable=True, runtime=Path(directory), fetcher=lambda _: payload())
            persisted = (Path(directory) / "receipts" / "20260924.json").read_text(encoding="utf-8")
        self.assertEqual(receipt["failure_code"], "AIRTABLE_WRITE_FAILED")
        self.assertEqual(receipt["business_write_count"], 0)
        self.assertNotIn("plaintext-secret-marker", persisted)

    def test_production_command_defaults_to_previous_jst_day(self):
        with mock.patch.object(daily, "previous_jst_day", return_value="2026-09-25"):
            args = daily.parser().parse_args([])
        self.assertEqual(args.date, "2026-09-25")
        self.assertFalse(args.write_airtable)

    def test_raw_source_pii_fails_before_airtable(self):
        source = payload(); source["casts"][0]["email"] = "customer@example.test"
        calls = []
        with TemporaryDirectory() as directory:
            receipt = run("2026-09-24", write_airtable=True, runtime=Path(directory), fetcher=lambda _: source, request=lambda *a, **k: calls.append((a, k)))
            self.assertEqual(receipt["failure_code"], "SOURCE_MISSING")
            self.assertEqual(receipt["airtable_write_count"], 0)
            self.assertFalse(calls)

    def test_reader_is_read_only(self):
        source = (Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js").read_text(encoding="utf-8")
        self.assertIn("method: 'GET'", source)
        for forbidden in ("method: 'POST'", ".click(", ".submit(", ".save(", "fetch('/C2GirlSave"):
            self.assertNotIn(forbidden, source)


class DailyBrowserBootstrapTests(unittest.TestCase):
    @staticmethod
    def completed(*, returncode=0, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def opera_config():
        return {"cityheaven_daily": {"browser": "OPERA", "cdp_port": 9236}}

    @staticmethod
    def reservation_config():
        return {
            "state_dir": "C:/State",
            "cityheaven_daily": {"browser": "RESERVATION_CHROME_REUSE", "cdp_port": 9236},
            "cityheaven_reservation": {
                "cdp_port": 9227, "chrome_executable": "C:/Chrome/chrome.exe",
                "dedicated_profile_path": "C:/Profiles/ReservationRuntimeChromeProfile",
                "reader_path": "C:/Readers/reservation.js", "node_executable": "C:/Node/node.exe",
            },
        }

    def test_matching_runtime_is_checked_before_reader(self):
        bootstrap = self.completed(stdout=json.dumps({"cdp_reachable": True, "existing_profile_reused": True}))
        reader = self.completed(stdout=json.dumps(payload()))
        with mock.patch.object(daily, "_load_configuration", return_value=self.opera_config()), \
                mock.patch.object(daily.subprocess, "run", side_effect=[bootstrap, reader]) as called:
            self.assertEqual(fetch("2026-09-30"), payload())
        self.assertEqual(called.call_count, 2)
        self.assertIn("cityheaven_daily_opera_runtime.js", called.call_args_list[0].args[0][1])
        self.assertIn("cityheaven_daily_reader.js", called.call_args_list[1].args[0][1])

    def test_absent_runtime_bootstrap_result_proceeds_to_reader(self):
        bootstrap = self.completed(stdout=json.dumps({"cdp_reachable": True, "existing_profile_reused": False}))
        reader = self.completed(stdout=json.dumps(payload(casts=2)))
        with mock.patch.object(daily, "_load_configuration", return_value=self.opera_config()), \
                mock.patch.object(daily.subprocess, "run", side_effect=[bootstrap, reader]) as called:
            self.assertEqual(len(fetch("2026-09-30")["casts"]), 2)
        self.assertEqual(called.call_count, 2)

    def test_runtime_mismatch_fails_closed_without_reader(self):
        mismatch = self.completed(returncode=20, stderr="CDP_PORT_RUNTIME_MISMATCH\n")
        with mock.patch.object(daily, "_load_configuration", return_value=self.opera_config()), \
                mock.patch.object(daily.subprocess, "run", return_value=mismatch) as called:
            with self.assertRaisesRegex(CollectorError, "^CDP_PORT_RUNTIME_MISMATCH$"):
                fetch("2026-09-30")
        self.assertEqual(called.call_count, 1)

    def test_bootstrap_failure_does_not_run_reader(self):
        failed = self.completed(returncode=20, stderr="BROWSER_CDP_UNAVAILABLE\n")
        with mock.patch.object(daily, "_load_configuration", return_value=self.opera_config()), \
                mock.patch.object(daily.subprocess, "run", return_value=failed) as called:
            with self.assertRaisesRegex(CollectorError, "^BROWSER_CDP_UNAVAILABLE$"):
                fetch("2026-09-30")
        self.assertEqual(called.call_count, 1)

    def test_reader_failure_propagates_after_successful_bootstrap(self):
        bootstrap = self.completed(stdout=json.dumps({"cdp_reachable": True, "existing_profile_reused": True}))
        reader = self.completed(returncode=20, stderr="AUTH_REQUIRED\n")
        with mock.patch.object(daily, "_load_configuration", return_value=self.opera_config()), \
                mock.patch.object(daily.subprocess, "run", side_effect=[bootstrap, reader]) as called:
            with self.assertRaisesRegex(CollectorError, "^AUTH_REQUIRED$"):
                fetch("2026-09-30")
        self.assertEqual(called.call_count, 2)

    def test_reservation_chrome_match_is_reused_and_reader_gets_9227_explicitly(self):
        readiness = self.completed(stdout=json.dumps({
            "cdp_reachable": True, "runtime_identity": "MATCH", "profile_identity": "MATCH",
            "cdp_port": 9227, "auth_state": "DEFERRED_TO_DAILY_READER", "daily_manager_ready": None,
            "existing_process_reused": True, "new_profile_created": False,
            "additional_browser_launch_count": 0, "browser_restart_count": 0,
        }))
        reader = self.completed(stdout=json.dumps(payload()))
        with mock.patch.object(daily, "_load_configuration", return_value=self.reservation_config()), \
                mock.patch.object(daily.subprocess, "run", side_effect=[readiness, reader]) as called:
            result = fetch("2026-09-30")
        self.assertEqual(result, payload())
        self.assertEqual(result.runtime_details["route"], "RESERVATION_CHROME_REUSE")
        self.assertEqual(called.call_count, 2)
        self.assertIn("cityheaven_reservation_runtime_reader.js", called.call_args_list[0].args[0][-1])
        runtime_input = json.loads(called.call_args_list[0].kwargs["input"])
        self.assertEqual(runtime_input["cdp_port"], 9227)
        self.assertIn("ReservationRuntimeChromeProfile", runtime_input["profile_path"])
        reader_command = called.call_args_list[1].args[0]
        self.assertEqual(reader_command[-2:], ["--cdp-port", "9227"])
        self.assertNotIn("--config", reader_command)

    def test_reservation_chrome_mismatch_fails_closed_without_reader(self):
        mismatch = self.completed(returncode=20, stderr="CDP_PORT_RUNTIME_MISMATCH\n")
        with mock.patch.object(daily, "_load_configuration", return_value=self.reservation_config()), \
                mock.patch.object(daily.subprocess, "run", return_value=mismatch) as called:
            with self.assertRaisesRegex(CollectorError, "^CDP_PORT_RUNTIME_MISMATCH$"):
                fetch("2026-09-30")
        self.assertEqual(called.call_count, 1)

    def test_reservation_chrome_reader_auth_required_stops_before_writer(self):
        readiness = self.completed(stdout=json.dumps({
            "cdp_reachable": True, "runtime_identity": "MATCH", "profile_identity": "MATCH",
            "cdp_port": 9227, "auth_state": "DEFERRED_TO_DAILY_READER", "daily_manager_ready": None,
        }))
        reader = self.completed(returncode=20, stderr="AUTH_REQUIRED\n")
        writes = []
        with TemporaryDirectory() as directory, \
                mock.patch.object(daily, "_load_configuration", return_value=self.reservation_config()), \
                mock.patch.object(daily.subprocess, "run", side_effect=[readiness, reader]) as called:
            receipt = run("2026-09-30", write_airtable=True, runtime=Path(directory),
                          fetcher=lambda date: fetch(date), request=lambda *a, **k: writes.append((a, k)))
        self.assertEqual(receipt["failure_code"], "AUTH_REQUIRED")
        self.assertEqual(receipt["airtable_write_count"], 0)
        self.assertFalse(writes)
        self.assertEqual(called.call_count, 2)

    def test_reservation_chrome_reader_source_failures_propagate(self):
        readiness = self.completed(stdout=json.dumps({
            "cdp_reachable": True, "runtime_identity": "MATCH", "profile_identity": "MATCH",
            "cdp_port": 9227, "auth_state": "DEFERRED_TO_DAILY_READER", "daily_manager_ready": None,
        }))
        for code in ("SOURCE_MISSING", "SCHEMA_DRIFT"):
            with self.subTest(code=code), \
                    mock.patch.object(daily, "_load_configuration", return_value=self.reservation_config()), \
                    mock.patch.object(daily.subprocess, "run", side_effect=[
                        readiness, self.completed(returncode=30, stderr=code + "\n"),
                    ]) as called:
                with self.assertRaisesRegex(CollectorError, "^" + code + "$"):
                    fetch("2026-09-30")
            self.assertEqual(called.call_count, 2)


class DailyReaderAuthTests(unittest.TestCase):
    @staticmethod
    def classify(**values):
        reader = Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        script = "const r=require(process.argv[1]);console.log(r.pageAuthState(JSON.parse(process.argv[2])))"
        completed = subprocess.run(
            ["node", "-e", script, str(reader), json.dumps(values)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_authenticated_manager_with_login_form_is_not_auth_required(self):
        self.assertEqual(self.classify(
            requestedUrl="/C2GirlList.php?shopdir=s_apronlady",
            responseUrl="https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady",
            documentUrl="about:blank", hasLoginForm=True, hasManagerStructure=True,
        ), "AUTHENTICATED")

    def test_real_login_only_page_is_auth_required(self):
        self.assertEqual(self.classify(
            requestedUrl="/C2GirlList.php?shopdir=s_apronlady",
            responseUrl="https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady",
            documentUrl="about:blank", hasLoginForm=True, hasManagerStructure=False,
        ), "AUTH_REQUIRED")

    def test_redirected_login_page_is_auth_required(self):
        self.assertEqual(self.classify(
            requestedUrl="/C2TokeiGirlDetail.php?member_id=1&shopdir=s_apronlady",
            responseUrl="https://newmanager.cityheaven.net/C1Login.php?shopdir=s_apronlady",
            documentUrl="about:blank", hasLoginForm=False, hasManagerStructure=False,
        ), "AUTH_REQUIRED")

    def test_malformed_manager_page_fails_closed_as_source_missing(self):
        self.assertEqual(self.classify(
            requestedUrl="/C2GirlList.php?shopdir=s_apronlady",
            responseUrl="https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady",
            documentUrl="about:blank", hasLoginForm=False, hasManagerStructure=False,
        ), "SOURCE_MISSING")


class DailyReaderCandidateTests(unittest.TestCase):
    @staticmethod
    def parse(sources):
        reader = Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        script = (
            "const r=require(process.argv[1]);const m=new Map();"
            "for(const x of JSON.parse(process.argv[2]))r.addCandidate(m,x.source,x.display);"
            "console.log(JSON.stringify([...m].map(([girl_id,display_name])=>({girl_id,display_name}))))"
        )
        completed = subprocess.run(
            ["node", "-e", script, str(reader), json.dumps(sources)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_current_girl_id_href_has_display_name(self):
        self.assertEqual(self.parse([{
            "source": "/shop/auth?shopdir=s_apronlady&girl_id=123&next=x",
            "display": "Current Cast",
        }]), [{"girl_id": "123", "display_name": "Current Cast"}])

    def test_legacy_member_id_href(self):
        self.assertEqual(self.parse([{
            "source": "/C2GirlDetail.php?member_id=456", "display": "Legacy Cast",
        }])[0]["girl_id"], "456")

    def test_duplicate_id_is_deduplicated(self):
        self.assertEqual(len(self.parse([
            {"source": "/shop/auth?girl_id=123", "display": "First"},
            {"source": "/C2GirlDetail.php?member_id=123", "display": "Second"},
        ])), 1)

    def test_malformed_or_missing_id_yields_source_missing_candidate_set(self):
        malformed = self.parse([
            {"source": "/shop/auth?girl_id=abc", "display": "Bad"},
            {"source": "/shop/auth?other_id=123", "display": "Missing"},
        ])
        self.assertEqual(malformed, [])
        reader = Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        completed = subprocess.run(
            ["node", "-e", "const r=require(process.argv[1]);try{r.requireCandidates(new Map())}catch(e){console.log(e.message)}", str(reader)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.stdout.strip(), "SOURCE_MISSING")


class DailyReaderTimeoutTests(unittest.TestCase):
    def test_timeout_uses_actual_cast_count_and_stays_bounded(self):
        reader = Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        script = (
            "const r=require(process.argv[1]);"
            "const values=[1,11,35,100].map(n=>r.timeoutForCastCount(n));"
            "let invalid='';try{r.timeoutForCastCount(0)}catch(e){invalid=e.message}"
            "console.log(JSON.stringify({values,invalid}))"
        )
        completed = subprocess.run(["node", "-e", script, str(reader)], capture_output=True,
                                   text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["values"], [28000, 108000, 300000, 300000])
        self.assertEqual(result["invalid"], "SOURCE_MISSING")
        self.assertGreater(READER_SUBPROCESS_TIMEOUT_SECONDS * 1000, max(result["values"]))


class DailyReaderHeaderTests(unittest.TestCase):
    def test_three_header_rows_exclude_date_and_numeric_data_rows(self):
        reader = Path(__file__).parents[1] / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        script = r"""
const assert = require('node:assert/strict');
const reader = require(process.argv[1]);
// Exercise the same self-contained function injected into Runtime.evaluate.
const extract = (0, eval)('(' + reader.headerPathsForTable.toString() + ')');
const cell = (text, rowspan=1, colspan=1, tagName='TH') => ({
  textContent:text, tagName,
  getAttribute:name => String(name === 'rowspan' ? rowspan : colspan),
});
const headers = [
  [cell('2026年 9月',3), cell(''), cell('アクセス状況',1,2), cell('写メ日記')],
  [cell('総数',2), cell('パソコン',2), cell('スマホ',2), cell('投稿数')],
  [cell('日次')],
];
const expected = ['2026年 9月','総数','アクセス状況 > パソコン','アクセス状況 > スマホ','写メ日記 > 投稿数 > 日次'];
const table = rows => ({querySelectorAll:selector => {
  assert.equal(selector,'tr'); return rows.map(children => ({children}));
}});
const dataRows = [
  [cell('9/1(火)'), ...['60','8','52','2'].map(x=>cell(x,1,1,'TD'))],
  ['9/1(火)','60','8','52','2'].map(x=>cell(x)),
  ['60','8','52','2'].map(x=>cell(x)),
  ['60','8','52','2'].map(x=>cell(x,1,1,'TD')),
];
for (const data of dataRows) {
  assert.deepEqual(extract(table([...headers,data,[cell('後続の集計')]])),expected);
}
assert.throws(()=>extract(table([dataRows[0]])),/SCHEMA_DRIFT/);
process.stdout.write('PASS');
"""
        completed = subprocess.run(["node", "-e", script, str(reader)], capture_output=True,
                                   text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "PASS")


if __name__ == "__main__":
    unittest.main()
