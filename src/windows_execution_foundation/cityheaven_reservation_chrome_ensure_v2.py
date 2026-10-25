"""Start this job's Chrome on port 9227, then close it when the run ends.

The 11:30 daily run and the 14:30 retry are the same job and do not overlap, so
both use port 9227 and ``ReservationRuntimeChromeProfile``. There is no shared
lock or refcount. Each run launches that Chrome when it is not already up. A
leftover process is reused only after the command line shows that profile, and
``finally`` still closes it on success, error, or a block. Close is CDP
``Browser.close``, then ``taskkill`` of processes whose ``--user-data-dir`` is
that profile. The profile directory is kept. Other Chrome processes are never
signalled.

A launch does not restore previous tabs. The command line has no
``--restore-last-session``; it opens ``about:blank`` and hides the crash-restore
bubble. Immediately before that launch, while this Chrome is stopped,
``Default\\Preferences`` is backed up once and set so the next start is
``about:blank`` with a normal exit. A leftover Chrome is not edited while it
is running.

The launch task is a one-shot. Its action starts Chrome with ``UseShellExecute``
and exits, so Task Scheduler does not hold the browser open.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol
from xml.sax.saxutils import escape

from .cityheaven_daily_metrics import CREATE_NO_WINDOW, CollectorError

PROFILE_DIR_MARKER = "ReservationRuntimeChromeProfile"
CDP_PORT = 9227
DEFAULT_CHROME = r"C:\Users\epuro\AppData\Local\Google\Chrome\Application\chrome.exe"
DEFAULT_PROFILE = r"C:\Users\epuro\CityHeavenAI\ReservationRuntimeChromeProfile"
DEFAULT_USER_SID = "S-1-5-21-3349146081-1340512296-21348510-1002"
# Live one-shot that already started reservation Chrome. schtasks /Create /F on this
# name would replace that task, so v2 registers a different task.
LIVE_RESTART_TASK = r"\TantanX-CityReservationChrome9227-Restart-Once"
TASK_NAME = r"\TantanX-CityReservationChrome9227-Ensure-V2"
# Caps the detached launcher process only. Chrome is not a child of the task.
EXECUTION_TIME_LIMIT = "PT1M"
FAILURE = "CDP_PORT_RUNTIME_MISMATCH"
LAUNCHER = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
LAUNCH_TAIL = (
    "--profile-directory=Default",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "about:blank",
)
# 4 opens session.startup_urls. 5 would open the new tab page instead of about:blank.
RESTORE_ON_STARTUP = 4
START_URL = "about:blank"
PREFS_BACKUP_NAME = "Preferences.before-norestore"
PREFS_BUSY = "CHROME_PREFERENCES_BUSY"
PREFS_INVALID = "CHROME_PREFERENCES_INVALID"


class RealIoBlocked(RuntimeError):
    """Raised when a unit test reaches a real process, port, or scheduled task."""


def real_io_blocked(kind: str) -> None:
    if os.environ.get("CITYHEAVEN_UNIT_TEST") == "1":
        raise RealIoBlocked(f"unit test blocked real {kind}")


@dataclass
class ProcessInfo:
    pid: int
    name: str
    command_line: str


@dataclass
class Timing:
    verify_attempts: int = 3
    verify_spacing_seconds: float = 3
    post_launch_attempts: int = 8
    post_launch_spacing_seconds: float = 2


@dataclass
class EnsureSession:
    """One relaunch per job process. A second call may verify, not launch again."""

    relaunched: bool = False


@dataclass
class LaunchSpec:
    task_name: str
    chrome_executable: str
    arguments: str
    user_id: str
    execution_time_limit: str
    enabled: bool
    xml: str


@dataclass
class ChromeSettings:
    chrome_executable: str
    profile: str
    port: int
    user_id: str
    task_name: str = TASK_NAME


class EnsureDeps(Protocol):
    def list_processes(self) -> list[ProcessInfo]: ...

    def probe_cdp(self, port: int) -> str: ...

    def sleep(self, seconds: float) -> None: ...

    def stop(self, pids: list[int]) -> None: ...

    def launch(self, spec: LaunchSpec) -> None: ...

    def prepare_profile(self, profile: str) -> None: ...

    def browser_close(self, port: int) -> None: ...


@dataclass
class ChromeLease:
    """One run of this job's Chrome. ``finally`` always closes it."""

    job: str
    report: dict[str, Any] | None = None
    ensure_error: CollectorError | None = None
    release_report: dict[str, Any] | None = None
    released: bool = False


