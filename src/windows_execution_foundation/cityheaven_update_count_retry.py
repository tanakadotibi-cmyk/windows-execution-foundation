"""Same-day single retry for CityHeaven "update count" metrics (mitene / okini_people / okini_sends).

Runs only when the morning receipt for the business date failed with SOURCE_MISSING because
those three metrics were "---" in the saved source. Morning numeric values are kept; only the
update-count group (columns 7-11) is filled from one fresh read-only fetch. Never writes Airtable.
At most one retry per business date (exclusive claim file).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import cityheaven_daily_metrics as daily

RETRY_METRICS = ("mitene", "okini_people", "okini_sends")
UPDATE_GROUP_COLUMNS = (7, 8, 9, 10, 11)
DASH = {"---"}
MISSING_TOKENS = {"", "---", "-", "UNKNOWN", "不明"}
SCHEMA = "cityheaven_update_count_retry/1.0"
MAX_MISSING_FRACTION = 0.20  # more absent casts than this => roster drift, fail closed


def _now() -> str:
    return datetime.now(daily.JST).isoformat(timespec="seconds")


def _plain_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8-sig") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _numeric(value: Any) -> bool:
    return value is not None and re.fullmatch(r"\d+", str(value).strip().replace(",", "")) is not None


def dash_columns(payload: dict[str, Any]) -> dict[int, int]:
    """Count casts whose update-group cell is exactly '---', per column (read only)."""
    counts: dict[int, int] = {}
    for cast in payload.get("casts") or []:
        values = cast.get("values") or []
        for index in UPDATE_GROUP_COLUMNS:
            if index < len(values) and str(values[index]).strip() in DASH:
                counts[index] = counts.get(index, 0) + 1
    return counts


def eligibility(runtime: Path, target_date: str) -> tuple[bool, str]:
    """Read-only decision. Returns (eligible, reason_code)."""
    stem = target_date.replace("-", "")
    receipt = _plain_json(runtime / "receipts" / f"{stem}.json")
    if receipt is None:
        return False, "NO_MORNING_RECEIPT"
    if receipt.get("target_date") != target_date:
        return False, "RECEIPT_DATE_MISMATCH"
    if receipt.get("status") != "FAILED":
        return False, "MORNING_NOT_FAILED"
    if receipt.get("failure_code") != "SOURCE_MISSING":
        return False, "FAILURE_NOT_SOURCE_MISSING"
    if receipt.get("partial_analysis_status") != "PARTIAL":
        return False, "NOT_PARTIAL"
    if receipt.get("auth_status") != "PASS" or receipt.get("cityheaven_fetch_count") != 1:
        return False, "MORNING_FETCH_NOT_EVIDENCED"
    analysis = _plain_json(runtime / "offline-source" / "analysis" / f"{stem}.json")
    if analysis is None or analysis.get("status") != "PARTIAL":
        return False, "ANALYSIS_NOT_PARTIAL"
    missing = analysis.get("missing_cells") or []
    if not missing or not int(analysis.get("numeric_cell_count") or 0):
        return False, "NO_USABLE_MORNING_VALUES"
    for cell in missing:
        if cell.get("metric") not in RETRY_METRICS:
            return False, "OTHER_METRIC_MISSING"
        if str(cell.get("raw_value")).strip() not in DASH or cell.get("reason") != "SOURCE_VALUE_MISSING":
            return False, "MISSING_NOT_DASH"
    return True, "ELIGIBLE_UPDATE_COUNT_DASH"


def merge_update_counts(morning: dict[str, Any], retry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Keep morning values; fill only update-group cells that were '---' in the morning."""
    m_casts = morning.get("casts") or []
    by_id = {str(c.get("girl_id")): c for c in retry.get("casts") or []}
    # Match by girl_id (order may differ); every morning cast must be present. Extra retry casts are ignored.
    if not m_casts or any(str(c.get("girl_id")) not in by_id for c in m_casts):
        raise daily.CollectorError("RETRY_ROSTER_MISMATCH")
    merged_casts, filled = [], 0
    for m in m_casts:
        r = by_id[str(m.get("girl_id"))]
        if m.get("header_paths") != r.get("header_paths"):
            raise daily.CollectorError("RETRY_SCHEMA_MISMATCH")
        values = list(m["values"])
        for index in UPDATE_GROUP_COLUMNS:
            if index < len(values) and str(values[index]).strip() in DASH and index < len(r["values"]) and _numeric(r["values"][index]):
                values[index] = r["values"][index]
                filled += 1
        merged_casts.append({"girl_id": m["girl_id"], "display_name": m["display_name"], "header_paths": m["header_paths"], "values": values})
    merged = {k: morning[k] for k in morning if k != "casts"}
    merged["casts"] = merged_casts
    return merged, filled


