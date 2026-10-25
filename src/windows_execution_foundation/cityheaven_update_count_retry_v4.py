"""14:30 update-count retry that appends to Google Sheets after a morning failure.

Eligibility is ``eligibility_v2`` (same decision as the v2 retry). A day the
morning job did not fail is not fetched and is not written. When the retry
completes, the merged analysis is appended with the v4 writer. The reservation
Chrome is closed on every path that opened it.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import cityheaven_daily_metrics as daily
from . import cityheaven_daily_metrics_v4 as v4
from .cityheaven_update_count_retry_v2 import eligibility_v2, retry_v2

SCHEMA = "cityheaven_update_count_retry_v4/1.0"
Fetcher = Callable[[str], dict[str, Any]]


def _base(target_date: str, *, dry_run: bool, test_tab: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "target_date": target_date,
        "status": "FAILED",
        "failure_code": None,
        "dry_run": dry_run,
        "test_tab": test_tab,
        "writer": v4.WRITER,
        "spreadsheet_id": v4.SPREADSHEET_ID,
        "sheet_write_count": 0,
        "airtable_write_count": 0,
        "browser_closed": False,
        "credential_secret_output_count": 0,
        "pii_scan_status": "NOT_CHECKED",
    }


def _analysis_path(runtime: Path, target_date: str) -> Path:
    return runtime / "offline-source" / "analysis" / f"{target_date.replace('-', '')}.json"


def _receipt_path(runtime: Path, target_date: str) -> Path:
    return runtime / "receipts" / f"{target_date.replace('-', '')}.json"


def _publish_saved(
    target_date: str,
    runtime: Path,
    *,
    mode: str,
    titles: dict[str, str],
    transport: Any | None,
    dry_run: bool,
    create_missing: bool,
    allow_partial: bool,
    now: datetime | None,
) -> dict[str, Any]:
    analysis, _raw = v4._read_json_file(_analysis_path(runtime, target_date), "OFFLINE_ANALYSIS_MISSING")
    if analysis.get("target_date") != target_date:
        raise v4.SheetError("ANALYSIS_DATE_MISMATCH")
    receipt_file = _receipt_path(runtime, target_date)
    if receipt_file.is_file():
        receipt, receipt_bytes = v4._read_json_file(receipt_file, "OFFLINE_ANALYSIS_MISSING")
        meta = v4.meta_from_receipt(receipt, receipt_bytes)
    else:
        meta = v4.meta_from_receipt({})
    written_at = (now or datetime.now(daily.JST)).astimezone(daily.JST).isoformat(timespec="seconds")
    return v4.publish_analysis(
        analysis,
        meta=meta,
        mode=mode,
        written_at=written_at,
        titles=titles,
        transport=transport,
        dry_run=dry_run,
        create_missing=create_missing,
        allow_partial=allow_partial,
    )


def run_retry_v4(
    target_date: str,
    *,
    runtime: Path = daily.DEFAULT_RUNTIME,
    v4_runtime: Path | None = None,
    config: Path = daily.DEFAULT_CONFIG,
    credential: Path = v4.DEFAULT_CREDENTIAL,
    dry_run: bool = False,
    test_tab: str | None = None,
    mode: str | None = None,
    check_only: bool = False,
    fetcher: Fetcher | None = None,
    ensure_fn: v4.EnsureFn | None = None,
    release_fn: v4.ReleaseFn | None = None,
    transport: Any | None = None,
    transport_loader: Callable[[Path], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = _base(target_date, dry_run=dry_run, test_tab=test_tab)
    browser: dict[str, Any] = {"entered": False, "released": False}
    scrubber = v4.get_scrubber()
    selected_mode = "NATURAL"
    try:
        daily._check_date(target_date)
        titles = v4.resolve_tabs(test_tab)
        selected_mode = v4.resolve_mode(mode, offline=False)
        result["mode"] = selected_mode
        result["tabs"] = titles
        if check_only:
            ok, reason = eligibility_v2(runtime, target_date)
            result.update({
                "status": "PASS", "eligible": ok, "reason_code": reason,
                "failure_code": None, "sheet_write_count": 0,
            })
            return result
        if dry_run:
            ok, reason = eligibility_v2(runtime, target_date)
            result.update({"eligible": ok, "reason_code": reason, "dry_run": True})
            if _analysis_path(runtime, target_date).is_file():
                outcome = _publish_saved(
                    target_date, runtime, mode=selected_mode, titles=titles,
                    transport=transport, dry_run=True, create_missing=False,
                    allow_partial=True, now=now,
                )
                result.update(outcome)
            result["status"] = "PASS"
            result["failure_code"] = None
            result["sheet_write_count"] = 0
            result["dry_run"] = True
            return result

        def collect() -> dict[str, Any]:
            return retry_v2(
                target_date, runtime=runtime, fetcher=fetcher, ensure_fn=ensure_fn,
                release_fn=release_fn, config=config,
            )

        record = v4.call_under_chrome(collect, browser)
        result["retry_result"] = record.get("result")
        result["reason_code"] = record.get("reason_code")
        result["airtable_write_count"] = 0
        if record.get("result") not in {"PASS", "PASS_PARTIAL"}:
            passed = record.get("result") in {"NOT_ELIGIBLE", "ALREADY_RETRIED"}
            result["status"] = "PASS" if passed else "FAILED"
            result["failure_code"] = None if passed else record.get("reason_code")
            result["sheet_write_count"] = 0
            return result
        if transport is None:
            loader = transport_loader or v4.open_transport
            transport = loader(credential)
        outcome = _publish_saved(
            target_date, runtime, mode=selected_mode, titles=titles, transport=transport,
            dry_run=False, create_missing=test_tab is not None, allow_partial=False, now=now,
        )
        result.update(outcome)
    except AssertionError:
        raise
    except v4.SheetError as exc:
        result["status"] = "FAILED"
        result["failure_code"] = exc.code
        for key, value in exc.details.items():
            if key not in {"status", "browser_closed", "airtable_write_count", "schema_version"}:
                result[key] = value
        if exc.code == "PII_SCAN_FAILED":
            result["pii_scan_status"] = "FAIL"
    except daily.CollectorError as exc:
        code = str(exc)
        result["status"] = "FAILED"
        result["failure_code"] = code if code.isascii() and len(code) <= 80 else "COLLECTOR_FAILED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["failure_code"] = "SHEET_UNEXPECTED_" + type(exc).__name__
    finally:
        v4._finalize_browser(browser, release_fn, None if dry_run or check_only else config)
        result["browser_closed"] = (not browser.get("entered")) or bool(browser.get("released"))
        result["airtable_write_count"] = 0
        result["writer"] = v4.WRITER
        result["mode"] = result.get("mode") or selected_mode
        report = browser.get("release_report")
        if isinstance(report, dict) and "skipped_chrome_pids" in report:
            result["skipped_chrome_pids"] = list(report.get("skipped_chrome_pids") or [])
        result["completed_at"] = datetime.now(daily.JST).isoformat(timespec="seconds")
        v4._stamp_secret_count(result, scrubber)
        if v4_runtime is not None:
            try:
                root = v4._checked_v4_runtime(v4_runtime)
                stem = target_date.replace("-", "")
                if len(stem) == 8 and stem.isdigit():
                    v4._save_json(root / "receipts" / f"retry-{stem}.json", result, scrubber)
                    result["receipt_path"] = str(root / "receipts" / f"retry-{stem}.json")
                    if dry_run:
                        v4._save_json(root / "dry-run" / f"retry-{stem}.json", result, scrubber)
                        result["dry_run_path"] = str(root / "dry-run" / f"retry-{stem}.json")
            except AssertionError:
                raise
            except Exception:
                result["receipt_save_error"] = "RECEIPT_SAVE_FAILED"
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="CityHeaven 14:30 update-count retry v4")
    value.add_argument("--date", default=daily.previous_jst_day())
    value.add_argument("--runtime", type=Path, default=daily.DEFAULT_RUNTIME)
    value.add_argument("--v4-runtime", type=Path, default=None)
    value.add_argument("--config", type=Path, default=daily.DEFAULT_CONFIG)
    value.add_argument("--credential", type=Path, default=v4.DEFAULT_CREDENTIAL)
    value.add_argument("--dry-run", action="store_true", help="print planned rows from the saved analysis and do not append")
    value.add_argument("--test-tab", default=None)
    value.add_argument("--mode", choices=("NATURAL", "MANUAL"), default=None)
    value.add_argument("--check-only", action="store_true", help="read-only eligibility check")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    scrubber = v4.get_scrubber()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(scrubber.apply(json.dumps({
            "result": "FAILED", "reason_code": "SOURCE_DATE_INVALID", "status": "FAILED",
            "failure_code": "SOURCE_DATE_INVALID", "airtable_write_count": 0,
            "browser_closed": True, "sheet_write_count": 0,
        }, sort_keys=True)))
        return 2
    result = run_retry_v4(
        args.date,
        runtime=args.runtime,
        v4_runtime=args.v4_runtime if args.v4_runtime is not None else v4.default_v4_runtime(),
        config=args.config,
        credential=args.credential,
        dry_run=args.dry_run,
        test_tab=args.test_tab,
        mode=args.mode,
        check_only=args.check_only,
    )
    text = scrubber.apply(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(text)
    passed = result.get("status") == "PASS"
    return 0 if passed else 20


if __name__ == "__main__":
    raise SystemExit(main())
