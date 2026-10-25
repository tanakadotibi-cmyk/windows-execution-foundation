"""Offline tests for the CityHeaven daily Sheets writer. No browser, network, or DPAPI."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import types
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import unquote

from tests.offline_guard import install

install()

from src.windows_execution_foundation import cityheaven_daily_metrics as daily
from src.windows_execution_foundation import cityheaven_daily_metrics_v4 as v4

ROOT = Path(__file__).resolve().parents[1]
NEW_FILES = [
    ROOT / "src/windows_execution_foundation/cityheaven_daily_metrics_v4.py",
    ROOT / "src/windows_execution_foundation/cityheaven_update_count_retry_v4.py",
    ROOT / "run-cityheaven-daily-metrics-v4.py",
    ROOT / "run-cityheaven-daily-metrics-logged-v4.py",
    ROOT / "run-cityheaven-update-count-retry-v4.py",
    ROOT / "run-cityheaven-update-count-retry-logged-v4.py",
]
SECRET = "-----BEGIN PRIVATE KEY-----\nSUPERSECRETv4KEY9876543210\n-----END PRIVATE KEY-----\n"


class MemorySheets:
    def __init__(self, *, production: bool = True):
        self.tabs: dict[str, list[list]] = {}
        self.appends: list[tuple[str, list]] = []
        self.created: list[str] = []
        if production:
            for name in v4.PRODUCTION_TABS:
                self.tabs[name] = [list(v4.COLUMNS[name])]

    def titles(self) -> list[str]:
        return list(self.tabs)

    def values(self, tab: str, columns: list[str]) -> list[list]:
        if tab not in self.tabs:
            raise v4.SheetError("SHEET_TAB_MISSING")
        return self.tabs[tab]

    def append_rows(self, tab: str, columns: list[str], rows: list[list]) -> None:
        self.appends.append((tab, rows))
        self.tabs.setdefault(tab, [list(columns)]).extend(rows)

    def add_sheet_with_header(self, tab: str, header: list[str]) -> None:
        if tab in v4.PRODUCTION_TABS:
            raise v4.SheetError("TEST_TAB_REJECTED")
        self.created.append(tab)
        self.tabs[tab] = [list(header)]


class Resp:
    def __init__(self, code: int, payload: dict):
        self.status_code = code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeGoogle:
    def __init__(self):
        self.titles_now: list[str] = []
        self.values: dict[str, list] = {}
        self.posts: list[tuple] = []
        self.puts: list[tuple] = []

    def get(self, url, params=None, timeout=None):
        if params and "fields" in params:
            return Resp(200, {"sheets": [{"properties": {"title": title}} for title in self.titles_now]})
        tab = unquote(url.split("/values/")[1].split("!")[0])
        return Resp(200, {"values": self.values.get(tab, [])})

    def post(self, url, params=None, json=None, timeout=None):
        self.posts.append((url, params, json))
        if url.endswith(":batchUpdate"):
            title = json["requests"][0]["addSheet"]["properties"]["title"]
            if title in v4.PRODUCTION_TABS:
                raise AssertionError("production tab created")
            self.titles_now.append(title)
            self.values.setdefault(title, [])
        elif ":append" in url:
            tab = unquote(url.split("/values/")[1].split("!")[0])
            self.values.setdefault(tab, []).extend(json["values"])
        return Resp(200, {})

    def put(self, url, params=None, json=None, timeout=None):
        self.puts.append((url, params, json))
        tab = unquote(url.split("/values/")[1].split("!")[0])
        self.values[tab] = list(json["values"])
        return Resp(200, {})


def cells(**overrides):
    row = {metric: 0 for metric in daily.METRICS}
    row.update(overrides)
    return row


def make_analysis(date: str, rows_by_girl: dict, roster: str = "UNVERIFIED") -> dict:
    numeric, missing = [], []
    metrics = {}
    for metric in daily.METRICS:
        values = []
        missing_count = 0
        for girl, metric_values in rows_by_girl.items():
            value = metric_values[metric]
            if value is None:
                missing.append({
                    "girl_id": str(girl), "metric": metric,
                    "raw_value": "---", "reason": "SOURCE_VALUE_MISSING",
                })
                missing_count += 1
            else:
                numeric.append({"girl_id": str(girl), "metric": metric, "value": int(value)})
                values.append(int(value))
        metrics[metric] = {
            "observed_sum": sum(values) if values else None,
            "numeric_cast_count": len(values),
            "missing_cast_count": missing_count,
            "input_cast_count": len(rows_by_girl),
        }
    if not numeric:
        status = "NO_USABLE_VALUES"
    elif missing:
        status = "PARTIAL"
    else:
        status = "COMPLETE"
    return {
        "target_date": date,
        "status": status,
        "cast_count": len(rows_by_girl),
        "numeric_cell_count": len(numeric),
        "missing_cell_count": len(missing),
        "numeric_rows": numeric,
        "missing_cells": missing,
        "metrics": metrics,
        "roster_coverage_status": roster,
        "display_name": "花子",
    }


def parallel_tables(analysis, raw, run_id, written_at, writer):
    """Row formula copied from box_parallel/dryrun_build_rows.build_city."""
    biz, version = "biz_epron_lady", "cityheaven_daily_v1"
    revision = {"revision": 1, "is_current": "TRUE", "superseded_by_revision": "", "correction_reason": ""}
    common = {"run_id": run_id, "written_at": written_at, "writer": writer}
    detail = []
    day = analysis["target_date"]
    for source, state, value_of in (
        (analysis["numeric_rows"], "CONFIRMED", lambda row: row["value"]),
        (analysis.get("missing_cells", []), "UNKNOWN", lambda row: ""),
    ):
        for row in source:
            metric_key = f"cityheaven_unit_{row['metric']}_daily_v1"
            identity = f"{biz}|cityheaven_girl:{row['girl_id']}|{day}|{metric_key}|{version}"
            detail.append({
                "dedup_key": identity, **revision,
                "metric_record_id": "bm_cityheaven_daily_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                "business_date": day, "girl_id": str(row["girl_id"]), "store_alias": "",
                "metric": row["metric"], "value": value_of(row), "value_state": state,
                "header_sha256": "", "raw_source_sha256": raw, **common,
            })
    summary = []
    for metric, stats in sorted(analysis["metrics"].items()):
        summary.append({
            "summary_key": f"{biz}|{day}|{metric}|{version}", **revision,
            "business_date": day, "metric": metric, "observed_sum": stats["observed_sum"],
            "numeric_cast_count": stats["numeric_cast_count"],
            "missing_cast_count": stats["missing_cast_count"],
            "input_cast_count": stats["input_cast_count"],
            "roster_coverage_status": analysis.get("roster_coverage_status", ""),
            "status": analysis.get("status", ""), **common,
        })
    return detail, summary


def write_analysis(directory: Path, analysis: dict) -> Path:
    path = directory / "analysis.json"
    path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    return path


class DailyV4Tests(unittest.TestCase):
    def setUp(self):
        v4.get_scrubber().clear()

    def test_guard_blocks_network_and_dpapi(self):
        import ctypes
        import socket
        with self.assertRaises(AssertionError):
            subprocess.run(["python", "-V"])
        with self.assertRaises(AssertionError):
            socket.socket()
        with self.assertRaises(AssertionError):
            socket.create_connection(("127.0.0.1", 9))
        with self.assertRaises(AssertionError):
            ctypes.windll.crypt32

    def test_sources_do_not_name_airtable_clients(self):
        banned = ("airtable_request", "airtable_local_exec", "api.airtable.com")
        for path in NEW_FILES:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, path.name)

    def test_columns_match_sheet_headers(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sheet_headers_ref", ROOT / "box_parallel" / "sheet_headers.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(list(v4.DETAIL_COLUMNS), module.HEADERS["city_metric_detail"])
        self.assertEqual(list(v4.SUMMARY_COLUMNS), module.HEADERS["city_daily_summary"])
        self.assertEqual(list(v4.RUN_LOG_COLUMNS), module.HEADERS["run_log"])
        self.assertEqual(v4.SCOPES, ["https://www.googleapis.com/auth/spreadsheets"])
        self.assertEqual(v4.DEFAULT_CREDENTIAL, Path(r"C:\TantanJobs\secrets\sheets_writer_sa.dpapi"))
        self.assertEqual(v4.EXPECTED_CLIENT_EMAIL, "<SA_EMAIL>")

    def test_rows_match_parallel_builder_and_keep_zero_and_unknown(self):
        analysis = make_analysis("2026-10-21", {
            "37993209": cells(access=102, pc=0),
            "44789010": cells(access=3, mitene=None),
        })
        self.assertEqual(analysis["status"], "PARTIAL")
        built = v4.build_rows(
            analysis, raw_source_sha256="abc123", run_id="RID", written_at="T", writer="cityheaven_daily_v4",
        )
        expected_detail, expected_summary = parallel_tables(
            analysis, "abc123", "RID", "T", "cityheaven_daily_v4",
        )
        self.assertEqual(
            sorted(built["city_metric_detail"], key=lambda row: row["dedup_key"]),
            sorted(expected_detail, key=lambda row: row["dedup_key"]),
        )
        self.assertEqual(
            sorted(built["city_daily_summary"], key=lambda row: row["summary_key"]),
            sorted(expected_summary, key=lambda row: row["summary_key"]),
        )
        unknown = [row for row in built["city_metric_detail"] if row["value_state"] == "UNKNOWN"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["value"], "")
        self.assertNotEqual(unknown[0]["value"], 0)
        zeros = [row for row in built["city_metric_detail"] if row["metric"] == "pc" and row["girl_id"] == "37993209"]
        self.assertEqual(zeros[0]["value"], 0)
        self.assertEqual(zeros[0]["value_state"], "CONFIRMED")
        dumped = json.dumps(built, ensure_ascii=False)
        self.assertNotIn("花子", dumped)
        access = next(row for row in built["city_daily_summary"] if row["metric"] == "access")
        self.assertEqual(access["observed_sum"], 105)
        # One cast has a confirmed mitene of 0 and one is UNKNOWN, so the sum stays 0.
        mitene = next(row for row in built["city_daily_summary"] if row["metric"] == "mitene")
        self.assertEqual(mitene["observed_sum"], 0)
        blank = v4.build_rows(
            make_analysis("2026-10-21", {"37993209": cells(mitene=None)}),
            raw_source_sha256="abc123", run_id="RID", written_at="T",
        )
        blank_mitene = next(row for row in blank["city_daily_summary"] if row["metric"] == "mitene")
        self.assertIsNone(blank_mitene["observed_sum"])

    def test_receipt_example_feeds_run_metadata(self):
        path = ROOT / "logs_example" / "receipt_20261021.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        meta = v4.meta_from_receipt(receipt, path.read_bytes())
        self.assertEqual(meta["collector_sha256"], receipt["reader_sha256"])
        self.assertEqual(meta["raw_source_sha256"], receipt["raw_source_sha256"])
        self.assertEqual(meta["receipt_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt["metric_count"], 279)
        self.assertEqual(receipt["cast_count"], 31)
        girl = receipt["schema_mapping_evidence"][0]["girl_id"]
        analysis = make_analysis(receipt["target_date"], {girl: cells(access=4)})
        rows = v4.build_rows(analysis, raw_source_sha256=meta["raw_source_sha256"], run_id="RID", written_at="T")
        access = next(row for row in rows["city_metric_detail"] if row["girl_id"] == girl and row["metric"] == "access")
        self.assertTrue(access["dedup_key"].startswith(f"biz_epron_lady|cityheaven_girl:{girl}|2026-10-21|"))
        self.assertEqual(access["raw_source_sha256"], receipt["raw_source_sha256"])

    def test_live_collection_does_not_ask_for_an_external_write(self):
        seen = {}
        original = v4.v2.run_v2

        def fake_run_v2(*args, **kwargs):
            seen["write_airtable"] = kwargs["write_airtable"]
            seen["request"] = kwargs["request"]
            return {"status": "FAILED", "failure_code": "SOURCE_MISSING", "target_date": args[0]}

        v4.v2.run_v2 = fake_run_v2
        try:
            result = v4.run_v4("2020-03-03", transport=MemorySheets())
        finally:
            v4.v2.run_v2 = original
        self.assertIs(seen["write_airtable"], False)
        self.assertIs(seen["request"], v4._forbid_external_write)
        self.assertEqual(result["airtable_write_count"], 0)
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertTrue(result["browser_closed"])

    def test_airtable_functions_are_not_called(self):
        called = []
        original_request = daily.airtable_request
        original_write = daily.write_metrics

        def boom(*_args, **_kwargs):
            called.append("airtable")
            raise AssertionError("airtable called")

        daily.airtable_request = boom
        daily.write_metrics = boom
        try:
            analysis = make_analysis("2020-03-04", {"37993209": cells(access=2)})
            with tempfile.TemporaryDirectory() as tmp:
                path = write_analysis(Path(tmp), analysis)
                result = v4.run_v4(
                    "2020-03-04", offline_analysis=path, transport=MemorySheets(), mode="MANUAL",
                )
        finally:
            daily.airtable_request = original_request
            daily.write_metrics = original_write
        self.assertEqual(called, [])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["airtable_write_count"], 0)
        self.assertGreater(result["sheet_write_count"], 0)

    def test_dry_run_prints_rows_and_does_not_append(self):
        analysis = make_analysis("2020-03-05", {"37993209": cells(access=7), "44789010": cells(access=1)})
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_analysis(root, analysis)
            original = v4.open_transport
            v4.open_transport = lambda *_args, **_kwargs: sheets
            try:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = v4.main([
                        "--date", "2020-03-05", "--offline-analysis", str(path),
                        "--dry-run", "--v4-runtime", str(root / "v4"), "--mode", "MANUAL",
                    ])
            finally:
                v4.open_transport = original
            self.assertEqual(code, 0)
            self.assertEqual(sheets.appends, [])
            self.assertEqual(sheets.created, [])
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["sheet_write_count"], 0)
            self.assertEqual(payload["airtable_write_count"], 0)
            self.assertTrue(payload["browser_closed"])
            self.assertTrue(payload["dry_run"])
            self.assertGreater(payload["preview"]["city_metric_detail"]["planned"], 0)
            self.assertEqual(payload["preview"]["city_metric_detail"]["new"], 18)
            dry_file = root / "v4" / "dry-run" / "20200305.json"
            self.assertTrue(dry_file.is_file())
            saved = json.loads(dry_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["preview"]["city_metric_detail"]["rows"][0]["business_date"], "2020-03-05")
            self.assertIn("dedup_key", buffer.getvalue())

    def test_same_day_second_run_appends_nothing(self):
        analysis = make_analysis("2020-03-06", {"37993209": cells(access=4), "44789010": cells(reviews=2)})
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_analysis(Path(tmp), analysis)
            first = v4.run_v4("2020-03-06", offline_analysis=path, transport=sheets, mode="MANUAL")
            appended = len(sheets.appends)
            second = v4.run_v4("2020-03-06", offline_analysis=path, transport=sheets, mode="MANUAL")
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(first["sheet_write_count"], 18 + 9 + 1)
        self.assertGreater(appended, 0)
        self.assertEqual(second["status"], "PASS")
        self.assertEqual(second["sheet_write_count"], 0)
        self.assertEqual(len(sheets.appends), appended)
        self.assertEqual(second["preview"]["city_metric_detail"]["new"], 0)
        self.assertEqual(second["preview"]["city_metric_detail"]["replay"], 18)

    def test_value_mismatch_stops_without_append(self):
        analysis = make_analysis("2020-03-07", {"37993209": cells(access=4)})
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_analysis(Path(tmp), analysis)
            first = v4.run_v4("2020-03-07", offline_analysis=path, transport=sheets, mode="MANUAL")
            self.assertEqual(first["status"], "PASS")
            detail_rows = sheets.tabs["city_metric_detail"]
            value_index = v4.DETAIL_COLUMNS.index("value")
            metric_index = v4.DETAIL_COLUMNS.index("metric")
            for row in detail_rows[1:]:
                if row[metric_index] == "access":
                    row[value_index] = 999
                    break
            before = len(sheets.appends)
            second = v4.run_v4("2020-03-07", offline_analysis=path, transport=sheets, mode="MANUAL")
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(second["failure_code"], "SHEET_CONTENT_CONFLICT")
        self.assertEqual(len(sheets.appends), before)
        self.assertEqual(second["sheet_write_count"], 0)

    def test_header_mismatch_stops_without_append(self):
        analysis = make_analysis("2020-03-08", {"37993209": cells(access=1)})
        sheets = MemorySheets()
        sheets.tabs["city_metric_detail"] = [["wrong"]]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_analysis(Path(tmp), analysis)
            result = v4.run_v4("2020-03-08", offline_analysis=path, transport=sheets, mode="MANUAL")
        self.assertEqual(result["failure_code"], "SHEET_SCHEMA_DRIFT")
        self.assertEqual(sheets.appends, [])
        self.assertEqual(result["sheet_write_count"], 0)

    def test_unknown_is_blank_on_the_sheet(self):
        analysis = make_analysis("2020-03-09", {"37993209": cells(mitene=None, access=5)})
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_analysis(Path(tmp), analysis)
            result = v4.run_v4("2020-03-09", offline_analysis=path, transport=sheets, mode="MANUAL")
        self.assertEqual(result["status"], "PASS")
        self.assertGreater(result["unknown_count"], 0)
        value_index = v4.DETAIL_COLUMNS.index("value")
        state_index = v4.DETAIL_COLUMNS.index("value_state")
        metric_index = v4.DETAIL_COLUMNS.index("metric")
        unknown_rows = [
            row for row in sheets.tabs["city_metric_detail"][1:]
            if row[state_index] == "UNKNOWN"
        ]
        self.assertEqual(len(unknown_rows), 1)
        self.assertEqual(unknown_rows[0][metric_index], "mitene")
        self.assertEqual(unknown_rows[0][value_index], "")
        self.assertNotEqual(unknown_rows[0][value_index], 0)
        sum_index = v4.SUMMARY_COLUMNS.index("observed_sum")
        metric_summary = v4.SUMMARY_COLUMNS.index("metric")
        mitene = next(row for row in sheets.tabs["city_daily_summary"][1:] if row[metric_summary] == "mitene")
        self.assertEqual(mitene[sum_index], "")

    def test_test_tab_rejects_production_names_and_creates_a_side_tab(self):
        for name in ("city_metric_detail", "city_daily_summary", "run_log", "", "city_metric_detail "):
            with self.assertRaises(v4.SheetError):
                v4.resolve_tabs(name)
        result = v4.run_v4("2020-03-10", test_tab="city_metric_detail", transport=MemorySheets())
        self.assertEqual(result["failure_code"], "TEST_TAB_REJECTED")
        sheets = MemorySheets()
        analysis = make_analysis("2020-03-10", {"37993209": cells(access=6)})
        with tempfile.TemporaryDirectory() as tmp:
            path = write_analysis(Path(tmp), analysis)
            written = v4.run_v4(
                "2020-03-10", offline_analysis=path, transport=sheets, test_tab="try1", mode="MANUAL",
            )
        self.assertEqual(written["status"], "PASS")
        self.assertEqual(sorted(sheets.created), [
            "city_daily_summary__try1", "city_metric_detail__try1", "run_log__try1",
        ])
        self.assertEqual([name for name, _rows in sheets.appends], [
            "city_metric_detail__try1", "city_daily_summary__try1", "run_log__try1",
        ])
        self.assertEqual(len(sheets.tabs["city_metric_detail"]), 1)
        self.assertGreater(len(sheets.tabs["city_metric_detail__try1"]), 1)

    def test_exception_during_collection_still_releases_the_browser(self):
        released = []
        runtime = daily.DEFAULT_RUNTIME / "v4-unit" / f"browser-release-{uuid.uuid4().hex}"
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "cityheaven_daily": {"browser": "RESERVATION_CHROME_REUSE"},
            }), encoding="utf-8")

            def ensure_fn(_configuration, session=None):
                return {"state": "MATCH", "cdp_port": 9227, "action": "reused", "relaunch_count": 0}

            def release_fn(report):
                released.append(report)
                return {"closed": True, "skipped_chrome_pids": [111], "job": "daily"}

            def fetcher(_date):
                raise RuntimeError("collector blew up")

            result = v4.run_v4(
                "2020-02-02",
                runtime=runtime,
                config=config,
                fetcher=fetcher,
                ensure_fn=ensure_fn,
                release_fn=release_fn,
                transport=MemorySheets(),
            )
        self.assertEqual(len(released), 1)
        self.assertTrue(result["browser_closed"])
        self.assertEqual(result["skipped_chrome_pids"], [111])
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertEqual(result["airtable_write_count"], 0)
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("collector blew up", json.dumps(result))

    def test_credential_material_is_not_in_errors_or_output(self):
        captured = {}

        class Creds:
            def __init__(self):
                self.scopes = list(captured["scopes"])

            @staticmethod
            def from_service_account_info(info, scopes=None):
                captured["scopes"] = list(scopes or [])
                if "SUPERSECRETv4KEY" in str(info.get("private_key")):
                    raise RuntimeError(info["private_key"])
                return Creds()

        class AuthorizedSession:
            def __init__(self, creds):
                self.creds = creds

        previous = {}
        modules = {
            "google": types.ModuleType("google"),
            "google.oauth2": types.ModuleType("google.oauth2"),
            "google.oauth2.service_account": types.ModuleType("google.oauth2.service_account"),
            "google.auth": types.ModuleType("google.auth"),
            "google.auth.transport": types.ModuleType("google.auth.transport"),
            "google.auth.transport.requests": types.ModuleType("google.auth.transport.requests"),
        }
        modules["google"].__path__ = []
        modules["google.oauth2"].__path__ = []
        modules["google.auth"].__path__ = []
        modules["google.auth.transport"].__path__ = []
        modules["google"].oauth2 = modules["google.oauth2"]
        modules["google.oauth2"].service_account = modules["google.oauth2.service_account"]
        modules["google.auth"].transport = modules["google.auth.transport"]
        modules["google.auth.transport"].requests = modules["google.auth.transport.requests"]
        modules["google.oauth2.service_account"].Credentials = Creds
        modules["google.auth.transport.requests"].AuthorizedSession = AuthorizedSession
        import sys
        for name, module in modules.items():
            previous[name] = sys.modules.get(name)
            sys.modules[name] = module
        try:
            def decrypt(_data):
                return json.dumps({
                    "type": "service_account",
                    "client_email": v4.EXPECTED_CLIENT_EMAIL,
                    "private_key": SECRET,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }).encode()

            with tempfile.TemporaryDirectory() as tmp:
                credential = Path(tmp) / "sheets_writer_sa.dpapi"
                credential.write_bytes(b"ciphertext-placeholder")
                with self.assertRaises(v4.SheetError) as raised:
                    v4.open_authorized_session(credential, decrypt=decrypt)
            self.assertEqual(raised.exception.code, "CREDENTIAL_AUTH_FAILED")
            self.assertNotIn("SUPERSECRETv4KEY", str(raised.exception))
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertEqual(captured["scopes"], v4.SCOPES)
            redacted = v4.get_scrubber().apply(json.dumps({"private_key": SECRET}))
            self.assertNotIn("SUPERSECRETv4KEY", redacted)
            self.assertIn("[REDACTED]", redacted)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_google_transport_refuses_a_drifted_header_and_can_create_a_test_tab(self):
        analysis = make_analysis("2020-03-11", {"37993209": cells(access=8)})
        drifted = FakeGoogle()
        drifted.titles_now = list(v4.PRODUCTION_TABS)
        for name in v4.PRODUCTION_TABS:
            drifted.values[name] = [["wrong"]]
        with self.assertRaises(v4.SheetError) as raised:
            v4.publish_analysis(
                analysis,
                meta=v4.meta_from_receipt({}),
                mode="MANUAL",
                written_at="2020-03-12T00:00:00+09:00",
                titles={name: name for name in v4.PRODUCTION_TABS},
                transport=v4.GoogleSheetTransport(drifted),
                dry_run=False,
                create_missing=False,
                allow_partial=False,
            )
        self.assertEqual(raised.exception.code, "SHEET_SCHEMA_DRIFT")
        self.assertEqual(drifted.posts, [])

        created = FakeGoogle()
        outcome = v4.publish_analysis(
            analysis,
            meta={"raw_source_sha256": "abc", "failure_code": "", "collector_sha256": "", "receipt_sha256": "", "started_at": "", "completed_at": ""},
            mode="MANUAL",
            written_at="2020-03-12T00:00:00+09:00",
            titles=v4.resolve_tabs("side"),
            transport=v4.GoogleSheetTransport(created),
            dry_run=False,
            create_missing=True,
            allow_partial=False,
        )
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(created.titles_now, [
            "city_metric_detail__side", "city_daily_summary__side", "run_log__side",
        ])
        self.assertTrue(any(params and params.get("valueInputOption") == "RAW" and params.get("insertDataOption") == "INSERT_ROWS" for _url, params, _body in created.posts))
        self.assertEqual(created.values["city_metric_detail__side"][0], list(v4.DETAIL_COLUMNS))
        self.assertNotIn("city_metric_detail", created.titles_now)

    def test_bad_date_exits_2(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = v4.main(["--date", "2020/03/01"])
        self.assertEqual(code, 2)
        self.assertIn("SOURCE_DATE_INVALID", buffer.getvalue())

    def test_pii_failure_does_not_append(self):
        analysis = make_analysis("2020-03-13", {"37993209": cells(access=1)})
        sheets = MemorySheets()
        original = v4.build_rows

        def tainted(*args, **kwargs):
            tables = original(*args, **kwargs)
            tables["city_metric_detail"][0]["correction_reason"] = "user@example.com"
            return tables

        v4.build_rows = tainted
        try:
            with self.assertRaises(v4.SheetError) as raised:
                v4.publish_analysis(
                    analysis,
                    meta=v4.meta_from_receipt({}),
                    mode="MANUAL",
                    written_at="2020-03-13T00:00:00+09:00",
                    titles={name: name for name in v4.PRODUCTION_TABS},
                    transport=sheets,
                    dry_run=False,
                    create_missing=False,
                    allow_partial=False,
                )
        finally:
            v4.build_rows = original
        self.assertEqual(raised.exception.code, "PII_SCAN_FAILED")
        self.assertEqual(sheets.appends, [])


if __name__ == "__main__":
    unittest.main()
