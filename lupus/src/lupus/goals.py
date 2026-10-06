"""Goals, acceptance revisions, tasks, evidence, completion (§5, §6, ACCEPTANCE-01, LOOP-01).

State machine (docs/CONTRACT.md has the full table):
  * task states carry the real waiting reasons
  * goal.status is derived from its tasks, except the sticky PAUSED / CANCELLED / DONE
  * DONE is decided only from evidence bound to the current acceptance revision
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import budget, projects, verify
from .kernel import Kernel
from .util import LupusError, canonical_json, find_secret, new_id

# A goal shown as FAILED is derived from a failed branch and can be resolved by the user; only
# these are never recomputed.
GOAL_STICKY = ("PAUSED", "CANCELLED", "DONE")
GOAL_TERMINAL = ("CANCELLED", "DONE")
TASK_TERMINAL = ("DONE", "CANCELLED")
TASK_WAITING = (
    "NEEDS_ANSWER", "NEEDS_APPROVAL", "BUDGET_EXHAUSTED", "EXTERNAL_BLOCKED",
    "EXECUTION_UNKNOWN", "RECONCILING", "NO_PROGRESS", "FAILED",
)
# When several branches wait, the goal shows the one that most needs attention first.
WAIT_PRIORITY = (
    "EXECUTION_UNKNOWN", "RECONCILING", "NEEDS_APPROVAL", "NEEDS_ANSWER",
    "BUDGET_EXHAUSTED", "EXTERNAL_BLOCKED", "NO_PROGRESS", "FAILED",
)

TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PENDING": ("RUNNING", "CANCELLED"),
    "RUNNING": ("DONE", "PENDING", "CANCELLED") + TASK_WAITING,
    "NEEDS_ANSWER": ("PENDING", "CANCELLED"),
    "NEEDS_APPROVAL": ("PENDING", "CANCELLED"),
    "BUDGET_EXHAUSTED": ("PENDING", "CANCELLED"),
    "EXTERNAL_BLOCKED": ("PENDING", "CANCELLED"),
    "EXECUTION_UNKNOWN": ("RECONCILING", "PENDING", "CANCELLED"),
    "RECONCILING": ("PENDING", "EXECUTION_UNKNOWN", "CANCELLED"),
    "NO_PROGRESS": ("PENDING", "CANCELLED"),
    "FAILED": ("PENDING", "CANCELLED"),
    "DONE": (),
    "CANCELLED": (),
}

VERIFIER_KINDS = ("file_contains", "file_sha256", "command", "red_test", "document", "judge", "user_approval")
EXECUTING_KINDS = ("command", "red_test")


# ---------------------------------------------------------------- reads

def get(k: Kernel, goal_id: str) -> dict:
    row = k.one("SELECT * FROM goal WHERE goal_id = ?", goal_id)
    if row is None:
        raise LupusError("GOAL_NOT_FOUND", goal_id)
    return dict(row)


def get_task(k: Kernel, task_id: str) -> dict:
    row = k.one("SELECT * FROM task WHERE task_id = ?", task_id)
    if row is None:
        raise LupusError("TASK_NOT_FOUND", task_id)
    out = dict(row)
    out["spec"] = json.loads(row["spec"])
    return out


def criteria(k: Kernel, goal_id: str, revision: int | None = None) -> list[dict]:
    if revision is None:
        revision = get(k, goal_id)["acceptance_revision"]
    row = k.one("SELECT criteria FROM acceptance WHERE goal_id = ? AND revision = ?", goal_id, revision)
    if row is None:
        raise LupusError("ACCEPTANCE_NOT_FOUND", f"{goal_id}@{revision}")
    return json.loads(row["criteria"])


def tasks(k: Kernel, goal_id: str) -> list[dict]:
    rows = k.q("SELECT task_id FROM task WHERE goal_id = ? ORDER BY created_at, rowid", goal_id)
    return [get_task(k, r["task_id"]) for r in rows]


def deps_done(k: Kernel, task_id: str) -> bool:
    row = k.one(
        "SELECT COUNT(*) AS n FROM task_dep d JOIN task t ON t.task_id = d.depends_on "
        "WHERE d.task_id = ? AND t.status <> 'DONE'",
        task_id,
    )
    return row["n"] == 0


def next_runnable(k: Kernel, goal_id: str) -> dict | None:
    for task in tasks(k, goal_id):
        if task["status"] == "PENDING" and deps_done(k, task["task_id"]):
            return task
    return None


def wait_reasons(k: Kernel, goal_id: str) -> list[dict]:
    """Every waiting branch, not just the one shown as goal.status."""
    return [
        {"task_id": t["task_id"], "status": t["status"], "reason": t["wait_reason"] or ""}
        for t in tasks(k, goal_id) if t["status"] in TASK_WAITING
    ]


# ---------------------------------------------------------------- creation

_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}(?::[A-Za-z0-9._-]{1,100})?(?:@sha256:[0-9a-f]{64})?")


def _validate_criteria(items: list[dict]) -> list[dict]:
    if not items:
        raise LupusError("ACCEPTANCE_EMPTY", "a goal needs at least one measurable criterion")
    seen, out = set(), []
    for item in items:
        cid, text, verifier = item.get("id"), item.get("text"), item.get("verifier")
        if not cid or not text or not isinstance(verifier, dict):
            raise LupusError("CRITERION_INVALID", canonical_json(item))
        if verifier.get("kind") not in VERIFIER_KINDS:
            raise LupusError("VERIFIER_UNKNOWN", str(verifier.get("kind")))
        if verifier["kind"] in EXECUTING_KINDS and not (verifier.get("argv") and verifier.get("paths")):
            # A command's evidence is tied to the files it declares; without them it could never
            # be recognised as out of date.
            raise LupusError("CRITERION_INVALID", f"{cid}: command verifier needs argv and paths")
        if verifier["kind"] in ("document", "judge", "user_approval") and not verifier.get("path"):
            raise LupusError("CRITERION_INVALID", f"{cid}: {verifier['kind']} needs a path")
        if verifier["kind"] == "judge":
            rubric = verifier.get("rubric")
            if (not isinstance(rubric, list) or not 1 <= len(rubric) <= 12 or not isinstance(verifier.get("driver"), str)
                    or any(not isinstance(r, str) or not r.strip() or len(r) > 300 for r in rubric)):
                raise LupusError("CRITERION_INVALID", f"{cid}: judge needs a driver and a rubric of 1..12 short items")
        image = verifier.get("container")
        if image is not None and (not isinstance(image, str) or not _IMAGE.fullmatch(image)):
            raise LupusError("CRITERION_INVALID", f"{cid}: container must be an image name")
        env = verifier.get("env", {})
        if not isinstance(env, dict) or any(
                not isinstance(n, str) or not isinstance(v, str) or not n.replace("_", "").isalnum()
                or n in ("PATH", "HOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES") for n, v in env.items()):
            raise LupusError("CRITERION_INVALID", f"{cid}: env must map plain variable names to strings")
        declared = [verifier[key] for key in ("path",) if key in verifier] + list(verifier.get("paths", [])) + list(
            verifier.get("protect", [])) + list(verifier.get("protect_except", [])) + list(verifier.get("forbid_new", [])) + list(
            verifier.get("frozen_trees", []))
        if any(not isinstance(n, str) or not n or "/" in n or n in (".", "..") for n in verifier.get("forbid_new_names", [])):
            raise LupusError("CRITERION_INVALID", f"{cid}: forbid_new_names are plain file names")
        for rel in declared:
            # Project-relative only. An absolute path or a ".." could point verification, protection
            # or restoration at something outside the project.
            if not isinstance(rel, str) or not rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts:
                raise LupusError("CRITERION_INVALID", f"{cid}: path must be relative to the project: {rel!r}")
        if cid in seen:
            raise LupusError("CRITERION_DUPLICATE", cid)
        seen.add(cid)
        out.append({"id": cid, "text": text, "verifier": verifier})
    return out


def submit(
    k: Kernel,
    project_id: str,
    objective: str,
    criteria_: list[dict],
    caps: dict[str, int],
    actor: str = "user",
    parent_budget_id: str | None = None,
) -> dict:
    if actor != "user":
        # Starting a CLI is not consent to start work; a goal exists because the user asked (§4.1).
        raise LupusError("USER_AUTHORITY_REQUIRED", "goals are submitted by the user")
    items = _validate_criteria(criteria_)
    with k.tx():
        if k.one("SELECT 1 FROM project WHERE project_id = ?", project_id) is None:
            raise LupusError("PROJECT_NOT_FOUND", project_id)
        goal_id = new_id("goal")
        if parent_budget_id is None:
            # Shared caps the user set for this project or for everything (see alpha.py).
            shared = k.one("SELECT budget_id FROM alpha WHERE project_id = ? AND budget_id IS NOT NULL", project_id) or k.one(
                "SELECT budget_id FROM alpha WHERE scope = 'global' AND budget_id IS NOT NULL")
            parent_budget_id = shared["budget_id"] if shared else None
        budget_id = budget.create(k, f"goal:{goal_id}", caps, parent_budget_id)
        now = k.now()
        k.run(
            "INSERT INTO goal(goal_id, project_id, objective, status, budget_id, created_at, updated_at) "
            "VALUES (?,?,?, 'ACTIVE', ?,?,?)",
            goal_id, project_id, objective, budget_id, now, now,
        )
        k.run(
            "INSERT INTO acceptance(goal_id, revision, criteria, changed_by, reason, created_at) "
            "VALUES (?, 1, ?, ?, 'initial', ?)",
            goal_id, canonical_json(items), actor, now,
        )
        k.emit(actor, "goal.submitted", "goal", goal_id, criteria=[c["id"] for c in items])
        return get(k, goal_id)


def add_task(
    k: Kernel,
    goal_id: str,
    title: str,
    prompt: str,
    criterion_ids: list[str],
    depends_on: list[str] | None = None,
    inputs: list[str] | None = None,
) -> dict:
    with k.tx():
        goal = get(k, goal_id)
        if goal["status"] in GOAL_TERMINAL:
            raise LupusError("GOAL_TERMINAL", goal["status"])
        known = {c["id"] for c in criteria(k, goal_id)}
        unknown = [c for c in criterion_ids if c not in known]
        if unknown or not criterion_ids:
            # Every task must serve the active acceptance criteria (§1.1 rule 1).
            raise LupusError("TASK_WITHOUT_PURPOSE", ",".join(unknown) or "no criterion")
        task_id = new_id("task")
        now = k.now()
        k.run(
            "INSERT INTO task(task_id, goal_id, title, spec, status, created_at, updated_at) "
            "VALUES (?,?,?,?, 'PENDING', ?,?)",
            task_id, goal_id, title, canonical_json({"prompt": prompt, "criteria": criterion_ids, "inputs": list(inputs or [])}), now, now,
        )
        for dep in depends_on or []:
            dep_row = k.one("SELECT goal_id FROM task WHERE task_id = ?", dep)
            if dep_row is None or dep_row["goal_id"] != goal_id:
                raise LupusError("TASK_DEP_INVALID", dep)
            k.run("INSERT INTO task_dep(task_id, depends_on) VALUES (?,?)", task_id, dep)
        k.emit("supervisor", "task.added", "task", task_id, goal_id=goal_id)
        refresh(k, goal_id)
        return get_task(k, task_id)


# ---------------------------------------------------------------- state

def move_task(k: Kernel, task_id: str, new_status: str, reason: str = "", actor: str = "supervisor") -> None:
    with k.tx():
        task = get_task(k, task_id)
        if new_status not in TASK_TRANSITIONS[task["status"]]:
            raise LupusError("TASK_TRANSITION_INVALID", f"{task['status']}->{new_status}")
        k.run(
            "UPDATE task SET status = ?, wait_reason = ?, updated_at = ? WHERE task_id = ?",
            new_status, reason if new_status in TASK_WAITING else None, k.now(), task_id,
        )
        k.emit(actor, "task.status", "task", task_id, old=task["status"], new=new_status, reason=reason)
        refresh(k, task["goal_id"])


def refresh(k: Kernel, goal_id: str) -> str:
    """Recompute goal.status from task states. Waiting is a branch property: the goal waits only
    when no permitted branch can run (§6.5, WAIT-01)."""
    with k.tx():
        goal = get(k, goal_id)
        if goal["status"] in GOAL_STICKY:
            return goal["status"]
        all_tasks = tasks(k, goal_id)
        statuses = [t["status"] for t in all_tasks]
        runnable = "RUNNING" in statuses or next_runnable(k, goal_id) is not None
        waiting = [s for s in WAIT_PRIORITY if s in statuses]
        status = "ACTIVE" if runnable or not waiting else waiting[0]
        if status != goal["status"]:
            k.run(
                "UPDATE goal SET status = ?, revision = revision + 1, updated_at = ? WHERE goal_id = ?",
                status, k.now(), goal_id,
            )
            k.emit("supervisor", "goal.status", "goal", goal_id, old=goal["status"], new=status)
        return status


def _stop_goal_runs(k: Kernel, goal_id: str) -> None:
    k.run("UPDATE run SET status = 'STOPPING' WHERE goal_id = ? AND status = 'ACTIVE'", goal_id)


def _release_pins(k: Kernel, goal_id: str) -> None:
    # A finished goal no longer needs its frozen copies; without this every goal would leave a
    # copy of its protected files in the database for good.
    k.run("UPDATE protected_file SET content = NULL WHERE goal_id = ?", goal_id)
    k.run(
        "UPDATE checkpoint_object SET released_at = ? WHERE released_at IS NULL AND checkpoint_id IN "
        "(SELECT checkpoint_id FROM checkpoint WHERE goal_id = ?)",
        k.now(), goal_id,
    )


def _set_sticky(k: Kernel, goal_id: str, status: str, actor: str, reason: str) -> None:
    k.run(
        "UPDATE goal SET status = ?, revision = revision + 1, updated_at = ? WHERE goal_id = ?",
        status, k.now(), goal_id,
    )
    k.emit(actor, "goal.status", "goal", goal_id, new=status, reason=reason)


def pause(k: Kernel, goal_id: str, actor: str, reason: str = "") -> None:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "pause")
    with k.tx():
        if get(k, goal_id)["status"] in GOAL_TERMINAL:
            raise LupusError("GOAL_TERMINAL", goal_id)
        _stop_goal_runs(k, goal_id)
        _set_sticky(k, goal_id, "PAUSED", actor, reason)


def resume_paused(k: Kernel, goal_id: str, actor: str) -> str:
    """Only the user un-pauses. This does not reset attempts, streaks or budgets."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "resume")
    with k.tx():
        if get(k, goal_id)["status"] != "PAUSED":
            raise LupusError("GOAL_NOT_PAUSED", goal_id)
        k.run("UPDATE goal SET status = 'ACTIVE', revision = revision + 1 WHERE goal_id = ?", goal_id)
        k.emit(actor, "goal.resumed", "goal", goal_id)
        return refresh(k, goal_id)


