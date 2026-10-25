from __future__ import annotations

import unittest

from windows_execution_foundation.manager_blog_platforms import (
    ManagerBlogPlatformError,
    build_effect_plan,
)


def operation(platform: str, action: str, **extra: str) -> dict[str, str]:
    return {
        "platform": platform,
        "action": action,
        "title": "canonical title",
        "body": "canonical body\nline two",
        **extra,
    }


def values_by_selector(plan: dict) -> dict:
    return {item["selector"]: item["value"] for item in plan["intended_assignments"]}


class ManagerBlogPlatformsTests(unittest.TestCase):
    def test_girls_heaven_reservation_plan(self):
        plan = build_effect_plan(
            operation("girls_heaven", "RESERVE_FUTURE"), "2026-10-05T09:07:00"
        )
        values = values_by_selector(plan)
        self.assertEqual(plan["form_path"], "/blog/edit/")
        self.assertEqual(plan["final_control"], "NATIVE_RESERVATION_CONTROL_UNRESOLVED")
        self.assertEqual(
            [values[f"[name=keisai_dt_{part}]"] for part in "ymdhn"],
            ["2026", "10", "05", "09", "07"],
        )
        self.assertTrue(values["#radioPublic"])
        image_plan = build_effect_plan(
            operation("girls_heaven", "RESERVE_FUTURE", image_path="C:/staged/image.jpg"),
            "2026-10-05T09:07:00",
        )
        self.assertEqual(values_by_selector(image_plan)["#file_pic_pc"], "C:/staged/image.jpg")

    def test_vanilla_datetime_format_and_public_status(self):
        plan = build_effect_plan(
            operation("vanilla", "RESERVE_FUTURE"), "2026-10-05T09:07:08"
        )
        values = values_by_selector(plan)
        self.assertEqual(plan["form_path"], "/entry/shop_manager_blog.html")
        self.assertEqual(plan["list_path"], "/entry/shop_manager_bloglist.html")
        self.assertEqual(plan["final_control"], "[name=confirm]")
        self.assertEqual(values["#reservation_datetime"], "2026-10-05 09:07:08")
        self.assertTrue(values["#status_1"])
        image_plan = build_effect_plan(
            operation("vanilla", "RESERVE_FUTURE", image_path="C:/staged/image.jpg"),
            "2026-10-05T09:07:08",
        )
        self.assertEqual(values_by_selector(image_plan)["#input_file"], "C:/staged/image.jpg")

    def test_cocoa_publish_now_plan(self):
        plan = build_effect_plan(operation("cocoa", "PUBLISH_NOW"), None)
        values = values_by_selector(plan)
        self.assertTrue(values["[name=display_flg]"])
        self.assertEqual(plan["form_path"], "/entry/blog/add/")
        self.assertEqual(plan["list_path"], "/entry/blog/")
        self.assertEqual(plan["final_control"], "[name=entry]")
        self.assertEqual(
            set(values), {"[name=display_flg]", "[name=title]", "#ckeditor2"}
        )
        self.assertIn("NO_EXTERNAL_EFFECT_EXECUTION", plan["preconditions"])

    def test_cocoa_reservation_rejected(self):
        with self.assertRaisesRegex(ManagerBlogPlatformError, "UNSUPPORTED_RESERVATION"):
            build_effect_plan(operation("cocoa", "RESERVE_FUTURE"), "2026-10-05T09:00:00")

    def test_same_title_and_body_are_maintained(self):
        selectors = {
            "girls_heaven": ("#title", "#MpContent", "RESERVE_FUTURE"),
            "vanilla": ("#title", "#editor1", "RESERVE_FUTURE"),
            "cocoa": ("[name=title]", "#ckeditor2", "PUBLISH_NOW"),
        }
        for platform, (title_selector, body_selector, action) in selectors.items():
            with self.subTest(platform=platform):
                plan = build_effect_plan(operation(platform, action), "2026-10-05T09:00:00")
                values = values_by_selector(plan)
                self.assertEqual(values[title_selector], "canonical title")
                self.assertEqual(values[body_selector], "canonical body\nline two")

    def test_image_is_optional(self):
        without = build_effect_plan(operation("cocoa", "PUBLISH_NOW"), None)
        with_image = build_effect_plan(
            operation("cocoa", "PUBLISH_NOW", image_path="C:/staged/image.jpg"), None
        )
        self.assertNotIn("#js-upload_file", values_by_selector(without))
        self.assertEqual(values_by_selector(with_image)["#js-upload_file"], "C:/staged/image.jpg")

    def test_unknown_platform_rejected(self):
        with self.assertRaisesRegex(ManagerBlogPlatformError, "UNKNOWN_PLATFORM"):
            build_effect_plan(operation("other", "PUBLISH_NOW"), None)

    def test_unknown_action_rejected(self):
        with self.assertRaisesRegex(ManagerBlogPlatformError, "UNKNOWN_ACTION"):
            build_effect_plan(operation("cocoa", "DELETE"), None)

    def test_publish_now_is_not_supported_by_reservation_platforms(self):
        for platform in ("girls_heaven", "vanilla"):
            with self.subTest(platform=platform), self.assertRaisesRegex(
                ManagerBlogPlatformError, "UNSUPPORTED_ACTION"
            ):
                build_effect_plan(operation(platform, "PUBLISH_NOW"), None)

    def test_unresolved_boundaries_are_preserved(self):
        expected = {
            "girls_heaven": "UNRESOLVED_FINAL_SUBMIT",
            "vanilla": "UNRESOLVED_CONFIRM_TO_FINAL_PUBLISH",
            "cocoa": "UNRESOLVED_POST_PUBLISH_READBACK",
        }
        for platform, boundary in expected.items():
            action = "PUBLISH_NOW" if platform == "cocoa" else "RESERVE_FUTURE"
            plan = build_effect_plan(operation(platform, action), "2026-10-05T09:00:00")
            self.assertEqual(plan["unresolved_effect_boundary"], boundary)


if __name__ == "__main__":
    unittest.main()
