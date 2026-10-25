from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .core import ExecutionFoundation, atomic_write_json, exclusive_lock, iso_now, read_json

Notifier = Callable[[dict[str, Any]], Any]
_PAYLOAD_KEYS = (
    "work_id",
    "action_type",
    "route",
    "terminal_state",
    "error_code",
    "attempt_count",
    "host",
)
_DEFAULT_HEALER_ACTION_TYPES = frozenset({"CITYHEAVEN_RESERVATION_SCHEDULE"})
_SAFE_CODE = re.compile(r"[A-Z0-9_]{1,120}")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9|_.:-]{1,200}")
_CANONICAL_HEALER = Path(
    r"C:\Users\epuro\AppData\Local\TantanManagementOS\CodexBridge\runtime\ai_healer.py"
)
_CANONICAL_HEALER_PYTHON = _CANONICAL_HEALER.parent / "ai_healer_venv" / "Scripts" / "python.exe"


def _healer_action_types(settings: dict[str, Any] | None) -> frozenset[str]:
    configured = (settings or {}).get("healer_action_types")
    if configured is None:
        return _DEFAULT_HEALER_ACTION_TYPES
    if not isinstance(configured, list):
        return frozenset()
    return frozenset(
        value for value in configured
        if isinstance(value, str) and _SAFE_CODE.fullmatch(value)
    )


def healer_eligible(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> bool:
    action_type = str(payload.get("action_type") or "")
    request_id = str(payload.get("work_id") or "")
    return (
        action_type != "CODEX_LOCAL_JOB"
        and action_type in _healer_action_types(settings)
        and _SAFE_REQUEST_ID.fullmatch(request_id) is not None
    )


def _safe_reason(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_CODE.fullmatch(candidate) else fallback


def _sanitized_healer_context(
    payload: dict[str, Any], failed_receipt: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
    external = receipt.get("terminal_external_receipt")
    content = external.get("content") if isinstance(external, dict) else {}
    content = content if isinstance(content, dict) else {}
    fallback_reason = _safe_reason(payload.get("error_code"), "FOUNDATION_FAILED")
    reason = _safe_reason(content.get("reason_code"), fallback_reason)
    action_type = _safe_reason(payload.get("action_type"), "FOUNDATION_FAILURE")
    boundary = _safe_reason(content.get("first_failed_boundary"), action_type)
    status = {
        "request_id": str(payload["work_id"]),
        "status": "FAILED",
        "reason_code": reason,
        "first_failed_boundary": boundary,
    }
    log_fields = {
        "action_type": action_type,
        "route": re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("route") or "UNKNOWN"))[:120],
        "terminal_state": "EF_FAILED",
        "error_code": fallback_reason,
        "external_reason_code": reason,
        "attempt_count": max(1, int(payload.get("attempt_count", 1) or 1)),
    }
    log_text = "\n".join(f"{key}={value}" for key, value in log_fields.items()) + "\n"
    return status, log_text


def _bounded_healer_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"state": "PARSE_FAILED"}
    result: dict[str, Any] = {"state": _safe_reason(value.get("state"), "UNKNOWN")}
    if isinstance(value.get("model"), str):
        result["model"] = re.sub(r"[^A-Za-z0-9_.-]", "", value["model"])[:80]
    diagnosis = value.get("diagnosis")
    if isinstance(diagnosis, dict):
        result["diagnosis"] = {
            key: str(diagnosis.get(key) or "")[:1000]
            for key in ("summary", "likely_cause", "suggested_action")
        }
    write = value.get("airtable_write")
    if isinstance(write, dict):
        result["airtable_write"] = {
            "state": _safe_reason(write.get("state"), "UNKNOWN"),
            "write_count": min(1, max(0, int(write.get("write_count", 0) or 0))),
        }
        fingerprint = str(write.get("fingerprint") or "")
        if re.fullmatch(r"fp-[a-f0-9]{16}", fingerprint):
            result["airtable_write"]["fingerprint"] = fingerprint
    return result


