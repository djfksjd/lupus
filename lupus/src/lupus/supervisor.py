"""Foreground supervisor loop (§6). One process owns the database, the assignment and the
worker's lifecycle while it runs. It is ordinary code: no model call decides routing, budgets,
retries or completion (§1.1 rule 4, §11.2.1).

Per task:  claim -> attempt + reservations -> checkpoint -> worker -> usage -> verify ->
           close attempt -> checkpoint -> release writer
A worker's own "done" is ignored; only the deterministic verifier produces evidence.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from . import adapters, budget, gitx, goals, handoff, judging, memory, projects, protect, recovery, runs, service, usage, verify
from .adapters import Adapter
from .kernel import Kernel
from .util import LupusError, container_stop, crash_point, find_secret_bytes, proc_start, sha256_json

MAX_SNAPSHOT_BYTES = 256 * 1024


def _verify_reserve(criteria: list[dict]) -> dict[str, int]:
    """Safety reservation for one attempt: the worst-case time of its verifiers, one call per
    judge among them, plus one call held back for a safe shutdown/rollback step."""
    return {"calls": 1 + sum(c["verifier"]["kind"] == "judge" for c in criteria),
            "active_ms": math.ceil(sum(verify.timeout_s(c["verifier"]) for c in criteria) * 1000)}


def _timed_verify(k: Kernel, goal: dict, criteria: list[dict], root: Path) -> tuple[list[tuple], int, int]:
    """Run verifiers; returns (results, elapsed ms, model calls made by judges). Command verifiers
    are registered as process groups of the project before they start and cleared only after
    their group is confirmed empty."""
    started = time.monotonic()
    checked, calls = [], 0
    taken = gitx.guard(root)        # checks run the project's code: whatever it leaves in .git must not survive
    try:
        return _run_checks(k, goal, criteria, root, checked, calls, started)
    finally:
        gitx.unguard(k, root, taken, goal["goal_id"])


def _run_checks(k: Kernel, goal: dict, criteria: list[dict], root: Path, checked: list, calls: int, started: float):
    for c in criteria:
        if c["verifier"]["kind"] == "judge" and any(v != "PASS" for _, v, _, _ in checked):
            # A judge costs a model call; it is not asked about a document that already failed a
            # mechanical check in the same pass.
            verdict, digest, detail = "FAIL", verify.artifact_hash(c["verifier"], root), "not judged: an earlier check failed"
        elif c["verifier"]["kind"] in ("judge", "user_approval"):
            verdict, digest, detail, n = judging.run(k, goal, c, root)
            calls += n
        else:
            verdict, digest, detail = verify.run(
                c["verifier"], root, on_spawn=runs.aux_recorder(k, goal["project_id"], "verifier"),
                on_exit=lambda pid: runs.clear_aux(k, pid))
        checked.append((c, verdict, digest, detail))
    return checked, int((time.monotonic() - started) * 1000), calls

ENV_ERRORS = ("quota", "rate_limit", "auth", "unavailable")


# ---------------------------------------------------------------- startup recovery

def recover(k: Kernel, stop_stale_writer: bool = False) -> dict[str, Any]:
    with k.supervisor_lock():
        return _recover(k, stop_stale_writer)


def _recover(k: Kernel, stop_stale_writer: bool) -> dict[str, Any]:
    """Run before anything else after a restart. Old leases are never revived: every run that
    was not cleanly stopped is closed with evidence, or reported as a blocker.

    A worker left over from a dead supervisor is stopped only when the caller asks for it; until
    then the project's writer slot stays taken and nothing new starts there.
    """
    closed, alive = [], []
    for aux in runs.aux_alive(k):
        # A verifier left behind by a dead supervisor. Only signalled when it is provably ours.
        ours = proc_start(aux["pid"]) == aux["proc_start"]
        if stop_stale_writer and ours:
            adapters.stop_group(aux["pid"])
        if stop_stale_writer and runs.container_of(aux["purpose"]):
            container_stop(runs.container_of(aux["purpose"]))      # the name is random and ours alone
    alive += [{"aux_pid": a["pid"], "purpose": a["purpose"]} for a in runs.aux_alive(k)]
    for run in runs.unstopped(k):
        if run["pid"] is None:
            # The exec gate guarantees a worker never started without a recorded pid.
            runs.confirm_stopped(k, run["run_id"], {"kind": "never_spawned"})
            closed.append(run["run_id"])
            continue
        if runs.writer_alive(run):
            ours = proc_start(run["pid"]) == run["proc_start"]
            if not stop_stale_writer or not ours:
                # If the recorded leader is gone but its group id is still in use, the remaining
                # processes cannot be proven to be ours (the pid may have been reused), so they are
                # never signalled. The user checks and attests the stop instead.
                alive.append({"run_id": run["run_id"], "pid": run["pid"],
                              "leader": "alive" if ours else "gone; group id still in use, needs user attestation"})
                continue
            adapters.stop_group(run["pid"])
        try:
            runs.confirm_stopped(k, run["run_id"], {"kind": "process_exited"})
            closed.append(run["run_id"])
        except LupusError:
            alive.append({"run_id": run["run_id"], "pid": run["pid"]})
    service.close_interrupted(k)
    return {"runs_closed": closed, "writers_alive": alive, "holds_settled": budget.settle_unowned(k),
            **recovery.reconcile(k)}


# ---------------------------------------------------------------- task execution

def _task_criteria(k: Kernel, task: dict) -> list[dict]:
    wanted = set(task["spec"]["criteria"])
    return [c for c in goals.criteria(k, task["goal_id"]) if c["id"] in wanted]


def _artifact_paths(criteria: list[dict]) -> list[str]:
    paths: list[str] = []
    for c in criteria:
        v = c["verifier"]
        paths += [v["path"]] if "path" in v else list(v.get("paths", []))
    return sorted(set(paths))


LESSON_REQUEST = (
    "이전 시도가 실패했던 작업이다. 이번에 통과시킨 원인 중 다음에도 쓸 수 있는 사실이 있으면 답의 마지막에 "
    "'LESSON: <한 문장>' 형식으로 최대 2줄 적어라. 없으면 적지 마라."
)
SINGLE_PASS = (
    "검증은 이 작업 밖에서 따로 수행된다. 직접 테스트나 검증 명령을 실행하지 말고, 쓴 파일을 다시 읽지 마라. "
    "필요한 파일을 한 번에 최종 형태로 쓴 뒤 'done' 한 단어로 답하라."
)
EXPLORE = (
    "관련 코드를 먼저 찾아 읽은 뒤(파일 검색 도구 사용) 필요한 파일을 수정하라. 추측으로 쓰지 마라. "
    "검증은 이 작업 밖에서 따로 수행된다. 끝나면 'done' 한 단어로 답하라."
)
SELF_CHECK = "셸을 쓸 수 있으면 관련 테스트를 직접 실행해 확인해도 된다."
MAX_INLINE_FILE = 6_000


def _inline_files(k: Kernel, root: Path, paths: list[str]) -> list[str]:
    """Hand the worker the task's files in the prompt, so it does not spend model turns reading
    them one by one. Only files the task's verifiers declare, only small text files, never
    anything that looks like a credential; all within a byte cap (§11.1)."""
    budget, out = k.policy["context_inline_bytes"], []
    real_root = Path(os.path.realpath(root))
    for rel in paths:
        path = root / rel
        resolved = Path(os.path.realpath(path))
        if real_root not in resolved.parents:
            continue          # never hand a provider anything from outside the project
        if budget <= 0 or not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if len(data) > min(MAX_INLINE_FILE, budget) or find_secret_bytes(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        budget -= len(data)
        out += [f"--- 현재 파일 {rel} ---", text.rstrip("\n"), f"--- {rel} 끝 ---"]
    return (["아래는 관련 파일의 현재 내용이다(다시 읽을 필요 없다):"] + out) if out else []


def _last_failures(k: Kernel, task: dict, criteria: list[dict]) -> list[str]:
    """Latest failing evidence for this task's criteria, as recorded by the verifier."""
    latest = goals.latest_evidence(k, task["goal_id"])
    out = []
    for c in criteria:
        ev = latest.get(c["id"])
        if ev is not None and ev["result"] == "FAIL":
            out.append(f"- {c['text']}: {ev['detail'] or 'FAIL'}")
    return out