def roster_diff(morning: dict[str, Any], retry: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic only: which morning casts are absent from the retry list (e.g. hidden/removed since morning)."""
    r_ids = {str(c.get("girl_id")) for c in retry.get("casts") or []}
    absent = [str(c.get("girl_id")) for c in morning.get("casts") or [] if str(c.get("girl_id")) not in r_ids]
    return {"morning_cast_count": len(morning.get("casts") or []), "retry_cast_count": len(retry.get("casts") or []),
            "retry_absent_girl_ids": absent}


def merge_update_counts_partial(morning: dict[str, Any], retry: dict[str, Any]) -> tuple[dict[str, Any], int, list[str]]:
    """Like merge_update_counts, but morning casts absent from the retry list are kept unchanged (never fabricated).

    Returns (merged_with_all_morning_casts, filled_cell_count, missing_from_retry_ids).
    Fails closed when no cast matches or more than MAX_MISSING_FRACTION of the morning roster is absent.
    """
    m_casts = morning.get("casts") or []
    r_ids = {str(c.get("girl_id")) for c in retry.get("casts") or []}
    missing = [str(c.get("girl_id")) for c in m_casts if str(c.get("girl_id")) not in r_ids]
    if not m_casts or len(missing) == len(m_casts):
        raise daily.CollectorError("RETRY_ROSTER_EMPTY")
    if len(missing) > MAX_MISSING_FRACTION * len(m_casts):
        raise daily.CollectorError("RETRY_ROSTER_MISMATCH")
    present = {k: morning[k] for k in morning if k != "casts"}
    present["casts"] = [c for c in m_casts if str(c.get("girl_id")) not in missing]
    merged_present, filled = merge_update_counts(present, retry)
    by_id = {str(c["girl_id"]): c for c in merged_present["casts"]}
    merged = {k: morning[k] for k in morning if k != "casts"}
    merged["casts"] = [by_id.get(str(c.get("girl_id")), c) for c in m_casts]
    return merged, filled, missing


def _claim(path: Path, record: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    return True


def _finish(path: Path, record: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def retry(target_date: str, *, runtime: Path = daily.DEFAULT_RUNTIME,
          fetcher: Callable[[str], dict[str, Any]] = daily.fetch) -> dict[str, Any]:
    daily._check_date(target_date)
    runtime = daily._checked_runtime(runtime)
    stem = target_date.replace("-", "")
    record: dict[str, Any] = {"schema_version": SCHEMA, "target_date": target_date, "started_at": _now(),
                              "airtable_write_count": 0, "cityheaven_fetch_count": 0}
    ok, reason = eligibility(runtime, target_date)
    if not ok:
        record.update({"result": "NOT_ELIGIBLE", "reason_code": reason, "completed_at": _now()})
        return record
    claim = runtime / "update-count-retry" / f"{stem}.json"
    if not _claim(claim, dict(record, result="CLAIMED")):
        record.update({"result": "ALREADY_RETRIED", "reason_code": "ONE_RETRY_PER_DAY", "completed_at": _now()})
        return record
    sources = runtime / "offline-source" / "sources"
    archived: list[tuple[Path, Path]] = []
    try:
        morning = daily._read_saved_source(runtime, target_date)
        if morning is None:
            raise daily.CollectorError("MORNING_SOURCE_MISSING")
        record["morning_dash_columns"] = {str(k): v for k, v in sorted(dash_columns(morning).items())}
        record["cityheaven_fetch_count"] = 1
        fetched = fetcher(target_date)
        retry_payload = dict(fetched)
        daily._safe_offline_payload(retry_payload, target_date)
        record["retry_dash_columns"] = {str(k): v for k, v in sorted(dash_columns(retry_payload).items())}
        record.update(roster_diff(morning, retry_payload))
        merged, filled, missing_ids = merge_update_counts_partial(morning, retry_payload)
        record["filled_cell_count"] = filled
        record["missing_from_retry_ids"] = missing_ids
        record["skipped_cast_count"] = len(missing_ids)
        # Only casts present in the retry are written; absent casts are left unwritten (cells stay empty).
        writable = {k: merged[k] for k in merged if k != "casts"}
        writable["casts"] = [c for c in merged["casts"] if str(c.get("girl_id")) not in set(missing_ids)]
        record["written_cast_count"] = len(writable["casts"])
        analysis = daily.analyze_partial_source(writable, target_date)
        record.update({"merged_status": analysis["status"], "expected_cell_count": analysis["expected_cell_count"],
                       "numeric_cell_count": analysis["numeric_cell_count"], "missing_cell_count": analysis["missing_cell_count"]})
        if analysis["status"] != "COMPLETE":
            record.update({"result": "STILL_MISSING", "reason_code": "UPDATE_COUNT_STILL_DASH"})
            return record
        # Source archive is immutable per date: move the morning pair aside (kept), then publish the merged one.
        history = sources / "history" / stem / ("morning-before-update-count-retry-" + datetime.now(daily.JST).strftime("%Y%m%dT%H%M%S"))
        history.mkdir(parents=True, exist_ok=False)
        for name in (f"{stem}.json", f"{stem}.sha256.json"):
            dst = history / name
            os.replace(sources / name, dst)
            archived.append((dst, sources / name))
        receipt = daily.run(target_date, write_airtable=False, runtime=runtime, offline_payload=writable)
        receipt["update_count_retry"] = {"claim_ref": str(claim), "filled_cell_count": filled,
                                          "morning_source_archive": str(history), "retry_fetched_at": _now(),
                                          "missing_from_retry_ids": missing_ids, "skipped_cast_count": len(missing_ids)}
        daily._save_result(runtime / "receipts" / f"{stem}.json", receipt)
        passed = receipt.get("status") == "PASS"
        record.update({"result": ("PASS_PARTIAL" if missing_ids else "PASS") if passed else "FAILED",
                       "reason_code": receipt.get("failure_code"), "metric_count": receipt.get("metric_count"),
                       "receipt_status": receipt.get("status"), "morning_source_archive": str(history)})
        return record
    except daily.CollectorError as exc:
        record.update({"result": "FAILED", "reason_code": str(exc)})
        if archived and not (sources / f"{stem}.json").exists():
            for dst, src in archived:
                os.replace(dst, src)
            record["morning_source_restored"] = True
        return record
    finally:
        record.setdefault("completed_at", _now())
        _finish(claim, record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=daily.previous_jst_day())
    parser.add_argument("--check-only", action="store_true", help="read-only eligibility check")
    args = parser.parse_args()
    if args.check_only:
        ok, reason = eligibility(daily.DEFAULT_RUNTIME, args.date)
        print(json.dumps({"target_date": args.date, "eligible": ok, "reason_code": reason}))
        return 0
    record = retry(args.date)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0 if record["result"] in {"PASS", "PASS_PARTIAL", "NOT_ELIGIBLE", "ALREADY_RETRIED"} else 20
