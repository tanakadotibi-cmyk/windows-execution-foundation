"""CityHeaven daily metrics v4: same fetch as v2, append-only Google Sheets.

Collection calls ``run_v2(..., write_airtable=False)`` and never reads or writes
Airtable. Rows match the City tabs in ``box_parallel`` (same columns, dedup
keys, and metric values). Receipts for this writer live under
``%LOCALAPPDATA%\\WindowsExecutionFoundation\\cityheaven-daily-v4``, separate
from the v3 collection runtime.

``--dry-run`` prints the rows and does not append. ``--test-tab SUFFIX`` writes
``city_metric_detail__SUFFIX`` (and the summary / run_log twins) instead of the
production tabs. A suffix that names a production tab is rejected. The
reservation Chrome on port 9227 is closed on success, failure, and exceptions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

from . import cityheaven_daily_metrics as daily
from . import cityheaven_daily_metrics_v2 as v2
from .core import atomic_write_json

SPREADSHEET_ID = "1R_xQV6AYIwRRK1fW-AInwm2rXoThmOFRhNSjgeH8swI"
SCHEMA = "cityheaven_daily_metrics_v4/1.0"
WRITER = "cityheaven_daily_v4"
BUSINESS_ID = "biz_epron_lady"
VERSION = "cityheaven_daily_v1"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
EXPECTED_CLIENT_EMAIL = "<SA_EMAIL>"
DEFAULT_CREDENTIAL = Path(r"C:\TantanJobs\secrets\sheets_writer_sa.dpapi")
API = "https://sheets.googleapis.com/v4/spreadsheets/" + SPREADSHEET_ID
PRODUCTION_TABS = ("city_metric_detail", "city_daily_summary", "run_log")
REV = ["revision", "is_current", "superseded_by_revision", "correction_reason"]
COMMON = ["run_id", "written_at", "writer"]
DETAIL_COLUMNS = [
    "dedup_key", *REV,
    "metric_record_id", "business_date", "girl_id", "store_alias", "metric",
    "value", "value_state", "header_sha256", "raw_source_sha256", *COMMON,
]
SUMMARY_COLUMNS = [
    "summary_key", *REV,
    "business_date", "metric", "observed_sum", "numeric_cast_count",
    "missing_cast_count", "input_cast_count", "roster_coverage_status", "status",
    *COMMON,
]
RUN_LOG_COLUMNS = [
    "run_id", "source", "business_date", "mode", "natural_run", "status",
    "failure_code", "collector_sha256", "receipt_sha256", "started_at",
    "completed_at", "facts_count", "rows_planned", "rows_written", "rows_replayed",
    "rows_revised", "duplicate_count", "unknown_count", "unknown_overwrite_count",
    "pii_scan_status", "sheet_backup_ref", "written_at", "writer",
]
COLUMNS = {
    "city_metric_detail": DETAIL_COLUMNS,
    "city_daily_summary": SUMMARY_COLUMNS,
    "run_log": RUN_LOG_COLUMNS,
}
KEY_COLUMN = {
    "city_metric_detail": "dedup_key",
    "city_daily_summary": "summary_key",
    "run_log": "run_id",
}
# Identity and metric value. Timestamps, run_id and the writer name are not
# compared: a second run of the same day replays instead of appending.
COMPARE_COLUMNS = {
    "city_metric_detail": [
        "metric_record_id", "business_date", "girl_id", "store_alias", "metric",
        "value", "value_state",
    ],
    "city_daily_summary": [
        "business_date", "metric", "observed_sum", "numeric_cast_count",
        "missing_cast_count", "input_cast_count", "roster_coverage_status", "status",
    ],
    "run_log": [
        "source", "business_date", "mode", "natural_run", "status", "failure_code",
        "collector_sha256", "facts_count", "unknown_count", "pii_scan_status", "writer",
    ],
}
BLANK_NUMERIC = {"value", "observed_sum"}
SENSITIVE_KEYS = {
    "private_key", "private_key_id", "client_x509_cert_url", "token",
    "refresh_token", "client_secret", "id_token",
}
Fetcher = Callable[[str], dict[str, Any]]
EnsureFn = Callable[..., dict[str, Any]]
ReleaseFn = Callable[[dict[str, Any] | None], Any]


class SheetError(Exception):
    def __init__(self, code: str, **details: Any):
        super().__init__(code)
        self.code = code
        self.details = details


class Scrubber:
    """Remembers secret strings so they cannot reach stdout, logs, or receipts."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def clear(self) -> None:
        self._parts.clear()

    def remember(self, value: Any) -> None:
        if isinstance(value, (bytes, bytearray)):
            try:
                text = bytes(value).decode("utf-8")
            except UnicodeError:
                return
            self._remember_text(text)
            return
        if isinstance(value, str):
            self._remember_text(value)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in SENSITIVE_KEYS or "private" in str(key).lower() or "secret" in str(key).lower():
                    self.remember(item)
            return
        if isinstance(value, list):
            for item in value:
                self.remember(item)

    def _remember_text(self, text: str) -> None:
        if len(text) < 12 or text in self._parts:
            return
        self._parts.append(text)
        escaped = json.dumps(text, ensure_ascii=False)[1:-1]
        if escaped != text and len(escaped) >= 12 and escaped not in self._parts:
            self._parts.append(escaped)

    def count(self, text: str) -> int:
        return sum(text.count(part) for part in self._parts if part)

    def apply(self, text: str) -> str:
        redacted = text
        for part in sorted(self._parts, key=len, reverse=True):
            if part and part in redacted:
                redacted = redacted.replace(part, "[REDACTED]")
        return redacted


