"""Read-only tests: no fetch, no runtime writes."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.windows_execution_foundation import cityheaven_update_count_retry as r  # noqa: E402

RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "WindowsExecutionFoundation" / "cityheaven-daily"


def load(stem):
    # After a successful retry the live source is the merged one; use the archived morning source if present.
    hist = sorted((RUNTIME / "offline-source" / "sources" / "history" / stem).glob("morning-before-update-count-retry-*"))
    path = hist[0] / f"{stem}.json" if hist else RUNTIME / "offline-source" / "sources" / f"{stem}.json"
    with open(path, encoding="utf-8") as h:
        return json.load(h)


class RealDataTests(unittest.TestCase):
    def test_20261013_dash_detected_and_eligible(self):
        p = load("20261013")
        cols = r.dash_columns(p)
        n = len(p["casts"])
        for c in (7, 9, 10):
            self.assertEqual(cols.get(c), n)
        # 10/13 was already repaired by the retry (receipt PASS), so it must no longer be eligible.
        self.assertEqual(r.eligibility(RUNTIME, "2026-10-13"), (False, "MORNING_NOT_FAILED"))

    def test_20261012_no_retry(self):
        self.assertEqual(r.dash_columns(load("20261012")), {})
        ok, reason = r.eligibility(RUNTIME, "2026-10-12")
        self.assertFalse(ok)
        self.assertEqual(reason, "MORNING_NOT_FAILED")

    def test_auth_and_empty_failures_excluded(self):
        self.assertFalse(r.eligibility(RUNTIME, "2026-10-10")[0])  # AUTH_REQUIRED
        self.assertFalse(r.eligibility(RUNTIME, "2026-10-11")[0])  # SOURCE_MISSING with 0 fetched


class MergeTests(unittest.TestCase):
    def test_merge_keeps_morning_and_fills_dash(self):
        morning = load("20261013")
        fresh = copy.deepcopy(morning)
        for cast in fresh["casts"]:
            for i in r.UPDATE_GROUP_COLUMNS:
                cast["values"][i] = "5"
            cast["values"][0] = "999999"
        merged, filled = r.merge_update_counts(morning, fresh)
        self.assertEqual(filled, 5 * len(morning["casts"]))
        for m, x in zip(morning["casts"], merged["casts"]):
            self.assertEqual(x["values"][0], m["values"][0])
            self.assertEqual(x["values"][7], "5")
        analysis = r.daily.analyze_partial_source(merged, "2026-10-13")
        self.assertEqual((analysis["status"], analysis["numeric_cell_count"]), ("COMPLETE", analysis["expected_cell_count"]))

    def test_merge_still_dash(self):
        morning = load("20261013")
        merged, filled = r.merge_update_counts(morning, copy.deepcopy(morning))
        self.assertEqual(filled, 0)
        self.assertEqual(r.daily.analyze_partial_source(merged, "2026-10-13")["status"], "PARTIAL")

    def test_reordered_and_extra_roster_ok(self):
        morning = load("20261013")
        fresh = copy.deepcopy(morning)
        for cast in fresh["casts"]:
            for i in r.UPDATE_GROUP_COLUMNS:
                cast["values"][i] = "1"
        extra = copy.deepcopy(fresh["casts"][0])
        extra["girl_id"] = "999999999"
        fresh["casts"] = list(reversed(fresh["casts"])) + [extra]
        merged, filled = r.merge_update_counts(morning, fresh)
        self.assertEqual([c["girl_id"] for c in merged["casts"]], [c["girl_id"] for c in morning["casts"]])
        self.assertEqual(filled, 5 * len(morning["casts"]))

    def test_roster_mismatch_rejected(self):
        morning = load("20261013")
        fresh = copy.deepcopy(morning)
        fresh["casts"] = fresh["casts"][1:]
        with self.assertRaises(r.daily.CollectorError):
            r.merge_update_counts(morning, fresh)

    def test_20261014_cast_dropped_from_list_is_diagnosed_not_forced(self):
        # 2026-10-15 14:30 case: one morning cast was no longer in the CityHeaven list at retry time.
        morning = load("20261014")
        fresh = copy.deepcopy(morning)
        for cast in fresh["casts"]:
            for i in r.UPDATE_GROUP_COLUMNS:
                cast["values"][i] = "3"
        dropped = fresh["casts"].pop(24)["girl_id"]
        diff = r.roster_diff(morning, fresh)
        self.assertEqual(diff["retry_absent_girl_ids"], [dropped])
        self.assertEqual((diff["morning_cast_count"], diff["retry_cast_count"]), (32, 31))
        with self.assertRaises(r.daily.CollectorError):
            r.merge_update_counts(morning, fresh)


class PartialMergeTests(unittest.TestCase):
    def _fresh(self, stem="20261014"):
        morning = load(stem)
        fresh = copy.deepcopy(morning)
        for cast in fresh["casts"]:
            for i in r.UPDATE_GROUP_COLUMNS:
                cast["values"][i] = "3"
        return morning, fresh

    def test_one_dropped_cast_skipped_not_fabricated(self):
        morning, fresh = self._fresh()
        dropped = fresh["casts"].pop(24)["girl_id"]
        merged, filled, missing = r.merge_update_counts_partial(morning, fresh)
        self.assertEqual(missing, [dropped])
        self.assertEqual(filled, 5 * 31)
        kept = next(c for c in merged["casts"] if c["girl_id"] == dropped)
        self.assertEqual(kept["values"], next(c for c in morning["casts"] if c["girl_id"] == dropped)["values"])
        writable = dict(merged, casts=[c for c in merged["casts"] if c["girl_id"] not in missing])
        self.assertEqual(r.daily.analyze_partial_source(writable, "2026-10-14")["status"], "COMPLETE")

    def test_full_roster_no_missing(self):
        morning, fresh = self._fresh()
        _, filled, missing = r.merge_update_counts_partial(morning, fresh)
        self.assertEqual((filled, missing), (160, []))

    def test_empty_retry_fails(self):
        morning, fresh = self._fresh()
        fresh["casts"] = []
        with self.assertRaises(r.daily.CollectorError) as ctx:
            r.merge_update_counts_partial(morning, fresh)
        self.assertEqual(str(ctx.exception), "RETRY_ROSTER_EMPTY")

    def test_too_many_missing_fails(self):
        morning, fresh = self._fresh()
        fresh["casts"] = fresh["casts"][:25]  # 7/32 missing = 21.9% > 20%
        with self.assertRaises(r.daily.CollectorError) as ctx:
            r.merge_update_counts_partial(morning, fresh)
        self.assertEqual(str(ctx.exception), "RETRY_ROSTER_MISMATCH")

    def test_six_missing_allowed(self):
        morning, fresh = self._fresh()
        fresh["casts"] = fresh["casts"][:26]  # 6/32 = 18.75%
        _, _, missing = r.merge_update_counts_partial(morning, fresh)
        self.assertEqual(len(missing), 6)

    def test_schema_mismatch_still_fails(self):
        morning, fresh = self._fresh()
        fresh["casts"][0]["header_paths"] = ["x"] * 21
        with self.assertRaises(r.daily.CollectorError):
            r.merge_update_counts_partial(morning, fresh)


if __name__ == "__main__":
    unittest.main()
