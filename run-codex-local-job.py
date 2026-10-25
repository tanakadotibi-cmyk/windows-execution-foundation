from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from windows_execution_foundation.codex_local_job import main


if __name__ == "__main__":
    raise SystemExit(main())