def cancel(k: Kernel, goal_id: str, actor: str, reason: str = "") -> None:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "cancel")
    with k.tx():
        if get(k, goal_id)["status"] in GOAL_TERMINAL:
            raise LupusError("GOAL_TERMINAL", goal_id)
        _stop_goal_runs(k, goal_id)
        k.run(
            "UPDATE task SET status = 'CANCELLED', wait_reason = NULL, updated_at = ? "
            "WHERE goal_id = ? AND status NOT IN ('DONE','CANCELLED')",
            k.now(), goal_id,
        )
        _release_pins(k, goal_id)
        _set_sticky(k, goal_id, "CANCELLED", actor, reason)


def resolve_wait(k: Kernel, task_id: str, actor: str, note: str) -> None:
    """Return a waiting branch to PENDING. Who may do it depends on why it waits:
      NEEDS_ANSWER / NEEDS_APPROVAL / NO_PROGRESS / FAILED  -> user only (new input or decision)
      BUDGET_EXHAUSTED -> only once the user has raised a cap so something is available again
      EXTERNAL_BLOCKED -> supervisor may retry after its bounded backoff
    attempt_count and budget usage are untouched. no_progress_streak restarts only here, with
    the user's new input on record."""
    if not note.strip():
        raise LupusError("RESOLUTION_NOTE_REQUIRED", task_id)
    with k.tx():
        task = get_task(k, task_id)
        status = task["status"]
        if status in ("NEEDS_ANSWER", "NEEDS_APPROVAL", "NO_PROGRESS", "FAILED") and actor != "user":
            raise LupusError("USER_AUTHORITY_REQUIRED", status)
        if status == "BUDGET_EXHAUSTED":
            goal = get(k, task["goal_id"])
            left = budget.exhausted_dimensions(k, goal["budget_id"])
            if left:
                raise LupusError("BUDGET_EXHAUSTED", ",".join(left))
        if status not in ("NEEDS_ANSWER", "NEEDS_APPROVAL", "NO_PROGRESS", "FAILED",
                          "BUDGET_EXHAUSTED", "EXTERNAL_BLOCKED"):
            raise LupusError("TASK_NOT_WAITING", status)
        if status == "NO_PROGRESS":
            k.run("UPDATE task SET no_progress_streak = 0 WHERE task_id = ?", task_id)
        move_task(k, task_id, "PENDING", note, actor)


