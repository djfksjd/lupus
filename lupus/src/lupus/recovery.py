"""Recovery objects, durable checkpoints, restart reconciliation, resume readiness
(§6.6, RECOVERY.md, CHECKPOINT-COMMIT-01, RECOVERY-PIN-01, RESUME-01/02).

Commit order is fixed and is NOT assumed to be one atomic transaction:
  1. write each recovery object to a temp file, fsync, rename, fsync the directory
  2. re-read and hash it; refuse tombstoned or secret-bearing content
  3. one DB transaction: verify objects again, CAS on the goal's checkpoint revision, insert the
     checkpoint and its pins
  4. restart reconciles DB against the object store: a checkpoint whose objects are missing or
     corrupt becomes BLOCKED; unreferenced objects are orphans
COMMITTED means "recoverable from here". It never means verified or done.

Object GC and checkpoint commit are both run by the supervisor and both take the DB write lock,
so the sweeper cannot delete an object between a commit's verification and its pin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import budget, goals, projects, runs
from .kernel import Kernel
from .util import (
    LupusError, atomic_write, canonical_json, crash_point, find_secret_bytes, fsync_dir, is_sha256,
    new_id, sha256_bytes, sha256_file, sha256_json,
)

# ---------------------------------------------------------------- object store


def _object_path(k: Kernel, sha: str) -> Path:
    if not is_sha256(sha):
        # Never build a filesystem path from anything but a real content hash.
        raise LupusError("OBJECT_HASH_INVALID", repr(sha)[:80])
    return k.objects_dir / sha[:2] / sha


def put_object(k: Kernel, data: bytes) -> tuple[str, int]:
    """Durably store content-addressed bytes. Returns (sha256, size)."""
    leak = find_secret_bytes(data)
    if leak:
        # Sensitive material is excluded before it is stored, not after (§6.6, §10.1).
        raise LupusError("OBJECT_CONTAINS_SECRET", leak)
    sha = sha256_bytes(data)
    if k.one("SELECT 1 FROM object_tombstone WHERE sha256 = ?", sha):
        raise LupusError("OBJECT_TOMBSTONED", sha)
    path = _object_path(k, sha)
    if path.exists() and sha256_file(path) == sha:
        os.utime(path)   # reused content counts as fresh for the orphan grace period
    else:
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fsync_dir(k.objects_dir)   # make the new shard directory entry itself durable
        crash_point("object.before_write")
        atomic_write(path, data)
        crash_point("object.after_rename")
        if sha256_file(path) != sha:
            raise LupusError("OBJECT_WRITE_CORRUPT", sha)
    return sha, len(data)


def object_ok(k: Kernel, sha: str, size: int | None = None) -> bool:
    path = _object_path(k, sha)
    try:
        if size is not None and path.stat().st_size != size:
            return False
        return sha256_file(path) == sha
    except OSError:
        return False


def read_object(k: Kernel, sha: str) -> bytes:
    if not object_ok(k, sha):
        raise LupusError("OBJECT_MISSING_OR_CORRUPT", sha)
    return _object_path(k, sha).read_bytes()


# ---------------------------------------------------------------- workspace manifest

def workspace_manifest(root: Path, rel_paths: list[str]) -> list[dict]:
    """Hash the listed files of the ORIGINAL project. Paths must stay inside the root; the
    project is never copied into the Vault or cloned (§3.2)."""
    root = Path(os.path.realpath(root))
    out = []
    for rel in sorted(set(rel_paths)):
        target = Path(os.path.realpath(root / rel))
        if target != root and root not in target.parents:
            raise LupusError("PATH_ESCAPES_PROJECT", rel)
        if target.is_file():
            out.append({"path": rel, "sha256": sha256_file(target), "size": target.stat().st_size})
        else:
            out.append({"path": rel, "sha256": None, "size": 0})
    return out


def compare_workspace(root: Path, manifest: list[dict]) -> dict[str, list[str]]:
    """What changed on disk since the checkpoint: partial writes, user edits, deletions."""
    current = {m["path"]: m for m in workspace_manifest(root, [m["path"] for m in manifest])}
    changed, missing = [], []
    for item in manifest:
        now = current[item["path"]]
        if now["sha256"] == item["sha256"]:
            continue
        (missing if now["sha256"] is None else changed).append(item["path"])
    return {"changed": changed, "missing": missing}


# ---------------------------------------------------------------- checkpoints

def latest(k: Kernel, goal_id: str) -> dict | None:
    row = k.one("SELECT * FROM checkpoint WHERE goal_id = ? ORDER BY revision DESC LIMIT 1", goal_id)
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(row["payload"])
    out["manifest"] = json.loads(row["manifest"])
    return out


def kernel_state(k: Kernel, goal_id: str) -> dict[str, Any]:
    """Counters the kernel itself records in every checkpoint. They come from the DB, never from
    a worker, so a resume cannot present smaller numbers."""
    goal = goals.get(k, goal_id)
    return {
        "acceptance_revision": goal["acceptance_revision"],
        "budget": {d: {"used": v["used"], "cap": v["cap"]} for d, v in budget.snapshot(k, goal["budget_id"]).items()},
        "tasks": {
            t["task_id"]: {"status": t["status"], "attempts": t["attempt_count"],
                           "no_progress_streak": t["no_progress_streak"]}
            for t in goals.tasks(k, goal_id)
        },
        "in_flight_intents": [
            r["intent_id"] for r in k.q(
                "SELECT intent_id FROM action_intent WHERE goal_id = ? AND status IN ('DISPATCHED','UNKNOWN')",
                goal_id)
        ],
    }


def commit(
    k: Kernel,
    goal_id: str,
    *,
    expected_revision: int,
    done: list[str],
    remaining: list[str],
    next_action: str,
    decisions: list[str] | None = None,
    files: list[str] | None = None,
    objects: list[Mapping] | None = None,
    run_id: str | None = None,
    token: int | None = None,
) -> dict:
    """Durably commit a checkpoint. `objects` items: {label, role, data: bytes}.

    With run_id/token the caller is the live run and its authority is re-checked inside the
    commit transaction (a revocation racing the commit wins). Without them the caller is the
    supervisor recording state while no writer is alive (e.g. after a quota error).
    """
    goal = goals.get(k, goal_id)
    project = projects.check_root(k, goal["project_id"])
    stored = []
    for item in objects or []:
        sha, size = put_object(k, item["data"])
        stored.append({"label": item["label"], "role": item["role"], "sha256": sha, "size": size})
    crash_point("checkpoint.after_objects")
    manifest = workspace_manifest(Path(project["canonical_root"]), files or [])
    new_bytes = sum(o["size"] for o in stored)

    with k.tx():
        if run_id is not None:
            run = runs.guard(k, run_id, token if token is not None else -1)
            task_id = run["task_id"]
        else:
            if k.one("SELECT 1 FROM run WHERE project_id = ? AND status = 'ACTIVE'", project["project_id"]):
                raise LupusError("WRITER_NOT_STOPPED", "supervisor checkpoint while a run is active")
            task_id = None
        project = projects.get(k, goal["project_id"])   # current epoch, re-read under the write lock
        prev = latest(k, goal_id)
        if (prev["revision"] if prev else 0) != expected_revision:
            raise LupusError("REVISION_CONFLICT", f"current={prev['revision'] if prev else 0}")
        for obj in stored:
            if k.one("SELECT 1 FROM object_tombstone WHERE sha256 = ?", obj["sha256"]):
                raise LupusError("OBJECT_TOMBSTONED", obj["sha256"])
            if not object_ok(k, obj["sha256"], obj["size"]):
                raise LupusError("OBJECT_MISSING_OR_CORRUPT", obj["sha256"])
        if not budget.fits_direct(k, goal["budget_id"], "storage_bytes", new_bytes):
            # Never drop recovery material to keep going; refuse the checkpoint instead.
            raise LupusError("BUDGET_EXHAUSTED", "storage_bytes")
        budget.charge_direct(k, goal["budget_id"], "storage_bytes", new_bytes)
        payload = {
            "done": done, "remaining": remaining, "next_action": next_action,
            "decisions": decisions or [], "kernel": kernel_state(k, goal_id),
        }
        leak = find_secret_bytes(canonical_json(payload).encode("utf-8"))
        if leak:
            raise LupusError("CHECKPOINT_CONTAINS_SECRET", leak)
        checkpoint_id = new_id("cp")
        revision = expected_revision + 1
        k.run(
            "INSERT INTO checkpoint(checkpoint_id, project_id, goal_id, task_id, run_id, revision, state, "
            "payload, payload_hash, manifest, policy_version, revocation_epoch, recovery_epoch, created_at) "
            "VALUES (?,?,?,?,?,?, 'COMMITTED', ?,?,?,?,?,?,?)",
            checkpoint_id, project["project_id"], goal_id, task_id, run_id, revision,
            canonical_json(payload), sha256_json(payload), canonical_json(manifest),
            project["policy_version"], project["revocation_epoch"], k.recovery_epoch, k.now(),
        )
        # Recovery material the new checkpoint does not replace is carried forward and pinned
        # again, so superseding a checkpoint never makes still-needed objects collectable.
        labels = {obj["label"] for obj in stored}
        carried = [o for o in (pinned_objects(k, prev["checkpoint_id"]) if prev else [])
                   if o["label"] not in labels]
        for obj in carried:
            if not object_ok(k, obj["sha256"], obj["size"]):
                raise LupusError("OBJECT_MISSING_OR_CORRUPT", f"carried object {obj['label']}")
        for obj in stored + carried:
            k.run(
                "INSERT INTO checkpoint_object(checkpoint_id, sha256, size, role, label) VALUES (?,?,?,?,?)",
                checkpoint_id, obj["sha256"], obj["size"], obj["role"], obj["label"],
            )
        if prev:
            k.run(
                "UPDATE checkpoint_object SET released_at = ? WHERE checkpoint_id = ? AND released_at IS NULL",
                k.now(), prev["checkpoint_id"],
            )
        k.emit("supervisor", "checkpoint.committed", "goal", goal_id, checkpoint_id=checkpoint_id,
               revision=revision, objects=len(stored))
    crash_point("checkpoint.after_db_commit")
    return latest(k, goal_id)


def pinned_objects(k: Kernel, checkpoint_id: str) -> list[dict]:
    return [dict(r) for r in k.q(
        "SELECT * FROM checkpoint_object WHERE checkpoint_id = ? AND released_at IS NULL ORDER BY label",
        checkpoint_id)]


def reconcile(k: Kernel) -> dict[str, Any]:
    """Run at supervisor start. Compares the DB with the object store and blocks what cannot be
    recovered instead of pretending it is ready."""
    blocked, orphans, stale_tmp = [], [], 0
    with k.tx():
        for goal in k.q("SELECT goal_id FROM goal WHERE status NOT IN ('DONE','CANCELLED')"):
            cp = latest(k, goal["goal_id"])
            if cp is None or cp["state"] != "COMMITTED":
                continue
            bad = [o["sha256"] for o in pinned_objects(k, cp["checkpoint_id"])
                   if not object_ok(k, o["sha256"], o["size"])]
            if bad:
                k.run(
                    "UPDATE checkpoint SET state = 'BLOCKED', blocked_reason = ? WHERE checkpoint_id = ?",
                    f"objects missing or corrupt: {','.join(s[:12] for s in bad)}", cp["checkpoint_id"],
                )
                k.emit("supervisor", "checkpoint.blocked", "goal", goal["goal_id"],
                       checkpoint_id=cp["checkpoint_id"], objects=bad)
                blocked.append(cp["checkpoint_id"])
        # Orphans: on disk, not pinned by any checkpoint. Deleted under the write lock, and only
        # once older than the grace period so an in-progress commit's objects are left alone.
        grace = k.policy["orphan_grace_ms"]
        now = k.now()
        for path in sorted(k.objects_dir.glob("??/*")):
            sha = path.name
            pinned = k.one(
                "SELECT 1 FROM checkpoint_object WHERE sha256 = ? AND released_at IS NULL", sha)
            if pinned:
                continue
            if now - int(path.stat().st_mtime * 1000) >= grace:
                path.unlink(missing_ok=True)
                orphans.append(sha)
        for path in list(k.objects_dir.glob("??/.*.tmp")) + list((k.objects_dir / "tmp").glob("*")):
            if now - int(path.stat().st_mtime * 1000) >= grace:
                path.unlink(missing_ok=True)
                stale_tmp += 1
        if orphans or stale_tmp:
            k.emit("supervisor", "objects.swept", "runtime", "objects", orphans=len(orphans), tmp=stale_tmp)
    return {"blocked_checkpoints": blocked, "orphans_removed": orphans, "tmp_removed": stale_tmp}


def tombstone(k: Kernel, sha: str, actor: str, reason: str) -> list[str]:
    """Delete recovery content for good. Deletion outranks any pin; checkpoints that needed the
    object are blocked and resumability is re-evaluated (§6.6)."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "tombstone")
    with k.tx():
        k.run("INSERT OR IGNORE INTO object_tombstone(sha256, reason, created_at) VALUES (?,?,?)",
              sha, reason, k.now())
        affected = [r["checkpoint_id"] for r in k.q(
            "SELECT DISTINCT checkpoint_id FROM checkpoint_object WHERE sha256 = ? AND released_at IS NULL", sha)]
        for checkpoint_id in affected:
            k.run("UPDATE checkpoint SET state = 'BLOCKED', blocked_reason = ? WHERE checkpoint_id = ?",
                  f"object deleted by user: {sha[:12]}", checkpoint_id)
        k.run("UPDATE checkpoint_object SET released_at = ? WHERE sha256 = ? AND released_at IS NULL",
              k.now(), sha)
        _object_path(k, sha).unlink(missing_ok=True)
        k.emit(actor, "object.tombstoned", "runtime", "objects", sha=sha, reason=reason, blocked=affected)
        return affected


