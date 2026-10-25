"""Offline tests for the v4 logged launchers."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.offline_guard import install

install()

ROOT = Path(__file__).resolve().parents[1]


def _load(filename: str):
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_").replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoggedLauncherV4Tests(unittest.TestCase):
    def _run(self, filename: str, child_name: str):
        module = _load(filename)
        seen = {}

        def runner(cmd, **_kwargs):
            seen["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"status": "PASS"}\n', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "cityheaven-daily-v4"
            code = module.main(
                ["--date", "2020-05-05", "--dry-run", "--v4-runtime", str(runtime)],
                runner=runner,
            )
            logs = list((runtime / "logs").glob("*.log"))
            text = logs[0].read_text(encoding="utf-8") if logs else ""
        self.assertEqual(code, 0)
        self.assertEqual(len(logs), 1)
        self.assertIn("launcher_exit_code=0", text)
        self.assertIn(child_name, text)
        self.assertIn("--dry-run", text)
        self.assertTrue(any(child_name in part for part in seen["cmd"]))
        self.assertFalse(any("logged-v3" in part or part.endswith("logged.py") for part in seen["cmd"]))
        self.assertNotIn("cityheaven-daily\\logs", text)

    def test_daily_launcher_logs_under_v4(self):
        self._run("run-cityheaven-daily-metrics-logged-v4.py", "run-cityheaven-daily-metrics-v4.py")

    def test_retry_launcher_logs_under_v4(self):
        self._run("run-cityheaven-update-count-retry-logged-v4.py", "run-cityheaven-update-count-retry-v4.py")


if __name__ == "__main__":
    unittest.main()
