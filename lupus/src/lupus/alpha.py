"""Alpha: one standing identity per project and one for the whole runtime.

An Alpha here is NOT a model that stays awake and it makes no model call of its own. It is what
the supervisor uses to run several projects as one piece of work:

  * a shared budget: every new goal's budget hangs under its project's Alpha budget, which hangs
    under the global one, so a cap set once holds across goals and projects
  * an order: which unfinished goal goes next (the user's priority, then age), one step each in
    turn so no project starves
  * routing: which approved AI works on a goal, and a switch to the other one (through the usual
    validated handoff) when the first is out of quota or logged out

No project's files, memory or failure output are shown to another project's workers by any of
this: the only thing that crosses projects is the numbers in the budget and what the user
explicitly promoted to global knowledge.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

from . import budget, goals, projects, runs, supervisor
from .adapters import Adapter
from .kernel import Kernel, Stop
from .util import LupusError, container_stop, new_id, proc_start, stop_group

DRIVERS = ("native_claude", "native_codex")
RETRYABLE = ("quota", "rate_limit", "auth", "unavailable")
MAX_PARALLEL = 4      # worker steps in flight at once (`alpha-run --parallel`)


def ensure(k: Kernel, project_id: str | None = None) -> dict:
    with k.tx():
        row = (k.one("SELECT * FROM alpha WHERE project_id = ?", project_id) if project_id
               else k.one("SELECT * FROM alpha WHERE scope = 'global'"))
        if row is None:
            if project_id:
                projects.get(k, project_id)
            k.run("INSERT INTO alpha(alpha_id, scope, project_id, created_at) VALUES (?,?,?,?)",
                  new_id("alpha"), "project" if project_id else "global", project_id, k.now())
            row = (k.one("SELECT * FROM alpha WHERE project_id = ?", project_id) if project_id
                   else k.one("SELECT * FROM alpha WHERE scope = 'global'"))
        return dict(row)


def set_budget(k: Kernel, project_id: str | None, caps: dict[str, int], actor: str, reason: str = "") -> dict:
    """Give an Alpha a cap that all goals under it share. It is a total, not a rate: when it is
    used up nothing under it starts until the user raises it. Applies to goals created afterwards."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "shared budgets are set by the user")
    with k.tx():
        me = ensure(k, project_id)
        if me["budget_id"] is None:
            parent = ensure(k)["budget_id"] if project_id else None
            budget_id = budget.create(k, f"alpha:{project_id or 'global'}", caps, parent)
            k.run("UPDATE alpha SET budget_id = ? WHERE alpha_id = ?", budget_id, me["alpha_id"])
            if project_id is None and k.one("SELECT 1 FROM reservation WHERE status = 'HELD'"):
                # A hold was counted against the old chain; moving the chain under it would unbalance the books.
                raise LupusError("BUDGET_IN_USE", "work is running or was interrupted; finish or `lupus recover` first")
            if project_id is None:      # project Alphas that already had a budget now answer to the global one
                k.run("UPDATE budget SET parent_budget_id = ? WHERE budget_id IN "
                      "(SELECT budget_id FROM alpha WHERE scope = 'project' AND budget_id IS NOT NULL) "
                      "AND parent_budget_id IS NULL", budget_id)
            k.emit(actor, "alpha.budget_set", "alpha", me["alpha_id"], caps=caps)
        else:
            for dimension, cap in caps.items():
                if k.one("SELECT 1 FROM budget_line WHERE budget_id = ? AND dimension = ?", me["budget_id"], dimension):
                    budget.raise_cap(k, me["budget_id"], dimension, int(cap), actor, reason or "alpha budget changed")
                else:
                    k.run("INSERT INTO budget_line(budget_id, dimension, cap) VALUES (?,?,?)", me["budget_id"], dimension, int(cap))
        return {**ensure(k, project_id), "budget": budget.snapshot(k, ensure(k, project_id)["budget_id"])}


def parent_budget(k: Kernel, project_id: str) -> str | None:
    """What a new goal's budget hangs under: the project's Alpha budget, else the global one."""
    row = k.one("SELECT budget_id FROM alpha WHERE project_id = ? AND budget_id IS NOT NULL", project_id)
    if row is None:
        row = k.one("SELECT budget_id FROM alpha WHERE scope = 'global' AND budget_id IS NOT NULL")
    return row["budget_id"] if row else None


