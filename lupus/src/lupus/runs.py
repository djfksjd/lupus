"""Runs, leases, fencing, writer ownership, attempts and no-progress accounting
(§3, §5, §6.2–6.3, DRIVER-01, LOOP-02, LOOP-04, HANDOFF-02).

Fixed rules:
  * claiming a task acquires the project's single writer slot in the same transaction
  * a result is integrated only when run, fencing token, lease, epochs and goal state all match
  * an expired lease is NOT proof that the process stopped; a new writer needs recorded stop
    evidence for the old one
  * attempts are recorded durably before dispatch and are never reset by crash, resume, a
    driver change or a renamed hypothesis
"""

from __future__ import annotations

import json
from typing import Mapping

from . import budget, goals, projects
from .kernel import Kernel
from .util import LupusError, canonical_json, group_alive, new_id, proc_start, sha256_json

BLOCK_STATUSES = ("NEEDS_ANSWER", "NEEDS_APPROVAL", "BUDGET_EXHAUSTED", "EXTERNAL_BLOCKED", "NO_PROGRESS",
                  "FAILED")


def get(k: Kernel, run_id: str) -> dict:
    row = k.one("SELECT * FROM run WHERE run_id = ?", run_id)
    if row is None:
        raise LupusError("RUN_NOT_FOUND", run_id)
    return dict(row)


# ---------------------------------------------------------------- claim / guard

def claim(k: Kernel, task_id: str, driver: str, auth_mode: str, ttl_ms: int | None = None) -> dict:
    """Atomically take the task lease and the project's writer slot."""
    with k.tx():
        task = goals.get_task(k, task_id)
        goal = goals.get(k, task["goal_id"])
        if goal["status"] != "ACTIVE":
            raise LupusError("GOAL_NOT_ACTIVE", goal["status"])
        if task["status"] != "PENDING":
            raise LupusError("TASK_NOT_CLAIMABLE", task["status"])
        if not goals.deps_done(k, task_id):
            raise LupusError("TASK_DEPS_NOT_DONE", task_id)
        project = projects.check_root(k, goal["project_id"])
        if not projects.provider_allowed(project, driver):
            raise LupusError("PROVIDER_NOT_APPROVED", driver)
        unverified = projects.capability_blockers(k, driver)
        if unverified:
            raise LupusError("CAPABILITY_UNVERIFIED", ";".join(unverified))
        unconfined = projects.confinement_blocker(k, project, driver)
        if unconfined:
            raise LupusError("UNCONFINED_READS_NOT_ALLOWED",
                             f"{driver} workers can read files outside the project; the user must allow it for this project")
        current = {c["id"] for c in goals.criteria(k, goal["goal_id"])}
        if not current & set(task["spec"]["criteria"]):
            # Its criteria were removed from the contract: running it would be spend without purpose.
            raise LupusError("TASK_WITHOUT_PURPOSE", task_id)
        if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]):
            raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
        if aux_alive(k, project["project_id"]):
            raise LupusError("WRITER_NOT_STOPPED", "a verifier process is still alive in this project")
        if k.one("SELECT 1 FROM action_intent WHERE goal_id = ? AND status IN ('DISPATCHED','UNKNOWN')",
                 goal["goal_id"]):
            raise LupusError("UNRESOLVED_ACTION", goal["goal_id"])
        run_id = new_id("run")
        token = k.next_counter("next_fencing_token")
        now = k.now()
        k.run(
            "INSERT INTO run(run_id, project_id, goal_id, task_id, execution_driver, auth_mode, "
            "fencing_token, status, lease_expires_at, revocation_epoch, recovery_epoch, policy_version, "
            "started_at) VALUES (?,?,?,?,?,?,?, 'ACTIVE', ?,?,?,?,?)",
            run_id, project["project_id"], goal["goal_id"], task_id, driver, auth_mode, token,
            now + (ttl_ms or k.policy["lease_ttl_ms"]), project["revocation_epoch"], k.recovery_epoch,
            project["policy_version"], now,
        )
        goals.move_task(k, task_id, "RUNNING")
        k.emit("supervisor", "run.claimed", "run", run_id, task_id=task_id, driver=driver, token=token)
        return get(k, run_id)


