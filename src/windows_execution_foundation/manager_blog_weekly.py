from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from .core import stable_key


PLATFORMS = ("girls_heaven", "vanilla", "cocoa")
APPROVAL_STATUSES = frozenset({"DRAFT", "APPROVED"})
PLATFORM_STATUSES = frozenset(
    {"PENDING", "RESERVED", "PUBLISHED_VERIFIED", "FAILED", "SKIPPED"}
)
RECEIPT_FIELDS = frozenset(
    {
        "article_id",
        "platform",
        "dedup_key",
        "result",
        "site_article_id",
        "public_url",
        "observed_title",
        "observed_body_hash",
        "published_at",
        "verified_at",
    }
)


class ManagerBlogQueueError(ValueError):
    pass


def content_hash(title: str, body: str) -> str:
    """Hash the canonical, untransformed title/body pair."""
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()


def dedup_key(article: Mapping[str, Any], platform: str) -> str:
    if platform not in PLATFORMS:
        raise ManagerBlogQueueError("UNKNOWN_PLATFORM")
    source = "\n".join(
        (
            str(article["article_id"]),
            platform,
            str(article["publish_date"]),
            str(article["content_hash"]),
        )
    )
    return stable_key(source)


def validate_queue(queue: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version", "week_id", "generated_at", "approved_at", "learning_refs", "articles"
    }
    missing = required - queue.keys()
    if missing:
        raise ManagerBlogQueueError(f"QUEUE_FIELDS_MISSING:{','.join(sorted(missing))}")
    articles = queue["articles"]
    if not isinstance(articles, list) or len(articles) != 7:
        raise ManagerBlogQueueError("ARTICLE_COUNT_MUST_BE_7")
    if not isinstance(queue["learning_refs"], list):
        raise ManagerBlogQueueError("LEARNING_REFS_MUST_BE_LIST")

    parsed_dates: list[date] = []
    article_ids: set[str] = set()
    article_required = {
        "article_id", "publish_date", "title", "body", "content_hash",
        "approval_status", "revision_refs", "platforms",
    }
    for article in articles:
        if not isinstance(article, Mapping):
            raise ManagerBlogQueueError("ARTICLE_MUST_BE_OBJECT")
        missing_article = article_required - article.keys()
        if missing_article:
            raise ManagerBlogQueueError(
                f"ARTICLE_FIELDS_MISSING:{','.join(sorted(missing_article))}"
            )
        try:
            publish_date = date.fromisoformat(str(article["publish_date"]))
        except ValueError as exc:
            raise ManagerBlogQueueError("INVALID_PUBLISH_DATE") from exc
        parsed_dates.append(publish_date)
        article_id = str(article["article_id"])
        if publish_date.isoformat() not in article_id:
            raise ManagerBlogQueueError("ARTICLE_ID_MUST_CONTAIN_DATE")
        if article_id in article_ids:
            raise ManagerBlogQueueError("DUPLICATE_ARTICLE_ID")
        article_ids.add(article_id)
        expected_hash = content_hash(str(article["title"]), str(article["body"]))
        if article["content_hash"] != expected_hash:
            raise ManagerBlogQueueError("CONTENT_HASH_MISMATCH")
        if article["approval_status"] not in APPROVAL_STATUSES:
            raise ManagerBlogQueueError("INVALID_APPROVAL_STATUS")
        if not isinstance(article["revision_refs"], list):
            raise ManagerBlogQueueError("REVISION_REFS_MUST_BE_LIST")
        platforms = article["platforms"]
        if not isinstance(platforms, Mapping) or set(platforms) != set(PLATFORMS):
            raise ManagerBlogQueueError("THREE_PLATFORMS_REQUIRED")
        if any(status not in PLATFORM_STATUSES for status in platforms.values()):
            raise ManagerBlogQueueError("INVALID_PLATFORM_STATUS")

    if len(set(parsed_dates)) != 7:
        raise ManagerBlogQueueError("DUPLICATE_PUBLISH_DATE")
    if any(a["approval_status"] == "APPROVED" for a in articles) and not queue["approved_at"]:
        raise ManagerBlogQueueError("APPROVED_AT_REQUIRED")
    ordered = sorted(parsed_dates)
    if any(current != ordered[0] + timedelta(days=index) for index, current in enumerate(ordered)):
        raise ManagerBlogQueueError("PUBLISH_DATES_MUST_BE_CONSECUTIVE")
    return dict(queue)


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if set(receipt) - RECEIPT_FIELDS:
        raise ManagerBlogQueueError("RECEIPT_FIELDS_NOT_ALLOWED")
    required = {"article_id", "platform", "dedup_key", "result"}
    if required - receipt.keys():
        raise ManagerBlogQueueError("RECEIPT_FIELDS_MISSING")
    if receipt["platform"] not in PLATFORMS:
        raise ManagerBlogQueueError("UNKNOWN_PLATFORM")
    if receipt["result"] not in PLATFORM_STATUSES:
        raise ManagerBlogQueueError("INVALID_RECEIPT_RESULT")
    return dict(receipt)


