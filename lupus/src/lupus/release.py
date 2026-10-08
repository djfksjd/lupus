"""When a request means that existing tests have to change.

A goal freezes the project's tests before any worker runs, and that is what makes "the existing
suite still passes" worth something. But some requests change behaviour that an existing test or
fixture pins (on the pilot: TOML 1.1 syntax that the old fixtures list as invalid). Then the
suite can never pass, the worker's edits to the tests are put back every time, and a correct
change ends as "no progress".

What is frozen is the user's decision, so this is the user's to resolve, in three steps:

  release   the user names frozen FILES (tests, fixtures) that may change for this goal. That
            authorises a proposal and nothing more: the frozen versions stay what is verified.
  proposal  the next worker's edits to exactly those files are taken aside before verification
            (everything protected is put back as always) and the task waits. Edits to any other
            protected file are undone as before.
  approval  the user is shown the exact diff and approves THAT diff (bound by hash to the goal,
            its acceptance revision, and the old and new content of every file). Only then does
            a new acceptance revision begin, whose frozen state is the previous one with the
            approved files replaced. All evidence is taken again under it.

Nothing is ever "allowed to fail": after approval the whole suite must pass, with the approved
versions. Not releasable: the check the user approved for this goal, and runner configuration.

The granularity is the file, because that is what can be frozen for seven runners in four
languages. A released file may hold tests that should NOT change; that is why the diff is
approved, and why the screen says which test names disappeared where it can tell.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from pathlib import Path

from . import goals, protect, runs
from . import timing
from .kernel import Kernel
from .util import LupusError, atomic_write, find_secret_bytes, sha256_bytes, sha256_file, sha256_json

MAX_FILE, MAX_TOTAL, MAX_FILES = 512 * 1024, 8 * 1024 * 1024, 200
_TEST_NAME = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)|^\s*(?:it|test)\(\s*['\"]([^'\"]+)|^func\s+(Test\w+)|^\s*fn\s+(test\w*)", re.M)


def _dir(k: Kernel, goal_id: str) -> Path:
    return k.runtime / "released" / goal_id


def _rows(k: Kernel, goal_id: str, revision: int) -> dict[str, dict]:
    return {r["path"]: dict(r) for r in k.q(
        "SELECT * FROM protected_file WHERE goal_id = ? AND acceptance_revision = ?", goal_id, revision)}


def _busy(k: Kernel, project_id: str) -> bool:
    return bool(k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project_id) or runs.aux_alive(k, project_id))


def _not_releasable(k: Kernel, goal_id: str) -> tuple[set[str], set[str]]:
    """(exact paths, file names) that stay frozen whatever the user names."""
    paths, names = set(), set()
    for row in k.q("SELECT payload FROM event WHERE type = 'check.approved' AND aggregate_id = ?", goal_id):
        paths.add(os.path.normpath(json.loads(row["payload"])["test"]))
    for c in goals.criteria(k, goal_id):
        paths |= {os.path.normpath(p) for p in c["verifier"].get("forbid_new", [])}
        # by name at any depth: a conftest.py or a runner's ini inside a released folder changes what runs
        names |= set(c["verifier"].get("forbid_new_names", [])) | {os.path.basename(p) for p in c["verifier"].get("forbid_new", [])}
    return paths, names


def _stays_frozen(path: str, fixed: tuple[set[str], set[str]]) -> bool:
    return path in fixed[0] or os.path.basename(path) in fixed[1]


# ---------------------------------------------------------------- release

def granted(k: Kernel, goal_id: str) -> list[str]:
    """Paths released for the goal's CURRENT acceptance revision (a directory ends with a separator)."""
    revision = goals.get(k, goal_id)["acceptance_revision"]
    out: set[str] = set()
    for row in k.q("SELECT payload FROM event WHERE type = 'release.granted' AND aggregate_id = ? ORDER BY seq", goal_id):
        seen = json.loads(row["payload"])
        if seen["revision"] == revision:
            out |= set(seen["paths"])
    return sorted(out)


def worker_note(k: Kernel, goal_id: str) -> str:
    """What a worker of this goal is told about released files. Built from the current state each
    time, so it is complete however many times the user released something."""
    allowed = granted(k, goal_id)
    if not allowed:
        return ""
    return ("사용자가 이 목표에서 변경을 허용한 보호 파일(테스트·fixture): " + ", ".join(allowed) + ". 요청이 이 파일들이 정해 둔 동작을 "
            "바꾸는 것이라면 요청에 맞게 고쳐라. 그 밖의 테스트와 설정은 여전히 고정이고 고쳐도 되돌려진다. 고친 내용은 사용자가 diff를 "
            "보고 승인해야 반영된다. 요청과 무관한 기대값은 바꾸지 말고, 테스트를 지워서 통과시키지 마라.")


