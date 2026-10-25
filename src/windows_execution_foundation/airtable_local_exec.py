from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .core import ExecutionFoundation, read_json, stable_key
from .reservation_pause import record_paused
from .foundation_failure_slack import Notifier, attempt_once, send_foundation_failure_slack
from .result_safe_facts import sanitize_result_safe_facts
from .work_identity import MAILBOX_V3, result_metadata, validate_v3_fields

RESULT_KEYS = (
    "observer_exists",
    "running",
    "last_run_at",
    "last_result",
    "next_run_at",
    "latest_report_at",
)

PREIMPLEMENTATION_GATE_FIELDS = (
    "existing_success_route",
    "reuse_decision",
    "scope_mode",
    "deterministic_first",
    "known_failure_reuse",
    "new_system_reason",
    "generalization_reason",
)
PREIMPLEMENTATION_SSOT_FIELDS = (
    "推奨再利用Asset ID",
    "Chief of Staff Gate",
)

JOB_INTENT_FIELDS = ("goal", "task_class", "target", "success", "forbidden")
JOB_INTENT_LIMITS = {
    "goal": 1000,
    "task_class": 32,
    "target": 500,
    "success": 1000,
    "forbidden": 1000,
}
JOB_INTENT_CLASSES = frozenset({"READ_ONLY_CHECKPOINT", "CODE_REPAIR", "VERIFY_ONLY"})
JOB_INTENT_REQUIRED_CONTEXT = (
    "既にtantan-mf-a1c6659aabef611c上で実行中。端末解決しない。"
    "RDC/Remote MCP禁止。PC名をroutingに使わない。"
)
JOB_INTENT_PLACEHOLDER = {"version": 1, "criteria": "JOB_INTENT_V1", "required_files": []}
JOB_INTENT_CLASS_RULES = {
    "READ_ONLY_CHECKPOINT": (
        "REUSE",
        "Read only: do not modify files or runtime state and do not perform external writes or sends.",
    ),
    "CODE_REPAIR": (
        "EXTEND",
        "Limit changes to the scoped repair. Do not add infrastructure, routes, services, databases, "
        "tables, fields, queues, watchers, or external side effects.",
    ),
    "VERIFY_ONLY": (
        "REUSE",
        "Verification only: do not modify production files or runtime state and do not perform external writes or sends.",
    ),
}

AUTO_REPAIR_REASON_CODES = frozenset({
    "REQUIRED_OUTPUT_MISSING",
    "EVIDENCE_MISSING",
    "TEST_EVIDENCE_INVALID",
})
AUTO_REPAIR_HUMAN_MARKERS = (
    "AUTH", "LOGIN", "PERMISSION", "MFA", "CREDENTIAL", "LEGAL", "PII",
    "CUSTOMER_SEND", "RESERVATION", "PUBLISH", "PURCHASE", "CHARGE",
    "IRREVERSIBLE", "EXTERNAL_SIDE_EFFECT", "UNKNOWN",
)
AUTO_REPAIR_EXTERNAL_INSTRUCTION = re.compile(
    r"(?i)(?:\b(?:send|publish|deploy|purchase|charge|book|reserve)\b.{0,40}"
    r"(?:customer|email|message|production|public|payment|reservation)|"
    r"(?:顧客.{0,20}送信|公開|課金|購入|予約.{0,20}(?:作成|変更|取消)))"
)

# minkue_m720s_result_v1 — Autonomous Evidence へ通す契約キー（秘密値なし）
MINKUE_RESULT_KEYS = (
    "schema_version",
    "request_id",
    "request_hash",
    "run_key",
    "origin",
    "business_date_jst",
    "operation",
    "host_device_id",
    "fetcher_sha256",
    "started_at",
    "completed_at",
    "acquisition_status",
    "first_failed_boundary",
    "manifest_observation_id",
    "manifest_sha256",
    "ordered_new_ids",
    "ordered_new_list",
    "detail_observation_ids",
    "question_titles",
    "publication_results",
    "parent_request_id",
    "parent_request_hash",
    "parent_manifest_sha256",
    "hash_scope",
    "cache_bypass_evidence",
    "browser_closed",
    "external_post_count",
    # optional safe diagnostics (no secrets)
    "replay",
    "browser_launch_count",
    "durable_evidence_path",
    "durable_obs_source",
    "manifest_restore_status",
    "manifest_restore",
    "upsert_errors",
    "status",
    "error_code",
)
CODEX_RESULT_KEYS = (
    "schema_version", "request_id", "request_hash", "job_id", "terminal_status",
    "reason_code", "codex_launch_count", "verifier_launch_count",
    "execution_status", "first_failed_boundary", "verification_reason", "secondary_reason", "failure_evidence",
    "result_safe_facts",
    "slack_state", "slack_attempt_count", "slack_http_status", "replay",
    "lane_id", "resource_scope", "resource_key", "resource_release_safe", "work_number",
    "work_identity_key", "display_name_ja", "stage_key", "completion_scope",
    "existing_action_id", "existing_work_number", "existing_result_ref",
    "selected_lane", "selection_reason", "a_health", "b_health",
    "a_busy", "b_busy", "resource_conflict", "explicit_or_auto",
)
CITYHEAVEN_RESULT_KEYS = (
    "schema_version", "reservation_key", "request_hash", "terminal_status",
    "reason_code", "observed_at", "reservation_at", "cast", "course",
    "candidate_count", "observed_status", "reservation_ref",
    "course_source", "policy_version", "template_key", "chat_match_count",
    "message_match_count", "message_state", "would_send", "send_enabled",
    "chat_read_count", "reservation_mutation_count",
    "external_change_count", "chat_open_count", "customer_send_count",
    "task_cleanup_status",
)


class LocalExecError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _choice(value: Any) -> str:
    return str(value.get("name") or "") if isinstance(value, dict) else str(value or "")


