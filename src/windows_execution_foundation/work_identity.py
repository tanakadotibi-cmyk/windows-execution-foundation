"""Mailbox v3 work identity normalization and deterministic keys."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


MAILBOX_V3 = "codex_local_job_mailbox_v3"
IDENTITY_FIELDS = (
    "project_id",
    "scope_key",
    "target_key",
    "execution_unit",
    "acceptance_version",
)
RESULT_METADATA_FIELDS = (
    "work_number",
    "work_identity_key",
    "display_name_ja",
    "stage_key",
    "completion_scope",
)


def _text(value: Any, code: str, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ValueError(code)
    return normalized


def normalize_work_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS):
        raise ValueError("WORK_IDENTITY_INVALID")
    return {field: _text(value.get(field), "WORK_IDENTITY_INVALID") for field in IDENTITY_FIELDS}


def work_identity_key(value: Any) -> tuple[str, dict[str, str]]:
    normalized = normalize_work_identity(value)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(("codex-local-work-identity-v1\n" + canonical).encode("utf-8")).hexdigest()
    return "wi-v1-" + digest, normalized


def verification_spec_hash(spec: Any) -> str:
    if not isinstance(spec, dict):
        raise ValueError("VERIFICATION_SPEC_INVALID")
    canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_v3_fields(request: dict[str, Any]) -> dict[str, Any]:
    key, normalized = work_identity_key(request.get("work_identity"))
    display_name = _text(request.get("display_name_ja"), "DISPLAY_NAME_JA_REQUIRED", 160)
    stage_key = _text(request.get("stage_key"), "STAGE_KEY_REQUIRED", 160)
    completion_scope = request.get("completion_scope")
    if completion_scope not in {"STEP", "FINAL"}:
        raise ValueError("COMPLETION_SCOPE_INVALID")
    resume = request.get("resume_of_action_id")
    if resume is not None:
        resume = _text(resume, "RESUME_OF_ACTION_ID_INVALID", 160)
    return {
        "work_identity_key": key,
        "work_identity": normalized,
        "display_name_ja": display_name,
        "stage_key": stage_key,
        "completion_scope": completion_scope,
        "verification_spec_hash": verification_spec_hash(request.get("verification_spec")),
        **({"resume_of_action_id": resume} if resume else {}),
    }


def result_metadata(source: dict[str, Any]) -> dict[str, Any]:
    result = {field: source[field] for field in RESULT_METADATA_FIELDS if source.get(field) is not None}
    for field in ("existing_action_id", "existing_work_number"):
        if source.get(field) is not None:
            result[field] = source[field]
    return result
