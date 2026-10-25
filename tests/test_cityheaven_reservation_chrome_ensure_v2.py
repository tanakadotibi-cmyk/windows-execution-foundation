from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"

from src.windows_execution_foundation.cityheaven_daily_metrics import CollectorError
from src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2 import (
    CDP_PORT,
    DEFAULT_CHROME,
    DEFAULT_PROFILE,
    DEFAULT_USER_SID,
    EXECUTION_TIME_LIMIT,
    FAILURE,
    LIVE_RESTART_TASK,
    TASK_NAME,
    ChromeSettings,
    EnsureSession,
    ProcessInfo,
    RealIoBlocked,
    Timing,
    build_task_xml,
    cdp_browser_close,
    cdp_version_url,
    close_reservation_chrome,
    ensure_reservation_chrome,
    is_reservation_chrome,
    launch_argument_list,
    launch_spec,
    parse_process_json,
    port_flag_matches,
    probe_cdp,
    production_settings,
    reservation_chrome_session,
    select_processes,
    settings_from_configuration,
    split_windows_cmdline,
    windows_launch,
    windows_list_processes,
    windows_stop,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = DEFAULT_PROFILE


def fixture_text(name: str) -> str:
    path = FIXTURES / name
    if not path.is_file():
        raise unittest.SkipTest(f"missing fixture {name}")
    return path.read_text(encoding="utf-8")


def configuration(**overrides):
    city = {
        "cdp_port": 9227,
        "chrome_executable": DEFAULT_CHROME,
        "dedicated_profile_path": PROFILE,
        "reader_path": r"C:\Readers\reservation.js",
        "node_executable": r"C:\Node\node.exe",
        "task_user_id": DEFAULT_USER_SID,
    }
    city.update(overrides)
    return {
        "state_dir": r"C:\State",
        "cityheaven_daily": {"browser": "RESERVATION_CHROME_REUSE", "cdp_port": 9236},
        "cityheaven_reservation": city,
    }


def reservation_cmdline(*, port=9227, profile=PROFILE, extra=""):
    return (
        f'"{DEFAULT_CHROME}" --remote-debugging-port={port} '
        f'--user-data-dir={profile} --profile-directory=Default '
        f"--no-first-run --no-default-browser-check --restore-last-session{extra}"
    )


def proc(pid, command_line, name="chrome.exe"):
    return ProcessInfo(pid, name, command_line)


class FakeDeps:
    def __init__(self, processes, probes, *, relaunch_process=True, graceful=True, profile=PROFILE):
        self.processes = list(processes)
        self.probes = list(probes)
        self.relaunch_process = relaunch_process
        self.graceful = graceful
        self.profile = profile
        self.sleeps = []
        self.stopped = []
        self.launches = []
        self.closed_ports = []
        self.prepared = []
        self.trace = []
        self.prepare_error = None

    def list_processes(self):
        return list(self.processes)

    def probe_cdp(self, port):
        if port != CDP_PORT:
            raise AssertionError(port)
        if not self.probes:
            return "CLOSED"
        return self.probes.pop(0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def stop(self, pids):
        self.stopped.append(list(pids))
        banned = set(pids)
        self.processes = [item for item in self.processes if item.pid not in banned]

    def prepare_profile(self, profile):
        self.trace.append("prepare")
        self.prepared.append(profile)
        if self.prepare_error is not None:
            raise self.prepare_error

    def launch(self, spec):
        self.trace.append("launch")
        self.launches.append(spec)
        if self.relaunch_process:
            self.processes.append(proc(32636, reservation_cmdline(profile=self.profile)))

    def browser_close(self, port):
        self.closed_ports.append(port)
        if self.graceful:
            self.processes = [item for item in self.processes if not is_reservation_chrome(item, self.profile)]


def fast(post=2):
    return Timing(
        verify_attempts=3, verify_spacing_seconds=3,
        post_launch_attempts=post, post_launch_spacing_seconds=2,
    )


class EnsureTests(unittest.TestCase):
    def test_match_on_first_probe_does_not_sleep_stop_or_launch(self):
        other = proc(2, r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data')
        deps = FakeDeps([proc(1, reservation_cmdline()), other], ["CHROME"])
        report = ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(report["action"], "reused")
        self.assertEqual(report["relaunch_count"], 0)
        self.assertEqual(report["state"], "MATCH")
        self.assertEqual(deps.sleeps, [])
        self.assertEqual(deps.stopped, [])
        self.assertEqual(deps.launches, [])
        self.assertEqual(deps.prepared, [])
        self.assertEqual(report["skipped_chrome_pids"], [2])
        self.assertEqual(report["execution_time_limit"], EXECUTION_TIME_LIMIT)
        self.assertEqual(EXECUTION_TIME_LIMIT, "PT1M")

    def test_mismatch_then_match_on_third_probe_does_not_relaunch(self):
        deps = FakeDeps([proc(1, reservation_cmdline())], ["CLOSED", "CLOSED", "CHROME"])
        report = ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(report["action"], "reused")
        self.assertEqual(deps.sleeps, [3, 3])
        self.assertEqual(deps.launches, [])
        self.assertEqual(deps.stopped, [])

    def test_running_reservation_chrome_is_not_killed_or_relaunched(self):
        renderer = proc(11, f'"{DEFAULT_CHROME}" --type=renderer --user-data-dir={PROFILE}')
        other = proc(12, r'"C:\Chrome\chrome.exe" --remote-debugging-port=9333 --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data')
        deps = FakeDeps(
            [proc(10, reservation_cmdline()), renderer, other],
            ["CLOSED", "CLOSED", "CLOSED"],
        )
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(deps.stopped, [])
        self.assertEqual(deps.launches, [])
        self.assertEqual({item.pid for item in deps.processes}, {10, 11, 12})

    def test_not_running_relaunches_without_touching_other_chrome(self):
        other = proc(7, r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data')
        deps = FakeDeps([other], ["CLOSED", "CLOSED", "CLOSED", "CLOSED", "CHROME"])
        report = ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(report["action"], "launched")
        self.assertEqual(report["killed_pids"], [])
        self.assertEqual(deps.stopped, [])
        self.assertEqual(len(deps.launches), 1)
        spec = deps.launches[0]
        self.assertEqual(spec.execution_time_limit, "PT1M")
        self.assertIn("powershell.exe</Command>", spec.xml)
        self.assertNotIn("chrome.exe</Command>", spec.xml)
        self.assertIn("UseShellExecute", spec.xml)
        self.assertNotIn("--restore-last-session", spec.xml)
        self.assertIn("about:blank", spec.xml)
        self.assertIn("--disable-session-crashed-bubble", spec.xml)
        self.assertIn("--hide-crash-restore-bubble", spec.xml)
        self.assertEqual(deps.trace, ["prepare", "launch"])
        self.assertNotIn("PT0S", spec.xml)
        self.assertNotIn("PT72H", spec.xml)
        self.assertIn(7, {item.pid for item in deps.processes})

    def test_other_chrome_on_9227_is_not_killed_and_launch_is_not_retried(self):
        other = proc(
            21,
            r'"C:\Chrome\chrome.exe" --remote-debugging-port=9227 --user-data-dir=C:\Temp\OtherProfile',
        )
        deps = FakeDeps([other], ["CHROME"] * 4, relaunch_process=False)
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(deps.stopped, [])
        self.assertEqual(deps.launches, [])
        self.assertEqual(deps.closed_ports, [])
        self.assertIn(21, {item.pid for item in deps.processes})

    def test_second_ensure_in_the_same_session_does_not_launch_again(self):
        session = EnsureSession()
        first = FakeDeps([], ["CLOSED"] * 5, relaunch_process=False)
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(), deps=first, session=session, timing=fast(post=2))
        self.assertEqual(len(first.launches), 1)
        second = FakeDeps([], ["CLOSED"] * 3, relaunch_process=True)
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(), deps=second, session=session, timing=fast())
        self.assertEqual(second.launches, [])
        self.assertEqual(second.stopped, [])

    def test_profile_marker_in_a_url_is_not_the_reservation_profile(self):
        stranger = proc(
            4,
            r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data '
            r"https://example.invalid/ReservationRuntimeChromeProfile",
        )
        deps = FakeDeps([stranger], ["CLOSED", "CLOSED", "CLOSED", "CLOSED", "CHROME"])
        ensure_reservation_chrome(configuration(), deps=deps, timing=fast())
        self.assertEqual(deps.stopped, [])
        self.assertIn(4, {item.pid for item in deps.processes})

    def test_refuses_a_profile_path_without_the_marker_before_listing_processes(self):
        deps = FakeDeps([proc(1, reservation_cmdline())], ["CHROME"])
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(
                configuration(dedicated_profile_path=r"C:\Users\epuro\AppData\Local\Google\Chrome\User Data"),
                deps=deps, timing=fast(),
            )
        self.assertEqual(deps.launches, [])
        self.assertEqual(deps.stopped, [])

    def test_refuses_a_port_other_than_9227_and_a_non_chrome_executable(self):
        deps = FakeDeps([], ["CLOSED"])
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(cdp_port=9228), deps=deps)
        with self.assertRaisesRegex(CollectorError, FAILURE):
            ensure_reservation_chrome(configuration(chrome_executable=r"C:\Windows\notepad.exe"), deps=deps)
        self.assertEqual(deps.stopped, [])
        self.assertEqual(deps.launches, [])

    def test_quoted_user_data_dir_matches_and_92270_does_not(self):
        quoted = f'"{DEFAULT_CHROME}" --user-data-dir="{PROFILE}" --remote-debugging-port=9227'
        self.assertTrue(is_reservation_chrome(proc(1, quoted), PROFILE))
        self.assertTrue(port_flag_matches(quoted, 9227))
        self.assertFalse(port_flag_matches(quoted.replace("9227", "92270"), 9227))
        backup = reservation_cmdline(profile=PROFILE + "-backup")
        self.assertFalse(is_reservation_chrome(proc(2, backup), PROFILE))

    def test_launch_arguments_do_not_restore_the_recorded_session(self):
        raw = fixture_text("chrome9227_cmdline_after_restart.txt").splitlines()[0]
        tokens = split_windows_cmdline(raw)
        self.assertTrue(tokens[0].lower().endswith("chrome.exe"))
        self.assertIn("--restore-last-session", tokens)
        before = split_windows_cmdline(fixture_text("chrome9227_cmdline_before_restart.txt").splitlines()[0])
        self.assertIn("--restore-last-session", before)
        fresh = launch_argument_list(9227, PROFILE)
        self.assertNotIn("--restore-last-session", fresh)
        self.assertEqual(fresh[-1], "about:blank")
        self.assertIn("--disable-session-crashed-bubble", fresh)
        self.assertIn("--hide-crash-restore-bubble", fresh)
        self.assertNotEqual(tokens[1:], fresh)

        def flag(parts, name):
            for token in parts:
                if token == name or token.startswith(name):
                    return token
            self.fail(name)

        for name in (
            "--remote-debugging-port=",
            "--user-data-dir=",
            "--profile-directory=",
            "--no-first-run",
            "--no-default-browser-check",
        ):
            self.assertEqual(flag(tokens, name), flag(fresh, name))
            self.assertEqual(flag(before, name), flag(fresh, name))

    def test_process_json_accepts_one_object_or_a_list(self):
        one = parse_process_json('{"pid": 4, "name": "chrome.exe", "command_line": "chrome.exe"}')
        many = parse_process_json('[{"ProcessId": 5, "Name": "chrome.exe", "CommandLine": "x"}]')
        self.assertEqual([item.pid for item in one + many], [4, 5])

    def test_cdp_probe_is_only_localhost_version(self):
        self.assertEqual(cdp_version_url(9227), "http://127.0.0.1:9227/json/version")
        seen = {}

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(url, timeout):
            seen["url"] = url
            seen["timeout"] = timeout
            body = json.dumps({
                "Browser": "Chrome/129.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9227/devtools/browser/abc",
            }).encode()
            return Response(body)

        self.assertEqual(probe_cdp(9227, opener=opener), "CHROME")
        self.assertEqual(seen["url"], "http://127.0.0.1:9227/json/version")

        def edge(url, timeout):
            return Response(json.dumps({"Browser": "Edg/129", "webSocketDebuggerUrl": "ws://127.0.0.1:9227/x"}).encode())

        self.assertEqual(probe_cdp(9227, opener=edge), "OTHER")

        def closed(url, timeout):
            raise urllib.error.URLError("refused")

        self.assertEqual(probe_cdp(9227, opener=closed), "CLOSED")
        with self.assertRaisesRegex(CollectorError, FAILURE):
            cdp_version_url(9236)

    def test_shipped_chrome_task_has_no_execution_time_limit(self):
        settings = production_settings()
        text = build_task_xml(settings, enabled=False)
        self.assertIn("<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>", text)
        self.assertIn("powershell.exe</Command>", text)
        self.assertIn("UseShellExecute", text)
        self.assertNotIn("chrome.exe</Command>", text)
        self.assertNotIn("PT0S", text)
        self.assertNotIn("PT72H", text)
        self.assertIn("<Enabled>false</Enabled>", text)
        self.assertEqual(settings.task_name, TASK_NAME)
        self.assertNotEqual(TASK_NAME, LIVE_RESTART_TASK)
        self.assertIn(f"<URI>{TASK_NAME}</URI>", text)
        self.assertNotIn(LIVE_RESTART_TASK, text)
        self.assertIn(DEFAULT_PROFILE, text)
        self.assertNotIn("--restore-last-session", text)
        self.assertIn("about:blank", text)
        self.assertIn("--disable-session-crashed-bubble", text)
        self.assertIn("--hide-crash-restore-bubble", text)
        path = ROOT / "tasks" / "TantanX-CityReservationChrome9227-Restart-Once-v2.xml"
        stored = path.read_bytes()
        self.assertTrue(stored.startswith(b"\xff\xfe"))
        self.assertEqual(stored.decode("utf-16"), text)
        enabled = build_task_xml(settings, enabled=True)
        self.assertIn("<Enabled>true</Enabled>", enabled)
        self.assertIn("<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>", enabled)
        self.assertIn("<Triggers />", enabled)

    def test_select_processes_splits_reservation_from_other_chrome(self):
        processes = [
            proc(1, reservation_cmdline()),
            proc(2, r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data'),
        ]
        reservation, owners, others = select_processes(processes, PROFILE, 9227)
        self.assertEqual(reservation, [1])
        self.assertEqual(owners, [1])
        self.assertEqual(others, [2])
        self.assertEqual(settings_from_configuration(configuration()).port, 9227)

    def test_real_port_process_and_schtasks_are_refused(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("real io")

        target = "src.windows_execution_foundation.cityheaven_reservation_chrome_ensure_v2"
        with mock.patch(f"{target}.subprocess.run", explode), mock.patch.object(urllib.request, "urlopen", explode):
            with self.assertRaises(RealIoBlocked):
                probe_cdp(9227)
            with self.assertRaises(RealIoBlocked):
                windows_list_processes()
            with self.assertRaises(RealIoBlocked):
                windows_stop([32636], PROFILE)
            with self.assertRaises(RealIoBlocked):
                windows_launch(launch_spec(production_settings(), enabled=True))
            with self.assertRaises(RealIoBlocked):
                ensure_reservation_chrome(configuration())
            with self.assertRaises(RealIoBlocked):
                cdp_browser_close(9227)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "WindowsExecutionFoundation" / "cityheaven-daily"
        self.root.mkdir(parents=True)
        self.profile = self.root / "ReservationRuntimeChromeProfile"
        self.profile.mkdir()
        (self.profile / "Local State").write_text("keep", encoding="utf-8")
        self._patch = mock.patch(
            "src.windows_execution_foundation.cityheaven_daily_metrics.DEFAULT_RUNTIME",
            self.root,
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def config(self):
        return configuration(dedicated_profile_path=str(self.profile))

    def settings(self):
        return settings_from_configuration(self.config())

    def test_leftover_matching_profile_is_reused_and_always_closed(self):
        deps = FakeDeps(
            [proc(1, reservation_cmdline(profile=str(self.profile)))],
            ["CHROME", "CHROME"],
            profile=str(self.profile),
        )
        session = reservation_chrome_session(
            self.config(), runtime=self.root, job="daily", deps=deps, timing=fast(),
        )
        lease = session.__enter__()
        self.assertEqual(lease.report["action"], "reused")
        self.assertEqual(deps.launches, [])
        self.assertEqual(deps.closed_ports, [])
        session.__exit__(None, None, None)
        self.assertEqual(deps.closed_ports, [9227])
        self.assertEqual(lease.release_report["job"], "daily")
        self.assertTrue(lease.release_report["profile_kept"])
        self.assertFalse((self.root / "locks").exists())
        self.assertEqual((self.profile / "Local State").read_text(encoding="utf-8"), "keep")

    def test_each_run_closes_so_the_next_run_launches_again(self):
        deps = FakeDeps(
            [],
            ["CLOSED", "CLOSED", "CLOSED", "CHROME", "CHROME", "CLOSED", "CLOSED", "CLOSED", "CHROME", "CHROME"],
            profile=str(self.profile),
        )
        for job in ("daily", "retry"):
            session = reservation_chrome_session(
                self.config(), runtime=self.root, job=job, deps=deps, timing=fast(post=1),
            )
            session.__enter__()
            session.__exit__(None, None, None)
        self.assertEqual(len(deps.launches), 2)
        self.assertEqual(deps.closed_ports, [9227, 9227])
        self.assertFalse((self.root / "locks").exists())
        self.assertEqual((self.profile / "Local State").read_text(encoding="utf-8"), "keep")

    def test_wrong_profile_on_9227_is_not_closed(self):
        other = proc(
            21,
            r'"C:\Chrome\chrome.exe" --remote-debugging-port=9227 --user-data-dir=C:\Temp\OtherProfile',
        )
        deps = FakeDeps([other], ["CHROME"] * 4, profile=str(self.profile))
        session = reservation_chrome_session(
            self.config(), runtime=self.root, job="daily", deps=deps, timing=fast(),
        )
        lease = session.__enter__()
        self.assertIsNotNone(lease.ensure_error)
        self.assertEqual(deps.launches, [])
        session.__exit__(None, None, None)
        self.assertEqual(deps.closed_ports, [])
        self.assertEqual(deps.stopped, [])
        self.assertIn(21, {item.pid for item in deps.processes})

    def test_close_falls_back_to_reservation_pids_and_skips_other_chrome(self):
        other = proc(9, r'"C:\Chrome\chrome.exe" --user-data-dir=C:\Users\epuro\AppData\Local\Google\Chrome\User Data')
        renderer = proc(2, f'"{DEFAULT_CHROME}" --type=renderer --user-data-dir={self.profile}')
        deps = FakeDeps(
            [proc(1, reservation_cmdline(profile=str(self.profile))), renderer, other],
            ["CHROME"],
            graceful=False,
            profile=str(self.profile),
        )
        report = close_reservation_chrome(self.settings(), deps, timing=fast())
        self.assertEqual(deps.closed_ports, [9227])
        self.assertEqual(deps.stopped, [[1, 2]])
        self.assertIn(9, {item.pid for item in deps.processes})
        self.assertTrue(report["profile_kept"])
        self.assertEqual(report["graceful"], True)
        self.assertEqual((self.profile / "Local State").read_text(encoding="utf-8"), "keep")

    def test_close_does_not_send_browser_close_to_a_foreign_chrome(self):
        other = proc(
            21,
            r'"C:\Chrome\chrome.exe" --remote-debugging-port=9227 --user-data-dir=C:\Temp\OtherProfile',
        )
        deps = FakeDeps([other], ["CHROME"], profile=str(self.profile))
        report = close_reservation_chrome(self.settings(), deps, timing=fast())
        self.assertEqual(deps.closed_ports, [])
        self.assertEqual(deps.stopped, [])
        self.assertIn(21, {item.pid for item in deps.processes})
        self.assertTrue(report["profile_kept"])
        self.assertFalse(report["graceful"])


class TaskXmlTests(unittest.TestCase):
    def test_daily_and_retry_tasks_point_at_the_logged_wrappers(self):
        daily = (ROOT / "tasks" / "TantanManagementOS-CityHeavenDaily-1130-v2.xml").read_bytes()
        retry = (ROOT / "tasks" / "TantanManagementOS-CityHeavenUpdateCountRetry-1430-v2.xml").read_bytes()
        self.assertTrue(daily.startswith(b"\xff\xfe"))
        self.assertTrue(retry.startswith(b"\xff\xfe"))
        daily_text = daily.decode("utf-16")
        retry_text = retry.decode("utf-16")
        self.assertIn("run-cityheaven-daily-metrics-logged.py", daily_text)
        self.assertNotIn("run-cityheaven-daily-metrics.py\"", daily_text)
        self.assertIn("11:30:00+09:00", daily_text)
        self.assertIn("run-cityheaven-update-count-retry-logged.py", retry_text)
        self.assertNotIn("run-cityheaven-update-count-retry.py\"", retry_text)
        self.assertIn("14:30:00+09:00", retry_text)
        self.assertNotIn("install-cityheaven-daily-task.ps1", daily_text + retry_text)
        for text in (daily_text, retry_text):
            self.assertIn("<LogonType>InteractiveToken</LogonType>", text)
            self.assertIn("pythonw.exe", text)
        self.assertIn("closes Chrome", daily_text)
        self.assertIn("closes Chrome", retry_text)
        self.assertNotIn("holders", daily_text + retry_text)
        self.assertNotIn("refcount", (daily_text + retry_text).lower())


if __name__ == "__main__":
    unittest.main()
