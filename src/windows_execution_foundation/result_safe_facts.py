"""Bounded, typed result facts carried by the existing local-job receipt.

The Completion Bridge terminal declaration remains the exact legacy six-key
contract.  This module projects only Bridge-selected product artifacts and
never follows locators contained inside an artifact.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any


INPUT_SCHEMA = "codex_result_safe_facts_input_v1"
OUTPUT_SCHEMA = "codex_result_safe_facts_v1"
MAX_PROJECTION_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024
MAX_STRING = 512
MAX_ARRAY = 32
UNKNOWN = "UNKNOWN"
REDACTED = "REDACTED"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}")
_JSON_PATH = re.compile(r"\$(?:\.[A-Za-z0-9_-]+|\[[0-9]{1,9}\]){0,31}")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}")
_MARKER = re.compile(r"[A-Z][A-Z0-9_]{0,159}")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:secret|token|password|credential|bearer|api[_-]?key|private[_-]?key|"
    r"raw[_-]?(?:stdout|stderr|command)|authorization)"
)
_RELATIONSHIPS = {"CURRENT", "UPSTREAM", UNKNOWN}
_READ_STATUSES = {"READ_OK", "READ_FAILED", "NOT_READ", "CONFLICT", UNKNOWN, REDACTED}
_PROJECTION_STATUSES = {"OK", "PARTIAL", REDACTED, "TRUNCATED", "REJECTED"}
_IDENTITY_STATUSES = {"SAME_IDENTITY", "DISTINCT_SOURCE", "CONFLICT", UNKNOWN}
_CONFLICT_STATUSES = {"NONE", "CONFLICT", UNKNOWN}
_LOCATOR_STATUSES = {
    "CONFIRMED", "BRIDGE_EXISTENCE_CONFIRMED", "MISSING", "HASH_MISMATCH",
    "UNAVAILABLE", "REJECTED",
}
_PROVENANCE = {
    "BRIDGE_FROZEN_ARTIFACT", "BRIDGE_FAILURE_METADATA", "TERMINAL_ONLY", "MIXED",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _marker(markers: list[str], value: str) -> None:
    if value not in markers and len(markers) < MAX_ARRAY:
        markers.append(value)


def _safe_identifier(value: Any, markers: list[str], field: str) -> str:
    if value in (None, ""):
        return UNKNOWN
    if not isinstance(value, str):
        _marker(markers, f"{field}_TYPE_REJECTED")
        return UNKNOWN
    if value in {UNKNOWN, REDACTED}:
        return value
    if len(value) > MAX_STRING or not _IDENTIFIER.fullmatch(value) or _SENSITIVE_TEXT.search(value):
        _marker(markers, f"{field}_REDACTED")
        return REDACTED
    return value


def _safe_sha(value: Any, markers: list[str], field: str) -> str:
    if value in (None, "", UNKNOWN):
        return UNKNOWN
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _marker(markers, f"{field}_REJECTED")
        return REDACTED
    return value.lower()


def _safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_STRING or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))).casefold() == str(root).casefold()
    except ValueError:
        return False


def _identity(values: Any, markers: list[str], prefix: str) -> dict[str, str]:
    source = values if isinstance(values, dict) else {}
    allowed = {"request_id", "job_id", "work_number", "work_identity_key"}
    if not isinstance(values, dict) and values is not None:
        _marker(markers, f"{prefix}_TYPE_REJECTED")
    if set(source) - allowed:
        _marker(markers, f"{prefix}_KEY_REJECTED")
    return {
        key: _safe_identifier(source.get(key), markers, f"{prefix}_{key}".upper())
        for key in ("request_id", "job_id", "work_number", "work_identity_key")
    }


def _value_descriptor(value: Any, markers: list[str], prefix: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        _marker(markers, f"{prefix}_REJECTED")
        return {"kind": REDACTED, "value": REDACTED}
    kind = value.get("kind")
    if kind == "SHA256":
        safe = _safe_sha(value.get("value"), markers, prefix)
        return {"kind": "SHA256" if safe not in {UNKNOWN, REDACTED} else REDACTED, "value": safe}
    if kind == "IDENTIFIER":
        safe = _safe_identifier(value.get("value"), markers, prefix)
        return {"kind": "IDENTIFIER" if safe not in {UNKNOWN, REDACTED} else REDACTED, "value": safe}
    if kind == UNKNOWN and value.get("value") == UNKNOWN:
        return {"kind": UNKNOWN, "value": UNKNOWN}
    _marker(markers, f"{prefix}_REJECTED")
    return {"kind": REDACTED, "value": REDACTED}


def _safe_input(raw: Any) -> tuple[dict[str, Any], list[str], str]:
    markers: list[str] = []
    empty = {
        "source_identity": _identity({}, markers, "SOURCE_IDENTITY"),
        "identity_relationship": UNKNOWN,
        "conflicts": [],
        "input_read": {
            "status": UNKNOWN, "expected_sha256": UNKNOWN, "computed_sha256": UNKNOWN,
            "byte_size": UNKNOWN, "elapsed_seconds": UNKNOWN,
        },
    }
    if raw is None:
        return empty, ["SAFE_FACTS_INPUT_ABSENT"], "PARTIAL"
    try:
        if len(canonical_bytes(raw)) > MAX_PROJECTION_BYTES:
            return empty, ["SAFE_FACTS_INPUT_TOO_LARGE"], "REJECTED"
    except (TypeError, ValueError):
        return empty, ["SAFE_FACTS_INPUT_INVALID"], "REJECTED"
    allowed = {"schema_version", "source_identity", "identity_relationship", "conflicts", "input_read"}
    if not isinstance(raw, dict) or set(raw) - allowed or raw.get("schema_version") != INPUT_SCHEMA:
        return empty, ["SAFE_FACTS_SCHEMA_REJECTED"], "REJECTED"
    if any(_SENSITIVE_TEXT.search(str(key)) for key in raw):
        return empty, ["SAFE_FACTS_FORBIDDEN_KEY"], "REJECTED"

    source_identity = _identity(raw.get("source_identity"), markers, "SOURCE_IDENTITY")
    relationship = raw.get("identity_relationship", UNKNOWN)
    if relationship not in _RELATIONSHIPS:
        _marker(markers, "IDENTITY_RELATIONSHIP_REJECTED")
        relationship = UNKNOWN

    conflicts: list[dict[str, Any]] = []
    raw_conflicts = raw.get("conflicts", [])
    if not isinstance(raw_conflicts, list):
        _marker(markers, "CONFLICTS_TYPE_REJECTED")
    else:
        if len(raw_conflicts) > MAX_ARRAY:
            _marker(markers, "CONFLICTS_TRUNCATED")
        for index, item in enumerate(raw_conflicts[:MAX_ARRAY]):
            if not isinstance(item, dict) or set(item) != {"json_path", "expected", "observed"}:
                _marker(markers, "CONFLICT_ITEM_REJECTED")
                continue
            json_path = item.get("json_path")
            if not isinstance(json_path, str) or len(json_path) > MAX_STRING or not _JSON_PATH.fullmatch(json_path):
                _marker(markers, "CONFLICT_PATH_REJECTED")
                continue
            conflicts.append({
                "json_path": json_path,
                "expected": _value_descriptor(item.get("expected"), markers, f"EXPECTED_{index}"),
                "observed": _value_descriptor(item.get("observed"), markers, f"OBSERVED_{index}"),
            })

    read = raw.get("input_read")
    allowed_read = {"status", "expected_sha256", "computed_sha256", "byte_size", "elapsed_seconds"}
    if not isinstance(read, dict) or set(read) - allowed_read:
        _marker(markers, "INPUT_READ_REJECTED")
        read = {}
    read_status = read.get("status", UNKNOWN)
    if read_status not in _READ_STATUSES:
        _marker(markers, "INPUT_READ_STATUS_REJECTED")
        read_status = UNKNOWN
    byte_size = read.get("byte_size", UNKNOWN)
    if not (byte_size == UNKNOWN or isinstance(byte_size, int) and not isinstance(byte_size, bool) and 0 <= byte_size <= 2**63 - 1):
        _marker(markers, "INPUT_READ_BYTE_SIZE_REJECTED")
        byte_size = UNKNOWN
    elapsed = read.get("elapsed_seconds", UNKNOWN)
    if not (elapsed == UNKNOWN or isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            and math.isfinite(elapsed) and 0 <= elapsed <= 10**9):
        _marker(markers, "INPUT_READ_ELAPSED_REJECTED")
        elapsed = UNKNOWN
    input_read = {
        "status": read_status,
        "expected_sha256": _safe_sha(read.get("expected_sha256"), markers, "EXPECTED_SHA256"),
        "computed_sha256": _safe_sha(read.get("computed_sha256"), markers, "COMPUTED_SHA256"),
        "byte_size": byte_size,
        "elapsed_seconds": elapsed,
    }
    status = "OK"
    if any(value.endswith("TRUNCATED") for value in markers):
        status = "TRUNCATED"
    elif markers:
        status = REDACTED
    return {
        "source_identity": source_identity,
        "identity_relationship": relationship,
        "conflicts": conflicts,
        "input_read": input_read,
    }, markers, status


def _terminal_result(status: dict[str, Any]) -> dict[str, Any] | None:
    value = (status.get("codex") or {}).get("terminal_result")
    keys = {
        "execution_status", "reason_code", "first_failed_boundary",
        "evidence_created", "produced_files", "retry_requested",
    }
    if not isinstance(value, dict) or set(value) != keys or not isinstance(value.get("evidence_created"), bool):
        return None
    if value.get("retry_requested") is not False or not isinstance(value.get("produced_files"), list):
        return None
    if len(value["produced_files"]) > 128 or any(_safe_relative(item) is None for item in value["produced_files"]):
        return None
    return value


def _file_entry(relative: str, content: bytes | None, locator_status: str, provenance: str) -> dict[str, Any]:
    return {
        "locator": relative,
        "sha256": hashlib.sha256(content).hexdigest() if content is not None else UNKNOWN,
        "size_bytes": len(content) if content is not None else UNKNOWN,
        "locator_status": locator_status,
        "provenance": provenance,
    }


def _frozen_artifacts(status_path: Path, status: dict[str, Any], produced: list[str], markers: list[str]) -> dict[str, bytes]:
    jobdir = status_path.parent.resolve()
    evidence_root = (jobdir / "evidence").resolve()
    manifest_path = jobdir / "reader-manifest.json"
    try:
        if manifest_path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("manifest size")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _marker(markers, "BRIDGE_MANIFEST_UNAVAILABLE")
        return {}
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("job_id") != status.get("job_id"):
        _marker(markers, "BRIDGE_MANIFEST_REJECTED")
        return {}
    artifacts = manifest.get("artifacts")
    observed = manifest.get("evidence")
    status_observed = status.get("evidence")
    if not isinstance(artifacts, list) or not isinstance(observed, list) or not isinstance(status_observed, list):
        _marker(markers, "BRIDGE_MANIFEST_REJECTED")
        return {}
    manifest_hashes = {item.get("path"): item.get("sha256") for item in observed if isinstance(item, dict)}
    status_hashes = {item.get("path"): item.get("sha256") for item in status_observed if isinstance(item, dict)}
    contents: dict[str, bytes] = {}
    by_source = {item.get("source_path"): item for item in artifacts if isinstance(item, dict)}
    for relative in produced[:MAX_ARRAY]:
        item = by_source.get(relative)
        expected_snapshot = f"artifacts/{relative}"
        if not isinstance(item, dict) or item.get("snapshot_path") != expected_snapshot:
            continue
        safe_snapshot = _safe_relative(item.get("snapshot_path"))
        expected_hash = manifest_hashes.get(expected_snapshot)
        if safe_snapshot is None or expected_hash != status_hashes.get(expected_snapshot) or not _SHA256.fullmatch(str(expected_hash or "")):
            continue
        try:
            path = (evidence_root / Path(*PurePosixPath(safe_snapshot).parts)).resolve(strict=True)
            if not _inside(evidence_root, path) or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            content = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(content).hexdigest() == expected_hash.lower():
            contents[relative] = content
    return contents


def _failure_artifacts(status_path: Path, status: dict[str, Any], workdir: Path,
                       produced: list[str], markers: list[str]) -> dict[str, bytes]:
    source = status.get("failure_evidence")
    if not isinstance(source, dict) or source.get("source") != "BRIDGE" or source.get("path") != "bridge-failure-evidence.json":
        _marker(markers, "BRIDGE_FAILURE_METADATA_UNAVAILABLE")
        return {}
    try:
        failure_path = (status_path.parent.resolve() / "bridge-failure-evidence.json").resolve(strict=True)
        if failure_path.parent != status_path.parent.resolve() or failure_path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("failure evidence")
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _marker(markers, "BRIDGE_FAILURE_METADATA_REJECTED")
        return {}
    if (not isinstance(failure, dict) or failure.get("schema_version") != "bridge_failure_evidence_v1"
            or failure.get("job_id") != status.get("job_id") or failure.get("request_id") != status.get("request_id")):
        _marker(markers, "BRIDGE_FAILURE_IDENTITY_REJECTED")
        return {}
    confirmed = set(failure.get("produced_files") or [])
    root = workdir.resolve()
    contents: dict[str, bytes] = {}
    for relative in produced[:MAX_ARRAY]:
        if relative not in confirmed:
            continue
        try:
            path = (root / Path(*PurePosixPath(relative).parts)).resolve(strict=True)
            if not _inside(root, path) or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            contents[relative] = path.read_bytes()
        except OSError:
            continue
    return contents


def _finalize(data: dict[str, Any], projection_status: str, markers: list[str]) -> dict[str, Any]:
    data["markers"] = list(dict.fromkeys(markers))[:MAX_ARRAY]
    while True:
        payload = canonical_bytes(data)
        projection = {
            "schema_version": OUTPUT_SCHEMA,
            "projection_status": projection_status,
            "canonical_scope": "data",
            "canonical_sha256": hashlib.sha256(payload).hexdigest(),
            "canonical_bytes": len(payload),
            "data": data,
        }
        if len(canonical_bytes(projection)) <= MAX_PROJECTION_BYTES:
            return projection
        if data["produced_files"]:
            data["produced_files"].pop()
        elif data["conflicts"]:
            data["conflicts"].pop()
        else:
            data = {
                "current_identity": {key: UNKNOWN for key in ("request_id", "job_id", "work_number", "work_identity_key")},
                "source_identity": {key: UNKNOWN for key in ("request_id", "job_id", "work_number", "work_identity_key")},
                "identity_relationship": UNKNOWN, "identity_status": UNKNOWN,
                "conflict_status": UNKNOWN, "conflicts": [],
                "input_read": {"status": UNKNOWN, "expected_sha256": UNKNOWN, "computed_sha256": UNKNOWN,
                               "byte_size": UNKNOWN, "elapsed_seconds": UNKNOWN},
                "evidence_created": {"status": UNKNOWN, "value": UNKNOWN},
                "produced_files": [], "provenance": "TERMINAL_ONLY",
                "markers": ["OUTPUT_SIZE_REJECTED"],
            }
            projection_status = "REJECTED"
            continue
        _marker(data["markers"], "OUTPUT_TRUNCATED")
        if projection_status != "REJECTED":
            projection_status = "TRUNCATED"


def project_result_safe_facts(status_path: Path, status: dict[str, Any], *, workdir: Path,
                              current_identity: dict[str, Any]) -> dict[str, Any] | None:
    """Project safe facts from the Bridge status and its bounded artifact route."""
    terminal = _terminal_result(status)
    if terminal is None:
        return None
    markers: list[str] = []
    produced = [_safe_relative(item) for item in terminal["produced_files"]]
    produced = [item for item in produced if item is not None]
    if len(produced) > MAX_ARRAY:
        _marker(markers, "PRODUCED_FILES_TRUNCATED")
    selected = produced[:MAX_ARRAY]
    verified = status.get("status") == "VERIFIED"
    contents = _frozen_artifacts(status_path, status, selected, markers) if verified else _failure_artifacts(
        status_path, status, workdir, selected, markers,
    )
    item_provenance = "BRIDGE_FROZEN_ARTIFACT" if verified else "BRIDGE_FAILURE_METADATA"
    files = [
        _file_entry(
            relative, contents.get(relative),
            "CONFIRMED" if verified and relative in contents else
            "BRIDGE_EXISTENCE_CONFIRMED" if relative in contents else "UNAVAILABLE",
            item_provenance if relative in contents else "TERMINAL_ONLY",
        )
        for relative in selected
    ]

    candidates: list[Any] = []
    for relative, content in contents.items():
        if not relative.lower().endswith(".json"):
            continue
        try:
            document = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        if isinstance(document, dict) and "result_safe_facts" in document:
            candidates.append(document["result_safe_facts"])
    if len(candidates) > 1:
        safe_input, input_markers, projection_status = _safe_input(None)
        input_markers = ["MULTIPLE_SAFE_FACTS_INPUTS_REJECTED"]
        projection_status = "REJECTED"
    else:
        safe_input, input_markers, projection_status = _safe_input(candidates[0] if candidates else None)
    markers.extend(input_markers)
    if any(value.endswith("TRUNCATED") for value in markers) and projection_status != "REJECTED":
        projection_status = "TRUNCATED"
    elif markers and projection_status == "OK":
        projection_status = "PARTIAL"

    current = _identity(current_identity, markers, "CURRENT_IDENTITY")
    source = safe_input["source_identity"]
    relationship = safe_input["identity_relationship"]
    comparable = [key for key in current if source[key] not in {UNKNOWN, REDACTED} and current[key] not in {UNKNOWN, REDACTED}]
    mismatch = any(source[key] != current[key] for key in comparable)
    if relationship == "CURRENT":
        identity_status = "CONFLICT" if mismatch else "SAME_IDENTITY" if comparable else UNKNOWN
    elif relationship == "UPSTREAM":
        identity_status = "DISTINCT_SOURCE" if mismatch else "SAME_IDENTITY" if comparable else UNKNOWN
    else:
        identity_status = UNKNOWN
    conflicts = safe_input["conflicts"]
    conflict_status = "CONFLICT" if conflicts else "NONE" if candidates else UNKNOWN
    provenance_values = {item["provenance"] for item in files}
    provenance = next(iter(provenance_values)) if len(provenance_values) == 1 else "MIXED" if provenance_values else "TERMINAL_ONLY"
    data = {
        "current_identity": current,
        "source_identity": source,
        "identity_relationship": relationship,
        "identity_status": identity_status,
        "conflict_status": conflict_status,
        "conflicts": conflicts,
        "input_read": safe_input["input_read"],
        "evidence_created": {"status": "KNOWN", "value": terminal["evidence_created"]},
        "produced_files": files,
        "provenance": provenance,
        "markers": [],
    }
    return _finalize(data, projection_status, markers)


def _projection_shape_valid(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "projection_status", "canonical_scope", "canonical_sha256",
        "canonical_bytes", "data",
    }:
        return False
    if value.get("schema_version") != OUTPUT_SCHEMA or value.get("projection_status") not in _PROJECTION_STATUSES or value.get("canonical_scope") != "data":
        return False
    if not _SHA256.fullmatch(str(value.get("canonical_sha256") or "")) or not isinstance(value.get("canonical_bytes"), int):
        return False
    data = value.get("data")
    if not isinstance(data, dict) or set(data) != {
        "current_identity", "source_identity", "identity_relationship", "identity_status",
        "conflict_status", "conflicts", "input_read", "evidence_created", "produced_files",
        "provenance", "markers",
    }:
        return False
    for name in ("current_identity", "source_identity"):
        identity = data.get(name)
        if not isinstance(identity, dict) or set(identity) != {"request_id", "job_id", "work_number", "work_identity_key"}:
            return False
        if any(not isinstance(item, str) or len(item) > MAX_STRING or (item not in {UNKNOWN, REDACTED} and not _IDENTIFIER.fullmatch(item)) for item in identity.values()):
            return False
    if data.get("identity_relationship") not in _RELATIONSHIPS or data.get("identity_status") not in _IDENTITY_STATUSES or data.get("conflict_status") not in _CONFLICT_STATUSES:
        return False
    if data.get("provenance") not in _PROVENANCE:
        return False
    conflicts = data.get("conflicts")
    files = data.get("produced_files")
    markers = data.get("markers")
    if not isinstance(conflicts, list) or len(conflicts) > MAX_ARRAY or not isinstance(files, list) or len(files) > MAX_ARRAY or not isinstance(markers, list) or len(markers) > MAX_ARRAY:
        return False
    if any(not isinstance(item, str) or not _MARKER.fullmatch(item) for item in markers):
        return False
    for item in conflicts:
        if not isinstance(item, dict) or set(item) != {"json_path", "expected", "observed"} or not _JSON_PATH.fullmatch(str(item.get("json_path") or "")):
            return False
        for side in ("expected", "observed"):
            descriptor = item.get(side)
            if not isinstance(descriptor, dict) or set(descriptor) != {"kind", "value"} or descriptor.get("kind") not in {"IDENTIFIER", "SHA256", UNKNOWN, REDACTED} or not isinstance(descriptor.get("value"), str):
                return False
    read = data.get("input_read")
    if not isinstance(read, dict) or set(read) != {"status", "expected_sha256", "computed_sha256", "byte_size", "elapsed_seconds"} or read.get("status") not in _READ_STATUSES:
        return False
    evidence = data.get("evidence_created")
    if not isinstance(evidence, dict) or set(evidence) != {"status", "value"} or evidence.get("status") not in {"KNOWN", UNKNOWN} or not (isinstance(evidence.get("value"), bool) or evidence.get("value") == UNKNOWN):
        return False
    for item in files:
        if not isinstance(item, dict) or set(item) != {"locator", "sha256", "size_bytes", "locator_status", "provenance"}:
            return False
        if _safe_relative(item.get("locator")) is None or item.get("locator_status") not in _LOCATOR_STATUSES or item.get("provenance") not in _PROVENANCE:
            return False
        if item.get("sha256") != UNKNOWN and not _SHA256.fullmatch(str(item.get("sha256") or "")):
            return False
        if item.get("size_bytes") != UNKNOWN and not (isinstance(item.get("size_bytes"), int) and not isinstance(item.get("size_bytes"), bool) and item["size_bytes"] >= 0):
            return False
    return True


def rejected_result_safe_facts(marker: str = "WRITER_VALIDATION_REJECTED") -> dict[str, Any]:
    data = {
        "current_identity": {key: UNKNOWN for key in ("request_id", "job_id", "work_number", "work_identity_key")},
        "source_identity": {key: UNKNOWN for key in ("request_id", "job_id", "work_number", "work_identity_key")},
        "identity_relationship": UNKNOWN, "identity_status": UNKNOWN,
        "conflict_status": UNKNOWN, "conflicts": [],
        "input_read": {"status": UNKNOWN, "expected_sha256": UNKNOWN, "computed_sha256": UNKNOWN,
                       "byte_size": UNKNOWN, "elapsed_seconds": UNKNOWN},
        "evidence_created": {"status": UNKNOWN, "value": UNKNOWN},
        "produced_files": [], "provenance": "TERMINAL_ONLY", "markers": [],
    }
    return _finalize(data, "REJECTED", [marker])


def sanitize_result_safe_facts(value: Any) -> dict[str, Any]:
    """Strict writer boundary: verify the canonical projection before persistence."""
    try:
        if len(canonical_bytes(value)) > MAX_PROJECTION_BYTES or not _projection_shape_valid(value):
            return rejected_result_safe_facts()
        payload = canonical_bytes(value["data"])
        if value["canonical_bytes"] != len(payload) or value["canonical_sha256"].lower() != hashlib.sha256(payload).hexdigest():
            return rejected_result_safe_facts("WRITER_CANONICAL_MISMATCH_REJECTED")
        return json.loads(canonical_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, KeyError):
        return rejected_result_safe_facts()
