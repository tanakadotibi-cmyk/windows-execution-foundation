"""Daily metrics entry that recovers a pre-contact CDP profile mismatch.

The original ``run`` refetches a failed receipt only when the failure is
``AUTH_REQUIRED``. A ``CDP_PORT_RUNTIME_MISMATCH`` receipt is written before the
reader contacts CityHeaven (auth still ``NOT_CHECKED``, ``reader_run_count`` 0).
Re-running that receipt used to return the stale failure. This wrapper archives
that receipt the same way the login-expired branch archives its predecessor,
then lets the original ``run`` fetch.

Chrome is checked only when this invocation will contact CityHeaven. A reused
PASS or other failed receipt does not stop any browser.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from . import cityheaven_daily_metrics as daily
from .cityheaven_reservation_chrome_ensure_v2 import (
    FAILURE,
    EnsureSession,
    RealIoBlocked,
    reservation_chrome_session,
    reservation_route,
)

EnsureFn = Callable[..., dict[str, Any]]


def cityheaven_contact_evidenced(previous: dict[str, Any]) -> bool:
    """True when the receipt shows the reader or a login attempt reached CityHeaven."""
    if not isinstance(previous, dict):
        return True
    if previous.get("auth_status") not in {None, "NOT_CHECKED"}:
        return True
    if previous.get("reader_run_count") not in {None, 0}:
        return True
    if previous.get("fetched_at") not in {None, ""}:
        return True
    if previous.get("source_fetched_at") not in {None, ""}:
        return True
    if previous.get("source_route") not in {None, ""}:
        return True
    if previous.get("route") not in {None, ""}:
        return True
    if previous.get("profile_identity_result") not in {None, ""}:
        return True
    if previous.get("runtime_identity_result") not in {None, ""}:
        return True
    if previous.get("cdp_port") not in {None, ""}:
        return True
    if previous.get("source_payload_persisted") is True:
        return True
    if previous.get("saved_login_recovery"):
        return True
    for key in ("cityheaven_write_count", "airtable_write_count", "business_write_count"):
        try:
            if int(previous.get(key) or 0) != 0:
                return True
        except (TypeError, ValueError):
            return True
    diagnostic = previous.get("reader_diagnostics_safe")
    if isinstance(diagnostic, dict) and diagnostic:
        stage = str(diagnostic.get("stage") or "")
        if stage not in {"", "FETCH"} or diagnostic.get("http_status") or diagnostic.get("last_stage"):
            return True
    return False


def mismatch_allows_rerun(previous: dict[str, Any]) -> bool:
    if not isinstance(previous, dict):
        return False
    if previous.get("status") != "FAILED" or previous.get("failure_code") != FAILURE:
        return False
    if previous.get("schema_version") != daily.SCHEMA:
        return False
    if previous.get("source_payload_persisted") is not False:
        return False
    if cityheaven_contact_evidenced(previous):
        return False
    return True


def release_mismatch_receipt_if_safe(runtime: Path, target_date: str) -> bool:
    """Archive a pre-contact mismatch receipt so the original run will fetch."""
    daily._check_date(target_date)
    runtime = daily._checked_runtime(runtime)
    if daily._read_saved_source(runtime, target_date) is not None:
        return False
    path = runtime / "receipts" / (target_date.replace("-", "") + ".json")
    if not path.exists():
        return False
    previous = daily._read_json(path)
    if previous.get("target_date") != target_date:
        return False
    if not mismatch_allows_rerun(previous):
        return False
    data = path.read_bytes()
    dest = path.parent / "history" / path.stem / (hashlib.sha256(data).hexdigest()[:16] + ".json")
    daily._publish_bytes(dest, data)
    path.unlink()
    return True


def will_contact_cityheaven(
    runtime: Path,
    target_date: str,
    *,
    offline_payload: dict[str, Any] | None,
    write_airtable: bool,
    request: Callable[..., dict[str, Any]],
) -> bool:
    """Mirror the original run's decision to call the fetcher. Do not restart Chrome otherwise."""
    if offline_payload is not None:
        return False
    if write_airtable and request is daily.airtable_request:
        return False
    daily._check_date(target_date)
    runtime = daily._checked_runtime(runtime)
    if daily._read_saved_source(runtime, target_date) is not None:
        return False
    path = runtime / "receipts" / (target_date.replace("-", "") + ".json")
    if not path.exists():
        return True
    previous = daily._read_json(path)
    if previous.get("target_date") != target_date or previous.get("schema_version") != daily.SCHEMA:
        return False
    if previous.get("status") not in {"PASS", "FAILED"}:
        return False
    if previous.get("source_payload_persisted") is not False:
        return False
    if previous.get("status") == "FAILED" and previous.get("failure_code") == "AUTH_REQUIRED":
        return True
    if mismatch_allows_rerun(previous):
        return True
    return False


