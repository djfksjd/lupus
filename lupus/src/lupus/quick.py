"""`lupus fix-tests`: one command, no goal file, for the one kind of request whose meaning a
program can pin down by itself — "make the currently failing tests pass".

The supervisor runs the project's tests BEFORE any worker and requires them to be red:
  * no tests found / nothing ran  -> refused (an empty run is not a goal)
  * tests already pass            -> nothing to do; this is NOT reported as work completed
  * tests fail                    -> that observed failure becomes the goal
The test files and runner configuration are frozen (see protect.py), so the only way to finish
is to change the product code until the same frozen tests pass. A worker's own "fixed it" or a
test log it pastes is never the evidence; the supervisor's own run of the frozen tests is.

Scope: Python projects using unittest or pytest. General feature requests are out of scope on
purpose: an existing test suite going green says nothing about whether a new request was done.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import goals, protect, runs, verify
from .kernel import Kernel
from .util import LupusError, find_secret, scrubbed_env, sha256_bytes

# Everything that can change which tests run. pyproject.toml is included because pytest reads
# its options from there (e.g. an added `addopts = "-k …"` would deselect the failing test).
CONFIG_FILES = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
DEFAULT_CAPS = {"calls": 40, "attempts": 3, "active_ms": 1_800_000, "tokens": 3_000_000}
# Files that change which tests are collected or how they run. If one does not exist when a check
# is approved, it must not be created during implementation either.
FORBID_NEW = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "sitecustomize.py",
              "usercustomize.py")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def detect(root: Path, need_tests: bool = True) -> dict:
    """Pick the test command and what to freeze, from files only (no model, no guessing)."""
    root = Path(root)
    test_dirs = [d for d in ("tests", "test") if (root / d).is_dir()]
    loose = sorted(p.name for p in root.glob("test_*.py")) + sorted(p.name for p in root.glob("*_test.py"))
    if need_tests and not test_dirs and not loose:
        raise LupusError("NO_TESTS_FOUND", "no tests/ directory and no test_*.py in the project root")
    uses_pytest = any((root / f).is_file() for f in ("pytest.ini", "conftest.py")) or (
        (root / "pyproject.toml").is_file() and "[tool.pytest" in (root / "pyproject.toml").read_text(errors="ignore"))
    if uses_pytest:
        # Not run inside the project and with the scrubbed environment: importing pytest there
        # could execute the project's conftest/plugins with the user's credentials.
        probe = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True,
                               cwd=tempfile.gettempdir(), env=scrubbed_env(), stdin=subprocess.DEVNULL)
        if probe.returncode != 0:
            raise LupusError("TEST_RUNNER_UNAVAILABLE", "the project is configured for pytest but pytest is not installed")
        argv, runner = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "pytest"
    else:
        argv, runner = [sys.executable, "-m", "unittest", "discover", "-q"], "unittest"
    protect_paths = test_dirs + loose + [f for f in CONFIG_FILES if (root / f).is_file()]
    # Hand the worker the tests and the local modules they import, so it need not go looking.
    tests = loose + [str(p.relative_to(root)) for d in test_dirs for p in sorted((root / d).rglob("test*.py"))]
    related: list[str] = []
    for rel in tests[:8]:
        for module in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", (root / rel).read_text(errors="ignore"), re.M):
            if (root / f"{module}.py").is_file() and f"{module}.py" not in related:
                related.append(f"{module}.py")
    return {"runner": runner, "argv": argv, "protect": protect_paths, "inputs": tests[:8] + related[:8],
            "has_tests": bool(test_dirs or loose), "test_dir": test_dirs[0] if test_dirs else ""}


def fix_tests(k: Kernel, project: dict, actor: str, caps: dict | None = None) -> dict:
    """Create the goal from an observed test failure. Returns {"goal_id": …} or a refusal reason
    under "nothing_to_do". Runs the tests once (in the project, under the same rules as any
    verifier)."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "goals are submitted by the user")
    root = Path(project["canonical_root"])
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]) or runs.aux_alive(
            k, project["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
    found = detect(root)
    verifier = {"kind": "command", "argv": found["argv"], "paths": ["."], "protect": found["protect"],
                "require_tests": found["runner"], "timeout_s": 600}
    verdict, _, detail = verify.run(
        verifier, root, on_spawn=lambda pid: runs.register_aux(k, project["project_id"], pid, "baseline"),
        on_exit=lambda pid: runs.clear_aux(k, pid))
    if verdict == "PASS":
        return {"nothing_to_do": "tests already pass", "runner": found["runner"]}
    nothing_ran = (detail == "no tests ran" or "NO TESTS RAN" in detail or "Ran 0 tests" in detail
                   or (found["runner"] == "pytest" and detail.startswith("exit 5")))
    if nothing_ran:
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


def _one_file_argv(runner: str, test_path: str) -> list[str]:
    if runner == "pytest":
        return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_path]
    return [sys.executable, "-m", "unittest", "-q", test_path]


def _baseline(k: Kernel, project: dict, verifier: dict) -> tuple[str, str]:
    verdict, _, detail = verify.run(
        verifier, Path(project["canonical_root"]),
        on_spawn=lambda pid: runs.register_aux(k, project["project_id"], pid, "baseline"),
        on_exit=lambda pid: runs.clear_aux(k, pid))
    return verdict, detail


def draft_check(k: Kernel, project: dict, request: str, actor: str, caps: dict | None = None) -> dict:
    """Step 1 of `lupus do`: a goal whose only job is to WRITE the acceptance test for a request.

    Nothing else in the project may change during this step (everything but the one new test
    file is frozen), and the step is complete only when the supervisor has run the new test
    itself and seen it fail on the current code. The existing tests must be green first, so a
    later regression can be told apart from a failure that was already there."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "requests are submitted by the user")
    request = " ".join(request.split())
    if not request or len(request) > MAX_REQUEST or find_secret(request):
        raise LupusError("REQUEST_INVALID", f"1..{MAX_REQUEST} characters, no credentials")
    root = Path(project["canonical_root"])
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]) or runs.aux_alive(
            k, project["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
    found = detect(root, need_tests=False)
    if found["has_tests"]:
        verdict, detail = _baseline(k, project, {"kind": "command", "argv": found["argv"], "paths": ["."],
                                                 "require_tests": found["runner"], "timeout_s": 600})
        if verdict != "PASS":
            # Otherwise "the old tests fail afterwards" would say nothing about this request.
            raise LupusError("BASELINE_RED", "existing tests do not pass; run `lupus fix-tests` first: " + detail[-200:])
    name = f"test_lupus_{sha256_bytes(request.encode('utf-8'))[:8]}.py"
    test_path = f"{found['test_dir']}/{name}" if found["test_dir"] else name
    if (root / test_path).exists():
        raise LupusError("CHECK_EXISTS", test_path)
    sources = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                     if not any(part in SKIP for part in p.parts) and "test" not in p.name)[:40]
    verifier = {"kind": "red_test", "argv": _one_file_argv(found["runner"], test_path), "path": test_path,
                "paths": [test_path], "protect_except": [test_path], "require_tests": found["runner"], "timeout_s": 300}
    with k.tx():
        goal = goals.submit(k, project["project_id"], f"요청의 검사 작성: {request[:80]}", [
            {"id": "c0", "text": f"{test_path} 가 존재하고, 현재 코드에서는 실패한다(요청이 아직 구현되지 않았으므로)",
             "verifier": verifier}], caps or DEFAULT_CAPS, actor="user")
        goals.add_task(
            k, goal["goal_id"], "검사 작성",
            "아래 요청이 구현됐는지 판정하는 테스트만 작성하라. 구현은 하지 마라.\n"
            f"요청: {request}\n"
            f"- 새 파일 {test_path} 하나만 만든다({found['runner']} 형식). 다른 파일은 만들거나 수정하지 마라"
            "(수정해도 되돌려진다).\n"
            "- 요청의 요구 동작을 구체적인 입력과 기대 결과로 검사하라. 경계 사례를 포함하라.\n"
            "- 요청이 아직 구현되지 않았으므로 이 테스트는 지금 실패해야 한다. 단, 파일 자체는 지금도 import 되어야 한다: "
            "아직 없는 함수·클래스는 파일 맨 위가 아니라 각 테스트 함수 안에서 import 하거나 모듈을 import 한 뒤 속성으로 호출하라.\n"
            "프로젝트의 파이썬 파일: " + (", ".join(sources) or "(없음)"), ["c0"])
    return {"goal_id": goal["goal_id"], "test_path": test_path, "runner": found["runner"], "request": request}


def approve_check(k: Kernel, project: dict, draft_goal_id: str, request: str, test_sha256: str, actor: str,
                  caps: dict | None = None) -> dict:
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
    found = detect(root)
    # Run the approved version once more: it must still be red on the code as it is now, and the
    # number of tests it runs becomes the minimum that must run (and pass) later.
    verdict, detail = _baseline(k, project, red)
    if verdict != "PASS":
        raise LupusError("CHECK_NOT_RED", detail[:200])
    # Counted from the source (a failing import hides the individual tests from the runner's summary).
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise LupusError("CHECK_NOT_RED", f"the approved file does not parse: {exc}") from exc
    count = max(1, sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
                       for n in ast.walk(tree)))
    protect_paths = sorted(set(found["protect"]) | {test_path})
    common = {"protect": protect_paths, "forbid_new": list(FORBID_NEW), "require_tests": found["runner"], "paths": ["."]}
    criteria = [{"id": "c0", "text": f"승인된 검사 {test_path} 의 테스트 {count}개가 모두 실행되어 통과한다",
                 "verifier": {"kind": "command", "argv": red["argv"], "timeout_s": 300, "min_tests": count, **common}},
                {"id": "c1", "text": "기존 테스트를 포함한 전체 테스트가 통과한다",
                 "verifier": {"kind": "command", "argv": found["argv"], "timeout_s": 600, **common}}]
    related = [m for m in found["inputs"] if m != test_path]
    with k.tx():
        goal = goals.submit(k, project["project_id"], request[:200], criteria, caps or DEFAULT_CAPS, actor="user")
        goals.add_task(
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
    return {"goal_id": goal["goal_id"], "test_path": test_path}


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

