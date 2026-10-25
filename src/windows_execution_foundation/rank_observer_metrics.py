from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .cityheaven_daily_metrics import airtable_request
from .core import atomic_write_json


JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
BASE_ID = "appdtl4ewm5XkOiGd"
TABLE_ID = "tblRB9Tueun2U38Ds"
BUSINESS_ID = "biz_epron_lady"
VERSION = "rank_observer_daily_v1"
SCHEMA = "rank_observer_airtable_sync/1.0"
EXPECTED_OBSERVATIONS_PER_DAY = 288
METRICS = ("last_rank", "best_rank", "worst_rank", "average_rank", "valid_observation_count", "coverage")
WRITER_CREDENTIAL_ROUTE_CLASS = "CTI_BRIDGE_WATCHER_DPAPI_SECURE_STORE_EXISTING"
DEFAULT_EVIDENCE = ROOT / "local" / "rank-observer-airtable-sync-v01-20260926.json"
DEFAULT_RUNTIME = ROOT / "local" / "rank-observer-metrics"


class RankMetricsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSpec:
    media: str
    path: Path
    rank_column: str
    context_columns: tuple[str, ...]
    context_name_column: str | None = None


SOURCES = (
    SourceSpec(
        "ekichika",
        Path(r"C:\Users\epuro\Documents\Codex\2026-09-21\ekichika-rank-observer-m720s-v01\data\observations.csv"),
        "rank",
        ("listing_url",),
    ),
    SourceSpec(
        "cocoa",
        Path(r"C:\Users\epuro\Documents\Codex\2026-09-21\cocoa-rank-observer-m720s-v01\data\observations.csv"),
        "rank",
        ("listing_url",),
    ),
    SourceSpec(
        "undernavi",
        Path(r"C:\Users\epuro\Documents\Codex\2026-09-22\undernavi-rank-observer-m720s-v01\data\observations.csv"),
        "position",
        ("listing_base_url", "filter_signature"),
    ),
    SourceSpec(
        "vanilla",
        Path(r"C:\Users\epuro\Documents\Codex\2026-09-08\vanilla-rank-observer-m720s-v01\data\observations_multitarget.csv"),
        "rank",
        ("listing_key", "listing_url"),
        "listing_name",
    ),
)

