from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, timedelta

from windows_execution_foundation.manager_blog_weekly import (
    ManagerBlogQueueError, content_hash, dedup_key, plan_operations, validate_queue,
)


def make_queue(count: int = 7, approval: str = "APPROVED") -> dict:
    articles = []
    start = date(2026, 10, 5)
    for offset in range(count):
        day = (start + timedelta(days=offset)).isoformat()
        title, body = f"title {offset}", f"body {offset}"
        articles.append({
            "article_id": f"manager-blog-{day}", "publish_date": day,
            "title": title, "body": body, "content_hash": content_hash(title, body),
            "approval_status": approval, "revision_refs": [],
            "platforms": {"girls_heaven": "PENDING", "vanilla": "PENDING", "cocoa": "PENDING"},
        })
    return {"version": "1.0", "week_id": "2026-W41", "generated_at": "2026-09-30T00:00:00Z",
            "approved_at": "2026-09-30T01:00:00Z" if approval == "APPROVED" else None,
            "learning_refs": ["learning-1"], "articles": articles}


class ManagerBlogWeeklyTests(unittest.TestCase):
    def test_seven_consecutive_days_pass(self):
        self.assertEqual(len(validate_queue(make_queue())["articles"]), 7)

    def test_six_and_eight_rejected(self):
        for count in (6, 8):
            with self.subTest(count=count), self.assertRaises(ManagerBlogQueueError):
                validate_queue(make_queue(count))

    def test_duplicate_day_rejected(self):
        queue = make_queue()
        queue["articles"][1]["publish_date"] = queue["articles"][0]["publish_date"]
        queue["articles"][1]["article_id"] = "other-" + queue["articles"][0]["publish_date"]
        with self.assertRaisesRegex(ManagerBlogQueueError, "DUPLICATE_PUBLISH_DATE"):
            validate_queue(queue)

    def test_content_hash_mismatch_rejected(self):
        queue = make_queue(); queue["articles"][0]["content_hash"] = "bad"
        with self.assertRaisesRegex(ManagerBlogQueueError, "CONTENT_HASH_MISMATCH"):
            validate_queue(queue)

    def test_draft_has_zero_operations(self):
        self.assertEqual(plan_operations(make_queue(approval="DRAFT"), today="2026-10-05")["operations"], [])

    def test_gh_and_vanilla_each_have_seven_reservations(self):
        operations = plan_operations(make_queue(), today="2026-10-04")["operations"]
        for platform in ("girls_heaven", "vanilla"):
            self.assertEqual(sum(o["platform"] == platform for o in operations), 7)

    def test_cocoa_due_today_once_and_future_wait(self):
        due = plan_operations(make_queue(), today="2026-10-05")
        self.assertEqual(sum(o["action"] == "PUBLISH_NOW" for o in due["operations"]), 1)
        future = plan_operations(make_queue(), today="2026-10-04")
        self.assertEqual(sum(o["platform"] == "cocoa" for o in future["operations"]), 0)
        self.assertEqual(future["cocoa_status"], "WAIT")

    def test_multiple_past_backlog_fails_closed(self):
        result = plan_operations(make_queue(), today="2026-10-08")
        self.assertEqual(result["cocoa_status"], "FAIL_CLOSED_BACKLOG")
        self.assertFalse(any(o["platform"] == "cocoa" for o in result["operations"]))

    def test_receipt_replay_is_noop(self):
        queue = make_queue(); article = queue["articles"][0]
        receipts = [{"article_id": article["article_id"], "platform": platform,
                     "dedup_key": dedup_key(article, platform), "result": "PUBLISHED_VERIFIED"}
                    for platform in ("girls_heaven", "vanilla", "cocoa")]
        operations = plan_operations(queue, today=article["publish_date"], receipts=receipts)["operations"]
        self.assertFalse(any(o["article_id"] == article["article_id"] for o in operations))

    def test_receipt_must_match_queue_identity(self):
        queue = make_queue(); article = queue["articles"][0]
        receipt = {"article_id": article["article_id"], "platform": "cocoa",
                   "dedup_key": dedup_key(article, "vanilla"), "result": "PUBLISHED_VERIFIED"}
        with self.assertRaisesRegex(ManagerBlogQueueError, "RECEIPT_QUEUE_MISMATCH"):
            plan_operations(queue, today=article["publish_date"], receipts=[receipt])

    def test_cocoa_terminal_state_is_noop(self):
        for status in ("RESERVED", "PUBLISHED_VERIFIED", "SKIPPED"):
            queue = make_queue(); queue["articles"][0]["platforms"]["cocoa"] = status
            result = plan_operations(queue, today=queue["articles"][0]["publish_date"])
            self.assertFalse(any(o["platform"] == "cocoa" for o in result["operations"]))

    def test_approved_queue_requires_approved_at(self):
        queue = make_queue(); queue["approved_at"] = None
        with self.assertRaisesRegex(ManagerBlogQueueError, "APPROVED_AT_REQUIRED"):
            validate_queue(queue)

    def test_dedup_is_deterministic(self):
        article = make_queue()["articles"][0]
        self.assertEqual(dedup_key(article, "cocoa"), dedup_key(deepcopy(article), "cocoa"))

    def test_three_platforms_required(self):
        queue = make_queue(); del queue["articles"][0]["platforms"]["vanilla"]
        with self.assertRaisesRegex(ManagerBlogQueueError, "THREE_PLATFORMS_REQUIRED"):
            validate_queue(queue)


if __name__ == "__main__":
    unittest.main()