def run_foundation_healer(
    payload: dict[str, Any],
    failed_receipt: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the canonical healer once using only a short-lived allowlisted context."""
    if not healer_eligible(payload, settings):
        return {"state": "NOT_ELIGIBLE", "gemini_call_count": 0, "airtable_write_count": 0}
    status, log_text = _sanitized_healer_context(payload, failed_receipt)
    python = Path(_environment_value("AI_HEALER_PYTHON") or _CANONICAL_HEALER_PYTHON)
    if not python.is_file() or not _CANONICAL_HEALER.is_file():
        return {"state": "RUNTIME_MISSING", "gemini_call_count": 0, "airtable_write_count": 0}
    configured_timeout = int((settings or {}).get("healer_timeout_seconds", 20) or 20)
    timeout = min(30, max(1, configured_timeout))
    try:
        with tempfile.TemporaryDirectory(prefix="foundation-healer-") as directory:
            job_dir = Path(directory)
            (job_dir / "status.json").write_text(
                json.dumps(status, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            (job_dir / "executor-output.log").write_text(log_text, encoding="utf-8")
            request = {
                "terminal_state": "FAILED",
                "reason_code": status["reason_code"],
                "first_failed_boundary": status["first_failed_boundary"],
                "adapter": "script",
                "job_dir": str(job_dir),
            }
            completed = subprocess.run(
                [str(python), str(_CANONICAL_HEALER)],
                input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        if completed.returncode != 0:
            return {"state": "PROCESS_FAILED", "gemini_call_count": 0, "airtable_write_count": 0}
        result = _bounded_healer_result(json.loads(completed.stdout))
    except subprocess.TimeoutExpired:
        return {"state": "TIMEOUT", "gemini_call_count": 0, "airtable_write_count": 0}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"state": "PARSE_FAILED", "gemini_call_count": 0, "airtable_write_count": 0}
    state = str(result.get("state") or "UNKNOWN")
    airtable_state = str((result.get("airtable_write") or {}).get("state") or "")
    result["gemini_call_count"] = int(
        state in {"DIAGNOSED", "API_FAILED"} and airtable_state != "SKIPPED_DUPLICATE"
    )
    result["airtable_write_count"] = int((result.get("airtable_write") or {}).get("write_count", 0) or 0)
    return result


def _safe_ai_text(value: Any, limit: int = 300) -> str:
    text = str(value or "")
    substitutions = (
        (r"https?://\S+", "[URL]"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
        (r"\b(?:\+?\d[\d\- ()]{8,}\d)\b", "[PHONE]"),
        (r"[A-Za-z]:\\[^\s\"']+", "[LOCAL_PATH]"),
        (r"(?i)(token|cookie|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    return text.replace("\r", " ").replace("\n", " ")[:limit]


def _healer_message_lines(payload: dict[str, Any]) -> list[str]:
    result = payload.get("_foundation_healer")
    if not isinstance(result, dict):
        return []
    state = str(result.get("state") or "UNKNOWN")
    if state == "DIAGNOSED" and isinstance(result.get("diagnosis"), dict):
        diagnosis = result["diagnosis"]
        return [
            "AI参考診断（未検証）:",
            f"- model: {_safe_ai_text(result.get('model') or 'UNKNOWN', 80)}",
            f"- summary: {_safe_ai_text(diagnosis.get('summary'))}",
            f"- likely_cause: {_safe_ai_text(diagnosis.get('likely_cause'))}",
            f"- suggested_action: {_safe_ai_text(diagnosis.get('suggested_action'))}",
        ]
    if state.startswith("RATE_LIMITED") or state in {
        "QUOTA_UNAVAILABLE", "KEY_MISSING", "API_FAILED", "SDK_MISSING", "TIMEOUT",
        "PROCESS_FAILED", "PARSE_FAILED", "RUNTIME_MISSING", "DISABLED",
    }:
        return [f"AI参考診断: スキップ（{state}）"]
    return []


def _environment_value(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except (OSError, ImportError):
        return ""


def _message(payload: dict[str, Any], notify_user_name: str = "TANTAN_SLACK_NOTIFY_USER_ID") -> str:
    if all(str(payload.get(key) or "").strip() for key in ("work_number", "display_name_ja", "stage_key")):
        code = str(payload.get("error_code") or "UNKNOWN")
        known_reasons = {
            "DUPLICATE_WORK_ACTIVE": "同じ作業が既に実行中です",
            "WORK_ALREADY_COMPLETE": "同じ作業は検証済みです",
            "WORK_REQUIRES_RESUME": "既存の失敗からの再開指定が必要です",
            "WORK_IDENTITY_SPEC_CONFLICT": "同じ作業識別子の仕様が一致しません",
            "WORK_IDENTITY_V3_REQUIRED": "mailbox_v3の作業識別情報が必要です",
        }
        reason = known_reasons.get(code, f"原因未分類（コード: {code}）")
        terminal = str(payload.get("terminal_state") or "UNKNOWN")
        if code == "DUPLICATE_WORK_ACTIVE":
            next_step, tantan = "既存の実行を継続します。追加実行はありません。", "不要"
        elif code == "WORK_ALREADY_COMPLETE":
            next_step, tantan = "既存の検証済み結果を参照します。追加実行はありません。", "不要"
        elif code == "WORK_REQUIRES_RESUME":
            next_step, tantan = "既存の失敗を確認し、再開可否を判断します。自動再実行はありません。", "必要"
        elif terminal in {"EF_FAILED", "FAILED", "VERIFY_FAILED", "BLOCKED"}:
            next_step, tantan = "失敗状態を保持します。業務Jobの自動再実行はありません。", "必要"
        else:
            next_step, tantan = "状態を確認します。追加実行はありません。", "未判定"
        lines = [
            "Foundation 作業結果",
            f"作業番号: {payload['work_number']}",
            f"作業: {payload['display_name_ja']}",
            f"工程: {str(payload['stage_key']).replace('_', ' / ').replace('-', ' / ')}",
            f"結果: {terminal}（{reason}）",
            f"次: {next_step}",
            f"たんたんの対応: {tantan}",
            "詳細:",
        ]
        lines.extend(f"- {key}: {payload.get(key, 'UNKNOWN')}" for key in _PAYLOAD_KEYS)
        lines.append(f"- completion_scope: {payload.get('completion_scope', 'UNKNOWN')}")
        notify_user = _environment_value(notify_user_name)
        mention = f"<@{notify_user}> " if re.fullmatch(r"[UW][A-Z0-9]{8,}", notify_user) else ""
        lines.extend(_healer_message_lines(payload))
        return mention + "\n".join(lines)
    safe = {key: payload[key] for key in _PAYLOAD_KEYS}
    lines = ["Foundation terminal failure"]
    lines.extend(f"{key}: {safe[key]}" for key in _PAYLOAD_KEYS)
    lines.extend(_healer_message_lines(payload))
    notify_user = _environment_value(notify_user_name)
    mention = f"<@{notify_user}> " if re.fullmatch(r"[UW][A-Z0-9]{8,}", notify_user) else ""
    return mention + "\n".join(lines)


def send_foundation_failure_slack(
    payload: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the Completion Bridge Slack environment contract without storing its secret."""
    config = settings or {}
    webhook_name = str(config.get("webhook_environment_variable") or "TANTAN_SLACK_WEBHOOK_URL")
    notify_user_name = str(config.get("notify_user_environment_variable") or "TANTAN_SLACK_NOTIFY_USER_ID")
    webhook = _environment_value(webhook_name)
    if not webhook:
        return {"state": "NOT_CONFIGURED"}
    try:
        target = urllib.parse.urlsplit(webhook)
    except ValueError:
        return {"state": "CONFIG_INVALID"}
    if (
        target.scheme != "https"
        or target.hostname != "hooks.slack.com"
        or not target.path.startswith("/services/")
        or target.username
        or target.password
    ):
        return {"state": "CONFIG_INVALID"}
    body = json.dumps(
        {"text": _message(payload, notify_user_name)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = int(response.status)
            return {"state": "SENT" if status == 200 else "HTTP_FAILED", "http_status": status}
    except urllib.error.HTTPError as exc:
        return {"state": "HTTP_FAILED", "http_status": int(exc.code)}
    except Exception:
        return {"state": "UNKNOWN"}


def attempt_once(
    foundation: ExecutionFoundation,
    failed_path: Path,
    payload: dict[str, Any],
    notifier: Notifier = send_foundation_failure_slack,
    *,
    failed_receipt: dict[str, Any] | None = None,
    healer_settings: dict[str, Any] | None = None,
    standard_slack: bool = False,
) -> bool:
    """Persist send intent before the one allowed attempt; notification errors are non-authoritative."""
    marker_key = "foundation_failure_notification"
    with exclusive_lock(foundation.root / ".spool.lock"):
        failed = read_json(failed_path, {})
        marker = failed.get(marker_key) or {}
        if int(marker.get("attempt_count", 0) or 0) >= 1:
            return False
        body_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        failed[marker_key] = {
            "state": "INTENT_RECORDED",
            "attempt_count": 1,
            "attempted_at": iso_now(),
            "payload_sha256": body_hash,
        }
        atomic_write_json(failed_path, failed)

    notify_payload = dict(payload)
    if standard_slack and healer_eligible(payload, healer_settings):
        healer_marker = {
            "state": "INTENT_RECORDED",
            "attempt_count": 1,
            "attempted_at": iso_now(),
            "action_type": str(payload.get("action_type") or "UNKNOWN")[:120],
        }
        try:
            with exclusive_lock(foundation.root / ".spool.lock"):
                failed = read_json(failed_path, {})
                failed["foundation_failure_healer"] = healer_marker
                atomic_write_json(failed_path, failed)
        except Exception:
            healer_marker = {**healer_marker, "state": "INTENT_PERSIST_FAILED"}
        if healer_marker["state"] == "INTENT_RECORDED":
            try:
                healer_result = run_foundation_healer(payload, failed_receipt, healer_settings)
            except Exception:
                healer_result = {"state": "ADAPTER_FAILED", "gemini_call_count": 0, "airtable_write_count": 0}
            notify_payload["_foundation_healer"] = healer_result
            stored = {
                "state": str(healer_result.get("state") or "UNKNOWN")[:40],
                "attempt_count": 1,
                "completed_at": iso_now(),
                "gemini_call_count": min(1, max(0, int(healer_result.get("gemini_call_count", 0) or 0))),
                "airtable_write_count": min(1, max(0, int(healer_result.get("airtable_write_count", 0) or 0))),
            }
            if isinstance(healer_result.get("model"), str):
                stored["model"] = str(healer_result["model"])[:80]
            write = healer_result.get("airtable_write")
            if isinstance(write, dict):
                stored["airtable_write_state"] = str(write.get("state") or "UNKNOWN")[:40]
                if re.fullmatch(r"fp-[a-f0-9]{16}", str(write.get("fingerprint") or "")):
                    stored["fingerprint"] = str(write["fingerprint"])
            try:
                with exclusive_lock(foundation.root / ".spool.lock"):
                    failed = read_json(failed_path, {})
                    failed["foundation_failure_healer"] = stored
                    atomic_write_json(failed_path, failed)
            except Exception:
                pass

    try:
        result = notifier(notify_payload)
        delivery = dict(result) if isinstance(result, dict) else {"state": "SENT"}
        state = str(delivery.get("state") or "UNKNOWN")
        http_status = delivery.get("http_status")
    except Exception as exc:
        state = "FAILED"
        http_status = None
        delivery = {"error_type": type(exc).__name__}

    try:
        with exclusive_lock(foundation.root / ".spool.lock"):
            failed = read_json(failed_path, {})
            marker = dict(failed.get(marker_key) or {})
            marker["state"] = state[:40]
            marker["completed_at"] = iso_now()
            if isinstance(http_status, int):
                marker["http_status"] = http_status
            if delivery.get("error_type"):
                marker["error_type"] = str(delivery["error_type"])[:80]
            failed[marker_key] = marker
            atomic_write_json(failed_path, failed)
    except Exception:
        # INTENT_RECORDED is already durable, so replay remains at-most-once.
        pass
    return True
