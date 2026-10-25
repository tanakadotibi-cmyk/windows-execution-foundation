"""Lane monitoring (2026-09-26): dead-owner lease probes, RESOURCE_WAIT timer and
watchdog alerts. Every entry point here is best-effort: callers wrap it so a probe
or Slack failure can never crash a worker or watchdog.

Owner liveness contract (used by lanes.acquire_resource):
  * A lease is LIVE while its worker_pid is a running python/pythonw process that
    was created no later than the lease's acquired_at. Elapsed time never matters.
  * Otherwise the owner is DEAD (not found / exited / PID reused by another image
    or by a process created after the lease / inaccessible = other user).
  * A DEAD owner's lease is still NOT reclaimed when (a) a live child process of
    the dead worker created after acquired_at exists, or (b) a failed-spool job
    still references the lease token (fail-closed retention; human rescue).
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import atomic_write_json, iso_now, parse_time, read_json

RESOURCE_WAIT_ALERT_SECONDS = 15 * 60
REPEAT_ALERT_SECONDS = 60 * 60
_PYTHON_IMAGE = re.compile(r"pythonw?(\d+(\.\d+)*)?(\.exe)?", re.IGNORECASE)
_JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- Slack
def _host_label() -> str:
    return f"{os.environ.get('COMPUTERNAME') or 'host'}/{os.environ.get('USERNAME') or 'user'}"


def send_text(text: str, config: dict[str, Any], timeout: int = 5) -> dict[str, Any]:
    """Send a plain monitoring message using the Foundation Slack env contract.

    Disabled unless config.foundation_failure_slack.enabled is True, and always
    disabled when WEF_DISABLE_MONITOR_SLACK=1 (tests). Never raises, never logs the URL.
    """
    try:
        if os.environ.get("WEF_DISABLE_MONITOR_SLACK") == "1":
            return {"state": "DISABLED"}
        settings = config.get("foundation_failure_slack") or {}
        if not isinstance(settings, dict) or settings.get("enabled") is not True:
            return {"state": "DISABLED"}
        from .foundation_failure_slack import _environment_value

        webhook = _environment_value(str(settings.get("webhook_environment_variable") or "TANTAN_SLACK_WEBHOOK_URL"))
        if not webhook:
            return {"state": "NOT_CONFIGURED"}
        target = urllib.parse.urlsplit(webhook)
        if (target.scheme != "https" or target.hostname != "hooks.slack.com"
                or not target.path.startswith("/services/") or target.username or target.password):
            return {"state": "CONFIG_INVALID"}
        user = _environment_value(str(settings.get("notify_user_environment_variable") or "TANTAN_SLACK_NOTIFY_USER_ID"))
        mention = f"<@{user}> " if re.fullmatch(r"[UW][A-Z0-9]{8,}", user) else ""
        body = json.dumps({"text": mention + text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(webhook, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            return {"state": "SENT" if status == 200 else "HTTP_FAILED", "http_status": status}
    except urllib.error.HTTPError as exc:
        return {"state": "HTTP_FAILED", "http_status": int(exc.code)}
    except Exception as exc:  # noqa: BLE001 - monitoring is best-effort
        return {"state": "UNKNOWN", "error_type": type(exc).__name__}


def _jst(value: Any) -> str:
    try:
        return parse_time(str(value)).astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return str(value)


def _lease_line(lease: dict[str, Any], verdict: dict[str, Any] | None = None) -> str:
    resource = lease.get("resource") or {}
    line = (f"key={resource.get('key')} lane={lease.get('lane_id')} request_id={lease.get('request_id')} "
            f"worker_pid={lease.get('worker_pid')} acquired={_jst(lease.get('acquired_at'))}")
    if verdict:
        line += f" owner={verdict.get('owner')}({verdict.get('reason')})"
    return line


# ---------------------------------------------------------------- process probes
def _epoch(value: Any) -> float | None:
    try:
        return parse_time(str(value)).timestamp()
    except Exception:
        return None


def process_info(pid: Any) -> dict[str, Any]:
    """Return {'alive', 'image', 'created', 'access'} for a pid without psutil."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"alive": False, "image": None, "created": None, "access": "none"}
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"alive": False, "image": None, "created": None, "access": "none"}
        except PermissionError:
            return {"alive": True, "image": None, "created": None, "access": "denied"}
        try:
            image = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            image = None
        return {"alive": True, "image": image, "created": None, "access": "ok"}

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:  # ERROR_ACCESS_DENIED: exists, but not a same-user process
            return {"alive": True, "image": None, "created": None, "access": "denied"}
        return {"alive": False, "image": None, "created": None, "access": "none", "error": error}
    try:
        code = wintypes.DWORD()
        alive = bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        image = buffer.value if k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        times = [wintypes.FILETIME() for _ in range(4)]
        created = None
        if k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = ticks / 10_000_000 - 11_644_473_600
        return {"alive": alive, "image": image, "created": created, "access": "ok"}
    finally:
        k32.CloseHandle(handle)


