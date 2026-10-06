"""`lupus fix-tests`: one command, no goal file, for the one kind of request whose meaning a
program can pin down by itself — "make the currently failing tests pass".

The supervisor runs the project's tests BEFORE any worker and requires them to be red:
  * no tests found / nothing ran  -> refused (an empty run is not a goal)
  * tests already pass            -> nothing to do; this is NOT reported as work completed
  * tests fail                    -> that observed failure becomes the goal
The test files and runner configuration are frozen (see protect.py), so the only way to finish
is to change the product code until the same frozen tests pass. A worker's own "fixed it" or a
test log it pastes is never the evidence; the supervisor's own run of the frozen tests is.

Scope: Python (unittest, pytest), Node (node:test, jest, vitest), Go and Rust projects, see
runners.py; any other project by naming the test command (`--check`). A general feature request
is a different thing (`lupus do`): an existing test suite going green says nothing about whether
a new request was done.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from pathlib import Path

from . import gitx, goals, protect, review, runners, runs, verify
from .kernel import Kernel
from .util import LupusError, atomic_write, find_secret, sha256_bytes

MAX_PINNED = 20_000
# Time is reserved at its worst case (worker and check timeouts) and an interrupted attempt is
# charged in full, so the cap has to leave room for that to happen and the goal still to finish.
DEFAULT_CAPS = {"calls": 40, "attempts": 3, "active_ms": 7_200_000, "tokens": 3_000_000}
detect = runners.detect


def check_argv(root: Path, command: str) -> list[str]:
    """A test command the user typed (`--check`). A bare program name is resolved on the sanitized
    PATH to a file outside the project; a path the user wrote out is taken as written."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise LupusError("CHECK_INVALID", str(exc)) from exc
    if not argv or find_secret(command):
        raise LupusError("CHECK_INVALID", "empty command, or it contains a credential")
    if "/" not in argv[0]:
        argv[0] = runners._tool(argv[0], root)
    return argv