def _preimplementation_gate_required(
    config: dict[str, Any], source_created_time: str | None, *, replay: bool = False,
) -> bool:
    cutoff_text = str(config.get("preimplementation_gate_v1_enforcement_cutoff") or "").strip()
    if not cutoff_text:
        return False
    if not source_created_time:
        if replay:
            return False
        raise LocalExecError("PREIMPLEMENTATION_GATE_ACTIVATION_SOURCE_UNAVAILABLE")
    try:
        cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
        created = datetime.fromisoformat(str(source_created_time).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalExecError("PREIMPLEMENTATION_GATE_ACTIVATION_SOURCE_UNAVAILABLE") from exc
    if cutoff.tzinfo is None or created.tzinfo is None:
        raise LocalExecError("PREIMPLEMENTATION_GATE_ACTIVATION_SOURCE_UNAVAILABLE")
    return created.astimezone(UTC) >= cutoff.astimezone(UTC)


def _parse_preimplementation_gate(task_instruction: Any) -> dict[str, str] | None:
    if not isinstance(task_instruction, str):
        return None
    lines = task_instruction.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    markers = [index for index, line in enumerate(lines) if line == "PREIMPLEMENTATION_GATE_V1"]
    if len(markers) != 1:
        return None
    start = markers[0] + 1
    if start + len(PREIMPLEMENTATION_GATE_FIELDS) > len(lines):
        return None
    result: dict[str, str] = {}
    for offset, expected_key in enumerate(PREIMPLEMENTATION_GATE_FIELDS):
        line = lines[start + offset]
        if "=" not in line:
            return None
        key, value = line.split("=", 1)
        if key != expected_key:
            return None
        result[key] = value.strip()
    return result


def evaluate_preimplementation_gate(
    task_instruction: Any, ssot_fields: dict[str, Any] | None = None,
) -> str | None:
    """Return the deterministic stop reason, or None when the declaration passes."""
    declaration = _parse_preimplementation_gate(task_instruction)
    if declaration is None:
        return "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID"
    if declaration["deterministic_first"] not in {"PASS", "NOT_APPLICABLE"}:
        return "DETERMINISTIC_FIRST_NOT_SATISFIED"
    if declaration["known_failure_reuse"] not in {"BLOCKED", "NONE"}:
        return "KNOWN_FAILURE_ROUTE_REUSE_BLOCKED"
    if declaration["existing_success_route"] not in {"REUSE", "NONE"}:
        return "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID"
    if declaration["reuse_decision"] not in {
        "REUSE", "EXTEND", "ADAPTER", "INTEGRATE", "NEW_JUSTIFIED",
    }:
        return "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID"
    if declaration["scope_mode"] not in {"MINIMUM", "GENERALIZATION_JUSTIFIED"}:
        return "PREIMPLEMENTATION_GATE_MISSING_OR_INVALID"

    fields = ssot_fields or {}
    recommended_asset = str(fields.get("推奨再利用Asset ID") or "").strip()
    if declaration["reuse_decision"] == "NEW_JUSTIFIED" and (
        recommended_asset or declaration["existing_success_route"] == "REUSE"
    ):
        return "SHORTEST_PATH_EXISTING_ASSET_NOT_REUSED"
    if declaration["reuse_decision"] == "NEW_JUSTIFIED" and not declaration["new_system_reason"]:
        return "NEW_SYSTEM_NOT_JUSTIFIED"
    if declaration["scope_mode"] == "GENERALIZATION_JUSTIFIED" and not declaration["generalization_reason"]:
        return "GENERALIZATION_NOT_JUSTIFIED"
    if _choice(fields.get("Chief of Staff Gate")).strip().upper() == "FAIL":
        return "CHIEF_OF_STAFF_GATE_FAILED"
    return None


def _load_preimplementation_ssot_fields(config: dict[str, Any], record_id: str) -> dict[str, Any]:
    """Read existing Next Actions fields by display name; never writes or changes schema."""
    record = _request(config, "GET", record_id=record_id)
    values = record.get("fields") if isinstance(record, dict) else None
    if not isinstance(values, dict):
        raise LocalExecError("PREIMPLEMENTATION_GATE_SSOT_READ_FAILED")
    return {name: values.get(name) for name in PREIMPLEMENTATION_SSOT_FIELDS}


def _load_token(config: dict[str, Any]) -> str:
    module_path = Path(config["secure_store_module"])
    secret_path = Path(config["credential_path"])
    spec = importlib.util.spec_from_file_location("wef_airtable_secure_store", module_path)
    if spec is None or spec.loader is None:
        raise LocalExecError("CREDENTIAL_LOADER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = module.read_secret(secret_path)
    if not raw:
        raise LocalExecError("CREDENTIAL_UNAVAILABLE")
    return raw.decode("utf-8").strip()


def _request(config: dict[str, Any], method: str, *, record_id: str | None = None,
             params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = config["base_id"]
    table = config["table_id"]
    url = f"https://api.airtable.com/v0/{base}/{table}"
    if record_id:
        url += "/" + urllib.parse.quote(record_id, safe="")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + _load_token(config))
    request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise LocalExecError(f"AIRTABLE_HTTP_{exc.code}") from exc
    except Exception as exc:
        raise LocalExecError("AIRTABLE_REQUEST_FAILED") from exc


def _patch(config: dict[str, Any], record_id: str, fields: dict[str, Any]) -> None:
    _request(
        config,
        "PATCH",
        record_id=record_id,
        body={"fields": fields, "typecast": False},
    )


def _filter_formula(config: dict[str, Any]) -> str:
    schema = config["schema_version"]
    device = config["device_id"]
    return (
        f'AND({{Local Exec Schema}}="{schema}",'
        f'{{Requested Host Device ID}}="{device}",'
        'OR({Local Exec State}="EF_QUEUED",{Local Exec State}="EF_RUNNING"))'
    )


def _records(config: dict[str, Any]) -> list[dict[str, Any]]:
    params = {"filterByFormula": _filter_formula(config), "pageSize": 50,
              "returnFieldsByFieldId": "true"}
    records = []
    while True:
        response = _request(config, "GET", params=params)
        records.extend(response.get("records") or [])
        if not response.get("offset"):
            return records
        params["offset"] = response["offset"]


def records_by_request_id(config: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    escaped = request_id.replace("\\", "\\\\").replace('"', '\\"')
    response = _request(
        config,
        "GET",
        params={
            "filterByFormula": f'{{Request ID}}="{escaped}"',
            "pageSize": 2,
            "returnFieldsByFieldId": "true",
        },
    )
    return list(response.get("records") or [])


def records_by_work_identity_key(config: dict[str, Any], identity_key: str) -> list[dict[str, Any]]:
    escaped = identity_key.replace("\\", "\\\\").replace('"', '\\"')
    field_name = str(config.get("dedup_key_formula_field") or "Dedup Key")
    response = _request(
        config,
        "GET",
        params={
            "filterByFormula": f'{{{field_name}}}="{escaped}"',
            "pageSize": 50,
            "returnFieldsByFieldId": "true",
        },
    )
    return list(response.get("records") or [])


def _fixed_fields(config: dict[str, Any]) -> dict[str, str]:
    return dict(config["fields"])


def _sanitize_scalar(item: Any) -> Any:
    if isinstance(item, (str, int, float, bool)) or item is None:
        return item
    if isinstance(item, list):
        out = []
        for el in item:
            if isinstance(el, (str, int, float, bool)) or el is None:
                out.append(el)
            elif isinstance(el, dict):
                out.append(_sanitize_scalar(el))
            else:
                out.append("UNKNOWN")
        return out
    if isinstance(item, dict):
        # one-level + nested list/dict of scalars for minkue result extras
        nested: dict[str, Any] = {}
        for k, v in item.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                nested[k] = v
            elif isinstance(v, list):
                nested[k] = _sanitize_scalar(v)
            elif isinstance(v, dict):
                inner: dict[str, Any] = {}
                for ik, iv in v.items():
                    if not isinstance(ik, str):
                        continue
                    if isinstance(iv, (str, int, float, bool)) or iv is None:
                        inner[ik] = iv
                    elif isinstance(iv, list):
                        inner[ik] = [
                            x if isinstance(x, (str, int, float, bool)) or x is None else "UNKNOWN"
                            for x in iv
                        ]
                    else:
                        inner[ik] = "UNKNOWN"
                nested[k] = inner
            else:
                nested[k] = "UNKNOWN"
        return nested
    return "UNKNOWN"


def _sanitize_result(value: Any) -> dict[str, Any]:
    """Pass minkue_m720s_result_v1 contract fields; keep 6-key API-observer sanitizer for others."""
    source = value if isinstance(value, dict) else {}
    schema = str(source.get("schema_version") or "")
    if schema == "codex_local_job_result_v1":
        return {
            key: sanitize_result_safe_facts(source[key]) if key == "result_safe_facts" else _sanitize_scalar(source[key])
            for key in CODEX_RESULT_KEYS if key in source
        }
    if schema == "cityheaven_reservation_result_v1":
        return {key: _sanitize_scalar(source[key]) for key in CITYHEAVEN_RESULT_KEYS if key in source}
    if schema == "minkue_m720s_result_v1":
        result: dict[str, Any] = {}
        for key in MINKUE_RESULT_KEYS:
            if key not in source:
                continue
            result[key] = _sanitize_scalar(source.get(key))
        # Always keep schema_version even if somehow missing from loop
        result["schema_version"] = "minkue_m720s_result_v1"
        return result

    result = {}
    for key in RESULT_KEYS:
        item = source.get(key, "UNKNOWN")
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        else:
            result[key] = "UNKNOWN"
    return result


def _receipt_evidence(path: Path, key_hash: str) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"foundation_receipt={path}; receipt_sha256={digest}; dedup_key={key_hash}"


def _failed_job(foundation: ExecutionFoundation, key_hash: str) -> dict[str, Any] | None:
    return read_json(foundation.failed / f"{key_hash}.json")


def _safe_error_code(value: Any, fallback: str = "FOUNDATION_FAILED") -> str:
    code = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback).strip()).strip("_.-")
    return (code or fallback)[:120]


def _safe_notification_value(value: Any, fallback: str, maximum: int = 120) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:@/-]+", "_", str(value or fallback).strip()).strip("_.")
    return (safe or fallback)[:maximum]


