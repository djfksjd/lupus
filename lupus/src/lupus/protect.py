"""Protected verification inputs (keeps "the means of verification" out of the worker's reach).

A criterion's verifier may declare `protect: [paths]`: test files, test directories, runner
configuration. They are frozen once per acceptance revision, before the goal's first worker.

  before a worker starts   a protected file that no longer matches was changed by someone else
                           (the user, another tool). Lupus does not overwrite it; the task
                           waits for the user to confirm the new state (`refreeze`)
  after a worker exits     anything that changed was changed during this run: it is put back
                           (edited/deleted files restored, files added to a protected directory
                           removed) BEFORE verification, and reported to the next attempt

This stops the common ways a result is made to "pass" without doing the work: editing or
deleting tests, adding skips, dropping in a conftest. It is not a sandbox: a worker running as
the same user can still tamper in ways this does not see (e.g. shadowing a module the tests
import). Files too large to keep, or that look like credentials, are detected but not restored.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import goals
from .kernel import Kernel
from .util import LupusError, atomic_write, find_secret_bytes, sha256_bytes, sha256_file

MAX_KEEP = 512 * 1024
MAX_KEEP_WHOLE = 8 * 1024 * 1024      # per file when the whole project is frozen (`protect_except`)
MAX_KEEP_TOTAL = 100 * 1024 * 1024    # beyond this, further files are detect-only (or refused, see freeze)
MAX_FILES = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def _declared(k: Kernel, goal_id: str) -> list[str]:
    out: list[str] = []
    for c in goals.criteria(k, goal_id):
        out += list(c["verifier"].get("protect", []))
        if "protect_except" in c["verifier"]:
            out.append(".")          # the whole project, minus the excepted paths
    return sorted(set(out))


def _excepted(k: Kernel, goal_id: str) -> set[str]:
    """Paths a `protect_except` criterion leaves writable (e.g. the one test file being drafted)."""
    out: set[str] = set()
    for c in goals.criteria(k, goal_id):
        out |= {os.path.normpath(p) for p in c["verifier"].get("protect_except", [])}
    return out


def _inside(root: Path, rel: str) -> Path:
    real_root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(real_root / rel))
    if target != real_root and real_root not in target.parents:
        raise LupusError("PATH_ESCAPES_PROJECT", rel)
    return target


def _files_under(root: Path, rel_dir: str, strict: bool = False) -> list[str]:
    """Regular files under a protected directory. With `strict`, a symlink anywhere inside is
    refused: its content would be checked by the tests but could not be frozen or restored."""
    base = _inside(root, rel_dir)
    out = []
    for current, dirs, files in os.walk(base):
        for name in dirs:
            if strict and (Path(current) / name).is_symlink():
                raise LupusError("PROTECTED_SYMLINK", os.path.relpath(Path(current) / name, os.path.realpath(root)))
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink():
                if strict:
                    raise LupusError("PROTECTED_SYMLINK", os.path.relpath(path, os.path.realpath(root)))
                continue
            out.append(os.path.relpath(path, os.path.realpath(root)))
    return out


def _plain(real_root: Path, rel: str) -> Path | None:
    """The path inside the project, or None if any component on the way is a symlink (then a
    write through it could land outside the project, so nothing is written)."""
    current = real_root
    for part in Path(os.path.normpath(rel)).parts:
        if part == ".":
            continue
        current = current / part
        if current.is_symlink():
            return None
    return current


def freeze(k: Kernel, goal_id: str, root: Path, force: bool = False) -> int:
    """Record the protected files for the goal's current acceptance revision. Idempotent."""
    goal = goals.get(k, goal_id)
    revision = goal["acceptance_revision"]
    with k.tx():
        frozen = k.one("SELECT 1 FROM protected_file WHERE goal_id = ? AND acceptance_revision = ? LIMIT 1",
                       goal_id, revision) or k.one(
            "SELECT 1 FROM protected_dir WHERE goal_id = ? AND acceptance_revision = ? LIMIT 1", goal_id, revision)
        if frozen and not force:
            return 0
        k.run("DELETE FROM protected_file WHERE goal_id = ? AND acceptance_revision = ?", goal_id, revision)
        k.run("DELETE FROM protected_dir WHERE goal_id = ? AND acceptance_revision = ?", goal_id, revision)
        files: list[str] = []
        real_root = Path(os.path.realpath(root))
        for rel in _declared(k, goal_id):
            if _plain(real_root, rel) is None:
                raise LupusError("PROTECTED_SYMLINK", rel)      # refuse before any worker is dispatched
            target = _inside(root, rel)
            if target.is_dir():
                k.run("INSERT INTO protected_dir(goal_id, acceptance_revision, path) VALUES (?,?,?)",
                      goal_id, revision, rel)
                files += _files_under(root, rel, strict=True)
            elif target.is_file():
                files.append(rel)
            else:
                raise LupusError("PROTECTED_PATH_MISSING", rel)
        whole_project = bool(_excepted(k, goal_id))
        files = sorted({os.path.normpath(f) for f in files} - _excepted(k, goal_id))
        for c in goals.criteria(k, goal_id):
            for rel in c["verifier"].get("forbid_new", []):
                # A file that changes which tests run (conftest.py, pytest.ini, …) and does not
                # exist now must not be introduced later either.
                if not _inside(root, rel).exists() and os.path.normpath(rel) not in files:
                    k.run("INSERT OR IGNORE INTO protected_file(goal_id, acceptance_revision, path, sha256, content, "
                          "frozen_at) VALUES (?,?,?, 'absent', NULL, ?)", goal_id, revision, os.path.normpath(rel), k.now())
        if len(files) > MAX_FILES:
            raise LupusError("PROTECT_TOO_LARGE", f"{len(files)} files; limit {MAX_FILES}")
        kept_total = 0
        for rel in files:
            data = _inside(root, rel).read_bytes()
            limit = MAX_KEEP_WHOLE if whole_project else MAX_KEEP
            keep = len(data) <= limit and kept_total + len(data) <= MAX_KEEP_TOTAL and not find_secret_bytes(data)
            kept_total += len(data) if keep else 0
            if whole_project and not keep:
                # In whole-project mode the worker is not supposed to touch this file at all, and
                # Lupus could not put it back if it did. Do not start.
                raise LupusError("PROTECT_UNRESTORABLE",
                                 f"{rel}: too large or credential-like to keep a restore copy; move it out of the project "
                                 "or use a goal file instead")
            k.run("INSERT INTO protected_file(goal_id, acceptance_revision, path, sha256, content, frozen_at) "
                  "VALUES (?,?,?,?,?,?)", goal_id, revision, rel, sha256_bytes(data), data if keep else None, k.now())
        if files:
            k.emit("supervisor", "protect.frozen", "goal", goal_id, files=len(files), forced=force)
        return len(files)