def root_of(k: Kernel, goal_id: str) -> str:
    return str(goals.project_root(k, goal_id))


def _is_released(path: str, allowed: list[str]) -> bool:
    return any(path == a or (a.endswith(os.sep) and path.startswith(a)) for a in allowed)


@timing.operation("acceptance")
def grant(k: Kernel, goal_id: str, paths: list[str], actor: str) -> dict:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user releases a frozen file")
    goal = goals.get(k, goal_id)
    if goal["status"] in goals.GOAL_TERMINAL:
        raise LupusError("GOAL_TERMINAL", goal["status"])
    if _busy(k, goal["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", goal["project_id"])
    rows = _rows(k, goal_id, goal["acceptance_revision"])
    dirs = [r["path"] for r in k.q("SELECT path FROM protected_dir WHERE goal_id = ? AND acceptance_revision = ?",
                                   goal_id, goal["acceptance_revision"])]
    fixed_paths, fixed_names = _not_releasable(k, goal_id)
    accepted: list[str] = []
    for raw in paths:
        rel = os.path.normpath(raw)
        if rel.startswith("..") or os.path.isabs(rel) or rel == ".":
            raise LupusError("RELEASE_REFUSED", f"{raw}: name a frozen file or folder inside the project")
        as_dir = rel + os.sep
        under = [p for p in rows if p.startswith(as_dir)]
        if rel in rows and not rows[rel]["sha256"].startswith(("tree:", "absent")) and not rel.endswith(os.sep):
            chosen = [rel]
            accepted.append(rel)
        elif rel in rows and rows[rel]["sha256"] == "absent":
            chosen = []                    # frozen as absent (its deletion was approved earlier): it may be proposed again
            accepted.append(rel)
        elif under or any(d == rel or d.startswith(as_dir) for d in dirs):
            chosen = under                 # a frozen folder: whatever is in it, and what gets added to it
            accepted.append(as_dir)
        elif any(rel.startswith(os.path.normpath(d) + os.sep) for d in dirs) and not os.path.lexists(os.path.join(root_of(k, goal_id), rel)):
            chosen = []                    # a file that does not exist yet, inside a frozen folder: it may be added
            accepted.append(rel)
        else:
            raise LupusError("RELEASE_REFUSED", f"{raw}: not a frozen file or folder of this goal")
        for path in [*chosen, rel]:
            if path in fixed_paths or os.path.basename(path) in fixed_names:
                raise LupusError("RELEASE_REFUSED", f"{path}: the check you approved for this goal and the test runner's "
                                                    "configuration stay frozen")
        if any(rows[p]["content"] is None for p in chosen):
            raise LupusError("RELEASE_REFUSED", f"{raw}: holds a file Lupus keeps no copy of (too large or credential-like)")
    with k.tx():
        k.emit("user", "release.granted", "goal", goal_id, paths=sorted(set(accepted)), revision=goal["acceptance_revision"])
        # (which files, the worker is told from the state itself on every prompt: see `worker_note`)
        note = "사용자가 보호 파일 일부의 변경을 허용했다: " + ", ".join(sorted(set(accepted)))
        resumed = []
        for task in goals.tasks(k, goal_id):
            if task["status"] in ("NO_PROGRESS", "NEEDS_ANSWER", "FAILED"):
                goals.resolve_wait(k, task["task_id"], "user", note)
                resumed.append(task["task_id"])
    return {"released": granted(k, goal_id), "resumed_tasks": resumed}


# ---------------------------------------------------------------- proposal

def digest(goal_id: str, revision: int, entries: list[dict]) -> str:
    return sha256_json([goal_id, revision, sorted([e["path"], e["change"], e["old_sha256"], e["new_sha256"], e["mode"]] for e in entries)])


@timing.measured("workspace")
def capture(k: Kernel, goal_id: str, root: Path) -> dict | None:
    """Called after a worker and BEFORE the protected files are put back: take aside what the
    worker did to released files. Returns the proposal, or None when it touched none of them."""
    allowed = granted(k, goal_id)
    if not allowed:
        return None
    revision = goals.get(k, goal_id)["acceptance_revision"]
    real_root = Path(os.path.realpath(root))
    entries, total = [], 0
    dest = _dir(k, goal_id)
    shutil.rmtree(dest, ignore_errors=True)
    fixed = _not_releasable(k, goal_id)
    for item in sorted(protect.drift(k, goal_id, root), key=lambda i: i["path"]):
        path = item["path"]
        if not _is_released(path, allowed) or item["change"] not in ("modified", "added", "deleted", "mode"):
            continue
        if _stays_frozen(path, fixed):
            continue                                  # e.g. a conftest.py added inside a released folder: undone like any other
        target = protect._plain(real_root, path)
        if target is None:
            continue                                  # a link in the way: left to the ordinary restore, which refuses it
        entry = {"path": path, "change": item["change"], "old_sha256": item["row"]["sha256"] if item["row"] else None,
                 "new_sha256": None, "mode": None}
        if item["change"] != "deleted":
            data = target.read_bytes()
            total += len(data)
            if len(data) > MAX_FILE or total > MAX_TOTAL or len(entries) >= MAX_FILES or find_secret_bytes(data):
                shutil.rmtree(dest, ignore_errors=True)
                raise LupusError("RELEASE_TOO_LARGE", f"{path}: a proposed change that is too large, or credential-like, to hold for approval")
            kept = dest / "files" / path
            kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write(kept, data)
            entry.update(new_sha256=sha256_bytes(data), mode=target.stat().st_mode & 0o777)
        entries.append(entry)
    if not entries:
        return None
    proposal = {"goal_id": goal_id, "revision": revision, "entries": entries, "digest": digest(goal_id, revision, entries)}
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(dest / "proposal.json", json.dumps(proposal, ensure_ascii=False, indent=1).encode())      # written last: complete or absent
    with k.tx():
        k.emit("supervisor", "release.proposed", "goal", goal_id, revision=revision, digest=proposal["digest"],
               paths=[e["path"] for e in entries])
    return proposal


def _held(k: Kernel, goal_id: str) -> dict | None:
    try:
        return json.loads((_dir(k, goal_id) / "proposal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _was_approved(k: Kernel, goal_id: str, found: dict) -> bool:
    return k.one("SELECT 1 FROM event WHERE type = 'release.approved' AND aggregate_id = ? AND json_extract(payload, '$.digest') = ?",
                 goal_id, found["digest"]) is not None


def proposal(k: Kernel, goal_id: str) -> dict | None:
    """The proposal that waits for the user's decision, if any."""
    found = _held(k, goal_id)
    return None if found is None or _was_approved(k, goal_id, found) else found


@timing.measured("workspace")
def complete(k: Kernel, goal_id: str, root: Path) -> bool:
    """Finish an approval that was recorded but whose files did not all get written (the process
    stopped in between). The record is the decision, and the record is all that is used: the
    paths come from the approval event and the bytes from the frozen state it committed. The
    held proposal is only the sign that something was left to do; nothing is read from it."""
    found = _held(k, goal_id)
    if found is None:
        return False
    row = k.one("SELECT payload FROM event WHERE type = 'release.approved' AND aggregate_id = ? AND json_extract(payload, '$.digest') = ?",
                goal_id, str(found.get("digest")))
    if row is None:
        return False
    approved = json.loads(row["payload"])
    if goals.get(k, goal_id)["acceptance_revision"] != approved["revision_to"]:
        return False
    frozen = _rows(k, goal_id, approved["revision_to"])
    real_root = Path(os.path.realpath(root))
    for path in approved["paths"]:
        target = protect._plain(real_root, path)
        entry = frozen.get(path)
        if target is None or entry is None:
            continue                                  # left to the ordinary drift check, which stops and asks
        if entry["sha256"] == "absent":
            target.unlink(missing_ok=True)
        elif entry["content"] is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, bytes(entry["content"]), mode=entry["mode"])
    shutil.rmtree(_dir(k, goal_id), ignore_errors=True)
    return True


@timing.measured("workspace")
def apply(k: Kernel, goal_id: str, root: Path, found: dict) -> None:
    """Write the proposed versions into the project (never through a link)."""
    real_root = Path(os.path.realpath(root))
    for entry in found["entries"]:
        target = protect._plain(real_root, entry["path"])
        if target is None:
            raise LupusError("RELEASE_REFUSED", f"{entry['path']}: a link is in the way")
        if entry["change"] == "deleted":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, (_dir(k, goal_id) / "files" / entry["path"]).read_bytes(), mode=entry["mode"])


def note_provisional(k: Kernel, goal_id: str, found: dict, verdicts: dict[str, str], details: dict[str, str]) -> None:
    """How the checks came out with the proposed versions in place. Information for the approval
    screen only: it is never evidence, and nothing is DONE because of it."""
    with k.tx():
        k.emit("supervisor", "release.provisional", "goal", goal_id, digest=found["digest"], verdicts=verdicts,
               details={cid: " ".join(text.split())[-300:] for cid, text in details.items()})


_UNSEEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff]")