# ---------------------------------------------------------------- acceptance

def revise_acceptance(
    k: Kernel, goal_id: str, criteria_: list[dict], changed_by: str, reason: str, expected_revision: int
) -> int:
    """New acceptance revision. Anything that removes or rewrites an existing criterion narrows
    or changes the contract and needs the user; a worker can never relax what "done" means.
    Evidence and approvals bound to the old revision stop counting automatically."""
    items = _validate_criteria(criteria_)
    with k.tx():
        goal = get(k, goal_id)
        if goal["status"] in GOAL_TERMINAL:
            # A finished goal is not reopened; new requirements are a new goal (§6.1).
            raise LupusError("GOAL_TERMINAL", goal["status"])
        if goal["acceptance_revision"] != expected_revision:
            raise LupusError("REVISION_CONFLICT", f"current={goal['acceptance_revision']}")
        old = {c["id"]: c for c in criteria(k, goal_id)}
        new = {c["id"]: c for c in items}
        changed = [cid for cid, c in old.items() if new.get(cid) != c]
        if changed and changed_by != "user":
            raise LupusError("USER_AUTHORITY_REQUIRED", f"criteria changed or removed: {','.join(changed)}")
        executing = [cid for cid, c in new.items() if cid not in old and (
            c["verifier"]["kind"] in EXECUTING_KINDS or c["verifier"].get("protect") or c["verifier"].get("env_pass")
            or "protect_except" in c["verifier"] or c["verifier"].get("sandbox") is False
            or c["verifier"]["kind"] in ("judge", "user_approval") or c["verifier"].get("env")
            or c["verifier"].get("container"))]
        if executing and changed_by != "user":
            # A command verifier is arbitrary code execution on this machine; `protect` decides which
            # files get restored; `env_pass` hands out secrets. None of that is a supervisor's call.
            raise LupusError("USER_AUTHORITY_REQUIRED", f"criteria that execute commands: {','.join(executing)}")
        revision = expected_revision + 1
        k.run(
            "INSERT INTO acceptance(goal_id, revision, criteria, changed_by, reason, narrows, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            goal_id, revision, canonical_json(items), changed_by, reason, 1 if changed else 0, k.now(),
        )
        k.run(
            "UPDATE goal SET acceptance_revision = ?, revision = revision + 1, updated_at = ? WHERE goal_id = ?",
            revision, k.now(), goal_id,
        )
        k.emit(changed_by, "goal.acceptance_revised", "goal", goal_id, revision=revision,
               changed=changed, reason=reason)
        for task in tasks(k, goal_id):
            cancel_if_obsolete(k, task["task_id"], changed_by)   # a RUNNING one is handled when its run stops
        refresh(k, goal_id)
        return revision


