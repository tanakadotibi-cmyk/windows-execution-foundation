from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .core import CREATE_NO_WINDOW, atomic_write_json

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
READER = Path(__file__).with_name("cityheaven_daily_reader.js")
OPERA_RUNTIME = Path(__file__).with_name("cityheaven_daily_opera_runtime.js")
RESERVATION_RUNTIME = Path(__file__).with_name("cityheaven_reservation_runtime_reader.js")
# Reader caps its count-based evaluate deadline at 300 s; leave 30 s for startup and exit.
READER_SUBPROCESS_TIMEOUT_SECONDS = 330
OPERA_BOOTSTRAP_TIMEOUT_SECONDS = 20
RESERVATION_BOOTSTRAP_TIMEOUT_SECONDS = 45
DEFAULT_CONFIG = ROOT / "local" / "config.json"
DEFAULT_RUNTIME = Path(os.environ.get("LOCALAPPDATA", str(ROOT / "local"))) / "WindowsExecutionFoundation" / "cityheaven-daily"
BRIDGE = Path(os.environ.get("LOCALAPPDATA", "")) / "TantanManagementOS" / "CodexBridge"
BASE_ID = "appdtl4ewm5XkOiGd"
TABLE_ID = "tblRB9Tueun2U38Ds"
BUSINESS_ID = "biz_epron_lady"
VERSION = "cityheaven_daily_v1"
SCHEMA = "cityheaven_daily_metrics_receipt/1.0"
WRITER_CREDENTIAL_ROUTE_CLASS = "CTI_BRIDGE_WATCHER_DPAPI_SECURE_STORE_EXISTING"
METRICS = ("access", "pc", "sp", "mygirl", "reviews", "mitene", "okini_people", "okini_sends", "diary_count")
OPERA_ROUTE = "OPERA"
RESERVATION_CHROME_REUSE_ROUTE = "RESERVATION_CHROME_REUSE"
INDEX_EVIDENCE = {"access": 0, "pc": 1, "sp": 2, "mygirl": 3, "reviews": 6, "mitene": 7, "okini_people": 9, "okini_sends": 10, "diary_count": 12}
ALIASES = {
    "access": (("アクセス", "合計"), ("access", "total")),
    "pc": (("pc",),), "sp": (("sp",),),
    "mygirl": (("マイガール",), ("mygirl",)),
    "reviews": (("口コミ",), ("レビュー",), ("review",)),
    "mitene": (("ミテネ",), ("見てね",), ("mitene",)),
    "okini_people": (("オキニ", "人数"), ("お気に", "人数"), ("recipient",)),
    "okini_sends": (("オキニ", "送信"), ("お気に", "送信"), ("send",)),
    "diary_count": (("写メ日記",), ("日記", "投稿"), ("diary",)),
}
# Observed source paths, matched in full at the existing fixed column positions.
# Keep raw labels in the payload and the six other metrics' existing rules unchanged.
SOURCE_HEADER_PATHS = {
    "access": {("総数",)},
    "pc": {("パソコン",), ("アクセス状況", "パソコン")},
    "sp": {("スマホ",), ("アクセス状況", "スマホ")},
}
PII_PATTERN = re.compile(r"(?:\b\d{2,4}-\d{2,4}-\d{3,4}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.I)
PII_KEYS = {"customer", "customer_name", "phone", "telephone", "email", "mail", "address", "reservation_id", "rsv_id"}


class CollectorError(RuntimeError):
    def __init__(self, code: str, safe_diagnostics: dict[str, Any] | None = None):
        super().__init__(code)
        self.safe_diagnostics = safe_diagnostics or {}


def _reader_diagnostics_safe(stderr: str, returncode: int) -> dict[str, Any]:
    # Never retain arbitrary stderr, HTML, URLs, authentication or personal values.
    stages = {"READER_MAIN", "BROWSER_EVALUATE", "GIRL_LIST_HTTP",
              "GIRL_DETAIL_HTTP", "GIRL_LIST_STRUCTURE", "GIRL_DETAIL_STRUCTURE",
              "GIRL_LIST_CANDIDATES", "GIRL_DETAIL_TABLE", "GIRL_DETAIL_DAY_ROW",
              "PAYLOAD_ENVELOPE", "COMPLETE"}
    classes = {"HTTP_NOT_OK", "FETCH_EXCEPTION", "LOGIN_REDIRECT_OR_FORM",
               "MANAGER_STRUCTURE_MISSING", "NO_CANDIDATES", "METRIC_TABLE_MISSING",
               "TARGET_DAY_ROW_MISSING", "INVALID_READER_RESULT", "NONE",
               "BROWSER_EXCEPTION_UNCLASSIFIED", "AUTH_REQUIRED", "BROWSER_UNAVAILABLE",
               "SCHEMA_DRIFT", "SOURCE_MISSING"}
    result: dict[str, Any] = {"reader_exit_code": int(returncode)}
    for line in stderr.splitlines():
        if not line.startswith("CITYHEAVEN_DAILY_DIAGNOSTIC:"):
            continue
        try:
            value = json.loads(line.split(":", 1)[1])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("stage"), str) and value["stage"] in stages:
            result["stage"] = value["stage"]
        if isinstance(value.get("error_class"), str) and value["error_class"] in classes:
            result["error_class"] = value["error_class"]
        status = value.get("http_status")
        if type(status) is int and 100 <= status <= 599:
            result["http_status"] = status
        last = value.get("last_stage")
        if isinstance(last, str) and re.fullmatch(r"WEBSOCKET_(CONNECT|OPEN)|GIRL_LIST_PARSED|DETAIL_[0-9]{1,3}_(START|COMPLETE)", last):
            result["last_stage"] = last
    return result



class ReceiptSaveError(CollectorError):
    pass