def guard(k: Kernel, run_id: str, token: int) -> dict:
    """Every integration of a run's result passes through here, inside the same transaction as
    the write it protects. Checks CURRENT authority, not what was true at claim time."""
    run = get(k, run_id)
    if run["fencing_token"] != token:
        raise LupusError("STALE_FENCING", run_id)
    if run["status"] != "ACTIVE":
        raise LupusError("RUN_NOT_ACTIVE", run["status"])
    if k.now() >= run["lease_expires_at"]:
        raise LupusError("LEASE_EXPIRED", run_id)
    project = projects.get(k, run["project_id"])
    if run["revocation_epoch"] != project["revocation_epoch"]:
        raise LupusError("REVOKED", "revocation_epoch changed")
    if run["policy_version"] != project["policy_version"]:
        raise LupusError("POLICY_CHANGED", run_id)
    if run["recovery_epoch"] != k.recovery_epoch:
        raise LupusError("RECOVERY_EPOCH_CHANGED", run_id)
    if projects.confinement_blocker(k, project, run["execution_driver"]):
        raise LupusError("UNCONFINED_READS_NOT_ALLOWED", "consent was withdrawn or confinement is no longer measured")
    if goals.get(k, run["goal_id"])["status"] != "ACTIVE":
        raise LupusError("GOAL_NOT_ACTIVE", run["goal_id"])
    return run


def heartbeat(k: Kernel, run_id: str, token: int, ttl_ms: int | None = None) -> int:
    """Extend a still-valid lease. An expired lease cannot be revived: after sleep or a stall the
    run has to be stopped, confirmed and claimed again."""
    with k.tx():
        guard(k, run_id, token)
        expires = k.now() + (ttl_ms or k.policy["lease_ttl_ms"])
        k.run("UPDATE run SET lease_expires_at = ? WHERE run_id = ?", expires, run_id)
        return expires


def attach_process(k: Kernel, run_id: str, token: int, pid: int) -> None:
    """Record the worker's process-group leader BEFORE it is allowed to exec (see adapters.gate),
    so a supervisor crash can never leave an unrecorded writer behind."""
    with k.tx():
        guard(k, run_id, token)
        start = proc_start(pid)
        if start is None:
            raise LupusError("PROCESS_NOT_FOUND", str(pid))
        k.run("UPDATE run SET pid = ?, proc_start = ? WHERE run_id = ?", pid, start, run_id)


# ---------------------------------------------------------------- auxiliary process groups

def _proc_alive(pid: int, start: str) -> bool:
    return proc_start(pid) == start or group_alive(pid)


def register_aux(k: Kernel, project_id: str, pid: int, purpose: str) -> None:
    """Durably record a non-worker process group (a command verifier) before it may exec."""
    with k.tx():
        start = proc_start(pid)
        if start is None:
            raise LupusError("PROCESS_NOT_FOUND", str(pid))
        k.run(
            "INSERT OR REPLACE INTO aux_process(pid, proc_start, project_id, purpose, created_at) VALUES (?,?,?,?,?)",
            pid, start, project_id, purpose, k.now(),
        )


def clear_aux(k: Kernel, pid: int) -> None:
    """Forget a recorded group, but only once it is really gone."""
    with k.tx():
        row = k.one("SELECT * FROM aux_process WHERE pid = ?", pid)
        if row is None:
            return
        if _proc_alive(row["pid"], row["proc_start"]):
            raise LupusError("WRITER_STILL_ALIVE", f"{row['purpose']} pid {pid}")
        k.run("DELETE FROM aux_process WHERE pid = ?", pid)


def aux_alive(k: Kernel, project_id: str | None = None) -> list[dict]:
    """Recorded groups that still exist. Rows whose group is gone are pruned."""
    alive = []
    with k.tx():
        for row in k.q("SELECT * FROM aux_process"):
            if _proc_alive(row["pid"], row["proc_start"]):
                if project_id is None or row["project_id"] == project_id:
                    alive.append(dict(row))
            else:
                k.run("DELETE FROM aux_process WHERE pid = ?", row["pid"])
    return alive


# ---------------------------------------------------------------- stopping

def writer_alive(run: Mapping) -> bool:
    """True while the recorded worker (or anything left in its process group) still exists.
    A reused pid that leads a new group reads as alive; that errs towards blocking, and the user
    can attest the stop."""
    pid = run["pid"]
    if pid is None:
        return False
    if proc_start(pid) == run["proc_start"]:
        return True
    return group_alive(pid)


def begin_stop(k: Kernel, run_id: str, actor: str = "supervisor") -> None:
    with k.tx():
        if k.run("UPDATE run SET status = 'STOPPING' WHERE run_id = ? AND status = 'ACTIVE'", run_id):
            k.emit(actor, "run.stopping", "run", run_id)


