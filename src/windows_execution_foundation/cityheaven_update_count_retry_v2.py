"""Update-count retry that does not treat a pre-contact CDP mismatch as the one daily attempt.

The original retry claims the day before the fetch. If that fetch dies in
``ensure_reservation_browser`` with ``CDP_PORT_RUNTIME_MISMATCH``, the claim is
kept and every later run returns ``ALREADY_RETRIED`` without reading CityHeaven.
A claim with that code and no fetched payload is moved aside so the single
attempt can still happen. A morning receipt that itself failed before contact
is not eligible and is not fetched.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import cityheaven_daily_metrics as daily
from . import cityheaven_update_count_retry as retry
from .cityheaven_daily_metrics_v2 import (
    dry_run,
    fetch_v2,
    mismatch_allows_rerun,
    open_reservation_chrome,
)
from .cityheaven_reservation_chrome_ensure_v2 import (
    FAILURE,
    EnsureSession,
    reservation_route,
)


def retry_contact_evidenced(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return True
    if record.get("retry_dash_columns") is not None:
        return True
    if record.get("morning_source_archive"):
        return True
    if record.get("morning_source_restored") is True:
        return True
    if record.get("filled_cell_count") not in {None}:
        return True
    if record.get("receipt_status") not in {None}:
        return True
    try:
        if int(record.get("airtable_write_count") or 0) != 0:
            return True
    except (TypeError, ValueError):
        return True
    return False


def mismatch_claim_reusable(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("result") != "FAILED" or record.get("reason_code") != FAILURE:
        return False
    if retry_contact_evidenced(record):
        return False
    return True


def release_mismatch_claim_if_safe(runtime: Path, target_date: str) -> bool:
    daily._check_date(target_date)
    runtime = daily._checked_runtime(runtime)
    path = runtime / "update-count-retry" / f"{target_date.replace('-', '')}.json"
    record = retry._plain_json(path)
    if not mismatch_claim_reusable(record):
        return False
    history = path.parent / "history" / path.stem
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(daily.JST).strftime("%Y%m%dT%H%M%S")
    dest = history / f"mismatch-before-retry-{stamp}.json"
    suffix = 1
    while dest.exists():
        dest = history / f"mismatch-before-retry-{stamp}-{suffix}.json"
        suffix += 1
    os_replace(path, dest)
    return True


def os_replace(source: Path, dest: Path) -> None:
    import os
    os.replace(source, dest)


def eligibility_v2(runtime: Path, target_date: str) -> tuple[bool, str]:
    stem = target_date.replace("-", "")
    receipt = retry._plain_json(runtime / "receipts" / f"{stem}.json")
    if isinstance(receipt, dict) and mismatch_allows_rerun(receipt):
        return False, "MORNING_CDP_MISMATCH_NO_CONTACT"
    return retry.eligibility(runtime, target_date)


def _record(target_date: str, **extra: Any) -> dict[str, Any]:
    value = {
        "schema_version": retry.SCHEMA, "target_date": target_date,
        "started_at": datetime.now(daily.JST).isoformat(timespec="seconds"),
        "airtable_write_count": 0, "cityheaven_fetch_count": 0,
        "completed_at": datetime.now(daily.JST).isoformat(timespec="seconds"),
    }
    value.update(extra)
    return value


def retry_v2(
    target_date: str,
    *,
    runtime: Path = daily.DEFAULT_RUNTIME,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    ensure_fn: Callable[..., dict[str, Any]] | None = None,
    release_fn: Callable[[dict[str, Any] | None], Any] | None = None,
    session: EnsureSession | None = None,
    config: Path = daily.DEFAULT_CONFIG,
) -> dict[str, Any]:
    daily._check_date(target_date)
    runtime = daily._checked_runtime(runtime)
    session = session or EnsureSession()
    released = release_mismatch_claim_if_safe(runtime, target_date)
    ok, reason = eligibility_v2(runtime, target_date)
    if not ok:
        record = _record(target_date, result="NOT_ELIGIBLE", reason_code=reason, mismatch_claim_released=released)
        return record
    claim = runtime / "update-count-retry" / f"{target_date.replace('-', '')}.json"
    if claim.exists():
        record = _record(
            target_date, result="ALREADY_RETRIED", reason_code="ONE_RETRY_PER_DAY",
            mismatch_claim_released=released,
        )
        return record
    configuration = daily._load_configuration(config)
    if not reservation_route(configuration):
        def plain(date: str) -> dict[str, Any]:
            if fetcher is not None:
                return fetcher(date)
            return daily.fetch(date, config=config)

        record = retry.retry(target_date, runtime=runtime, fetcher=plain)
        record["mismatch_claim_released"] = released
        return record

    with open_reservation_chrome(
        configuration, runtime=runtime, job="retry", ensure_fn=ensure_fn,
        release_fn=release_fn, session=session,
    ) as lease:
        ensure_error = lease.ensure_error
        report = lease.report

        def bound(date: str) -> dict[str, Any]:
            if ensure_error is not None:
                raise daily.CollectorError(FAILURE)
            if fetcher is not None:
                return fetcher(date)
            if report is None:
                return daily.fetch(date, config=config)
            return fetch_v2(date, config=config, report=report)

        record = retry.retry(target_date, runtime=runtime, fetcher=bound)
        record["mismatch_claim_released"] = released
        return record


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--date", default=daily.previous_jst_day())
    value.add_argument("--check-only", action="store_true", help="read-only eligibility check")
    value.add_argument("--runtime", type=Path, default=daily.DEFAULT_RUNTIME)
    value.add_argument("--config", type=Path, default=daily.DEFAULT_CONFIG)
    value.add_argument(
        "--dry-run", action="store_true",
        help="launch reservation Chrome on port 9227 if it is down, then close it when this process is the last holder; do not contact CityHeaven",
    )
    return value


def main(
    argv: list[str] | None = None,
    *,
    ensure_fn: Callable[..., dict[str, Any]] | None = None,
    release_fn: Callable[[dict[str, Any] | None], Any] | None = None,
) -> int:
    args = parser().parse_args(argv)
    try:
        from datetime import datetime as clock
        clock.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"result": "FAILED", "reason_code": "SOURCE_DATE_INVALID"}))
        return 2
    try:
        if args.dry_run:
            report = dry_run(args.config, runtime=args.runtime, ensure_fn=ensure_fn, release_fn=release_fn)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report.get("state") == "MATCH" else 20
        if args.check_only:
            ok, reason = eligibility_v2(args.runtime, args.date)
            print(json.dumps({"target_date": args.date, "eligible": ok, "reason_code": reason}, sort_keys=True))
            return 0
        record = retry_v2(
            args.date, runtime=args.runtime, config=args.config,
            ensure_fn=ensure_fn, release_fn=release_fn,
        )
    except daily.CollectorError as exc:
        print(json.dumps({"result": "FAILED", "reason_code": str(exc), "dry_run": bool(args.dry_run)}, sort_keys=True))
        return 20
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0 if record.get("result") in {"PASS", "PASS_PARTIAL", "NOT_ELIGIBLE", "ALREADY_RETRIED"} else 20