def _safe_save_failure(error: Exception, receipt: dict[str, Any]) -> dict[str, Any]:
    # Only fixed stage/class/code and the already filtered reader diagnostic.
    codes = {"SOURCE_MISSING", "AUTH_REQUIRED", "BROWSER_UNAVAILABLE", "BROWSER_CDP_UNAVAILABLE",
             "SCHEMA_DRIFT", "SOURCE_ARCHIVE_IO_FAILED", "SOURCE_ARCHIVE_CONFLICT",
             "UNSAFE_ARCHIVE_PATH", "RUNTIME_ROOT_REQUIRED", "AIRTABLE_WRITE_FAILED"}
    stages = {"SOURCE_SELECTION", "FETCH", "SOURCE_PERSIST", "SOURCE_PARSE", "COMPLETE",
              "READER_SUBPROCESS", "READER_STDOUT", "READER_MAIN", "BROWSER_EVALUATE",
              "GIRL_LIST_HTTP", "GIRL_DETAIL_HTTP", "GIRL_LIST_STRUCTURE", "GIRL_DETAIL_STRUCTURE",
              "GIRL_LIST_CANDIDATES", "GIRL_DETAIL_TABLE", "GIRL_DETAIL_DAY_ROW", "PAYLOAD_ENVELOPE"}
    classes = {"CollectorError", "PermissionError", "FileNotFoundError", "OSError",
               "ValueError", "TypeError", "RuntimeError"}
    cause = error.__cause__
    result: dict[str, Any] = {
        "status": "FAILED", "failure_code": "DIAGNOSTIC_RECEIPT_SAVE_FAILED",
        "diagnostic_stage": "RECEIPT_SAVE",
        "exception_class": type(error).__name__ if type(error).__name__ in classes else "OTHER_EXCEPTION",
        "cause_class": type(cause).__name__ if cause is not None and type(cause).__name__ in classes else None,
        "safe_error_code": str(error) if isinstance(error, CollectorError) and str(error) in codes else "SAVE_EXCEPTION",
        "collection_status": receipt.get("status") if isinstance(receipt.get("status"), str) and receipt["status"] in {"PASS", "FAILED"} else "UNKNOWN",
        "collection_failure_code": receipt.get("failure_code") if isinstance(receipt.get("failure_code"), str) and receipt["failure_code"] in codes else None,
        "collection_diagnostic_stage": receipt.get("diagnostic_stage") if isinstance(receipt.get("diagnostic_stage"), str) and receipt["diagnostic_stage"] in stages else "UNKNOWN",
    }
    date = receipt.get("target_date")
    if isinstance(date, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        result["target_date"] = date
    diagnostic = receipt.get("reader_diagnostics_safe")
    if isinstance(diagnostic, dict):
        code = diagnostic.get("reader_exit_code")
        code = code if type(code) is int and -255 <= code <= 255 else -1
        result["reader_diagnostics_safe"] = _reader_diagnostics_safe(
            "CITYHEAVEN_DAILY_DIAGNOSTIC:" + json.dumps(diagnostic), code)
    return result


def _persist_terminal_receipt(path: Path, receipt: dict[str, Any], preserve: bool) -> None:
    try:
        if preserve:
            data = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _publish_bytes(path.parent / "history" / path.stem / (hashlib.sha256(data).hexdigest()[:16] + ".json"), data)
        else:
            _save_result(path, receipt)
    except Exception as error:
        raise ReceiptSaveError("DIAGNOSTIC_RECEIPT_SAVE_FAILED", _safe_save_failure(error, receipt)) from None


class FetchedPayload(dict[str, Any]):
    def __init__(self, value: dict[str, Any], runtime_details: dict[str, Any]):
        super().__init__(value)
        self.runtime_details = runtime_details


def previous_jst_day(now: datetime | None = None) -> str:
    current = (now or datetime.now(JST)).astimezone(JST)
    return (current.date() - timedelta(days=1)).isoformat()


def _normalized(value: Any) -> str:
    return re.sub(r"[\s　>／/_-]+", "", str(value or "")).casefold()


def schema_mapping(headers: list[str]) -> dict[str, int]:
    if len(headers) < 13:
        raise CollectorError("SCHEMA_DRIFT")
    mapping: dict[str, int] = {}
    normalized = [_normalized(item) for item in headers]
    for metric, evidence_index in INDEX_EVIDENCE.items():
        if evidence_index >= len(normalized):
            raise CollectorError("SCHEMA_DRIFT")
        header = normalized[evidence_index]
        source_path = tuple(_normalized(part) for part in str(headers[evidence_index]).split(">"))
        if (source_path not in SOURCE_HEADER_PATHS.get(metric, set())
                and not any(all(_normalized(term) in header for term in terms) for terms in ALIASES[metric])):
            raise CollectorError("SCHEMA_DRIFT")
        if metric in mapping or evidence_index in mapping.values():
            raise CollectorError("SCHEMA_DRIFT")
        mapping[metric] = evidence_index
    return mapping


def parse_source(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    casts = payload.get("casts")
    if not isinstance(casts, list) or not casts:
        raise CollectorError("SOURCE_MISSING")
    results, mappings = [], []
    seen: set[str] = set()
    for cast in casts:
        girl_id = str(cast.get("girl_id") or "")
        display_name = str(cast.get("display_name") or "").strip()
        headers, values = cast.get("header_paths"), cast.get("values")
        if not girl_id.isdigit() or not display_name or girl_id in seen:
            raise CollectorError("SOURCE_MISSING")
        if not isinstance(headers, list) or not isinstance(values, list):
            raise CollectorError("SCHEMA_DRIFT")
        mapping = schema_mapping([str(item) for item in headers])
        row: dict[str, Any] = {"girl_id": girl_id, "display_name": display_name}
        for metric, index in mapping.items():
            if index >= len(values):
                raise CollectorError("SCHEMA_DRIFT")
            raw = str(values[index]).strip().replace(",", "")
            if raw in {"", "---", "-", "UNKNOWN", "不明"}:
                raise CollectorError("SOURCE_MISSING")
            if not re.fullmatch(r"\d+", raw):
                raise CollectorError("SCHEMA_DRIFT")
            row[metric] = int(raw)
        seen.add(girl_id)
        results.append(row)
        mappings.append({"girl_id": girl_id, "header_sha256": hashlib.sha256("|".join(headers).encode("utf-8")).hexdigest(), "mapping": mapping})
    return results, mappings


def metric_plan(casts: list[dict[str, Any]], target_date: str, receipt_ref: str) -> list[dict[str, Any]]:
    rows = []
    for cast in casts:
        unit = f"cityheaven_girl:{cast['girl_id']}"
        for metric in METRICS:
            value = cast.get(metric)
            if isinstance(value, bool) or not isinstance(value, int):
                raise CollectorError("SOURCE_MISSING")
            metric_key = f"cityheaven_unit_{metric}_daily_v1"
            identity = f"{BUSINESS_ID}|{unit}|{target_date}|{metric_key}|{VERSION}"
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            evidence = json.dumps({"source_page": "C2TokeiGirlDetail", "source_date": target_date, "girl_id": cast["girl_id"], "unit": unit, "read_only_receipt_ref": receipt_ref}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            rows.append({
                "Metric Record ID": f"bm_cityheaven_daily_{digest}", "Business ID": BUSINESS_ID,
                "Metric Key": metric_key, "Metric Label": f"CityHeaven {metric} {target_date}",
                "Value": value, "Unit": "count", "Window Start": target_date, "Window End": target_date,
                "Data Status": "CONFIRMED", "Confidence": "HIGH", "Source": "CityHeaven C2TokeiGirlDetail daily READ ONLY",
                "Evidence": evidence, "Dedup Key": identity, "Notes": f"Unit={unit}; {VERSION}; unknown values are never zero-imputed.",
                "Metric Class": "PRODUCTIVITY", "Metric Layer": "UNIT", "Source Project ID": "business_state_baseline",
            })
    return sorted(rows, key=lambda row: row["Metric Record ID"])


def pii_guard(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalized(key) in PII_KEYS or not pii_guard(item):
                return False
        return True
    if isinstance(value, list):
        return all(pii_guard(item) for item in value)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return PII_PATTERN.search(serialized) is None


_CTI_WRITER: Any | None = None


def _load_cti_writer() -> Any:
    """Load the existing production writer without reading or exposing its secret here."""
    global _CTI_WRITER
    if _CTI_WRITER is not None:
        return _CTI_WRITER
    writer_path = BRIDGE / "bridge_watcher.py"
    spec = importlib.util.spec_from_file_location("cityheaven_cti_bridge_writer", writer_path)
    if spec is None or spec.loader is None:
        raise CollectorError("AIRTABLE_WRITE_FAILED")
    bridge_string = str(BRIDGE)
    if bridge_string not in sys.path:
        sys.path.insert(0, bridge_string)
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CollectorError("AIRTABLE_WRITE_FAILED") from exc
    if (getattr(module, "BASE_ID", None) != BASE_ID
            or not callable(getattr(module, "request", None))
            or not getattr(module, "BUSINESS_METRICS", None)):
        raise CollectorError("AIRTABLE_WRITE_FAILED")
    _CTI_WRITER = module
    return module


def airtable_request(method: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        writer = _load_cti_writer()
        result = writer.request(method, writer.BUSINESS_METRICS, params=params, body=body)
        if not isinstance(result, dict):
            raise RuntimeError("AIRTABLE_RESPONSE_INVALID")
        return result
    except Exception as exc:
        raise CollectorError("AIRTABLE_WRITE_FAILED") from exc


def _unknown_count(payload: dict[str, Any]) -> int:
    count = 0
    casts = payload.get("casts")
    if not isinstance(casts, list):
        return 0
    for cast in casts:
        values = cast.get("values") if isinstance(cast, dict) else None
        if not isinstance(values, list):
            continue
        for index in INDEX_EVIDENCE.values():
            if index >= len(values) or str(values[index] if values[index] is not None else "").strip() in {"", "---", "-", "UNKNOWN", "荳肴・"}:
                count += 1
    return count


def _read_existing(ids: list[str], request: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for start in range(0, len(ids), 20):
        clauses = ["{Metric Record ID}='" + item.replace("'", "\\'") + "'" for item in ids[start:start + 20]]
        formula = clauses[0] if len(clauses) == 1 else "OR(" + ",".join(clauses) + ")"
        response = request("GET", params={"filterByFormula": formula, "pageSize": 100, "returnFieldsByFieldId": "false"})
        rows = response.get("records")
        if not isinstance(rows, list) or response.get("offset"):
            raise CollectorError("AIRTABLE_WRITE_FAILED")
        found.extend(rows)
    return found


def write_metrics(rows: list[dict[str, Any]], request: Callable[..., dict[str, Any]] = airtable_request) -> dict[str, int]:
    wanted = {row["Metric Record ID"]: row for row in rows}
    existing = _read_existing(list(wanted), request)
    existing_by_id: dict[str, dict[str, Any]] = {}
    for record in existing:
        fields = record.get("fields") or {}; metric_id = fields.get("Metric Record ID")
        if metric_id not in wanted or metric_id in existing_by_id:
            raise CollectorError("AIRTABLE_WRITE_FAILED")
        for key, value in wanted[metric_id].items():
            if fields.get(key) != value:
                raise CollectorError("AIRTABLE_WRITE_FAILED")
        existing_by_id[metric_id] = record
    missing = [row for ident, row in wanted.items() if ident not in existing_by_id]
    created = 0
    for start in range(0, len(missing), 10):
        batch = missing[start:start + 10]
        response = request("POST", body={"records": [{"fields": {**row, "Observed At": datetime.now(JST).isoformat(timespec="seconds")}} for row in batch], "typecast": False})
        if len(response.get("records") or []) != len(batch):
            raise CollectorError("AIRTABLE_WRITE_FAILED")
        created += len(batch)
    final = _read_existing(list(wanted), request)
    final_ids = [str((record.get("fields") or {}).get("Metric Record ID") or "") for record in final]
    if len(final_ids) != len(set(final_ids)) or set(final_ids) != set(wanted):
        raise CollectorError("AIRTABLE_WRITE_FAILED")
    return {"write_count": created, "readback_count": len(final_ids), "duplicate_count": 0, "replay_count": len(existing_by_id)}


def _load_configuration(config: Path) -> dict[str, Any]:
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if not isinstance(value, dict):
        raise CollectorError("BROWSER_CDP_UNAVAILABLE")
    return value


def ensure_daily_browser(node: str = "node", config: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [node, str(OPERA_RUNTIME), "--config", str(config)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=OPERA_BOOTSTRAP_TIMEOUT_SECONDS, check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if completed.returncode != 0:
        code = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "BROWSER_CDP_UNAVAILABLE"
        allowed = {
            "BROWSER_CDP_UNAVAILABLE", "BROWSER_LAUNCH_FAILED", "CDP_PORT_RUNTIME_MISMATCH",
            "OPERA_RUNTIME_CONFIG_INVALID", "OPERA_RUNTIME_UNAVAILABLE", "OPERA_START_LOCKED",
        }
        raise CollectorError(code if code in allowed else "BROWSER_CDP_UNAVAILABLE")
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if not isinstance(value, dict) or value.get("cdp_reachable") is not True:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE")
    return value


def _reservation_runtime_config(configuration: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        city = configuration["cityheaven_reservation"]
        state_dir = Path(configuration["state_dir"])
        node = str(Path(city.get("node_executable") or "node.exe"))
        runtime = {
            "browser": "CHROME",
            "chrome_executable": str(Path(city["chrome_executable"])),
            "profile_path": str(Path(city["dedicated_profile_path"])),
            "cdp_port": int(city["cdp_port"]),
            "base_reader_path": str(Path(city["reader_path"])),
            "startup_lock_path": str(state_dir / "runtime-locks" / "cityheaven-reservation-chrome.lock"),
            "start_url": "https://newmanager.cityheaven.net/",
            "restore_session": True,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    return node, runtime


def ensure_reservation_browser(configuration: dict[str, Any]) -> tuple[dict[str, Any], str]:
    node, runtime = _reservation_runtime_config(configuration)
    adapter = (
        "const fs=require('node:fs');const r=require(process.argv[1]);"
        "const c=JSON.parse(fs.readFileSync(0,'utf8'));"
        "r.dailyReadiness(c).then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(e=>{process.stderr.write(String(e.runtimeCode||'BROWSER_CDP_UNAVAILABLE')+'\\n');process.exit(20)})"
    )
    try:
        completed = subprocess.run(
            [node, "-e", adapter, str(RESERVATION_RUNTIME)],
            input=json.dumps(runtime, ensure_ascii=False, separators=(",", ":")),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=RESERVATION_BOOTSTRAP_TIMEOUT_SECONDS, check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if completed.returncode != 0:
        code = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "BROWSER_CDP_UNAVAILABLE"
        allowed = {
            "BROWSER_CDP_UNAVAILABLE", "BROWSER_LAUNCH_FAILED", "CDP_PORT_RUNTIME_MISMATCH",
            "CHROME_RUNTIME_CONFIG_INVALID", "CHROME_RUNTIME_UNAVAILABLE", "CHROME_START_ALREADY_ATTEMPTED",
            "CHROME_START_IN_PROGRESS", "CHROME_START_LOCK_FAILED", "CDP_DISCOVERY_FAILED", "STOPPED",
        }
        raise CollectorError(code if code in allowed else "BROWSER_CDP_UNAVAILABLE")
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE") from exc
    if not isinstance(value, dict) or value.get("runtime_identity") != "MATCH" or value.get("profile_identity") != "MATCH":
        raise CollectorError("CDP_PORT_RUNTIME_MISMATCH")
    if value.get("cdp_reachable") is not True:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE")
    return value, node


def recover_saved_login(configuration: dict[str, Any]) -> dict[str, Any]:
    node, runtime = _reservation_runtime_config(configuration)
    adapter = (
        "const fs=require('node:fs');const r=require(process.argv[1]);"
        "r.dailySavedLogin(JSON.parse(fs.readFileSync(0,'utf8')))"
        ".then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(()=>process.exit(20))"
    )
    try:
        completed = subprocess.run([node, "-e", adapter, str(RESERVATION_RUNTIME)],
            input=json.dumps(runtime), capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False, creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        value = json.loads(completed.stdout)
        if completed.returncode or not isinstance(value, dict): raise ValueError()
        return {key: value[key] for key in ("status", "logout_notice_ack_count", "submit_count", "password_typed", "owned_target_closed") if key in value}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"status": "OWNER_AUTH_REQUIRED", "submit_count": 1, "submission_outcome": "UNKNOWN"}


def run_reader_with_auth_recovery(reader_command: list[str], configuration: dict[str, Any], route: str) -> tuple[Any, dict[str, Any]]:
    recovery: dict[str, Any] = {}
    for attempt in range(2):
        completed = subprocess.run(reader_command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=READER_SUBPROCESS_TIMEOUT_SECONDS, check=False, creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        lines = completed.stderr.strip().splitlines()
        code = lines[-1] if lines else ""
        if completed.returncode and code == "AUTH_REQUIRED" and attempt == 0 and route == RESERVATION_CHROME_REUSE_ROUTE:
            recovery = recover_saved_login(configuration)
            if recovery.get("status") == "AUTHENTICATED" and recovery.get("owned_target_closed") is True:
                continue
        return completed, recovery
    raise CollectorError("AUTH_REQUIRED")


def fetch(target_date: str, node: str = "node", config: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    configuration = _load_configuration(config)
    daily = configuration.get("cityheaven_daily")
    if not isinstance(daily, dict):
        raise CollectorError("BROWSER_CDP_UNAVAILABLE")
    route = str(daily.get("browser") or OPERA_ROUTE)
    if route == OPERA_ROUTE:
        readiness = ensure_daily_browser(node=node, config=config)
        reader_command = [node, str(READER), target_date, "--config", str(config)]
        runtime_details = {
            "route": OPERA_ROUTE, "runtime_identity_result": "MATCH",
            "profile_identity_result": "MATCH", "cdp_port": int(daily.get("cdp_port") or 0),
            "additional_browser_launch_count": int(readiness.get("existing_profile_reused") is False),
            "browser_restart_count": 0,
        }
    elif route == RESERVATION_CHROME_REUSE_ROUTE:
        readiness, reservation_node = ensure_reservation_browser(configuration)
        port = int(readiness["cdp_port"])
        reader_command = [reservation_node, str(READER), target_date, "--cdp-port", str(port)]
        runtime_details = {
            "route": RESERVATION_CHROME_REUSE_ROUTE,
            "runtime_identity_result": readiness["runtime_identity"],
            "profile_identity_result": readiness["profile_identity"], "cdp_port": port,
            "additional_browser_launch_count": int(readiness.get("additional_browser_launch_count") or 0),
            "browser_restart_count": int(readiness.get("browser_restart_count") or 0),
        }
    else:
        raise CollectorError("BROWSER_CDP_UNAVAILABLE")
    try:
        completed, recovery = run_reader_with_auth_recovery(reader_command, configuration, route)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise CollectorError("BROWSER_UNAVAILABLE", {
            "stage": "READER_SUBPROCESS",
            "error_class": "TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "START_FAILED",
            "route": route,
        }) from exc
    if completed.returncode != 0:
        code = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "BROWSER_UNAVAILABLE"
        diagnostic = _reader_diagnostics_safe(completed.stderr, completed.returncode)
        diagnostic["route"] = route
        if recovery: diagnostic["saved_login_recovery"] = recovery
        raise CollectorError(code if code in {"AUTH_REQUIRED", "BROWSER_UNAVAILABLE", "SCHEMA_DRIFT", "SOURCE_MISSING"} else "BROWSER_UNAVAILABLE", diagnostic)
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise CollectorError("SOURCE_MISSING", {"stage": "READER_STDOUT", "error_class": "INVALID_JSON", "route": route}) from exc
    if not isinstance(value, dict):
        raise CollectorError("SOURCE_MISSING", {"stage": "READER_STDOUT", "error_class": "NON_OBJECT", "route": route})
    runtime_details["reader_diagnostics_safe"] = _reader_diagnostics_safe(completed.stderr, completed.returncode)
    if recovery: runtime_details["saved_login_recovery"] = recovery
    return FetchedPayload(value, runtime_details)


def run(target_date: str, *, write_airtable: bool, runtime: Path = DEFAULT_RUNTIME, fetcher: Callable[[str], dict[str, Any]] = fetch, request: Callable[..., dict[str, Any]] = airtable_request, offline_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _check_date(target_date)
    runtime = _checked_runtime(runtime)
    started = datetime.now(JST).isoformat(timespec="seconds")
    receipt_path = runtime / "receipts" / (target_date.replace("-", "") + ".json")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA, "started_at": started, "target_date": target_date,
        "read_only_source": True, "cityheaven_fetch_count": 0, "cityheaven_write_count": 0,
        "external_change_count": 0, "airtable_write_run_count": 0, "airtable_write_count": 0,
        "business_write_count": 0, "business_readback_count": 0, "duplicate_count": 0,
        "replay_count": 0, "unknown_count": 0, "cast_count": 0, "metric_count": 0,
        "schema_status": "NOT_CHECKED", "auth_status": "NOT_CHECKED", "pii_count": 0,
        "credential_route_class": WRITER_CREDENTIAL_ROUTE_CLASS,
        "credential_secret_output_count": 0, "source_payload_persisted": False,
        "route": None, "runtime_identity_result": None, "profile_identity_result": None,
        "cdp_port": None, "additional_browser_launch_count": 0, "browser_restart_count": 0,
        "reader_run_count": 0, "fetched_at": None, "processed_at": started,
        "source_fetched_at": None, "source_route": None,
        "source_runtime_identity_result": None, "source_profile_identity_result": None,
        "source_metadata_status": "UNKNOWN", "source_metadata_receipt_ref": None,
        "source_metadata_receipt_sha256": None,
        "diagnostic_version": "cityheaven_safe_stage/1.0",
        "diagnostic_stage": "SOURCE_SELECTION",
        "invocation_safe": {"entrypoint": "run-cityheaven-daily-metrics.py",
                            "target_date": target_date, "shopdir": "s_apronlady",
                            "write_airtable": bool(write_airtable), "runtime": str(runtime)},
        "reader_file": READER.name,
        "reader_sha256": hashlib.sha256(READER.read_bytes()).hexdigest(),
    }
    preserve_existing_receipt = receipt_path.exists() and offline_payload is None
    try:
        if write_airtable and request is airtable_request:
            raise CollectorError("AIRTABLE_WRITE_FAILED")
        payload = None if offline_payload is not None else _read_saved_source(runtime, target_date)
        if offline_payload is not None:
            payload = offline_payload
            receipt["source_reused"] = False
        elif payload is not None:
            preserve_existing_receipt = False
            receipt["source_reused"] = True
        elif receipt_path.exists():
            previous = _read_json(receipt_path)
            if previous.get("target_date") != target_date or previous.get("schema_version") != SCHEMA or previous.get("status") not in {"PASS", "FAILED"}:
                raise CollectorError("SOURCE_RECEIPT_MISMATCH")
            if previous.get("source_payload_persisted") is not False:
                raise CollectorError("SOURCE_ARCHIVE_MISSING")
            if previous["status"] == "FAILED" and previous.get("failure_code") == "AUTH_REQUIRED":
                # Re-login can recover this exact day; retain the failed evidence first.
                prior = receipt_path.read_bytes()
                _publish_bytes(receipt_path.parent / "history" / receipt_path.stem / (hashlib.sha256(prior).hexdigest()[:16] + ".json"), prior)
                preserve_existing_receipt = False
                receipt["cityheaven_fetch_count"] = 1
                receipt["diagnostic_stage"] = "FETCH"
                payload = fetcher(target_date)
                receipt["source_reused"] = False
            else:
                receipt.update({"status": previous["status"], "failure_code": previous.get("failure_code"),
                                "source_reused": False, "receipt_reused": True,
                                "result_scope": "EXISTING_UNSAVED_RECEIPT_ONLY",
                                "prior_receipt_ref": str(receipt_path),
                                "prior_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                                "completed_at": datetime.now(JST).isoformat(timespec="seconds")})
                receipt["processed_at"] = receipt["completed_at"]
                return receipt
        else:
            receipt["cityheaven_fetch_count"] = 1
            receipt["diagnostic_stage"] = "FETCH"
            payload = fetcher(target_date)
            receipt["source_reused"] = False
        runtime_details = getattr(payload, "runtime_details", {})
        if receipt["cityheaven_fetch_count"] and isinstance(runtime_details, dict):
            receipt.update({key: runtime_details[key] for key in (
                "route", "runtime_identity_result", "profile_identity_result", "cdp_port",
                "additional_browser_launch_count", "browser_restart_count", "saved_login_recovery",
            ) if key in runtime_details})
        if isinstance(runtime_details.get("reader_diagnostics_safe"), dict):
            receipt["reader_diagnostics_safe"] = runtime_details["reader_diagnostics_safe"]
        receipt["reader_run_count"] = receipt["cityheaven_fetch_count"]
        if receipt["cityheaven_fetch_count"]:
            receipt["auth_status"] = "PASS"
            receipt["fetched_at"] = datetime.now(JST).isoformat(timespec="seconds")
        receipt["diagnostic_stage"] = "SOURCE_PERSIST"
        offline = persist_offline_source(payload, target_date, runtime=runtime)
        receipt.update({key: offline[key] for key in ("source_payload_persisted", "source_path", "source_sha256", "source_sha256_scope", "analysis_path") if key in offline})
        if not receipt["cityheaven_fetch_count"]:
            receipt.update(_prior_source_metadata(runtime, target_date, offline["source_sha256"]))
        receipt["partial_analysis_status"] = offline["status"]
        if offline["status"] == "FAILED":
            raise CollectorError(offline["failure_code"])
        receipt["unknown_count"] = _unknown_count(payload)
        receipt["diagnostic_stage"] = "SOURCE_PARSE"
        casts, mappings = parse_source(payload)
        receipt["schema_status"] = "PASS"
        plan = metric_plan(casts, target_date, str(receipt_path))
        if not pii_guard({"raw_source_payload": payload, "mappings": mappings, "plan": plan}):
            raise CollectorError("SOURCE_MISSING")
        receipt.update({"cast_count": len(casts), "metric_source_count": len(plan), "metric_count": len(plan), "schema_mapping_evidence": mappings, "pii_guard": "PASS"})
        receipt["airtable_write_run_count"] = int(write_airtable)
        result = write_metrics(plan, request) if write_airtable else {"write_count": 0, "readback_count": 0, "duplicate_count": 0, "replay_count": 0}
        raw_serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        receipt.update({
            "status": "PASS", "failure_code": None, "diagnostic_stage": "COMPLETE",
            "processed_at": datetime.now(JST).isoformat(timespec="seconds"),
            "completed_at": datetime.now(JST).isoformat(timespec="seconds"),
            "raw_source_sha256": hashlib.sha256(raw_serialized.encode("utf-8")).hexdigest(),
            "airtable_write_count": result["write_count"], "business_write_count": result["write_count"],
            "business_readback_count": result["readback_count"], "duplicate_count": result["duplicate_count"],
            "replay_count": result["replay_count"],
        })
    except CollectorError as exc:
        if exc.safe_diagnostics:
            receipt["reader_diagnostics_safe"] = exc.safe_diagnostics
            receipt["diagnostic_stage"] = exc.safe_diagnostics.get("stage", receipt["diagnostic_stage"])
        code = "SOURCE_MISSING" if str(exc) == "UNSAFE_SOURCE_PAYLOAD" else str(exc)
        if code == "AUTH_REQUIRED" and receipt["cityheaven_fetch_count"]:
            receipt["auth_status"] = "FAILED"
        elif code in {"SCHEMA_DRIFT", "SOURCE_MISSING"}:
            receipt["schema_status"] = "FAILED_CLOSED"
        receipt.update({"status": "FAILED", "failure_code": code, "completed_at": datetime.now(JST).isoformat(timespec="seconds")})
        receipt.setdefault("metric_source_count", receipt["metric_count"])
        receipt.setdefault("schema_mapping_evidence", [])
    receipt["processed_at"] = receipt["completed_at"]
    _persist_terminal_receipt(receipt_path, receipt, preserve_existing_receipt)
    return receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(); value.add_argument("--date", default=previous_jst_day()); value.add_argument("--write-airtable", action="store_true"); value.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME); return value


def main() -> int:
    args = parser().parse_args()
    try: datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError: print(json.dumps({"status": "FAILED", "failure_code": "SOURCE_MISSING"})); return 2
    try:
        receipt = run(args.date, write_airtable=args.write_airtable, runtime=args.runtime)
    except ReceiptSaveError as error:
        print(json.dumps(error.safe_diagnostics, ensure_ascii=False, sort_keys=True))
        return 20
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 20


def _check_date(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise CollectorError("SOURCE_DATE_INVALID")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise CollectorError("SOURCE_DATE_INVALID") from exc


def _checked_runtime(runtime: Path) -> Path:
    root = DEFAULT_RUNTIME.absolute()
    path = Path(runtime).absolute()
    try:
        path.relative_to(root)
    except ValueError:
        raise CollectorError("RUNTIME_ROOT_REQUIRED")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise CollectorError("UNSAFE_ARCHIVE_PATH")
    if not path.resolve().is_relative_to(root.resolve()):
        raise CollectorError("RUNTIME_ROOT_REQUIRED")
    return path


def _read_json(path: Path, *, allow_owned_links: bool = True) -> dict[str, Any]:
    _checked_runtime(path)
    try:
        if not path.is_file():
            raise CollectorError("UNSAFE_ARCHIVE_PATH")
        data = path.read_bytes()
        if allow_owned_links:
            _owned_pending_links(path, data)
        elif path.stat().st_nlink != 1:
            raise CollectorError("UNSAFE_ARCHIVE_PATH")
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError) as exc:
        raise CollectorError("SOURCE_ARCHIVE_READ_FAILED") from exc


def _owned_pending_links(path: Path, data: bytes) -> None:
    """Accept only extra links with our durable pre-publication ownership evidence."""
    links = path.stat().st_nlink
    if links == 1:
        return
    owned = 0
    digest = hashlib.sha256(data).hexdigest()
    for pending in path.parent.glob(".p-*"):
        if pending.name.endswith(".owner.json"):
            continue
        proof_path = pending.with_name(pending.name + ".owner.json")
        if not proof_path.exists():
            continue
        _checked_runtime(pending)
        proof = _read_json(proof_path, allow_owned_links=False)
        if (pending.is_file() and os.path.samefile(pending, path)
                and proof == {"target": path.name, "pending": pending.name, "sha256": digest}
                and pending.read_bytes() == data):
            owned += 1
    if links != 1 + owned:
        raise CollectorError("SOURCE_ARCHIVE_CONFLICT")
    # Keep interrupted evidence; do not delete another execution's pending files.


def _publish_bytes(path: Path, data: bytes) -> None:
    _checked_runtime(path)
    temporary = None
    proof_path = None
    proof_created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise CollectorError("SOURCE_ARCHIVE_CONFLICT")
            _owned_pending_links(path, data)
            return
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".p-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        proof_path = temporary.with_name(temporary.name + ".owner.json")
        proof = {"target": path.name, "pending": temporary.name, "sha256": hashlib.sha256(data).hexdigest()}
        _checked_runtime(proof_path)
        with proof_path.open("xb") as owner:
            proof_created = True
            owner.write(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            owner.flush()
            os.fsync(owner.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise CollectorError("SOURCE_ARCHIVE_CONFLICT")
            _owned_pending_links(path, data)
    except OSError as exc:
        raise CollectorError("SOURCE_ARCHIVE_IO_FAILED") from exc
    finally:
        if temporary is not None:
            # A hard interruption here may leave two links; the proof makes replay possible.
            try:
                temporary.unlink(missing_ok=True)
                if proof_created:
                    proof_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CollectorError("SOURCE_ARCHIVE_IO_FAILED") from exc


def _publish_json(path: Path, value: dict[str, Any]) -> None:
    _publish_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _save_result(path: Path, value: dict[str, Any]) -> None:
    """Immutable result versions first, then atomically update the derived current view."""
    _checked_runtime(path)
    history = path.parent / "history" / path.stem
    try:
        if path.exists():
            previous = _read_json(path)
            old_bytes = json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _publish_bytes(history / (hashlib.sha256(old_bytes).hexdigest()[:16] + ".json"), old_bytes)
            if previous == value:
                return
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        _publish_bytes(history / (hashlib.sha256(data).hexdigest()[:16] + ".json"), data)
        atomic_write_json(path, value)
    except OSError as exc:
        raise CollectorError("SOURCE_ARCHIVE_IO_FAILED") from exc


def _prior_source_metadata(runtime: Path, target_date: str, digest: str) -> dict[str, Any]:
    """Reference prior receipt evidence; never check current browser/auth to fill gaps."""
    path = runtime / "receipts" / (target_date.replace("-", "") + ".json")
    if not path.exists():
        return {}
    try:
        previous = _read_json(path)
        if previous.get("source_metadata_receipt_ref"):
            evidence_path = Path(previous["source_metadata_receipt_ref"])
            evidence = _read_json(evidence_path)
            evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            if evidence_sha != previous.get("source_metadata_receipt_sha256"):
                return {}
        else:
            evidence = previous
            data = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            evidence_sha = hashlib.sha256(data).hexdigest()
            evidence_path = path.parent / "history" / path.stem / (evidence_sha[:16] + ".json")
            if _read_json(evidence_path) != evidence or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence_sha:
                return {}
        if (evidence.get("schema_version") != SCHEMA or evidence.get("target_date") != target_date
                or evidence.get("source_sha256") != digest or evidence.get("cityheaven_fetch_count") != 1
                or evidence.get("reader_run_count") != 1 or evidence.get("auth_status") != "PASS"
                or not isinstance(evidence.get("fetched_at"), str)):
            return {}
        datetime.fromisoformat(evidence["fetched_at"])
        return {"source_fetched_at": evidence["fetched_at"], "source_route": evidence.get("route"),
                "source_runtime_identity_result": evidence.get("runtime_identity_result"),
                "source_profile_identity_result": evidence.get("profile_identity_result"),
                "source_metadata_status": "EVIDENCED", "source_metadata_receipt_ref": str(evidence_path),
                "source_metadata_receipt_sha256": evidence_sha}
    except (CollectorError, OSError, ValueError, TypeError):
        return {}


def _read_saved_source(runtime: Path, target_date: str) -> dict[str, Any] | None:
    archive = runtime / "offline-source" / "sources"
    stem = target_date.replace("-", "")
    path = archive / (stem + ".json")
    proof_path = archive / (stem + ".sha256.json")
    _checked_runtime(path)
    if not path.exists():
        if proof_path.exists():
            raise CollectorError("SOURCE_ARCHIVE_INCOMPLETE")
        return None
    proof = _read_json(proof_path)
    try:
        data = path.read_bytes()
        _owned_pending_links(path, data)
        if proof != {"target_date": target_date, "sha256": hashlib.sha256(data).hexdigest()}:
            raise CollectorError("SOURCE_ARCHIVE_READBACK_MISMATCH")
        payload = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectorError("SOURCE_ARCHIVE_READ_FAILED") from exc
    _safe_offline_payload(payload, target_date)
    return payload


def _safe_offline_payload(payload: dict[str, Any], target_date: str) -> None:
    """Reject unrecognized fields, secrets and customer data before persistence."""
    _check_date(target_date)
    allowed = {"target_date", "casts", "source_pages", "read_only", "cityheaven_write_count"}
    if not isinstance(payload, dict) or set(payload) - allowed or not pii_guard(payload):
        raise CollectorError("UNSAFE_SOURCE_PAYLOAD")
    if payload.get("target_date") != target_date or payload.get("read_only") is not True or payload.get("cityheaven_write_count") != 0:
        raise CollectorError("SOURCE_IDENTITY_MISMATCH")
    if payload.get("source_pages") != ["C2GirlList", "C2TokeiGirlDetail"]:
        raise CollectorError("SOURCE_IDENTITY_MISMATCH")
    secret_pattern = re.compile(r"(?i)bearer\s+|-----BEGIN .*PRIVATE KEY|eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+|(?:password|passwd|cookie|token|secret|authorization)\s*[:=]")
    def scan(value):
        if isinstance(value, str) and secret_pattern.search(value):
            raise CollectorError("UNSAFE_SOURCE_PAYLOAD")
        if isinstance(value, dict):
            for item in value.values(): scan(item)
        elif isinstance(value, list):
            for item in value: scan(item)
    scan(payload)
    casts = payload.get("casts")
    if not isinstance(casts, list) or not casts:
        raise CollectorError("SOURCE_MISSING")
    for cast in casts:
        if not isinstance(cast, dict) or set(cast) != {"girl_id", "display_name", "header_paths", "values"}:
            raise CollectorError("UNSAFE_SOURCE_PAYLOAD")
        if type(cast["girl_id"]) not in (str, int) or not re.fullmatch(r"[0-9]{1,32}", str(cast["girl_id"])):
            raise CollectorError("SOURCE_IDENTITY_MISMATCH")
        if not isinstance(cast["display_name"], str) or not cast["display_name"].strip():
            raise CollectorError("SOURCE_MISSING")
        if not isinstance(cast["header_paths"], list) or not all(isinstance(x, str) for x in cast["header_paths"]):
            raise CollectorError("SCHEMA_DRIFT")
        if not isinstance(cast["values"], list):
            raise CollectorError("SCHEMA_DRIFT")
        # Values are only count cells, never arbitrary uninspected free text.
        for cell in cast["values"]:
            if cell is not None and (isinstance(cell, bool) or not isinstance(cell, (str, int))):
                raise CollectorError("UNSAFE_SOURCE_PAYLOAD")
            if cell is not None and len(str(cell)) > 64:
                raise CollectorError("SOURCE_NUMERIC_LIMIT")
            if cell is not None and str(cell).strip() not in {"", "---", "-", "UNKNOWN", "不明"} and not re.fullmatch(r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)", str(cell).strip()):
                raise CollectorError("UNSAFE_SOURCE_PAYLOAD")


def analyze_partial_source(payload: dict[str, Any], target_date: str) -> dict[str, Any]:
    _safe_offline_payload(payload, target_date)
    seen = set()
    rows, missing = [], []
    for cast in payload["casts"]:
        identity = str(cast["girl_id"])
        if not identity.isdigit() or identity in seen:
            raise CollectorError("SOURCE_MISSING")
        seen.add(identity)
        mapping = schema_mapping(cast["header_paths"])
        for metric, index in mapping.items():
            if index >= len(cast["values"]):
                raise CollectorError("SCHEMA_DRIFT")
            raw = cast["values"][index]
            text = "" if raw is None else str(raw).strip().replace(",", "")
            if text in {"", "---", "-", "UNKNOWN", "不明"}:
                missing.append({"girl_id": identity, "metric": metric, "column_index": index, "raw_value": raw, "reason": "SOURCE_VALUE_MISSING"})
            else:
                rows.append({"girl_id": identity, "metric": metric, "value": int(text)})
    expected = len(seen) * len(METRICS)
    metrics = {}
    for metric in METRICS:
        values = [row["value"] for row in rows if row["metric"] == metric]
        metrics[metric] = {"observed_sum": sum(values) if values else None, "numeric_cast_count": len(values), "input_cast_count": len(seen), "missing_cast_count": len(seen) - len(values)}
    return {"target_date": target_date, "status": "NO_USABLE_VALUES" if not rows else ("PARTIAL" if missing else "COMPLETE"), "cast_count": len(seen), "completeness_scope": "INPUT_CAST_CELLS_ONLY", "roster_coverage_status": "UNVERIFIED", "upstream_expected_cast_count": None, "expected_cell_count": expected, "numeric_cell_count": len(rows), "missing_cell_count": len(missing), "numeric_rows": rows, "missing_cells": missing, "metrics": metrics}


def persist_offline_source(payload: dict[str, Any], target_date: str, *, runtime: Path) -> dict[str, Any]:
    """Explicit offline-only entry: no fetch, writer, credentials or default runtime."""
    _check_date(target_date)
    runtime = _checked_runtime(runtime) / "offline-source"
    _safe_offline_payload(payload, target_date)
    raw_path = runtime / "sources" / (target_date.replace("-", "") + ".json")
    raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    _publish_json(raw_path.with_name(raw_path.stem + ".sha256.json"), {"target_date": target_date, "sha256": digest})
    _publish_bytes(raw_path, raw_bytes)
    try:
        committed_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CollectorError("SOURCE_ARCHIVE_IO_FAILED") from exc
    if committed_digest != digest:
        raise CollectorError("SOURCE_ARCHIVE_READBACK_MISMATCH")
    receipt = {"target_date": target_date, "source_payload_persisted": True, "source_path": str(raw_path), "source_sha256": digest, "source_sha256_scope": "SAVED_FETCHED_PAYLOAD_JSON_NOT_HTTP_BODY", "airtable_write_count": 0}
    try:
        analysis = analyze_partial_source(payload, target_date)
        analysis_path = runtime / "analysis" / (target_date.replace("-", "") + ".json")
        _save_result(analysis_path, analysis)
        receipt.update({"status": analysis["status"], "analysis_path": str(analysis_path), "expected_cell_count": analysis["expected_cell_count"], "numeric_cell_count": analysis["numeric_cell_count"], "missing_cell_count": analysis["missing_cell_count"]})
    except CollectorError as exc:
        receipt.update({"status": "FAILED", "failure_code": str(exc)})
    _save_result(runtime / "receipts" / (target_date.replace("-", "") + ".json"), receipt)
    return receipt


if __name__ == "__main__":
    raise SystemExit(main())
