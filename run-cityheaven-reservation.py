import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from windows_execution_foundation.cityheaven_reservation import main

raise SystemExit(main())
