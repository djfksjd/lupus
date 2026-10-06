"""Handoff between execution drivers, e.g. Claude <-> Codex (§6.7, RECOVERY.md, HANDOFF-01..03).

The packet is issued by the supervisor from DB state; no model writes or summarises it. The
copy in the DB is the only authoritative one. A presented packet is accepted when
  * its canonical hash equals the stored hash (any edit, including edit-plus-rehash, fails:
    the hash is compared with the DB, not with a field inside the packet)
  * it is still PREPARED, not expired, and bound to the latest checkpoint revision
  * readiness holds NOW: old writer confirmed stopped, no unresolved external action, epochs
    current, budget left, target provider approved and its capabilities measured
Accept happens once. A replay of the same accept (lost response) returns the same run.

Carried over by construction, because they live in the DB and not in the packet: goal/task
identity, acceptance revision, attempt counts, no-progress streaks, budget usage. The packet
never contains credentials or session tokens; the target uses its own verified login.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from . import goals, projects, recovery, runs
from .kernel import Kernel
from .util import LupusError, canonical_json, new_id, sha256_json

SCHEMA_VERSION = 1


def _last_driver(k: Kernel, goal_id: str) -> str:
    row = k.one("SELECT execution_driver FROM run WHERE goal_id = ? ORDER BY fencing_token DESC LIMIT 1",
                goal_id)
    return row["execution_driver"] if row else "none"


def prepare(k: Kernel, goal_id: str, target_driver: str) -> dict:
    with k.tx():
        ready = recovery.readiness(k, goal_id, target_driver)
        if not ready["ready"]:
            raise LupusError("HANDOFF_NOT_READY", ";".join(ready["blockers"]))
        goal = goals.get(k, goal_id)
        project = projects.get(k, goal["project_id"])
        cp = recovery.latest(k, goal_id)
        task = goals.get_task(k, ready["next_task_id"])
        handoff_id = new_id("hof")
        now = k.now()
        partial = bool(ready["workspace"]["changed"] or ready["workspace"]["missing"])
        state = recovery.kernel_state(k, goal_id)
        packet = {
            "schema_version": SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "checkpoint": {"id": cp["checkpoint_id"], "revision": cp["revision"],
                           "payload_hash": cp["payload_hash"]},
            "binding": {
                "project_id": project["project_id"],
                "goal_id": goal_id,
                "alpha_id": project["alpha_id"],
                "source_adapter": _last_driver(k, goal_id),
                "target_adapter": target_driver,
                "acceptance_revision": goal["acceptance_revision"],
                "policy_version": project["policy_version"],
                "revocation_epoch": project["revocation_epoch"],
                "recovery_epoch": k.recovery_epoch,
            },
            "work": {
                "objective": goal["objective"],
                "acceptance": [{"id": c["id"], "text": c["text"]} for c in goals.criteria(k, goal_id)],
                "done": cp["payload"]["done"],
                "remaining": cp["payload"]["remaining"],
                "decisions": cp["payload"]["decisions"],
                "next_task_id": task["task_id"],
                "next_action": "verify-partial-artifact" if partial else cp["payload"]["next_action"],
                "workspace_diff": ready["workspace"],
                "artifact_manifest": cp["manifest"],
                "recovery_objects": [
                    {"label": o["label"], "role": o["role"], "sha256": o["sha256"]}
                    for o in recovery.pinned_objects(k, cp["checkpoint_id"])
                ],
            },
            # Live counters from the DB, not the (older) checkpoint snapshot.
            "budget": state["budget"],
            "attempts": state["tasks"],
            "safety": {"unresolved_action_intents": [], "resume_ready": True},
            "expires_at": now + k.policy["handoff_ttl_ms"],
        }
        k.run(
            "UPDATE handoff SET status = 'REJECTED', reject_reason = 'superseded', resolved_at = ? "
            "WHERE goal_id = ? AND status = 'PREPARED'",
            now, goal_id,
        )
        k.run(
            "INSERT INTO handoff(handoff_id, goal_id, checkpoint_id, checkpoint_revision, source_adapter, "
            "target_adapter, packet, packet_hash, status, expires_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'PREPARED', ?,?)",
            handoff_id, goal_id, cp["checkpoint_id"], cp["revision"], packet["binding"]["source_adapter"],
            target_driver, canonical_json(packet), sha256_json(packet), packet["expires_at"], now,
        )
        k.emit("supervisor", "handoff.prepared", "goal", goal_id, handoff_id=handoff_id, target=target_driver)
        return packet


def get(k: Kernel, handoff_id: str) -> dict:
    row = k.one("SELECT * FROM handoff WHERE handoff_id = ?", handoff_id)
    if row is None:
        raise LupusError("HANDOFF_NOT_FOUND", handoff_id)
    out = dict(row)
    out["packet"] = json.loads(row["packet"])
    return out


def validate(k: Kernel, packet: Mapping[str, Any]) -> dict:
    """Check a presented packet against CURRENT supervisor state. Raises on the first problem."""
    handoff_id = packet.get("handoff_id") if isinstance(packet, Mapping) else None
    if not isinstance(handoff_id, str):
        raise LupusError("HANDOFF_PACKET_INVALID", "no handoff_id")
    row = get(k, handoff_id)
    if packet.get("schema_version") != SCHEMA_VERSION:
        raise LupusError("HANDOFF_SCHEMA_UNSUPPORTED", str(packet.get("schema_version")))
    if sha256_json(packet) != row["packet_hash"]:
        raise LupusError("HANDOFF_PACKET_TAMPERED", handoff_id)
    if row["status"] != "PREPARED":
        raise LupusError("HANDOFF_NOT_OPEN", row["status"])
    if k.now() >= row["expires_at"]:
        raise LupusError("HANDOFF_EXPIRED", handoff_id)
    cp = recovery.latest(k, row["goal_id"])
    if cp is None or cp["revision"] != row["checkpoint_revision"]:
        raise LupusError("HANDOFF_STALE", "a newer checkpoint exists")
    goal = goals.get(k, row["goal_id"])
    project = projects.get(k, goal["project_id"])
    current = {"acceptance_revision": goal["acceptance_revision"], "policy_version": project["policy_version"],
               "revocation_epoch": project["revocation_epoch"], "recovery_epoch": k.recovery_epoch}
    stale = [name for name, value in current.items() if row["packet"]["binding"][name] != value]
    if stale:
        # e.g. the user revised the acceptance criteria after the packet was issued
        raise LupusError("HANDOFF_STALE", f"binding changed: {','.join(stale)}")
    ready = recovery.readiness(k, row["goal_id"], row["target_adapter"])
    if not ready["ready"]:
        raise LupusError("HANDOFF_NOT_READY", ";".join(ready["blockers"]))
    if ready["next_task_id"] != packet["work"]["next_task_id"]:
        raise LupusError("HANDOFF_STALE", "next task changed")
    return row


def accept(k: Kernel, request_id: str, packet: Mapping[str, Any], ack: Mapping[str, Any],
           auth_mode: str) -> dict:
    """The new run acknowledges the exact revision and next task it received, then gets the only
    lease. Idempotent per request_id."""

    def _do() -> dict:
        row = validate(k, packet)
        if (ack.get("checkpoint_revision") != row["checkpoint_revision"]
                or ack.get("next_task_id") != packet["work"]["next_task_id"]):
            raise LupusError("HANDOFF_ACK_MISMATCH", canonical_json(dict(ack)))
        run = runs.claim(k, packet["work"]["next_task_id"], row["target_adapter"], auth_mode)
        k.run(
            "UPDATE handoff SET status = 'ACCEPTED', accepted_by_run = ?, resolved_at = ? WHERE handoff_id = ?",
            run["run_id"], k.now(), row["handoff_id"],
        )
        k.emit("supervisor", "handoff.accepted", "goal", row["goal_id"], handoff_id=row["handoff_id"],
               run_id=run["run_id"])
        return {"run_id": run["run_id"], "fencing_token": run["fencing_token"],
                "task_id": run["task_id"], "handoff_id": row["handoff_id"]}

    return k.idempotent(request_id, "handoff.accept", {"packet": packet, "ack": dict(ack)}, _do)


def reject(k: Kernel, handoff_id: str, reason: str) -> None:
    with k.tx():
        k.run(
            "UPDATE handoff SET status = 'REJECTED', reject_reason = ?, resolved_at = ? "
            "WHERE handoff_id = ? AND status = 'PREPARED'",
            reason, k.now(), handoff_id,
        )