REQUIRED_COLUMNS = {"timestamp_jst", "status", "error_text"}
PII_PATTERN = re.compile(r"(?:\b\d{2,4}-\d{2,4}-\d{3,4}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)


def previous_jst_day(now: datetime | None = None) -> str:
    current = (now or datetime.now(JST)).astimezone(JST)
    return (current.date() - timedelta(days=1)).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _context_key(spec: SourceSpec, row: dict[str, str]) -> str:
    parts = [str(row.get(column) or "").strip() for column in spec.context_columns]
    if not all(parts):
        raise RankMetricsError("SCHEMA_DRIFT")
    return "|".join(parts)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RankMetricsError("SCHEMA_DRIFT") from exc
    if parsed.tzinfo is None:
        raise RankMetricsError("SCHEMA_DRIFT")
    return parsed.astimezone(JST)


def _parse_positive_rank(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[1-9]\d*", text):
        return None
    return int(text)


def read_source(spec: SourceSpec, target_date: str) -> dict[str, Any]:
    if not spec.path.is_file():
        raise RankMetricsError("SOURCE_MISSING")
    try:
        with spec.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            required = REQUIRED_COLUMNS | {spec.rank_column, *spec.context_columns}
            if not required.issubset(fields):
                raise RankMetricsError("SCHEMA_DRIFT")
            rows = []
            for line_number, row in enumerate(reader, start=2):
                timestamp = _parse_timestamp(str(row.get("timestamp_jst") or ""))
                if timestamp.date().isoformat() != target_date:
                    continue
                context_key = _context_key(spec, row)
                rank = _parse_positive_rank(str(row.get(spec.rank_column) or ""))
                status = str(row.get("status") or "").strip().upper()
                error_text = str(row.get("error_text") or "").strip()
                valid = status == "PASS" and rank is not None and not error_text
                rows.append({
                    "line_number": line_number,
                    "timestamp": timestamp,
                    "context_key": context_key,
                    "context_name": str(row.get(spec.context_name_column) or "").strip() if spec.context_name_column else "",
                    "rank": rank if valid else None,
                    "status": status or "UNKNOWN",
                    "valid": valid,
                    "invalid_reason": None if valid else (
                        "STATUS_NOT_PASS" if status != "PASS" else "RANK_UNKNOWN_OR_INVALID" if rank is None else "ERROR_PRESENT"
                    ),
                })
    except UnicodeError as exc:
        raise RankMetricsError("SCHEMA_DRIFT") from exc
    return {"spec": spec, "rows": rows, "sha256": sha256_file(spec.path)}


def _slug(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if compact:
        return compact[:48]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def aggregate_sources(target_date: str, specs: Iterable[SourceSpec] = SOURCES) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aggregates: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for spec in specs:
        loaded = read_source(spec, target_date)
        rows = loaded["rows"]
        sources.append({
            "media": spec.media,
            "path": str(spec.path),
            "sha256": loaded["sha256"],
            "target_date_row_count": len(rows),
        })
        contexts = sorted({row["context_key"] for row in rows})
        if not contexts:
            raise RankMetricsError("SOURCE_MISSING")
        for context in contexts:
            subset = sorted((row for row in rows if row["context_key"] == context), key=lambda row: (row["timestamp"], row["line_number"]))
            valid = [row for row in subset if row["valid"]]
            if not valid:
                raise RankMetricsError("NO_VALID_OBSERVATIONS")
            ranks = [int(row["rank"]) for row in valid]
            coverage = min(len(valid) / EXPECTED_OBSERVATIONS_PER_DAY, 1.0)
            status_counts = dict(sorted(Counter(row["status"] for row in subset).items()))
            invalid_counts = dict(sorted(Counter(row["invalid_reason"] for row in subset if row["invalid_reason"]).items()))
            name = next((row["context_name"] for row in subset if row["context_name"]), "")
            aggregates.append({
                "media": spec.media,
                "context_key": context,
                "context_name": name,
                "context_id": f"{spec.media}:{_slug(context)}",
                "target_date": target_date,
                "last_rank": ranks[-1],
                "best_rank": min(ranks),
                "worst_rank": max(ranks),
                "average_rank": round(fmean(ranks), 6),
                "valid_observation_count": len(valid),
                "observed_row_count": len(subset),
                "invalid_observation_count": len(subset) - len(valid),
                "expected_observation_count": EXPECTED_OBSERVATIONS_PER_DAY,
                "coverage": round(coverage, 6),
                "coverage_percent": round(coverage * 100, 4),
                "status_counts": status_counts,
                "invalid_reason_counts": invalid_counts,
                "first_observed_at": subset[0]["timestamp"].isoformat(timespec="milliseconds"),
                "last_observed_at": subset[-1]["timestamp"].isoformat(timespec="milliseconds"),
                "data_quality": "COMPLETE" if len(valid) >= EXPECTED_OBSERVATIONS_PER_DAY and len(valid) == len(subset) else "PARTIAL",
                "source_path": str(spec.path),
                "source_sha256": loaded["sha256"],
            })
    aggregates.sort(key=lambda item: (item["media"], item["context_id"]))
    return aggregates, sources


def _evidence_string(aggregate: dict[str, Any]) -> str:
    value = {
        "schema": "rank_observer_daily_evidence/1.0",
        "media": aggregate["media"],
        "context_id": aggregate["context_id"],
        "context_key": aggregate["context_key"],
        "date": aggregate["target_date"],
        "source_sha256": aggregate["source_sha256"],
        "observed_rows": aggregate["observed_row_count"],
        "valid_observations": aggregate["valid_observation_count"],
        "invalid_observations": aggregate["invalid_observation_count"],
        "expected_observations": aggregate["expected_observation_count"],
        "coverage": aggregate["coverage"],
        "data_quality": aggregate["data_quality"],
        "status_counts": aggregate["status_counts"],
        "invalid_reason_counts": aggregate["invalid_reason_counts"],
        "first_observed_at": aggregate["first_observed_at"],
        "last_observed_at": aggregate["last_observed_at"],
        "unknown_rank_zero_imputed": False,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def metric_plan(aggregates: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for aggregate in aggregates:
        values = {
            "last_rank": (aggregate["last_rank"], "rank"),
            "best_rank": (aggregate["best_rank"], "rank"),
            "worst_rank": (aggregate["worst_rank"], "rank"),
            "average_rank": (aggregate["average_rank"], "rank"),
            "valid_observation_count": (aggregate["valid_observation_count"], "count"),
            "coverage": (aggregate["coverage"], "ratio"),
        }
        evidence = _evidence_string(aggregate)
        for metric, (value, unit) in values.items():
            metric_key = f"rank_observer_{aggregate['media']}_{_slug(aggregate['context_id'])}_{metric}_daily_v1"
            identity = "|".join((BUSINESS_ID, aggregate["context_id"], target_date, metric_key, VERSION))
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            fields: dict[str, Any] = {
                "Metric Record ID": f"bm_rank_observer_{digest}",
                "Business ID": BUSINESS_ID,
                "Metric Key": metric_key,
                "Metric Label": f"{aggregate['media']} {aggregate['context_name'] or aggregate['context_id']} {metric} {target_date}",
                "Value": value,
                "Unit": unit,
                "Window Start": target_date,
                "Window End": target_date,
                "Source": "Rank Observer local CSV daily aggregate",
                "Evidence": evidence,
                "Dedup Key": identity,
                "Notes": f"{VERSION}; valid observations only; UNKNOWN/absent/ambiguous never zero-imputed; data_quality={aggregate['data_quality']}",
                "Metric Class": "FUNNEL",
                "Metric Layer": "UNIT",
                "Source Project ID": "business_state_baseline",
            }
            if aggregate["data_quality"] == "COMPLETE":
                fields.update({"Data Status": "CONFIRMED", "Confidence": "HIGH"})
            rows.append(fields)
    rows.sort(key=lambda row: row["Metric Record ID"])
    if len(rows) != len({row["Metric Record ID"] for row in rows}) or len(rows) != len({row["Dedup Key"] for row in rows}):
        raise RankMetricsError("PLAN_IDENTITY_COLLISION")
    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    if PII_PATTERN.search(serialized):
        raise RankMetricsError("PII_GUARD_FAILED")
    return rows


def _escape_formula(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _read_by_field(values: list[str], field: str, request: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for start in range(0, len(values), 10):
        chunk = values[start:start + 10]
        clauses = ["{" + field + "}='" + _escape_formula(value) + "'" for value in chunk]
        formula = clauses[0] if len(clauses) == 1 else "OR(" + ",".join(clauses) + ")"
        response = request("GET", params={"filterByFormula": formula, "pageSize": 100, "returnFieldsByFieldId": "false"})
        rows = response.get("records") if isinstance(response, dict) else None
        if not isinstance(rows, list) or response.get("offset"):
            raise RankMetricsError("AIRTABLE_READ_FAILED")
        found.extend(rows)
    return found


def write_metrics(rows: list[dict[str, Any]], request: Callable[..., dict[str, Any]] = airtable_request) -> dict[str, Any]:
    wanted = {row["Metric Record ID"]: row for row in rows}
    wanted_dedup = {row["Dedup Key"]: row for row in rows}
    existing = _read_by_field(list(wanted), "Metric Record ID", request)
    by_dedup = _read_by_field(list(wanted_dedup), "Dedup Key", request)
    union: dict[str, dict[str, Any]] = {}
    for record in [*existing, *by_dedup]:
        record_id = str(record.get("id") or "")
        if not record_id:
            raise RankMetricsError("AIRTABLE_READ_FAILED")
        union[record_id] = record
    exact_by_metric: dict[str, dict[str, Any]] = {}
    seen_dedup: set[str] = set()
    for record in union.values():
        fields = record.get("fields") or {}
        metric_id = fields.get("Metric Record ID")
        dedup = fields.get("Dedup Key")
        if metric_id not in wanted or dedup not in wanted_dedup:
            raise RankMetricsError("AIRTABLE_IDENTITY_CONFLICT")
        if wanted[metric_id]["Dedup Key"] != dedup or wanted_dedup[dedup]["Metric Record ID"] != metric_id:
            raise RankMetricsError("AIRTABLE_IDENTITY_CONFLICT")
        if metric_id in exact_by_metric or dedup in seen_dedup:
            raise RankMetricsError("AIRTABLE_DUPLICATE_CONFLICT")
        for key, value in wanted[metric_id].items():
            if fields.get(key) != value:
                raise RankMetricsError("AIRTABLE_CONTENT_CONFLICT")
        exact_by_metric[metric_id] = record
        seen_dedup.add(dedup)
    missing = [row for metric_id, row in wanted.items() if metric_id not in exact_by_metric]
    created: list[dict[str, Any]] = []
    observed_at = datetime.now(JST).isoformat(timespec="seconds")
    for start in range(0, len(missing), 10):
        batch = missing[start:start + 10]
        response = request("POST", body={"records": [{"fields": {**row, "Observed At": observed_at}} for row in batch], "typecast": False})
        records = response.get("records") if isinstance(response, dict) else None
        if not isinstance(records, list) or len(records) != len(batch):
            raise RankMetricsError("AIRTABLE_WRITE_FAILED")
        created.extend(records)
    final = _read_by_field(list(wanted), "Metric Record ID", request)
    final_by_id: dict[str, dict[str, Any]] = {}
    record_ids: dict[str, str] = {}
    for record in final:
        fields = record.get("fields") or {}
        metric_id = fields.get("Metric Record ID")
        if metric_id not in wanted or metric_id in final_by_id:
            raise RankMetricsError("AIRTABLE_READBACK_FAILED")
        for key, value in wanted[metric_id].items():
            if fields.get(key) != value:
                raise RankMetricsError("AIRTABLE_READBACK_FAILED")
        if not record.get("id"):
            raise RankMetricsError("AIRTABLE_READBACK_FAILED")
        final_by_id[metric_id] = record
        record_ids[metric_id] = str(record["id"])
    if set(final_by_id) != set(wanted):
        raise RankMetricsError("AIRTABLE_READBACK_FAILED")
    return {
        "write_count": len(created),
        "readback_count": len(final_by_id),
        "duplicate_count": 0,
        "replay_count": len(exact_by_metric),
        "business_metric_record_ids": dict(sorted(record_ids.items())),
    }


def run(
    target_date: str,
    *,
    write_airtable: bool,
    acceptance_replay: bool,
    evidence_path: Path = DEFAULT_EVIDENCE,
    runtime: Path = DEFAULT_RUNTIME,
    specs: Iterable[SourceSpec] = SOURCES,
    request: Callable[..., dict[str, Any]] = airtable_request,
) -> dict[str, Any]:
    started_at = datetime.now(JST).isoformat(timespec="seconds")
    receipt_path = runtime / "receipts" / f"{target_date.replace('-', '')}.json"
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "started_at": started_at,
        "completed_at": None,
        "status": "FAILED",
        "failure_code": None,
        "target_date": target_date,
        "base_id": BASE_ID,
        "table_id": TABLE_ID,
        "credential_route_class": WRITER_CREDENTIAL_ROUTE_CLASS,
        "credential_secret_output_count": 0,
        "browser_launches": 0,
        "media_site_access_count": 0,
        "source_payload_persisted": False,
        "pii_count": 0,
        "planned_count": 0,
        "written_count": 0,
        "readback_count": 0,
        "duplicate_count": 0,
        "replay_new_write_count": None,
        "replay_readback_count": 0,
        "source_paths": [],
        "aggregation_contexts": [],
        "planned_metric_record_ids": [],
        "business_metric_record_ids": {},
        "observer_changes": 0,
        "partial_handling": "PARTIAL days omit Data Status/Confidence; coverage and invalid reasons remain in Evidence; unknown ranks are never zero-imputed.",
        "receipt_path": str(receipt_path),
    }
    try:
        aggregates, sources = aggregate_sources(target_date, specs)
        plan = metric_plan(aggregates, target_date)
        evidence.update({
            "source_paths": sources,
            "aggregation_contexts": aggregates,
            "planned_count": len(plan),
            "planned_metric_record_ids": [row["Metric Record ID"] for row in plan],
        })
        if write_airtable:
            first = write_metrics(plan, request)
            evidence.update({
                "written_count": first["write_count"],
                "readback_count": first["readback_count"],
                "duplicate_count": first["duplicate_count"],
                "business_metric_record_ids": first["business_metric_record_ids"],
            })
            if acceptance_replay:
                replay = write_metrics(plan, request)
                evidence.update({
                    "replay_new_write_count": replay["write_count"],
                    "replay_readback_count": replay["readback_count"],
                    "replay_duplicate_count": replay["duplicate_count"],
                })
                if replay["write_count"] != 0 or replay["duplicate_count"] != 0 or replay["readback_count"] != len(plan):
                    raise RankMetricsError("REPLAY_NOT_IDEMPOTENT")
        evidence["status"] = "PASS"
    except RankMetricsError as exc:
        evidence["failure_code"] = str(exc)
    except Exception:
        evidence["failure_code"] = "AIRTABLE_WRITE_FAILED" if write_airtable else "UNEXPECTED_FAILURE"
    evidence["completed_at"] = datetime.now(JST).isoformat(timespec="seconds")
    atomic_write_json(receipt_path, evidence)
    atomic_write_json(evidence_path, evidence)
    return evidence


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--date", default=previous_jst_day())
    value.add_argument("--write-airtable", action="store_true")
    value.add_argument("--acceptance-replay", action="store_true")
    value.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    value.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"status": "FAILED", "failure_code": "INVALID_DATE"}))
        return 2
    receipt = run(
        args.date,
        write_airtable=args.write_airtable,
        acceptance_replay=args.acceptance_replay,
        evidence_path=args.evidence,
        runtime=args.runtime,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 20


if __name__ == "__main__":
    raise SystemExit(main())
