from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"

from src.windows_execution_foundation.cityheaven_daily_metrics import CollectorError
from src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2 import (
    DEFAULT_PROFILE,
    PREFS_BACKUP_NAME,
    PREFS_BUSY,
    PREFS_INVALID,
    START_URL,
    WindowsDeps,
    prepare_profile_norestore,
    profile_preferences_paths,
)
from tests.test_cityheaven_reservation_chrome_ensure_v2 import (
    PROFILE,
    FakeDeps,
    configuration,
    ensure_reservation_chrome,
    fast,
    proc,
    reservation_cmdline,
)

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "prepare-reservation-chrome-norestore.py"
    spec = importlib.util.spec_from_file_location("prepare_reservation_chrome_norestore", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_prefs(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class NorestorePreferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name) / "ReservationRuntimeChromeProfile"
        self.profile.mkdir()
        self.default = self.profile / "Default"
        self.prefs = self.default / "Preferences"
        self.backup = self.default / PREFS_BACKUP_NAME

    def tearDown(self):
        self._tmp.cleanup()

    def test_windows_preference_paths_keep_the_backup_name(self):
        default_dir, preferences, backup = profile_preferences_paths(DEFAULT_PROFILE)
        self.assertEqual(default_dir, DEFAULT_PROFILE + r"\Default")
        self.assertEqual(preferences, DEFAULT_PROFILE + r"\Default\Preferences")
        self.assertEqual(backup, DEFAULT_PROFILE + r"\Default\Preferences.before-norestore")

    def test_backup_is_written_once_and_unrelated_keys_stay(self):
        original = {
            "homepage": "https://newmanager.cityheaven.net/",
            "session": {
                "restore_on_startup": 1,
                "startup_urls": ["https://example.invalid/old"],
                "extra": 1,
            },
            "profile": {
                "name": "Default",
                "exit_type": "Crashed",
                "exited_cleanly": False,
                "avatar_index": 3,
            },
            "other": {"keep": True},
        }
        write_prefs(self.prefs, original)
        before = self.prefs.read_bytes()
        first = prepare_profile_norestore(str(self.profile), [])
        self.assertTrue(first["backup_written"])
        self.assertEqual(self.backup.read_bytes(), before)
        saved = json.loads(self.prefs.read_text(encoding="utf-8"))
        self.assertEqual(saved["homepage"], original["homepage"])
        self.assertNotIn("cityheaven", json.dumps(saved["session"]["startup_urls"]).lower())
        self.assertEqual(saved["other"], original["other"])
        self.assertEqual(saved["session"]["extra"], 1)
        self.assertEqual(saved["session"]["restore_on_startup"], 4)
        self.assertEqual(saved["session"]["startup_urls"], [START_URL])
        self.assertEqual(saved["profile"]["name"], "Default")
        self.assertEqual(saved["profile"]["avatar_index"], 3)
        self.assertEqual(saved["profile"]["exit_type"], "Normal")
        self.assertIs(saved["profile"]["exited_cleanly"], True)
        self.assertFalse((self.profile / "Preferences.tmp").exists())
        edited = self.prefs.read_bytes()
        second = prepare_profile_norestore(str(self.profile), [])
        self.assertFalse(second["backup_written"])
        self.assertEqual(self.backup.read_bytes(), before)
        self.assertNotEqual(edited, before)
        self.assertTrue(self.profile.is_dir())

    def test_running_chrome_refuses_without_writing(self):
        write_prefs(self.prefs, {"session": {"restore_on_startup": 1}, "marker": "stay"})
        before = self.prefs.read_bytes()
        running = proc(4, reservation_cmdline(profile=str(self.profile)))
        with self.assertRaisesRegex(CollectorError, PREFS_BUSY):
            prepare_profile_norestore(str(self.profile), [running])
        self.assertEqual(self.prefs.read_bytes(), before)
        self.assertFalse(self.backup.exists())

    def test_missing_preferences_are_created_without_a_backup(self):
        report = prepare_profile_norestore(str(self.profile), [])
        self.assertFalse(report["backup_written"])
        self.assertFalse(self.backup.exists())
        saved = json.loads(self.prefs.read_text(encoding="utf-8"))
        self.assertEqual(saved["session"]["startup_urls"], ["about:blank"])
        self.assertEqual(saved["session"]["restore_on_startup"], 4)
        self.assertEqual(saved["profile"]["exit_type"], "Normal")
        self.assertIs(saved["profile"]["exited_cleanly"], True)
        self.assertNotIn("http", json.dumps(saved["session"]))

    def test_invalid_json_is_backed_up_and_left_unchanged(self):
        write_prefs(self.prefs, "{")
        before = self.prefs.read_bytes()
        with self.assertRaisesRegex(CollectorError, PREFS_INVALID):
            prepare_profile_norestore(str(self.profile), [])
        self.assertEqual(self.prefs.read_bytes(), before)
        self.assertEqual(self.backup.read_bytes(), before)
        self.assertFalse(list(self.default.glob("Preferences.tmp")))

    def test_profile_without_the_marker_is_not_edited(self):
        stranger = Path(self._tmp.name) / "OtherProfile"
        stranger.mkdir()
        with self.assertRaisesRegex(CollectorError, "CDP_PORT_RUNTIME_MISMATCH"):
            prepare_profile_norestore(str(stranger), [])
        self.assertFalse((stranger / "Default").exists())

    def test_prepare_failure_does_not_launch_and_reuse_does_not_edit(self):
        other = proc(7, r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data')
        deps = FakeDeps([other], ["CLOSED", "CLOSED", "CLOSED", "CLOSED", "CHROME"], profile=PROFILE)
        deps.prepare_error = CollectorError(PREFS_BUSY)
        with self.assertRaisesRegex(CollectorError, PREFS_BUSY):
            ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(deps.trace, ["prepare"])
        self.assertEqual(deps.launches, [])
        reused = FakeDeps([proc(1, reservation_cmdline())], ["CHROME"], profile=PROFILE)
        report = ensure_reservation_chrome(configuration(), deps=reused, timing=fast())
        self.assertEqual(report["action"], "reused")
        self.assertEqual(reused.prepared, [])

    def test_real_prepare_is_blocked_in_unit_tests(self):
        with self.assertRaisesRegex(Exception, "unit test blocked"):
            WindowsDeps(profile=PROFILE).prepare_profile(PROFILE)


class NorestoreScriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name) / "ReservationRuntimeChromeProfile"
        self.profile.mkdir()
        self.script = load_script()

    def tearDown(self):
        self._tmp.cleanup()

    def test_script_backs_up_and_does_not_name_a_page(self):
        source = (ROOT / "prepare-reservation-chrome-norestore.py").read_text(encoding="utf-8")
        self.assertIn("Preferences.before-norestore", source)
        for banned in ("http://", "https://", "cityheaven.net", "newmanager", "webbrowser", "startfile"):
            self.assertNotIn(banned, source)
        prefs = self.profile / "Default" / "Preferences"
        write_prefs(prefs, {"homepage": "https://example.invalid/keep", "session": {"restore_on_startup": 1}})
        before = prefs.read_bytes()
        code = self.script.main(argv=[], list_processes=lambda: [], profile=str(self.profile))
        self.assertEqual(code, 0)
        saved = json.loads(prefs.read_text(encoding="utf-8"))
        self.assertEqual(saved["session"]["startup_urls"], ["about:blank"])
        self.assertEqual(saved["homepage"], "https://example.invalid/keep")
        self.assertEqual((self.profile / "Default" / PREFS_BACKUP_NAME).read_bytes(), before)

    def test_script_refuses_while_chrome_is_open(self):
        prefs = self.profile / "Default" / "Preferences"
        write_prefs(prefs, {"session": {"restore_on_startup": 1}})
        before = prefs.read_bytes()
        running = proc(9, reservation_cmdline(profile=str(self.profile)))
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = self.script.main(argv=[], list_processes=lambda: [running], profile=str(self.profile))
        self.assertEqual(code, 1)
        self.assertIn(PREFS_BUSY, errors.getvalue())
        self.assertEqual(prefs.read_bytes(), before)
        self.assertFalse((self.profile / "Default" / PREFS_BACKUP_NAME).exists())

    def test_script_rejects_a_url_profile(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = self.script.main(argv=["--profile", "https://example.invalid/ReservationRuntimeChromeProfile"], list_processes=lambda: [])
        self.assertEqual(code, 2)
        self.assertIn("usage:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