def _emit_release(report: dict[str, Any] | None) -> None:
    if not isinstance(report, dict):
        return
    safe = {}
    for key in ("closed", "graceful", "killed_pids", "job", "profile_kept", "skipped_chrome_pids"):
        if key in report and report[key] is not None:
            safe[key] = report[key]
    print("CITYHEAVEN_CHROME_RELEASE:" + json.dumps(safe, sort_keys=True, separators=(",", ":")), file=sys.stderr)


@contextmanager
def open_reservation_chrome(
    configuration: dict[str, Any],
    *,
    runtime: Path,
    job: str,
    ensure_fn: EnsureFn | None = None,
    release_fn: Callable[[dict[str, Any] | None], Any] | None = None,
    session: EnsureSession | None = None,
) -> Iterator[Any]:
    """Acquire Chrome before the job and release it on every exit, including errors."""
    if ensure_fn is None and release_fn is None:
        context = reservation_chrome_session(
            configuration, runtime=runtime, job=job, session=session,
        )
        lease = context.__enter__()
        try:
            yield lease
        finally:
            try:
                context.__exit__(*sys.exc_info())
            finally:
                _emit_release(lease.release_report)
        return
    report: dict[str, Any] | None = None
    error: daily.CollectorError | None = None
    try:
        try:
            if ensure_fn is not None:
                report = ensure_fn(configuration, session=session)
        except daily.CollectorError as exc:
            error = exc
        yield SimpleNamespace(report=report, ensure_error=error, release_report=None)
    finally:
        if release_fn is not None:
            try:
                outcome = release_fn(report)
            except Exception:
                print("CITYHEAVEN_CHROME_RELEASE_FAILED", file=sys.stderr)
                raise
            if isinstance(outcome, dict):
                _emit_release(outcome)


def _emit_ensure(report: dict[str, Any]) -> None:
    safe = {}
    for key in (
        "state", "action", "relaunch_count", "killed_pids", "skipped_chrome_count",
        "verify_attempts_used", "execution_time_limit", "cdp_port",
    ):
        if key in report and report[key] is not None:
            safe[key] = report[key]
    print("CITYHEAVEN_ENSURE:" + json.dumps(safe, sort_keys=True, separators=(",", ":")), file=sys.stderr)