def _seen(text: str) -> str:
    """Text from a worker, as it goes to the user's terminal: a character that would move the
    cursor, clear the screen or hide what follows is printed as its code instead of being obeyed."""
    return _UNSEEN.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)


def _names(text: str) -> set[str]:
    return {next(g for g in m.groups() if g) for m in _TEST_NAME.finditer(text)}


def show(k: Kernel, goal_id: str) -> str:
    """What the user approves: every released file's exact change, and what can be said about it."""
    found = proposal(k, goal_id)
    if found is None:
        raise LupusError("RELEASE_NOTHING_PROPOSED", goal_id)
    rows = _rows(k, goal_id, found["revision"])
    out = [f"보호 파일 변경 제안 ({len(found['entries'])}개 파일). 승인하면 이 판본으로 다시 고정되고, 모든 검사를 처음부터 다시 실행합니다."]
    for entry in found["entries"]:
        path = entry["path"]
        old = bytes(rows[path]["content"]) if path in rows and rows[path]["content"] is not None else b""
        new = (_dir(k, goal_id) / "files" / path).read_bytes() if entry["change"] != "deleted" else b""
        label = {"modified": "수정", "added": "추가", "deleted": "삭제", "mode": "실행 권한 변경"}[entry["change"]]
        out.append(f"\n=== {label}: {_seen(path)}")
        try:
            before, after = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeDecodeError:
            out.append(f"(이진 파일: {len(old)}바이트 → {len(new)}바이트. 내용을 보여 줄 수 없습니다)")
            continue
        gone, came = sorted(_names(before) - _names(after)), sorted(_names(after) - _names(before))
        if gone:
            out.append(f"!! 없어지는 테스트 {len(gone)}개: " + _seen(", ".join(gone)))
        if came:
            out.append(f"   새 테스트 {len(came)}개: " + _seen(", ".join(came)))
        if not (_names(before) or _names(after)):
            out.append("   (테스트 이름을 읽을 수 없는 형식입니다: 데이터나 fixture일 수 있고, 없어지는 경우가 있는지는 아래 diff로만 알 수 있습니다)")
        # Every changed line, however many: what is approved is the whole file, so the whole diff is shown.
        # Lines end at a newline and nowhere else: a vertical tab or a carriage return is content, and is
        # shown as its code (splitting on it would make such a change vanish from the diff).
        changed = [_seen(line) for line in difflib.unified_diff(before.split("\n"), after.split("\n"), "고정된 판본", "제안", lineterm="", n=3)]
        out += changed or ([f"(내용의 차이는 없습니다: {entry['change']})"] if old == new else ["(바이트는 다르지만 줄 단위 차이로 보이지 않습니다)"])
    row = k.one("SELECT payload FROM event WHERE type = 'release.provisional' AND aggregate_id = ? ORDER BY seq DESC LIMIT 1", goal_id)
    seen = json.loads(row["payload"]) if row is not None else {}
    if seen.get("digest") != found["digest"]:
        out.append("\n제안된 판본으로 미리 돌려 본 결과가 없습니다(이 제안으로는 돌려 보지 못했습니다).")
    else:
        out.append("\n제안된 판본을 넣고 미리 돌려 본 결과(참고용이며 완료의 근거가 아닙니다): "
                   + ", ".join(f"{cid} {verdict}" for cid, verdict in sorted(seen["verdicts"].items())))
        out += [f"  {cid}: {_seen(text)}" for cid, text in sorted(seen["details"].items()) if seen["verdicts"].get(cid) != "PASS"]
    return "\n".join(out)