def cancel_if_obsolete(k: Kernel, task_id: str, actor: str) -> bool:
    """Close a task none of whose criteria are in the current contract any more. Tasks that
    waited on it stop waiting: the work will never happen, and whether they can succeed without
    it is decided by their own verifiers, not by leaving them blocked forever."""
    with k.tx():
        task = get_task(k, task_id)
        if task["status"] in TASK_TERMINAL + ("RUNNING",):
            return False
        current = {c["id"] for c in criteria(k, task["goal_id"])}
        if current & set(task["spec"]["criteria"]):
            return False
        k.run("DELETE FROM task_dep WHERE depends_on = ?", task_id)
        move_task(k, task_id, "CANCELLED", "its acceptance criteria were removed", actor)
        return True


def record_evidence(
    k: Kernel,
    goal_id: str,
    criterion_id: str,
    verifier_version: str,
    artifact_hash: str,
    result: str,
    detail: str = "",
    task_id: str | None = None,
    attempt_id: str | None = None,
    *,
    acceptance_revision: int,
    verifier: dict,
) -> str:
    """Evidence is produced by the supervisor's deterministic verifier. The caller states which
    acceptance revision and which verifier definition it actually ran; if the contract changed
    in the meantime the result is refused instead of being stamped onto the new requirement.
    A worker's own report is not evidence (§12.2)."""
    with k.tx():
        goal = get(k, goal_id)
        if goal["acceptance_revision"] != acceptance_revision:
            raise LupusError("ACCEPTANCE_CHANGED", f"verified={acceptance_revision} current={goal['acceptance_revision']}")
        criterion = next((c for c in criteria(k, goal_id) if c["id"] == criterion_id), None)
        if criterion is None:
            raise LupusError("CRITERION_NOT_FOUND", criterion_id)
        if criterion["verifier"] != verifier:
            raise LupusError("ACCEPTANCE_CHANGED", f"verifier of {criterion_id} differs from the one that ran")
        evidence_id = new_id("ev")
        if find_secret(detail):
            detail = "(검증 출력 생략: 자격증명 패턴이 포함됨)"   # never stored, never fed back to a model
        k.run(
            "INSERT INTO evidence(evidence_id, goal_id, task_id, attempt_id, criterion_id, "
            "acceptance_revision, verifier, verifier_version, artifact_hash, result, detail, "
            "checked_at, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            evidence_id, goal_id, task_id, attempt_id, criterion_id, goal["acceptance_revision"],
            canonical_json(criterion["verifier"]), verifier_version, artifact_hash, result,
            detail[:800], k.now(), k.next_counter("next_order_seq"),
        )
        k.emit("verifier", "evidence.recorded", "goal", goal_id, evidence_id=evidence_id,
               criterion=criterion_id, result=result)
        return evidence_id