def suite(k: Kernel, project: dict, check: str | None = None, protect_paths: list[str] | None = None,
          files_only: bool = False, container: str | None = None, need_tests: bool = True) -> tuple[dict, dict, str, str]:
    """The project's own test run as a verifier, and what it says right now:
    (what was detected, the verifier, PASS|FAIL, detail). With `check`, the user's own command is
    the judge (exit status only) and `protect_paths` are what gets frozen. `files_only` freezes
    the files that exist now but lets new ones be added next to them (an interactive session may
    write new tests; a headless fix may not)."""
    root = Path(project["canonical_root"])
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]) or runs.aux_alive(
            k, project["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
    if check:
        try:
            known = detect(root, need_tests=False, host=not container, python_from=gitx.origin_root(k, project))
        except LupusError:
            known = {"protect": [], "inputs": []}
        found = {"runner": "custom", "argv": shlex.split(check) if container else check_argv(root, check), "env": {},
                 "inputs": known["inputs"],
                 "protect": sorted(set(protect_paths or []) | set(known["protect"]))}
    else:
        found = detect(root, need_tests=need_tests, host=not container, python_from=gitx.origin_root(k, project))
    if files_only:
        files: list[str] = []
        for rel in found["protect"]:
            files += protect._files_under(root, rel) if (root / rel).is_dir() else [rel]
        found["protect"] = sorted(set(files))
    verifier = {"kind": "command", "argv": found["argv"], "paths": ["."], "protect": found["protect"], "timeout_s": 600}
    if not check:
        verifier["require_tests"] = found["runner"]
    if found["env"]:
        verifier["env"] = found["env"]
    if found.get("frozen_trees"):
        verifier["frozen_trees"] = found["frozen_trees"]
    if container:
        # The same command, by the program's name inside the image instead of its path on this machine.
        verifier["container"] = container
    found["argv"] = verifier["argv"]
    # One run: its verdict, and (Rust) the complete list of test names from the same output.
    taken = gitx.guard(root)
    try:
        code, output = verify._run_gated(verifier, root, runs.aux_recorder(k, project["project_id"], "baseline"),
                                         lambda pid: runs.clear_aux(k, pid))
    finally:
        gitx.unguard(k, root, taken)
    verdict, _, detail = verify.judge_command(verifier, root, code, output, "")
    found["output"] = output
    if found["runner"] == "cargo":
        # Rust unit tests live in the source files a worker edits and cannot be frozen. Two rules
        # instead: a failing one is not accepted as the goal (it could be "fixed" by deleting it),
        # and every test that passes now must still be there and pass at the end.
        if code is None or code in (verify.OUTPUT_TOO_LARGE, verify.GATE_EXEC_FAILED):
            # Without the complete list there is nothing to pin and nothing to check for failures in source.
            raise LupusError("TESTS_NOT_FREEZABLE", "the test run could not be read in full (timed out, would not start, or printed too much)")
        passing, failing_in_source = runners.cargo_tests(output)
        if failing_in_source:
            raise LupusError("TESTS_NOT_FREEZABLE", "failing tests are inside source files, which cannot be frozen: "
                             + ", ".join(failing_in_source[:5]) + ". Move them to tests/ or describe the goal in a goal file")
        if len(passing) > MAX_PINNED:
            raise LupusError("TESTS_NOT_FREEZABLE", f"{len(passing)} tests to pin by name; the limit is {MAX_PINNED}")
        verifier["must_pass"] = sorted(passing)
    return found, verifier, verdict, detail


def fix_tests(k: Kernel, project: dict, actor: str, caps: dict | None = None, check: str | None = None,
              protect_paths: list[str] | None = None, container: str | None = None) -> dict:
    """Create the goal from an observed test failure. Returns {"goal_id": …} or a refusal reason
    under "nothing_to_do". Runs the tests once (in the project, under the same rules as any
    verifier)."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "goals are submitted by the user")
    found, verifier, verdict, detail = suite(k, project, check, protect_paths, container=container)
    if verdict == "PASS":
        return {"nothing_to_do": "tests already pass", "runner": found["runner"]}
    if not check and runners.nothing_ran(found["runner"], detail):
        raise LupusError("NO_TESTS_FOUND", "the test runner found nothing to run")
    if detail.startswith("exit 127") or "could not be started" in detail:
        raise LupusError("TEST_RUNNER_UNAVAILABLE", detail[:200])
    shown = "(출력 생략: 자격증명 패턴이 포함됨)" if find_secret(detail) else detail
    with k.tx():
        goal = goals.submit(k, project["project_id"], "현재 실패하는 테스트를 통과시키기", [
            {"id": "c0", "text": f"고정된 테스트가 모두 통과한다 ({found['runner']})", "verifier": verifier}],
            caps or DEFAULT_CAPS, actor="user")
        goals.add_task(
            k, goal["goal_id"], "실패하는 테스트 복구",
            "아래 테스트 실패의 원인을 제품 코드에서 찾아 고쳐라. 테스트 파일과 테스트 설정은 수정하지 마라"
            "(수정해도 검증 전에 되돌려진다).\n실행 전 관측한 실패:\n" + shown, ["c0"], inputs=found["inputs"])
    return {"goal_id": goal["goal_id"], "runner": found["runner"], "protect": found["protect"]}


# ---------------------------------------------------------------- any request, red-first

MAX_REQUEST = 2000


def _baseline(k: Kernel, project: dict, verifier: dict) -> tuple[str, str]:
    root = Path(project["canonical_root"])
    taken = gitx.guard(root)
    try:
        verdict, _, detail = verify.run(
            verifier, root, on_spawn=runs.aux_recorder(k, project["project_id"], "baseline"),
            on_exit=lambda pid: runs.clear_aux(k, pid))
    finally:
        gitx.unguard(k, root, taken)
    return verdict, detail


MAX_STAGED_FILES = 60
MAX_STAGED_BYTES = 2 * 1024 * 1024


MAX_ALLOWED_FAILURES = 300


def draft_check(k: Kernel, project: dict, request: str, actor: str, caps: dict | None = None, stage: bool = True,
                allow_failing: bool = False) -> dict:
    """Step 1 of `lupus do`: a goal whose only job is to WRITE the acceptance test for a request.

    Nothing else in the project may change during this step (everything but the one new test
    file is frozen), and the step is complete only when the supervisor has run the new test
    itself and seen it fail on the current code. The existing tests must be green first, so a
    later regression can be told apart from a failure that was already there.

    With `stage`, the same call also implements the request, editing the project's sources in the
    ordinary way. The supervisor takes those edits out again before it judges the test (every file
    but the new test is frozen, and the displaced versions are kept in the runtime, outside the
    project). The test is therefore still judged, shown and approved against the code as it is.
    Only after the user approved the test are the kept versions put back and verified like any
    worker's result; if they do not pass, an ordinary implementation attempt follows. This saves the
    second model call when the first proposal is right."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "requests are submitted by the user")
    request = " ".join(request.split())
    if not request or len(request) > MAX_REQUEST or find_secret(request):
        raise LupusError("REQUEST_INVALID", f"1..{MAX_REQUEST} characters, no credentials")
    root = Path(project["canonical_root"])
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]) or runs.aux_alive(
            k, project["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
    found = detect(root, need_tests=False, python_from=gitx.origin_root(k, project))
    common = {"require_tests": found["runner"], **({"env": found["env"]} if found["env"] else {}),
              **({"frozen_trees": found["frozen_trees"]} if found["frozen_trees"] else {})}
    pinned: list[str] = []
    already_failing: list[str] = []
    if found["has_tests"] or found["runner"] == "cargo":      # Rust: always, tests may hide in source in forms a scan misses
        _, whole, verdict, detail = suite(k, project, need_tests=False)
        pinned = whole.get("must_pass", [])      # taken now, while everything builds and passes
        if verdict != "PASS" and (found["has_tests"] or pinned or not runners.nothing_ran(found["runner"], detail)):
            # A red suite makes "the old tests fail afterwards" say nothing about this request —
            # unless the user accepts the tests that fail NOW, by name, as the starting point.
            if allow_failing and runners.failed_ids(found["runner"], "") is not None:
                again = {**whole, "argv": runners.with_failure_names(found["runner"], whole["argv"])}
                taken = gitx.guard(root)
                try:
                    _, output = verify._run_gated(again, root, runs.aux_recorder(k, project["project_id"], "baseline"),
                                                  lambda pid: runs.clear_aux(k, pid))
                finally:
                    gitx.unguard(k, root, taken)
                already_failing = sorted(runners.failed_ids(found["runner"], output) or [])
            if not already_failing or len(already_failing) > MAX_ALLOWED_FAILURES:
                raise LupusError("BASELINE_RED", "existing tests do not pass; run `lupus fix-tests` first, or accept the tests "
                                 "failing now as the starting point with --allow-failing: " + detail[-200:])
    test_path = runners.new_test_path(root, found, sha256_bytes(request.encode("utf-8"))[:8])
    if (root / test_path).exists():
        raise LupusError("CHECK_EXISTS", test_path)
    sources = runners.sources(root, found["language"], about=request)
    verifier = {"kind": "red_test", "argv": runners.one_file_argv(found, test_path), "path": test_path,
                "paths": [test_path], "protect_except": [test_path], "timeout_s": 300, **common,
                **({"proposal": True} if stage else {}),
                **({"baseline_pass": pinned} if pinned else {}),
                **({"baseline_failing": already_failing} if already_failing else {})}
    with k.tx():
        goal = goals.submit(k, project["project_id"], f"요청의 검사 작성: {request[:80]}", [
            {"id": "c0", "text": (f"{test_path} 가 존재하고, 요청을 구현하기 전의 코드에서는 실패한다" if stage else
                                  f"{test_path} 가 존재하고, 현재 코드에서는 실패한다(요청이 아직 구현되지 않았으므로)"),
             "verifier": verifier}], caps or DEFAULT_CAPS, actor="user")
        goals.add_task(
            k, goal["goal_id"], "검사 작성과 구현" if stage else "검사 작성",
            ("아래 요청에 대해 두 가지를 하라: (1) 요청이 구현됐는지 판정하는 테스트를 새로 쓰고, (2) 요청을 구현한다.\n" if stage else
             "아래 요청이 구현됐는지 판정하는 테스트만 작성하라. 구현은 하지 마라.\n")
            + f"요청: {request}\n"
            f"- 새 파일 {test_path} 를 만든다({runners.FORMAT[found['runner']]} 형식). "
            + ("기존 테스트 파일과 테스트 설정은 만들거나 수정하지 마라" if stage else "프로젝트의 기존 파일은 만들거나 수정하지 마라")
            + "(수정해도 되돌려진다).\n"
            "- 요청의 요구 동작을 구체적인 입력과 기대 결과로 검사하라. 경계 사례를 포함하라.\n"
            + ("- 이 테스트는 원래 코드(네가 구현하기 전)에서는 실패하고 네 구현 뒤에는 통과해야 한다. 원래 코드에서 실패하는지는 "
               "Lupus가 원본을 따로 보관해 두었다가 직접 확인하므로, 네가 코드를 되돌리거나 구현을 미룰 필요가 없다. 원래 코드를 "
               "기준으로: " if stage else "- 요청이 아직 구현되지 않았으므로 이 테스트는 지금 실패해야 한다. ")
            + runners.DRAFT_HINT[found["language"]] + "\n"
            + ("- 그리고 요청을 지금 프로젝트의 소스 파일을 직접 수정해 구현하라(필요한 부분만 고쳐라). 패치 파일, 사본, 설명 "
               "문서로 내지 마라: 소스 파일 자체가 바뀌어 있어야 하고, 그 상태에서 이 테스트와 기존 테스트가 모두 통과해야 한다.\n"
               if stage else "")
            + "프로젝트의 소스 파일: " + (", ".join(sources) or "(없음)"), ["c0"],
            inputs=sources[:8])      # small ones are handed over in the prompt, so they need not be read one by one
    return {"goal_id": goal["goal_id"], "test_path": test_path, "runner": found["runner"], "request": request,
            "already_failing": already_failing}


def approve_check(k: Kernel, project: dict, draft_goal_id: str, request: str, test_sha256: str, actor: str,
                  caps: dict | None = None, keep_base: bool = False) -> dict:
    """Step 2: the user has read the drafted test and approves THIS exact version. Creates the
    implementation goal: the approved test and every existing test are frozen, and the goal is
    done only when the supervisor sees the approved test and the existing tests pass."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user approves a check")
    draft = goals.get(k, draft_goal_id)
    if draft["project_id"] != project["project_id"] or draft["status"] != "DONE":
        raise LupusError("CHECK_NOT_READY", draft["status"])
    red = goals.criteria(k, draft_goal_id)[0]["verifier"]
    if red.get("kind") != "red_test":
        raise LupusError("CHECK_NOT_READY", "not a drafted check")
    if _approved(k, draft_goal_id):
        raise LupusError("CHECK_ALREADY_APPROVED", draft_goal_id)       # one approval, one implementation goal
    root = Path(project["canonical_root"])
    test_path = red["path"]
    target = root / test_path
    if not target.is_file() or target.is_symlink() or sha256_bytes(target.read_bytes()) != test_sha256:
        # What gets frozen must be what the user actually read.
        raise LupusError("CHECK_CHANGED", test_path)
    found = detect(root, python_from=gitx.origin_root(k, project))
    if found["language"] == "python" and red["argv"][0] != found["argv"][0]:
        # Decided when the request was made, before any worker touched the project. An environment
        # that showed up since is not the project's.
        found["argv"] = [red["argv"][0], *found["argv"][1:]]
        found["frozen_trees"] = red.get("frozen_trees", [])
    # Run the approved version once more: it must still be red on the code as it is now, and the
    # number of tests it runs becomes the minimum that must run (and pass) later.
    verdict, detail = _baseline(k, project, red)
    if verdict != "PASS":
        raise LupusError("CHECK_NOT_RED", detail[:200])
    try:
        count = runners.count_tests(found["runner"], target.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise LupusError("CHECK_NOT_RED", f"the approved file does not parse: {exc}") from exc
    protect_paths = sorted(set(found["protect"]) | {test_path})
    common = {"protect": protect_paths, "forbid_new": list(runners.CONFIG[found["language"]]),
              "require_tests": found["runner"], "paths": ["."], **({"env": found["env"]} if found["env"] else {}),
              **({"frozen_trees": found["frozen_trees"]} if found["frozen_trees"] else {})}
    criteria = [{"id": "c0", "text": f"승인된 검사 {test_path} 의 테스트 {count}개가 모두 실행되어 통과한다",
                 "verifier": {"kind": "command", "argv": red["argv"], "timeout_s": 300, **common,
                              # a compiled language reports nothing per test when the run is filtered by name
                              **({"min_tests": count} if runners.passed(found["runner"], "") is not None else {})}},
                {"id": "c1", "text": ("기존 테스트를 포함한 전체 테스트가 통과한다" if not red.get("baseline_failing") else
                                      f"전체 테스트에서 요청 전부터 실패하던 {len(red['baseline_failing'])}개 외에는 실패가 없다"),
                 "verifier": {"kind": "command", "timeout_s": 600, **common,
                              "argv": (runners.with_failure_names(found["runner"], found["argv"]) if red.get("baseline_failing")
                                       else found["argv"]),
                              **({"allowed_failures": red["baseline_failing"]} if red.get("baseline_failing") else {}),
                              # names recorded from the green run BEFORE the new test existed
                              **({"must_pass": red["baseline_pass"]} if red.get("baseline_pass") else {})}}]
    related = [m for m in found["inputs"] if m != test_path]
    base = base_error = None
    if keep_base:
        # What the project looked like before the implementation, for a reviewer to compare against.
        try:
            base = review.snapshot(k, draft_goal_id, root)
        except LupusError as exc:
            base_error = f"{exc.code}: {exc.detail}"
    with k.tx():
        goal = goals.submit(k, project["project_id"], request[:200], criteria, caps or DEFAULT_CAPS, actor="user")
        task = goals.add_task(
            k, goal["goal_id"], "요청 구현",
            f"다음 요청을 구현하라: {request}\n승인된 검사 {test_path} 와 기존 테스트가 모두 통과해야 한다. "
            "테스트 파일과 테스트 설정은 수정하지 마라(수정해도 검증 전에 되돌려진다).",
            ["c0", "c1"], inputs=[test_path] + related[:8])
        # Freeze NOW, in the same transaction as the approval, and make sure what was frozen is what
        # the user read: no window in which the file can be swapped between approval and first run.
        protect.freeze(k, goal["goal_id"], root)
        frozen = k.one("SELECT sha256 FROM protected_file WHERE goal_id = ? AND path = ?", goal["goal_id"],
                       os.path.normpath(test_path))
        if frozen is None or frozen["sha256"] != test_sha256:
            raise LupusError("CHECK_CHANGED", test_path)
        k.emit("user", "check.approved", "goal", goal["goal_id"], draft_goal=draft_goal_id, test=test_path,
               sha256=test_sha256)
    # The test is approved and frozen. Only now does the implementation proposed alongside it enter
    # the project; whether it is any good is decided by the frozen tests, before any further call.
    # …and only if THIS draft was asked for one.
    applied = _apply_proposal(k, draft_goal_id, root, set(protect_paths), found["language"]) if red.get("proposal") else []
    if applied:
        with k.tx():
            k.emit("supervisor", "do.staged_applied", "goal", goal["goal_id"], files=applied)
            k.emit("supervisor", "batch.planned", "task", task["task_id"], lead_attempt=None,
                   acceptance_revision=goal["acceptance_revision"])       # verified first, called only if that fails
    return {"goal_id": goal["goal_id"], "test_path": test_path, "staged_applied": applied,
            **({"review_base": str(base)} if base else {}), **({"review_unavailable": base_error} if base_error else {})}


def proposal(k: Kernel, draft_goal_id: str) -> tuple[Path, list[str]] | None:
    """Where the implementation written alongside a drafted check is kept, and which files it has:
    the versions the supervisor displaced when it put the project back after the draft's LAST
    successful call. An earlier, rejected call's edits are not part of it."""
    last = k.one("SELECT run_id FROM attempt WHERE goal_id = ? AND outcome = 'PROGRESS' ORDER BY rowid DESC LIMIT 1",
                 draft_goal_id)
    if last is None:
        return None
    kept_in = k.runtime / "displaced" / last["run_id"]
    row = k.one("SELECT payload FROM event WHERE type = 'protect.restored' AND aggregate_id = ? "
                "AND json_extract(payload, '$.displaced_kept_in') = ? ORDER BY seq LIMIT 1", draft_goal_id, str(kept_in))
    if row is None:
        return None
    return kept_in, sorted(json.loads(row["payload"]).get("kept", []))


def _apply_proposal(k: Kernel, draft_goal_id: str, root: Path, frozen: set[str], language: str) -> list[str]:
    """Put the kept versions into the project. Never a test, a runner configuration, a frozen
    path, a link, or anything that would land outside the project."""
    found, applied = proposal(k, draft_goal_id), []
    if found is None:
        return applied
    kept_in, rels = found
    # The kept versions were written against the project as the draft left it. A file edited since
    # (by the user, while reading the test) is not overwritten: the whole proposal is dropped and
    # an ordinary implementation attempt follows.
    edited = sorted({d["path"] for d in protect.drift(k, draft_goal_id, root)} & {os.path.normpath(r) for r in rels})
    if edited:
        with k.tx():
            k.emit("supervisor", "do.proposal_dropped", "goal", draft_goal_id, edited_since_draft=edited[:20])
        return applied
    for rel in rels:
        rel = os.path.normpath(rel)
        parts = Path(rel).parts
        source = kept_in.joinpath(*parts)
        if os.path.isabs(rel) or ".." in parts or len(applied) >= MAX_STAGED_FILES:
            continue
        try:
            info = os.lstat(source)
        except OSError:
            continue
        on_the_way = [root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STAGED_BYTES      # no links, pipes, devices
                or any(p.is_symlink() for p in on_the_way)                         # nor through a link, wherever it leads
                or parts[0] in protect.SKIP_DIRS
                or rel in frozen or any(rel.startswith(f + os.sep) for f in frozen)
                or runners.is_test_file(language, rel) or parts[-1] in runners.CONFIG[language]):
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, source.read_bytes(), mode=(target.stat().st_mode & 0o777) if target.is_file() else 0o644)
        applied.append(rel)
    return applied


def _approved(k: Kernel, draft_goal_id: str) -> bool:
    return k.one("SELECT 1 FROM event WHERE type = 'check.approved' AND json_extract(payload, '$.draft_goal') = ?",
                 draft_goal_id) is not None


def discard_draft(k: Kernel, project: dict, draft_goal_id: str, actor: str) -> str | None:
    """Drop a drafted check that was not approved (or never became a valid red test), so it does
    not stay behind as a failing test. The file is moved into the runtime, not deleted; an
    unfinished draft goal is cancelled. Returns where the file was kept."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "discard")
    draft = goals.get(k, draft_goal_id)
    red = goals.criteria(k, draft_goal_id)[0]["verifier"]
    if draft["project_id"] != project["project_id"] or red.get("kind") != "red_test":
        raise LupusError("CHECK_NOT_READY", "not a drafted check")
    if _approved(k, draft_goal_id):
        raise LupusError("CHECK_IN_USE", "this check was approved and is frozen by an implementation goal")
    root = Path(project["canonical_root"])
    if draft["status"] not in goals.GOAL_TERMINAL:
        # Undo whatever the draft step left changed BEFORE its frozen copies are released.
        protect.restore(k, draft_goal_id, root, keep_dir=k.runtime / "displaced" / draft_goal_id / "restored")
        goals.cancel(k, draft_goal_id, "user", "draft discarded")
    target = root / red["path"]
    if not target.is_file() or target.is_symlink():
        return None
    kept = k.runtime / "displaced" / draft_goal_id / red["path"]
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    kept.write_bytes(target.read_bytes())
    target.unlink()
    return str(kept)

