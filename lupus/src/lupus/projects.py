"""Project registration and binding (§3.2, §4.1, §5).

A project is bound by a durable id to a canonical root plus the root's device/inode identity.
Unregistered folders are never bound implicitly; the home directory cannot be a project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .kernel import Kernel
from .util import LupusError, canonical_json, new_id

# execution_driver -> provider whose approval is required to send project data there
# "fake"/"fake_alt" are the scripted local drivers used by tests and fault injection.
DRIVER_PROVIDER = {"native_claude": "anthropic", "native_codex": "openai", "fake": "local", "fake_alt": "local"}


def _identity(root: Path) -> str:
    st = os.stat(root)
    return f"{st.st_dev}:{st.st_ino}"


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def register(k: Kernel, root: str | Path, name: str, approved_providers: list[str]) -> dict:
    canonical = Path(os.path.realpath(root))
    if not canonical.is_dir():
        raise LupusError("PROJECT_ROOT_NOT_DIR", str(canonical))
    if str(canonical) in ("/", os.path.realpath(Path.home())):
        raise LupusError("PROJECT_ROOT_TOO_BROAD", str(canonical))
    if _within(str(canonical), os.path.realpath(k.home)) or _within(os.path.realpath(k.home), str(canonical)):
        raise LupusError("PROJECT_OVERLAPS_RUNTIME", str(canonical))
    with k.tx():
        for row in k.q("SELECT project_id, canonical_root FROM project"):
            other = row["canonical_root"]
            if _within(str(canonical), other) or _within(other, str(canonical)):
                raise LupusError("PROJECT_ROOT_OVERLAP", f"{row['project_id']}:{other}")
        project_id = new_id("prj")
        k.run(
            "INSERT INTO project(project_id, alpha_id, name, canonical_root, root_identity, "
            "approved_providers, created_at) VALUES (?,?,?,?,?,?,?)",
            project_id, f"alpha_{project_id}", name, str(canonical), _identity(canonical),
            canonical_json(sorted(set(approved_providers))), k.now(),
        )
        k.emit("user", "project.registered", "project", project_id, name=name)
        return get(k, project_id)


def get(k: Kernel, project_id: str) -> dict:
    row = k.one("SELECT * FROM project WHERE project_id = ?", project_id)
    if row is None:
        raise LupusError("PROJECT_NOT_FOUND", project_id)
    out = dict(row)
    out["approved_providers"] = json.loads(row["approved_providers"])
    return out


def check_root(k: Kernel, project_id: str) -> dict:
    """The registered root must still be the same directory (not moved, replaced or symlinked
    elsewhere)."""
    project = get(k, project_id)
    root = Path(project["canonical_root"])
    try:
        same = os.path.realpath(root) == str(root) and _identity(root) == project["root_identity"]
    except OSError:
        same = False
    if not same:
        raise LupusError("PROJECT_ROOT_CHANGED", project["canonical_root"])
    return project


def resolve(k: Kernel, cwd: str | Path) -> dict | None:
    """Project that owns `cwd`, or None for unregistered folders."""
    here = os.path.realpath(cwd)
    for row in k.q("SELECT project_id, canonical_root FROM project"):
        if _within(here, row["canonical_root"]):
            return check_root(k, row["project_id"])
    return None


def remove(k: Kernel, project_id: str, actor: str) -> None:
    """Take back a registration that has no work under it (e.g. the wrong folder was registered).
    A project with goals is history and stays; use `revoke` to stop work on it."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "remove a project")
    with k.tx():
        get(k, project_id)
        if k.one("SELECT 1 FROM goal WHERE project_id = ?", project_id) or k.one(
                "SELECT 1 FROM aux_process WHERE project_id = ?", project_id):
            raise LupusError("PROJECT_IN_USE", "this project has goals; it cannot be removed")
        k.run("DELETE FROM service_call WHERE project_id = ? AND goal_id IS NULL AND status <> 'RUNNING'", project_id)
        k.run("DELETE FROM node WHERE project_id = ? AND NOT EXISTS (SELECT 1 FROM recall r WHERE r.node_id = node.node_id)", project_id)
        k.run("DELETE FROM alpha WHERE project_id = ?", project_id)
        try:
            k.run("DELETE FROM project WHERE project_id = ?", project_id)
        except Exception as exc:      # still referenced by something recorded: keep it
            raise LupusError("PROJECT_IN_USE", str(exc)) from exc
        k.emit(actor, "project.removed", "project", project_id)


def provider_allowed(project: dict, driver: str) -> bool:
    provider = DRIVER_PROVIDER.get(driver)
    return provider is not None and provider in project["approved_providers"]


# A native CLI may hold a lease only after `lupus probe --live` measured these on this machine.
REQUIRED_CAPABILITIES = {
    "native_claude": ("subscription_auth", "headless_exec"),
    "native_codex": ("subscription_auth", "headless_exec"),
}


def capability_blockers(k: Kernel, driver: str) -> list[str]:
    out = []
    for name in REQUIRED_CAPABILITIES.get(driver, ()):
        if k.one("SELECT 1 FROM capability WHERE adapter = ? AND name = ? AND status = 'verified'", driver, name) is None:
            out.append(f"CAPABILITY_UNVERIFIED:{driver}:{name}")
    return out


def confinement_blocker(k: Kernel, project: dict, driver: str) -> str | None:
    """A native driver whose worker can read outside the project (or for which that was never
    measured) needs the user's explicit consent for this project. Fails closed."""
    if driver not in REQUIRED_CAPABILITIES or project.get("allow_unconfined_reads"):
        return None
    row = k.one("SELECT status FROM capability WHERE adapter = ? AND name = 'read_confinement'", driver)
    if row is not None and row["status"] == "verified":
        return None
    return f"UNCONFINED_READS_NOT_ALLOWED:{driver}"


def allow_unconfined_reads(k: Kernel, project_id: str, actor: str, allow: bool = True) -> None:
    """The user accepts that workers of an unconfined driver can read any file this user can
    (SSH keys, tokens, other projects) while working on this project."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "consent to unconfined reads")
    with k.tx():
        get(k, project_id)
        k.run("UPDATE project SET allow_unconfined_reads = ? WHERE project_id = ?", 1 if allow else 0, project_id)
        k.emit(actor, "project.unconfined_reads", "project", project_id, allowed=allow)


def revoke(k: Kernel, project_id: str, actor: str, reason: str) -> int:
    """Withdraw previously granted access (§12, REVOKE-01). Bumps revocation_epoch, which
    invalidates every lease, unconsumed approval and checkpoint bound to the old epoch. Context a
    model already read cannot be recalled; affected runs must restart with reduced input."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "revocation is a user decision")
    with k.tx():
        project = get(k, project_id)
        epoch = project["revocation_epoch"] + 1
        k.run("UPDATE project SET revocation_epoch = ? WHERE project_id = ?", epoch, project_id)
        k.run(
            "UPDATE run SET status = 'STOPPING' WHERE project_id = ? AND status = 'ACTIVE'", project_id
        )
        k.run(
            "UPDATE approval SET revoked_at = ? WHERE consumed_at IS NULL AND revoked_at IS NULL "
            "AND goal_id IN (SELECT goal_id FROM goal WHERE project_id = ?)",
            k.now(), project_id,
        )
        k.emit(actor, "project.revoked", "project", project_id, epoch=epoch, reason=reason)
        return epoch
