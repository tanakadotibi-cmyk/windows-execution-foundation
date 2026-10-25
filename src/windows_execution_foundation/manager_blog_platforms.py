from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


class ManagerBlogPlatformError(ValueError):
    """Raised when no safe offline adapter plan can be produced."""


@dataclass(frozen=True)
class PlatformContract:
    form_path: str
    list_path: str | None
    supported_actions: frozenset[str]
    final_control: str
    unresolved_effect_boundary: str


PLATFORM_CONTRACTS = {
    "girls_heaven": PlatformContract(
        form_path="/blog/edit/",
        list_path=None,
        supported_actions=frozenset({"RESERVE_FUTURE"}),
        final_control="NATIVE_RESERVATION_CONTROL_UNRESOLVED",
        unresolved_effect_boundary="UNRESOLVED_FINAL_SUBMIT",
    ),
    "vanilla": PlatformContract(
        form_path="/entry/shop_manager_blog.html",
        list_path="/entry/shop_manager_bloglist.html",
        supported_actions=frozenset({"RESERVE_FUTURE"}),
        final_control="[name=confirm]",
        unresolved_effect_boundary="UNRESOLVED_CONFIRM_TO_FINAL_PUBLISH",
    ),
    "cocoa": PlatformContract(
        form_path="/entry/blog/add/",
        list_path="/entry/blog/",
        supported_actions=frozenset({"PUBLISH_NOW"}),
        final_control="[name=entry]",
        unresolved_effect_boundary="UNRESOLVED_POST_PUBLISH_READBACK",
    ),
}


def build_effect_plan(
    operation: Mapping[str, Any], publish_at: str | datetime | None
) -> dict[str, Any]:
    """Build an offline-only description of intended form assignments."""
    platform = str(operation.get("platform", ""))
    contract = PLATFORM_CONTRACTS.get(platform)
    if contract is None:
        raise ManagerBlogPlatformError("UNKNOWN_PLATFORM")

    action = str(operation.get("action", ""))
    if action not in {"RESERVE_FUTURE", "PUBLISH_NOW"}:
        raise ManagerBlogPlatformError("UNKNOWN_ACTION")
    if action not in contract.supported_actions:
        if platform == "cocoa" and action == "RESERVE_FUTURE":
            raise ManagerBlogPlatformError("UNSUPPORTED_RESERVATION")
        raise ManagerBlogPlatformError("UNSUPPORTED_ACTION")

    title = str(operation["title"])
    body = str(operation["body"])
    assignments: list[dict[str, Any]]
    preconditions = [
        "FORM_PATH_MATCHES_CONTRACT",
        "TITLE_AND_BODY_PRESENT",
        "NO_EXTERNAL_EFFECT_EXECUTION",
    ]

    if platform == "girls_heaven":
        instant = _require_publish_at(publish_at)
        assignments = [
            _assignment("#title", title),
            _assignment("#MpContent", body),
            _assignment("#radioPublic", True),
            _assignment("[name=keisai_dt_y]", f"{instant.year:04d}"),
            _assignment("[name=keisai_dt_m]", f"{instant.month:02d}"),
            _assignment("[name=keisai_dt_d]", f"{instant.day:02d}"),
            _assignment("[name=keisai_dt_h]", f"{instant.hour:02d}"),
            _assignment("[name=keisai_dt_n]", f"{instant.minute:02d}"),
        ]
        image_selector = "#file_pic_pc"
    elif platform == "vanilla":
        instant = _require_publish_at(publish_at)
        assignments = [
            _assignment("#reservation_datetime", instant.strftime("%Y-%m-%d %H:%M:%S")),
            _assignment("#status_1", True),
            _assignment("#title", title),
            _assignment("#editor1", body),
        ]
        image_selector = "#input_file"
    else:
        assignments = [
            _assignment("[name=display_flg]", True),
            _assignment("[name=title]", title),
            _assignment("#ckeditor2", body),
        ]
        image_selector = "#js-upload_file"

    image_path = operation.get("image_path")
    if image_path:
        assignments.append(_assignment(image_selector, str(image_path)))

    return {
        "platform": platform,
        "action": action,
        "form_path": contract.form_path,
        "list_path": contract.list_path,
        "final_control": contract.final_control,
        "intended_assignments": assignments,
        "preconditions": preconditions,
        "unresolved_effect_boundary": contract.unresolved_effect_boundary,
    }


def _assignment(selector: str, value: Any) -> dict[str, Any]:
    return {"selector": selector, "value": value}


def _require_publish_at(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        raise ManagerBlogPlatformError("PUBLISH_AT_REQUIRED")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManagerBlogPlatformError("INVALID_PUBLISH_AT") from exc
