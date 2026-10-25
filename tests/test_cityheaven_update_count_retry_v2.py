from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"
from tempfile import TemporaryDirectory
from unittest import mock

from src.windows_execution_foundation import cityheaven_daily_metrics as daily
from src.windows_execution_foundation import cityheaven_update_count_retry_v2 as v2
from src.windows_execution_foundation.cityheaven_daily_metrics import CollectorError
from src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2 import FAILURE

DATE = "2026-10-20"
HEADERS = [
    "アクセス状況 > アクセス数 > 合計", "アクセス状況 > PC", "アクセス状況 > SP",
    "マイガール数", "unused-4", "unused-5", "口コミ数", "ミテネ数", "unused-8",
    "オキニトーク > 受信人数", "オキニトーク > 送信数", "unused-11", "写メ日記投稿数",
]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class RetryV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "WindowsExecutionFoundation" / "cityheaven-daily"
        self.root.mkdir(parents=True)
        self._patch = mock.patch.object(daily, "DEFAULT_RUNTIME", self.root)
        self._patch.start()
        self.config = Path(self._tmp.name) / "config.json"
        self.config.write_text(json.dumps({
            "state_dir": str(self.root),
            "cityheaven_daily": {"browser": "RESERVATION_CHROME_REUSE"},
            "cityheaven_reservation": {
                "cdp_port": 9227,
                "chrome_executable": r"C:\Users\epuro\AppData\Local\Google\Chrome\Application\chrome.exe",
                "dedicated_profile_path": r"C:\Users\epuro\CityHeavenAI\ReservationRuntimeChromeProfile",
                "reader_path": r"C:\Readers\reservation.js",
                "node_executable": r"C:\Node\node.exe",
                "task_user_id": "S-1-5-21-3349146081-1340512296-21348510-1002",
            },
        }), encoding="utf-8")
        self.events = []
        self.releases = []

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def eligible_morning(self):
        values = ["10", "2", "8", "3", "0", "0", "1", "---", "0", "---", "---", "0", "7"]
        payload = {
            "target_date": DATE, "read_only": True, "cityheaven_write_count": 0,
            "source_pages": ["C2GirlList", "C2TokeiGirlDetail"],
            "casts": [{
                "girl_id": "101", "display_name": "cast-0",
                "header_paths": list(HEADERS), "values": values,
            }],
        }
        raw = json.dumps(payload).encode("utf-8")
        source = self.root / "offline-source" / "sources" / "20261020.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(raw)
        proof = {"target_date": DATE, "sha256": hashlib.sha256(raw).hexdigest()}
        write_json(source.with_name("20261020.sha256.json"), proof)
        write_json(self.root / "offline-source" / "analysis" / "20261020.json", {
            "status": "PARTIAL", "numeric_cell_count": 6,
            "missing_cells": [
                {"metric": metric, "raw_value": "---", "reason": "SOURCE_VALUE_MISSING"}
                for metric in ("mitene", "okini_people", "okini_sends")
            ],
        })
        write_json(self.root / "receipts" / "20261020.json", {
            "schema_version": daily.SCHEMA, "target_date": DATE, "status": "FAILED",
            "failure_code": "SOURCE_MISSING", "partial_analysis_status": "PARTIAL",
            "auth_status": "PASS", "cityheaven_fetch_count": 1, "source_payload_persisted": True,
        })

    def ensure_ok(self, configuration, session=None):
        self.events.append("ensure")
        return {"state": "MATCH", "relaunch_count": 0}

    def ensure_fail(self, configuration, session=None):
        self.events.append("ensure")
        raise CollectorError(FAILURE)

    def fetch(self, date):
        self.events.append("fetch")
        raise CollectorError("SOURCE_MISSING")

    def release_fn(self, report):
        self.releases.append("release")
        return {"closed": True, "profile_kept": True}

    def test_morning_mismatch_does_not_fetch_or_restart_chrome(self):
        write_json(self.root / "receipts" / "20261020.json", {
            "schema_version": daily.SCHEMA, "status": "FAILED", "failure_code": FAILURE,
            "target_date": DATE, "source_payload_persisted": False, "auth_status": "NOT_CHECKED",
            "reader_run_count": 0, "fetched_at": None, "cdp_port": None,
            "cityheaven_write_count": 0, "airtable_write_count": 0, "business_write_count": 0,
        })
        self.assertEqual(v2.eligibility_v2(self.root, DATE), (False, "MORNING_CDP_MISMATCH_NO_CONTACT"))
        record = v2.retry_v2(
            DATE, runtime=self.root, fetcher=self.fetch, ensure_fn=self.ensure_fail,
            release_fn=self.release_fn, config=self.config,
        )
        self.assertEqual(record["result"], "NOT_ELIGIBLE")
        self.assertEqual(record["reason_code"], "MORNING_CDP_MISMATCH_NO_CONTACT")
        self.assertEqual(self.events, [])
        self.assertEqual(self.releases, [])

    def test_pre_contact_mismatch_claim_is_not_the_consumed_retry(self):
        self.eligible_morning()
        claim = self.root / "update-count-retry" / "20261020.json"
        write_json(claim, {
            "schema_version": "cityheaven_update_count_retry/1.0", "target_date": DATE,
            "result": "FAILED", "reason_code": FAILURE, "airtable_write_count": 0,
            "cityheaven_fetch_count": 1,
        })
        record = v2.retry_v2(
            DATE, runtime=self.root, fetcher=self.fetch, ensure_fn=self.ensure_ok,
            release_fn=self.release_fn, config=self.config,
        )
        self.assertEqual(self.events, ["ensure", "fetch"])
        self.assertEqual(self.releases, ["release"])
        self.assertEqual(record["reason_code"], "SOURCE_MISSING")
        self.assertTrue(record["mismatch_claim_released"])
        archived = list((claim.parent / "history" / "20261020").glob("mismatch-before-retry-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding="utf-8"))["reason_code"], FAILURE)

    def test_claim_that_already_fetched_is_not_released_or_ensured(self):
        self.eligible_morning()
        write_json(self.root / "update-count-retry" / "20261020.json", {
            "result": "FAILED", "reason_code": FAILURE, "airtable_write_count": 0,
            "retry_dash_columns": {"7": 1}, "cityheaven_fetch_count": 1,
        })
        record = v2.retry_v2(
            DATE, runtime=self.root, fetcher=self.fetch, ensure_fn=self.ensure_fail,
            release_fn=self.release_fn, config=self.config,
        )
        self.assertEqual(record["result"], "ALREADY_RETRIED")
        self.assertEqual(self.events, [])
        self.assertEqual(self.releases, [])
        self.assertFalse(record["mismatch_claim_released"])

    def test_ensure_failure_does_not_call_the_fetcher_and_stays_reusable(self):
        self.eligible_morning()

        def fetch(date):
            raise AssertionError("cityheaven")

        record = v2.retry_v2(
            DATE, runtime=self.root, fetcher=fetch, ensure_fn=self.ensure_fail,
            release_fn=self.release_fn, config=self.config,
        )
        self.assertEqual(self.events, ["ensure"])
        self.assertEqual(self.releases, ["release"])
        self.assertEqual(record["reason_code"], FAILURE)
        claim = json.loads((self.root / "update-count-retry" / "20261020.json").read_text(encoding="utf-8"))
        self.assertTrue(v2.mismatch_claim_reusable(claim))
        self.assertNotIn("retry_dash_columns", claim)

    def test_check_only_and_dry_run_do_not_retry(self):
        write_json(self.root / "receipts" / "20261020.json", {
            "schema_version": daily.SCHEMA, "status": "FAILED", "failure_code": FAILURE,
            "target_date": DATE, "source_payload_persisted": False, "auth_status": "NOT_CHECKED",
            "reader_run_count": 0,
        })
        code = v2.main(["--check-only", "--date", DATE, "--runtime", str(self.root)])
        self.assertEqual(code, 0)
        with mock.patch.object(v2, "retry_v2", side_effect=AssertionError("retry")):
            dry = v2.main(
                ["--dry-run", "--date", DATE, "--config", str(self.config), "--runtime", str(self.root)],
                ensure_fn=self.ensure_ok, release_fn=self.release_fn,
            )
        self.assertEqual(dry, 0)
        self.assertEqual(self.events, ["ensure"])
        self.assertEqual(self.releases, ["release"])

    def test_fetcher_error_still_releases_the_hold(self):
        self.eligible_morning()

        def blocked(date):
            self.events.append("fetch")
            raise RuntimeError("403")

        with self.assertRaises(RuntimeError):
            v2.retry_v2(
                DATE, runtime=self.root, fetcher=blocked, ensure_fn=self.ensure_ok,
                release_fn=self.release_fn, config=self.config,
            )
        self.assertEqual(self.events, ["ensure", "fetch"])
        self.assertEqual(self.releases, ["release"])


if __name__ == "__main__":
    unittest.main()
