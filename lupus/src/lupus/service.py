"""Model calls that are not project writers (a judge, a learning pass).

Readers run in an empty directory; cross-check authors get a disposable pristine copy. Calls are
recorded before they start, and what they report is charged to the budget like any other spend. The caller holds the
reservation for the call; this module records the call and charges the tokens it reports.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path

from . import adapters, budget, projects, runs
from .adapters import Adapter, AdapterResult
from .kernel import Kernel
from .util import PROTECTED_STATE, LupusError, new_id, sandbox_available

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


def author_adapter(driver: str, workspace: Path, exclude: tuple[str, ...] = ()) -> Adapter:
    """The exact author adapter used by cross-checking and the live canary harness."""
    if driver in OVERRIDE:
        return OVERRIDE[driver]
    if driver == "native_claude":
        if not sandbox_available():
            raise LupusError("CROSSCHECK_SANDBOX_UNAVAILABLE", "authoring requires the OS sandbox")
        adapter = adapters.ClaudeAdapter()
        adapter.shared_tmp = False
        adapter.require_os_sandbox = True
        adapter.read_exclude = exclude
    elif driver == "native_codex":
        adapter = adapters.CodexAdapter(shared_tmp=False)
        adapter.read_exclude = tuple(sorted(set(exclude) | PROTECTED_STATE))
    else:
        raise LupusError("DRIVER_UNKNOWN", driver)
    # Redirect cooperative temporary files into the workspace. Claude also needs its
    # fixed per-user scratch directory, independently of TMPDIR (see the OS profile).
    tmp = workspace / ".lupus-author-tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    adapter.private_tmp = tmp.resolve()
    return adapter


def call(k: Kernel, *, project_id: str, goal_id: str | None, budget_id: str, purpose: str, driver: str, prompt: str,
         timeout_s: float = 300, workspace: Path | None = None, author_exclude: tuple[str, ...] = ()) -> AdapterResult:
    project = projects.check_root(k, project_id)
    if not projects.provider_allowed(project, driver):
        raise LupusError("PROVIDER_NOT_APPROVED", driver)
    unverified = projects.capability_blockers(k, driver)
    if unverified:
        raise LupusError("CAPABILITY_UNVERIFIED", ";".join(unverified))
    if workspace is not None:
        work, root = workspace.resolve(), Path(project["canonical_root"]).resolve()
        if work.is_relative_to(root) or root.is_relative_to(work):
            raise LupusError("CROSSCHECK_WORKSPACE_INVALID", "authoring requires a copy outside the project")
    adapter = adapter_for(driver) if workspace is None else author_adapter(driver, workspace, (project["canonical_root"], *author_exclude))
    call_id = new_id("svc")
    with k.tx():
        k.run("INSERT INTO service_call(call_id, project_id, goal_id, budget_id, purpose, driver, status, started_at) "
              "VALUES (?,?,?,?,?,?, 'RUNNING', ?)", call_id, project_id, goal_id, budget_id, purpose, driver, k.now())
    pids: list[int] = []

    def spawned(pid: int) -> None:
        runs.register_aux(k, project_id, pid, purpose)
        pids.append(pid)

    with (tempfile.TemporaryDirectory(prefix="lupus-svc-") if workspace is None else nullcontext(str(workspace))) as tmp:
        try:
            result = adapters.execute(adapter, prompt, Path(os.path.realpath(tmp)), spawned, timeout_s)
        except Exception:
            with k.tx():
                k.run("UPDATE service_call SET status = 'FAILED', error_class = 'exception', ended_at = ? WHERE call_id = ?",
                      k.now(), call_id)
            raise
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