# ---------------------------------------------------------------- decision

@timing.operation("acceptance")
def approve(k: Kernel, goal_id: str, shown_digest: str, actor: str) -> dict:
    """The user approves exactly the proposal they were shown. A new acceptance revision begins:
    the previous frozen state with the approved files replaced, and nothing else."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user approves a change to frozen files")
    with k.supervisor_lock():
        goal = goals.get(k, goal_id)
        if goal["status"] in goals.GOAL_TERMINAL:
            raise LupusError("GOAL_TERMINAL", goal["status"])
        if _busy(k, goal["project_id"]):
            raise LupusError("WRITER_NOT_STOPPED", goal["project_id"])
        found = proposal(k, goal_id)
        if found is None:
            raise LupusError("RELEASE_NOTHING_PROPOSED", goal_id)
        old_revision = goal["acceptance_revision"]
        if found["revision"] != old_revision or found["digest"] != digest(goal_id, old_revision, found["entries"]):
            raise LupusError("RELEASE_STALE", "the proposal belongs to an earlier state of this goal")
        if shown_digest != found["digest"]:
            raise LupusError("RELEASE_CHANGED", "the proposal is not the one that was shown")
        rows, allowed = _rows(k, goal_id, old_revision), granted(k, goal_id)
        root = goals.project_root(k, goal_id)
        real_root = Path(os.path.realpath(root))
        contents: dict[str, bytes] = {}
        for entry in found["entries"]:
            path = entry["path"]
            frozen = rows.get(path)
            # (a file whose deletion was approved earlier is frozen as absent: for a proposal that brings
            #  it back, "there was nothing" is what it was made against)
            was = None if frozen is None or frozen["sha256"] == "absent" else frozen["sha256"]
            if not _is_released(path, allowed) or was != entry["old_sha256"]:
                raise LupusError("RELEASE_STALE", f"{path}: what is frozen is no longer what the proposal was made against")
            if _stays_frozen(path, _not_releasable(k, goal_id)):
                raise LupusError("RELEASE_REFUSED", f"{path}: the approved check and the test runner's configuration stay frozen")
            if protect._plain(real_root, path) is None:
                raise LupusError("RELEASE_REFUSED", f"{path}: a link is in the way")
            if entry["change"] != "deleted":
                kept = _dir(k, goal_id) / "files" / path
                if kept.is_symlink() or not kept.is_file() or sha256_file(kept) != entry["new_sha256"]:
                    raise LupusError("RELEASE_CHANGED", f"{path}: the held copy is not what was proposed")
                contents[path] = kept.read_bytes()
        with k.tx():
            revision = goals.revise_acceptance(k, goal_id, goals.criteria(k, goal_id), "user",
                                               "released files approved: " + ", ".join(e["path"] for e in found["entries"])[:300],
                                               old_revision)
            # The new baseline is the old one plus the approved replacements, row by row. The working
            # tree is NOT read for it: whatever else differs there is not something the user approved.
            k.run("INSERT INTO protected_file(goal_id, acceptance_revision, path, sha256, content, frozen_at, mode) "
                  "SELECT goal_id, ?, path, sha256, content, frozen_at, mode FROM protected_file "
                  "WHERE goal_id = ? AND acceptance_revision = ?", revision, goal_id, old_revision)
            k.run("INSERT INTO protected_dir(goal_id, acceptance_revision, path) SELECT goal_id, ?, path FROM protected_dir "
                  "WHERE goal_id = ? AND acceptance_revision = ?", revision, goal_id, old_revision)
            for entry in found["entries"]:
                k.run("DELETE FROM protected_file WHERE goal_id = ? AND acceptance_revision = ? AND path = ?",
                      goal_id, revision, entry["path"])
                if entry["change"] != "deleted":
                    k.run("INSERT INTO protected_file(goal_id, acceptance_revision, path, sha256, content, frozen_at, mode) "
                          "VALUES (?,?,?,?,?,?,?)", goal_id, revision, entry["path"], entry["new_sha256"],
                          contents[entry["path"]], k.now(), entry["mode"])
                else:      # the deletion is part of the frozen state too: the file may not come back unapproved
                    k.run("INSERT INTO protected_file(goal_id, acceptance_revision, path, sha256, content, frozen_at) "
                          "VALUES (?,?,?, 'absent', NULL, ?)", goal_id, revision, entry["path"], k.now())
            k.emit("user", "release.approved", "goal", goal_id, digest=found["digest"], revision_from=old_revision,
                   revision_to=revision, paths=[e["path"] for e in found["entries"]])
            for task in goals.tasks(k, goal_id):
                if task["status"] == "NEEDS_APPROVAL":
                    goals.resolve_wait(k, task["task_id"], "user", "사용자가 보호 파일 변경을 승인했다. 승인된 판본으로 다시 고정됐다: "
                                       + ", ".join(e["path"] for e in found["entries"])[:400])
        # After the commit: if this is interrupted, the frozen state already says what the files must
        # be, and the ordinary restore before the next verification writes them.
        apply(k, goal_id, root, found)
        shutil.rmtree(_dir(k, goal_id), ignore_errors=True)
        return {"approved": [e["path"] for e in found["entries"]], "acceptance_revision": revision}


def reject(k: Kernel, goal_id: str, actor: str, note: str) -> dict:
    """The user does not want these changes. The frozen versions stand; the worker is told why."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user decides about frozen files")
    with k.supervisor_lock():
        goal = goals.get(k, goal_id)
        if _busy(k, goal["project_id"]):
            raise LupusError("WRITER_NOT_STOPPED", goal["project_id"])
        found = proposal(k, goal_id)
        if found is None:
            raise LupusError("RELEASE_NOTHING_PROPOSED", goal_id)
        with k.tx():
            k.emit("user", "release.rejected", "goal", goal_id, digest=found["digest"], note=note.strip()[:500])
            for task in goals.tasks(k, goal_id):
                if task["status"] == "NEEDS_APPROVAL":
                    goals.resolve_wait(k, task["task_id"], "user", "사용자가 보호 파일 변경 제안을 승인하지 않았다: "
                                       + (note.strip()[:500] or "이유를 적지 않음") + ". 고정된 테스트가 그대로 통과해야 한다.")
        shutil.rmtree(_dir(k, goal_id), ignore_errors=True)
        return {"rejected": [e["path"] for e in found["entries"]]}


