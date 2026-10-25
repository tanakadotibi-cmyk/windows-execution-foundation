from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .airtable_local_exec import _request

POLICY_BASE_ID = "appdtl4ewm5XkOiGd"
POLICY_TABLE_ID = "tblN420dG307aa2Qa"
EVENT_FIELD_ID = "fld88o21vFmyM1rfR"
VERIFICATION_FIELD_ID = "fldoR6PVCGUJU8nVB"
VERSION_FIELD_ID = "fldSEQEdzm0NXBcIA"
CANONICAL_EVENT_ID = "evt_cityheaven_ab_message_canonical_20260925_v01"
CANONICAL_VERSION = "cityheaven_ab_message_v1_20260923"
POLICY_SCHEMA = "cityheaven_ab_policy_v1"


class CityHeavenPolicyError(RuntimeError):
    pass


def _formula_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def validate_policy(value: Any, required_version: str = CANONICAL_VERSION) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CityHeavenPolicyError("POLICY_JSON_INVALID")
    if value.get("schema_version") != POLICY_SCHEMA:
        raise CityHeavenPolicyError("POLICY_SCHEMA_MISMATCH")
    if (
        not isinstance(required_version, str)
        or not required_version
        or value.get("event_id") != CANONICAL_EVENT_ID
        or value.get("version") != required_version
    ):
        raise CityHeavenPolicyError("POLICY_IDENTITY_MISMATCH")
    rules = value.get("rules")
    template_a = value.get("template_a")
    template_b = value.get("template_b")
    if not isinstance(rules, dict) or rules.get("message_send_enabled") is not False:
        raise CityHeavenPolicyError("POLICY_SEND_NOT_DISABLED")
    if (
        rules.get("course_authority") != "CITYHEAVEN_RESERVATION_DETAIL"
        or rules.get("payload_course_is_hint_only") is not True
        or rules.get("unknown_course_must_fail_closed") is not True
        or rules.get("exact_message_match_required") is not True
    ):
        raise CityHeavenPolicyError("POLICY_RULES_INVALID")
    if (
        not isinstance(template_a, dict)
        or template_a.get("match_type") != "course_exact"
        or not isinstance(template_a.get("course_exact"), str)
        or not template_a["course_exact"].strip()
        or not isinstance(template_a.get("text"), str)
        or not template_a["text"].strip()
        or not isinstance(template_b, dict)
        or template_b.get("match_type") != "fallback_after_source_backed_course"
        or not isinstance(template_b.get("text"), str)
        or not template_b["text"].strip()
    ):
        raise CityHeavenPolicyError("POLICY_TEMPLATES_INVALID")
    return value


def load_canonical_policy(
    local_exec_config: dict[str, Any],
    *,
    required_version: str = CANONICAL_VERSION,
    request: Callable[..., dict[str, Any]] = _request,
) -> dict[str, Any]:
    config = {**local_exec_config, "base_id": POLICY_BASE_ID, "table_id": POLICY_TABLE_ID}
    event = _formula_string(CANONICAL_EVENT_ID)
    if not isinstance(required_version, str) or not required_version:
        raise CityHeavenPolicyError("POLICY_IDENTITY_MISMATCH")
    version = _formula_string(required_version)
    response = request(
        config,
        "GET",
        params={
            "filterByFormula": (
                f'AND({{{EVENT_FIELD_ID}}}="{event}",'
                f'{{{VERSION_FIELD_ID}}}="{version}")'
            ),
            "pageSize": 2,
            "returnFieldsByFieldId": "true",
            "fields[]": [EVENT_FIELD_ID, VERSION_FIELD_ID, VERIFICATION_FIELD_ID],
        },
    )
    records = response.get("records") if isinstance(response, dict) else None
    if not isinstance(records, list) or len(records) != 1:
        raise CityHeavenPolicyError("POLICY_RECORD_NOT_UNIQUE")
    fields = records[0].get("fields") if isinstance(records[0], dict) else None
    if not isinstance(fields, dict):
        raise CityHeavenPolicyError("POLICY_RECORD_INVALID")
    if fields.get(EVENT_FIELD_ID) != CANONICAL_EVENT_ID or fields.get(VERSION_FIELD_ID) != required_version:
        raise CityHeavenPolicyError("POLICY_IDENTITY_MISMATCH")
    try:
        policy = json.loads(fields.get(VERIFICATION_FIELD_ID))
    except (TypeError, ValueError) as exc:
        raise CityHeavenPolicyError("POLICY_JSON_INVALID") from exc
    return validate_policy(policy, required_version)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--required-version", required=True)
    args = parser.parse_args()
    try:
        configuration = json.loads(args.config.read_text(encoding="utf-8"))
        policy = load_canonical_policy(
            configuration["local_exec_airtable"], required_version=args.required_version
        )
    except CityHeavenPolicyError as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")))
        return 20
    except Exception:
        print('{"error":"POLICY_LOAD_FAILED"}')
        return 20
    print(json.dumps(policy, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
