from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CityHeavenLauncherTests(unittest.TestCase):
    def test_configured_python_can_show_help_without_pythonpath(self):
        configuration = json.loads((ROOT / "local" / "config.json").read_text(encoding="utf-8"))
        configured_python = configuration["python_executable"]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)

        completed = subprocess.run(
            [configured_python, str(ROOT / "run-cityheaven-reservation.py"), "--help"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage:", completed.stdout.lower())
        self.assertIn("schedule", completed.stdout)
        self.assertIn("run-once", completed.stdout)


if __name__ == "__main__":
    unittest.main()
