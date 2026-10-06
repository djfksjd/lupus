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
from typing import Any, Callable

from . import budget, goals, projects, supervisor
from .adapters import Adapter
from .kernel import Kernel
from .util import LupusError, new_id

DRIVERS = ("native_claude", "native_codex")
RETRYABLE = ("quota", "rate_limit", "auth", "unavailable")


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


def run(k: Kernel, drivers: list[str], make_adapter: Callable[[str], Adapter], *, max_steps: int = 30,
        timeout_s: float = 600, unavailable: dict[str, str] | None = None) -> dict[str, Any]:
    """Advance every unfinished goal, one step each in turn, until nothing can move or the step
    limit is reached. One supervisor, one worker at a time."""
    with k.supervisor_lock():
        recovered = supervisor._recover(k, False)
        steps: list[dict] = []
        unavailable = dict(unavailable or {})
        skipped: dict[str, str] = {}
        if recovered["writers_alive"]:
            return {"steps": [], "stopped": "WRITER_NOT_STOPPED", "writers_alive": recovered["writers_alive"]}
        while len(steps) < max_steps:
            moved = False
            queue = k.q("SELECT goal_id FROM goal WHERE status IN ('ACTIVE','EXTERNAL_BLOCKED') "
                        "ORDER BY priority DESC, created_at, rowid")
            for row in queue:
                if len(steps) >= max_steps:
                    break
                goal = goals.get(k, row["goal_id"])
                driver, why = route(k, goal, drivers, unavailable)
                if driver is None:
                    skipped[goal["goal_id"]] = "no usable AI: " + json.dumps(why, ensure_ascii=False)
                    continue
                if goal["status"] == "EXTERNAL_BLOCKED" and not _unblock(k, goal["goal_id"], driver):
                    skipped[goal["goal_id"]] = "blocked on its provider and no other approved AI is available"
                    continue
                if goals.get(k, goal["goal_id"])["status"] != "ACTIVE":
                    continue
                if goals.next_runnable(k, goal["goal_id"]) is None:
                    report = supervisor._run_goal(k, goal["goal_id"], make_adapter(driver), 0, timeout_s, 8)   # completion only
                    if report["done"]:
                        steps.append({"goal_id": goal["goal_id"], "driver": None, "status": "DONE", "done": True})
                        moved = True
                    else:
                        skipped[goal["goal_id"]] = "waiting: " + ";".join(report["completion_blockers"])[:200]
                    continue
                report = supervisor._run_goal(k, goal["goal_id"], make_adapter(driver), 1, timeout_s, 8)
                last = report["steps"][-1] if report["steps"] else {"status": "NOTHING"}
                step = {"goal_id": goal["goal_id"], "driver": driver, "status": last.get("status"),
                        "done": report["done"], "reason": last.get("reason") or last.get("blockers")}
                steps.append(step)
                skipped.pop(goal["goal_id"], None)
                if last.get("error_class") in RETRYABLE:
                    # This AI cannot work right now; do not offer it to the other goals either.
                    unavailable[driver] = f"{last['error_class']} during this run"
                    moved = True
                elif last.get("status") in ("DONE", "PENDING") or report["done"]:
                    moved = True
            if not moved:
                break
        return {"steps": steps, "skipped": skipped, "unavailable": unavailable, "portfolio": portfolio(k)}
