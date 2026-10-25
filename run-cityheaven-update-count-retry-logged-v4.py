"""Logged launcher for the 14:30 CityHeaven update-count retry v4.

Logs go to the v4 runtime (``cityheaven-daily-v4\\logs``, or ``--v4-runtime``).
"""
import sys
from pathlib import Path

from src.windows_execution_foundation.cityheaven_daily_metrics_v4 import v4_runtime_from_argv
from src.windows_execution_foundation.cityheaven_logged_launch_v3 import run_logged_v3


def main(argv: list[str] | None = None, runner=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    runtime = v4_runtime_from_argv(args)
    child = Path(__file__).with_name("run-cityheaven-update-count-retry-v4.py")
    if runner is None:
        return run_logged_v3(child, args, runtime=runtime)
    return run_logged_v3(child, args, runtime=runtime, runner=runner)


if __name__ == "__main__":
    raise SystemExit(main())
