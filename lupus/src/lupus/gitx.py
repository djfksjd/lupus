"""Git, for two things only.

  guard      A worker can write anywhere in the project, and that includes `.git`. A hook or a
             config entry planted there would run, unsandboxed, the next time anyone (Lupus or
             the user) uses git in the repository. The parts of `.git` that can make git execute
             something are therefore copied before a worker starts and put back afterwards.

  isolation  `--isolated`: the work happens in a separate checkout (a git worktree on its own
             branch, made from the committed HEAD), not in the user's working tree. The user's
             files, staged changes and branch are not touched while the goal runs or if it fails.
             `lupus diff` shows the result, `lupus accept` brings it over as one commit (only by
             fast-forward, and only if the user's branch has not moved), `lupus discard` drops it.

Lupus never pushes, never rewrites history, and runs git with hooks, the file-system monitor,
external diff/merge programs and commit signing switched off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import goals, projects, protect, timing
from .kernel import Kernel
from .util import LupusError, new_id, safe_path, scrubbed_env

SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
        "-c", "core.pager=cat", "-c", "diff.external=", "-c", "protocol.ext.allow=never", "-c", "advice.detachedHead=false"]
MAX_GUARD_FILE = 256 * 1024
MAX_HOOKS = 64


def _git() -> str:
    found = shutil.which("git", path=safe_path())
    if found is None:
        raise LupusError("GIT_UNAVAILABLE", "git was not found on PATH")
    return found


def run(root: Path | str, *args: str, check: bool = True, git_dir: str | None = None) -> str:
    """`git_dir`: use exactly this metadata directory for the work tree at `root`, whatever the
    `.git` entry found there says (in a checkout a worker wrote to, that entry is not trusted)."""
    bound = [f"--git-dir={git_dir}", f"--work-tree={root}"] if git_dir else []
    proc = subprocess.run([_git(), *SAFE, *bound, "-C", str(root), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env=scrubbed_env({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}), timeout=120)
    if check and proc.returncode != 0:
        raise LupusError("GIT_FAILED", f"git {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


# ---------------------------------------------------------------- guard

def _project_config_sources(root: Path) -> list[str]:
    """Files INSIDE the project that git reads configuration from, in any scope (the repository's
    config, per-worktree config, and anything a config — even the user's global one — includes
    from within the project). Asked of git once, before a worker exists; listing executes nothing.
    A source reached through a link is refused: its target could be swapped."""
    out: list[str] = []
    real_root = os.path.realpath(root)
    listing = run(root, "config", "--includes", "--show-origin", "--list", "-z", check=False)
    for entry in listing.split("\0"):
        if not entry.startswith("file:"):
            continue
        name = entry[5:].split("\t", 1)[0].split("\n", 1)[0]
        path = os.path.normpath(os.path.join(real_root, name))
        if not path.startswith(real_root + os.sep):
            continue                                   # the user's own files elsewhere: not writable by a worker
        rel = os.path.relpath(path, real_root)
        walked = Path(real_root)
        for part in Path(rel).parts:
            walked = walked / part
            if walked.is_symlink():
                raise LupusError("GIT_META_UNSUPPORTED", f"git reads configuration through a link inside the project: {rel}")
        out.append(rel)
    return sorted(set(out))


def guard_snapshot(root: Path) -> dict | None:
    """What in the project can make git execute something, as it is now: for a repository, every
    configuration source inside the project, and all of `.git/info` and `.git/hooks`; for a
    worktree, the `.git` locator file (its metadata lives elsewhere, out of a worker's reach).
    None when there is no repository.

    This is the second line. The first is that workers and checks cannot write `.git` at all
    (OS sandbox / Codex profile); this catches what runs without that (a container check, an
    opted-out sandbox, a platform without one).

    Anything that could not be put back faithfully (a link, something very large) is refused
    here, before a worker starts, rather than guessed at afterwards."""
    root = Path(root)
    meta = root / ".git"
    if meta.is_symlink():
        raise LupusError("GIT_META_UNSUPPORTED", ".git is a symbolic link")
    if meta.is_file():
        return {"locator": meta.read_bytes()}
    if not meta.is_dir():
        return None
    names = set(_project_config_sources(root)) | {".git/config", ".git/config.worktree"}
    listed: dict[str, set[str]] = {}
    for folder in ("info", "hooks"):
        base = meta / folder
        if base.is_symlink():
            raise LupusError("GIT_META_UNSUPPORTED", f".git/{folder} is a symbolic link")
        entries = sorted(base.iterdir()) if base.is_dir() else []
        if len(entries) > MAX_HOOKS:
            raise LupusError("GIT_META_UNSUPPORTED", f".git/{folder} holds more than {MAX_HOOKS} entries")
        listed[folder] = {p.name for p in entries}          # complete listing: anything else later was created later
        names |= {f".git/{folder}/{p.name}" for p in entries}
    files: dict[str, tuple[bytes, int]] = {}
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or path.is_dir() or (path.exists() and path.stat().st_size > MAX_GUARD_FILE):
            raise LupusError("GIT_META_UNSUPPORTED", f"{name} is a link, a folder or too large to guard")
        if path.is_file():
            files[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
    return {"files": files, "listed": listed}


def _plain_dir(root: Path, rel: str) -> None:
    """Make `rel` an ordinary directory chain inside the project: a link put in the way is removed,
    never followed."""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            current.unlink()
        if not current.exists():
            current.mkdir()


def guard_restore(root: Path, snap: dict | None) -> list[str]:
    """Put back what was snapshotted and remove what was created in `.git/info` and `.git/hooks`.
    Git itself is not run here: the configuration may be hostile until this has finished.
    Returns what was put back or removed."""
    if snap is None:
        return []
    root, undone = Path(root), []
    meta = root / ".git"
    if "locator" in snap:
        if meta.is_symlink() or not meta.is_file() or meta.read_bytes() != snap["locator"]:
            if meta.is_dir() and not meta.is_symlink():
                shutil.rmtree(meta)
            elif meta.is_symlink() or meta.exists():
                meta.unlink()
            meta.write_bytes(snap["locator"])
            undone.append(".git (locator)")
        return undone
    if meta.is_symlink() or not meta.is_dir():
        return [".git (replaced; not restored)"]
    for folder, before in snap["listed"].items():
        base = meta / folder
        if base.is_symlink():
            base.unlink()
            undone.append(f".git/{folder} (link removed)")
        for entry in (sorted(base.iterdir()) if base.is_dir() else []):
            if entry.name not in before:               # not there when the listing was taken: created since
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
                undone.append(f".git/{folder}/{entry.name} (added)")
    for name in (".git/config.worktree",):
        if name not in snap["files"] and ((root / name).is_symlink() or (root / name).is_file()):
            (root / name).unlink()
            undone.append(name + " (added)")
    for name, (data, mode) in snap["files"].items():
        path = root / name
        _plain_dir(root, os.path.dirname(name))
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data or (path.stat().st_mode & 0o777) != mode:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.is_symlink():
                path.unlink()
            path.write_bytes(data)
            path.chmod(mode)
            undone.append(name)
    return undone


@timing.measured("workspace")
def guard(root: Path) -> tuple:
    """Taken before anything of the project's is executed (a worker, a check, a baseline run)."""
    return guard_snapshot(root), protect.env_snapshot(root)


@timing.measured("workspace")
def unguard(k: Kernel, root: Path, taken: tuple, goal_id: str | None = None) -> list[str]:
    keep = k.runtime / "displaced" / (goal_id or "baseline") / "environment"
    undone = guard_restore(root, taken[0]) + [f"{name}/ (new environment set aside)" for name in protect.env_restore(root, taken[1], keep)]
    if undone:
        with k.tx():
            k.emit("supervisor", "git.meta_restored", "goal" if goal_id else "runtime", goal_id or "baseline", files=sorted(set(undone)))
    return undone


# ---------------------------------------------------------------- isolation

def _worktrees(k: Kernel) -> Path:
    # Not inside the runtime: that directory is closed to workers and verifiers.
    return Path(str(k.home) + "-worktrees")


def isolate(k: Kernel, project: dict, actor: str) -> dict:
    """A separate checkout of the project's committed HEAD, registered as a project of its own."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "isolated work is started by the user")
    origin = Path(project["canonical_root"])
    if not (origin / ".git").is_dir():
        raise LupusError("ISOLATION_UNSUPPORTED", "this project is not the top folder of a git repository")
    base = run(origin, "rev-parse", "--verify", "HEAD^{commit}").strip()
    name = new_id("wt")
    path = _worktrees(k) / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    branch = f"lupus/{name}"
    run(origin, "worktree", "add", "-q", "-b", branch, str(path), base)
    git_dir = run(path, "rev-parse", "--absolute-git-dir").strip()      # recorded now, before any worker exists
    try:
        isolated = projects.register(k, path, f"{project['name']}@{name[-6:]}", list(project["approved_providers"]))
        if project.get("allow_unconfined_reads"):
            projects.allow_unconfined_reads(k, isolated["project_id"], "user", True)
        with k.tx():
            k.emit("user", "isolation.created", "project", isolated["project_id"], origin_project=project["project_id"],
                   origin_root=str(origin), base=base, branch=branch, path=str(path), git_dir=git_dir)
    except BaseException:
        run(origin, "worktree", "remove", "--force", str(path), check=False)
        run(origin, "branch", "-D", branch, check=False)
        raise
    dirty = bool(run(origin, "status", "--porcelain").strip())
    return {**isolated, "isolated_from": str(origin), "base": base, "branch": branch, "origin_has_uncommitted_changes": dirty}


def info(k: Kernel, project_id: str) -> dict | None:
    row = k.one("SELECT payload FROM event WHERE type = 'isolation.created' AND aggregate_id = ? ORDER BY seq DESC LIMIT 1", project_id)
    return json.loads(row["payload"]) if row else None


def origin_root(k: Kernel, project: dict) -> Path | None:
    found = info(k, project["project_id"])
    return Path(found["origin_root"]) if found else None


def _of_goal(k: Kernel, goal_id: str) -> tuple[dict, dict]:
    goal = goals.get(k, goal_id)
    found = info(k, goal["project_id"])
    if found is None:
        raise LupusError("NOT_ISOLATED", "this goal did not run in an isolated checkout (start it with --isolated)")
    if k.one("SELECT 1 FROM event WHERE type IN ('isolation.accepted','isolation.discarded') AND aggregate_id = ?", goal["project_id"]):
        raise LupusError("ISOLATION_CLOSED", "this checkout was already accepted or discarded")
    return goal, found


def diff(k: Kernel, goal_id: str) -> str:
    """Everything the goal changed, against the commit it started from."""
    goal = goals.get(k, goal_id)
    found = info(k, goal["project_id"])
    if found is None:
        root = goals.project_root(k, goal_id)
        note = "# 격리되지 않은 목표입니다. 작업 폴더와 HEAD의 차이이며, 직접 바꾼 내용도 포함될 수 있습니다.\n"
        return note + run(root, "diff", "--no-ext-diff", "--no-textconv", "HEAD")
    path = found["path"]
    run(path, "add", "-A", git_dir=found["git_dir"])      # the checkout's own index; the user's index is a different one
    left_out = run(path, "status", "--porcelain", "--ignored", git_dir=found["git_dir"])
    ignored = [line[3:] for line in left_out.splitlines() if line.startswith("!! ")]
    note = ("# git이 무시하는 파일은 반영되지 않습니다: " + ", ".join(ignored[:8]) + "\n") if ignored else ""
    return note + run(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", found["base"], git_dir=found["git_dir"])


def accept(k: Kernel, goal_id: str, actor: str) -> dict:
    """Bring the verified result into the user's repository as one commit. Refused unless the goal
    is DONE; git itself refuses if an uncommitted file of the user would be overwritten. Nothing
    is forced.

    When the user's branch has moved since the work started (another result was accepted first,
    or the user committed), the result is combined with the branch as it is now, and that
    combination is what gets checked and accepted (see `_onto`). Any conflict refuses."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user accepts a result")
    goal, found = _of_goal(k, goal_id)
    if goal["status"] != "DONE":
        raise LupusError("GOAL_NOT_COMPLETE", f"{goal['status']}: only a verified result can be accepted (or `lupus discard`)")
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", goal["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", goal["project_id"])
    origin, path, git_dir = found["origin_root"], found["path"], found["git_dir"]
    head = run(origin, "rev-parse", "HEAD").strip()
    # What is accepted is checked again as the exact commit (below). Two things that re-check cannot
    # vouch for must still be as they were when the goal was verified: the frozen tests it runs,
    # and anything judged by a model or approved by the user.
    changed = sorted(d["path"] for d in protect.drift(k, goal_id, Path(path)))
    if changed:
        raise LupusError("RESULT_CHANGED", "frozen test files were changed after verification: " + ", ".join(changed[:5])
                         + ". Put them back (git checkout in the checkout) or discard the result")
    latest = goals.latest_evidence(k, goal_id)
    opinions = [c["id"] for c in goals.criteria(k, goal_id) if c["verifier"]["kind"] in ("judge", "user_approval") and (
        latest[c["id"]] is None or latest[c["id"]]["result"] != "PASS" or goals.evidence_stale(k, goal_id, c, latest[c["id"]]))]
    if opinions:
        raise LupusError("RESULT_CHANGED", "the document changed after it was judged or approved (" + ",".join(opinions) + ")")
    run(path, "add", "-A", git_dir=git_dir)
    if not run(path, "diff", "--cached", "--name-only", found["base"], git_dir=git_dir).strip():
        return {"accepted": False, "note": "the goal changed nothing"}
    checks = "\n".join(f"- {c['text']}" for c in goals.criteria(k, goal_id))
    run(path, "commit", "-q", "--no-verify", "-m", f"{goal['objective'][:72]}\n\nLupus goal {goal_id}. Verified by:\n{checks}",
        git_dir=git_dir)
    commit = run(path, "rev-parse", "HEAD", git_dir=git_dir).strip()
    try:
        if head != found["base"]:
            commit = _onto(k, goal, found, commit, head)
            if commit is None:
                # Everything this result changed is already in the user's branch (another result made
                # the same edits): there is nothing to add, and an empty commit would only be noise.
                # "Accepted" still has to mean "these files pass this goal's checks", and the branch
                # may hold other changes besides: the branch as it is gets checked like any result.
                _verify_commit(k, goal, found, head)
                if run(origin, "rev-parse", "HEAD").strip() != head:
                    # No merge follows on this path, so nothing else would notice that the branch
                    # that was just checked is no longer the one the user is on.
                    raise LupusError("BASELINE_CHANGED", "your branch moved while the result was being checked; nothing was "
                                                         "changed, run `lupus accept` again")
                run(path, "reset", "-q", "--soft", found["base"], git_dir=git_dir, check=False)
                with k.tx():
                    k.emit("user", "isolation.accepted", "project", goal["project_id"], goal=goal_id, commit=head, combined_with=head,
                           nothing_new=True)
                run(origin, "worktree", "remove", "--force", path, check=False)
                run(origin, "branch", "-D", found["branch"], check=False)
                return {"accepted": True, "commit": head, "files": [], "combined_with": head,
                        "note": "your branch already contains everything this result changed; no commit was added"}
        # The checks passed in the checkout, which may hold files git ignores. What the user gets is
        # the commit, so the commit alone is checked once more, in a clean export of it. After a
        # moved branch this is also the first time the COMBINED files are checked at all.
        _verify_commit(k, goal, found, commit)
        # --no-overwrite-ignore: an ignored file of the user's (an .env, say) is not replaced either.
        run(origin, "merge", "--ff-only", "--no-overwrite-ignore", "-q", commit)
    except LupusError as exc:
        run(path, "reset", "-q", "--soft", found["base"], git_dir=git_dir, check=False)      # the checkout stays as it was
        if exc.code != "GIT_FAILED":
            raise
        raise LupusError("ACCEPT_REFUSED_BY_GIT", exc.detail + f" — nothing was changed; the result stays on branch {found['branch']}") from exc
    with k.tx():
        k.emit("user", "isolation.accepted", "project", goal["project_id"], goal=goal_id, commit=commit,
               **({"combined_with": head} if head != found["base"] else {}))
    run(origin, "worktree", "remove", "--force", path, check=False)
    run(origin, "branch", "-D", found["branch"], check=False)
    return {"accepted": True, "commit": commit, "files": run(origin, "show", "--name-only", "--format=", commit).split(),
            **({"combined_with": head} if head != found["base"] else {})}


