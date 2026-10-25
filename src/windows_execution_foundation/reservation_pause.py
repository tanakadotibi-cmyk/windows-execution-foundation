"""Local cancellation gate: only reservation automation; no I/O except marker read."""
import json
from pathlib import Path

MARKER = Path(__file__).resolve().parents[2] / "local" / "reservation-chat-pause.json"
ACTION = "cityheaven-reservation-schedule"
ACTION_TYPE = "CITYHEAVEN_RESERVATION_SCHEDULE"
MAX_BYTES = 4096

def paused():
    try:
        with MARKER.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return True
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return True
    return not (isinstance(value, dict) and set(value) == {"scope", "paused"}
                and value["scope"] == ACTION_TYPE and value["paused"] is False)

def action_paused(action):
    return action == ACTION and paused()

def record_paused(config, record):
    return str((record.get("fields") or {}).get(config["fields"]["action_type"]) or "") == ACTION_TYPE and paused()

NOTICE_SCHEMA = "reservation_pause_receipt/1"
STAGES = {"PYTHON_BEFORE_NODE", "NODE_BEFORE_READER"}

def pause_notice(no_child_confirmed, stop_stage):
    confirmed = no_child_confirmed is True and stop_stage in STAGES
    return {"schema": NOTICE_SCHEMA, "status": "PAUSED_BY_OWNER", "scope": ACTION_TYPE,
            "no_child_confirmed": confirmed,
            "stop_stage": stop_stage if confirmed else "UNKNOWN_CHILD_STATE"}

def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate notice key")
        value[key] = item
    return value

def parse_pause_notice(stdout, producer):
    try:
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > 2048:
            return None
        v = json.loads(stdout, object_pairs_hook=_unique_object)
        if not isinstance(v, dict) or set(v) != {"schema", "status", "scope", "no_child_confirmed", "stop_stage"}:
            return None
        if v["schema"] != NOTICE_SCHEMA or v["status"] != "PAUSED_BY_OWNER" or v["scope"] != ACTION_TYPE:
            return None
        if type(v["no_child_confirmed"]) is not bool:
            return None
        allowed = {"NODE_BEFORE_READER"} if producer == "node" else STAGES if producer == "parent" else set()
        if v["no_child_confirmed"]:
            return v if v["stop_stage"] in allowed else None
        return v if v["stop_stage"] == "UNKNOWN_CHILD_STATE" else None
    except Exception:
        return None

class ReservationPauseExit(int):
    def __new__(cls, notice):
        instance = int.__new__(cls, 30)
        instance.notice = parse_pause_notice(json.dumps(notice), "parent") or pause_notice(False, "UNKNOWN_CHILD_STATE")
        return instance