def _batch_followers(k: Kernel, task: dict, adapter: Adapter) -> list[dict]:
    """Tasks that may ride along in the same worker call: the unbroken chain of PENDING tasks
    that each depend only on the one before. Each keeps its own criteria, evidence and
    checkpoint; riding along only means its instructions are in the same prompt."""
    # Only on the lead's first attempt, and a task rides along at most once: after that it is
    # verified and retried on its own, under its own attempt and no-progress limits.
    limit = k.policy["batch_max_tasks"] if adapter.batch and task["attempt_count"] == 0 else 1
    out, current = [], task
    while len(out) + 1 < limit:
        nxt = [t for t in goals.tasks(k, task["goal_id"]) if t["status"] == "PENDING" and [
            r["depends_on"] for r in k.q("SELECT depends_on FROM task_dep WHERE task_id = ?", t["task_id"])
        ] == [current["task_id"]]]
        if len(nxt) != 1 or not _task_criteria(k, nxt[0]) or _was_batched(k, nxt[0]["task_id"]):
            break
        out.append(nxt[0])
        current = nxt[0]
    return out


def _was_batched(k: Kernel, task_id: str) -> bool:
    return k.one("SELECT 1 FROM event WHERE type = 'batch.planned' AND aggregate_id = ?", task_id) is not None