_SCRUBBER = Scrubber()


def get_scrubber() -> Scrubber:
    return _SCRUBBER


def default_v4_runtime() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "WindowsExecutionFoundation" / "cityheaven-daily-v4"
    return Path(__file__).resolve().parents[2] / "local" / "WindowsExecutionFoundation" / "cityheaven-daily-v4"


def v4_runtime_from_argv(argv: list[str]) -> Path:
    if "--v4-runtime" in argv:
        index = argv.index("--v4-runtime")
        if index + 1 < len(argv):
            return Path(argv[index + 1])
    return default_v4_runtime()


def resolve_tabs(suffix: str | None) -> dict[str, str]:
    """Map logical tab names to the sheet titles this run writes."""
    if suffix is None:
        return {name: name for name in PRODUCTION_TABS}
    token = str(suffix).strip()
    if token in PRODUCTION_TABS or token.casefold() in {name.casefold() for name in PRODUCTION_TABS}:
        raise SheetError("TEST_TAB_REJECTED")
    if not token or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", token):
        raise SheetError("TEST_TAB_REJECTED")
    titles = {name: f"{name}__{token}" for name in PRODUCTION_TABS}
    if any(title in PRODUCTION_TABS for title in titles.values()):
        raise SheetError("TEST_TAB_REJECTED")
    return titles


def resolve_mode(mode: str | None, *, offline: bool) -> str:
    if mode is not None:
        if mode not in {"NATURAL", "MANUAL"}:
            raise SheetError("MODE_INVALID")
        return mode
    return "MANUAL" if offline else "NATURAL"


def run_id_for(target_date: str, mode: str) -> str:
    return f"CITYHEAVEN_DAILY_V4_{target_date.replace('-', '')}_{mode}"


