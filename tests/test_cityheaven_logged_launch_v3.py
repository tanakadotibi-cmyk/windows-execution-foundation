"""pythonw (stdout/stderr is None) must not replace the child exit code."""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.windows_execution_foundation.cityheaven_logged_launch_v3 import run_logged_v3


def _child(directory: Path, code: int) -> Path:
    path = directory / f"child_exit_{code}.py"
    path.write_text(
        "import sys\n"
        "print('child-stdout')\n"
        "print('child-stderr', file=sys.stderr)\n"
        f"raise SystemExit({code})\n",
        encoding="utf-8",
    )
    return path


def _logs(runtime: Path) -> list[Path]:
    return sorted((runtime / "logs").glob("*.log"))


class RunLoggedV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_streams)
        self.work = Path(self._tmp.name)
        self.runtime = self.work / "runtime"

    def _restore_streams(self) -> None:
        sys.stdout = self._stdout
        sys.stderr = self._stderr

    def _run(self, code: int) -> int:
        sys.stdout = None
        sys.stderr = None
        return run_logged_v3(
            _child(self.work, code),
            [],
            runtime=self.runtime,
            cwd=self.work,
        )

    def test_none_streams_child_prints_and_exits_0(self) -> None:
        code = self._run(0)
        self.assertEqual(code, 0)
        logs = _logs(self.runtime)
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("child-stdout", text)
        self.assertIn("child-stderr", text)
        self.assertIn("exit_code=0\n", text)
        self.assertTrue(text.endswith("launcher_exit_code=0\n"))
        self.assertEqual(list((self.work / "logs").glob("launcher_error_*.log")), [])

    def test_none_streams_child_exits_3(self) -> None:
        code = self._run(3)
        self.assertEqual(code, 3)
        logs = _logs(self.runtime)
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("child-stdout", text)
        self.assertIn("exit_code=3\n", text)
        self.assertTrue(text.endswith("launcher_exit_code=3\n"))

    def test_stream_write_errors_do_not_change_exit_code(self) -> None:
        class Boom:
            def write(self, _text: str) -> None:
                raise OSError("closed stream")

        sys.stdout = Boom()
        sys.stderr = Boom()
        code = run_logged_v3(_child(self.work, 3), [], runtime=self.runtime, cwd=self.work)
        self.assertEqual(code, 3)
        text = _logs(self.runtime)[0].read_text(encoding="utf-8")
        self.assertTrue(text.endswith("launcher_exit_code=3\n"))

    def test_relays_when_streams_exist(self) -> None:
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        code = run_logged_v3(_child(self.work, 0), [], runtime=self.runtime, cwd=self.work)
        self.assertEqual(code, 0)
        self.assertIn("child-stdout", sys.stdout.getvalue())
        self.assertIn("child-stderr", sys.stderr.getvalue())

    def test_primary_log_failure_falls_back_and_keeps_child_code(self) -> None:
        sys.stdout = None
        sys.stderr = None
        blocker = self.work / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        code = run_logged_v3(
            _child(self.work, 0),
            [],
            runtime=blocker / "runtime",
            cwd=self.work,
        )
        self.assertEqual(code, 0)
        fallbacks = sorted((self.work / "logs").glob("launcher_error_*.log"))
        self.assertEqual(len(fallbacks), 1)
        text = fallbacks[0].read_text(encoding="utf-8")
        self.assertIn("child-stdout", text)
        self.assertTrue(text.endswith("launcher_exit_code=0\n"))

    def test_log_write_failures_still_return_child_code(self) -> None:
        sys.stdout = None
        sys.stderr = None
        original = Path.write_text

        def fail_logs(self: Path, *args: object, **kwargs: object) -> int:
            if self.suffix == ".log":
                raise OSError("log directory is not writable")
            return original(self, *args, **kwargs)

        with mock.patch.object(Path, "write_text", fail_logs):
            code = run_logged_v3(_child(self.work, 3), [], runtime=self.runtime, cwd=self.work)
        self.assertEqual(code, 3)

    def test_launcher_calls_v3_with_v2_child(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "logged_v3_launcher",
            ROOT / "run-cityheaven-daily-metrics-logged-v3.py",
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        captured: dict[str, object] = {}

        def fake(child: Path, argv: list[str]) -> int:
            captured["child"] = child
            captured["argv"] = list(argv)
            return 0

        with mock.patch.object(module, "run_logged_v3", fake):
            with mock.patch.object(sys, "argv", ["run-cityheaven-daily-metrics-logged-v3.py", "--runtime", "rt"]):
                self.assertEqual(module.main(), 0)
        child = captured["child"]
        self.assertIsInstance(child, Path)
        assert isinstance(child, Path)
        self.assertEqual(child.name, "run-cityheaven-daily-metrics-v2.py")
        self.assertEqual(captured["argv"], ["--runtime", "rt"])


if __name__ == "__main__":
    unittest.main()