def confirm_stopped(k: Kernel, run_id: str, evidence: dict, actor: str = "supervisor") -> dict:
    """Release the writer slot. Requires evidence the kernel can check itself:
      process_exited  the recorded process group is gone (checked here, not trusted)
      never_spawned   no process was ever attached to this run
      user_attested   the user states the writer is stopped (actor must be 'user')
    Side effects of a non-clean stop: open attempts become ABANDONED (still counted), held
    reservations are charged in full as estimated usage, DISPATCHED actions become UNKNOWN."""
    with k.tx():
        run = get(k, run_id)
        if run["status"] == "STOPPED":
            return run
        kind = evidence.get("kind")
        if kind == "process_exited":
            if run["pid"] is None:
                raise LupusError("STOP_EVIDENCE_INVALID", "no process recorded")
            if writer_alive(run):
                raise LupusError("WRITER_STILL_ALIVE", str(run["pid"]))
        elif kind == "never_spawned":
            if run["pid"] is not None:
                raise LupusError("STOP_EVIDENCE_INVALID", "a process was attached")
        elif kind == "user_attested":
            if actor != "user":
                raise LupusError("USER_AUTHORITY_REQUIRED", "attestation")
            k.run("DELETE FROM aux_process WHERE project_id = ?", run["project_id"])
        else:
            raise LupusError("STOP_EVIDENCE_INVALID", str(kind))
        if aux_alive(k, run["project_id"]):
            raise LupusError("WRITER_STILL_ALIVE", "verifier process group")
        _close_run(k, run, {**evidence, "actor": actor}, result=evidence.get("result", "stopped"))
        return get(k, run_id)


def _close_run(k: Kernel, run: Mapping, evidence: dict, result: str) -> None:
    run_id, task_id = run["run_id"], run["task_id"]
    now = k.now()
    k.run(
        "UPDATE attempt SET outcome = 'ABANDONED', outcome_note = 'run stopped before the attempt was closed', "
        "ended_at = ? WHERE run_id = ? AND outcome IS NULL",
        now, run_id,
    )
    budget.settle_orphans(k, run_id)
    unknown = k.run(
        "UPDATE action_intent SET status = 'UNKNOWN', updated_at = ? WHERE run_id = ? AND status = 'DISPATCHED'",
        now, run_id,
    )
    k.run(
        "UPDATE run SET status = 'STOPPED', stop_evidence = ?, result = ?, ended_at = ?, closed_seq = ? "
        "WHERE run_id = ?",
        canonical_json(evidence), result, now, k.next_counter("next_order_seq"), run_id,
    )
    task = goals.get_task(k, task_id)
    if task["status"] == "RUNNING":
        if unknown or k.one(
            "SELECT 1 FROM action_intent WHERE task_id = ? AND status = 'UNKNOWN'", task_id
        ):
            goals.move_task(k, task_id, "EXECUTION_UNKNOWN", "external action outcome not established")
        else:
            goals.move_task(k, task_id, "PENDING", "run stopped; task resumable")
            # Its criteria may have been removed from the contract while it ran.
            goals.cancel_if_obsolete(k, task_id, "supervisor")
    k.emit("supervisor", "run.stopped", "run", run_id, evidence=evidence.get("kind"), result=result)
    goals.refresh(k, run["goal_id"])


def finish(k: Kernel, run_id: str, token: int, task_status: str, result: str, reason: str = "") -> None:
    """Normal end of a run by the supervisor that owns it, after its worker process exited.
    task_status: DONE, PENDING (more work later) or a waiting status with its reason."""
    with k.tx():
        run = guard(k, run_id, token)
        if k.one("SELECT 1 FROM attempt WHERE run_id = ? AND outcome IS NULL", run_id):
            raise LupusError("OPEN_ATTEMPT", run_id)
        if run["pid"] is not None and writer_alive(run):
            raise LupusError("WRITER_STILL_ALIVE", str(run["pid"]))
        if aux_alive(k, run["project_id"]):
            raise LupusError("WRITER_STILL_ALIVE", "verifier process group")
        if task_status not in ("DONE", "PENDING") + BLOCK_STATUSES:
            raise LupusError("TASK_STATUS_INVALID", task_status)
        goals.move_task(k, run["task_id"], task_status, reason)
        _close_run(k, run, {"kind": "process_exited" if run["pid"] else "never_spawned",
                            "actor": "supervisor"}, result)


# ---------------------------------------------------------------- attempts

def fingerprint(hypothesis_id: str, baseline_hash: str, change_scope: str, verifier_version: str,
                env_hash: str) -> str:
    return sha256_json([hypothesis_id, baseline_hash, change_scope, verifier_version, env_hash])


