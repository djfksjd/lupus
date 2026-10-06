"""`lupus session`: the user's ordinary interactive `claude` / `codex`, with Lupus around it.

The user talks to the CLI exactly as usual, with their own configuration. What Lupus adds:

  * the project's single writer slot is held for the session, like for any worker
  * the tests that exist now (and the files that configure them) are frozen: an edit to them is
    refused while it happens (Claude Code) and undone when the session ends (both CLIs)
  * when the session ends, the supervisor itself runs the frozen tests; only that run can mark
    the session's goal done. What the model said in the conversation is not evidence
  * the whole thing is one budgeted, recorded attempt that can be recovered after a crash

Lupus does not see or store the conversation, and the host reports no usage for an interactive
session, so the session is charged at its full reservation (calls) and its real duration.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from . import goals, quick, runners
from .adapters import InteractiveAdapter
from .kernel import Kernel
from .util import LupusError

TITLE = "대화형 세션"
HOOK = str(Path(__file__).resolve().with_name("hook.py"))
DEFAULT_CAPS = {"calls": 60, "attempts": 2, "active_ms": 6 * 3_600_000}


def start(k: Kernel, project: dict, actor: str, check: str | None = None, protect_paths: list[str] | None = None,
          caps: dict | None = None, container: str | None = None) -> dict:
    """Create the session's goal: "the project's tests pass, as frozen now". Sessions left
    unfinished in this project are closed first; each session is its own goal and budget."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "sessions are started by the user")
    found, verifier, verdict, detail = quick.suite(k, project, check, protect_paths, files_only=True, container=container)
    if not check and verdict != "PASS" and runners.nothing_ran(found["runner"], detail):
        raise LupusError("NO_TESTS_FOUND", "the test runner found nothing to run; name a command with --check")
    if not check:
        verifier["forbid_new"] = list(runners.CONFIG[found["language"]])
        # New test files may be added in a session, but not new collection hooks anywhere in the tree
        # (a nested conftest.py could deselect every frozen test).
        verifier["forbid_new_names"] = [n for n in runners.CONFIG[found["language"]] if n not in ("package.json", "tsconfig.json")]
    for old in k.q("SELECT g.goal_id FROM goal g JOIN task t ON t.goal_id = g.goal_id WHERE g.project_id = ? "
                   "AND t.title = ? AND g.status NOT IN ('DONE','CANCELLED')", project["project_id"], TITLE):
        goals.cancel(k, old["goal_id"], "user", "a new session was started")
    with k.tx():
        goal = goals.submit(k, project["project_id"], "대화형 세션: 고정된 테스트가 통과한다", [
            {"id": "c0", "text": f"세션 시작 시점의 테스트가 고정된 그대로 모두 통과한다 ({found['runner']})",
             "verifier": verifier}], caps or DEFAULT_CAPS, actor="user")
        goals.add_task(k, goal["goal_id"], TITLE, "사용자가 CLI 화면에서 직접 지시한다.", ["c0"])
    return {"goal_id": goal["goal_id"], "runner": found["runner"], "frozen": len(found["protect"]),
            "baseline": verdict, "baseline_detail": detail[-300:] if verdict != "PASS" else ""}


def adapter(k: Kernel, goal_id: str, cli: str, extra: list[str] | None = None, stop_check: bool = True) -> InteractiveAdapter:
    criteria = goals.criteria(k, goal_id)
    notice = (
        "이 세션은 Lupus가 감독한다. 완료 조건: " + "; ".join(c["text"] for c in criteria) + ". "
        "지금 있는 테스트 파일과 테스트 설정은 고정되어 있어 수정할 수 없다(새 테스트 파일을 추가하는 것은 된다). "
        "세션이 끝나면 Lupus가 고정된 테스트를 직접 실행해 완료 여부를 판정한다. 테스트가 통과했다는 보고만으로는 완료로 인정되지 않는다."
    )
    settings = None
    if cli == "claude":
        base = " ".join(shlex.quote(part) for part in (sys.executable, HOOK))
        tail = f" {shlex.quote(str(k.home))} {shlex.quote(goal_id)}"
        hooks = {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                                 "hooks": [{"type": "command", "command": base + " pre-edit" + tail, "timeout": 20}]}]}
        if stop_check:
            hooks["Stop"] = [{"hooks": [{"type": "command", "command": base + " stop" + tail, "timeout": 900}]}]
        settings = {"hooks": hooks}
    with k.tx():
        k.run("INSERT OR REPLACE INTO session(goal_id, cli, created_at) VALUES (?,?,?)", goal_id, cli, k.now())
    return InteractiveAdapter(cli, extra, notice, settings)
