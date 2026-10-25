from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.windows_execution_foundation.rank_observer_metrics import (
    EXPECTED_OBSERVATIONS_PER_DAY,
    METRICS,
    RankMetricsError,
    SourceSpec,
    aggregate_sources,
    metric_plan,
    run,
    write_metrics,
)


def write_fixture(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def row(timestamp: str, rank: str, *, context: str = "main", status: str = "PASS", error: str = "") -> dict[str, str]:
    return {
        "timestamp_jst": timestamp,
        "rank": rank,
        "status": status,
        "error_text": error,
        "listing_key": context,
        "listing_url": f"https://example.test/{context}/",
        "listing_name": context,
    }


class ParsingTests(unittest.TestCase):
    def test_four_media_fixture_parsing_and_vanilla_contexts_stay_separate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            specs = []
            for media in ("ekichika", "cocoa", "undernavi"):
                path = root / f"{media}.csv"
                write_fixture(path, [row("2026-09-25T00:00:00+09:00", "4")])
                specs.append(SourceSpec(media, path, "rank", ("listing_url",)))
            vanilla = root / "vanilla.csv"
            write_fixture(vanilla, [
                row("2026-09-25T00:00:00+09:00", "2", context="main"),
                row("2026-09-25T00:00:01+09:00", "8", context="tainyu"),
            ])
            specs.append(SourceSpec("vanilla", vanilla, "rank", ("listing_key", "listing_url"), "listing_name"))
            aggregates, sources = aggregate_sources("2026-09-25", specs)
        self.assertEqual(len(sources), 4)
        self.assertEqual(len(aggregates), 5)
        vanilla_values = {(item["context_name"], item["last_rank"]) for item in aggregates if item["media"] == "vanilla"}
        self.assertEqual(vanilla_values, {("main", 2), ("tainyu", 8)})

    def test_unknown_absent_and_ambiguous_are_not_zero(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rows.csv"
            write_fixture(path, [
                row("2026-09-25T00:00:00+09:00", "3"),
                row("2026-09-25T00:05:00+09:00", "", status="TARGET_ABSENT"),
                row("2026-09-25T00:10:00+09:00", "0"),
                row("2026-09-25T00:15:00+09:00", "9", status="AMBIGUOUS"),
            ])
            aggregates, _ = aggregate_sources("2026-09-25", [SourceSpec("fixture", path, "rank", ("listing_url",))])
        item = aggregates[0]
        self.assertEqual(item["last_rank"], 3)
        self.assertEqual(item["valid_observation_count"], 1)
        self.assertEqual(item["invalid_observation_count"], 3)
        self.assertEqual(item["coverage"], round(1 / EXPECTED_OBSERVATIONS_PER_DAY, 6))

    def test_daily_metrics_and_partial_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rows.csv"
            write_fixture(path, [
                row("2026-09-25T00:00:00+09:00", "5"),
                row("2026-09-25T00:05:00+09:00", "2"),
                row("2026-09-25T00:10:00+09:00", "8"),
            ])
            aggregates, _ = aggregate_sources("2026-09-25", [SourceSpec("fixture", path, "rank", ("listing_url",))])
            plan = metric_plan(aggregates, "2026-09-25")
        self.assertEqual(len(plan), len(METRICS))
        values = {item["Metric Key"].rsplit("_", 3)[0].split("_")[-1]: item["Value"] for item in []}
        metric_values = {next(metric for metric in METRICS if metric in item["Metric Key"]): item["Value"] for item in plan}
        self.assertEqual(metric_values["last_rank"], 8)
        self.assertEqual(metric_values["best_rank"], 2)
        self.assertEqual(metric_values["worst_rank"], 8)
        self.assertEqual(metric_values["average_rank"], 5)
        self.assertNotIn("Data Status", plan[0])
        self.assertIn('"data_quality":"PARTIAL"', plan[0]["Evidence"])

    def test_schema_drift_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("timestamp_jst,rank\n2026-09-25T00:00:00+09:00,1\n", encoding="utf-8")
            with self.assertRaisesRegex(RankMetricsError, "SCHEMA_DRIFT"):
                aggregate_sources("2026-09-25", [SourceSpec("fixture", path, "rank", ("listing_url",))])


class WriterTests(unittest.TestCase):
    @staticmethod
    def fake_request_store():
        records: list[dict] = []
        calls: list[str] = []

        def request(method, *, params=None, body=None):
            calls.append(method)
            if method == "GET":
                formula = str((params or {}).get("filterByFormula") or "")
                if "Metric Record ID" in formula:
                    selected = [record for record in records if str(record["fields"].get("Metric Record ID")) in formula]
                elif "Dedup Key" in formula:
                    selected = [record for record in records if str(record["fields"].get("Dedup Key")) in formula]
                else:
                    selected = []
                return {"records": selected}
            created = []
            for item in body["records"]:
                record = {"id": f"rec{len(records):014d}", "fields": dict(item["fields"])}
                records.append(record)
                created.append(record)
            return {"records": created}

        return records, calls, request

    @staticmethod
    def sample_plan():
        aggregate = {
            "media": "fixture", "context_key": "main|https://example.test/main/", "context_name": "main",
            "context_id": "fixture:main", "target_date": "2026-09-25", "last_rank": 3, "best_rank": 2,
            "worst_rank": 4, "average_rank": 3, "valid_observation_count": 3, "observed_row_count": 3,
            "invalid_observation_count": 0, "expected_observation_count": 288, "coverage": 0.010417,
            "coverage_percent": 1.0417, "status_counts": {"PASS": 3}, "invalid_reason_counts": {},
            "first_observed_at": "2026-09-25T00:00:00.000+09:00", "last_observed_at": "2026-09-25T00:10:00.000+09:00",
            "data_quality": "PARTIAL", "source_path": "fixture.csv", "source_sha256": "a" * 64,
        }
        return metric_plan([aggregate], "2026-09-25")

    def test_same_input_replay_has_zero_post(self):
        plan = self.sample_plan()
        records, calls, request = self.fake_request_store()
        first = write_metrics(plan, request)
        first_post_count = calls.count("POST")
        second = write_metrics(plan, request)
        self.assertEqual(first["write_count"], len(plan))
        self.assertEqual(second["write_count"], 0)
        self.assertEqual(second["duplicate_count"], 0)
        self.assertEqual(second["readback_count"], len(plan))
        self.assertEqual(calls.count("POST"), first_post_count)
        self.assertEqual(len(records), len(plan))

    def test_conflicting_existing_id_fails_before_post(self):
        plan = self.sample_plan()
        records, calls, request = self.fake_request_store()
        fields = dict(plan[0]); fields["Value"] = 999
        records.append({"id": "recConflict", "fields": fields})
        with self.assertRaisesRegex(RankMetricsError, "AIRTABLE_CONTENT_CONFLICT"):
            write_metrics(plan, request)
        self.assertNotIn("POST", calls)

    def test_run_writes_sanitized_evidence_and_replay_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rows.csv"
            write_fixture(path, [row("2026-09-25T00:00:00+09:00", "3")])
            _, _, request = self.fake_request_store()
            evidence_path = root / "evidence.json"
            result = run(
                "2026-09-25", write_airtable=True, acceptance_replay=True,
                evidence_path=evidence_path, runtime=root / "runtime",
                specs=[SourceSpec("fixture", path, "rank", ("listing_url",))], request=request,
            )
            persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["written_count"], len(METRICS))
        self.assertEqual(result["replay_new_write_count"], 0)
        self.assertEqual(result["browser_launches"], 0)
        self.assertEqual(persisted["credential_secret_output_count"], 0)


if __name__ == "__main__":
    unittest.main()
