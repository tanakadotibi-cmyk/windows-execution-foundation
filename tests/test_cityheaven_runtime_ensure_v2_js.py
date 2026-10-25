from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

os.environ["CITYHEAVEN_UNIT_TEST"] = "1"

ROOT = Path(__file__).resolve().parents[1]


class RuntimeEnsureScriptTests(unittest.TestCase):
    def test_patience_and_command_line_trust_are_mocked(self):
        script = ROOT / "tests" / "test_cityheaven_reservation_runtime_ensure_v2.js"
        source = (ROOT / "src" / "windows_execution_foundation" / "cityheaven_reservation_runtime_ensure_v2.js").read_text(encoding="utf-8")
        self.assertIn("cityheaven_reservation_runtime_reader.js", source)
        self.assertNotIn("taskkill", source)
        self.assertNotIn("ReservationRuntimeChromeProfile", source)
        completed = subprocess.run(
            ["node", str(script)], cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