def relink_evidence(k: Kernel, evidence_id: str) -> str:
    """Carry evidence forward to the current acceptance revision. Allowed only when the criterion
    (text and verifier) is byte-identical in both revisions (§5, LOOP-03)."""
    with k.tx():
        ev = k.one("SELECT * FROM evidence WHERE evidence_id = ?", evidence_id)
        if ev is None:
            raise LupusError("EVIDENCE_NOT_FOUND", evidence_id)
        goal = get(k, ev["goal_id"])
        if ev["acceptance_revision"] == goal["acceptance_revision"]:
            return evidence_id
        old = next((c for c in criteria(k, ev["goal_id"], ev["acceptance_revision"])
                    if c["id"] == ev["criterion_id"]), None)
        new = next((c for c in criteria(k, ev["goal_id"]) if c["id"] == ev["criterion_id"]), None)
        if old is None or old != new:
            raise LupusError("EVIDENCE_NOT_REUSABLE", ev["criterion_id"])
        new_id_ = new_id("ev")
        k.run(
            "INSERT INTO evidence(evidence_id, goal_id, task_id, attempt_id, criterion_id, "
            "acceptance_revision, verifier, verifier_version, artifact_hash, result, detail, "
            "relinked_from, checked_at, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            new_id_, ev["goal_id"], ev["task_id"], ev["attempt_id"], ev["criterion_id"],
            goal["acceptance_revision"], ev["verifier"], ev["verifier_version"], ev["artifact_hash"],
            # Keeps its original position in time: relinking must not make old evidence look fresh.
            ev["result"], ev["detail"], evidence_id, ev["checked_at"], ev["seq"],
        )
        k.emit("supervisor", "evidence.relinked", "goal", ev["goal_id"], source=evidence_id, new=new_id_)
        return new_id_


