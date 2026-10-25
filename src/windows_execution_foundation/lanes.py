"""One Foundation implementation, explicit lane routing and durable shared leases.

Leases deliberately survive a worker crash. Only the worker that owns a lease
may release it after confirmed completion; elapsed time is never ownership proof.
2026-09-26: a lease whose owner process is provably dead (and that has no live
child and is not retained by a failed job) is moved to released/ with
released_reason=OWNER_DEAD_RECLAIMED by the next acquire (see lane_monitor).
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path

from .core import (
    FoundationError, atomic_write_json, exclusive_lock, iso_now, parse_time,
    read_json, stable_key,
)


@contextmanager
def registry_lock(root: Path):
    with ExitStack() as stack:
        for attempt in range(51):
            try:
                stack.enter_context(exclusive_lock(root / ".registry.lock"))
                break
            except FoundationError as exc:
                if str(exc) != "SINGLE_WORKER_LOCKED" or attempt == 50:
                    raise
                time.sleep(0.04)
        yield


def load_config(path: Path, lane: str = "A") -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if lane not in {"A", "B"}:
        raise ValueError("LANE_INVALID")
    if not config.get("lanes", {}).get("enabled"):
        if lane != "A":
            raise ValueError("LANES_NOT_ENABLED")
        return config
    config = copy.deepcopy(config)
    base = Path(config["state_dir"])
    config["lane_id"] = lane
    config["shared_resource_dir"] = str(base / "shared-resource-locks")
    config["local_exec_airtable"]["lane_id"] = lane
    if lane == "B":
        config["state_dir"] = str(base / "lanes" / "B")
        config["worker_task_name"] = config["lanes"]["worker_b_task_name"]
        config["actions"] = {"codex-local-job": config["actions"]["codex-local-job"]}
        config["local_exec_airtable"]["action_map"] = {"CODEX_LOCAL_JOB": "codex-local-job"}
    action = config["actions"]["codex-local-job"]
    action["command"] += ["--lane", lane]
    output = str(Path(config["state_dir"]) / "local-exec-v01" / "codex-local-job-result.json")
    action["command"][action["command"].index("--output") + 1] = output
    action["receipt_source"] = output
    return config


def record_lane(config: dict, values: dict) -> str:
    if values.get(config["fields"]["action_type"]) != "CODEX_LOCAL_JOB":
        return "A"
    try:
        envelope = json.loads(values.get(config["fields"].get("action_json", "action_json")) or "{}")
        return envelope.get("request", {}).get("lane", "A")
    except (ValueError, TypeError, AttributeError):
        return "A"  # A rejects invalid input; B never steals malformed legacy work.


def owns_record(config: dict, values: dict) -> bool:
    lane = record_lane(config, values)
    if lane == "AUTO":
        # Both workers may inspect an AUTO row.  The shared request registry
        # elects exactly one owner before either lane can enqueue it.
        return True
    # Invalid lane values are handled by A's validation, never silently admitted.
    if lane not in ("A", "B"):
        lane = "A"
    return lane == config.get("lane_id", "A")


def validate_resource(request: dict) -> None:
    if request.get("lane", "A") not in ("A", "B", "AUTO"):
        raise ValueError("LANE_INVALID")
    present = [key in request for key in ("resource_scope", "resource_key")]
    if any(present) and not all(present):
        raise ValueError("RESOURCE_FIELDS_INVALID")
    if all(present):
        for name in ("resource_scope", "resource_key"):
            value = request[name]
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,159}", value):
                raise ValueError("RESOURCE_FIELDS_INVALID")


def resource_descriptor(request: dict, settings: dict) -> dict:
    from .codex_local_job import resolve_workdir
    workdir = resolve_workdir(request.get("workdir_hint"), settings)
    # Repository descendants share the repository root, including .git worktrees.
    root = next((p for p in (workdir, *workdir.parents) if (p / ".git").exists()), workdir)
    scope, key = request.get("resource_scope"), request.get("resource_key")
    return {"key": f"{scope}:{key}".casefold() if scope and key else "*",
            "workdir": os.path.normcase(str(root)).casefold()}


def conflicts(left: dict, right: dict) -> bool:
    if left.get("key", "*") == "*" or right.get("key", "*") == "*":
        return True
    if left["key"] == right["key"]:
        return True
    a, b = left.get("workdir"), right.get("workdir")
    if a and b:
        try:
            return os.path.commonpath((a, b)) in (a, b)
        except ValueError:
            return False
    return False


def select_auto_lane(*, a_health: str, b_health: str, a_busy: bool,
                     b_busy: bool, resource_conflict: bool,
                     b_compatible: bool) -> dict:
    """Pure, deterministic A-first lane selection.

    A shared-resource conflict blocks both lanes.  Stale lock recovery remains
    exclusively owned by acquire_resource/the monitoring contract.
    """
    evidence = {
        "a_health": a_health,
        "b_health": b_health,
        "a_busy": bool(a_busy),
        "b_busy": bool(b_busy),
        "resource_conflict": bool(resource_conflict),
        "explicit_or_auto": "AUTO",
    }
    if resource_conflict:
        return {**evidence, "selected_lane": None,
                "selection_reason": "RESOURCE_CONFLICT_WAIT"}
    if a_health == "HEALTHY" and not a_busy:
        return {**evidence, "selected_lane": "A",
                "selection_reason": "A_HEALTHY_FREE"}
    if b_health == "HEALTHY" and not b_busy and b_compatible:
        return {**evidence, "selected_lane": "B",
                "selection_reason": "A_NOT_READY_B_HEALTHY_FREE_COMPATIBLE"}
    if not b_compatible:
        reason = "B_INCOMPATIBLE_WAIT_A"
    elif a_busy and b_busy:
        reason = "BOTH_BUSY_WAIT"
    else:
        reason = "NO_HEALTHY_FREE_LANE"
    return {**evidence, "selected_lane": None, "selection_reason": reason}


def _lane_health(root: Path, maximum_age: int, now: float) -> str:
    heartbeat = read_json(root / "heartbeats" / "worker.json", {}) or {}
    observed = heartbeat.get("observed_at")
    if not observed:
        return "MISSING"
    try:
        age = now - parse_time(str(observed)).timestamp()
    except (TypeError, ValueError):
        return "INVALID"
    if age < -5 or age > maximum_age:
        return "STALE"
    return "HEALTHY" if heartbeat.get("status") in {"IDLE", "RUNNING"} else "UNAVAILABLE"


def inspect_auto_lane_state(foundation, resource: dict, *, now: float | None = None) -> dict:
    """Read current lane/lease evidence without reclaiming or mutating it."""
    shared = Path(foundation.config["shared_resource_dir"])
    base = shared.parent
    roots = {"A": base, "B": base / "lanes" / "B"}
    maximum = int(foundation.config.get("worker_heartbeat_max_age_seconds", 900))
    observed_at = time.time() if now is None else now
    health = {lane: _lane_health(root, maximum, observed_at) for lane, root in roots.items()}
    busy = {
        lane: any(any((roots[lane] / "spool" / part).glob("*.json"))
                      for part in ("pending", "inflight"))
        for lane in ("A", "B")
    }
    resource_conflict = False
    active = shared / "active"
    for path in sorted(active.glob("*.json")):
        try:
            lease = read_json(path, {}) or {}
            lane = lease.get("lane_id")
            if lane in busy:
                busy[lane] = True
            current_resource = lease.get("resource")
            if not isinstance(current_resource, dict) or conflicts(resource, current_resource):
                resource_conflict = True
        except (OSError, ValueError, TypeError):
            resource_conflict = True  # unreadable active state is unsafe
    local_exec = foundation.config.get("local_exec_airtable") or {}
    b_compatible = bool(
        (foundation.config.get("lanes") or {}).get("enabled")
        and (local_exec.get("action_map") or {}).get("CODEX_LOCAL_JOB") == "codex-local-job"
        and "codex-local-job" in (foundation.config.get("actions") or {})
    )
    return {
        "a_health": health["A"], "b_health": health["B"],
        "a_busy": busy["A"], "b_busy": busy["B"],
        "resource_conflict": resource_conflict,
        "b_compatible": b_compatible,
    }


def admit_auto_request(foundation, request_id: str, request_hash: str,
                       resource: dict) -> dict:
    """Select and reserve an AUTO request using the existing registry CAS lock."""
    root = Path(foundation.config["shared_resource_dir"])
    with registry_lock(root):
        path = root / "requests" / (stable_key(request_id) + ".json")
        prior = read_json(path)
        if prior:
            if prior.get("request_hash") != request_hash:
                raise ValueError("REQUEST_ID_INPUT_MISMATCH")
            evidence = prior.get("lane_selection")
            if not isinstance(evidence, dict) or prior.get("lane_id") not in {"A", "B"}:
                raise ValueError("REQUEST_ID_INPUT_MISMATCH")
            return evidence
        observed = inspect_auto_lane_state(foundation, resource)
        evidence = select_auto_lane(**observed)
        selected = evidence.get("selected_lane")
        if selected:
            atomic_write_json(path, {
                "request_id": request_id,
                "request_hash": request_hash,
                "lane_id": selected,
                "admitted_at": iso_now(),
                "lane_selection": evidence,
            })
        return evidence


def admit_request(foundation, request_id: str, request_hash: str) -> None:
    if not foundation.config.get("shared_resource_dir"):
        return
    root = Path(foundation.config["shared_resource_dir"])
    with registry_lock(root):
        path = root / "requests" / (stable_key(request_id) + ".json")
        prior = read_json(path)
        lane = foundation.config["lane_id"]
        if prior and (prior["request_hash"] != request_hash or prior["lane_id"] != lane):
            raise ValueError("REQUEST_ID_INPUT_MISMATCH")
        if not prior:
            atomic_write_json(path, {"request_id": request_id, "request_hash": request_hash,
                                     "lane_id": lane, "admitted_at": iso_now()})


def claim_work_identity(foundation, candidate: dict, history: list[dict]) -> dict:
    """Atomically decide and reserve one v3 identity on this shared Windows host."""
    directory = foundation.config.get("shared_resource_dir")
    if not directory:
        return {"allowed": False, "code": "SAME_HOST_ATOMICITY_NOT_PROVEN"}
    root = Path(directory)
    with registry_lock(root):
        path = root / "work-identities" / (stable_key(candidate["work_identity_key"]) + ".json")
        registry = read_json(path)
        if isinstance(registry, dict) and registry.get("identity_state") == "ACTIVE":
            # Airtable can already be terminal when a fail-closed executor path
            # writes the row directly, before normal result sync updates this
            # registry.  Only the exact action/request/identity/spec may repair
            # that stale ACTIVE claim; unrelated or conflicting history remains
            # subject to the ordinary fail-closed admission checks below.
            exact_terminal = [item for item in history if (
                item.get("action_id") == registry.get("action_id")
                and item.get("request_id") == registry.get("request_id")
                and item.get("work_identity_key") == registry.get("work_identity_key")
                and item.get("verification_spec_hash") == registry.get("verification_spec_hash")
                and item.get("identity_state") in {"FAILED", "VERIFIED"}
            )]
            terminal_states = {item["identity_state"] for item in exact_terminal}
            if len(terminal_states) == 1:
                terminal = exact_terminal[0]
                registry["identity_state"] = terminal["identity_state"]
                registry["updated_at"] = iso_now()
                if terminal.get("result_ref"):
                    registry["result_ref"] = terminal["result_ref"]
                atomic_write_json(path, registry)
        contenders = list(history)
        if isinstance(registry, dict):
            contenders.append(registry)

        # A replay of the same Airtable Action/request is handled by Foundation's
        # existing request/spool idempotency after this gate.
        others = [item for item in contenders if not (
            item.get("action_id") == candidate["action_id"]
            and item.get("request_id") == candidate["request_id"]
        )]
        for item in others:
            if item.get("work_identity_key") != candidate["work_identity_key"] \
                    or item.get("verification_spec_hash") != candidate["verification_spec_hash"]:
                return {"allowed": False, "code": "WORK_IDENTITY_SPEC_CONFLICT",
                        "existing_action_id": item.get("action_id"),
                        "existing_work_number": item.get("work_number")}

        active = next((item for item in others if item.get("identity_state") == "ACTIVE"), None)
        if active:
            return {"allowed": False, "code": "DUPLICATE_WORK_ACTIVE",
                    "existing_action_id": active.get("action_id"),
                    "existing_work_number": active.get("work_number")}
        complete = next((item for item in others if item.get("identity_state") == "VERIFIED"), None)
        if complete:
            return {"allowed": False, "code": "WORK_ALREADY_COMPLETE",
                    "existing_action_id": complete.get("action_id"),
                    "existing_work_number": complete.get("work_number"),
                    "existing_result_ref": complete.get("result_ref")}
        failed = next((item for item in others if item.get("identity_state") in {"FAILED", "BLOCKED"}), None)
        if failed and candidate.get("resume_of_action_id") != failed.get("action_id"):
            return {"allowed": False, "code": "WORK_REQUIRES_RESUME",
                    "existing_action_id": failed.get("action_id"),
                    "existing_work_number": failed.get("work_number")}

        admission = {
            **candidate,
            "identity_state": "ACTIVE",
            "lane_id": foundation.config.get("lane_id", "A"),
            "claimed_at": iso_now(),
            **({"resume_of_action_id": candidate["resume_of_action_id"]}
               if candidate.get("resume_of_action_id") else {}),
        }
        atomic_write_json(path, admission)
        return {"allowed": True, "code": "WORK_IDENTITY_CLAIMED"}


def update_work_identity_state(foundation, metadata: dict, state: str,
                               result_ref: str | None = None) -> None:
    directory = foundation.config.get("shared_resource_dir")
    identity_key = metadata.get("work_identity_key")
    if not directory or not identity_key:
        return
    root = Path(directory)
    with registry_lock(root):
        path = root / "work-identities" / (stable_key(identity_key) + ".json")
        current = read_json(path)
        if not isinstance(current, dict) or current.get("action_id") != metadata.get("airtable_record_id"):
            return
        current["identity_state"] = state
        current["updated_at"] = iso_now()
        if result_ref:
            current["result_ref"] = result_ref
        atomic_write_json(path, current)


def acquire_resource(foundation, job: dict) -> dict | None:
    directory = foundation.config.get("shared_resource_dir")
    if not directory:
        return {}  # Existing single-lane installations retain their contract.
    root = Path(directory)
    resource = (job.get("metadata") or {}).get("resource") or {"key": "*"}
    reclaimed: list[dict] = []
    lease = None
    try:
        with registry_lock(root):
            leases = root / "active"
            leases.mkdir(parents=True, exist_ok=True)
            blocked = False
            for path in leases.glob("*.json"):
                current = read_json(path)
                record = _reclaim_if_owner_dead(foundation, root, path, current)
                if record:
                    reclaimed.append(record)
                    continue
                if conflicts(resource, current["resource"]):
                    blocked = True
                    break
            if not blocked:
                token = uuid.uuid4().hex
                lease = {"token": token, "lane_id": foundation.config["lane_id"],
                         "request_id": job["idempotency_key"], "resource": resource,
                         "worker_pid": os.getpid(), "acquired_at": iso_now()}
                atomic_write_json(leases / (token + ".json"), lease)
    finally:
        if reclaimed:
            try:
                from .lane_monitor import notify_reclaimed
                notify_reclaimed(foundation.config, reclaimed)
            except Exception:
                pass  # Alerting is best-effort; the reclaim itself is durable.
    return lease


def _reclaim_if_owner_dead(foundation, root: Path, path: Path, lease: dict) -> dict | None:
    """Move a lease whose owner process is provably gone to released/.

    Caller holds registry_lock. A live python owner is never reclaimed, whatever
    the lease age. Any probe error keeps the lease (fail closed).
    """
    try:
        from .lane_monitor import reclaim_verdict
        verdict = reclaim_verdict(root, lease)
        if not verdict.get("reclaim"):
            return None
        record = {**lease, "released_at": iso_now(), "released_reason": "OWNER_DEAD_RECLAIMED",
                  "reclaim": {"by_pid": os.getpid(), "by_lane": foundation.config.get("lane_id", "A"),
                              "owner_probe": {k: verdict.get(k) for k in ("owner", "reason", "pid", "image", "access")}}}
        released = root / "released" / (str(lease["token"]) + ".json")
        if released.exists():
            released = root / "released" / (str(lease["token"]) + f".reclaimed-{int(time.time())}.json")
        atomic_write_json(released, record)
        path.unlink()
        return record
    except Exception:
        return None


def release_resource(foundation, lease: dict) -> None:
    if not lease:
        return
    if lease["lane_id"] != foundation.config["lane_id"] or lease["worker_pid"] != os.getpid():
        raise FoundationError("RESOURCE_OWNER_MISMATCH")
    root = Path(foundation.config["shared_resource_dir"])
    with registry_lock(root):
        path = root / "active" / (lease["token"] + ".json")
        if read_json(path) != lease:
            raise FoundationError("RESOURCE_OWNER_MISMATCH")
        # Retain a bounded-per-job audit trail using existing state storage.
        atomic_write_json(root / "released" / (lease["token"] + ".json"),
                          {**lease, "released_at": iso_now()})
        path.unlink()