def _safe_human_value(value: Any, fallback: str, maximum: int = 160) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or fallback)).strip()
    return (text or fallback)[:maximum]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _auto_repair_reason(values: dict[str, Any], field: dict[str, str]) -> tuple[str | None, dict[str, Any]]:
    evidence = _json_object(values.get(field.get("evidence", "")))
    candidates = (
        evidence.get("reason_code"), evidence.get("verification_reason"),
        evidence.get("secondary_reason"), values.get(field.get("error_code", "")),
    )
    normalized = [str(value or "").strip().upper() for value in candidates]
    if any(marker in value for value in normalized for marker in AUTO_REPAIR_HUMAN_MARKERS):
        return None, evidence
    return next((value for value in normalized if value in AUTO_REPAIR_REASON_CODES), None), evidence


def _auto_repair_repo_is_local(request: dict[str, Any], settings: dict[str, Any]) -> bool:
    try:
        from .codex_local_job import resolve_workdir
        workdir = resolve_workdir(request.get("workdir_hint"), settings)
    except (KeyError, OSError, ValueError):
        return False
    if AUTO_REPAIR_EXTERNAL_INSTRUCTION.search(str(request.get("task_instruction") or "")):
        return False
    return any((candidate / ".git").exists() or (candidate / "pyproject.toml").is_file()
               for candidate in (workdir, *workdir.parents))


def _successor_exists(records: list[dict[str, Any]], parent_action_id: str,
                      expected_identity_key: str) -> bool:
    for record in records:
        values = record.get("fields") or {}
        raw = next((value for value in values.values() if isinstance(value, str)
                    and '"schema_version"' in value and '"request"' in value), "")
        envelope = _json_object(raw)
        request = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
        if request.get("resume_of_action_id") != parent_action_id:
            continue
        try:
            if validate_v3_fields(request)["work_identity_key"] == expected_identity_key:
                return True
        except ValueError:
            # A malformed claimed successor is a conflict, never permission to create another.
            return True
    return False