def latest_evidence(k: Kernel, goal_id: str) -> dict[str, dict | None]:
    """Latest evidence per criterion for the CURRENT acceptance revision."""
    goal = get(k, goal_id)
    out: dict[str, dict | None] = {}
    for criterion in criteria(k, goal_id):
        row = k.one(
            "SELECT * FROM evidence WHERE goal_id = ? AND acceptance_revision = ? AND criterion_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            goal_id, goal["acceptance_revision"], criterion["id"],
        )
        out[criterion["id"]] = dict(row) if row else None
    return out


def project_root(k: Kernel, goal_id: str) -> Path:
    row = k.one("SELECT p.canonical_root FROM project p JOIN goal g ON g.project_id = p.project_id "
                "WHERE g.goal_id = ?", goal_id)
    return Path(row["canonical_root"])


def evidence_stale(k: Kernel, goal_id: str, criterion: dict, ev: dict) -> bool:
    """Evidence stops counting when what it looked at changed (§6.4):
      * any declared file differs from the hash recorded with the evidence
      * for `command` verifiers, additionally: a run ended in this project after the evidence
        was taken. A command can depend on files it did not declare, so its result is trusted
        only as long as no OTHER run has executed since.
    Not detected: a manual edit, outside the declared paths, with no run in between."""
    if ev["artifact_hash"] != verify.artifact_hash(criterion["verifier"], project_root(k, goal_id)):
        return True
    if criterion["verifier"]["kind"] in EXECUTING_KINDS:
        # The run that produced the evidence verified AFTER its own worker exited, so its own
        # closing does not count as "something executed since".
        own = k.one("SELECT run_id FROM attempt WHERE attempt_id = ?", ev["attempt_id"])
        later = k.one(
            "SELECT 1 FROM run r JOIN goal g ON g.goal_id = ? WHERE r.project_id = g.project_id "
            "AND r.run_id IS NOT ? AND (r.status <> 'STOPPED' OR r.closed_seq > ?)",
            goal_id, own["run_id"] if own else None, ev["seq"])
        return later is not None
    return False