def plan_operations(
    queue: Mapping[str, Any], *, today: str | date, receipts: Iterable[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    validated = validate_queue(queue)
    try:
        today_date = today if isinstance(today, date) else date.fromisoformat(today)
    except ValueError as exc:
        raise ManagerBlogQueueError("INVALID_TODAY") from exc

    articles_by_id = {str(a["article_id"]): a for a in validated["articles"]}
    receipt_keys: set[str] = set()
    for raw_receipt in receipts:
        receipt = validate_receipt(raw_receipt)
        article = articles_by_id.get(str(receipt["article_id"]))
        if article is None or receipt["dedup_key"] != dedup_key(article, str(receipt["platform"])):
            raise ManagerBlogQueueError("RECEIPT_QUEUE_MISMATCH")
        receipt_keys.add(str(receipt["dedup_key"]))
    operations: list[dict[str, str]] = []
    approved = [a for a in validated["articles"] if a["approval_status"] == "APPROVED"]

    for article in approved:
        for platform in ("girls_heaven", "vanilla"):
            key = dedup_key(article, platform)
            if key in receipt_keys or article["platforms"][platform] in {
                "RESERVED", "PUBLISHED_VERIFIED"
            }:
                continue
            operations.append(_operation("RESERVE_FUTURE", article, platform, key))

    due = [a for a in approved if date.fromisoformat(a["publish_date"]) == today_date]
    backlog = [
        a for a in approved
        if date.fromisoformat(a["publish_date"]) < today_date
        and a["platforms"]["cocoa"] not in {"RESERVED", "PUBLISHED_VERIFIED", "SKIPPED"}
        and dedup_key(a, "cocoa") not in receipt_keys
    ]
    if len(backlog) > 1:
        cocoa_status = "FAIL_CLOSED_BACKLOG"
    elif len(due) == 1:
        article = due[0]
        key = dedup_key(article, "cocoa")
        if key in receipt_keys or article["platforms"]["cocoa"] in {
            "RESERVED", "PUBLISHED_VERIFIED", "SKIPPED"
        }:
            cocoa_status = "NOOP_RECEIPT"
        else:
            operations.append(_operation("PUBLISH_NOW", article, "cocoa", key))
            cocoa_status = "PUBLISH_NOW"
    elif any(date.fromisoformat(a["publish_date"]) > today_date for a in approved):
        cocoa_status = "WAIT"
    else:
        cocoa_status = "NO_DUE_ARTICLE"
    return {"operations": operations, "cocoa_status": cocoa_status}


def _operation(action: str, article: Mapping[str, Any], platform: str, key: str) -> dict[str, str]:
    return {
        "action": action,
        "article_id": str(article["article_id"]),
        "platform": platform,
        "publish_date": str(article["publish_date"]),
        "content_hash": str(article["content_hash"]),
        "dedup_key": key,
        "title": str(article["title"]),
        "body": str(article["body"]),
    }