def _onto(k: Kernel, goal: dict, found: dict, commit: str, head: str) -> str | None:
    """The result as one commit on top of `head`, the user's branch as it is now; None when
    combining adds nothing to it.

    Integration in order, one result after another, is how Ruflo's worktree coordinator brings
    parallel writers together (MIT, Copyright (c) 2024-2026 ruvnet;
    `v3/@claude-flow/codex/src/worktrees/coordinator.ts`, `integrate`). Two things differ here.
    The merge is computed without touching any working tree (`git merge-tree`), so a conflict
    leaves nothing half-merged anywhere. And a clean merge is not yet a result: the caller
    checks the combined files against every criterion before the user's branch moves."""
    path, git_dir = found["path"], found["git_dir"]
    proc = subprocess.run(
        [_git(), *SAFE, f"--git-dir={git_dir}", "merge-tree", "--write-tree", "--name-only", f"--merge-base={found['base']}", head, commit],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=scrubbed_env({"GIT_TERMINAL_PROMPT": "0"}), timeout=120)
    lines = proc.stdout.split("\n")
    if proc.returncode == 1:
        clash = [name for name in lines[1:lines.index("")] if name] if "" in lines else []
        raise LupusError("BASELINE_CONFLICT", "your branch has moved since this work started and both changed the same place: "
                         + ", ".join(clash[:5]) + f". Nothing was changed; apply it yourself (branch {found['branch']}) or discard it")
    if proc.returncode != 0:      # e.g. a git too old for this: say that the branch moved, as before
        raise LupusError("BASELINE_CHANGED", "your branch has moved since this work started; look at `lupus diff` and apply it "
                                             f"yourself (branch {found['branch']}), or discard it")
    # A judge's verdict and the user's sign-off are about exact bytes and are not asked again here:
    # if combining changed what they looked at, they no longer cover what would be accepted.
    opinions = sorted({p for c in goals.criteria(k, goal["goal_id"]) if c["verifier"]["kind"] in ("judge", "user_approval")
                       for p in ([c["verifier"]["path"]] if "path" in c["verifier"] else c["verifier"].get("paths", []))})
    tree = lines[0].strip()
    if opinions and run(path, "diff", "--name-only", commit, tree, "--", *opinions, git_dir=git_dir).strip():
        raise LupusError("RESULT_CHANGED", "your branch has moved and changed a document that was judged or approved: "
                         + ", ".join(opinions[:5]))
    if tree == run(path, "rev-parse", f"{head}^{{tree}}", git_dir=git_dir).strip():
        return None
    message = run(path, "log", "-1", "--format=%B", commit, git_dir=git_dir).strip()
    return run(path, "commit-tree", tree, "-p", head, "-m", message, git_dir=git_dir).strip()