def start_attempt(
    k: Kernel,
    run_id: str,
    token: int,
    *,
    hypothesis_id: str,
    baseline_hash: str,
    change_scope: str,
    verifier_version: str,
    env_hash: str,
    new_evidence: str,
    work: Mapping[str, int],
    safety: Mapping[str, int],
    retry: Mapping | None = None,
) -> dict:
    """Record an attempt and reserve its budget before anything is dispatched.

    Refused when:
      DUPLICATE_ATTEMPT       same baseline/change/verifier/environment/hypothesis already got a
                              verdict and no resampling cap was declared up front (or it is used
                              up); or it was interrupted before a verdict too many times
      NO_NEW_EVIDENCE         the stated reason for trying again was already used on this task
      HYPOTHESIS_EXHAUSTED    more distinct tries than policy allows for one hypothesis
      NO_PROGRESS_LIMIT       the task already reached the consecutive no-progress limit
      BUDGET_EXHAUSTED        work + verification/shutdown/rollback reservation does not fit;
                              nothing is started (LOOP-04)
    The kernel checks structure and repetition; it cannot judge whether the stated evidence is
    actually meaningful (§11.2.1).
    """
    if not new_evidence.strip():
        raise LupusError("NO_NEW_EVIDENCE", "empty")
    if not safety or not any(int(v) > 0 for v in safety.values()):
        raise LupusError("SAFETY_RESERVATION_REQUIRED", "verification/shutdown/rollback budget")
    with k.tx():
        run = guard(k, run_id, token)
        task = goals.get_task(k, run["task_id"])
        goal = goals.get(k, run["goal_id"])
        if task["no_progress_streak"] >= k.policy["no_progress_limit"]:
            # Persisted, so a crash between closing the attempt and parking the task cannot be
            # used to slip in one more try.
            raise LupusError("NO_PROGRESS_LIMIT", f"streak={task['no_progress_streak']}")
        fp = fingerprint(hypothesis_id, baseline_hash, change_scope, verifier_version, env_hash)
        prior = k.q("SELECT * FROM attempt WHERE task_id = ? ORDER BY started_at, rowid", task["task_id"])
        same = [a for a in prior if a["fingerprint"] == fp]
        judged = [a for a in same if a["outcome"] in ("PROGRESS", "NEW_INFO", "NO_PROGRESS")]
        interrupted = [a for a in same if a["outcome"] in ("ABANDONED", "ENV_BLOCKED")]
        if judged:
            # Same inputs already produced a verdict: repeating needs a cap declared up front.
            cap = same[0]["retry_cap"]
            if cap is None or len(judged) >= cap:
                raise LupusError("DUPLICATE_ATTEMPT", f"same attempt judged {len(judged)}x, cap={cap}")
        if len(interrupted) >= k.policy["max_interrupted_retries"]:
            # Crash/quota/timeout before a verdict may be retried, but only a bounded number of times.
            raise LupusError("DUPLICATE_ATTEMPT", f"interrupted {len(interrupted)}x without a verdict")
        if not same:
            if any(a["new_evidence"].strip() == new_evidence.strip() for a in prior):
                raise LupusError("NO_NEW_EVIDENCE", "same justification as an earlier attempt")
            distinct = {a["fingerprint"] for a in prior if a["hypothesis_id"] == hypothesis_id}
            if len(distinct) >= k.policy["max_attempts_per_hypothesis"]:
                raise LupusError("HYPOTHESIS_EXHAUSTED", hypothesis_id)
        # Safety first: if verification/rollback cannot be paid for, the change must not start.
        safety_id = budget.reserve(k, goal["budget_id"], "safety", "verify+shutdown+rollback", safety, run_id)
        work_id = budget.reserve(k, goal["budget_id"], "work", f"attempt:{hypothesis_id}",
                                 {**work, "attempts": 1}, run_id)
        attempt_id = new_id("att")
        k.run(
            "INSERT INTO attempt(attempt_id, goal_id, task_id, run_id, hypothesis_id, acceptance_revision, "
            "fingerprint, new_evidence, retry_reason, retry_cap, work_reservation_id, safety_reservation_id, "
            "started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            attempt_id, goal["goal_id"], task["task_id"], run_id, hypothesis_id, goal["acceptance_revision"],
            fp, new_evidence, (retry or {}).get("reason") if not same else same[0]["retry_reason"],
            (retry or {}).get("cap") if not same else same[0]["retry_cap"], work_id, safety_id, k.now(),
        )
        k.run("UPDATE task SET attempt_count = attempt_count + 1 WHERE task_id = ?", task["task_id"])
        k.emit("supervisor", "attempt.started", "attempt", attempt_id, task_id=task["task_id"],
               hypothesis=hypothesis_id)
        return dict(k.one("SELECT * FROM attempt WHERE attempt_id = ?", attempt_id))