def norm_path(value: str) -> str:
    text = str(value or "").strip().strip('"').replace("/", "\\")
    prefix = "\\\\" if text.startswith("\\\\") else ""
    body = text[2:] if prefix else text
    while "\\\\" in body:
        body = body.replace("\\\\", "\\")
    return (prefix + body).rstrip("\\").lower()


def split_windows_cmdline(command_line: str) -> list[str]:
    tokens: list[str] = []
    buf: list[str] = []
    quote = False
    for char in command_line or "":
        if char == '"':
            quote = not quote
            continue
        if not quote and char in " \t":
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(char)
    if buf:
        tokens.append("".join(buf))
    return tokens


def user_data_dir_from_cmdline(command_line: str) -> str | None:
    parts = split_windows_cmdline(command_line)
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            return part.split("=", 1)[1]
        if part == "--user-data-dir" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def port_flag_matches(command_line: str, port: int) -> bool:
    return f"--remote-debugging-port={int(port)}" in split_windows_cmdline(command_line)


def path_name(value: str) -> str:
    """Last path segment, accepting Windows separators on any host."""
    text = str(value or "").strip().strip('"').replace("/", "\\").rstrip("\\")
    return text.rsplit("\\", 1)[-1].lower()


def is_chrome_image(proc: ProcessInfo) -> bool:
    name = (proc.name or "").strip().lower()
    if name == "chrome.exe":
        return True
    parts = split_windows_cmdline(proc.command_line)
    if not parts:
        return False
    return path_name(parts[0]) == "chrome.exe"


def is_reservation_chrome(proc: ProcessInfo, profile: str) -> bool:
    """True only for chrome.exe whose --user-data-dir is exactly the reservation profile."""
    if not isinstance(proc.pid, int) or proc.pid <= 0:
        return False
    if not is_chrome_image(proc):
        return False
    directory = user_data_dir_from_cmdline(proc.command_line)
    if not directory:
        return False
    normalized = norm_path(directory)
    if PROFILE_DIR_MARKER.lower() not in normalized:
        return False
    return normalized == norm_path(profile)


def launch_argument_list(port: int, profile: str) -> list[str]:
    folder = str(profile).rstrip("\\/")
    return [
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={folder}",
        *LAUNCH_TAIL,
    ]


def launch_arguments(port: int, profile: str) -> str:
    arguments = launch_argument_list(port, profile)
    if "--restore-last-session" in arguments or arguments[-1] != START_URL:
        raise CollectorError(FAILURE)
    return " ".join(arguments)


def profile_preferences_paths(profile: str) -> tuple[str, str, str]:
    """Return ``Default``, ``Preferences``, and ``Preferences.before-norestore``."""
    separator = "\\" if "\\" in profile else "/"
    default_dir = separator.join([str(profile).rstrip("\\/"), "Default"])
    preferences = separator.join([default_dir, "Preferences"])
    backup = separator.join([default_dir, PREFS_BACKUP_NAME])
    return default_dir, preferences, backup