def build_prompt(k: Kernel, task: dict, continuation: dict | None = None, recalled: str = "",
                 root: Path | None = None, followers: list[dict] | None = None, shell: bool = False) -> str:
    """Minimal worker context, assembled from structured state. No model writes or summarises
    it, and no transcript of an earlier CLI session is carried over (§6.7, §11.1)."""
    goal = goals.get(k, task["goal_id"])
    criteria = _task_criteria(k, task)
    lines = [
        f"목표: {goal['objective']}",
        f"현재 작업: {task['title']}",
        task["spec"]["prompt"],
        "완료 조건(별도의 결정적 검사로 확인되며, 완료했다는 보고만으로는 인정되지 않는다):",
        *[f"- {c['text']}" for c in criteria],
        "현재 디렉터리 안의 파일만 읽고 수정하라. 다른 작업은 하지 마라.",
    ]
    notes = goals.resolutions(k, task["task_id"])
    if notes:
        lines += ["사용자가 이 작업에 추가로 준 지시(가장 최근 것이 우선한다):", *[f"- {n}" for n in notes[-3:]]]
    failures = _last_failures(k, task, criteria)     # from an earlier attempt or a batch pre-check
    if failures:
        # Second pass is the careful one: say exactly what failed and allow self-checking.
        lines += ["이전 시도는 아래 검증에 실패했다(검증기의 실제 출력이다). 원인을 먼저 확인하고 고쳐라. "
                  "필요하면 파일을 다시 읽어 확인해도 된다.", *failures]
    paths = _artifact_paths(criteria) + task["spec"].get("inputs", [])
    for i, follower in enumerate(followers or [], 2):
        extra = _task_criteria(k, follower)
        lines += [f"이어서 다음 작업도 수행하라({i}번째, 따로 검증된다): {follower['title']}", follower["spec"]["prompt"],
                  *[f"- {c['text']}" for c in extra]]
        paths += _artifact_paths(extra) + follower["spec"].get("inputs", [])
    # The verifiers' files plus files the tasks name as their inputs (same limits apply).
    inlined = _inline_files(k, root, list(dict.fromkeys(paths))) if root is not None else []
    if not failures:
        # Everything needed is in front of the worker: write once. Otherwise it has to look first,
        # and writing blind in one pass would only produce a confident wrong answer.
        whole = root is None or all((root / p).is_file() and f"--- 현재 파일 {p} ---" in inlined for p in task["spec"].get("inputs", []))
        lines.append(SINGLE_PASS if whole else EXPLORE + (" " + SELF_CHECK if shell else ""))
    lines += inlined
    if continuation:
        work = continuation["work"]
        lines += [
            "이 작업은 중단된 업무를 이어받은 것이다. 이미 끝난 작업을 다시 하지 마라.",
            f"완료된 작업: {json.dumps(work['done'], ensure_ascii=False)}",
            f"남은 작업: {json.dumps(work['remaining'], ensure_ascii=False)}",
            f"다음 행동: {work['next_action']}",
        ]
        diff = work["workspace_diff"]
        if diff["changed"] or diff["missing"]:
            lines.append(
                "마지막 저장 이후 바뀌었거나 사라진 파일(완료로 간주하지 말고 먼저 확인): "
                + json.dumps(diff, ensure_ascii=False)
            )
    if recalled:
        lines.append(recalled)
    if k.policy["memory_recall_tokens"] > 0 and task["attempt_count"] >= 1:
        # Asked only after a failure: that is when there is something worth recording, and a
        # first-try success should not pay output tokens for it.
        lines.append(LESSON_REQUEST)
    return "\n".join(lines)


def _progress_lists(k: Kernel, goal_id: str) -> tuple[list[str], list[str]]:
    all_tasks = goals.tasks(k, goal_id)
    return ([t["task_id"] for t in all_tasks if t["status"] == "DONE"],
            [t["task_id"] for t in all_tasks if t["status"] not in ("DONE", "CANCELLED")])


def _snapshots(root: Path, paths: list[str]) -> tuple[list[dict], list[str]]:
    """Small verified artifacts become pinned recovery objects, so a checkpoint can restore
    content and not merely notice that it is gone. Oversized or secret-looking files are left
    out and the omission is recorded."""
    objects, notes = [], []
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) > MAX_SNAPSHOT_BYTES:
            notes.append(f"snapshot skipped (size): {rel}")
        elif find_secret_bytes(data):
            notes.append(f"snapshot skipped (credential pattern): {rel}")
        else:
            objects.append({"label": rel, "role": "snapshot", "data": data})
    return objects, notes


def _checkpoint(k: Kernel, goal_id: str, next_action: str, paths: list[str], root: Path,
                run_id: str | None = None, token: int | None = None, with_objects: bool = False) -> dict:
    done, remaining = _progress_lists(k, goal_id)
    objects, notes = _snapshots(root, paths) if with_objects else ([], [])
    prev = recovery.latest(k, goal_id)
    return recovery.commit(
        k, goal_id, expected_revision=prev["revision"] if prev else 0, done=done, remaining=remaining,
        next_action=next_action, decisions=notes, files=paths, objects=objects, run_id=run_id, token=token,
    )


