"""Fixed CODEX_LOCAL_JOB binding to the existing local Completion Core."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .airtable_local_exec import (
    LocalExecError,
    _load_preimplementation_ssot_fields,
    _patch,
    _preimplementation_gate_required,
    _request,
    iso_now,
    validate_codex_request,
    validate_verification_spec,
)
from .core import atomic_write_json, read_json, stable_key
from .result_safe_facts import project_result_safe_facts
from .work_identity import MAILBOX_V3, result_metadata, validate_v3_fields

TERMINAL = {"VERIFIED", "FAILED", "STOPPED", "VERIFY_FAILED"}
EXPECTED_DEVICE_ID = "tantan-mf-a1c6659aabef611c"
BRIDGE_FAILURE_EVIDENCE_NAME = "bridge-failure-evidence.json"
WORK_METADATA_KEYS = (
    "work_number", "work_identity_key", "display_name_ja", "stage_key", "completion_scope",
)
RESUME_CONTEXT_REASON = "RESUME_CONTEXT_READBACK_FAILED"
RESUME_CONTEXT_BOUNDARY = "RESUME_CONTEXT_READBACK"
RESUME_CONTEXT_TEXT_LIMIT = 2400
RESUME_CONTEXT_FIELDS = (
    "Action ID", "Request ID", "作業番号", "Local Exec State",
    "Local Exec Error Code", "Autonomous Evidence", "Terminal Evidence",
    "問題Fingerprint", "03｜問題", "03｜推奨修理",
)


def _safe_resume_text(value: object, limit: int = RESUME_CONTEXT_TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    text = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(token|secret|password|passwd|credential|cookie|authorization|api[_ -]?key)\b"
        r"(\s*[:=]\s*)[^\s,;\]\}\"']+", r"\1\2[REDACTED]", text,
    )
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?<!\d)(?:\+?81[- ]?|0)\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4}(?!\d)", "[REDACTED_PHONE]", text)
    text = text.replace("\x00", "")
    if len(text) > limit:
        text = text[:limit] + "...[TRUNCATED_AT_%d_CHARS]" % limit
    return text


def _first_failed_boundary(fields: dict) -> str:
    for field_name in ("Autonomous Evidence", "Terminal Evidence"):
        raw = fields.get(field_name)
        candidates = [raw]
        if isinstance(raw, str):
            try:
                candidates.insert(0, json.loads(raw))
            except (ValueError, TypeError):
                pass
        for candidate in candidates:
            if isinstance(candidate, dict):
                value = candidate.get("first_failed_boundary") or candidate.get("firstFailedBoundary")
                if value:
                    return _safe_resume_text(value, 240)
    return ""


def read_resume_context(airtable: dict, source_record_id: str) -> dict:
    """Read exactly one prior Next Actions row and expose only the fixed allowlist."""
    if not isinstance(source_record_id, str) or not re.fullmatch(r"rec[A-Za-z0-9]+", source_record_id):
        raise LocalExecError(RESUME_CONTEXT_REASON)
    try:
        # Airtable's retrieve-record route is already an exact record-id lookup.
        # `fields[]` belongs to list-records requests and makes this route fail
        # before a valid record can be read; keep the allowlist at extraction.
        record = _request(airtable, "GET", record_id=source_record_id)
    except (LocalExecError, ValueError, TypeError) as exc:
        raise LocalExecError(RESUME_CONTEXT_REASON) from exc
    if not isinstance(record, dict) or record.get("id") != source_record_id:
        raise LocalExecError(RESUME_CONTEXT_REASON)
    raw_fields = record.get("fields")
    if not isinstance(raw_fields, dict):
        raise LocalExecError(RESUME_CONTEXT_REASON)
    fields = {name: raw_fields.get(name) for name in RESUME_CONTEXT_FIELDS}
    state = _safe_resume_text(fields["Local Exec State"], 240)
    error = _safe_resume_text(fields["Local Exec Error Code"], 500)
    autonomous = _safe_resume_text(fields["Autonomous Evidence"])
    terminal = _safe_resume_text(fields["Terminal Evidence"])
    if not (error or autonomous or terminal):
        raise LocalExecError(RESUME_CONTEXT_REASON)
    healer_available = any(fields[name] not in (None, "", [], {}) for name in (
        "問題Fingerprint", "03｜問題", "03｜推奨修理"))
    return {
        "source_record_id": source_record_id,
        "prior_action_id": _safe_resume_text(fields["Action ID"], 240),
        "prior_request_id": _safe_resume_text(fields["Request ID"], 240),
        "prior_work_number": _safe_resume_text(fields["作業番号"], 240),
        "prior_local_exec_state": state,
        "prior_error_code": error,
        "first_failed_boundary": _first_failed_boundary(fields),
        "prior_autonomous_evidence": autonomous,
        "prior_terminal_evidence": terminal,
        "problem_fingerprint": _safe_resume_text(fields["問題Fingerprint"], 500),
        "healer_problem": _safe_resume_text(fields["03｜問題"]),
        "healer_recommended_repair": _safe_resume_text(fields["03｜推奨修理"]),
        "diagnosis_status": "DIAGNOSTIC_HYPOTHESIS_AVAILABLE" if healer_available else "HEALER_DIAGNOSIS_NOT_AVAILABLE",
    }


def compose_resume_instruction(context: dict, original_instruction: str) -> str:
    ordered = (
        "source_record_id", "prior_action_id", "prior_request_id", "prior_work_number",
        "prior_local_exec_state", "prior_error_code", "first_failed_boundary",
        "prior_autonomous_evidence", "prior_terminal_evidence", "problem_fingerprint",
        "healer_problem", "healer_recommended_repair", "diagnosis_status",
    )
    lines = ["PREVIOUS_FAILURE_CONTEXT_V1",
             "evidence_priority: PRIOR_FAILURE_EVIDENCE_OVER_DIAGNOSTIC_HYPOTHESIS",
             "healer_semantics: DIAGNOSTIC_HYPOTHESIS_NOT_CONFIRMED_FACT"]
    lines.extend(f"{key}: {json.dumps(context.get(key) or '', ensure_ascii=False)}" for key in ordered)
    lines.extend(("END_PREVIOUS_FAILURE_CONTEXT_V1", "", original_instruction))
    return "\n".join(lines)


def _resume_fail_closed_result(*, request: dict, request_hash: str, request_id: str,
                               job_id: str, lane: str, identity_contract: dict,
                               replay: bool) -> dict:
    return {
        "schema_version": "codex_local_job_result_v1", "lane_id": lane,
        "resource_scope": request.get("resource_scope", "global"),
        "resource_key": request.get("resource_key", "unknown"), "resource_release_safe": True,
        "request_id": request_id, "request_hash": request_hash,
        **({"work_number": request["work_number"]} if "work_number" in request else {}),
        **result_metadata({**request, **identity_contract}),
        "job_id": job_id, "terminal_status": "FAILED", "execution_status": "FAIL_CLOSED",
        "reason_code": RESUME_CONTEXT_REASON, "first_failed_boundary": RESUME_CONTEXT_BOUNDARY,
        "failure_evidence": {"source": "RESUME_PREFLIGHT", "verifier_eligible": False},
        "codex_launch_count": 0, "verifier_launch_count": 0, "replay": replay,
    }


def resolve_workdir(hint: str | None, settings: dict) -> Path:
    default = Path(settings["default_workdir"]).resolve(strict=True)
    candidate = Path(hint).resolve(strict=True) if hint else default
    if not candidate.is_dir():
        raise ValueError("WORKDIR_INVALID")
    roots = [Path(value).resolve(strict=True) for value in settings["approved_roots"]]
    if not any(os.path.commonpath((str(candidate), str(root))).casefold() == str(root).casefold() for root in roots):
        raise ValueError("WORKDIR_NOT_APPROVED")
    return candidate


def _project_failure_evidence(
    status_path: Path,
    status: dict,
    *,
    job_id: str,
    request_id: str,
    work_metadata: dict,
) -> dict | None:
    """Return a verified Bridge artifact locator, never a guessed relative path."""
    source = status.get("failure_evidence")
    if not isinstance(source, dict):
        return None
    identity = {"job_id": job_id, "request_id": request_id}
    expected_work = {
        key: work_metadata[key] for key in WORK_METADATA_KEYS
        if work_metadata.get(key) not in (None, "")
    }
    identity.update({
        key: expected_work[key] for key in (
            "work_number", "work_identity_key", "stage_key", "completion_scope",
        ) if key in expected_work
    })

    def unknown(reason: str) -> dict:
        return {
            "path": "UNKNOWN", "sha256": "UNKNOWN",
            "source": "BRIDGE" if source.get("source") == "BRIDGE" else "UNKNOWN",
            "verifier_eligible": source.get("verifier_eligible")
            if isinstance(source.get("verifier_eligible"), bool) else False,
            "locator_status": reason,
            **identity,
        }

    if source.get("source") != "BRIDGE" or source.get("path") != BRIDGE_FAILURE_EVIDENCE_NAME:
        return unknown("UNRESOLVED")
    if status.get("job_id") != job_id or status.get("request_id") != request_id:
        return unknown("IDENTITY_MISMATCH")
    if expected_work:
        frozen_work = status.get("work_metadata")
        if not isinstance(frozen_work, dict) or any(frozen_work.get(key) != value for key, value in expected_work.items()):
            return unknown("IDENTITY_MISMATCH")
    try:
        jobdir = status_path.parent.resolve(strict=True)
        resolved_status = status_path.resolve(strict=True)
        if resolved_status.parent != jobdir or jobdir.name != job_id:
            return unknown("UNRESOLVED")
        artifact_path = (jobdir / BRIDGE_FAILURE_EVIDENCE_NAME).resolve(strict=True)
        if artifact_path.parent != jobdir or not artifact_path.is_file():
            return unknown("UNRESOLVED")
        artifact_bytes = artifact_path.read_bytes()
    except FileNotFoundError:
        return unknown("ARTIFACT_MISSING")
    except PermissionError:
        return unknown("PERMISSION_DENIED")
    except OSError:
        return unknown("IO_UNAVAILABLE")
    actual_hash = hashlib.sha256(artifact_bytes).hexdigest()
    expected_hash = source.get("sha256")
    if expected_hash is not None and (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash)
            or expected_hash.lower() != actual_hash):
        return unknown("HASH_MISMATCH")
    try:
        artifact = json.loads(artifact_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return unknown("ARTIFACT_INVALID")
    first_failure = status.get("first_failure") if isinstance(status.get("first_failure"), dict) else {}
    expected_reason = first_failure.get("reason_code") or status.get("reason_code")
    expected_boundary = status.get("first_failed_boundary")
    expected_execution = status.get("execution_status")
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != "bridge_failure_evidence_v1"
        or artifact.get("source") != "BRIDGE"
        or artifact.get("job_id") != job_id
        or artifact.get("request_id") != request_id
        or artifact.get("reason_code") != expected_reason
        or artifact.get("first_failed_boundary") != expected_boundary
        or artifact.get("execution_status") != expected_execution
    ):
        return unknown("IDENTITY_MISMATCH")
    return {
        "path": str(artifact_path), "sha256": actual_hash, "source": "BRIDGE",
        "verifier_eligible": source.get("verifier_eligible") is True,
        "locator_status": "CONFIRMED", **identity,
    }


def execute(config_path: Path, record_id: str, request_id: str, output: Path, lane: str = "A") -> dict:
    from .lanes import load_config, resource_descriptor
    foundation_config = load_config(config_path, lane)
    airtable = foundation_config["local_exec_airtable"]
    settings = foundation_config["codex_local_job"]
    if airtable["device_id"] != EXPECTED_DEVICE_ID:
        raise ValueError("ROUTE_MISMATCH")
    record = _request(airtable, "GET", record_id=record_id, params={"returnFieldsByFieldId": "true"})
    fields = record.get("fields") or {}
    mapping = airtable["fields"]
    route = (
        fields.get(mapping["schema"]) == "EF_v0.1"
        and fields.get(mapping["requested_host"]) == EXPECTED_DEVICE_ID
        and fields.get(mapping["action_type"]) == "CODEX_LOCAL_JOB"
        and fields.get(mapping["state"]) in {"EF_QUEUED", "EF_RUNNING"}
        and fields.get(mapping["request_id"]) == request_id
    )
    if not route:
        raise ValueError("REQUEST_NOT_CURRENT")
    job_id = "codex-local-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    bridge = Path(settings["bridge_root"]).resolve(strict=True)
    status_path = bridge / "runtime" / "jobs" / job_id / "status.json"
    prior = read_json(status_path)
    try:
        gate_ssot_fields = None
        if _preimplementation_gate_required(
            airtable, record.get("createdTime"), replay=bool(prior and prior.get("status") in TERMINAL),
        ):
            gate_ssot_fields = _load_preimplementation_ssot_fields(airtable, record_id)
        request, request_hash = validate_codex_request(
            airtable, fields, request_id,
            replay=bool(prior and prior.get("status") in TERMINAL),
            source_created_time=record.get("createdTime"),
            gate_ssot_fields=gate_ssot_fields,
        )
    except LocalExecError as exc:
        if str(exc) in {"WORK_NUMBER_REQUIRED", "WORK_NUMBER_INVALID", "WORK_NUMBER_MISMATCH"}:
            _patch(airtable, record_id, {
                mapping["state"]: "EF_REJECTED",
                mapping["finished"]: iso_now(),
                mapping["resolved_host"]: airtable["device_id"],
                mapping["heartbeat"]: iso_now(),
                mapping["error_code"]: str(exc),
            })
        raise
    key_hash = stable_key(request_id)
    state_root = Path(foundation_config["state_dir"])
    spooled = read_json(state_root / "spool" / "inflight" / f"{key_hash}.json")
    if not spooled:
        spooled = read_json(state_root / "spool" / "pending" / f"{key_hash}.json")
    metadata = (spooled or {}).get("metadata") or {}
    selection_keys = (
        "selected_lane", "selection_reason", "a_health", "b_health",
        "a_busy", "b_busy", "resource_conflict", "explicit_or_auto",
    )
    lane_selection = {key: metadata.get(key) for key in selection_keys}
    if request.get("lane") == "AUTO" and (
            lane_selection.get("selected_lane") != lane
            or lane_selection.get("explicit_or_auto") != "AUTO"
            or not lane_selection.get("selection_reason")):
        raise ValueError("AUTO_LANE_SELECTION_REQUIRED")
    if metadata.get("work_number") != request.get("work_number"):
        _patch(airtable, record_id, {
            mapping["state"]: "EF_REJECTED",
            mapping["finished"]: iso_now(),
            mapping["resolved_host"]: airtable["device_id"],
            mapping["heartbeat"]: iso_now(),
            mapping["error_code"]: "WORK_NUMBER_MISMATCH",
        })
        raise LocalExecError("WORK_NUMBER_MISMATCH")
    if metadata.get("airtable_record_id") != record_id or metadata.get("request_hash") != request_hash:
        raise ValueError("REQUEST_INPUT_CHANGED")
    try:
        envelope = json.loads(str(fields.get(mapping["action_json"]) or ""))
    except (ValueError, TypeError):
        envelope = {}
    identity_contract = validate_v3_fields(request) if envelope.get("schema_version") == MAILBOX_V3 else {}
    if identity_contract:
        expected_metadata = result_metadata({**request, **identity_contract})
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            raise ValueError("WORK_IDENTITY_INPUT_CHANGED")
        if metadata.get("verification_spec_hash") != identity_contract["verification_spec_hash"]:
            raise ValueError("WORK_IDENTITY_SPEC_CONFLICT")
    if foundation_config.get("shared_resource_dir"):
        legacy_unknown = not metadata.get("resource") and "resource_scope" not in request and lane == "A"
        if not legacy_unknown and metadata.get("resource") != resource_descriptor(request, settings):
            raise ValueError("RESOURCE_INPUT_CHANGED")
        lease = (spooled or {}).get("resource_lease") or {}
        lease_path = Path(foundation_config["shared_resource_dir"]) / "active" / (str(lease.get("token", "MISSING")) + ".json")
        if not lease or lease.get("lane_id") != lane or lease.get("request_id") != request_id or read_json(lease_path) != lease:
            raise ValueError("RESOURCE_LEASE_REQUIRED")
    workdir = resolve_workdir(request.get("workdir_hint"), settings)
    model = request.get("model", settings["default_model"])
    reasoning = request.get("reasoning", settings["default_reasoning"])
    if model not in settings["approved_models"] or reasoning not in settings["approved_reasoning"]:
        if status_path.exists():
            # Prior producer state may own live or unconfirmed descendants.
            raise ValueError("MODEL_SETTING_NOT_APPROVED")
        # Validation rejected before any managed child launch. Keep approval
        # fail-closed and persist terminal evidence through the accepted path.
        result = _resume_fail_closed_result(
            request=request, request_hash=request_hash, request_id=request_id,
            job_id=job_id, lane=lane, identity_contract=identity_contract,
            replay=bool(prior),
        )
        result.update(lane_selection)
        result.update({
            "reason_code": "MODEL_SETTING_NOT_APPROVED",
            "first_failed_boundary": "MODEL_APPROVAL_PREFLIGHT",
            "failure_evidence": {"source": "MODEL_APPROVAL_PREFLIGHT", "verifier_eligible": False},
        })
        atomic_write_json(output, result)
        return result
    # Direct/legacy callers cross the same pure boundary as mailbox intake.
    # This returns a normalized deep copy, so the validated object is exactly
    # the object serialized for the Completion Bridge.
    spec = validate_verification_spec(request["verification_spec"])
    task_instruction = request["task_instruction"]
    if request.get("resume_of_action_id"):
        try:
            resume_context = read_resume_context(airtable, request["resume_of_action_id"])
        except LocalExecError:
            result = _resume_fail_closed_result(
                request=request, request_hash=request_hash, request_id=request_id,
                job_id=job_id, lane=lane, identity_contract=identity_contract,
                replay=bool(prior),
            )
            result.update(lane_selection)
            atomic_write_json(output, result)
            try:
                failed_at = iso_now()
                _patch(airtable, record_id, {
                    mapping["state"]: "EF_FAILED", mapping["finished"]: failed_at,
                    mapping["resolved_host"]: airtable["device_id"], mapping["heartbeat"]: failed_at,
                    mapping["error_code"]: RESUME_CONTEXT_REASON,
                    **({mapping["evidence"]: json.dumps(result, ensure_ascii=False, sort_keys=True)}
                       if mapping.get("evidence") else {}),
                })
            except LocalExecError:
                # The durable local receipt prevents a blind retry; normal
                # result sync can perform the Airtable write on its next poll.
                pass
            return result
        task_instruction = compose_resume_instruction(resume_context, task_instruction)
    input_root = Path(foundation_config["state_dir"]) / "local-exec-v01"
    input_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-local-input-", dir=input_root) as temporary:
        prompt_file = Path(temporary) / "prompt.txt"
        spec_file = Path(temporary) / "verification-spec.json"
        prompt_file.write_text(task_instruction, encoding="utf-8")
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-File",
            str(bridge / "Run-CodexManagedJob.ps1"),
            "-JobId", job_id, "-Workdir", str(workdir), "-PromptFile", str(prompt_file),
            "-TaskName", request["task_name"], "-VerificationSpec", str(spec_file),
            "-Model", model, "-Reasoning", reasoning,
            "-CodexTimeout", str(settings["codex_timeout_seconds"]),
            "-VerifierTimeout", str(settings["verifier_timeout_seconds"]),
        ]
        environment = os.environ.copy()
        environment["TANTAN_COMPLETION_HOST_ID"] = "TANTAN-M720S"
        environment["TANTAN_EXECUTION_HOST_DEVICE_ID"] = airtable["device_id"]
        environment["TANTAN_COMPLETION_REQUEST_ID"] = request_id
        environment["TANTAN_EXECUTION_LANE"] = lane
        if identity_contract:
            environment["TANTAN_WORK_NUMBER"] = request["work_number"]
            environment["TANTAN_WORK_IDENTITY_KEY"] = identity_contract["work_identity_key"]
            environment["TANTAN_WORK_DISPLAY_NAME_JA"] = identity_contract["display_name_ja"]
            environment["TANTAN_WORK_STAGE_KEY"] = identity_contract["stage_key"]
            environment["TANTAN_COMPLETION_SCOPE"] = identity_contract["completion_scope"]
        if fields.get(mapping["state"]) == "EF_QUEUED":
            started = iso_now()
            _patch(
                airtable,
                record_id,
                {
                    mapping["state"]: "EF_RUNNING",
                    mapping["resolved_host"]: airtable["device_id"],
                    mapping["started"]: fields.get(mapping["started"]) or started,
                    mapping["heartbeat"]: started,
                    mapping["error_code"]: "",
                },
            )
        completed = subprocess.run(command, cwd=bridge, env=environment, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=settings["run_timeout_seconds"], check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        invocation = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("COMPLETION_INVOCATION_INVALID") from exc
    if invocation.get("ok") is not True or invocation.get("job_id") != job_id:
        raise RuntimeError("COMPLETION_INVOCATION_REJECTED")
    status = read_json(status_path)
    if not isinstance(status, dict) or status.get("status") not in TERMINAL:
        raise RuntimeError("COMPLETION_TERMINAL_MISSING")
    notification = status.get("notification") or {}
    projected_identity = {
        **({"work_number": request["work_number"]} if "work_number" in request else {}),
        **result_metadata({**request, **identity_contract}),
    }
    failure_evidence = _project_failure_evidence(
        status_path, status, job_id=job_id, request_id=request_id,
        work_metadata=projected_identity,
    )
    result_safe_facts = project_result_safe_facts(
        status_path,
        status,
        workdir=workdir,
        current_identity={
            "request_id": request_id,
            "job_id": job_id,
            "work_number": projected_identity.get("work_number"),
            "work_identity_key": projected_identity.get("work_identity_key"),
        },
    )
    result = {
        "schema_version": "codex_local_job_result_v1",
        "lane_id": lane,
        "resource_scope": request.get("resource_scope", "global"),
        "resource_key": request.get("resource_key", "unknown"),
        "resource_release_safe": all(
            not child.get("launch_count") or (
                (child.get("exit_code") is not None or child.get("spawn_failed") or child.get("termination_confirmed") is True)
                and (not child.get("timed_out") or child.get("termination_confirmed") is True)
                and (not child.get("output_limited") or child.get("termination_confirmed") is True)
            ) for child in (status.get("codex") or {}, status.get("verifier") or {})),
        "request_id": request_id,
        "request_hash": request_hash,
        **lane_selection,
        **projected_identity,
        "job_id": job_id,
        "terminal_status": status["status"],
        "reason_code": status.get("reason_code"),
        **{key: status[key] for key in (
            "execution_status", "first_failed_boundary", "verification_reason",
            "secondary_reason",
        ) if key in status},
        **({"failure_evidence": failure_evidence} if failure_evidence is not None else {}),
        **({"result_safe_facts": result_safe_facts} if result_safe_facts is not None else {}),
        "codex_launch_count": (status.get("codex") or {}).get("launch_count"),
        "verifier_launch_count": (status.get("verifier") or {}).get("launch_count"),
        "slack_state": notification.get("state"),
        "slack_attempt_count": notification.get("attempt_count"),
        "slack_http_status": notification.get("http_status"),
        "replay": bool(prior),
    }
    atomic_write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--record-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lane", choices=("A", "B"), default="A")
    args = parser.parse_args()
    execute(args.config, args.record_id, args.request_id, args.output, args.lane)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
