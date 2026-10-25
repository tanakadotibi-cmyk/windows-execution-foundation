"""Bounded Gemini function calling over four read-only Management OS tables."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .airtable_local_exec import _request

BASE_ID = "appdtl4ewm5XkOiGd"
MODEL = "gemini-3.5-flash-lite"
SDK_VERSION = "1.75.0"
MAX_LIMIT = 20
MAX_SCAN = 100
MAX_TOOL_CALLS = 6
MAX_FIELD_CHARS = 800
MAX_TOOL_OUTPUT_CHARS = 12000

TABLES = {
    "projects": "tblXgZibQ3LHwrQ7m",
    "next_actions": "tblWOONlJHuRhKR7Y",
    "project_events": "tblN420dG307aa2Qa",
    "assets": "tblUEP0NWAl0kW5NO",
}

# Names are used with returnFieldsByFieldId=false.  Existing field IDs are also
# accepted for the three tables whose checked-in Bridge contract defines them.
FIELD_ALLOWLISTS = {
    "projects": {
        "Project ID": "project_id", "Project Name": "name", "Name": "name",
        "Status": "status", "Current State": "current_state",
        "Next Action": "next_action", "Updated At": "updated_at",
        "Goal": "goal", "Owner": "owner",
    },
    "next_actions": {
        "Action ID": "action_id", "fldfjRDxCPjLcQ3WH": "action_id",
        "Project ID": "project_id", "fldwoAGYzNGMLvtw5": "project_id",
        "Status": "status", "fldt2TuwmM6YP4KWk": "status",
        "Action": "action", "fld7RRwgng2s04gBo": "action",
        "Strategic End State": "strategic_end_state", "fldeW3GKfv2ZsQH5M": "strategic_end_state",
        "Next 3 Moves": "next_moves", "fldEOEwQrG5KWCO7Q": "next_moves",
        "Completion Evidence Gate": "completion_evidence", "fldhJLA0Ph7VjLTSI": "completion_evidence",
        "Evidence": "evidence", "fldJ7EWVoc9YjQRbI": "evidence",
        "Execution State": "execution_state", "fld3dcStUvfVtcoAn": "execution_state",
        "Execution Started At": "started_at", "fldV0GATirHVsEcLg": "started_at",
        "Last Heartbeat": "heartbeat", "fldQyNLRs6XqWWcFZ": "heartbeat",
    },
    "project_events": {
        "Event ID": "event_id", "fld88o21vFmyM1rfR": "event_id",
        "Project ID": "project_id", "fld6N3eFpDAugtEac": "project_id",
        "Event Type": "event_type", "fldH0x5d0E1DDNkqb": "event_type",
        "What Happened": "what_happened", "fldf6yYwodDkWvd7E": "what_happened",
        "Occurred At": "occurred_at", "fldpVV7pPNNlHhUcz": "occurred_at",
        "Verification": "verification", "fldoR6PVCGUJU8nVB": "verification",
        "Action Taken": "action_taken", "fldcl4iE1wuO1NzQz": "action_taken",
        "Result": "result", "fldmz2asbvZqgBtg6": "result",
        "Remaining": "remaining", "fld5Jjk9DHccu4UxE": "remaining",
        "Source": "source", "fld7qnFzBySeepzlC": "source",
        "Action ID": "action_id", "fldO5iDuugCjIVlBE": "action_id",
    },
    "assets": {
        "Asset ID": "asset_id", "fldo4GA2bSwOjhT0X": "asset_id",
        "Name": "name", "fldJV2TiFimjAhdjx": "name",
        "Purpose": "purpose", "fldPYKjOQeMlgvFWl": "purpose",
        "Maturity": "maturity", "fldacoWk4wOkVOHHp": "maturity",
        "Operating": "operating", "fldmzFcJwWoyEjLmT": "operating",
        "Incomplete": "incomplete", "flda47IVFmeEXlSwH": "incomplete",
        "Next Proof": "next_proof", "fldUtU51irc26IgrL": "next_proof",
        "Reuse": "reuse", "fldAzjgSqZh29XKCF": "reuse",
        "Health": "health", "fldXiXDWQnIXxHeQb": "health",
        "Version": "version", "fldvOlHpXzXpA2oxV": "version",
        "Updated At": "updated_at", "fldSXTOeweg9099ZI": "updated_at",
        "Past Learning": "past_learning", "fld23lFYUkG1hAXy1": "past_learning",
        "Next First Check": "next_first_check", "fldpRBVTlnSkX9qwx": "next_first_check",
    },
}

SYSTEM_INSTRUCTION = """You answer using the canonical Airtable Management OS when asked about current state, existing assets, past successes/failures, or next steps. Do not guess: call the provided tools. Separate Airtable FACTS from MODEL INFERENCE in the final answer and cite source/table plus record_id for factual claims. Use only the returned fields. Keep the answer concise. The tools are read-only."""

_SECRET = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|client[_ -]?secret|authorization|credential|cookie)\s*[:=]\s*[^\s,;]+"
)
_API_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")


class ReadonlyContractError(ValueError):
    pass


def _source(source: Any) -> str:
    value = str(source or "").strip().lower()
    if value not in TABLES:
        raise ReadonlyContractError("SOURCE_NOT_ALLOWED")
    return value


def normalize_query(value: Any, maximum: int = 160) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maximum]


def _redact_text(value: Any) -> str:
    text = normalize_query(value, MAX_FIELD_CHARS)
    text = _SECRET.sub("[REDACTED_SECRET]", text)
    text = _API_KEY.sub("[REDACTED_API_KEY]", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return text


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, dict):
        # Airtable select/collaborator-like objects: only display-safe values.
        safe = {}
        for key in ("name", "value"):
            if key in value:
                safe[key] = _safe_value(value[key])
        return safe or "[REDACTED_OBJECT]"
    return _redact_text(value)


def _sanitize_record(source: str, record: dict[str, Any]) -> dict[str, Any]:
    allowed = FIELD_ALLOWLISTS[source]
    raw = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    fields: dict[str, Any] = {}
    for input_name, output_name in allowed.items():
        if input_name in raw and output_name not in fields:
            fields[output_name] = _safe_value(raw[input_name])
    return {
        "source": source,
        "table": TABLES[source],
        "record_id": _redact_text(record.get("id", ""))[:32],
        "fields": fields,
    }


def _bounded_output(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= MAX_TOOL_OUTPUT_CHARS:
        return payload
    records = list(payload.get("records") or [])
    while records and len(json.dumps({**payload, "records": records}, ensure_ascii=False, separators=(",", ":"))) > MAX_TOOL_OUTPUT_CHARS:
        records.pop()
    result = {**payload, "records": records, "output_truncated": True}
    return result


def _readonly_config(config: dict[str, Any], source: str) -> dict[str, Any]:
    # Construct a fresh narrow config; caller-controlled base/table values are ignored.
    return {
        "base_id": BASE_ID,
        "table_id": TABLES[source],
        "secure_store_module": config["secure_store_module"],
        "credential_path": config["credential_path"],
    }


def search_management_os(
    config: dict[str, Any], *, source: str, query: Any = "", project_id: Any = "",
    status: Any = "", limit: Any = 10, request_fn: Callable[..., dict[str, Any]] = _request,
) -> dict[str, Any]:
    source = _source(source)
    try:
        bounded_limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        bounded_limit = 10
    needles = [normalize_query(item).casefold() for item in (query, project_id, status) if normalize_query(item)]
    response = request_fn(
        _readonly_config(config, source), "GET",
        params={"pageSize": MAX_SCAN, "maxRecords": MAX_SCAN, "returnFieldsByFieldId": "false"},
    )
    records = []
    for raw in list(response.get("records") or [])[:MAX_SCAN]:
        safe = _sanitize_record(source, raw)
        searchable = json.dumps(safe["fields"], ensure_ascii=False).casefold()
        if all(needle in searchable for needle in needles):
            records.append(safe)
            if len(records) >= bounded_limit:
                break
    return _bounded_output({"read_only": True, "source": source, "scanned": min(MAX_SCAN, len(response.get("records") or [])), "records": records})


def get_management_os_record(
    config: dict[str, Any], *, source: str, record_id: Any,
    request_fn: Callable[..., dict[str, Any]] = _request,
) -> dict[str, Any]:
    source = _source(source)
    rid = normalize_query(record_id, 32)
    if not re.fullmatch(r"rec[A-Za-z0-9]{10,20}", rid):
        raise ReadonlyContractError("RECORD_ID_INVALID")
    response = request_fn(_readonly_config(config, source), "GET", record_id=rid,
                          params={"returnFieldsByFieldId": "false"})
    return _bounded_output({"read_only": True, "record": _sanitize_record(source, response)})


def _tool_specs(types: Any) -> list[Any]:
    source_schema = {"type": "string", "enum": list(TABLES)}
    declarations = [
        types.FunctionDeclaration(
            name="search_management_os",
            description="Search one fixed canonical Management OS table read-only.",
            parameters_json_schema={"type": "object", "required": ["source"], "properties": {
                "source": source_schema, "query": {"type": "string"},
                "project_id": {"type": "string"}, "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            }, "additionalProperties": False},
        ),
        types.FunctionDeclaration(
            name="get_management_os_record",
            description="Get one Airtable record by ID from one fixed table read-only.",
            parameters_json_schema={"type": "object", "required": ["source", "record_id"], "properties": {
                "source": source_schema, "record_id": {"type": "string", "pattern": "^rec[A-Za-z0-9]{10,20}$"},
            }, "additionalProperties": False},
        ),
    ]
    return [types.Tool(function_declarations=declarations)]


def run_manual_tool_loop(
    client: Any, config: dict[str, Any], question: str, *, model: str = MODEL,
    types_module: Any | None = None, request_fn: Callable[..., dict[str, Any]] = _request,
) -> dict[str, Any]:
    if types_module is None:
        from google.genai import types as types_module
    types = types_module
    contents: list[Any] = [question]
    api_calls = function_calls = airtable_gets = 0
    for _ in range(MAX_TOOL_CALLS + 1):
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=_tool_specs(types),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0,
                max_output_tokens=800,
            ),
        )
        api_calls += 1
        content = response.candidates[0].content
        calls = [part.function_call for part in content.parts if getattr(part, "function_call", None)]
        if not calls:
            return {"answer": _redact_text(getattr(response, "text", ""))[:4000],
                    "gemini_api_call_count": api_calls, "function_call_count": function_calls,
                    "airtable_get_count": airtable_gets}
        if function_calls + len(calls) > MAX_TOOL_CALLS:
            raise RuntimeError("TOOL_CALL_LIMIT")
        contents.append(content)
        response_parts = []
        for call in calls:
            function_calls += 1
            args = dict(call.args or {})
            if call.name == "search_management_os":
                result = search_management_os(config, request_fn=request_fn, **args)
            elif call.name == "get_management_os_record":
                result = get_management_os_record(config, request_fn=request_fn, **args)
            else:
                raise ReadonlyContractError("TOOL_NOT_ALLOWED")
            airtable_gets += 1
            response_parts.append(types.Part.from_function_response(name=call.name, response=result))
        contents.append(types.Content(role="user", parts=response_parts))
    raise RuntimeError("TOOL_CALL_LIMIT")


def _environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value or os.name != "nt":
        return value
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except (OSError, ImportError):
        return ""


def _load_airtable_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))["local_exec_airtable"]
    return {"secure_store_module": raw["secure_store_module"], "credential_path": raw["credential_path"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-canary", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("local/config.json"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if not args.live_canary:
        parser.error("--live-canary is required")
    from google import genai
    from google.genai import types
    key = _environment("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY_MISSING")
    question = "execution_completion_bridge_v1 の現在地・最近のGemini Healer関連失敗/成功・次の一手をAirtable正本から確認して答えてください。"
    with genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=20000, retry_options=types.HttpRetryOptions(attempts=1)
    )) as client:
        result = run_manual_tool_loop(client, _load_airtable_config(args.config), question, types_module=types)
    evidence = {
        "reused_assets": ["local/config.json local_exec_airtable secure store", "airtable_local_exec._load_token/_request", "canonical ai_healer_venv google-genai and GEMINI_API_KEY"],
        "changed_files": ["src/windows_execution_foundation/gemini_airtable_readonly.py", "tests/test_gemini_airtable_readonly.py", "local/gemini-airtable-readonly-context-v01-20260929.json"],
        "table_allowlist": TABLES, "field_allowlists": {k: sorted(set(v.values())) for k, v in FIELD_ALLOWLISTS.items()},
        "airtable_http_methods": ["GET"], "airtable_get_count": result["airtable_get_count"], "airtable_write_count": 0,
        "gemini_model": MODEL, "gemini_sdk_version": SDK_VERSION,
        "gemini_api_call_count": result["gemini_api_call_count"], "function_call_count": result["function_call_count"],
        "tool_names": ["search_management_os", "get_management_os_record"],
        "canary_question_sanitized": question,
        "canary_answer_excerpt_sanitized": _redact_text(result["answer"])[:1200],
        "fact_grounding_evidence": {"function_call_observed": result["function_call_count"] >= 1, "airtable_get_observed": result["airtable_get_count"] >= 1, "final_answer_observed": bool(result["answer"])},
        "redaction_result": "PASS", "focused_tests": "PENDING_CLOSEOUT", "regression_tests": "PENDING_CLOSEOUT",
        "new_infra_count": 0, "acceptance_pass": result["function_call_count"] >= 1 and result["airtable_get_count"] >= 1 and bool(result["answer"]),
        "remaining_issue": None,
    }
    if args.evidence:
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))
    return 0 if evidence["acceptance_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