def _already_satisfied(k: Kernel, goal: dict, task: dict, criteria: list[dict], root: Path, run_id: str,
                       token: int) -> dict[str, Any] | None:
    """Verify the task's own criteria without calling a model. All pass: evidence is recorded,
    a checkpoint is committed and the task is DONE. Otherwise the failures are recorded (they
    become the next attempt's feedback) and None is returned. Verification time is reserved and
    charged like any other."""
    goal_id = goal["goal_id"]
    try:
        held = _verify_reserve(criteria)
        hold = budget.reserve(k, goal["budget_id"], "safety", "pre-attempt verification",
                              {"active_ms": held["active_ms"], "calls": held["calls"] - 1}, run_id)
    except LupusError:
        return None
    try:
        checked, verify_ms, judge_calls = _timed_verify(k, goal, criteria, root)
    except LupusError:
        budget.settle(k, hold, None, "estimated")
        return None
    budget.settle(k, hold, {"active_ms": verify_ms, "calls": judge_calls}, "measured")
    with k.tx():
        runs.guard(k, run_id, token)
        for c, verdict, digest, detail in checked:
            goals.record_evidence(k, goal_id, c["id"], verify.VERSION, digest, verdict, detail, task_id=task["task_id"],
                                  acceptance_revision=goal["acceptance_revision"], verifier=c["verifier"])
    if not criteria or any(verdict != "PASS" for _, verdict, _, _ in checked):
        return None
    _checkpoint(k, goal_id, "verified without a model call", _artifact_paths(criteria), root, run_id, token,
                with_objects=True)
    runs.finish(k, run_id, token, "DONE", "already_satisfied")
    return {"status": "DONE", "task_id": task["task_id"], "outcome": "ALREADY_SATISFIED",
            "verdicts": {c["id"]: v for c, v, _, _ in checked}, "variant": None, "error_class": None,
            "usage_observed": False, "leftover_processes": False}


