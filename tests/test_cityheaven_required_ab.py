from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class CityHeavenRequiredABTests(unittest.TestCase):
    def test_staff_guidance_matrix(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            ["node", "--test", str(root / "tests/cityheaven_required_ab.test.js")],
            cwd=root, env={**os.environ, "CITYHEAVEN_TEST_PYTHON": sys.executable},
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