def finish_attempt(
    k: Kernel,
    run_id: str | None,
    token: int | None,
    attempt_id: str,
    outcome: str,
    note: str,
    work_actual: Mapping[str, int] | None,
    safety_actual: Mapping[str, int] | None,
    observation: str = "measured",
) -> dict:
    """Close an attempt and charge what it really cost.

    The COST is always settled, even when the result is rejected as stale (run_id/token no longer
    valid): a refused result does not make the spend disappear. In that case the outcome is
    forced to ABANDONED and nothing else is integrated.

    Progress must be backed by recorded evidence, not by confidence or output volume (§6.2):
      PROGRESS  at least one PASS evidence row was recorded by the verifier during this attempt
      NEW_INFO  some evidence (PASS or FAIL) was recorded during this attempt plus a note
    Whether a PASS is new compared with the previous attempt is decided by the supervisor loop.
    A late measurement for an attempt that was already closed is ignored: the earlier, larger
    estimate stands (over-counting is the safe direction).
    """
    if outcome not in ("PROGRESS", "NEW_INFO", "NO_PROGRESS", "ENV_BLOCKED", "ABANDONED"):
        raise LupusError("OUTCOME_INVALID", outcome)
    with k.tx():
        attempt = k.one("SELECT * FROM attempt WHERE attempt_id = ?", attempt_id)
        if attempt is None:
            raise LupusError("ATTEMPT_NOT_FOUND", attempt_id)
        already_closed = attempt["outcome"] is not None
        stale = None
        if not already_closed:
            try:
                if run_id != attempt["run_id"] or token is None:
                    raise LupusError("STALE_FENCING", attempt_id)
                guard(k, run_id, token)
            except LupusError as exc:
                stale, outcome, note = exc.code, "ABANDONED", f"result rejected: {exc.code}"
        budget.settle(k, attempt["work_reservation_id"],
                      None if work_actual is None else {**work_actual, "attempts": 1}, observation)
        budget.settle(k, attempt["safety_reservation_id"], safety_actual, observation)
        if already_closed:
            return {"outcome": attempt["outcome"], "must_stop": False, "stale": None}
        if stale is None:
            n_pass = k.one(
                "SELECT COUNT(*) AS n FROM evidence WHERE attempt_id = ? AND result = 'PASS'", attempt_id
            )["n"]
            n_any = k.one("SELECT COUNT(*) AS n FROM evidence WHERE attempt_id = ?", attempt_id)["n"]
            if outcome == "PROGRESS" and n_pass == 0:
                raise LupusError("PROGRESS_WITHOUT_EVIDENCE", attempt_id)
            if outcome == "NEW_INFO" and (n_any == 0 or not note.strip()):
                raise LupusError("NEW_INFO_WITHOUT_EVIDENCE", attempt_id)
        k.run(
            "UPDATE attempt SET outcome = ?, outcome_note = ?, ended_at = ? WHERE attempt_id = ?",
            outcome, note[:500], k.now(), attempt_id,
        )
        must_stop = False
        if stale is None:
            if outcome in ("PROGRESS", "NEW_INFO"):
                k.run("UPDATE task SET no_progress_streak = 0 WHERE task_id = ?", attempt["task_id"])
            elif outcome == "NO_PROGRESS":
                k.run("UPDATE task SET no_progress_streak = no_progress_streak + 1 WHERE task_id = ?",
                      attempt["task_id"])
                streak = goals.get_task(k, attempt["task_id"])["no_progress_streak"]
                must_stop = streak >= k.policy["no_progress_limit"]
        k.emit("supervisor", "attempt.finished", "attempt", attempt_id, outcome=outcome, stale=stale)
        return {"outcome": outcome, "must_stop": must_stop, "stale": stale}


# ---------------------------------------------------------------- restart recovery

def unstopped(k: Kernel) -> list[dict]:
    return [dict(r) for r in k.q("SELECT * FROM run WHERE status <> 'STOPPED' ORDER BY started_at")]


def json_evidence(run: Mapping) -> dict:
    return json.loads(run["stop_evidence"]) if run["stop_evidence"] else {}