def _load_preferences(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as handle:
            raw = handle.read()
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(PREFS_INVALID) from exc
    except OSError as exc:
        raise CollectorError(FAILURE) from exc
    if not isinstance(value, dict):
        raise CollectorError(PREFS_INVALID)
    return value


def _apply_norestore(data: dict[str, Any]) -> None:
    session = data.get("session")
    if not isinstance(session, dict):
        session = {}
        data["session"] = session
    session["restore_on_startup"] = RESTORE_ON_STARTUP
    session["startup_urls"] = [START_URL]
    block = data.get("profile")
    if not isinstance(block, dict):
        block = {}
        data["profile"] = block
    block["exit_type"] = "Normal"
    block["exited_cleanly"] = True
    if session.get("startup_urls") != [START_URL] or session.get("restore_on_startup") != RESTORE_ON_STARTUP:
        raise CollectorError(FAILURE)
    if any("cityheaven" in str(url).lower() for url in session["startup_urls"]):
        raise CollectorError(FAILURE)


def _write_preferences(path: str, data: dict[str, Any]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise CollectorError(FAILURE) from exc


def prepare_profile_norestore(profile: str, processes: list[ProcessInfo]) -> dict[str, Any]:
    """Back up ``Default\\Preferences`` once and disable session restore.

    Chrome for this profile must already be stopped. The backup
    ``Preferences.before-norestore`` is kept when it already exists. No page
    is opened. Unrelated preference keys are left in place.
    """
    folder = str(profile).rstrip("\\/")
    if PROFILE_DIR_MARKER.lower() not in norm_path(folder):
        raise CollectorError(FAILURE)
    if any(is_reservation_chrome(proc, folder) for proc in processes):
        raise CollectorError(PREFS_BUSY)
    default_dir, preferences, backup = profile_preferences_paths(folder)
    backup_written = False
    if os.path.isfile(preferences):
        if not os.path.exists(backup):
            try:
                shutil.copy2(preferences, backup)
            except OSError as exc:
                raise CollectorError(FAILURE) from exc
            backup_written = True
        data = _load_preferences(preferences)
    else:
        try:
            os.makedirs(default_dir, exist_ok=True)
        except OSError as exc:
            raise CollectorError(FAILURE) from exc
        data = {}
    _apply_norestore(data)
    _write_preferences(preferences, data)
    return {
        "preferences": preferences,
        "backup": backup,
        "backup_written": backup_written,
        "restore_on_startup": RESTORE_ON_STARTUP,
        "startup_urls": [START_URL],
        "exit_type": "Normal",
        "exited_cleanly": True,
    }


def cdp_version_url(port: int) -> str:
    if int(port) != CDP_PORT:
        raise CollectorError(FAILURE)
    return f"http://127.0.0.1:{CDP_PORT}/json/version"


def classify_cdp(cdp: str, port_owner_count: int, reservation_count: int) -> str:
    if cdp == "CHROME" and port_owner_count > 0:
        return "MATCH"
    if cdp == "CLOSED" and reservation_count == 0 and port_owner_count == 0:
        return "ABSENT"
    return "MISMATCH"


def select_processes(processes: list[ProcessInfo], profile: str, port: int) -> tuple[list[int], list[int], list[int]]:
    """Return (reservation pids, port-owner pids, other chrome pids)."""
    reservation: list[int] = []
    owners: list[int] = []
    others: list[int] = []
    for proc in processes:
        if is_reservation_chrome(proc, profile):
            reservation.append(proc.pid)
            if port_flag_matches(proc.command_line, port):
                owners.append(proc.pid)
        elif is_chrome_image(proc) and proc.pid > 0:
            others.append(proc.pid)
    return reservation, owners, others


def _xml_text(value: str) -> str:
    if any(char in value for char in "<>&"):
        raise CollectorError(FAILURE)
    return escape(value, {'"': "&quot;"})


def detached_launcher_arguments(settings: ChromeSettings) -> str:
    """PowerShell that Start-Process's Chrome and returns. The task does not stay running."""
    chrome = settings.chrome_executable
    arguments = launch_arguments(settings.port, settings.profile)
    if any(char in chrome + arguments for char in "'\"<>&"):
        raise CollectorError(FAILURE)
    script = (
        "$s=New-Object System.Diagnostics.ProcessStartInfo; "
        f"$s.FileName='{chrome}'; "
        f"$s.Arguments='{arguments}'; "
        "$s.UseShellExecute=$true; "
        "[void][System.Diagnostics.Process]::Start($s)"
    )
    return f'-NoProfile -NonInteractive -WindowStyle Hidden -Command "{script}"'


def build_task_xml(settings: ChromeSettings, *, enabled: bool) -> str:
    """One-shot task: start Chrome detached, then the launcher exits. Limit is PT1M."""
    if settings.port != CDP_PORT:
        raise CollectorError(FAILURE)
    if path_name(settings.chrome_executable) != "chrome.exe":
        raise CollectorError(FAILURE)
    if PROFILE_DIR_MARKER.lower() not in norm_path(settings.profile):
        raise CollectorError(FAILURE)
    enabled_text = "true" if enabled else "false"
    launcher_arguments = detached_launcher_arguments(settings)
    # Same principal and idle settings as the original Restart-Once task.
    # Chrome is not the task command, so the task does not hold the browser open.
    return "\r\n".join([
        '<?xml version="1.0" encoding="UTF-16"?>',
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
        "  <RegistrationInfo>",
        f"    <URI>{_xml_text(settings.task_name)}</URI>",
        "    <Description>One-shot detached launch of the daily job Chrome on port 9227 (ReservationRuntimeChromeProfile). The 11:30 run and 14:30 retry share this profile and do not overlap. The launch opens about:blank and does not restore previous tabs. The task exits after Start-Process. Each run closes Chrome. ExecutionTimeLimit PT1M applies to the launcher only.</Description>",
        "  </RegistrationInfo>",
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{_xml_text(settings.user_id)}</UserId>",
        "      <LogonType>InteractiveToken</LogonType>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>",
        f"    <ExecutionTimeLimit>{EXECUTION_TIME_LIMIT}</ExecutionTimeLimit>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        f"    <Enabled>{enabled_text}</Enabled>",
        "    <IdleSettings>",
        "      <Duration>PT10M</Duration>",
        "      <WaitTimeout>PT1H</WaitTimeout>",
        "      <StopOnIdleEnd>true</StopOnIdleEnd>",
        "      <RestartOnIdle>false</RestartOnIdle>",
        "    </IdleSettings>",
        "    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
        "  </Settings>",
        "  <Triggers />",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{_xml_text(LAUNCHER)}</Command>",
        f"      <Arguments>{_xml_text(launcher_arguments)}</Arguments>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
        "",
    ])


def launch_spec(settings: ChromeSettings, *, enabled: bool = True) -> LaunchSpec:
    xml = build_task_xml(settings, enabled=enabled)
    if f"<ExecutionTimeLimit>{EXECUTION_TIME_LIMIT}</ExecutionTimeLimit>" not in xml:
        raise CollectorError(FAILURE)
    return LaunchSpec(
        task_name=settings.task_name,
        chrome_executable=settings.chrome_executable,
        arguments=launch_arguments(settings.port, settings.profile),
        user_id=settings.user_id,
        execution_time_limit=EXECUTION_TIME_LIMIT,
        enabled=enabled,
        xml=xml,
    )


def settings_from_configuration(configuration: dict[str, Any]) -> ChromeSettings:
    try:
        city = configuration["cityheaven_reservation"]
        profile = str(city["dedicated_profile_path"])
        port = int(city["cdp_port"])
        executable = str(city["chrome_executable"])
        user_id = str(city.get("task_user_id") or DEFAULT_USER_SID)
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectorError(FAILURE) from exc
    if port != CDP_PORT:
        raise CollectorError(FAILURE)
    if path_name(executable) != "chrome.exe":
        raise CollectorError(FAILURE)
    if PROFILE_DIR_MARKER.lower() not in norm_path(profile):
        raise CollectorError(FAILURE)
    if not user_id.startswith("S-1-5-") or any(char in user_id for char in " <>\"'&"):
        raise CollectorError(FAILURE)
    return ChromeSettings(
        chrome_executable=executable,
        profile=profile.rstrip("\\/"),
        port=port,
        user_id=user_id,
    )


def reservation_route(configuration: dict[str, Any]) -> bool:
    daily_cfg = configuration.get("cityheaven_daily")
    if not isinstance(daily_cfg, dict):
        return False
    return str(daily_cfg.get("browser") or "") == "RESERVATION_CHROME_REUSE"


def _poll(settings: ChromeSettings, deps: EnsureDeps) -> tuple[str, list[int], list[int]]:
    processes = deps.list_processes()
    reservation, owners, others = select_processes(processes, settings.profile, settings.port)
    cdp = deps.probe_cdp(settings.port)
    if cdp not in {"CHROME", "OTHER", "CLOSED", "INVALID"}:
        cdp = "INVALID"
    return classify_cdp(cdp, len(owners), len(reservation)), reservation, others


def _verify(settings: ChromeSettings, deps: EnsureDeps, attempts: int, spacing: float) -> tuple[str, list[int], list[int], int]:
    state, reservation, others = "ABSENT", [], []
    used = 0
    for attempt in range(attempts):
        used = attempt + 1
        state, reservation, others = _poll(settings, deps)
        if state == "MATCH":
            return state, reservation, others, used
        if attempt + 1 < attempts:
            deps.sleep(spacing)
    return state, reservation, others, used


def _report(
    *,
    state: str,
    action: str,
    relaunch_count: int,
    killed_pids: list[int],
    skipped_chrome_pids: list[int],
    verify_attempts_used: int,
    settings: ChromeSettings,
) -> dict[str, Any]:
    return {
        "ok": state == "MATCH",
        "state": state,
        "action": action,
        "relaunch_count": relaunch_count,
        "killed_pids": list(killed_pids),
        "skipped_chrome_pids": list(skipped_chrome_pids),
        "skipped_chrome_count": len(skipped_chrome_pids),
        "verify_attempts_used": verify_attempts_used,
        "execution_time_limit": EXECUTION_TIME_LIMIT,
        "task_name": settings.task_name,
        "cdp_port": settings.port,
        "launch_arguments": launch_argument_list(settings.port, settings.profile),
        "profile_marker": PROFILE_DIR_MARKER,
    }


def ensure_reservation_chrome(
    configuration: dict[str, Any],
    *,
    deps: EnsureDeps | None = None,
    session: EnsureSession | None = None,
    timing: Timing | None = None,
) -> dict[str, Any]:
    """Launch when this profile is down. Reuse a leftover whose command line matches it."""
    settings = settings_from_configuration(configuration)
    if deps is None:
        real_io_blocked("ensure")
        deps = windows_deps(settings.profile)
    session = session or EnsureSession()
    timing = timing or Timing()
    state, reservation, others, used = _verify(
        settings, deps, timing.verify_attempts, timing.verify_spacing_seconds,
    )
    if state == "MATCH":
        return _report(
            state="MATCH", action="reused", relaunch_count=0, killed_pids=[],
            skipped_chrome_pids=others, verify_attempts_used=used, settings=settings,
        )
    # Wrong profile on 9227, or our Chrome is up but CDP never matched. Do not kill
    # here; the run's finally closes only this profile.
    if reservation or state != "ABSENT":
        raise CollectorError(FAILURE)
    if session.relaunched:
        raise CollectorError(FAILURE)
    session.relaunched = True
    deps.prepare_profile(settings.profile)
    deps.launch(launch_spec(settings, enabled=True))
    state, _reservation, others, post_used = _verify(
        settings, deps, timing.post_launch_attempts, timing.post_launch_spacing_seconds,
    )
    if state != "MATCH":
        raise CollectorError(FAILURE)
    return _report(
        state="MATCH", action="launched", relaunch_count=1, killed_pids=[],
        skipped_chrome_pids=others, verify_attempts_used=used + post_used, settings=settings,
    )


def probe_cdp(port: int, *, opener: Callable[..., Any] | None = None) -> str:
    url = cdp_version_url(port)
    if url != f"http://127.0.0.1:{CDP_PORT}/json/version":
        raise CollectorError(FAILURE)
    if opener is None:
        real_io_blocked("cdp")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return "INVALID"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "CLOSED"
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "INVALID"
    if not isinstance(payload, dict):
        return "INVALID"
    browser = str(payload.get("Browser") or "")
    socket = payload.get("webSocketDebuggerUrl")
    if browser.startswith("Chrome/") and isinstance(socket, str) and socket.startswith("ws://127.0.0.1:"):
        return "CHROME"
    return "OTHER"


def parse_process_json(text: str) -> list[ProcessInfo]:
    raw = (text or "").strip()
    if not raw:
        return []
    value = json.loads(raw)
    rows = value if isinstance(value, list) else [value]
    processes: list[ProcessInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid") or row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        processes.append(ProcessInfo(
            pid=pid,
            name=str(row.get("name") or row.get("Name") or ""),
            command_line=str(row.get("command_line") or row.get("CommandLine") or ""),
        ))
    return processes


_PS_LIST = r"""
$ErrorActionPreference = 'Stop'
$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'")
$result = foreach ($p in $procs) {
  [PSCustomObject]@{
    pid = [int]$p.ProcessId
    name = [string]$p.Name
    command_line = [string]$p.CommandLine
  }
}
if (-not $result) { '[]' } else { ConvertTo-Json -InputObject @($result) -Compress -Depth 4 }
"""


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    real_io_blocked("subprocess")
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def windows_list_processes() -> list[ProcessInfo]:
    real_io_blocked("process-list")
    if os.name != "nt":
        raise CollectorError(FAILURE)
    completed = _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
        timeout=30,
    )
    if completed.returncode != 0:
        raise CollectorError(FAILURE)
    try:
        return parse_process_json(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CollectorError(FAILURE) from exc


def windows_stop(pids: list[int], profile: str) -> None:
    """taskkill by pid, and only if that pid still has the reservation user-data-dir."""
    real_io_blocked("taskkill")
    if os.name != "nt":
        raise CollectorError(FAILURE)
    current = windows_list_processes()
    allowed = {proc.pid for proc in current if is_reservation_chrome(proc, profile)}
    for pid in pids:
        if pid not in allowed:
            continue
        _run(["taskkill", "/PID", str(int(pid)), "/F"], timeout=20)


def windows_launch(spec: LaunchSpec) -> None:
    real_io_blocked("schtasks")
    if os.name != "nt":
        raise CollectorError(FAILURE)
    if spec.execution_time_limit != EXECUTION_TIME_LIMIT:
        raise CollectorError(FAILURE)
    if f"<ExecutionTimeLimit>{EXECUTION_TIME_LIMIT}</ExecutionTimeLimit>" not in spec.xml:
        raise CollectorError(FAILURE)
    if "<LogonType>InteractiveToken</LogonType>" not in spec.xml:
        raise CollectorError(FAILURE)
    if "UseShellExecute" not in spec.xml or "powershell.exe" not in spec.xml:
        raise CollectorError(FAILURE)
    if "chrome.exe</Command>" in spec.xml or "PT0S" in spec.xml or "PT72H" in spec.xml:
        raise CollectorError(FAILURE)
    handle, name = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    path = name
    try:
        with open(path, "wb") as xml_file:
            xml_file.write(b"\xff\xfe")
            xml_file.write(spec.xml.encode("utf-16-le"))
        created = _run(["schtasks", "/Create", "/TN", spec.task_name, "/XML", path, "/F"], timeout=60)
        if created.returncode != 0:
            raise CollectorError(FAILURE)
        started = _run(["schtasks", "/Run", "/TN", spec.task_name], timeout=60)
        if started.returncode != 0:
            raise CollectorError(FAILURE)
        # Disabling the definition does not stop the running Chrome. The task has no trigger.
        _run(["schtasks", "/Change", "/TN", spec.task_name, "/DISABLE"], timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@dataclass
class WindowsDeps:
    profile: str
    sleeper: Callable[[float], None] = time.sleep

    def list_processes(self) -> list[ProcessInfo]:
        return windows_list_processes()

    def probe_cdp(self, port: int) -> str:
        return probe_cdp(port)

    def sleep(self, seconds: float) -> None:
        self.sleeper(seconds)

    def stop(self, pids: list[int]) -> None:
        windows_stop(pids, self.profile)

    def launch(self, spec: LaunchSpec) -> None:
        windows_launch(spec)

    def prepare_profile(self, profile: str) -> None:
        prepare_profile_norestore(profile, self.list_processes())

    def browser_close(self, port: int) -> None:
        cdp_browser_close(port)


def windows_deps(profile: str) -> WindowsDeps:
    return WindowsDeps(profile=profile)


def write_task_xml(path: str, settings: ChromeSettings, *, enabled: bool) -> None:
    xml = build_task_xml(settings, enabled=enabled)
    data = b"\xff\xfe" + xml.encode("utf-16-le")
    with open(path, "wb") as handle:
        handle.write(data)


def production_settings() -> ChromeSettings:
    return ChromeSettings(
        chrome_executable=DEFAULT_CHROME,
        profile=DEFAULT_PROFILE,
        port=CDP_PORT,
        user_id=DEFAULT_USER_SID,
    )


def cdp_browser_close(port: int, *, opener: Callable[..., Any] | None = None) -> None:
    """Send CDP Browser.close to 127.0.0.1:9227 only."""
    url = cdp_version_url(port)
    if opener is None:
        real_io_blocked("cdp-close")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise CollectorError(FAILURE) from exc
    socket = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
    if not isinstance(socket, str):
        raise CollectorError(FAILURE)
    _websocket_send(socket, json.dumps({"id": 1, "method": "Browser.close"}))


def _websocket_send(url: str, payload: str) -> None:
    real_io_blocked("websocket")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != CDP_PORT:
        raise CollectorError(FAILURE)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    key = uuid.uuid4().hex
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1:9227\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    import socket
    connection = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=2)
    try:
        connection.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header and len(header) < 4096:
            chunk = connection.recv(256)
            if not chunk:
                break
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise CollectorError(FAILURE)
        body = payload.encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81, 0x80 | len(body)])
        frame.extend(mask)
        frame.extend(byte ^ mask[index % 4] for index, byte in enumerate(body))
        connection.sendall(frame)
    finally:
        connection.close()