def run_task(
    k: Kernel,
    goal_id: str,
    adapter: Adapter,
    *,
    timeout_s: float = 600,
    work_calls: int = 8,
    claimed: dict | None = None,
    continuation: dict | None = None,
    tiers: list[Adapter] | None = None,
) -> dict[str, Any]:
    """Execute the next runnable task once. Returns a small status record; never raises for an
    expected refusal (budget, repetition, stale lease): those become recorded task states.

    `tiers` are variants of the SAME driver (lighter first). The n-th attempt of a task uses the
    n-th tier, so a lighter model is tried once and the default one only after verification
    failed. Escalation is an ordinary second attempt: same budget, same attempt limits."""
    if claimed is None:
        task = goals.next_runnable(k, goal_id)
        if task is None:
            return {"status": "NO_RUNNABLE_TASK"}
    else:
        task = goals.get_task(k, claimed["task_id"])
    if tiers:
        adapter = tiers[min(task["attempt_count"], len(tiers) - 1)]
    criteria = _task_criteria(k, task)
    safety = _verify_reserve(criteria)
    # The lease has to outlive the worker AND its verification.
    ttl_ms = int(timeout_s * 1000) + safety["active_ms"] + 120_000
    if claimed is None:
        run = runs.claim(k, task["task_id"], adapter.driver, adapter.auth_mode, ttl_ms=ttl_ms)
        run_id, token = run["run_id"], run["fencing_token"]
    else:
        run_id, token = claimed["run_id"], claimed["fencing_token"]
        runs.heartbeat(k, run_id, token, ttl_ms)
    task_id = task["task_id"]
    goal = goals.get(k, goal_id)
    root = Path(projects.get(k, goal["project_id"])["canonical_root"])
    paths = _artifact_paths(criteria)
    # Verification inputs (tests, runner config) are frozen before the goal's first worker. If
    # they already differ, someone other than a Lupus worker changed them: do not overwrite,
    # do not run, ask.
    try:
        protect.freeze(k, goal_id, root)
    except LupusError as exc:
        # e.g. a symlinked test that cannot be frozen: stop here, before any worker, and say why.
        runs.finish(k, run_id, token, "NEEDS_ANSWER", "protect_refused", f"{exc.code}: {exc.detail}")
        return {"status": "NEEDS_ANSWER", "task_id": task_id, "reason": exc.code}
    interrupted = k.one("SELECT outcome FROM attempt WHERE goal_id = ? ORDER BY rowid DESC LIMIT 1", goal_id)
    if interrupted is not None and interrupted["outcome"] == "ABANDONED":
        # The last worker of this goal was cut off before its changes could be checked. What it
        # left in the protected files is its doing: put it back now (versions are kept aside).
        protect.restore(k, goal_id, root, keep_dir=k.runtime / "displaced" / run_id)
    outside = protect.drift(k, goal_id, root)
    if outside:
        names = ", ".join(sorted(d["path"] for d in outside)[:5])
        runs.finish(k, run_id, token, "NEEDS_ANSWER", "protected_changed",
                    f"보호된 검증 파일이 Lupus 밖에서 바뀌었다: {names}. 의도한 변경이면 `lupus refreeze`로 확정할 것")
        return {"status": "NEEDS_ANSWER", "task_id": task_id, "reason": "PROTECTED_CHANGED_OUTSIDE"}
    # After an interrupted attempt the work may already be on disk (the supervisor died after
    # the worker finished). Check before paying for another model call. Done inside the run, so
    # the verifier executes under the project's writer slot like any other.
    last = k.one("SELECT outcome FROM attempt WHERE task_id = ? ORDER BY rowid DESC LIMIT 1", task_id)
    # …and when this task's instructions already rode along in an earlier call (a batch), its own
    # criteria decide whether that call did the work: verified first, called only if it fails.
    if (last is not None and last["outcome"] in ("ABANDONED", "ENV_BLOCKED")) or (
            last is None and _was_batched(k, task_id)):
        skipped = _already_satisfied(k, goal, task, criteria, root, run_id, token)
        if skipped is not None:
            return skipped
    before = goals.latest_evidence(k, goal_id)
    try:
        baseline = sha256_json([[c["id"], verify.artifact_hash(c["verifier"], root)] for c in criteria])
    except LupusError as exc:      # e.g. a declared evidence path with too many files to hash
        runs.finish(k, run_id, token, "NEEDS_ANSWER", "evidence_scope_refused", f"{exc.code}: {exc.detail}")
        return {"status": "NEEDS_ANSWER", "task_id": task_id, "reason": exc.code}
    passing_before = {
        c["id"] for c in criteria
        if before.get(c["id"]) and before[c["id"]]["result"] == "PASS"
        and before[c["id"]]["artifact_hash"] == verify.artifact_hash(c["verifier"], root)
    }

    # 1. attempt + reservations, durably, before anything runs
    try:
        attempt = runs.start_attempt(
            k, run_id, token,
            # Each time the user sends the task back with new input, that is a new approach they
            # authorised. Attempts, budget and the audit trail stay cumulative.
            hypothesis_id=f"{task_id}:direct" + (f":r{len(goals.resolutions(k, task_id))}" if goals.resolutions(k, task_id) else ""),
            baseline_hash=baseline,
            # What the worker is told is part of the attempt's identity: a retry that carries the
            # verifier's failure output is not a repeat of the attempt that produced that failure.
            change_scope=sha256_json([task["spec"], _last_failures(k, task, criteria), goals.resolutions(k, task_id)]),
            verifier_version=verify.VERSION,
            env_hash=f"{adapter.driver}:{adapter.variant}",
            new_evidence=f"task attempt {task['attempt_count'] + 1} from artifact state {baseline[:12]}",
            work={"calls": work_calls, "active_ms": int(timeout_s * 1000)},
            safety=safety,
        )
    except LupusError as exc:
        status = "BUDGET_EXHAUSTED" if exc.code == "BUDGET_EXHAUSTED" else "NO_PROGRESS"
        if exc.code not in ("BUDGET_EXHAUSTED", "DUPLICATE_ATTEMPT", "NO_NEW_EVIDENCE", "HYPOTHESIS_EXHAUSTED",
                            "NO_PROGRESS_LIMIT"):
            runs.begin_stop(k, run_id)
            runs.confirm_stopped(k, run_id, {"kind": "never_spawned"})
            raise
        runs.finish(k, run_id, token, status, "not_started", f"{exc.code}: {exc.detail}")
        return {"status": status, "task_id": task_id, "reason": exc.code}
    attempt_id = attempt["attempt_id"]

    # 2. checkpoint the intent, then run the worker behind the exec gate
    try:
        _checkpoint(k, goal_id, f"run task {task_id}", paths, root, run_id, token)
        # Memory: a local lookup scoped to this project (+ global), recorded against the attempt.
        followers = _batch_followers(k, task, adapter)
        with k.tx():      # declared before the call: after an interruption these are all re-verified
            for follower in followers:
                k.emit("supervisor", "batch.planned", "task", follower["task_id"], lead_attempt=attempt_id,
                       acceptance_revision=goal["acceptance_revision"])
        recalled = memory.recall(
            k, project_id=goal["project_id"], goal_id=goal_id, attempt_id=attempt_id,
            query="\n".join([task["title"], task["spec"]["prompt"], *[c["text"] for c in criteria]]))
        crash_point("supervisor.before_worker")
        taken = gitx.guard(root)
        result = adapters.execute(
            adapter, build_prompt(k, task, continuation, memory.render(recalled), root, followers,
                                  shell=getattr(adapter, "shell", False)), root,
            on_spawn=lambda pid: runs.attach_process(k, run_id, token, pid), timeout_s=timeout_s,
        )
    except LupusError as exc:
        return _abandon(k, run_id, token, attempt_id, task_id, exc)
    crash_point("supervisor.after_worker")

    # 3. usage is recorded whatever happens next: spend that occurred is never dropped
    if result.usage is not None:
        usage.record(
            k, event_id=f"{run_id}:{attempt_id}", run_id=run_id,
            provider_session_id=result.session_id or run_id, mode="delta", sequence=1,
            reported=result.usage, observation="measured", source=adapter.driver,
        )
    work_actual = {"calls": result.usage["calls"] if result.usage and "calls" in result.usage else work_calls,
                   "active_ms": result.duration_ms}
    observation = "measured" if result.usage is not None else "estimated"

    # 4. verify (outside any transaction), then integrate under the fencing guard
    try:
        # Whatever the worker did to the protected files is undone BEFORE verifying, so the
        # checks that run are the ones that were frozen.
        undone = protect.restore(k, goal_id, root, keep_dir=k.runtime / "displaced" / run_id)
        gitx.unguard(k, root, taken, goal_id)      # hooks/config a worker left in .git never get to run
        checked, verify_ms, judge_calls = _timed_verify(k, goal, criteria, root)
        # Running the checks can itself write into protected paths (a test with side effects):
        # leave the project as frozen, not as the verifier left it.
        protect.restore(k, goal_id, root, keep_dir=k.runtime / "displaced" / run_id / "after-verify")
    except LupusError as exc:
        # Verification was cut short (e.g. a judge could not answer). What it had already spent is not
        # known here, so its whole reservation is charged.
        return _abandon(k, run_id, token, attempt_id, task_id, exc, work_actual, observation, dict(safety))
    if undone["unrestorable"]:
        why = "보호된 검증 파일이 바뀌었고 되돌릴 수 없다: " + ", ".join(undone["unrestorable"])
        checked = [(c, "FAIL", digest, why) for c, _, digest, _ in checked]
    elif undone["restored"]:
        note_undo = "보호된 검증 파일을 건드려 되돌렸다(수정 금지): " + ", ".join(undone["restored"]) + ". "
        checked = [(c, v, digest, (note_undo + detail) if v == "FAIL" else detail) for c, v, digest, detail in checked]
    safety_actual = {"calls": judge_calls, "active_ms": verify_ms}
    verdicts = {c["id"]: verdict for c, verdict, _, _ in checked}
    all_pass = all(v == "PASS" for v in verdicts.values())
    if result.error_class in ENV_ERRORS and not all_pass:
        outcome = "ENV_BLOCKED"       # not an implementation failure; no improvement loop (§6.3)
    elif {cid for cid, v in verdicts.items() if v == "PASS"} - passing_before:
        outcome = "PROGRESS"
    else:
        outcome = "NO_PROGRESS"
    note = f"exit={result.exit_code} error={result.error_class} verdicts={json.dumps(verdicts)}"
    try:
        with k.tx():
            runs.guard(k, run_id, token)
            for c, verdict, digest, detail in checked:
                # Refused if the user changed the contract while the worker ran: an old PASS
                # must not become evidence for a new requirement.
                goals.record_evidence(k, goal_id, c["id"], verify.VERSION, digest, verdict, detail,
                                      task_id=task_id, attempt_id=attempt_id,
                                      acceptance_revision=attempt["acceptance_revision"], verifier=c["verifier"])
            closed = runs.finish_attempt(k, run_id, token, attempt_id, outcome, note, work_actual,
                                         safety_actual, observation)
            memory.feedback(k, attempt_id, outcome)
            if outcome == "PROGRESS" and k.policy["memory_recall_tokens"] > 0:
                memory.capture_worker_lessons(k, result.text, project_id=goal["project_id"], goal_id=goal_id,
                                              task_id=task_id, attempt_id=attempt_id)
            if closed["must_stop"]:
                memory.capture_dead_end(
                    k, project_id=goal["project_id"], goal_id=goal_id, task_id=task_id, attempt_id=attempt_id,
                    task_title=task["title"], attempts=task["attempt_count"] + 1,
                    details={c["id"]: detail for c, verdict, _, detail in checked if verdict != "PASS"})
        _checkpoint(k, goal_id, "verify and continue", paths, root, run_id, token, with_objects=True)
    except LupusError as exc:
        return _abandon(k, run_id, token, attempt_id, task_id, exc, work_actual, observation, safety_actual)

    # 5. release the writer with the task's real state
    if all_pass:
        task_status, reason = "DONE", ""
    elif outcome == "ENV_BLOCKED":
        task_status = "EXTERNAL_BLOCKED"
        reason = {"quota": "provider usage limit: wait for reset or hand off to another approved AI",
                  "rate_limit": "provider rate limit: bounded backoff",
                  "auth": "provider login required",
                  "unavailable": "worker CLI could not be started"}[result.error_class]
    elif closed["must_stop"]:
        task_status = "NO_PROGRESS"
        reason = f"no valid progress in {k.policy['no_progress_limit']} consecutive attempts; last: {note}"
    else:
        task_status, reason = "PENDING", ""
    try:
        runs.finish(k, run_id, token, task_status, outcome.lower(), reason)
    except LupusError as exc:
        return _abandon(k, run_id, token, attempt_id, task_id, exc, work_actual, observation, safety_actual)
    return {"status": task_status, "task_id": task_id, "outcome": outcome, "verdicts": verdicts,
            "variant": adapter.variant, "batched_with": [f["task_id"] for f in followers],
            "error_class": result.error_class, "usage_observed": result.usage is not None,
            "os_sandbox": bool(result.raw.get("os_sandbox")), "leftover_processes": result.leftover_processes}