def refreeze(k: Kernel, goal_id: str, root: Path, actor: str) -> int:
    """The user accepts the protected files as they are now (e.g. after editing a test)."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "refreeze")
    return freeze(k, goal_id, root, force=True)


def drift(k: Kernel, goal_id: str, root: Path) -> list[dict]:
    """How the protected files differ from their frozen state right now."""
    revision = goals.get(k, goal_id)["acceptance_revision"]
    rows = {r["path"]: r for r in k.q(
        "SELECT * FROM protected_file WHERE goal_id = ? AND acceptance_revision = ?", goal_id, revision)}
    out = []
    real_root = Path(os.path.realpath(root))
    for rel, row in rows.items():
        path = _plain(real_root, rel)
        if path is None:
            out.append({"path": rel, "change": "redirected", "row": row})     # a symlink now sits on the path
        elif row["sha256"] == "absent":
            if path.exists():
                out.append({"path": rel, "change": "added", "row": None})     # must not exist: remove it
        elif not path.is_file():
            out.append({"path": rel, "change": "deleted", "row": row})
        elif sha256_file(path) != row["sha256"]:
            out.append({"path": rel, "change": "modified", "row": row})
    for d in k.q("SELECT path FROM protected_dir WHERE goal_id = ? AND acceptance_revision = ?", goal_id, revision):
        base = _plain(real_root, d["path"])
        if base is not None and base.is_dir():
            excepted = _excepted(k, goal_id)
            for rel in _files_under(root, d["path"]):
                rel = os.path.normpath(rel)
                if rel not in rows and rel not in excepted:
                    out.append({"path": rel, "change": "added", "row": None})
    return out


def restore(k: Kernel, goal_id: str, root: Path, keep_dir: Path | None = None) -> dict[str, list[str]]:
    """Put protected files back after a worker ran. Returns what was undone and what could only
    be detected (no stored content).

    A hash cannot tell whether the worker or a person changed a file while the run was going
    on. So nothing is thrown away: every displaced version is first moved to `keep_dir` (in the
    runtime, outside the project) and can be recovered from there."""
    restored, unrestorable = [], []
    real_root = Path(os.path.realpath(root))
    for item in drift(k, goal_id, root):
        target = _plain(real_root, item["path"])
        if target is None:
            unrestorable.append(item["path"])      # never write through a symlinked path
            continue
        if keep_dir is not None and target.is_file() and (
                item["change"] == "added" or item["row"]["content"] is not None):
            saved = keep_dir / item["path"]
            saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write(saved, target.read_bytes())
        if item["change"] == "added":
            target.unlink(missing_ok=True)
            restored.append(f"{item['path']} (추가된 파일 제거)")
        elif item["row"]["content"] is None:
            unrestorable.append(item["path"])
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, bytes(item["row"]["content"]), mode=0o644)
            restored.append(f"{item['path']} ({'삭제' if item['change'] == 'deleted' else '수정'} 되돌림)")
    if restored or unrestorable:
        k.emit("supervisor", "protect.restored", "goal", goal_id, restored=restored, unrestorable=unrestorable,
               displaced_kept_in=str(keep_dir) if keep_dir else None)
    return {"restored": restored, "unrestorable": unrestorable}