def set_priority(k: Kernel, goal_id: str, priority: int, actor: str) -> None:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "priorities are set by the user")
    with k.tx():
        goals.get(k, goal_id)
        k.run("UPDATE goal SET priority = ? WHERE goal_id = ?", int(priority), goal_id)
        k.emit(actor, "goal.priority", "goal", goal_id, priority=int(priority))


def portfolio(k: Kernel) -> dict[str, Any]:
    """Status across projects: counts, states and budgets. Metadata only."""
    out = []
    for p in k.q("SELECT project_id, name FROM project ORDER BY created_at"):
        rows = k.q("SELECT goal_id, status, priority, objective FROM goal WHERE project_id = ? ORDER BY created_at", p["project_id"])
        me = k.one("SELECT budget_id FROM alpha WHERE project_id = ?", p["project_id"])
        open_goals = [{"goal_id": g["goal_id"], "status": g["status"], "priority": g["priority"], "objective": g["objective"][:60],
                       "waiting": [w["reason"][:80] for w in goals.wait_reasons(k, g["goal_id"])]}
                      for g in rows if g["status"] not in goals.GOAL_TERMINAL]
        out.append({"project_id": p["project_id"], "name": p["name"], "open": open_goals,
                    "done": sum(g["status"] == "DONE" for g in rows),
                    "budget": budget.snapshot(k, me["budget_id"]) if me and me["budget_id"] else None})
    top = k.one("SELECT budget_id FROM alpha WHERE scope = 'global'")
    return {"projects": out, "global_budget": budget.snapshot(k, top["budget_id"]) if top and top["budget_id"] else None}


def _usable(k: Kernel, project: dict, driver: str) -> str | None:
    """None when this driver may work on the project; otherwise why not."""
    if not projects.provider_allowed(project, driver):
        return "provider not approved for this project"
    missing = projects.capability_blockers(k, driver)
    if missing:
        return "not measured on this machine (lupus probe --live)"
    return projects.confinement_blocker(k, project, driver)