def live_children(pid: Any, since_epoch: float | None) -> list[dict[str, Any]]:
    """Live processes whose parent is pid and that started after since_epoch."""
    if not isinstance(pid, int) or pid <= 0:
        return []
    candidates: list[int] = []
    if os.name != "nt":
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().rsplit(")", 1)[1].split()
                if int(fields[1]) == pid:
                    candidates.append(int(stat.parent.name))
            except (OSError, ValueError, IndexError):
                continue
    else:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            # Cannot prove there are no orphans: fail closed.
            return [{"pid": None, "image": "SNAPSHOT_FAILED"}]
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32ParentProcessID == pid and entry.th32ProcessID != pid:
                    candidates.append(int(entry.th32ProcessID))
                ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snapshot)
    children = []
    for child in candidates:
        info = process_info(child)
        if not info.get("alive"):
            continue
        created = info.get("created")
        # A child created before the lease cannot belong to this lease's work
        # (the parent pid was merely reused), so only newer children block.
        if created is None or since_epoch is None or created >= since_epoch - 2:
            children.append({"pid": child, "image": os.path.basename(info.get("image") or "") or None})
    return children


def assess_owner(lease: dict[str, Any]) -> dict[str, Any]:
    pid = lease.get("worker_pid")
    acquired = _epoch(lease.get("acquired_at"))
    info = process_info(pid)
    image = os.path.basename(info.get("image") or "")
    base = {"pid": pid, "image": image or None, "access": info.get("access")}
    if not info.get("alive"):
        return {"owner": "DEAD", "reason": "PROCESS_NOT_FOUND" if info.get("access") == "none"
                else "PROCESS_EXITED", **base}
    if info.get("access") != "ok":
        return {"owner": "DEAD", "reason": "PID_REUSED_INACCESSIBLE", **base}
    if not _PYTHON_IMAGE.fullmatch(image):
        return {"owner": "DEAD", "reason": "PID_REUSED_NON_PYTHON", **base}
    created = info.get("created")
    if created is not None and acquired is not None and created > acquired + 1:
        return {"owner": "DEAD", "reason": "PID_REUSED_AFTER_ACQUIRE", **base}
    return {"owner": "LIVE", "reason": "PYTHON_OWNER_RUNNING", **base}


def _failed_dirs(shared_root: Path) -> list[Path]:
    base = shared_root.parent
    dirs = [base / "spool" / "failed"]
    for parent in (base, base / "lanes"):
        try:
            dirs.extend(child / "spool" / "failed" for child in parent.iterdir() if child.is_dir())
        except OSError:
            continue
    return [d for d in dict.fromkeys(dirs) if d.is_dir()]


def lease_retained_by_failed_job(shared_root: Path, token: str) -> str | None:
    for directory in _failed_dirs(shared_root):
        for path in directory.glob("*.json"):
            try:
                job = read_json(path, {}) or {}
            except Exception:
                continue
            if (job.get("resource_lease") or {}).get("token") == token:
                return str(path)
    return None


def reclaim_verdict(shared_root: Path, lease: dict[str, Any]) -> dict[str, Any]:
    """Decide (without side effects) whether a lease may be reclaimed."""
    verdict = assess_owner(lease)
    if verdict["owner"] != "DEAD":
        return {**verdict, "reclaim": False}
    children = live_children(lease.get("worker_pid"), _epoch(lease.get("acquired_at")))
    if children:
        return {**verdict, "reclaim": False, "blocked_by": "LIVE_CHILDREN", "children": children[:5]}
    retained = lease_retained_by_failed_job(shared_root, str(lease.get("token")))
    if retained:
        return {**verdict, "reclaim": False, "blocked_by": "FAILED_JOB_RETAINS_LEASE",
                "failed_job": os.path.basename(retained)}
    return {**verdict, "reclaim": True}