def _run_node_readiness(configuration: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("CITYHEAVEN_UNIT_TEST") == "1" and getattr(subprocess.run, "__module__", "") == "subprocess":
        raise RealIoBlocked("unit test blocked real node readiness")
    node, runtime = daily._reservation_runtime_config(configuration)
    adapter = (
        "const v2=require(process.argv[1]);"
        "const fs=require('node:fs');"
        "v2.dailyReadinessV2(JSON.parse(fs.readFileSync(0,'utf8')))"
        ".then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(e=>{process.stderr.write(String((e&&e.runtimeCode)||'BROWSER_CDP_UNAVAILABLE')+'\\n');process.exit(20)})"
    )
    env = os.environ.copy()
    if report.get("state") == "MATCH":
        env["CITYHEAVEN_PROFILE_CMDLINE_MATCH"] = "1"
    env["CITYHEAVEN_INSPECT_ATTEMPTS"] = "3"
    env["CITYHEAVEN_INSPECT_SPACING_MS"] = "3000"
    script = Path(__file__).with_name("cityheaven_reservation_runtime_ensure_v2.js")
    try:
        completed = subprocess.run(
            [node, "-e", adapter, str(script)],
            input=json.dumps(runtime, ensure_ascii=False, separators=(",", ":")),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, check=False, env=env,
            creationflags=daily.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise daily.CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if completed.returncode != 0:
        code = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "BROWSER_CDP_UNAVAILABLE"
        allowed = {
            "BROWSER_CDP_UNAVAILABLE", "BROWSER_LAUNCH_FAILED", FAILURE,
            "CHROME_RUNTIME_CONFIG_INVALID", "CHROME_RUNTIME_UNAVAILABLE", "CHROME_START_ALREADY_ATTEMPTED",
            "CHROME_START_IN_PROGRESS", "CHROME_START_LOCK_FAILED", "CDP_DISCOVERY_FAILED", "STOPPED",
        }
        raise daily.CollectorError(code if code in allowed else "BROWSER_CDP_UNAVAILABLE")
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise daily.CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if not isinstance(value, dict) or value.get("runtime_identity") != "MATCH" or value.get("profile_identity") != "MATCH":
        raise daily.CollectorError(FAILURE)
    if value.get("cdp_reachable") is not True:
        raise daily.CollectorError("BROWSER_CDP_UNAVAILABLE")
    value["browser_restart_count"] = int(report.get("relaunch_count") or 0)
    value["additional_browser_launch_count"] = int(report.get("relaunch_count") or 0)
    return value


def fetch_v2(
    target_date: str,
    *,
    config: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Original fetch, with reservation readiness that retries a misread profile."""

    def patched(current: dict[str, Any]) -> tuple[dict[str, Any], str]:
        value = _run_node_readiness(current, report)
        node, _runtime = daily._reservation_runtime_config(current)
        return value, node

    original = daily.ensure_reservation_browser
    daily.ensure_reservation_browser = patched
    try:
        return daily.fetch(target_date, config=config)
    finally:
        daily.ensure_reservation_browser = original


def run_v2(
    target_date: str,
    *,
    write_airtable: bool,
    runtime: Path = daily.DEFAULT_RUNTIME,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    request: Callable[..., dict[str, Any]] = daily.airtable_request,
    offline_payload: dict[str, Any] | None = None,
    config: Path = daily.DEFAULT_CONFIG,
    ensure_fn: EnsureFn | None = None,
    release_fn: Callable[[dict[str, Any] | None], Any] | None = None,
    session: EnsureSession | None = None,
) -> dict[str, Any]:
    session = session or EnsureSession()
    release_mismatch_receipt_if_safe(runtime, target_date)
    contact = will_contact_cityheaven(
        runtime, target_date, offline_payload=offline_payload,
        write_airtable=write_airtable, request=request,
    )
    configuration = daily._load_configuration(config) if contact else None
    if not contact or not reservation_route(configuration or {}):
        def plain(date: str) -> dict[str, Any]:
            if fetcher is not None:
                return fetcher(date)
            return daily.fetch(date, config=config)

        return daily.run(
            target_date, write_airtable=write_airtable, runtime=runtime,
            fetcher=plain, request=request, offline_payload=offline_payload,
        )

    with open_reservation_chrome(
        configuration, runtime=runtime, job="daily", ensure_fn=ensure_fn,
        release_fn=release_fn, session=session,
    ) as lease:
        report = lease.report
        ensure_error = lease.ensure_error
        if report is not None and ensure_error is None:
            _emit_ensure(report)
        elif ensure_error is not None:
            print("CITYHEAVEN_ENSURE_FAILED:" + FAILURE, file=sys.stderr)

        def bound(date: str) -> dict[str, Any]:
            if ensure_error is not None:
                raise daily.CollectorError(FAILURE)
            if fetcher is not None:
                return fetcher(date)
            if report is not None:
                return fetch_v2(date, config=config, report=report)
            return daily.fetch(date, config=config)

        return daily.run(
            target_date, write_airtable=write_airtable, runtime=runtime,
            fetcher=bound, request=request, offline_payload=offline_payload,
        )


def dry_run(
    config: Path,
    *,
    runtime: Path = daily.DEFAULT_RUNTIME,
    ensure_fn: EnsureFn | None = None,
    release_fn: Callable[[dict[str, Any] | None], Any] | None = None,
    session: EnsureSession | None = None,
) -> dict[str, Any]:
    """Launch reservation Chrome if it is down, then release the hold. Does not contact CityHeaven."""
    configuration = daily._load_configuration(config)
    if not reservation_route(configuration):
        raise daily.CollectorError("BROWSER_CDP_UNAVAILABLE")
    with open_reservation_chrome(
        configuration, runtime=runtime, job="dry-run", ensure_fn=ensure_fn,
        release_fn=release_fn, session=session,
    ) as lease:
        if lease.ensure_error is not None:
            raise lease.ensure_error
        report = dict(lease.report or {})
        _emit_ensure(report)
        report["cityheaven_fetch_count"] = 0
        report["dry_run"] = True
        return report


def parser() -> argparse.ArgumentParser:
    value = daily.parser()
    value.add_argument("--config", type=Path, default=daily.DEFAULT_CONFIG)
    value.add_argument(
        "--dry-run", action="store_true",
        help="launch reservation Chrome on port 9227 if it is down, then close it when this process is the last holder; do not contact CityHeaven",
    )
    return value


def main(argv: list[str] | None = None, *, ensure_fn: EnsureFn | None = None, release_fn: Callable[[dict[str, Any] | None], Any] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"status": "FAILED", "failure_code": "SOURCE_MISSING"}))
        return 2
    try:
        if args.dry_run:
            report = dry_run(args.config, runtime=args.runtime, ensure_fn=ensure_fn, release_fn=release_fn)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report.get("state") == "MATCH" else 20
        receipt = run_v2(
            args.date, write_airtable=args.write_airtable, runtime=args.runtime,
            config=args.config, ensure_fn=ensure_fn, release_fn=release_fn,
        )
    except daily.ReceiptSaveError as error:
        print(json.dumps(error.safe_diagnostics, ensure_ascii=False, sort_keys=True))
        return 20
    except daily.CollectorError as exc:
        print(json.dumps({"status": "FAILED", "failure_code": str(exc), "dry_run": bool(args.dry_run)}, sort_keys=True))
        return 20
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt.get("status") == "PASS" else 20
