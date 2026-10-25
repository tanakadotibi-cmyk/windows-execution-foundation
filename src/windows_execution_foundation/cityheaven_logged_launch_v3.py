"""Run a CityHeaven job and keep stdout, stderr, and any wrapper traceback.

Same contract as ``run_logged`` in ``cityheaven_logged_launch_v2``. The
scheduled task uses pythonw, which sets ``sys.stdout`` and ``sys.stderr`` to
``None``. Captured output is written only when that stream exists. Saving the
log and relaying captured output are wrapped so neither can replace the child
exit code. The log ends with ``launcher_exit_code=<n>``. If the primary log
cannot be saved, the same body is written to
``logs/launcher_error_<timestamp>.log`` under the working directory (the
repository root when ``cwd`` is omitted).
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


def _fallback_log_path(directory: Path, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    path = directory / f"launcher_error_{stamp}.log"
    suffix = 1
    while path.exists():
        path = directory / f"launcher_error_{stamp}_{suffix}.log"
        suffix += 1
    return path


def _save_log(root: Path, moment: datetime, body: str, cwd: Path | None) -> None:
    try:
        path = log_path(root / "logs", moment)
        path.write_text(body, encoding="utf-8", newline="\n")
        return
    except Exception:
        pass
    try:
        anchor = cwd if cwd is not None else Path(__file__).resolve().parents[2]
        path = _fallback_log_path(anchor / "logs", moment)
        path.write_text(body, encoding="utf-8", newline="\n")
    except Exception:
        return


def _write_if_present(stream: Any, text: str) -> None:
    if not text or stream is None:
        return
    stream.write(text)


def _relay_captured(stdout: str, stderr: str, traced: str) -> None:
    try:
        _write_if_present(sys.stdout, stdout)
    except Exception:
        pass
    try:
        _write_if_present(sys.stderr, stderr)
        _write_if_present(sys.stderr, traced)
    except Exception:
        return


def run_logged_v3(
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
        f"launcher_exit_code={code}\n",
    ])
    try:
        _save_log(root, moment, body, cwd)
    except Exception:
        pass
    try:
        _relay_captured(stdout, stderr, traced)
    except Exception:
        pass
    return code


def json_argv(args: Sequence[str]) -> str:
    return " ".join(args)