def notify_reclaimed(config: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    lines = [f"Foundation 共有ロック自動回収（{_host_label()} / lane {config.get('lane_id', 'A')}）",
             "結果: 持ち主の worker プロセスが停止していたロックを released に移しました（OWNER_DEAD_RECLAIMED）"]
    for record in records[:5]:
        lines.append("- " + _lease_line(record, (record.get("reclaim") or {}).get("owner_probe")))
    lines += ["次: 待機中のジョブは次の5分サイクルで自動再開します。", "たんたんの対応: 不要（繰り返す場合は確認）"]
    return send_text("\n".join(lines), config)


def describe_active_leases(config: dict[str, Any]) -> list[dict[str, Any]]:
    directory = config.get("shared_resource_dir")
    if not directory:
        return []
    root = Path(directory)
    result = []
    for path in sorted((root / "active").glob("*.json")):
        lease: dict[str, Any] = {}
        try:
            lease = read_json(path, {}) or {}
            verdict = reclaim_verdict(root, lease)
        except Exception as exc:  # noqa: BLE001
            verdict = {"owner": "UNKNOWN", "reason": type(exc).__name__}
        result.append({"lease": lease, "verdict": verdict})
    return result


# ---------------------------------------------------------------- RESOURCE_WAIT timer
def observe_cycle(foundation, outcomes: list[dict[str, Any]], now: float | None = None) -> dict[str, Any] | None:
    statuses = [str(item.get("status")) for item in outcomes or []]
    if not statuses or all(status == "NOOP_SINGLE_WORKER" for status in statuses):
        return None  # Nothing observed this tick; keep the timer as is.
    now = time.time() if now is None else now
    path = foundation.root / "monitoring" / "resource-wait.json"
    state = read_json(path, {}) or {}
    if "RESOURCE_WAIT" in statuses:
        if not state.get("since_epoch"):
            state = {"since_epoch": now, "since": iso_now(), "alerts_sent": 0}
        waited = now - float(state["since_epoch"])
        last = state.get("last_alert_epoch")
        if waited >= RESOURCE_WAIT_ALERT_SECONDS and (last is None or now - float(last) >= REPEAT_ALERT_SECONDS):
            blocking = describe_active_leases(foundation.config)
            lines = [f"Foundation RESOURCE_WAIT 継続（{_host_label()} / lane {foundation.config.get('lane_id', 'A')}）",
                     f"結果: 待機ジョブが共有ロック待ちのまま {int(waited // 60)} 分経過しています",
                     f"ブロック中のロック: {len(blocking)} 件"]
            for item in blocking[:5]:
                verdict = item["verdict"]
                extra = f" blocked_by={verdict['blocked_by']}" if verdict.get("blocked_by") else ""
                lines.append("- " + _lease_line(item["lease"], verdict) + extra)
            lines += ["次: 持ち主が動作中なら完了を待ちます。停止済みなのに回収されない場合は手動救出が必要です。",
                      "たんたんの対応: 確認（1時間ごとに最大1回通知）"]
            delivery = send_text("\n".join(lines), foundation.config)
            state["last_alert_epoch"] = now
            state["last_alert_at"] = iso_now()
            state["last_alert_delivery"] = delivery.get("state")
            state["alerts_sent"] = int(state.get("alerts_sent", 0)) + 1
        state["last_seen_epoch"] = now
        atomic_write_json(path, state)
        return state
    if state.get("since_epoch"):
        waited = int(now - float(state["since_epoch"]))
        cleared = {"cleared_at": iso_now(), "last_wait_seconds": waited,
                   "last_wait_alerts_sent": int(state.get("alerts_sent", 0))}
        if state.get("alerts_sent"):
            delivery = send_text(
                f"Foundation RESOURCE_WAIT 解消（{_host_label()} / lane {foundation.config.get('lane_id', 'A')}）\n"
                f"結果: 約 {waited // 60} 分の待機が解消しました。たんたんの対応: 不要", foundation.config)
            cleared["resolved_delivery"] = delivery.get("state")
        atomic_write_json(path, cleared)
    return None


# ---------------------------------------------------------------- watchdog alerts
def watchdog_alert(foundation, value: dict[str, Any], heartbeat_age: float | None,
                   now: float | None = None) -> dict[str, Any] | None:
    status = str(value.get("status"))
    restart = str(value.get("restart_result"))
    config = foundation.config
    conditions = []
    if status == "WORKER_STALE":
        conditions.append("WORKER_STALE")
    if restart.startswith("FAILED"):
        conditions.append("RESTART_FAILED")
    if status == "WORKER_BUSY_STALE_HEARTBEAT":
        timeouts = [int(a.get("timeout_seconds", 0) or 0) for a in (config.get("actions") or {}).values()
                    if isinstance(a, dict)]
        threshold = max(int(config.get("worker_heartbeat_max_age_seconds", 900)), max(timeouts or [0]) + 300)
        # A single legitimate action may run up to its timeout; alert only beyond that.
        if heartbeat_age is None or heartbeat_age > threshold:
            conditions.append("WORKER_BUSY_STALE_HEARTBEAT")
    if not conditions:
        return None
    now = time.time() if now is None else now
    path = foundation.root / "monitoring" / "watchdog-alerts.json"
    state = read_json(path, {}) or {}
    sent = {}
    for condition in conditions:
        last = (state.get(condition) or {}).get("last_epoch")
        if last is not None and now - float(last) < REPEAT_ALERT_SECONDS:
            continue
        age = "不明" if heartbeat_age is None else f"{int(heartbeat_age // 60)} 分"
        text = "\n".join([
            f"Foundation watchdog: {condition}（{_host_label()} / lane {config.get('lane_id', 'A')}）",
            f"結果: status={status} restart={restart} worker心拍の経過={age} recovered_inflight={value.get('recovered_inflight')}",
            "次: watchdog は5分ごとに再確認します（同じ条件の通知は1時間に最大1回）。",
            "たんたんの対応: " + ("確認（worker 再起動に失敗）" if condition == "RESTART_FAILED" else "確認"),
        ])
        delivery = send_text(text, config)
        state[condition] = {"last_epoch": now, "last_at": iso_now(), "delivery": delivery.get("state")}
        sent[condition] = delivery.get("state")
    atomic_write_json(path, state)
    return sent or None