def _sheet_cell(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _norm(column: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if column in BLANK_NUMERIC or column.endswith("_count") or column in {
        "revision", "facts_count", "rows_planned", "rows_written", "rows_replayed",
        "rows_revised", "duplicate_count",
    }:
        if value == "":
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value).strip()
        if number.is_integer():
            return str(int(number))
        return str(number)
    if column in {"is_current", "natural_run"}:
        return str(value).strip().upper()
    if column == "girl_id":
        return str(value).strip()
    return str(value).strip()


def _differ(logical: str, planned: dict[str, Any], existing: dict[str, Any]) -> list[str]:
    fields = []
    for column in COMPARE_COLUMNS[logical]:
        if _norm(column, planned.get(column, "")) != _norm(column, existing.get(column, "")):
            fields.append(column)
    return fields


def meta_from_receipt(receipt: dict[str, Any], receipt_bytes: bytes | None = None) -> dict[str, Any]:
    failure = receipt.get("failure_code")
    return {
        "collector_sha256": str(receipt.get("reader_sha256") or ""),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest() if receipt_bytes else "",
        "started_at": str(receipt.get("started_at") or ""),
        "completed_at": str(receipt.get("completed_at") or ""),
        "failure_code": "" if failure in {None, "null"} else str(failure),
        "raw_source_sha256": str(receipt.get("raw_source_sha256") or receipt.get("source_sha256") or ""),
    }


def _girl_id(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        raise SheetError("ANALYSIS_ROW_INVALID")
    return text


def _metric(value: Any) -> str:
    text = str(value or "")
    if text not in daily.METRICS:
        raise SheetError("ANALYSIS_ROW_INVALID")
    return text


def build_rows(
    analysis: dict[str, Any],
    *,
    raw_source_sha256: str,
    run_id: str,
    written_at: str,
    writer: str = WRITER,
) -> dict[str, list[dict[str, Any]]]:
    """Build city_metric_detail and city_daily_summary rows.

    The key and value rules are the ones in ``box_parallel/dryrun_build_rows.py``
    ``build_city``. UNKNOWN stays a blank value. Confirmed 0 stays 0.
    """
    target_date = str(analysis.get("target_date") or "")
    daily._check_date(target_date)
    numeric = analysis.get("numeric_rows")
    missing = analysis.get("missing_cells")
    metrics = analysis.get("metrics")
    if not isinstance(numeric, list) or not isinstance(missing, list) or not isinstance(metrics, dict):
        raise SheetError("ANALYSIS_ROW_INVALID")
    if set(metrics) != set(daily.METRICS):
        raise SheetError("ANALYSIS_ROW_INVALID")
    common = {"run_id": run_id, "written_at": written_at, "writer": writer}
    revision = {
        "revision": 1, "is_current": "TRUE", "superseded_by_revision": "", "correction_reason": "",
    }
    detail: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(girl: str, metric: str, value: Any, state: str) -> None:
        identity = f"{BUSINESS_ID}|cityheaven_girl:{girl}|{target_date}|cityheaven_unit_{metric}_daily_v1|{VERSION}"
        if (girl, metric) in seen:
            raise SheetError("PLAN_DUPLICATE_KEY")
        seen.add((girl, metric))
        detail.append({
            "dedup_key": identity,
            **revision,
            "metric_record_id": "bm_cityheaven_daily_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "business_date": target_date,
            "girl_id": girl,
            "store_alias": "",
            "metric": metric,
            "value": value,
            "value_state": state,
            "header_sha256": "",
            "raw_source_sha256": raw_source_sha256,
            **common,
        })

    for row in numeric:
        if not isinstance(row, dict):
            raise SheetError("ANALYSIS_ROW_INVALID")
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise SheetError("ANALYSIS_ROW_INVALID")
        add(_girl_id(row.get("girl_id")), _metric(row.get("metric")), value, "CONFIRMED")
    for row in missing:
        if not isinstance(row, dict):
            raise SheetError("ANALYSIS_ROW_INVALID")
        add(_girl_id(row.get("girl_id")), _metric(row.get("metric")), "", "UNKNOWN")
    cast_count = analysis.get("cast_count")
    if isinstance(cast_count, bool) or not isinstance(cast_count, int) or len(detail) != cast_count * len(daily.METRICS):
        raise SheetError("ANALYSIS_ROW_COUNT")
    detail.sort(key=lambda row: row["dedup_key"])
    summary: list[dict[str, Any]] = []
    for metric in sorted(metrics):
        stats = metrics[metric]
        if not isinstance(stats, dict):
            raise SheetError("ANALYSIS_ROW_INVALID")
        values = [row["value"] for row in detail if row["metric"] == metric and row["value_state"] == "CONFIRMED"]
        observed = stats.get("observed_sum")
        expected = sum(values) if values else None
        if observed != expected:
            raise SheetError("ANALYSIS_SUM_MISMATCH")
        if any(isinstance(stats.get(name), bool) or not isinstance(stats.get(name), int) for name in (
            "numeric_cast_count", "missing_cast_count", "input_cast_count",
        )):
            raise SheetError("ANALYSIS_ROW_INVALID")
        summary.append({
            "summary_key": f"{BUSINESS_ID}|{target_date}|{metric}|{VERSION}",
            **revision,
            "business_date": target_date,
            "metric": metric,
            "observed_sum": observed,
            "numeric_cast_count": stats["numeric_cast_count"],
            "missing_cast_count": stats["missing_cast_count"],
            "input_cast_count": stats["input_cast_count"],
            "roster_coverage_status": str(analysis.get("roster_coverage_status") or ""),
            "status": str(analysis.get("status") or ""),
            **common,
        })
    summary.sort(key=lambda row: row["summary_key"])
    return {"city_metric_detail": detail, "city_daily_summary": summary}


def build_run_log(
    analysis: dict[str, Any],
    detail: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    *,
    run_id: str,
    mode: str,
    written_at: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    unknown = sum(1 for row in detail if row["value_state"] == "UNKNOWN")
    status = "PASS" if analysis.get("status") == "COMPLETE" else "PASS_WITH_GAPS"
    numeric_count = analysis.get("numeric_cell_count")
    if not isinstance(numeric_count, int) or isinstance(numeric_count, bool):
        numeric_count = sum(1 for row in detail if row["value_state"] == "CONFIRMED")
    row = {column: "" for column in RUN_LOG_COLUMNS}
    row.update({
        "run_id": run_id,
        "source": "CITY",
        "business_date": str(analysis.get("target_date") or ""),
        "mode": mode,
        "natural_run": "TRUE" if mode == "NATURAL" else "FALSE",
        "status": status,
        "failure_code": str(meta.get("failure_code") or ""),
        "collector_sha256": str(meta.get("collector_sha256") or ""),
        "receipt_sha256": str(meta.get("receipt_sha256") or ""),
        "started_at": str(meta.get("started_at") or ""),
        "completed_at": str(meta.get("completed_at") or ""),
        "facts_count": numeric_count,
        "rows_planned": len(detail) + len(summary),
        "rows_written": 0,
        "rows_replayed": 0,
        "rows_revised": 0,
        "duplicate_count": 0,
        "unknown_count": unknown,
        "unknown_overwrite_count": 0,
        "pii_scan_status": "PASS",
        "sheet_backup_ref": "",
        "written_at": written_at,
        "writer": WRITER,
    })
    return row


def scan_rows(tables: dict[str, list[dict[str, Any]]]) -> bool:
    if not daily.pii_guard(tables):
        return False
    for rows in tables.values():
        for row in rows:
            if row.get("store_alias") not in {None, ""}:
                return False
            for key in row:
                if daily._normalized(key) in daily.PII_KEYS:
                    return False
    return True


def _pad(values: list[Any], width: int) -> list[Any]:
    row = list(values) + [""] * (width - len(values))
    return row[:width]


def load_index(
    values: list[list[Any]],
    columns: list[str],
    logical: str,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    if not values:
        raise SheetError("SHEET_SCHEMA_DRIFT")
    header = [str(cell) for cell in _pad(list(values[0]), len(columns))]
    if header != list(columns):
        raise SheetError("SHEET_SCHEMA_DRIFT")
    current: dict[str, dict[str, Any]] = {}
    present: set[str] = set()
    key_name = KEY_COLUMN[logical]
    for raw in values[1:]:
        record = {column: _pad(list(raw), len(columns))[index] for index, column in enumerate(columns)}
        key = str(record.get(key_name) or "").strip()
        if not key:
            continue
        present.add(key)
        if logical == "run_log":
            if key in current:
                raise SheetError("SHEET_DUPLICATE_DEDUP_KEY", key=key)
            current[key] = record
            continue
        if str(record.get("is_current") or "").strip().upper() != "TRUE":
            continue
        if key in current:
            raise SheetError("SHEET_DUPLICATE_DEDUP_KEY", key=key)
        current[key] = record
    return current, present


def _classify(
    logical: str,
    planned: list[dict[str, Any]],
    current: dict[str, dict[str, Any]],
    present: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    new_rows: list[dict[str, Any]] = []
    replay_rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unknown_overwrite = 0
    key_name = KEY_COLUMN[logical]
    for row in planned:
        key = str(row[key_name])
        if key not in present:
            new_rows.append(row)
            continue
        existing = current.get(key)
        if existing is None:
            conflicts.append({"tab": logical, "key": key, "fields": ["is_current"]})
            continue
        if logical == "city_metric_detail":
            existing_state = _norm("value_state", existing.get("value_state"))
            planned_state = _norm("value_state", row.get("value_state"))
            if existing_state == "CONFIRMED" and planned_state == "UNKNOWN":
                unknown_overwrite += 1
        fields = _differ(logical, row, existing)
        if fields:
            conflicts.append({"tab": logical, "key": key, "fields": fields})
        else:
            replay_rows.append(row)
    return new_rows, replay_rows, conflicts, unknown_overwrite


def _as_sheet_rows(rows: list[dict[str, Any]], columns: list[str]) -> list[list[Any]]:
    return [[_sheet_cell(row.get(column, "")) for column in columns] for row in rows]


class GoogleSheetTransport:
    """Spreadsheets v4 via an already-authorized session. Reads and appends only."""

    def __init__(self, session: Any):
        self._session = session

    def _ok(self, response: Any, code: str) -> dict[str, Any]:
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            if status == 400:
                raise SheetError("SHEET_TAB_MISSING")
            raise SheetError(f"{code}_{status}")
        try:
            payload = response.json()
        except Exception as exc:
            raise SheetError(code) from None
        if not isinstance(payload, dict):
            raise SheetError(code)
        return payload

    def titles(self) -> list[str]:
        response = self._session.get(API, params={"fields": "sheets.properties.title"}, timeout=60)
        payload = self._ok(response, "SHEET_READ_FAILED")
        found = []
        for sheet in payload.get("sheets") or []:
            if isinstance(sheet, dict):
                title = (sheet.get("properties") or {}).get("title")
                if isinstance(title, str):
                    found.append(title)
        return found

    def values(self, tab: str, columns: list[str]) -> list[list[Any]]:
        end = _column_letter(len(columns))
        url = f"{API}/values/{quote(tab, safe='')}!A1:{end}"
        response = self._session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=60)
        payload = self._ok(response, "SHEET_READ_FAILED")
        rows = payload.get("values") or []
        if not isinstance(rows, list):
            raise SheetError("SHEET_READ_FAILED")
        return rows

    def append_rows(self, tab: str, columns: list[str], rows: list[list[Any]]) -> None:
        if not rows:
            return
        end = _column_letter(len(columns))
        url = f"{API}/values/{quote(tab, safe='')}!A1:{end}1:append"
        response = self._session.post(
            url,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": rows},
            timeout=60,
        )
        self._ok(response, "SHEET_WRITE_FAILED")

    def add_sheet_with_header(self, tab: str, header: list[str]) -> None:
        if tab in PRODUCTION_TABS:
            raise SheetError("TEST_TAB_REJECTED")
        created = self._session.post(
            API + ":batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            timeout=60,
        )
        self._ok(created, "SHEET_CREATE_FAILED")
        end = _column_letter(len(header))
        url = f"{API}/values/{quote(tab, safe='')}!A1:{end}1"
        written = self._session.put(
            url,
            params={"valueInputOption": "RAW"},
            json={"values": [header]},
            timeout=60,
        )
        self._ok(written, "SHEET_HEADER_FAILED")


def _column_letter(count: int) -> str:
    letters = ""
    index = count
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def dpapi_decrypt(data: bytes) -> bytes:
    """Current-user DPAPI (CryptUnprotectData flag 0). The plaintext is not logged."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SheetError("CREDENTIAL_DECRYPT_FAILED")
    try:
        import ctypes
        import ctypes.wintypes as wintypes
    except AssertionError:
        raise
    except Exception:
        raise SheetError("CREDENTIAL_DECRYPT_FAILED") from None

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    try:
        raw = bytes(data)
        buffer = ctypes.create_string_buffer(raw, len(raw))
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        output = Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output),
        ):
            raise SheetError("CREDENTIAL_DECRYPT_FAILED")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
    except SheetError:
        raise
    except AssertionError:
        raise
    except Exception:
        raise SheetError("CREDENTIAL_DECRYPT_FAILED") from None


def open_authorized_session(
    path: Path,
    *,
    decrypt: Callable[[bytes], bytes] = dpapi_decrypt,
    scrubber: Scrubber | None = None,
) -> Any:
    scrubber = scrubber or get_scrubber()
    info: Any = None
    try:
        try:
            raw = decrypt(Path(path).read_bytes())
        except SheetError:
            raise
        except AssertionError:
            raise
        except Exception:
            raise SheetError("CREDENTIAL_DECRYPT_FAILED") from None
        scrubber.remember(raw)
        try:
            info = json.loads(raw.decode("utf-8"))
        except AssertionError:
            raise
        except Exception:
            raise SheetError("CREDENTIAL_DECRYPT_FAILED") from None
        if not isinstance(info, dict):
            raise SheetError("CREDENTIAL_DECRYPT_FAILED")
        scrubber.remember(info)
        if info.get("client_email") != EXPECTED_CLIENT_EMAIL:
            raise SheetError("CREDENTIAL_IDENTITY_MISMATCH")
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
            credentials = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
        except SheetError:
            raise
        except AssertionError:
            raise
        except Exception:
            raise SheetError("CREDENTIAL_AUTH_FAILED") from None
        scopes = list(getattr(credentials, "scopes", None) or [])
        if scopes != list(SCOPES):
            raise SheetError("CREDENTIAL_SCOPE_INVALID")
        try:
            return AuthorizedSession(credentials)
        except AssertionError:
            raise
        except Exception:
            raise SheetError("CREDENTIAL_AUTH_FAILED") from None
    finally:
        if isinstance(info, dict):
            for key in SENSITIVE_KEYS:
                if key in info:
                    info[key] = ""


def open_transport(path: Path, *, decrypt: Callable[[bytes], bytes] = dpapi_decrypt) -> GoogleSheetTransport:
    return GoogleSheetTransport(open_authorized_session(path, decrypt=decrypt))


def _read_json_file(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    file = Path(path)
    try:
        if not file.is_file():
            raise SheetError(code)
        if "sheets_writer_sa" in file.name:
            raise SheetError(code)
        if file.stat().st_size > 8_000_000:
            raise SheetError(code)
        data = file.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except SheetError:
        raise
    except AssertionError:
        raise
    except Exception:
        raise SheetError(code) from None
    if not isinstance(value, dict):
        raise SheetError(code)
    return value, data


def _analysis_writable(analysis: dict[str, Any], *, allow_partial: bool) -> None:
    status = analysis.get("status")
    allowed = {"COMPLETE", "PARTIAL"} if allow_partial else {"COMPLETE"}
    if status not in allowed:
        raise SheetError("ANALYSIS_NOT_COMPLETE")
    if not allow_partial and int(analysis.get("missing_cell_count") or 0) != 0:
        raise SheetError("ANALYSIS_NOT_COMPLETE")


def publish_analysis(
    analysis: dict[str, Any],
    *,
    meta: dict[str, Any],
    mode: str,
    written_at: str,
    titles: dict[str, str],
    transport: Any | None,
    dry_run: bool,
    create_missing: bool,
    allow_partial: bool,
) -> dict[str, Any]:
    _analysis_writable(analysis, allow_partial=allow_partial)
    run_id = run_id_for(str(analysis["target_date"]), mode)
    built = build_rows(
        analysis,
        raw_source_sha256=str(meta.get("raw_source_sha256") or ""),
        run_id=run_id,
        written_at=written_at,
    )
    log_row = build_run_log(
        analysis, built["city_metric_detail"], built["city_daily_summary"],
        run_id=run_id, mode=mode, written_at=written_at, meta=meta,
    )
    tables: dict[str, list[dict[str, Any]]] = {
        "city_metric_detail": built["city_metric_detail"],
        "city_daily_summary": built["city_daily_summary"],
        "run_log": [log_row],
    }
    if not scan_rows(tables):
        raise SheetError("PII_SCAN_FAILED", pii_scan_status="FAIL")
    preview: dict[str, Any] = {}
    if transport is None:
        for logical, rows in tables.items():
            preview[logical] = {
                "tab": titles[logical], "rows": rows, "planned": len(rows),
                "new": None, "existing": None, "replay": None,
            }
        return _publish_result(
            tables, preview, written=0, replayed=0, dry_run=True, sheet_read=False,
            run_id=run_id, unknown_overwrite=0,
        )
    loaded: dict[str, tuple[dict[str, dict[str, Any]], set[str]]] = {}
    sheet_read = True
    for logical, rows in tables.items():
        tab = titles[logical]
        columns = COLUMNS[logical]
        try:
            names = set(transport.titles())
        except SheetError as exc:
            if dry_run and _skippable_read(exc):
                sheet_read = False
                preview[logical] = {
                    "tab": tab, "rows": rows, "planned": len(rows),
                    "new": None, "existing": None, "replay": None,
                    "sheet_read_error": exc.code,
                }
                continue
            raise
        if tab not in names:
            if dry_run or not create_missing:
                if dry_run:
                    sheet_read = False
                    preview[logical] = {
                        "tab": tab, "rows": rows, "planned": len(rows),
                        "new": None, "existing": None, "replay": None,
                        "sheet_read_error": "SHEET_TAB_MISSING",
                    }
                    continue
                raise SheetError("SHEET_TAB_MISSING")
            transport.add_sheet_with_header(tab, columns)
        try:
            values = transport.values(tab, columns)
            loaded[logical] = load_index(values, columns, logical)
        except SheetError as exc:
            if dry_run and _skippable_read(exc):
                sheet_read = False
                preview[logical] = {
                    "tab": tab, "rows": rows, "planned": len(rows),
                    "new": None, "existing": None, "replay": None,
                    "sheet_read_error": exc.code,
                }
                continue
            raise
    if not sheet_read:
        for logical, rows in tables.items():
            preview.setdefault(logical, {
                "tab": titles[logical], "rows": rows, "planned": len(rows),
                "new": None, "existing": None, "replay": None,
            })
        return _publish_result(
            tables, preview, written=0, replayed=0, dry_run=True, sheet_read=False,
            run_id=run_id, unknown_overwrite=0,
        )
    detail_new, detail_replay, detail_conflicts, unknown_overwrite = _classify(
        "city_metric_detail", tables["city_metric_detail"], *loaded["city_metric_detail"],
    )
    summary_new, summary_replay, summary_conflicts, _ignored = _classify(
        "city_daily_summary", tables["city_daily_summary"], *loaded["city_daily_summary"],
    )
    conflicts = detail_conflicts + summary_conflicts
    if conflicts or unknown_overwrite:
        raise SheetError(
            "SHEET_CONTENT_CONFLICT",
            conflicts=conflicts,
            unknown_overwrite_count=unknown_overwrite,
        )
    log_row["rows_written"] = len(detail_new) + len(summary_new)
    log_row["rows_replayed"] = len(detail_replay) + len(summary_replay)
    log_row["unknown_overwrite_count"] = 0
    log_new, log_replay, log_conflicts, _ignored = _classify(
        "run_log", tables["run_log"], *loaded["run_log"],
    )
    if log_conflicts:
        raise SheetError("SHEET_CONTENT_CONFLICT", conflicts=log_conflicts, unknown_overwrite_count=0)
    data_written = len(detail_new) + len(summary_new)
    grouped = {
        "city_metric_detail": (detail_new, detail_replay),
        "city_daily_summary": (summary_new, summary_replay),
        "run_log": (log_new, log_replay),
    }
    for logical, (new_rows, replay_rows) in grouped.items():
        preview[logical] = {
            "tab": titles[logical],
            "rows": tables[logical] if dry_run else [],
            "planned": len(tables[logical]),
            "new": len(new_rows),
            "existing": len(replay_rows),
            "replay": len(replay_rows),
        }
    written = len(detail_new) + len(summary_new) + len(log_new)
    replayed = len(detail_replay) + len(summary_replay) + len(log_replay)
    if dry_run:
        return _publish_result(
            tables, preview, written=0, replayed=replayed, dry_run=True, sheet_read=True,
            run_id=run_id, unknown_overwrite=0, planned_appends=written, data_written=data_written,
        )
    for logical, (new_rows, _replay) in grouped.items():
        if new_rows:
            transport.append_rows(titles[logical], COLUMNS[logical], _as_sheet_rows(new_rows, COLUMNS[logical]))
    for logical, rows in tables.items():
        current, present = load_index(transport.values(titles[logical], COLUMNS[logical]), COLUMNS[logical], logical)
        for row in rows:
            key = str(row[KEY_COLUMN[logical]])
            existing = current.get(key)
            if key not in present or existing is None or _differ(logical, row, existing):
                raise SheetError("SHEET_READBACK_FAILED")
    return _publish_result(
        tables, preview, written=written, replayed=replayed, dry_run=False, sheet_read=True,
        run_id=run_id, unknown_overwrite=0, data_written=data_written,
    )


def _skippable_read(exc: SheetError) -> bool:
    if exc.code == "SHEET_SCHEMA_DRIFT" or exc.code == "SHEET_CONTENT_CONFLICT":
        return False
    if exc.code == "SHEET_DUPLICATE_DEDUP_KEY":
        return False
    return exc.code == "SHEET_TAB_MISSING" or exc.code.startswith("SHEET_READ_FAILED") or exc.code.startswith("CREDENTIAL_")


def _publish_result(
    tables: dict[str, list[dict[str, Any]]],
    preview: dict[str, Any],
    *,
    written: int,
    replayed: int,
    dry_run: bool,
    sheet_read: bool,
    run_id: str,
    unknown_overwrite: int,
    planned_appends: int | None = None,
    data_written: int = 0,
) -> dict[str, Any]:
    detail = tables["city_metric_detail"]
    summary = tables["city_daily_summary"]
    unknown = sum(1 for row in detail if row["value_state"] == "UNKNOWN")
    return {
        "status": "PASS",
        "failure_code": None,
        "run_id": run_id,
        "sheet_write_count": written,
        "planned_append_count": written if planned_appends is None else planned_appends,
        "rows_planned": len(detail) + len(summary),
        "rows_written": 0 if dry_run else data_written,
        "rows_replayed": replayed,
        "rows_revised": 0,
        "duplicate_count": 0,
        "unknown_count": unknown,
        "unknown_overwrite_count": unknown_overwrite,
        "pii_scan_status": "PASS",
        "sheet_read": sheet_read,
        "dry_run": dry_run,
        "preview": preview,
    }


def _checked_v4_runtime(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if "sheets_writer_sa" in str(resolved):
        raise SheetError("UNSAFE_RUNTIME_PATH")
    return resolved


def _save_json(path: Path, payload: dict[str, Any], scrubber: Scrubber) -> None:
    text = scrubber.apply(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    value = json.loads(text)
    atomic_write_json(path, value)


def _forbid_external_write(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise SheetError("WRITE_TARGET_FORBIDDEN")


def call_under_chrome(callback: Callable[[], Any], browser: dict[str, Any]) -> Any:
    """Run callback while reservation Chrome is always released by v2's session."""
    modules = [v2]
    retry_name = v2.__package__ + ".cityheaven_update_count_retry_v2"
    retry_mod = sys.modules.get(retry_name)
    if retry_mod is None:
        try:
            import importlib
            retry_mod = importlib.import_module(retry_name)
        except AssertionError:
            raise
        except Exception:
            retry_mod = None
    if retry_mod is not None and hasattr(retry_mod, "open_reservation_chrome"):
        modules.append(retry_mod)
    originals = {mod: mod.open_reservation_chrome for mod in modules}

    def make_wrapped(original: Any) -> Any:
        @contextmanager
        def wrapped(
            configuration: dict[str, Any],
            *,
            runtime: Path,
            job: str,
            ensure_fn: EnsureFn | None = None,
            release_fn: ReleaseFn | None = None,
            session: Any = None,
        ) -> Iterator[Any]:
            def tracking(report: dict[str, Any] | None) -> Any:
                outcome = release_fn(report) if release_fn is not None else None
                if isinstance(outcome, dict) and outcome.get("closed") is False:
                    browser["released"] = False
                else:
                    browser["released"] = True
                if isinstance(outcome, dict):
                    browser["release_report"] = outcome
                return outcome

            use_tracking = ensure_fn is not None or release_fn is not None
            lease: Any = None
            try:
                with original(
                    configuration, runtime=runtime, job=job, ensure_fn=ensure_fn,
                    release_fn=tracking if use_tracking else None, session=session,
                ) as lease:
                    browser["entered"] = True
                    yield lease
            finally:
                if lease is not None and not use_tracking:
                    report = getattr(lease, "release_report", None)
                    if isinstance(report, dict):
                        browser["release_report"] = report
                        browser["released"] = report.get("closed") is True
                    elif getattr(lease, "released", False):
                        browser["released"] = True

        return wrapped

    for mod, original in originals.items():
        mod.open_reservation_chrome = make_wrapped(original)
    try:
        return callback()
    finally:
        for mod, original in originals.items():
            mod.open_reservation_chrome = original


def _emergency_close(config: Path) -> dict[str, Any]:
    configuration = daily._load_configuration(config)
    from .cityheaven_reservation_chrome_ensure_v2 import (
        close_reservation_chrome,
        settings_from_configuration,
        windows_deps,
    )
    settings = settings_from_configuration(configuration)
    report = close_reservation_chrome(settings, windows_deps(settings.profile))
    report["job"] = "daily"
    return report


def _finalize_browser(
    browser: dict[str, Any],
    release_fn: ReleaseFn | None,
    config: Path | None,
) -> None:
    if not browser.get("entered") or browser.get("released"):
        return
    if release_fn is not None:
        try:
            outcome = release_fn(None)
        except AssertionError:
            raise
        except Exception:
            outcome = {"closed": False}
        browser["released"] = not (isinstance(outcome, dict) and outcome.get("closed") is False)
        return
    if config is None:
        return
    try:
        report = _emergency_close(config)
    except AssertionError:
        raise
    except Exception:
        browser["released"] = False
        return
    browser["release_report"] = report
    browser["released"] = report.get("closed") is True


def _load_analysis_for_receipt(receipt: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis_path = receipt.get("analysis_path")
    if not isinstance(analysis_path, str) or not analysis_path:
        raise SheetError("ANALYSIS_NOT_COMPLETE")
    analysis, _raw = _read_json_file(Path(analysis_path), "OFFLINE_ANALYSIS_INVALID")
    if analysis.get("target_date") != receipt.get("target_date"):
        raise SheetError("ANALYSIS_DATE_MISMATCH")
    receipt_path = None
    runtime_text = (receipt.get("invocation_safe") or {}).get("runtime")
    if isinstance(runtime_text, str) and receipt.get("target_date"):
        candidate = Path(runtime_text) / "receipts" / (str(receipt["target_date"]).replace("-", "") + ".json")
        if candidate.is_file():
            receipt_path = candidate
    receipt_bytes = receipt_path.read_bytes() if receipt_path is not None else None
    meta = meta_from_receipt(receipt, receipt_bytes)
    if not meta["raw_source_sha256"]:
        meta["raw_source_sha256"] = str(receipt.get("source_sha256") or "")
    return analysis, meta


def run_v4(
    target_date: str,
    *,
    runtime: Path = daily.DEFAULT_RUNTIME,
    v4_runtime: Path | None = None,
    config: Path = daily.DEFAULT_CONFIG,
    credential: Path = DEFAULT_CREDENTIAL,
    dry_run: bool = False,
    test_tab: str | None = None,
    offline_analysis: Path | None = None,
    offline_receipt: Path | None = None,
    mode: str | None = None,
    fetcher: Fetcher | None = None,
    ensure_fn: EnsureFn | None = None,
    release_fn: ReleaseFn | None = None,
    transport: Any | None = None,
    transport_loader: Callable[[Path], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    written_at = (now or datetime.now(daily.JST)).astimezone(daily.JST).isoformat(timespec="seconds")
    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "target_date": target_date,
        "status": "FAILED",
        "failure_code": None,
        "mode": None,
        "dry_run": bool(dry_run),
        "test_tab": test_tab,
        "writer": WRITER,
        "spreadsheet_id": SPREADSHEET_ID,
        "sheet_write_count": 0,
        "airtable_write_count": 0,
        "browser_closed": False,
        "credential_secret_output_count": 0,
        "pii_scan_status": "NOT_CHECKED",
        "started_at": written_at,
    }
    browser: dict[str, Any] = {"entered": False, "released": False}
    scrubber = get_scrubber()
    try:
        daily._check_date(target_date)
        titles = resolve_tabs(test_tab)
        selected_mode = resolve_mode(mode, offline=offline_analysis is not None)
        result["mode"] = selected_mode
        result["tabs"] = titles
        allow_partial = offline_analysis is not None
        if offline_analysis is not None:
            analysis, _bytes = _read_json_file(offline_analysis, "OFFLINE_ANALYSIS_MISSING")
            if analysis.get("target_date") != target_date:
                raise SheetError("ANALYSIS_DATE_MISMATCH")
            if offline_receipt is not None:
                receipt, receipt_bytes = _read_json_file(offline_receipt, "OFFLINE_ANALYSIS_MISSING")
                meta = meta_from_receipt(receipt, receipt_bytes)
            else:
                meta = meta_from_receipt({})
                if analysis.get("raw_source_sha256"):
                    meta["raw_source_sha256"] = str(analysis.get("raw_source_sha256"))
        else:
            def collect() -> dict[str, Any]:
                return v2.run_v2(
                    target_date,
                    write_airtable=False,
                    runtime=runtime,
                    fetcher=fetcher,
                    request=_forbid_external_write,
                    offline_payload=None,
                    config=config,
                    ensure_fn=ensure_fn,
                    release_fn=release_fn,
                )

            receipt = call_under_chrome(collect, browser)
            result["collection_status"] = receipt.get("status")
            result["collection_failure_code"] = receipt.get("failure_code")
            if receipt.get("status") != "PASS":
                result["failure_code"] = receipt.get("failure_code") or "SOURCE_MISSING"
                result["pii_scan_status"] = "NOT_CHECKED"
                return result
            analysis, meta = _load_analysis_for_receipt(receipt)
            allow_partial = False
        if transport is None and not dry_run:
            loader = transport_loader or open_transport
            transport = loader(credential)
        elif transport is None and dry_run:
            loader = transport_loader or open_transport
            try:
                transport = loader(credential)
            except SheetError as exc:
                if not _skippable_read(exc):
                    raise
                result["sheet_read_error"] = exc.code
                transport = None
        outcome = publish_analysis(
            analysis,
            meta=meta,
            mode=selected_mode,
            written_at=written_at,
            titles=titles,
            transport=transport,
            dry_run=dry_run or transport is None,
            create_missing=test_tab is not None and not dry_run,
            allow_partial=allow_partial,
        )
        # A live run must not treat a missing transport as success.
        if not dry_run and transport is None:
            raise SheetError("SHEET_READ_FAILED")
        result.update(outcome)
        if dry_run:
            result["dry_run"] = True
            result["sheet_write_count"] = 0
    except SheetError as exc:
        result["status"] = "FAILED"
        result["failure_code"] = exc.code
        for key, value in exc.details.items():
            if key not in {"status", "browser_closed", "airtable_write_count", "schema_version"}:
                result[key] = value
        if exc.code == "PII_SCAN_FAILED":
            result["pii_scan_status"] = "FAIL"
    except AssertionError:
        raise
    except daily.CollectorError as exc:
        code = str(exc)
        result["status"] = "FAILED"
        result["failure_code"] = code if code.isascii() and len(code) <= 80 else "COLLECTOR_FAILED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["failure_code"] = "SHEET_UNEXPECTED_" + type(exc).__name__
    finally:
        _finalize_browser(browser, release_fn, None if offline_analysis is not None else config)
        result["browser_closed"] = (not browser.get("entered")) or bool(browser.get("released"))
        result["airtable_write_count"] = 0
        result["writer"] = WRITER
        report = browser.get("release_report")
        if isinstance(report, dict) and "skipped_chrome_pids" in report:
            result["skipped_chrome_pids"] = list(report.get("skipped_chrome_pids") or [])
        result["completed_at"] = datetime.now(daily.JST).isoformat(timespec="seconds")
        if v4_runtime is not None and result.get("target_date"):
            try:
                root = _checked_v4_runtime(v4_runtime)
                stem = str(result["target_date"]).replace("-", "")
                if not re.fullmatch(r"[0-9]{8}", stem):
                    raise SheetError("UNSAFE_RUNTIME_PATH")
                _stamp_secret_count(result, scrubber)
                _save_json(root / "receipts" / f"{stem}.json", _public_result(result), scrubber)
                if dry_run:
                    _save_json(root / "dry-run" / f"{stem}.json", _public_result(result), scrubber)
                    result["dry_run_path"] = str(root / "dry-run" / f"{stem}.json")
                result["receipt_path"] = str(root / "receipts" / f"{stem}.json")
            except SheetError as exc:
                result["receipt_save_error"] = exc.code
            except AssertionError:
                raise
            except Exception:
                result["receipt_save_error"] = "RECEIPT_SAVE_FAILED"
        else:
            _stamp_secret_count(result, scrubber)
    return result


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    hidden = {"release_report"}
    return {key: value for key, value in result.items() if key not in hidden}


def _stamp_secret_count(result: dict[str, Any], scrubber: Scrubber) -> None:
    result["credential_secret_output_count"] = 0
    raw = json.dumps(_public_result(result), ensure_ascii=False, sort_keys=True)
    leaks = scrubber.count(raw)
    result["credential_secret_output_count"] = leaks


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="CityHeaven daily metrics v4 Google Sheets writer")
    value.add_argument("--date", default=daily.previous_jst_day())
    value.add_argument("--runtime", type=Path, default=daily.DEFAULT_RUNTIME, help="v3 collection runtime")
    value.add_argument("--v4-runtime", type=Path, default=None, help="receipt directory for this writer")
    value.add_argument("--config", type=Path, default=daily.DEFAULT_CONFIG)
    value.add_argument("--credential", type=Path, default=DEFAULT_CREDENTIAL)
    value.add_argument("--dry-run", action="store_true", help="print planned rows and do not append")
    value.add_argument("--test-tab", default=None, help="write city_metric_detail__SUFFIX instead of the production tab")
    value.add_argument("--offline-analysis", type=Path, default=None, help="build rows from a saved analysis and do not open Chrome")
    value.add_argument("--offline-receipt", type=Path, default=None)
    value.add_argument("--mode", choices=("NATURAL", "MANUAL"), default=None)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    scrubber = get_scrubber()
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(scrubber.apply(json.dumps({
            "status": "FAILED", "failure_code": "SOURCE_DATE_INVALID",
            "airtable_write_count": 0, "browser_closed": True, "sheet_write_count": 0,
        }, sort_keys=True)))
        return 2
    result = run_v4(
        args.date,
        runtime=args.runtime,
        v4_runtime=args.v4_runtime if args.v4_runtime is not None else default_v4_runtime(),
        config=args.config,
        credential=args.credential,
        dry_run=args.dry_run,
        test_tab=args.test_tab,
        offline_analysis=args.offline_analysis,
        offline_receipt=args.offline_receipt,
        mode=args.mode,
    )
    text = scrubber.apply(json.dumps(_public_result(result), ensure_ascii=False, sort_keys=True))
    print(text)
    return 0 if result.get("status") == "PASS" else 20


if __name__ == "__main__":
    raise SystemExit(main())