def _abandon(k: Kernel, run_id: str, token: int, attempt_id: str, task_id: str, exc: LupusError,
             work_actual: dict | None = None, observation: str = "estimated",
             safety_actual: dict | None = None) -> dict[str, Any]:
    """The run lost its authority mid-flight (lease expired during sleep, access revoked, goal
    paused/cancelled, checkpoint refused…). Its result is not integrated, its cost still is,
    and the writer slot is released only with stop evidence."""
    runs.finish_attempt(k, None, None, attempt_id, "ABANDONED", f"result rejected: {exc.code}",
                        work_actual, safety_actual or {"calls": 0, "active_ms": 0}, observation)
    memory.feedback(k, attempt_id, "ABANDONED")
    runs.begin_stop(k, run_id)
    run = runs.get(k, run_id)
    runs.confirm_stopped(k, run_id, {"kind": "process_exited" if run["pid"] else "never_spawned",
                                     "result": f"rejected:{exc.code}"})
    return {"status": "REJECTED", "task_id": task_id, "reason": exc.code, "detail": exc.detail}


# ---------------------------------------------------------------- goal loop

def _last_driver(k: Kernel, goal_id: str) -> str | None:
    row = k.one("SELECT execution_driver FROM run WHERE goal_id = ? ORDER BY fencing_token DESC LIMIT 1",
                goal_id)
    return row["execution_driver"] if row else None