def _new_successor_request(parent: dict[str, Any], parent_action_id: str,
                           fingerprint: str, *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    request = copy.deepcopy(parent)
    digest = hashlib.sha256(
        f"auto-repair-v1\n{parent_action_id}\n{fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    request["request_id"] = f"auto-repair-v1-{digest}"
    request["work_number"] = current.strftime("%Y%m%d-%H%M%S-01")
    request["requested_at"] = current.isoformat(timespec="seconds").replace("+00:00", "Z")
    request["expires_at"] = (current + timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    request["resume_of_action_id"] = parent_action_id
    return request


def run_autonomous_repair_controller(
    foundation: ExecutionFoundation,
    config: dict[str, Any],
    failed_records: list[dict[str, Any]],
) -> dict[str, int]:
    """Create at most one mailbox-v3 successor for a deterministic safe failure.

    This is called only from the existing worker cycle after result sync.  It
    creates no local queue, watcher, service, task, or daemon; the successor is
    consumed by the ordinary Airtable pull on a later cycle.
    """
    counts = {"created": 0, "skipped": 0, "errors": 0}
    if config.get("autonomous_repair_controller_v1_enabled") is not True:
        return counts
    field = _fixed_fields(config)
    settings = foundation.config.get("codex_local_job") or {}
    for stale in failed_records:
        if record_paused(config, stale):
            continue
        if _choice((stale.get("fields") or {}).get(field["state"])) != "EF_FAILED":
            continue
        try:
            record_id = str(stale.get("id") or "")
            if not re.fullmatch(r"rec[A-Za-z0-9]{14,}", record_id):
                counts["skipped"] += 1
                continue
            machine = _request(config, "GET", record_id=record_id,
                               params={"returnFieldsByFieldId": "true"})
            named = _request(config, "GET", record_id=record_id,
                             params={"returnFieldsByFieldId": "false"})
            values = machine.get("fields") if isinstance(machine, dict) else None
            named_values = named.get("fields") if isinstance(named, dict) else None
            if not isinstance(values, dict) or not isinstance(named_values, dict):
                counts["errors"] += 1
                continue
            if _choice(values.get(field["state"])).upper() != "EF_FAILED" \
                    or str(values.get(field["action_type"]) or "") != "CODEX_LOCAL_JOB":
                counts["skipped"] += 1
                continue
            try:
                envelope = json.loads(str(values.get(field["action_json"]) or ""))
            except (TypeError, ValueError):
                counts["skipped"] += 1
                continue
            parent = envelope.get("request") if isinstance(envelope, dict) else None
            if envelope.get("schema_version") != MAILBOX_V3 or not isinstance(parent, dict):
                counts["skipped"] += 1
                continue
            contract = validate_v3_fields(parent)
            # A failed successor is terminal for this root; it never becomes a new root.
            if parent.get("resume_of_action_id") or not _auto_repair_repo_is_local(parent, settings):
                counts["skipped"] += 1
                continue
            machine_evidence = _json_object(values.get(field.get("evidence", "")))
            if str(machine_evidence.get("terminal_status") or "").upper() == "VERIFIED":
                counts["skipped"] += 1
                continue
            diagnostic_text = "\n".join(str(named_values.get(name) or "").upper() for name in (
                "Local Exec Error Code", "Autonomous Evidence", "Terminal Evidence",
                "問題Fingerprint", "03｜問題", "03｜推奨修理",
            ))
            if any(marker in diagnostic_text for marker in AUTO_REPAIR_HUMAN_MARKERS):
                counts["skipped"] += 1
                continue
            merged = dict(values)
            merged[field.get("evidence", "")] = named_values.get("Autonomous Evidence") \
                or values.get(field.get("evidence", ""))
            merged[field.get("error_code", "")] = named_values.get("Local Exec Error Code") \
                or values.get(field.get("error_code", ""))
            reason, terminal_evidence = _auto_repair_reason(merged, field)
            if reason is None:
                counts["skipped"] += 1
                continue
            if str(terminal_evidence.get("terminal_status") or "").upper() == "VERIFIED":
                counts["skipped"] += 1
                continue
            fingerprint = str(named_values.get("問題Fingerprint") or "").strip()
            if not fingerprint:
                boundary = str(terminal_evidence.get("first_failed_boundary") or "")
                fingerprint = "fp-auto-" + hashlib.sha256(
                    f"{contract['work_identity_key']}\n{reason}\n{boundary}".encode("utf-8")
                ).hexdigest()[:16]
            from .lanes import registry_lock
            lock_root = Path(foundation.config.get("shared_resource_dir") or foundation.root)
            with registry_lock(lock_root):
                history = records_by_work_identity_key(config, contract["work_identity_key"])
                if _successor_exists(history, record_id, contract["work_identity_key"]):
                    counts["skipped"] += 1
                    continue
                successor = _new_successor_request(parent, record_id, fingerprint)
                successor_envelope = {
                    **envelope,
                    "request": successor,
                }
                create_fields = {
                    field["request_id"]: successor["request_id"],
                    field["requested_host"]: config["device_id"],
                    field["schema"]: config["schema_version"],
                    field["action_type"]: "CODEX_LOCAL_JOB",
                    field["state"]: "EF_QUEUED",
                    field["action_json"]: json.dumps(successor_envelope, ensure_ascii=False, separators=(",", ":")),
                    field["work_number"]: successor["work_number"],
                    field.get("dedup_key", "fldFuV5lSqHumynSX"): contract["work_identity_key"],
                }
                response = _request(config, "POST", body={
                    "records": [{"fields": create_fields}], "typecast": False,
                })
            created = response.get("records") if isinstance(response, dict) else None
            if not isinstance(created, list) or len(created) != 1 or not created[0].get("id"):
                counts["errors"] += 1
                continue
            counts["created"] += 1
        except (KeyError, LocalExecError, OSError, TypeError, ValueError):
            counts["errors"] += 1
    return counts


def sync_results(
    foundation: ExecutionFoundation,
    config: dict[str, Any],
    records: list[dict[str, Any]] | None = None,
    *,
    failure_notifier: Notifier | None = None,
) -> dict[str, int]:
    field = _fixed_fields(config)
    counts = {"succeeded": 0, "failed": 0}
    source_records = records if records is not None else _records(config)
    for record in source_records:
        if record_paused(config, record):
            continue
        values = record.get("fields") or {}
        from .lanes import owns_record
        if not owns_record(config, values):
            continue
        if str(values.get(field["action_type"]) or "") in {"CODEX_LOCAL_JOB", "CITYHEAVEN_RESERVATION_SCHEDULE"} and str(values.get(field["requested_host"]) or "") != config["device_id"]:
            _reject(config, record["id"], "WRONG_HOST")
            values[field["state"]] = "EF_REJECTED"
            counts["failed"] += 1
            continue
        request_id = str(values.get(field["request_id"]) or "").strip()
        if not request_id:
            continue
        key_hash = stable_key(request_id)
        receipt_path = foundation.receipt_path(key_hash)
        receipt = read_json(receipt_path)
        if receipt:
            external = receipt.get("external_receipt") or {}
            result = _sanitize_result(external.get("content"))
            if foundation.config.get("shared_resource_dir") and values.get(field["action_type"]) == "CODEX_LOCAL_JOB":
                try:
                    _, replay_hash = validate_codex_request(config, values, request_id, replay=True)
                    if replay_hash != result.get("request_hash"):
                        raise LocalExecError("REQUEST_ID_INPUT_MISMATCH")
                except LocalExecError as exc:
                    _reject(config, record["id"], str(exc))
                    values[field["state"]] = "EF_REJECTED"
                    counts["failed"] += 1
                    continue
            is_codex_result = result.get("schema_version") == "codex_local_job_result_v1"
            terminal_status = result.get("terminal_status") if is_codex_result else None
            terminal_failed = is_codex_result and terminal_status != "VERIFIED"
            state = "EF_FAILED" if terminal_failed else "EF_SUCCEEDED"
            error_code = _safe_error_code(result.get("reason_code"), str(terminal_status)) if terminal_failed else ""
            _patch(
                config,
                record["id"],
                {
                    field["state"]: state,
                    field["finished"]: iso_now(),
                    field["resolved_host"]: config["device_id"],
                    field["heartbeat"]: iso_now(),
                    field["evidence"]: json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    field["evidence_ref"]: _receipt_evidence(receipt_path, key_hash),
                    field["error_code"]: error_code,
                },
            )
            if result.get("work_identity_key"):
                from .lanes import update_work_identity_state
                update_work_identity_state(
                    foundation,
                    {"work_identity_key": result["work_identity_key"],
                     "airtable_record_id": record["id"]},
                    "VERIFIED" if terminal_status == "VERIFIED" else "FAILED",
                    _receipt_evidence(receipt_path, key_hash),
                )
            values[field["state"]] = state
            counts["failed" if terminal_failed else "succeeded"] += 1
            continue
        failed = _failed_job(foundation, key_hash)
        if failed:
            error = str(failed.get("last_error") or "FOUNDATION_FAILED")
            error_code = _safe_error_code(error.split(":", 1)[0])
            terminal_external = failed.get("terminal_external_receipt") or {}
            terminal_result = _sanitize_result(terminal_external.get("content"))
            evidence = terminal_result if terminal_result else {"status": "FAILED"}
            external_path = terminal_external.get("path")
            external_sha = terminal_external.get("sha256")
            evidence_ref = f"foundation_failed={foundation.failed / (key_hash + '.json')}; dedup_key={key_hash}"
            if external_path and external_sha:
                evidence_ref += f"; external_receipt={external_path}; external_sha256={external_sha}"
            _patch(
                config,
                record["id"],
                {
                    field["state"]: "EF_FAILED",
                    field["finished"]: iso_now(),
                    field["resolved_host"]: config["device_id"],
                    field["heartbeat"]: iso_now(),
                    field["evidence"]: json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    field["evidence_ref"]: evidence_ref,
                    field["error_code"]: error_code,
                },
            )
            values[field["state"]] = "EF_FAILED"
            counts["failed"] += 1
            metadata = failed.get("metadata") or {}
            if metadata.get("work_identity_key"):
                from .lanes import update_work_identity_state
                update_work_identity_state(
                    foundation, metadata, "FAILED",
                    str(foundation.failed / f"{key_hash}.json"),
                )
            action_type = str(values.get(field["action_type"]) or metadata.get("local_exec_action_type") or "UNKNOWN")
            route = str(failed.get("action") or "UNKNOWN")
            notifier = failure_notifier
            standard_slack = False
            notification_config = foundation.config.get("foundation_failure_slack") or {}
            if notifier is None and notification_config.get("enabled") is True:
                notifier = lambda payload: send_foundation_failure_slack(payload, notification_config)
                standard_slack = True
            is_v3_foundation_failure = metadata.get("mailbox_schema") == MAILBOX_V3
            if notifier is not None and (
                (action_type != "CODEX_LOCAL_JOB" and route != "codex-local-job")
                or is_v3_foundation_failure
            ):
                payload = {
                    "work_id": _safe_notification_value(request_id, "UNKNOWN"),
                    "action_type": _safe_notification_value(action_type, "UNKNOWN"),
                    "route": _safe_notification_value(route, "UNKNOWN"),
                    "terminal_state": "EF_FAILED",
                    "error_code": error_code,
                    "attempt_count": max(1, int(failed.get("attempt", 0) or 0)),
                    "host": _safe_notification_value(config.get("device_id"), "UNKNOWN"),
                    **({
                        "work_number": _safe_human_value(metadata.get("work_number"), "不明"),
                        "display_name_ja": _safe_human_value(metadata.get("display_name_ja"), "不明"),
                        "stage_key": _safe_human_value(metadata.get("stage_key"), "不明"),
                        "completion_scope": _safe_notification_value(metadata.get("completion_scope"), "UNKNOWN"),
                    } if is_v3_foundation_failure else {}),
                }
                attempt_once(
                    foundation,
                    foundation.failed / f"{key_hash}.json",
                    payload,
                    notifier,
                    failed_receipt=failed,
                    healer_settings=notification_config,
                    standard_slack=standard_slack,
                )
    return counts


def _reject(config: dict[str, Any], record_id: str, code: str,
            evidence: dict[str, Any] | None = None) -> None:
    field = _fixed_fields(config)
    _patch(
        config,
        record_id,
        {
            field["state"]: "EF_REJECTED",
            field["finished"]: iso_now(),
            field["resolved_host"]: config["device_id"],
            field["heartbeat"]: iso_now(),
            field["error_code"]: code,
            **({field["evidence"]: json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))}
               if evidence is not None and field.get("evidence") else {}),
        },
    )


def validate_verification_spec(value: Any) -> dict[str, Any]:
    """Return an isolated, Bridge-compatible verification spec.

    Artifact existence is deliberately not checked here: required files are
    outputs that the managed job is expected to create.
    """
    if not isinstance(value, dict):
        raise ValueError("VERIFICATION_SPEC_INVALID")
    spec = copy.deepcopy(value)
    if "version" in spec and (type(spec["version"]) is not int or spec["version"] != 1):
        raise ValueError("VERIFICATION_SPEC_INVALID")
    criteria = spec.get("criteria")
    required_files = spec.get("required_files")
    if (not isinstance(criteria, str) or not criteria.strip() or len(criteria) > 30000
            or not isinstance(required_files, list) or not required_files):
        raise ValueError("VERIFICATION_SPEC_INVALID")
    seen: set[str] = set()
    for item in required_files:
        if not isinstance(item, dict):
            raise ValueError("VERIFICATION_SPEC_INVALID")
        raw_path = item.get("path")
        if (not isinstance(raw_path, str) or not raw_path or "\0" in raw_path
                or re.match(r"^[a-z]:", raw_path, re.IGNORECASE)
                or raw_path.startswith(("/", "\\"))):
            raise ValueError("VERIFICATION_SPEC_INVALID")
        normalized = raw_path.replace("\\", "/")
        if any(not part or part in {".", ".."} or ":" in part for part in normalized.split("/")):
            raise ValueError("VERIFICATION_SPEC_INVALID")
        key = normalized.casefold()
        if key in seen:
            raise ValueError("VERIFICATION_SPEC_INVALID")
        seen.add(key)
        item["path"] = normalized
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in {"json", "text"}:
            raise ValueError("VERIFICATION_SPEC_INVALID")
        if "sha256" in item and (
                not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"], re.IGNORECASE)):
            raise ValueError("VERIFICATION_SPEC_INVALID")
        if "json_equals" in item and (item["kind"] != "json" or not isinstance(item["json_equals"], dict)):
            raise ValueError("VERIFICATION_SPEC_INVALID")
    required_output = spec.get("required_output_contains")
    if "required_output_contains" in spec and (
            not isinstance(required_output, list)
            or any(not isinstance(item, str) or not item for item in required_output)):
        raise ValueError("VERIFICATION_SPEC_INVALID")
    return spec


def _job_intent_mode(task_instruction: Any) -> bool:
    if not isinstance(task_instruction, str):
        return False
    return next((line for line in task_instruction.splitlines() if line.strip()), "") == "JOB_INTENT_V1"


def _parse_job_intent(task_instruction: str) -> dict[str, str]:
    if any((ord(char) < 32 and char not in "\r\n") or ord(char) == 127 for char in task_instruction):
        raise LocalExecError("JOB_INTENT_CONTROL_CHARACTER")
    lines = [line for line in task_instruction.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if not lines or lines[0] != "JOB_INTENT_V1" or len(lines) != len(JOB_INTENT_FIELDS) + 1:
        raise LocalExecError("JOB_INTENT_INVALID")
    intent: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise LocalExecError("JOB_INTENT_INVALID")
        key, value = line.split("=", 1)
        if key not in JOB_INTENT_FIELDS:
            raise LocalExecError("JOB_INTENT_UNKNOWN_KEY")
        if key in intent:
            raise LocalExecError("JOB_INTENT_DUPLICATE_KEY")
        if not value or value != value.strip() or len(value) > JOB_INTENT_LIMITS[key]:
            raise LocalExecError("JOB_INTENT_VALUE_INVALID")
        intent[key] = value
    if set(intent) != set(JOB_INTENT_FIELDS):
        raise LocalExecError("JOB_INTENT_INVALID")
    if intent["task_class"] not in JOB_INTENT_CLASSES:
        raise LocalExecError("JOB_INTENT_TASK_CLASS_INVALID")
    joined = "\n".join(intent.values())
    if "```" in joined:
        raise LocalExecError("JOB_INTENT_CODE_FENCE_FORBIDDEN")
    if re.search(r"(?i)(?:\b(?:shell|cmd(?:\.exe)?|powershell(?:\.exe)?)\b|(?:^|\s)(?:bash|zsh)(?:\s|$))", joined):
        raise LocalExecError("JOB_INTENT_COMMAND_FORBIDDEN")
    if re.search(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)", joined):
        raise LocalExecError("JOB_INTENT_URL_FORBIDDEN")
    if re.search(
        r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\bAKIA[0-9A-Z]{16}\b|"
        r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b|"
        r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret|credential)\s*[:=]\s*\S+|"
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)",
        joined,
    ):
        raise LocalExecError("JOB_INTENT_SECRET_FORBIDDEN")
    return intent


def _normalize_job_intent(request: dict[str, Any]) -> dict[str, Any]:
    intent = _parse_job_intent(request["task_instruction"])
    request_id = request.get("request_id")
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id)
            or request_id in {".", ".."} or ".." in request_id):
        raise LocalExecError("JOB_INTENT_REQUEST_ID_INVALID")
    if request.get("verification_spec") != JOB_INTENT_PLACEHOLDER:
        raise LocalExecError("JOB_INTENT_VERIFICATION_PLACEHOLDER_INVALID")
    task_class = intent["task_class"]
    reuse_decision, class_rule = JOB_INTENT_CLASS_RULES[task_class]
    gate = "\n".join((
        "PREIMPLEMENTATION_GATE_V1",
        "existing_success_route=REUSE",
        f"reuse_decision={reuse_decision}",
        "scope_mode=MINIMUM",
        "deterministic_first=PASS",
        "known_failure_reuse=BLOCKED",
        "new_system_reason=",
        "generalization_reason=",
    ))
    request["task_instruction"] = "\n".join((
        JOB_INTENT_REQUIRED_CONTEXT,
        f"Goal: {intent['goal']}",
        f"Task class: {task_class}",
        f"Target: {intent['target']}",
        f"Success condition: {intent['success']}",
        f"Caller prohibition: {intent['forbidden']}",
        f"Fixed prohibition: {class_rule}",
        "Use the existing CODEX_LOCAL_JOB route, admission, worker, Completion Bridge, and verifier.",
        gate,
    ))
    required_files = [{"path": f"local/{request_id}/summary.json", "kind": "json"}]
    if task_class == "CODE_REPAIR":
        required_files.append({"path": f"local/{request_id}/proof.json", "kind": "json"})
    request["verification_spec"] = {
        "version": 1,
        "criteria": (
            f"JOB_INTENT_V1 class={task_class}; goal={json.dumps(intent['goal'], ensure_ascii=False)}; "
            f"success={json.dumps(intent['success'], ensure_ascii=False)}; fixed prohibition={class_rule}; "
            "producer自己申告だけでVERIFIEDにしない。"
        ),
        "required_files": required_files,
    }
    return request


