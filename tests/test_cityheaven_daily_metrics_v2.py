from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"

from src.windows_execution_foundation import cityheaven_daily_metrics as daily
from src.windows_execution_foundation import cityheaven_daily_metrics_v2 as v2
from src.windows_execution_foundation.cityheaven_daily_metrics import CollectorError
from src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2 import FAILURE, RealIoBlocked

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DATE = "2026-10-20"
HEADERS = [
    "アクセス状況 > アクセス数 > 合計", "アクセス状況 > PC", "アクセス状況 > SP",
    "マイガール数", "unused-4", "unused-5", "口コミ数", "ミテネ数", "unused-8",
    "オキニトーク > 受信人数", "オキニトーク > 送信数", "unused-11", "写メ日記投稿数",
]


def good_payload(date=DATE):
    return {
        "target_date": date,
        "read_only": True,
        "cityheaven_write_count": 0,
        "source_pages": ["C2GirlList", "C2TokeiGirlDetail"],
        "casts": [{
            "girl_id": "101",
            "display_name": "cast-0",
            "header_paths": list(HEADERS),
            "values": ["10", "2", "8", "3", "0", "0", "1", "4", "0", "5", "6", "0", "7"],
        }],
    }


def mismatch_receipt(date=DATE):
    return {
        "schema_version": daily.SCHEMA,
        "status": "FAILED",
        "failure_code": FAILURE,
        "target_date": date,
        "source_payload_persisted": False,
        "auth_status": "NOT_CHECKED",
        "reader_run_count": 0,
        "fetched_at": None,
        "source_fetched_at": None,
        "source_route": None,
        "route": None,
        "profile_identity_result": None,
        "runtime_identity_result": None,
        "cdp_port": None,
        "cityheaven_fetch_count": 1,
        "cityheaven_write_count": 0,
        "airtable_write_count": 0,
        "business_write_count": 0,
    }


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def config_file(directory: Path) -> Path:
    path = directory / "config.json"
    path.write_text(json.dumps({
        "state_dir": str(directory),
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
    return path


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "WindowsExecutionFoundation" / "cityheaven-daily"
        self.root.mkdir(parents=True)
        self._patch = mock.patch.object(daily, "DEFAULT_RUNTIME", self.root)
        self._patch.start()
        self.config = config_file(Path(self._tmp.name))
        self.ensures = []
        self.fetches = []
        self.releases = []

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def ensure_fn(self, configuration, session=None):
        self.ensures.append(configuration["cityheaven_reservation"]["cdp_port"])
        return {
            "state": "MATCH", "action": "reused", "relaunch_count": 0,
            "killed_pids": [], "skipped_chrome_count": 1, "verify_attempts_used": 1,
            "execution_time_limit": "PT0S", "cdp_port": 9227,
        }

    def failing_ensure(self, configuration, session=None):
        self.ensures.append("fail")
        raise CollectorError(FAILURE)

    def release_fn(self, report):
        self.releases.append("release")
        return {"closed": True, "graceful": True, "job": "daily", "profile_kept": True, "killed_pids": []}


class PredicateTests(unittest.TestCase):
    def test_today_mismatch_receipt_is_rerunnable_and_the_later_source_missing_is_not(self):
        failed_path = FIXTURES / "receipts" / "20261020_FAILED_1130_moved_aside.json"
        later_path = FIXTURES / "receipts" / "20261020.json"
        if not failed_path.is_file() or not later_path.is_file():
            self.skipTest("receipt fixtures are not installed")
        failed = json.loads(failed_path.read_text(encoding="utf-8"))
        later = json.loads(later_path.read_text(encoding="utf-8"))
        self.assertTrue(v2.mismatch_allows_rerun(failed))
        self.assertFalse(v2.cityheaven_contact_evidenced(failed))
        self.assertFalse(v2.mismatch_allows_rerun(later))
        self.assertTrue(v2.cityheaven_contact_evidenced(later))
        authed = dict(failed, failure_code="AUTH_REQUIRED", auth_status="FAILED")
        self.assertFalse(v2.mismatch_allows_rerun(authed))
        contacted = dict(failed, reader_run_count=1)
        self.assertFalse(v2.mismatch_allows_rerun(contacted))
        fetched = dict(failed, fetched_at="2026-10-21T11:30:01+09:00")
        self.assertFalse(v2.mismatch_allows_rerun(fetched))
        persisted = dict(failed, source_payload_persisted=True)
        self.assertFalse(v2.mismatch_allows_rerun(persisted))


class RerunTests(Sandbox):
    def test_pre_contact_mismatch_is_archived_and_refetched(self):
        path = self.root / "receipts" / "20261020.json"
        write_json(path, mismatch_receipt())
        calls = []

        def fetcher(date):
            calls.append(date)
            return good_payload(date)

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=fetcher,
            config=self.config, ensure_fn=self.ensure_fn, release_fn=self.release_fn,
        )
        self.assertEqual(calls, [DATE])
        self.assertEqual(self.ensures, [9227])
        self.assertEqual(self.releases, ["release"])
        self.assertEqual(receipt["status"], "PASS")
        self.assertFalse(receipt.get("receipt_reused"))
        archived = list((self.root / "receipts" / "history" / "20261020").glob("*.json"))
        codes = [json.loads(item.read_text(encoding="utf-8")).get("failure_code") for item in archived]
        self.assertIn(FAILURE, codes)

    def test_other_failed_receipts_stay_stale_and_do_not_touch_chrome(self):
        write_json(self.root / "receipts" / "20261020.json", {
            "schema_version": daily.SCHEMA, "status": "FAILED", "failure_code": "SOURCE_MISSING",
            "target_date": DATE, "source_payload_persisted": False, "auth_status": "PASS",
            "reader_run_count": 1, "cityheaven_fetch_count": 1,
        })

        def fetcher(date):
            raise AssertionError("fetcher")

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=fetcher,
            config=self.config, ensure_fn=self.failing_ensure, release_fn=self.release_fn,
        )
        self.assertEqual(self.ensures, [])
        self.assertEqual(self.releases, [])
        self.assertTrue(receipt["receipt_reused"])
        self.assertEqual(receipt["failure_code"], "SOURCE_MISSING")

    def test_auth_required_still_refetches_after_ensure(self):
        write_json(self.root / "receipts" / "20261020.json", {
            "schema_version": daily.SCHEMA, "status": "FAILED", "failure_code": "AUTH_REQUIRED",
            "target_date": DATE, "source_payload_persisted": False, "auth_status": "FAILED",
            "reader_run_count": 0,
        })

        def fetcher(date):
            self.fetches.append(date)
            raise CollectorError("AUTH_REQUIRED")

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=fetcher,
            config=self.config, ensure_fn=self.ensure_fn, release_fn=self.release_fn,
        )
        self.assertEqual(self.ensures, [9227])
        self.assertEqual(self.fetches, [DATE])
        self.assertEqual(self.releases, ["release"])
        self.assertEqual(receipt["failure_code"], "AUTH_REQUIRED")

    def test_ensure_failure_writes_mismatch_without_calling_the_fetcher(self):
        def fetcher(date):
            raise AssertionError("cityheaven")

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=fetcher,
            config=self.config, ensure_fn=self.failing_ensure, release_fn=self.release_fn,
        )
        self.assertEqual(self.ensures, ["fail"])
        self.assertEqual(self.releases, ["release"])
        self.assertEqual(receipt["failure_code"], FAILURE)
        self.assertEqual(receipt["auth_status"], "NOT_CHECKED")
        self.assertEqual(receipt["reader_run_count"], 0)
        self.assertFalse(receipt["source_payload_persisted"])
        saved = json.loads((self.root / "receipts" / "20261020.json").read_text(encoding="utf-8"))
        self.assertTrue(v2.mismatch_allows_rerun(saved))

    def test_saved_source_does_not_ensure_or_fetch(self):
        daily.persist_offline_source(good_payload(), DATE, runtime=self.root)
        self.assertFalse(v2.will_contact_cityheaven(
            self.root, DATE, offline_payload=None, write_airtable=False, request=daily.airtable_request,
        ))

        def fetcher(date):
            raise AssertionError("fetcher")

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=fetcher,
            config=self.config, ensure_fn=self.failing_ensure, release_fn=self.release_fn,
        )
        self.assertEqual(self.ensures, [])
        self.assertEqual(self.releases, [])
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["source_reused"])


