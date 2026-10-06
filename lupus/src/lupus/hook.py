"""In-session checks for an interactive Claude Code session started by `lupus session`.

Claude Code runs this file as a hook (configured for that one process only). It gives the model
early feedback; it decides nothing. What counts is the supervisor's own verification after the
session ends, on the files as frozen.

  pre-edit   refuse an edit to a file Lupus froze (the tests and their configuration)
  stop       when the model stops after changing something and the frozen tests fail, show it the
             failure and send it back to work — a limited number of times per session

Exit status 2 with a message on stderr is how a hook tells Claude Code to block and why.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lupus import goals, protect, runs, verify   # noqa: E402
from lupus.kernel import Kernel   # noqa: E402
from lupus.util import LupusError, find_secret   # noqa: E402


def pre_edit(k: Kernel, goal_id: str, event: dict) -> tuple[int, str]:
    target = (event.get("tool_input") or {}).get("file_path") or (event.get("tool_input") or {}).get("notebook_path")
    if not target:
        return 0, ""
    root = os.path.realpath(goals.project_root(k, goal_id))
    real = os.path.realpath(target if os.path.isabs(target) else os.path.join(event.get("cwd") or root, target))
    if not real.startswith(root + os.sep):
        return 0, ""
    rel = os.path.relpath(real, root)
    revision = goals.get(k, goal_id)["acceptance_revision"]
    frozen_tree = any(rel.startswith(r["path"]) for r in k.q(
        "SELECT path FROM protected_file WHERE goal_id = ? AND acceptance_revision = ? AND sha256 LIKE 'tree:%'", goal_id, revision))
    if frozen_tree or os.path.basename(rel) in protect._names(k, goal_id) or k.one(
            "SELECT 1 FROM protected_file WHERE goal_id = ? AND acceptance_revision = ? AND path = ?", goal_id, revision, rel):
        return 2, (f"{rel} 은(는) 이 세션의 완료를 판정하는 고정된 검증 파일이라 수정할 수 없다(수정해도 세션이 끝나면 되돌려진다). "
                   "제품 코드를 고쳐라. 이 파일을 바꿔야 한다면 사용자에게 알려라.")
    return 0, ""


def stop(k: Kernel, goal_id: str, event: dict) -> tuple[int, str]:
    session = k.one("SELECT * FROM session WHERE goal_id = ?", goal_id)
    if session is None or session["stop_blocks"] >= k.policy["session_stop_blocks"]:
        return 0, ""
    root = goals.project_root(k, goal_id)
    project_id = goals.get(k, goal_id)["project_id"]
    criteria = [c for c in goals.criteria(k, goal_id) if c["verifier"]["kind"] == "command"]
    state_file = k.runtime / "sessions" / f"{goal_id}.json"
    try:
        seen = json.loads(state_file.read_text())
    except (OSError, ValueError):
        seen = {}
    current = verify.artifact_hash({"paths": ["."]}, root)
    if seen.get("hash") == current:
        return 0, ""              # nothing changed since the last look: do not interrupt a conversation
    problems = [f"- 고정된 검증 파일이 바뀌었다: {d['path']}" for d in protect.drift(k, goal_id, root)][:5]
    if not problems:
        for c in criteria:
            # Recorded like any verifier of this project, so a check left behind (and the container it
            # may drive) is found and stopped by recovery.
            verdict, _, detail = verify.run(c["verifier"], root, on_spawn=runs.aux_recorder(k, project_id, "session-check"),
                                            on_exit=lambda pid: runs.clear_aux(k, pid))
            if verdict != "PASS":
                problems.append(f"- {c['text']}: " + ("(출력 생략)" if find_secret(detail) else detail[-600:]))
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"hash": verify.artifact_hash({"paths": ["."]}, root)}))
    if not problems:
        return 0, ""
    with k.tx():
        k.run("UPDATE session SET stop_blocks = stop_blocks + 1 WHERE goal_id = ?", goal_id)
    return 2, ("Lupus가 이 세션의 완료 조건을 확인했고 아직 충족되지 않았다(검사의 실제 출력이다). 원인을 고친 뒤 멈춰라. "
               "사용자가 다른 것을 요청한 상태라면 이 사실을 한 줄로 알리고 사용자의 요청을 따르라.\n" + "\n".join(problems))


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("pre-edit", "stop"):
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    try:
        k = Kernel(Path(argv[2]))
        try:
            code, message = (pre_edit if argv[1] == "pre-edit" else stop)(k, argv[3], event)
        finally:
            k.close()
    except (LupusError, OSError, KeyError) as exc:
        # A check that cannot run never blocks the user's session; the final verification still happens.
        print(f"lupus hook skipped: {exc}", file=sys.stderr)
        return 0
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