def close_reservation_chrome(
    settings: ChromeSettings,
    deps: EnsureDeps,
    *,
    timing: Timing | None = None,
) -> dict[str, Any]:
    """Browser.close, then kill only reservation-profile chrome.exe. The profile stays."""
    timing = timing or Timing()
    processes = deps.list_processes()
    reservation, owners, others = select_processes(processes, settings.profile, settings.port)
    cdp = deps.probe_cdp(settings.port)
    graceful = False
    if owners and cdp == "CHROME":
        deps.browser_close(settings.port)
        graceful = True
        for _attempt in range(3):
            deps.sleep(timing.post_launch_spacing_seconds)
            processes = deps.list_processes()
            reservation, _owners, others = select_processes(processes, settings.profile, settings.port)
            if not reservation:
                break
    killed = list(dict.fromkeys(reservation))
    if killed:
        deps.stop(killed)
    return {
        "closed": True,
        "graceful": graceful,
        "killed_pids": killed,
        "skipped_chrome_pids": list(others),
        "profile_kept": True,
        "profile": settings.profile,
    }


@contextmanager
def reservation_chrome_session(
    configuration: dict[str, Any],
    *,
    runtime: Any = None,
    job: str,
    deps: EnsureDeps | None = None,
    session: EnsureSession | None = None,
    timing: Timing | None = None,
) -> Iterator[ChromeLease]:
    """Launch or reuse this job's Chrome, then always close it. No refcount file."""
    if not job or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in job):
        raise CollectorError(FAILURE)
    settings = settings_from_configuration(configuration)
    if deps is None:
        real_io_blocked("ensure")
        deps = windows_deps(settings.profile)
    del runtime
    lease = ChromeLease(job=job)
    try:
        try:
            lease.report = ensure_reservation_chrome(
                configuration, deps=deps, session=session, timing=timing,
            )
        except CollectorError as exc:
            lease.ensure_error = exc
        yield lease
    finally:
        try:
            report = close_reservation_chrome(settings, deps, timing=timing)
        except CollectorError:
            report = {
                "closed": False,
                "graceful": False,
                "profile_kept": True,
                "profile": settings.profile,
            }
        report["job"] = job
        lease.release_report = report
        lease.released = True
