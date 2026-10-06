"""Model calls that are not project writers (a judge, a learning pass).

They run in an empty directory with no access to the project, are recorded before they start,
and what they report is charged to the budget like any other spend. The caller holds the
reservation for the call; this module records the call and charges the tokens it reports.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import adapters, budget, projects, runs
from .adapters import Adapter, AdapterResult
from .kernel import Kernel
from .util import LupusError, new_id

# Tests and fault injection can stand in for a native CLI here.
OVERRIDE: dict[str, Adapter] = {}


def adapter_for(driver: str) -> Adapter:
    """A reader, not a writer: no file tools at all on Claude; on Codex a profile that can write
    nowhere and read only the system minimum and its own empty directory."""
    if driver in OVERRIDE:
        return OVERRIDE[driver]
    if driver == "native_claude":
        return adapters.ClaudeAdapter(tools=())
    if driver == "native_codex":
        return adapters.CodexAdapter(effort="low", readonly=True)
    raise LupusError("DRIVER_UNKNOWN", driver)


def call(k: Kernel, *, project_id: str, goal_id: str | None, budget_id: str, purpose: str, driver: str, prompt: str,
         timeout_s: float = 300) -> AdapterResult:
    project = projects.check_root(k, project_id)
    if not projects.provider_allowed(project, driver):
        raise LupusError("PROVIDER_NOT_APPROVED", driver)
    unverified = projects.capability_blockers(k, driver)
    if unverified:
        raise LupusError("CAPABILITY_UNVERIFIED", ";".join(unverified))
    adapter = adapter_for(driver)
    call_id = new_id("svc")
    with k.tx():
        k.run("INSERT INTO service_call(call_id, project_id, goal_id, budget_id, purpose, driver, status, started_at) "
              "VALUES (?,?,?,?,?,?, 'RUNNING', ?)", call_id, project_id, goal_id, budget_id, purpose, driver, k.now())
    pids: list[int] = []

    def spawned(pid: int) -> None:
        runs.register_aux(k, project_id, pid, purpose)
        pids.append(pid)

    with tempfile.TemporaryDirectory(prefix="lupus-svc-") as tmp:
        try:
            result = adapters.execute(adapter, prompt, Path(os.path.realpath(tmp)), spawned, timeout_s)
        finally:
            for pid in pids:
                runs.clear_aux(k, pid)
    with k.tx():
        k.run("UPDATE service_call SET status = ?, usage = ?, error_class = ?, ended_at = ? WHERE call_id = ?",
              "FAILED" if result.error_class else "DONE", json.dumps(result.usage) if result.usage else None,
              result.error_class, k.now(), call_id)
        if result.usage:
            u = result.usage
            budget.charge_direct(k, budget_id, "tokens",
                                 u.get("tokens_in", 0) + u.get("tokens_cached", 0) + u.get("tokens_out", 0))
    return result


def close_interrupted(k: Kernel) -> int:
    """Calls a dead supervisor left open. Their reservation is settled in full by the usual
    recovery; here the record is closed so it does not look as if a call were still running."""
    with k.tx():
        n = k.one("SELECT COUNT(*) FROM service_call WHERE status = 'RUNNING'")[0]
        k.run("UPDATE service_call SET status = 'FAILED', error_class = 'interrupted', ended_at = ? WHERE status = 'RUNNING'",
              k.now())
    return n


def totals(k: Kernel, goal_id: str) -> dict:
    out = {"calls": 0, "tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "not_observed": 0}
    for row in k.q("SELECT usage FROM service_call WHERE goal_id = ?", goal_id):
        out["calls"] += 1
        if row["usage"] is None:
            out["not_observed"] += 1
            continue
        for key, value in json.loads(row["usage"]).items():
            if key in out and key != "calls":
                out[key] += int(value)
    return out