def authority_blockers(k: Kernel, goal_id: str) -> list[str]:
    """The goal's latest checkpoint must be bound to the CURRENT revocation/recovery epoch and
    policy. After a revocation nothing proceeds — not a resume, not verification, not
    completion — until the user has revalidated the remaining scope."""
    row = k.one(
        "SELECT c.revocation_epoch, c.recovery_epoch, c.policy_version, p.revocation_epoch AS p_rev, "
        "p.policy_version AS p_pol FROM checkpoint c JOIN project p ON p.project_id = c.project_id "
        "WHERE c.goal_id = ? ORDER BY c.revision DESC LIMIT 1", goal_id)
    if row is None:
        return []
    out = []
    if row["revocation_epoch"] != row["p_rev"]:
        out.append("REVOCATION_EPOCH_CHANGED")
    if row["recovery_epoch"] != k.recovery_epoch:
        out.append("RECOVERY_EPOCH_CHANGED")
    if row["policy_version"] != row["p_pol"]:
        out.append("POLICY_VERSION_CHANGED")
    return out


def completion_blockers(k: Kernel, goal_id: str) -> list[str]:
    goal = get(k, goal_id)
    blockers = authority_blockers(k, goal_id)
    try:
        projects.check_root(k, goal["project_id"])
    except LupusError as exc:
        return blockers + [exc.code]     # evidence cannot be judged against a different directory
    if goal["status"] != "ACTIVE":
        blockers.append(f"GOAL_STATUS:{goal['status']}")
    by_id = {c["id"]: c for c in criteria(k, goal_id)}
    for cid, ev in latest_evidence(k, goal_id).items():
        if ev is None:
            blockers.append(f"NO_EVIDENCE:{cid}")
        elif ev["result"] != "PASS":
            blockers.append(f"EVIDENCE_FAIL:{cid}")
        elif evidence_stale(k, goal_id, by_id[cid], ev):
            blockers.append(f"EVIDENCE_STALE:{cid}")
    for task in tasks(k, goal_id):
        if task["status"] not in TASK_TERMINAL:   # a branch cancelled with its criteria does not block
            blockers.append(f"TASK_NOT_DONE:{task['task_id']}:{task['status']}")
    if k.one("SELECT 1 FROM run WHERE goal_id = ? AND status <> 'STOPPED'", goal_id):
        blockers.append("WRITER_NOT_STOPPED")
    if k.one("SELECT 1 FROM action_intent WHERE goal_id = ? AND status IN ('DISPATCHED','UNKNOWN')", goal_id):
        blockers.append("UNRESOLVED_ACTION")
    if k.one("SELECT 1 FROM attempt WHERE goal_id = ? AND outcome IS NULL", goal_id):
        blockers.append("OPEN_ATTEMPT")
    return blockers


def complete(k: Kernel, goal_id: str) -> None:
    """DONE means: every criterion of the current revision has PASS as its latest, still-current
    evidence, no task is left open, no writer is alive and no external action is unresolved. Once the goal is
    DONE the loop ends; optional improvement is a separate goal with its own budget (§6.1)."""
    with k.tx():
        blockers = completion_blockers(k, goal_id)
        if blockers:
            raise LupusError("GOAL_NOT_COMPLETE", ";".join(blockers))
        _release_pins(k, goal_id)
        _set_sticky(k, goal_id, "DONE", "supervisor", "all criteria verified")


def view(k: Kernel, goal_id: str) -> dict[str, Any]:
    goal = get(k, goal_id)
    return {
        **goal,
        "criteria": criteria(k, goal_id),
        "tasks": [
            {key: t[key] for key in ("task_id", "title", "status", "wait_reason", "attempt_count",
                                     "no_progress_streak")}
            for t in tasks(k, goal_id)
        ],
        "waiting": wait_reasons(k, goal_id),
        "budget": budget.snapshot(k, goal["budget_id"]),
        "evidence": {cid: (ev["result"] if ev else None) for cid, ev in latest_evidence(k, goal_id).items()},
    }