# ---------------------------------------------------------------- readiness

def readiness(k: Kernel, goal_id: str, target_driver: str | None = None) -> dict[str, Any]:
    """Can this goal be resumed right now, and if not, exactly why not. `ready` is computed from
    current state every time; nothing stores a READY flag, and a stale Markdown export is never
    consulted (§6.8, RESUME-02/03)."""
    goal = goals.get(k, goal_id)
    blockers: list[str] = []
    notes: list[str] = []
    if goal["status"] != "ACTIVE":
        blockers.append(f"GOAL_STATUS:{goal['status']}")
    try:
        project = projects.check_root(k, goal["project_id"])
    except LupusError as exc:
        project = projects.get(k, goal["project_id"])
        blockers.append(exc.code)
    cp = latest(k, goal_id)
    workspace = {"changed": [], "missing": []}
    if cp is None:
        blockers.append("NO_CHECKPOINT")
    else:
        if cp["state"] != "COMMITTED":
            blockers.append(f"CHECKPOINT_BLOCKED:{cp['blocked_reason']}")
        for obj in pinned_objects(k, cp["checkpoint_id"]):
            if not object_ok(k, obj["sha256"], obj["size"]):
                blockers.append(f"CHECKPOINT_OBJECT_MISSING:{obj['label']}")
        blockers += goals.authority_blockers(k, goal_id)
        if "PROJECT_ROOT_CHANGED" not in blockers:
            workspace = compare_workspace(Path(project["canonical_root"]), cp["manifest"])
            if workspace["changed"] or workspace["missing"]:
                # Not a blocker: partial results are re-verified, never promoted to done.
                notes.append("WORKSPACE_DIFFERS_FROM_CHECKPOINT:verify-partial-artifact")
    if (k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"])
            or runs.aux_alive(k, project["project_id"])):
        blockers.append("WRITER_NOT_STOPPED")
    if k.one("SELECT 1 FROM action_intent WHERE goal_id = ? AND status IN ('DISPATCHED','UNKNOWN')", goal_id):
        blockers.append("UNRESOLVED_ACTION")
    exhausted = budget.exhausted_dimensions(k, goal["budget_id"])
    if exhausted:
        blockers.append(f"BUDGET_EXHAUSTED:{','.join(exhausted)}")
    task = goals.next_runnable(k, goal_id)
    if task is None and goal["status"] == "ACTIVE":
        blockers.append("NO_RUNNABLE_TASK")
    if target_driver is not None:
        if not projects.provider_allowed(project, target_driver):
            blockers.append(f"PROVIDER_NOT_APPROVED:{target_driver}")
        blockers += projects.capability_blockers(k, target_driver)
        unconfined = projects.confinement_blocker(k, project, target_driver)
        if unconfined:
            blockers.append(unconfined)
    return {
        "ready": not blockers,
        "blockers": blockers,
        "notes": notes,
        "goal_status": goal["status"],
        "checkpoint_revision": cp["revision"] if cp else 0,
        "next_task_id": task["task_id"] if task else None,
        "workspace": workspace,
        "subscription_remaining": "unknown",   # not observable; never guessed (§6.8)
    }


def revalidate(k: Kernel, goal_id: str, actor: str, note: str) -> dict:
    """After a revocation or recovery-epoch change, the user confirms the remaining data scope.
    Creates a new checkpoint revision bound to the current epochs; objects must still verify."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "revalidate")
    cp = latest(k, goal_id)
    if cp is None:
        raise LupusError("NO_CHECKPOINT", goal_id)
    objects = [{"label": o["label"], "role": o["role"], "data": read_object(k, o["sha256"])}
               for o in pinned_objects(k, cp["checkpoint_id"])]
    payload = cp["payload"]
    return commit(
        k, goal_id, expected_revision=cp["revision"], done=payload["done"], remaining=payload["remaining"],
        next_action=payload["next_action"], decisions=payload["decisions"] + [f"revalidated by user: {note}"],
        files=[m["path"] for m in cp["manifest"]], objects=objects,
    )
