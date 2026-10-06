"""Approvals and external actions (§7, §6.5, APPROVAL-01, ACTION-01).

Fixed rules:
  * an approval is created only by the user and names an exact action digest
  * consuming the approval and creating the action intent are ONE transaction, committed before
    anything leaves the machine. There is no window where the action ran but the approval is
    still usable, and none where the approval is spent with no record of why
  * after a crash the intent is UNKNOWN. It is reconciled against the provider's receipt; it is
    never blindly retried
  * continuing after a confirmed NOT_DONE re-dispatches the same intent with the same
    idempotency key. The approval is never reused for a different intent
  * everything that goes through the broker needs an approval. Whether an action is "external"
    is not something the caller can declare away
  * a result is written only if the intent is still in the dispatch it was observed in, so a
    slow lookup cannot overwrite a newer outcome
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from . import goals, runs
from .kernel import Kernel
from .util import LupusError, canonical_json, crash_point, new_id, sha256_json

DIGEST_FIELDS = ("kind", "destination", "inputs_hash", "artifact_hash", "permissions", "cost_cap")


class Broker(Protocol):
    """The only component that performs external effects. Models never hold its credentials."""

    def perform(self, idempotency_key: str, action: Mapping[str, Any]) -> dict: ...

    def lookup(self, idempotency_key: str) -> dict | None: ...


def digest(action: Mapping[str, Any]) -> str:
    """What the user approves: kind, destination, exact inputs, final artifact, permissions and
    cost cap. Any change to these is a different action and needs a new approval."""
    missing = [f for f in DIGEST_FIELDS if f not in action]
    if missing:
        raise LupusError("ACTION_INCOMPLETE", ",".join(missing))
    return sha256_json({f: action[f] for f in DIGEST_FIELDS})


def issue_approval(k: Kernel, goal_id: str, action: Mapping[str, Any], scope: str, ttl_ms: int,
                   actor: str) -> str:
    if actor != "user":
        # Checkboxes in a note, web pages and agent messages are not approvals (§7).
        raise LupusError("USER_AUTHORITY_REQUIRED", "approval")
    with k.tx():
        goal = goals.get(k, goal_id)
        project = k.one("SELECT revocation_epoch FROM project WHERE project_id = ?", goal["project_id"])
        approval_id = new_id("apr")
        k.run(
            "INSERT INTO approval(approval_id, goal_id, acceptance_revision, action_digest, scope, "
            "issued_by, issued_at, expires_at, revocation_epoch, recovery_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
            approval_id, goal_id, goal["acceptance_revision"], digest(action), scope, actor, k.now(),
            k.now() + ttl_ms, project["revocation_epoch"], k.recovery_epoch,
        )
        k.emit(actor, "approval.issued", "goal", goal_id, approval_id=approval_id, scope=scope)
        return approval_id


def _check_approval(k: Kernel, approval_id: str, goal: Mapping, action_digest: str) -> None:
    row = k.one("SELECT * FROM approval WHERE approval_id = ?", approval_id)
    if row is None or row["goal_id"] != goal["goal_id"]:
        raise LupusError("APPROVAL_NOT_FOUND", approval_id)
    if row["consumed_at"] is not None:
        raise LupusError("APPROVAL_CONSUMED", approval_id)
    if row["revoked_at"] is not None:
        raise LupusError("APPROVAL_REVOKED", approval_id)
    if k.now() >= row["expires_at"]:
        raise LupusError("APPROVAL_EXPIRED", approval_id)
    if row["action_digest"] != action_digest:
        # The thing being executed is not the thing that was approved.
        raise LupusError("APPROVAL_DIGEST_MISMATCH", approval_id)
    if row["acceptance_revision"] != goal["acceptance_revision"]:
        raise LupusError("APPROVAL_STALE_REVISION", approval_id)
    project = k.one("SELECT revocation_epoch FROM project WHERE project_id = ?", goal["project_id"])
    if row["revocation_epoch"] != project["revocation_epoch"] or row["recovery_epoch"] != k.recovery_epoch:
        raise LupusError("APPROVAL_STALE_EPOCH", approval_id)


def _not_nested(k: Kernel) -> None:
    """The intent must be COMMITTED before the effect leaves the machine. Inside a caller's
    transaction that cannot be guaranteed (a later rollback would erase the intent and hand the
    approval back after the effect happened), so dispatch refuses to run there."""
    if k.in_tx:
        raise LupusError("NESTED_DISPATCH", "external actions cannot be dispatched inside a transaction")


def begin(k: Kernel, run_id: str, token: int, action: Mapping[str, Any], idempotency_key: str,
          approval_id: str | None) -> tuple[dict, bool]:
    """Record the intent and spend the approval in one transaction. Returns (intent, created).
    `action` must be the inputs as they are at execution time, so a file changed after approval
    yields a different digest."""
    action_digest = digest(action)
    with k.tx():
        run = runs.guard(k, run_id, token)
        existing = k.one("SELECT * FROM action_intent WHERE idempotency_key = ?", idempotency_key)
        if existing is not None:
            if existing["action_digest"] != action_digest:
                raise LupusError("IDEMPOTENCY_CONFLICT", idempotency_key)
            # Same request again: report the recorded intent. Do NOT execute a second time.
            return dict(existing), False
        goal = goals.get(k, run["goal_id"])
        if approval_id is None:
            raise LupusError("NEEDS_APPROVAL", str(action["kind"]))
        _check_approval(k, approval_id, goal, action_digest)
        intent_id = new_id("int")
        now = k.now()
        k.run(
            "INSERT INTO action_intent(intent_id, goal_id, task_id, run_id, approval_id, idempotency_key, "
            "action_digest, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'DISPATCHED', ?,?)",
            intent_id, run["goal_id"], run["task_id"], run_id, approval_id, idempotency_key,
            action_digest, now, now,
        )
        k.run("UPDATE approval SET consumed_at = ?, consumed_by_intent = ? WHERE approval_id = ?",
              now, intent_id, approval_id)
        k.emit("supervisor", "action.dispatched", "intent", intent_id, kind=action["kind"],
               approval_id=approval_id)
        return dict(k.one("SELECT * FROM action_intent WHERE intent_id = ?", intent_id)), True


def _settle(k: Kernel, intent_id: str, dispatch_count: int, expect: tuple[str, ...], status: str,
            receipt: Mapping | None, actor: str) -> dict:
    """Write an outcome only if the intent is still in the same dispatch and state it was
    observed in. Otherwise something newer already decided it; that decision stands."""
    with k.tx():
        placeholders = ",".join("?" * len(expect))
        changed = k.run(
            f"UPDATE action_intent SET status = ?, receipt = ?, updated_at = ? WHERE intent_id = ? "
            f"AND dispatch_count = ? AND status IN ({placeholders})",
            status, canonical_json(receipt) if receipt is not None else None, k.now(), intent_id,
            dispatch_count, *expect,
        )
        if changed:
            k.emit(actor, "action.resolved", "intent", intent_id, status=status)
        return dict(k.one("SELECT * FROM action_intent WHERE intent_id = ?", intent_id))


def _perform(k: Kernel, broker: Broker, intent: Mapping, action: Mapping[str, Any]) -> dict:
    crash_point("action.after_intent_commit")
    try:
        receipt = broker.perform(intent["idempotency_key"], action)
    except Exception as exc:   # connection dropped, timeout…: the effect may or may not have happened
        _settle(k, intent["intent_id"], intent["dispatch_count"], ("DISPATCHED",), "UNKNOWN", None, "supervisor")
        raise LupusError("EXECUTION_UNKNOWN", intent["intent_id"]) from exc
    crash_point("action.after_effect")
    return _settle(k, intent["intent_id"], intent["dispatch_count"], ("DISPATCHED", "UNKNOWN"), "CONFIRMED",
                   receipt, "supervisor")


def execute(k: Kernel, broker: Broker, run_id: str, token: int, action: Mapping[str, Any],
            idempotency_key: str, approval_id: str | None = None) -> dict:
    """begin -> perform -> record. Only an intent created by this very call is performed; a
    replayed key returns the stored state."""
    _not_nested(k)
    intent, created = begin(k, run_id, token, action, idempotency_key, approval_id)
    return _perform(k, broker, intent, action) if created else intent


def reconcile(k: Kernel, broker: Broker, intent_id: str) -> dict:
    """Establish what really happened by asking the destination, then record it with the
    receipt. Allowed only once the run that dispatched it is confirmed STOPPED: while it might
    still be in flight, "not found" proves nothing. If the destination cannot answer, the
    intent stays UNKNOWN."""
    _not_nested(k)
    intent = k.one("SELECT * FROM action_intent WHERE intent_id = ?", intent_id)
    if intent is None:
        raise LupusError("INTENT_NOT_FOUND", intent_id)
    if intent["status"] in ("CONFIRMED", "NOT_DONE"):
        return dict(intent)
    if k.one("SELECT 1 FROM run WHERE run_id = ? AND status <> 'STOPPED'", intent["run_id"]):
        raise LupusError("WRITER_NOT_STOPPED", "reconcile only after the dispatching run is confirmed stopped")
    try:
        receipt = broker.lookup(intent["idempotency_key"])
    except Exception as exc:
        raise LupusError("EXECUTION_UNKNOWN", f"destination unreachable: {type(exc).__name__}") from exc
    with k.tx():
        resolved = _settle(
            k, intent_id, intent["dispatch_count"], ("DISPATCHED", "UNKNOWN"),
            "CONFIRMED" if receipt is not None else "NOT_DONE",
            receipt if receipt is not None else {"lookup": "not_found", "at": k.now()}, "supervisor",
        )
        task = goals.get_task(k, intent["task_id"])
        unresolved = k.one(
            "SELECT 1 FROM action_intent WHERE task_id = ? AND status IN ('DISPATCHED','UNKNOWN')",
            intent["task_id"])
        if task["status"] in ("EXECUTION_UNKNOWN", "RECONCILING") and not unresolved:
            goals.move_task(k, intent["task_id"], "PENDING", "external action reconciled")
        return resolved


def redispatch(k: Kernel, broker: Broker, run_id: str, token: int, intent_id: str,
               action: Mapping[str, Any]) -> dict:
    """Continue the SAME intent after reconciliation proved it did not happen. Same idempotency
    key, same digest; the approval stays bound to this intent and must still be current."""
    _not_nested(k)
    with k.tx():
        run = runs.guard(k, run_id, token)
        intent = k.one("SELECT * FROM action_intent WHERE intent_id = ?", intent_id)
        if intent is None or intent["goal_id"] != run["goal_id"]:
            raise LupusError("INTENT_NOT_FOUND", intent_id)
        if intent["status"] != "NOT_DONE":
            raise LupusError("INTENT_NOT_RETRYABLE", intent["status"])
        if digest(action) != intent["action_digest"]:
            raise LupusError("APPROVAL_DIGEST_MISMATCH", intent_id)
        apr = k.one("SELECT * FROM approval WHERE approval_id = ?", intent["approval_id"])
        goal = goals.get(k, run["goal_id"])
        project = k.one("SELECT revocation_epoch FROM project WHERE project_id = ?", goal["project_id"])
        if (apr is None or apr["consumed_by_intent"] != intent_id or apr["revoked_at"] is not None
                or k.now() >= apr["expires_at"]
                or apr["acceptance_revision"] != goal["acceptance_revision"]
                or apr["revocation_epoch"] != project["revocation_epoch"]
                or apr["recovery_epoch"] != k.recovery_epoch):
            raise LupusError("NEEDS_APPROVAL", "the original approval is no longer current")
        k.run(
            "UPDATE action_intent SET status = 'DISPATCHED', run_id = ?, receipt = NULL, "
            "dispatch_count = dispatch_count + 1, updated_at = ? WHERE intent_id = ?",
            run_id, k.now(), intent_id,
        )
        k.emit("supervisor", "action.redispatched", "intent", intent_id)
        intent = dict(k.one("SELECT * FROM action_intent WHERE intent_id = ?", intent_id))
    return _perform(k, broker, intent, action)
