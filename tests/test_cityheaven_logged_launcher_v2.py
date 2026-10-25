from __future__ import annotations

import os
import subprocess
import unittest
from datetime import datetime

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from src.windows_execution_foundation.cityheaven_logged_launch_v2 import run_logged

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]


class Completed:
    def __init__(self, code, stdout, stderr):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class LoggedLaunchTests(unittest.TestCase):
    def test_log_contains_streams_and_preserves_exit_code(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            child = runtime / "child.py"

            def runner(args, **kwargs):
                self.assertEqual(args[1], str(child))
                self.assertEqual(args[2:], ["--date", "2026-10-20"])
                self.assertIn("capture_output", kwargs)
                return Completed(20, '{"status": "FAILED"}\n', "CITYHEAVEN_ENSURE:{}\n")

            code = run_logged(
                child, ["--date", "2026-10-20"], runtime=runtime,
                now=datetime(2026, 10, 21, 11, 30, 1, tzinfo=JST), runner=runner,
            )
            self.assertEqual(code, 20)
            log = (runtime / "logs" / "20261021_113001.log").read_text(encoding="utf-8")
            self.assertIn("exit_code=20", log)
            self.assertIn('{"status": "FAILED"}', log)
            self.assertIn("CITYHEAVEN_ENSURE:{}", log)
            self.assertIn("--- traceback ---\n", log)
            self.assertTrue(log.strip().endswith("--- traceback ---") or "Traceback" not in log.split("--- traceback ---", 1)[1])

    def test_runner_exception_is_logged_and_returns_1(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)

            def runner(args, **kwargs):
                raise OSError("spawn failed")

            code = run_logged(
                runtime / "missing.py", [], runtime=runtime,
                now=datetime(2026, 10, 21, 14, 30, tzinfo=JST), runner=runner,
            )
            self.assertEqual(code, 1)
            log = (runtime / "logs" / "20261021_143000.log").read_text(encoding="utf-8")
            self.assertIn("exit_code=1", log)
            self.assertIn("OSError: spawn failed", log)
            self.assertIn("--- traceback ---", log)

    def test_wrapper_scripts_point_at_the_v2_children(self):
        daily = (ROOT / "run-cityheaven-daily-metrics-logged.py").read_text(encoding="utf-8")
        retry = (ROOT / "run-cityheaven-update-count-retry-logged.py").read_text(encoding="utf-8")
        self.assertIn("run-cityheaven-daily-metrics-v2.py", daily)
        self.assertIn("run-cityheaven-update-count-retry-v2.py", retry)
        self.assertNotIn("install-cityheaven-daily-task.ps1", daily + retry)

    def test_real_child_exit_code_is_preserved(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            child = runtime / "child.py"
            child.write_text("import sys\nprint('out')\nprint('err', file=sys.stderr)\nraise SystemExit(20)\n", encoding="utf-8")
            code = run_logged(
                child, ["--date", "2026-10-20"], runtime=runtime,
                now=datetime(2026, 10, 21, 11, 30, 2, tzinfo=JST), runner=subprocess.run,
            )
            self.assertEqual(code, 20)
            log = (runtime / "logs" / "20261021_113002.log").read_text(encoding="utf-8")
            self.assertIn("exit_code=20", log)
            self.assertIn("out", log)
            self.assertIn("err", log)


if __name__ == "__main__":
    unittest.main()