def _final_blockers(k: Kernel, goal_id: str) -> list[str]:
    """Final verification executes verifier commands in the project, so it needs the same
    standing as a run: current checkpoint authority and nobody else alive in the project (the
    supervisor lock keeps another supervisor from claiming meanwhile)."""
    goal = goals.get(k, goal_id)
    blockers = goals.authority_blockers(k, goal_id)
    try:
        projects.check_root(k, goal["project_id"])
    except LupusError as exc:
        blockers.append(exc.code)
    if goal["status"] != "ACTIVE":
        blockers.append(f"GOAL_STATUS:{goal['status']}")
    if (k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", goal["project_id"])
            or runs.aux_alive(k, goal["project_id"])):
        blockers.append("WRITER_NOT_STOPPED")
    if "PROJECT_ROOT_CHANGED" not in blockers and protect.drift(k, goal_id, goals.project_root(k, goal_id)):
        blockers.append("PROTECTED_CHANGED_OUTSIDE")
    return blockers


def _final_verification(k: Kernel, goal_id: str, root: Path) -> str | None:
    """Before deciding DONE, check only what is not already covered (§6.4): criteria whose
    evidence went stale, and — once every task is finished — criteria with no evidence under
    the current acceptance revision (e.g. the user revised the contract). Current, unchanged
    evidence is reused, not re-run. Verification time is reserved first and charged afterwards;
    without budget or authority for it nothing is verified and the goal is simply not complete.
    Returns the reason nothing was verified, or None."""
    goal = goals.get(k, goal_id)
    blockers = _final_blockers(k, goal_id)
    if blockers:
        return ";".join(blockers)
    latest = goals.latest_evidence(k, goal_id)
    all_done = all(t["status"] in goals.TASK_TERMINAL for t in goals.tasks(k, goal_id))
    todo = []
    for c in goals.criteria(k, goal_id):
        ev = latest[c["id"]]
        if (ev is None and all_done) or (ev is not None and goals.evidence_stale(k, goal_id, c, ev)):
            todo.append(c)
    if not todo:
        return None
    try:
        held = _verify_reserve(todo)
        hold = budget.reserve(k, goal["budget_id"], "safety", "final verification",
                              {"active_ms": held["active_ms"], "calls": held["calls"] - 1})
    except LupusError as exc:
        return exc.code
    try:
        checked, verify_ms, judge_calls = _timed_verify(k, goal, todo, root)
    except LupusError as exc:
        budget.settle(k, hold, None, "estimated")      # interrupted: charged in full
        return exc.code
    budget.settle(k, hold, {"active_ms": verify_ms, "calls": judge_calls}, "measured")
    try:
        with k.tx():      # all or nothing; authority is re-checked at integration time
            blockers = _final_blockers(k, goal_id)
            if blockers:
                raise LupusError("NOT_READY", ";".join(blockers))
            for c, verdict, digest, detail in checked:
                goals.record_evidence(k, goal_id, c["id"], verify.VERSION, digest, verdict, detail,
                                      acceptance_revision=goal["acceptance_revision"], verifier=c["verifier"])
    except LupusError as exc:
        return f"{exc.code}:{exc.detail}"
    return None


def finish(k: Kernel, goal_id: str) -> dict[str, Any]:
    """Decide completion without running a worker (e.g. right after the user approved a document)."""
    with k.supervisor_lock():
        done = False
        if goals.get(k, goal_id)["status"] == "ACTIVE":
            _final_verification(k, goal_id, goals.project_root(k, goal_id))
            if not _final_blockers(k, goal_id) and not goals.completion_blockers(k, goal_id):
                goals.complete(k, goal_id)
                done = True
        return {"goal_id": goal_id, "done": done, "goal_status": goals.get(k, goal_id)["status"],
                "completion_blockers": [] if done else list(dict.fromkeys(
                    goals.completion_blockers(k, goal_id) + _final_blockers(k, goal_id)))}