def validate_codex_request(config: dict[str, Any], values: dict[str, Any],
                           request_id: str, *, replay: bool = False,
                           source_created_time: str | None = None,
                           gate_ssot_fields: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    field = _fixed_fields(config)
    if str(values.get(field["requested_host"]) or "") != config["device_id"]:
        raise LocalExecError("WRONG_HOST")
    try:
        envelope = json.loads(str(values.get(field["action_json"]) or ""))
    except (ValueError, TypeError) as exc:
        raise LocalExecError("REQUEST_JSON_INVALID") from exc
    if not isinstance(envelope, dict) or envelope.get("schema_version") not in {
        "codex_local_job_mailbox_v1", "codex_local_job_mailbox_v2", MAILBOX_V3,
    }:
        raise LocalExecError("REQUEST_SCHEMA_INVALID")
    if envelope.get("host_device_id") != config["device_id"] or envelope.get("action_type") != "CODEX_LOCAL_JOB":
        raise LocalExecError("REQUEST_ROUTE_INVALID")
    request = envelope.get("request")
    cutoff_text = str(config.get("codex_local_job_v3_enforcement_cutoff") or "").strip()
    if cutoff_text and envelope["schema_version"] != MAILBOX_V3 and not replay:
        if not source_created_time:
            raise LocalExecError("LEGACY_CUTOFF_SOURCE_UNAVAILABLE")
        try:
            cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
            created = datetime.fromisoformat(str(source_created_time).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LocalExecError("LEGACY_CUTOFF_SOURCE_UNAVAILABLE") from exc
        if cutoff.tzinfo is None or created.tzinfo is None:
            raise LocalExecError("LEGACY_CUTOFF_SOURCE_UNAVAILABLE")
        if created.astimezone(UTC) >= cutoff.astimezone(UTC):
            raise LocalExecError("WORK_IDENTITY_V3_REQUIRED")
    required = {"request_id", "task_name", "task_instruction", "verification_spec", "requested_at", "expires_at"}
    allowed = required | {"workdir_hint", "model", "reasoning", "lane", "resource_scope", "resource_key"}
    if envelope["schema_version"] in {"codex_local_job_mailbox_v2", MAILBOX_V3}:
        required.add("work_number")
        allowed.add("work_number")
    if envelope["schema_version"] == MAILBOX_V3:
        required |= {"display_name_ja", "stage_key", "completion_scope", "work_identity"}
        allowed |= {"display_name_ja", "stage_key", "completion_scope", "work_identity", "resume_of_action_id"}
    if not isinstance(request, dict) or not required <= request.keys() or not request.keys() <= allowed:
        if envelope["schema_version"] in {"codex_local_job_mailbox_v2", MAILBOX_V3} and (
            not isinstance(request, dict) or not str(request.get("work_number") or "").strip()
        ):
            raise LocalExecError("WORK_NUMBER_REQUIRED")
        if envelope["schema_version"] == MAILBOX_V3 and isinstance(request, dict):
            if not str(request.get("display_name_ja") or "").strip():
                raise LocalExecError("DISPLAY_NAME_JA_REQUIRED")
            if "work_identity" not in request:
                raise LocalExecError("WORK_IDENTITY_INVALID")
        raise LocalExecError("REQUEST_FIELDS_INVALID")
    request = copy.deepcopy(request)
    if _job_intent_mode(request.get("task_instruction")):
        request = _normalize_job_intent(request)
    try:
        request["verification_spec"] = validate_verification_spec(request["verification_spec"])
    except ValueError as exc:
        raise LocalExecError(str(exc)) from exc
    if envelope["schema_version"] in {"codex_local_job_mailbox_v2", MAILBOX_V3}:
        work_number = request["work_number"]
        if not isinstance(work_number, str) or not re.fullmatch(r"\d{8}-\d{6}(?:-\d{2})?", work_number):
            raise LocalExecError("WORK_NUMBER_INVALID")
        airtable_work_number = values.get(field.get("work_number", "flddVEFQwuv6s0YQS"))
        if not isinstance(airtable_work_number, str) or not airtable_work_number.strip():
            raise LocalExecError("WORK_NUMBER_REQUIRED")
        if airtable_work_number != work_number:
            raise LocalExecError("WORK_NUMBER_MISMATCH")
    if envelope["schema_version"] == MAILBOX_V3:
        try:
            contract = validate_v3_fields(request)
        except ValueError as exc:
            raise LocalExecError(str(exc)) from exc
        dedup_field = field.get("dedup_key", "fldFuV5lSqHumynSX")
        supplied_identity_key = values.get(dedup_field)
        if supplied_identity_key and supplied_identity_key != contract["work_identity_key"]:
            raise LocalExecError("WORK_IDENTITY_KEY_MISMATCH")
    from .lanes import validate_resource
    try:
        validate_resource(request)
    except ValueError as exc:
        raise LocalExecError(str(exc)) from exc
    if request.get("lane", "A") != "AUTO" and request.get("lane", "A") != config.get("lane_id", "A"):
        raise LocalExecError("REQUEST_LANE_MISMATCH")
    if request["request_id"] != request_id or not isinstance(request_id, str) or not request_id.strip():
        raise LocalExecError("REQUEST_ID_MISMATCH")
    if any(not isinstance(request[key], str) or not request[key].strip() for key in ("task_name", "task_instruction", "requested_at", "expires_at")):
        raise LocalExecError("REQUEST_FIELDS_INVALID")
    if len(request["task_name"]) > 120 or len(request["task_instruction"]) > 30000 or not isinstance(request["verification_spec"], dict):
        raise LocalExecError("REQUEST_FIELDS_INVALID")
    if any(key in request["task_instruction"].lower() for key in ("```powershell", "```bash", "```cmd", "powershell -", "cmd /c")):
        raise LocalExecError("TASK_INSTRUCTION_NOT_NATURAL_LANGUAGE")
    if _preimplementation_gate_required(config, source_created_time, replay=replay):
        gate_error = evaluate_preimplementation_gate(request["task_instruction"], gate_ssot_fields)
        if gate_error:
            raise LocalExecError(gate_error)
    for key in ("workdir_hint", "model", "reasoning"):
        if key in request and not isinstance(request[key], str):
            raise LocalExecError("REQUEST_FIELDS_INVALID")
    try:
        requested = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalExecError("REQUEST_TIME_INVALID") from exc
    if requested.tzinfo is None or expires.tzinfo is None or expires <= requested:
        raise LocalExecError("REQUEST_TIME_INVALID")
    if not replay and datetime.now(UTC) >= expires:
        raise LocalExecError("REQUEST_EXPIRED")
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return request, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _history_contract(config: dict[str, Any], record: dict[str, Any],
                      expected_identity_key: str) -> dict[str, Any] | None:
    values = record.get("fields") or {}
    field = _fixed_fields(config)
    action_id = str(record.get("id") or "")
    try:
        envelope = json.loads(str(values.get(field["action_json"]) or ""))
        if envelope.get("schema_version") != MAILBOX_V3:
            raise ValueError("not v3")
        request = envelope.get("request") or {}
        contract = validate_v3_fields(request)
        state = _choice(values.get(field["state"])).upper()
        evidence_raw = values.get(field.get("evidence", ""))
        try:
            evidence = json.loads(evidence_raw) if isinstance(evidence_raw, str) else (evidence_raw or {})
        except ValueError:
            evidence = {}
        terminal = str(evidence.get("terminal_status") or "").upper()
        if state in {"EF_QUEUED", "EF_RUNNING", "QUEUED", "RUNNING", "ACTIVE"}:
            identity_state = "ACTIVE"
        elif state in {"EF_SUCCEEDED", "SUCCEEDED", "COMPLETE", "COMPLETED"} and terminal == "VERIFIED":
            identity_state = "VERIFIED"
        elif state in {"EF_FAILED", "EF_REJECTED", "FAILED", "BLOCKED", "VERIFY_FAILED"} or terminal in {"FAILED", "VERIFY_FAILED"}:
            identity_state = "FAILED"
        else:
            identity_state = "BLOCKED"
        return {
            **contract,
            "action_id": action_id,
            "request_id": request.get("request_id"),
            "work_number": request.get("work_number") or values.get(field.get("work_number", "flddVEFQwuv6s0YQS")),
            "identity_state": identity_state,
            "result_ref": values.get(field.get("evidence_ref", "")),
        }
    except (ValueError, TypeError, AttributeError):
        # A row claiming this Dedup Key but not carrying the same valid v3
        # contract is an identity/spec conflict, never ignorable history.
        return {
            "work_identity_key": expected_identity_key,
            "verification_spec_hash": "INVALID_OR_CONFLICTING_HISTORY",
            "action_id": action_id,
            "work_number": values.get(field.get("work_number", "flddVEFQwuv6s0YQS")),
            "identity_state": "BLOCKED",
        }


def _identity_gate_result(contract: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "codex_local_job_result_v1",
        **result_metadata({**contract, **decision}),
        "terminal_status": "BLOCKED",
        "reason_code": decision["code"],
        "codex_launch_count": 0,
        "verifier_launch_count": 0,
    }


def ingest_queued(
    foundation: ExecutionFoundation,
    config: dict[str, Any],
    records: list[dict[str, Any]] | None = None,
    *,
    identity_records: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    field = _fixed_fields(config)
    allowed = dict(config["action_map"])
    counts = {"enqueued": 0, "noop": 0, "rejected": 0}
    source_records = records if records is not None else _records(config)
    for record in source_records:
        if record_paused(config, record):
            continue
        values = record.get("fields") or {}
        from .lanes import owns_record
        if not owns_record(config, values):
            continue
        if _choice(values.get(field["state"])) != "EF_QUEUED":
            continue
        request_id = str(values.get(field["request_id"]) or "").strip()
        action_type = str(values.get(field["action_type"]) or "")
        if not request_id:
            _reject(config, record["id"], "REQUEST_ID_REQUIRED")
            counts["rejected"] += 1
            continue
        internal_action = allowed.get(action_type)
        if internal_action is None:
            _reject(config, record["id"], "ALLOWLIST_DENIED")
            counts["rejected"] += 1
            continue
        request_hash = None
        resource = None
        identity_metadata: dict[str, Any] = {}
        lane_selection: dict[str, Any] = {}
        enqueue_options: dict[str, Any] = {}
        if action_type == "CODEX_LOCAL_JOB":
            key_hash = stable_key(request_id)
            existing = read_json(foundation.receipt_path(key_hash))
            if not existing:
                existing = read_json(foundation.pending / f"{key_hash}.json") or read_json(foundation.inflight / f"{key_hash}.json")
            try:
                gate_ssot_fields = None
                if _preimplementation_gate_required(
                    config, record.get("createdTime"), replay=bool(existing),
                ):
                    gate_ssot_fields = _load_preimplementation_ssot_fields(config, record["id"])
                request, request_hash = validate_codex_request(
                    config, values, request_id, replay=bool(existing),
                    source_created_time=record.get("createdTime"),
                    gate_ssot_fields=gate_ssot_fields,
                )
                if existing:
                    prior_hash = (existing.get("external_receipt") or {}).get("content", {}).get("request_hash") or (existing.get("metadata") or {}).get("request_hash")
                    if prior_hash != request_hash:
                        raise LocalExecError("REQUEST_ID_INPUT_MISMATCH")
                if request.get("lane") == "AUTO" and not foundation.config.get("shared_resource_dir"):
                    raise LocalExecError("AUTO_LANE_SHARED_REGISTRY_REQUIRED")
                if foundation.config.get("shared_resource_dir"):
                    from .lanes import admit_auto_request, admit_request, resource_descriptor
                    resource = resource_descriptor(request, foundation.config["codex_local_job"])
                    # Search both lane spools/receipts before immutable global admission.
                    shared_base = Path(foundation.config["shared_resource_dir"]).parent
                    for state_root in (shared_base, shared_base / "lanes" / "B"):
                        for part in ("receipts", "spool/pending", "spool/inflight", "spool/failed", "spool/done"):
                            old = read_json(state_root / part / f"{key_hash}.json")
                            if old:
                                old_hash = (old.get("metadata") or {}).get("request_hash") or (((old.get("external_receipt") or {}).get("content") or {}).get("request_hash"))
                                if old_hash and old_hash != request_hash:
                                    raise LocalExecError("REQUEST_ID_INPUT_MISMATCH")
                    requested_lane = request.get("lane", "A")
                    if requested_lane == "AUTO":
                        lane_selection = admit_auto_request(
                            foundation, request_id, request_hash, resource)
                        if lane_selection.get("selected_lane") != config.get("lane_id", "A"):
                            # Another worker owns the durable selection, or no
                            # safe lane exists yet.  Keep the Airtable row queued.
                            continue
                    else:
                        admit_request(foundation, request_id, request_hash)
                        lane_selection = {
                            "selected_lane": requested_lane,
                            "selection_reason": "EXPLICIT_LANE",
                            "a_health": None, "b_health": None,
                            "a_busy": None, "b_busy": None,
                            "resource_conflict": None,
                            "explicit_or_auto": "EXPLICIT",
                        }
                try:
                    envelope = json.loads(str(values.get(field["action_json"]) or ""))
                except (ValueError, TypeError):
                    envelope = {}
                if envelope.get("schema_version") == MAILBOX_V3:
                    from .lanes import claim_work_identity

                    contract = validate_v3_fields(request)
                    candidates = identity_records
                    if candidates is None:
                        candidates = records_by_work_identity_key(config, contract["work_identity_key"])
                    history = []
                    for item in candidates:
                        if item.get("id") == record.get("id"):
                            continue
                        item_contract = _history_contract(config, item, contract["work_identity_key"])
                        if item_contract:
                            history.append(item_contract)
                    candidate = {
                        **contract,
                        "action_id": record["id"],
                        "request_id": request_id,
                        "work_number": request["work_number"],
                    }
                    decision = claim_work_identity(foundation, candidate, history)
                    if not decision.get("allowed"):
                        evidence = _identity_gate_result({**request, **contract}, decision)
                        _reject(config, record["id"], str(decision["code"]), evidence)
                        counts["rejected"] += 1
                        continue
                    identity_metadata = {
                        **result_metadata({**request, **contract}),
                        "mailbox_schema": MAILBOX_V3,
                        "verification_spec_hash": contract["verification_spec_hash"],
                    }
                    dedup_field = field.get("dedup_key", "fldFuV5lSqHumynSX")
                    if not values.get(dedup_field):
                        _patch(config, record["id"], {dedup_field: contract["work_identity_key"]})
                        values[dedup_field] = contract["work_identity_key"]
            except (LocalExecError, ValueError, OSError) as exc:
                _reject(config, record["id"], str(exc) if isinstance(exc, (LocalExecError, ValueError)) else "WORKDIR_INVALID")
                counts["rejected"] += 1
                continue
        elif action_type == "CITYHEAVEN_RESERVATION_SCHEDULE":
            from .cityheaven_reservation import CityHeavenReservationError, validate_reservation_record

            key_hash = stable_key(request_id)
            existing = read_json(foundation.receipt_path(key_hash))
            if not existing:
                existing = read_json(foundation.pending / f"{key_hash}.json") or read_json(foundation.inflight / f"{key_hash}.json")
            try:
                _, request_hash = validate_reservation_record(config, values, request_id, expected_state="EF_QUEUED",
                                                               allow_past=bool(existing))
                due_at = datetime.fromisoformat(
                    str(values.get(field["due_date"]) or "").replace("Z", "+00:00")
                )
                if due_at.tzinfo is None:
                    raise CityHeavenReservationError("DUE_DATE_INVALID")
                enqueue_options["not_before_epoch"] = due_at.timestamp()
                if existing:
                    prior_hash = ((existing.get("external_receipt") or {}).get("content") or {}).get("request_hash") \
                        or (existing.get("metadata") or {}).get("request_hash")
                    if prior_hash != request_hash:
                        raise CityHeavenReservationError("REQUEST_ID_INPUT_MISMATCH")
            except CityHeavenReservationError as exc:
                _reject(config, record["id"], str(exc))
                counts["rejected"] += 1
                continue
        result = foundation.enqueue(
            action=internal_action,
            idempotency_key=request_id,
            args=[],
            metadata={
                "airtable_record_id": record["id"],
                "local_exec_action_type": action_type,
                "schema_version": config["schema_version"],
                "target_device": config["device_id"],
                **({"request_hash": request_hash} if request_hash else {}),
                **({"work_number": request.get("work_number")} if action_type == "CODEX_LOCAL_JOB" and request.get("work_number") else {}),
                **identity_metadata,
                **lane_selection,
                **({"resource": resource, "lane_id": config.get("lane_id", "A")} if resource else {}),
            },
            **enqueue_options,
        )
        status = str(result.get("status") or "")
        if action_type == "CITYHEAVEN_RESERVATION_SCHEDULE" and status == "PAUSED_BY_OWNER":
            counts["paused"] = counts.get("paused", 0) + 1
            continue
        if status not in {"ENQUEUED", "NOOP_QUEUED", "NOOP_RECEIPT"}:
            raise LocalExecError(f"FOUNDATION_ENQUEUE_{status or 'UNKNOWN'}")
        # A CODEX_LOCAL_JOB is only queued here. Its executor records EF_RUNNING
        # after the Foundation claim, resource lease, and complete preflight.
        # A receipt is terminal truth owned by sync_results and must not regress.
        if action_type != "CODEX_LOCAL_JOB":
            started = iso_now()
            _patch(
                config,
                record["id"],
                {
                    field["state"]: "EF_RUNNING",
                    field["resolved_host"]: config["device_id"],
                    field["started"]: values.get(field["started"]) or started,
                    field["heartbeat"]: started,
                    field["error_code"]: "",
                },
            )
        if status == "ENQUEUED":
            counts["enqueued"] += 1
        else:
            counts["noop"] += 1
    return counts


def run_before_work(foundation: ExecutionFoundation, config: dict[str, Any]) -> dict[str, Any]:
    records = _records(config)
    synced = sync_results(foundation, config, records)
    repaired = run_autonomous_repair_controller(foundation, config, records)
    ingested = ingest_queued(foundation, config, records)
    return {"status": "PASS", "synced": synced, "repaired": repaired, "ingested": ingested}


def run_after_work(foundation: ExecutionFoundation, config: dict[str, Any]) -> dict[str, Any]:
    records = _records(config)
    synced = sync_results(foundation, config, records)
    repaired = run_autonomous_repair_controller(foundation, config, records)
    return {"status": "PASS", "synced": synced, "repaired": repaired}