def _verify_commit(k: Kernel, goal: dict, found: dict, commit: str) -> None:
    """Check the files of `commit`, and nothing else, against every criterion of the goal."""
    from . import runs, verify           # (imported here: verify does not depend on git)
    criteria = goals.criteria(k, goal["goal_id"])
    tracked = set(run(found["path"], "ls-tree", "-r", "--name-only", "-z", commit, git_dir=found["git_dir"]).split("\0"))
    for c in criteria:      # what a criterion looks at must be part of what the user receives
        target = c["verifier"].get("path")
        if target and os.path.normpath(target) not in tracked:
            raise LupusError("ACCEPT_UNVERIFIED", f"{target}, which the goal was verified by, is not in the commit (ignored by git?). "
                                                  "Nothing was changed")
    with tempfile.TemporaryDirectory(prefix="lupus-accept-") as tmp:
        export = Path(os.path.realpath(tmp)) / "tree"
        export.mkdir()
        archive = subprocess.run([_git(), *SAFE, f"--git-dir={found['git_dir']}", "archive", "--format=tar", commit],
                                 capture_output=True, stdin=subprocess.DEVNULL, env=scrubbed_env(), timeout=120)
        if archive.returncode != 0:
            raise LupusError("GIT_FAILED", archive.stderr.decode("utf-8", "replace")[-200:])
        unpack = subprocess.run(["/usr/bin/tar", "-x", "-C", str(export)], input=archive.stdout, capture_output=True, timeout=120)
        if unpack.returncode != 0:
            raise LupusError("ACCEPT_UNVERIFIED", "the commit could not be exported for checking")
        for c in criteria:
            verifier = {key: value for key, value in c["verifier"].items() if key not in ("frozen_trees", "must_pass")}
            if verifier["kind"] in ("judge", "user_approval"):
                continue                     # their evidence was required to be current above
            links = []
            for tree in c["verifier"].get("frozen_trees", []):
                # Dependencies git ignores (node_modules, a virtualenv) are not in the commit. They were
                # fingerprinted as unchanged, so the export borrows them, read-only, from the checkout.
                source = Path(found["path"]) / tree
                if source.is_dir() and not os.path.lexists(export / tree):
                    os.symlink(source, export / tree)
                    links.append(str(source))
            if links:
                verifier["sandbox_read"] = links
            verdict, _, detail = verify.run(verifier, export, on_spawn=runs.aux_recorder(k, goal["project_id"], "accept-check"),
                                            on_exit=lambda pid: runs.clear_aux(k, pid))
            if verdict != "PASS":
                raise LupusError("ACCEPT_UNVERIFIED", "the committed files alone do not pass the check (is a file the result needs "
                                 f"ignored by git? see the top of `lupus diff`): {c['text'][:80]}: {detail[-200:]}")


def discard(k: Kernel, goal_id: str, actor: str) -> dict:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user discards a result")
    goal, found = _of_goal(k, goal_id)
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", goal["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", goal["project_id"])
    for row in k.q("SELECT goal_id FROM goal WHERE project_id = ? AND status NOT IN ('DONE','CANCELLED')", goal["project_id"]):
        goals.cancel(k, row["goal_id"], "user", "isolated checkout discarded")
    with k.tx():
        k.emit("user", "isolation.discarded", "project", goal["project_id"], goal=goal_id)
    run(found["origin_root"], "worktree", "remove", "--force", found["path"], check=False)
    shutil.rmtree(found["path"], ignore_errors=True)
    run(found["origin_root"], "worktree", "prune", check=False)
    run(found["origin_root"], "branch", "-D", found["branch"], check=False)
    return {"discarded": True, "branch": found["branch"]}
