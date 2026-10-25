"""Run a CityHeaven job and keep stdout, stderr, and any wrapper traceback.

The scheduled task uses pythonw, which has no console. The log directory is
``%LOCALAPPDATA%\\WindowsExecutionFoundation\\cityheaven-daily\\logs``
(or ``--runtime`` / ``logs`` when that argument is present). The child exit
code is returned unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
Runner = Callable[..., Any]


def default_runtime() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "WindowsExecutionFoundation" / "cityheaven-daily"
    return Path(__file__).resolve().parents[2] / "local" / "WindowsExecutionFoundation" / "cityheaven-daily"


def runtime_from_argv(argv: Sequence[str]) -> Path:
    args = list(argv)
    if "--runtime" in args:
        index = args.index("--runtime")
        if index + 1 < len(args):
            return Path(args[index + 1])
    return default_runtime()


def log_path(directory: Path, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    path = directory / f"{stamp}.log"
    suffix = 1
    while path.exists():
        path = directory / f"{stamp}_{suffix}.log"
        suffix += 1
    return path


def run_logged(
    child: Path,
    argv: Sequence[str] | None = None,
    *,
    runtime: Path | None = None,
    now: datetime | None = None,
    runner: Runner = subprocess.run,
    cwd: Path | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = runtime if runtime is not None else runtime_from_argv(args)
    moment = now or datetime.now(JST)
    path = log_path(root / "logs", moment)
    code = 1
    stdout = ""
    stderr = ""
    traced = ""
    try:
        completed = runner(
            [sys.executable, str(child), *args],
            cwd=str(cwd or Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except Exception:
        traced = traceback.format_exc()
        code = 1
    body = "".join([
        f"child={child.name}\n",
        f"exit_code={code}\n",
        "argv=" + json_argv(args) + "\n",
        "--- stdout ---\n",
        stdout if stdout.endswith("\n") or not stdout else stdout + "\n",
        "--- stderr ---\n",
        stderr if stderr.endswith("\n") or not stderr else stderr + "\n",
        "--- traceback ---\n",
        traced,
    ])
    path.write_text(body, encoding="utf-8", newline="\n")
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    if traced:
        sys.stderr.write(traced)
    return code


def json_argv(args: Sequence[str]) -> str:
    return " ".join(args)