def run_goal(k: Kernel, goal_id: str, adapter: Adapter, *, max_steps: int = 20,
             timeout_s: float = 600, work_calls: int = 8, tiers: list[Adapter] | None = None) -> dict[str, Any]:
    if tiers and any(t.driver != adapter.driver for t in tiers):
        raise LupusError("TIERS_INVALID", "tiers must be variants of the same driver")
    with k.supervisor_lock():
        # The user is running this goal now, with this AI: branches that stopped because a provider
        # was out of quota or logged out get their bounded retry (attempt limits still apply).
        for task in goals.tasks(k, goal_id):
            if task["status"] == "EXTERNAL_BLOCKED" and goals.get(k, goal_id)["status"] not in goals.GOAL_STICKY:
                goals.resolve_wait(k, task["task_id"], "supervisor", f"retry with {adapter.driver}")
        return _run_goal(k, goal_id, adapter, max_steps, timeout_s, work_calls, tiers)


def _run_goal(k: Kernel, goal_id: str, adapter: Adapter, max_steps: int, timeout_s: float,
              work_calls: int, tiers: list[Adapter] | None = None) -> dict[str, Any]:
    """Advance a goal until it is DONE or has to wait. Switching to a different driver than the
    one that last worked on the goal goes through a validated handoff, never a bare restart."""
    steps: list[dict] = []
    goal = goals.get(k, goal_id)
    root = Path(projects.get(k, goal["project_id"])["canonical_root"])
    if recovery.latest(k, goal_id) is None and goal["status"] == "ACTIVE":
        _checkpoint(k, goal_id, "start", [], root)
    for _ in range(max_steps):
        if goals.get(k, goal_id)["status"] != "ACTIVE" or goals.next_runnable(k, goal_id) is None:
            break
        # Every step proves that the goal may be resumed from its checkpoint with this driver
        # (epochs, writer stopped, unresolved actions, budget, provider, capabilities), not only
        # when the driver changes. The proof and the claim are one transaction, so a revocation
        # cannot slip in between them.
        previous = _last_driver(k, goal_id)
        claimed = continuation = None
        if previous is None or previous == adapter.driver:
            try:
                with k.tx():
                    ready = recovery.readiness(k, goal_id, adapter.driver)
                    if not ready["ready"]:
                        raise LupusError("NOT_READY", ";".join(ready["blockers"]))
                    run = runs.claim(k, ready["next_task_id"], adapter.driver, adapter.auth_mode)
                claimed = {"run_id": run["run_id"], "fencing_token": run["fencing_token"],
                           "task_id": run["task_id"]}
            except LupusError as exc:
                blockers = exc.detail.split(";") if exc.code == "NOT_READY" else [f"{exc.code}:{exc.detail}"]
                steps.append({"status": "NOT_READY", "blockers": blockers})
                break
        else:
            try:
                continuation = handoff.prepare(k, goal_id, adapter.driver)
                claimed = handoff.accept(
                    k, f"{continuation['handoff_id']}:accept", continuation,
                    {"checkpoint_revision": continuation["checkpoint"]["revision"],
                     "next_task_id": continuation["work"]["next_task_id"]},
                    adapter.auth_mode,
                )
            except LupusError as exc:
                steps.append({"status": "HANDOFF_REFUSED", "reason": exc.code, "detail": exc.detail})
                break
        try:
            step = run_task(k, goal_id, adapter, timeout_s=timeout_s, work_calls=work_calls,
                            claimed=claimed, continuation=continuation, tiers=tiers)
        except LupusError as exc:
            steps.append({"status": "REFUSED", "reason": exc.code, "detail": exc.detail})
            break
        steps.append(step)
        if step["status"] in ("NO_RUNNABLE_TASK", "REJECTED"):
            break
    done, unverified = False, None
    if goals.get(k, goal_id)["status"] == "ACTIVE":
        unverified = _final_verification(k, goal_id, root)
        # Completion needs the same standing as verification (authority, nobody else alive,
        # protected files as frozen), not only passing evidence.
        if not _final_blockers(k, goal_id) and not goals.completion_blockers(k, goal_id):
            goals.complete(k, goal_id)
            done = True
    view = goals.view(k, goal_id)
    return {
        "goal_id": goal_id, "done": done, "goal_status": view["status"], "steps": steps,
        "waiting": view["waiting"], "budget": budget.snapshot(k, goal["budget_id"]),
        "usage": usage.totals(k, goal_id),
        "judge_usage": service.totals(k, goal_id),
        "memory": memory.summary(k, goal_id),
        "completion_blockers": [] if done else list(dict.fromkeys(
            goals.completion_blockers(k, goal_id) + _final_blockers(k, goal_id))),
        # Why the last verification could not be run, when that is what stands between here and done.
        **({"final_verification_not_run": unverified} if unverified and not done else {}),
        # Exactly what was checked, in the user's words: "done" means these and nothing more.
        "checks": [{"id": c["id"], "text": c["text"], "result": view["evidence"].get(c["id"])} for c in view["criteria"]],
    }
