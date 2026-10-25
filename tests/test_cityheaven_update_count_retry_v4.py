"""Offline tests for the 14:30 v4 retry. No browser, network, or DPAPI."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from tests.offline_guard import install

install()

from src.windows_execution_foundation import cityheaven_daily_metrics as daily
from src.windows_execution_foundation import cityheaven_update_count_retry_v4 as retry_v4
from src.windows_execution_foundation.cityheaven_daily_metrics_v4 import PRODUCTION_TABS
from tests.test_cityheaven_daily_metrics_v4 import MemorySheets, cells, make_analysis


def _runtime(name: str) -> Path:
    path = daily.DEFAULT_RUNTIME / "v4-unit" / f"{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _headers() -> list[str]:
    headers = [""] * 13
    headers[0] = "アクセス合計"
    headers[1] = "pc"
    headers[2] = "sp"
    headers[3] = "マイガール"
    headers[6] = "口コミ"
    headers[7] = "ミテネ"
    headers[9] = "オキニ人数"
    headers[10] = "オキニ送信"
    headers[12] = "写メ日記"
    return headers


def _payload(date: str, *, dash: bool) -> dict:
    values = ["1"] * 13
    if dash:
        values[7] = "---"
    return {
        "target_date": date,
        "casts": [{
            "girl_id": "37993209",
            "display_name": "花子",
            "header_paths": _headers(),
            "values": values,
        }],
        "source_pages": ["C2GirlList", "C2TokeiGirlDetail"],
        "read_only": True,
        "cityheaven_write_count": 0,
    }


def _write_source(runtime: Path, date: str, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    stem = date.replace("-", "")
    source = runtime / "offline-source" / "sources"
    source.mkdir(parents=True, exist_ok=True)
    (source / f"{stem}.json").write_bytes(raw)
    proof = {"target_date": date, "sha256": hashlib.sha256(raw).hexdigest()}
    (source / f"{stem}.sha256.json").write_text(json.dumps(proof), encoding="utf-8")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class RetryV4Tests(unittest.TestCase):
    def test_check_only_reads_eligibility_without_sheets(self):
        runtime = _runtime("check-only")
        date = "2020-04-04"
        _write_json(runtime / "receipts" / "20200404.json", {
            "target_date": date, "status": "PASS", "failure_code": None,
            "schema_version": daily.SCHEMA,
        })
        sheets = MemorySheets()
        result = retry_v4.run_retry_v4(date, runtime=runtime, check_only=True, transport=sheets)
        self.assertEqual(result["status"], "PASS")
        self.assertIs(result["eligible"], False)
        self.assertEqual(result["reason_code"], "MORNING_NOT_FAILED")
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertEqual(result["airtable_write_count"], 0)
        self.assertTrue(result["browser_closed"])
        self.assertEqual(sheets.appends, [])

    def test_not_eligible_does_not_append(self):
        runtime = _runtime("not-eligible")
        date = "2020-04-05"
        _write_json(runtime / "receipts" / "20200405.json", {
            "target_date": date, "status": "PASS", "schema_version": daily.SCHEMA,
        })
        sheets = MemorySheets()
        result = retry_v4.run_retry_v4(date, runtime=runtime, transport=sheets)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["retry_result"], "NOT_ELIGIBLE")
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertEqual(sheets.appends, [])
        self.assertTrue(result["browser_closed"])
        self.assertEqual(result["airtable_write_count"], 0)

    def test_pass_writes_once_and_replays(self):
        runtime = _runtime("pass-write")
        date = "2020-04-06"
        analysis = make_analysis(date, {"37993209": cells(access=9)})
        _write_json(runtime / "offline-source" / "analysis" / "20200406.json", analysis)
        _write_json(runtime / "receipts" / "20200406.json", {
            "target_date": date, "status": "PASS", "reader_sha256": "abc",
            "raw_source_sha256": "def", "schema_version": daily.SCHEMA,
        })
        original = retry_v4.retry_v2

        def fake_retry(*_args, **_kwargs):
            return {"result": "PASS", "reason_code": None, "airtable_write_count": 0, "target_date": date}

        retry_v4.retry_v2 = fake_retry
        sheets = MemorySheets()
        try:
            first = retry_v4.run_retry_v4(date, runtime=runtime, transport=sheets)
            appended = len(sheets.appends)
            second = retry_v4.run_retry_v4(date, runtime=runtime, transport=sheets)
        finally:
            retry_v4.retry_v2 = original
        self.assertEqual(first["status"], "PASS")
        self.assertGreater(first["sheet_write_count"], 0)
        self.assertGreater(appended, 0)
        self.assertEqual(second["sheet_write_count"], 0)
        self.assertEqual(len(sheets.appends), appended)
        self.assertTrue(all(name in {f"{tab}" for tab in PRODUCTION_TABS} for name, _rows in sheets.appends))
        dumped = json.dumps(sheets.tabs, ensure_ascii=False)
        self.assertNotIn("花子", dumped)

    def test_dry_run_does_not_append(self):
        runtime = _runtime("dry-run")
        date = "2020-04-07"
        analysis = make_analysis(date, {"37993209": cells(mitene=None, access=2)})
        _write_json(runtime / "offline-source" / "analysis" / "20200407.json", analysis)
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            result = retry_v4.run_retry_v4(
                date, runtime=runtime, transport=sheets, dry_run=True, v4_runtime=Path(tmp),
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertEqual(sheets.appends, [])
        self.assertTrue(result["browser_closed"])
        self.assertGreater(result["preview"]["city_metric_detail"]["planned"], 0)
        unknown = [row for row in result["preview"]["city_metric_detail"]["rows"] if row["value_state"] == "UNKNOWN"]
        self.assertTrue(unknown)
        self.assertEqual(unknown[0]["value"], "")

    def test_exception_releases_the_browser_and_skips_sheets(self):
        runtime = _runtime("release")
        date = "2020-04-08"
        payload = _payload(date, dash=True)
        _write_source(runtime, date, payload)
        analysis = daily.analyze_partial_source(payload, date)
        self.assertEqual(analysis["status"], "PARTIAL")
        _write_json(runtime / "offline-source" / "analysis" / "20200408.json", analysis)
        _write_json(runtime / "receipts" / "20200408.json", {
            "target_date": date,
            "status": "FAILED",
            "failure_code": "SOURCE_MISSING",
            "partial_analysis_status": "PARTIAL",
            "auth_status": "PASS",
            "cityheaven_fetch_count": 1,
            "schema_version": daily.SCHEMA,
        })
        released = []
        sheets = MemorySheets()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"cityheaven_daily": {"browser": "RESERVATION_CHROME_REUSE"}}), encoding="utf-8")

            def ensure_fn(_configuration, session=None):
                return {"state": "MATCH", "cdp_port": 9227}

            def release_fn(report):
                released.append(True)
                return {"closed": True, "skipped_chrome_pids": [222], "job": "retry"}

            def fetcher(_date):
                raise RuntimeError("retry blew up")

            result = retry_v4.run_retry_v4(
                date, runtime=runtime, config=config, transport=sheets,
                ensure_fn=ensure_fn, release_fn=release_fn, fetcher=fetcher,
            )
        self.assertEqual(released, [True])
        self.assertTrue(result["browser_closed"])
        self.assertEqual(result["skipped_chrome_pids"], [222])
        self.assertEqual(result["sheet_write_count"], 0)
        self.assertEqual(sheets.appends, [])
        self.assertEqual(result["airtable_write_count"], 0)
        self.assertNotIn("retry blew up", json.dumps(result))

    def test_test_tab_rejects_a_production_name(self):
        result = retry_v4.run_retry_v4("2020-04-09", test_tab="run_log", check_only=True)
        self.assertEqual(result["failure_code"], "TEST_TAB_REJECTED")
        self.assertTrue(result["browser_closed"])

    def test_main_bad_date_and_check_only_stay_offline(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = retry_v4.main(["--date", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("SOURCE_DATE_INVALID", buffer.getvalue())
        runtime = _runtime("main-check")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = retry_v4.main(["--date", "2020-04-10", "--check-only", "--runtime", str(runtime)])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIs(payload["eligible"], False)
        self.assertEqual(payload["airtable_write_count"], 0)
        self.assertTrue(payload["browser_closed"])


if __name__ == "__main__":
    unittest.main()
