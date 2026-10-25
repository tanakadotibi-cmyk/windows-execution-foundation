from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .airtable_local_exec import LocalExecError, _load_token


ENDPOINT = "https://linehook.tantan-ops.com/line-webhook/cityheaven-notify"
EVENT = "cityheaven_unreplied"
STATE_SCHEMA = "cityheaven_staff_notify_state_v1"
SUCCESS_STATUSES = {"notified", "already_notified"}


def _result(reason_code: str, *, status: str = "FAILED", http_status: int | None = None,
            network_call_count: int = 0, duplicate_count: int = 0) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "http_status": http_status,
        "network_call_count": network_call_count,
        "duplicate_count": duplicate_count,
    }


def _read_state(path: Path, reservation_key: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("STAFF_NOTIFY_STATE_INVALID") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_SCHEMA
        or value.get("dedup_key") != reservation_key
        or value.get("purpose") != "STAFF_LINE_NOTIFY"
        or value.get("state") not in {"SENDING", "SENT"}
    ):
        raise RuntimeError("STAFF_NOTIFY_STATE_INVALID")
    return value


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _claim(path: Path, reservation_key: str, occurred_at: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": STATE_SCHEMA,
        "dedup_key": reservation_key,
        "purpose": "STAFF_LINE_NOTIFY",
        "state": "SENDING",
        "updated_at": occurred_at,
    }
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except FileExistsError:
        return False


def _mark_sent(path: Path, reservation_key: str, occurred_at: str) -> None:
    current = _read_state(path, reservation_key)
    if current is None or current.get("state") != "SENDING":
        raise RuntimeError("STAFF_NOTIFY_STATE_INVALID")
    _write_atomic(path, {**current, "state": "SENT", "updated_at": occurred_at})


def _load_internal_token(config_path: Path) -> str:
    try:
        configuration = json.loads(config_path.read_text(encoding="utf-8"))
        provider = configuration["cityheaven_staff_notify"]
        if not isinstance(provider, dict):
            raise KeyError("cityheaven_staff_notify")
        return _load_token(provider)
    except (KeyError, OSError, ValueError, LocalExecError, AttributeError, UnicodeError) as exc:
        raise RuntimeError("AUTH_REQUIRED_INTERNAL_TOKEN_NOT_PROVISIONED") from exc
    except Exception as exc:
        raise RuntimeError("AUTH_REQUIRED_INTERNAL_TOKEN_NOT_PROVISIONED") from exc


def _default_transport(request: urllib.request.Request, timeout: int) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def notify(
    config_path: Path,
    reservation_key: str,
    occurred_at: str,
    state_path: Path,
    *,
    transport: Callable[[urllib.request.Request, int], tuple[int, bytes]] = _default_transport,
) -> dict[str, Any]:
    if not reservation_key.startswith("ch_") or len(reservation_key) != 67:
        return _result("STAFF_NOTIFY_KEY_INVALID")
    try:
        int(reservation_key[3:], 16)
        parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        return _result("STAFF_NOTIFY_INPUT_INVALID")

    try:
        token = _load_internal_token(config_path)
    except RuntimeError as exc:
        return _result(str(exc))

    try:
        prior = _read_state(state_path, reservation_key)
    except RuntimeError as exc:
        return _result(str(exc))
    if prior is not None:
        if prior["state"] == "SENT":
            return _result("STAFF_ALREADY_NOTIFIED_LOCAL", status="already_notified")
        return _result("STAFF_NOTIFY_UNCERTAIN")
    try:
        if not _claim(state_path, reservation_key, occurred_at):
            concurrent = _read_state(state_path, reservation_key)
            if concurrent is not None and concurrent["state"] == "SENT":
                return _result("STAFF_ALREADY_NOTIFIED_LOCAL", status="already_notified")
            return _result("STAFF_NOTIFY_UNCERTAIN")
    except (OSError, RuntimeError):
        return _result("STAFF_NOTIFY_STATE_INVALID")

    payload = {
        "event": EVENT,
        "reservation_key": reservation_key,
        "occurred_at": occurred_at,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": token,
            "User-Agent": "Tantan-Foundation/1.0",
        },
    )
    try:
        http_status, raw_body = transport(request, 20)
    except Exception:
        return _result("STAFF_NOTIFY_NETWORK_FAILED", network_call_count=1)
    if http_status in {401, 403}:
        return _result("STAFF_NOTIFY_AUTH_FAILED", http_status=http_status, network_call_count=1)
    if http_status == 429:
        return _result("STAFF_NOTIFY_RATE_LIMITED", http_status=http_status, network_call_count=1)
    if 500 <= http_status <= 599:
        return _result("STAFF_NOTIFY_UPSTREAM_FAILED", http_status=http_status, network_call_count=1)
    if http_status != 200:
        return _result("STAFF_NOTIFY_HTTP_FAILED", http_status=http_status, network_call_count=1)
    try:
        body = json.loads(raw_body.decode("utf-8"))
        status = body.get("status") if isinstance(body, dict) else None
    except (UnicodeError, ValueError):
        status = None
    if status not in SUCCESS_STATUSES:
        return _result("STAFF_NOTIFY_RESPONSE_INVALID", http_status=200, network_call_count=1)
    try:
        _mark_sent(state_path, reservation_key, occurred_at)
    except (OSError, RuntimeError):
        return _result("STAFF_NOTIFY_UNCERTAIN", http_status=200, network_call_count=1)
    reason = "STAFF_NOTIFIED" if status == "notified" else "STAFF_ALREADY_NOTIFIED"
    return _result(reason, status=status, http_status=200, network_call_count=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--reservation-key", required=True)
    parser.add_argument("--occurred-at", required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    args = parser.parse_args()
    result = notify(args.config, args.reservation_key, args.occurred_at, args.state_path)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] in SUCCESS_STATUSES else 20


if __name__ == "__main__":
    raise SystemExit(main())
