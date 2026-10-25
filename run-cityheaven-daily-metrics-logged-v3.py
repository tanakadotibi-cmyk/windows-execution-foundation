import sys
from pathlib import Path

from src.windows_execution_foundation.cityheaven_logged_launch_v3 import run_logged_v3


def main() -> int:
    child = Path(__file__).with_name("run-cityheaven-daily-metrics-v2.py")
    return run_logged_v3(child, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