def route(k: Kernel, goal: dict, drivers: list[str], unavailable: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    """The driver for this goal's next step: the one that worked on it last if it still can
    (no handoff needed), otherwise the first usable one in the user's order."""
    project = projects.get(k, goal["project_id"])
    why: dict[str, str] = {}
    usable = []
    for driver in drivers:
        reason = unavailable.get(driver) or _usable(k, project, driver)
        if reason:
            why[driver] = reason
        else:
            usable.append(driver)
    last = supervisor._last_driver(k, goal["goal_id"])
    return (last if last in usable else (usable[0] if usable else None)), why


def _unblock(k: Kernel, goal_id: str, driver: str) -> bool:
    """A branch that stopped because ITS provider was out of quota / logged out may continue on a
    different approved one. Budget, attempts and the no-progress count are untouched."""
    moved = False
    for task in goals.tasks(k, goal_id):
        if task["status"] != "EXTERNAL_BLOCKED":
            continue
        last = k.one("SELECT execution_driver FROM run WHERE task_id = ? ORDER BY fencing_token DESC LIMIT 1", task["task_id"])
        if last is not None and last["execution_driver"] != driver:
            goals.resolve_wait(k, task["task_id"], "supervisor", f"continue on {driver}: {task['wait_reason'] or 'provider blocked'}")
            moved = True
    return moved


def _advance(k: Kernel, goal_id: str, drivers: list[str], make_adapter: Callable[[str], Adapter], timeout_s: float,
             unavailable: dict[str, str]) -> dict[str, Any]:
    """One turn for one goal: at most one worker step, or only the completion check when no task
    is left. Returns what happened; `moved` is False when the goal is exactly where it was."""
    goal = goals.get(k, goal_id)
    driver, why = route(k, goal, drivers, unavailable)
    if driver is None:
        return {"skip": "no usable AI: " + json.dumps(why, ensure_ascii=False), "moved": False}
    if goal["status"] == "EXTERNAL_BLOCKED" and not _unblock(k, goal_id, driver):
        return {"skip": "blocked on its provider and no other approved AI is available", "moved": False}
    if goals.get(k, goal_id)["status"] != "ACTIVE":
        return {"moved": False}
    if goals.next_runnable(k, goal_id) is None:
        report = supervisor._run_goal(k, goal_id, make_adapter(driver), 0, timeout_s, 8)   # completion only
        if report["done"]:
            return {"step": {"goal_id": goal_id, "driver": None, "status": "DONE", "done": True}, "moved": True}
        return {"skip": "waiting: " + ";".join(report["completion_blockers"])[:200], "moved": False}
    report = supervisor._run_goal(k, goal_id, make_adapter(driver), 1, timeout_s, 8)
    last = report["steps"][-1] if report["steps"] else {"status": "NOTHING"}
    out: dict[str, Any] = {"step": {"goal_id": goal_id, "driver": driver, "status": last.get("status"), "done": report["done"],
                                    "reason": last.get("reason") or last.get("blockers")}, "moved": False}
    if last.get("error_class") in RETRYABLE:
        # This AI cannot work right now; do not offer it to the other goals either.
        out["unavailable"] = {driver: f"{last['error_class']} during this run"}
        out["moved"] = True
    elif last.get("status") in ("DONE", "PENDING") or report["done"]:
        out["moved"] = True
    return out


def run(k: Kernel, drivers: list[str], make_adapter: Callable[[str], Adapter], *, max_steps: int = 30,
        timeout_s: float = 600, unavailable: dict[str, str] | None = None, parallel: int = 1) -> dict[str, Any]:
    """Advance every unfinished goal, one step each in turn, until nothing can move or the step
    limit is reached. One supervisor. With `parallel` > 1, up to that many goals have their step
    running at the same time, never two in the same project (see `_run_parallel`)."""
    if not 1 <= parallel <= MAX_PARALLEL:
        raise LupusError("PARALLEL_INVALID", f"1..{MAX_PARALLEL}")
    with k.supervisor_lock():
        recovered = supervisor._recover(k, False)
        steps: list[dict] = []
        unavailable = dict(unavailable or {})
        skipped: dict[str, str] = {}
        if recovered["writers_alive"]:
            return {"steps": [], "stopped": "WRITER_NOT_STOPPED", "writers_alive": recovered["writers_alive"]}
        if parallel > 1:
            _run_parallel(k, drivers, make_adapter, max_steps, timeout_s, unavailable, parallel, steps, skipped)
            return {"steps": steps, "skipped": skipped, "unavailable": unavailable, "portfolio": portfolio(k), "parallel": parallel}
        while len(steps) < max_steps:
            moved = False
            queue = k.q("SELECT goal_id FROM goal WHERE status IN ('ACTIVE','EXTERNAL_BLOCKED') "
                        "ORDER BY priority DESC, created_at, rowid")
            for row in queue:
                if len(steps) >= max_steps:
                    break
                out = _advance(k, row["goal_id"], drivers, make_adapter, timeout_s, unavailable)
                _note(out, row["goal_id"], steps, skipped, unavailable)
                moved = moved or out["moved"]
            if not moved:
                break
        return {"steps": steps, "skipped": skipped, "unavailable": unavailable, "portfolio": portfolio(k)}


def _note(out: dict, goal_id: str, steps: list[dict], skipped: dict[str, str], unavailable: dict[str, str]) -> None:
    if "step" in out:
        steps.append(out["step"])
        skipped.pop(goal_id, None)
    if "skip" in out:
        skipped[goal_id] = out["skip"]
    unavailable.update(out.get("unavailable", {}))


# ---------------------------------------------------------------- several goals at once

def _run_parallel(k: Kernel, drivers: list[str], make_adapter: Callable[[str], Adapter], max_steps: int, timeout_s: float,
                  unavailable: dict[str, str], parallel: int, steps: list[dict], skipped: dict[str, str]) -> None:
    """The same turns as the sequential loop, with up to `parallel` of them in flight.

    The scheduling is adapted from Ruflo's dual-mode orchestrator (MIT, Copyright (c) 2024-2026
    ruvnet; `v3/@claude-flow/codex/src/dual-mode/orchestrator.ts`): a bounded set of workers, and
    writers that run together must each have a working tree of their own. Ruflo runs fixed waves
    and keeps a separate cap for writers; here every step is a writer, a slot is refilled as soon
    as it frees, and "a tree of its own" is what the kernel already enforces: one writer per
    registered project, so goals of one repository run together only as isolated checkouts.

    What stays as it was: one supervisor (this process, holding the lock), one budget ledger,
    one completion rule. Each thread works through its own connection (`Kernel.fork`), and every
    claim, reservation and verdict is still a transaction of the single database.

    If this function is left by an exception (Ctrl-C, `lupus stop`, a failure in one thread),
    the threads are stopped the way a crash would stop them: their process groups are ended and
    they can record nothing further, so the next start's recovery closes what they left."""
    stop = Stop()
    results: queue.Queue = queue.Queue()
    flying: dict[str, tuple[str, threading.Thread]] = {}      # goal -> (project, thread)
    turns: dict[str, int] = {}                                # turns given in this run: the fewest goes first
    idle: set[str] = set()                                    # a turn changed nothing; asked again once something else has

    def work(goal_id: str, blocked: dict[str, str]) -> None:
        try:
            own = k.fork(stop)
            try:
                results.put((goal_id, _advance(own, goal_id, drivers, make_adapter, timeout_s, blocked), None))
            finally:
                own.close()
        except BaseException as exc:      # reported to the scheduler, which stops the others and re-raises it
            results.put((goal_id, None, exc))

    try:
        while True:
            if len(steps) + len(flying) < max_steps:
                busy = {project for project, _ in flying.values()}
                waiting = [row for row in k.q("SELECT goal_id, project_id FROM goal WHERE status IN ('ACTIVE','EXTERNAL_BLOCKED') "
                                              "ORDER BY priority DESC, created_at, rowid")
                           if row["goal_id"] not in flying and row["goal_id"] not in idle]
                for row in sorted(waiting, key=lambda r: turns.get(r["goal_id"], 0)):      # stable: the user's order within a count
                    if len(flying) >= parallel or len(steps) + len(flying) >= max_steps:
                        break
                    if row["project_id"] in busy:
                        continue          # its project already has a writer; it waits for that slot, as always
                    thread = threading.Thread(target=work, args=(row["goal_id"], dict(unavailable)), daemon=True)
                    flying[row["goal_id"]] = (row["project_id"], thread)
                    busy.add(row["project_id"])
                    turns[row["goal_id"]] = turns.get(row["goal_id"], 0) + 1
                    thread.start()
            if not flying:
                return
            try:
                goal_id, out, failure = results.get(timeout=1.0)
            except queue.Empty:
                continue
            flying.pop(goal_id)[1].join()
            if failure is not None:
                raise failure
            _note(out, goal_id, steps, skipped, unavailable)
            if out["moved"]:
                idle.clear()
            else:
                idle.add(goal_id)
    except BaseException:
        stop.set()
        _stop_flying(k, flying)
        raise


def _stop_flying(k: Kernel, flying: dict[str, tuple[str, threading.Thread]], patience_s: float = 60.0) -> None:
    """End what the threads are waiting on (a worker CLI, a verifier) until they have all returned.
    Only process groups this runtime recorded, and only while the recorded leader is still the
    same process."""
    projects_busy = sorted({project for project, _ in flying.values()})
    deadline = time.monotonic() + patience_s
    while any(thread.is_alive() for _, thread in flying.values()) and time.monotonic() < deadline:
        marks = ",".join("?" * len(projects_busy))
        rows = k.q(f"SELECT pid, proc_start, '' AS purpose FROM run WHERE project_id IN ({marks}) AND status <> 'STOPPED' "
                   f"AND pid IS NOT NULL UNION ALL SELECT pid, proc_start, purpose FROM aux_process WHERE project_id IN ({marks})",
                   *projects_busy, *projects_busy)
        for row in rows:
            if proc_start(row["pid"]) == row["proc_start"]:
                stop_group(row["pid"], grace_s=2.0)
            if runs.container_of(row["purpose"]):
                container_stop(runs.container_of(row["purpose"]))
        for _, thread in flying.values():
            thread.join(timeout=0.2)