class DryRunTests(Sandbox):
    def test_dry_run_exercises_ensure_and_does_not_run_the_job(self):
        with mock.patch.object(v2, "run_v2", side_effect=AssertionError("run")):
            code = v2.main(
                ["--dry-run", "--config", str(self.config), "--date", DATE, "--runtime", str(self.root)],
                ensure_fn=self.ensure_fn, release_fn=self.release_fn,
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.ensures, [9227])
        self.assertEqual(self.releases, ["release"])

    def test_http_403_and_an_unexpected_error_still_release_chrome(self):
        def blocked(date):
            self.fetches.append(date)
            raise CollectorError("HTTP_403")

        receipt = v2.run_v2(
            DATE, write_airtable=False, runtime=self.root, fetcher=blocked,
            config=self.config, ensure_fn=self.ensure_fn, release_fn=self.release_fn,
        )
        self.assertEqual(receipt["failure_code"], "HTTP_403")
        self.assertEqual(receipt["status"], "FAILED")
        self.assertEqual(self.releases, ["release"])

        def exploded(date):
            raise RuntimeError("blocked")

        (self.root / "receipts" / "20261020.json").unlink()
        self.releases.clear()
        self.ensures.clear()
        with self.assertRaises(RuntimeError):
            v2.run_v2(
                DATE, write_airtable=False, runtime=self.root, fetcher=exploded,
                config=self.config, ensure_fn=self.ensure_fn, release_fn=self.release_fn,
            )
        self.assertEqual(self.releases, ["release"])

    def test_fetch_v2_readiness_stays_on_the_local_cdp_script(self):
        readiness = json.dumps({
            "cdp_reachable": True, "runtime_identity": "MATCH", "profile_identity": "MATCH",
            "cdp_port": 9227, "additional_browser_launch_count": 0, "browser_restart_count": 0,
        })
        reader = json.dumps({"casts": []})
        calls = []

        def run(args, **kwargs):
            calls.append((list(args), kwargs))
            stdout = readiness if len(calls) == 1 else reader
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        original = daily.ensure_reservation_browser
        with mock.patch.object(v2.subprocess, "run", side_effect=run), \
                mock.patch.object(daily.subprocess, "run", side_effect=run):
            payload = v2.fetch_v2(DATE, config=self.config, report={"state": "MATCH", "relaunch_count": 1})
        self.assertIs(daily.ensure_reservation_browser, original)
        self.assertEqual(len(calls), 2)
        command = " ".join(calls[0][0])
        reader_command = " ".join(calls[1][0])
        self.assertIn("cityheaven_reservation_runtime_ensure_v2.js", command)
        self.assertNotIn("cityheaven.net", command)
        self.assertNotIn("http://", command)
        self.assertNotIn("http://", reader_command)
        self.assertNotIn("https://", command)
        self.assertNotIn("https://", reader_command)
        self.assertEqual(calls[0][1]["env"]["CITYHEAVEN_PROFILE_CMDLINE_MATCH"], "1")
        self.assertIn("cityheaven_daily_reader.js", reader_command)
        self.assertEqual(payload.runtime_details["browser_restart_count"], 1)
        self.assertEqual(payload.runtime_details["cdp_port"], 9227)

    def test_unmocked_readiness_does_not_start_node(self):
        with self.assertRaises(RealIoBlocked):
            v2._run_node_readiness(
                json.loads(self.config.read_text(encoding="utf-8")),
                {"state": "MATCH", "relaunch_count": 0},
            )


if __name__ == "__main__":
    unittest.main()