def approved_since_last_attempt(k: Kernel, task_id: str, goal_id: str) -> bool:
    """True right after an approval: the working tree now holds the approved versions and nothing
    has been tried since, so the checks are run first, before any model is called."""
    row = k.one("SELECT ts FROM event WHERE type = 'release.approved' AND aggregate_id = ? ORDER BY seq DESC LIMIT 1", goal_id)
    if row is None:
        return False
    last = k.one("SELECT MAX(started_at) FROM attempt WHERE task_id = ?", task_id)[0]
    return last is None or last <= row["ts"]


def hint(k: Kernel, goal_id: str) -> str | None:
    """What to tell the user when a goal stopped with the approved check passing and existing
    tests failing. An observation with the two possible answers, not a diagnosis."""
    if k.one("SELECT 1 FROM event WHERE type = 'check.approved' AND aggregate_id = ?", goal_id) is None:
        return None
    goal = goals.get(k, goal_id)
    if goal["status"] in goals.GOAL_TERMINAL:
        return None
    if proposal(k, goal_id) is not None:
        return f"보호 파일 변경 제안이 승인을 기다립니다. 보고 결정: `lupus release-approve {goal_id}` (거절: `lupus release-reject {goal_id} --note \"…\"`)"
    latest = goals.latest_evidence(k, goal_id)
    if not (latest.get("c0") and latest["c0"]["result"] == "PASS" and latest.get("c1") and latest["c1"]["result"] == "FAIL"):
        return None
    rows = _rows(k, goal_id, goal["acceptance_revision"])
    tried: list[str] = []
    for row in k.q("SELECT payload FROM event WHERE type = 'protect.restored' AND aggregate_id = ? ORDER BY seq DESC LIMIT 5", goal_id):
        for path in json.loads(row["payload"]).get("kept") or []:
            if path in rows and path not in tried:
                tried.append(path)
    return ("승인한 검사는 통과하지만 기존 테스트가 실패합니다: " + " ".join((latest["c1"]["detail"] or "").split())[-300:] + "\n"
            + ("worker가 고치려다 되돌려진 보호 파일: " + ", ".join(tried[:8]) + "\n" if tried else "")
            + "이 요청이 기존 테스트가 정해 둔 동작을 바꾸는 것이 맞다면, 바뀌어도 되는 테스트·fixture 파일을 지정하세요: "
            f"`lupus release {goal_id} <파일 또는 폴더>…` (worker의 수정은 diff를 승인해야 반영됩니다). "
            f"의도한 변경이 아니면 `lupus resolve`로 그렇게 알려 주세요.")
