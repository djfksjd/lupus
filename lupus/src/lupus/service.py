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
from .util import PROTECTED_STATE, LupusError, timeout_ms, new_id, sandbox_available

# Tests and fault injection can stand in for a native CLI here.
OVERRIDE: dict[str, Adapter] = {}
API_AUTHOR_MAX_ROUNDS = 8


class ReviewResultError(LupusError):
    """The owned reviewer stopped, but result recording or cleanup remains unconfirmed."""
    def __init__(self,code,result,recorded=False):
        super().__init__(code,'review result retained; recovery/recording is required')
        self.result=result
        self.recorded=recorded


def _observe_review(k,call_id,budget_id,result,*,purpose="review"):
    info=adapters.usage_info(result.usage,result.raw)
    values=dict(result.usage or {})
    values.update(_coverage=info['fields'],_token_lower_bound=info['known_token_lower_bound'])
    with k.tx():
        if k.one("SELECT 1 FROM event WHERE type='service.observed' AND aggregate_id=?",call_id):return
        k.run('UPDATE service_call SET usage=? WHERE call_id=?',json.dumps(values),call_id)
        budget.charge_direct(k,budget_id,'tokens',info['known_token_lower_bound'])
        k.emit('supervisor','service.observed','service_call',call_id,purpose=purpose,duration_ms=result.duration_ms,
               known_token_lower_bound=info['known_token_lower_bound'],usage_complete=info['complete'],
               process_lifetime=adapters.lifetime_diagnostics(result))

def call_limit(driver: str, *, author: bool = False) -> int:
    return API_AUTHOR_MAX_ROUNDS if author and driver.startswith("api:") else 1


def adapter_for(driver: str, *, home=None, model=None, effort=None) -> Adapter:
    """A reader, not a writer: no file tools at all on Claude; on Codex a profile that can write
    nowhere and read only the system minimum and its own empty directory."""
    if driver in OVERRIDE:
        return OVERRIDE[driver]
    if driver == "native_claude":
        return adapters.ClaudeAdapter(tools=(), **({"model":model} if model is not None else {}), **({'effort':effort} if effort is not None else {}))
    if driver == "native_codex":
        return adapters.CodexAdapter(effort=effort if effort is not None else "low", readonly=True, **({"model":model} if model is not None else {}))
    return adapters.APIAdapter(driver, model=model, effort=effort, home=home, readonly=True, tools=False, max_rounds=1)


def author_adapter(driver: str, workspace: Path, exclude: tuple[str, ...] = (), *, home=None, model=None) -> Adapter:
    """The exact author adapter used by cross-checking and the live canary harness."""
    if driver in OVERRIDE:
        return OVERRIDE[driver]
    if driver == "native_claude":
        if not sandbox_available():
            raise LupusError("CROSSCHECK_SANDBOX_UNAVAILABLE", "authoring requires the OS sandbox")
        adapter = adapters.ClaudeAdapter(**({"model":model} if model is not None else {}))
        adapter.shared_tmp = False
        adapter.require_os_sandbox = True
        adapter.read_exclude = exclude
    elif driver == "native_codex":
        adapter = adapters.CodexAdapter(shared_tmp=False, **({"model":model} if model is not None else {}))
        adapter.read_exclude = tuple(sorted(set(exclude) | PROTECTED_STATE))
    else:
        from .crosscheck import CROSSCHECK_DIR
        adapter = adapters.APIAdapter(driver, model=model, home=home, shell=False, max_rounds=API_AUTHOR_MAX_ROUNDS, write_paths=(CROSSCHECK_DIR,))
        adapter.shared_tmp = False
        adapter.read_exclude = tuple(sorted(set(exclude) | PROTECTED_STATE))
    # Redirect cooperative temporary files into the workspace. Claude also needs its
    # fixed per-user scratch directory, independently of TMPDIR (see the OS profile).
    tmp = workspace / ".lupus-author-tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    adapter.private_tmp = tmp.resolve()
    return adapter


def preflight_review(k,project_id,goal_id,driver,*,model=None,effort=None,binding=None):
    from . import review
    project=projects.check_root(k,project_id)
    review.check_authority(k,binding or review.authority(k,goal_id))
    if not projects.provider_allowed(project,driver):raise LupusError('PROVIDER_NOT_APPROVED',driver)
    blocked=projects.capability_blockers(k,driver)
    if blocked:raise LupusError('CAPABILITY_UNVERIFIED',';'.join(blocked))
    options={'home':k.home} if driver.startswith('api:') else {}
    if model is not None:options['model']=model
    if effort is not None:options['effort']=effort
    adapter=adapter_for(driver,**options)
    if isinstance(adapter,adapters.APIAdapter) and adapter.spec.api_key_env and not os.environ.get(adapter.spec.api_key_env):
        raise LupusError('API_AUTH_MISSING','set the selected reviewer key environment variable')
    if driver in ('native_codex','native_claude') and driver not in OVERRIDE and not sandbox_available():
        raise LupusError('REVIEW_SANDBOX_UNAVAILABLE','review requires the OS sandbox')


def call(k: Kernel, *, project_id: str, goal_id: str | None, budget_id: str, purpose: str, driver: str, prompt: str,
         timeout_s: float = 300, workspace: Path | None = None, author_exclude: tuple[str, ...] = (), model: str | None = None, effort: str | None = None, owner_run: tuple[str,int] | None = None, review_authority: dict | None = None) -> AdapterResult:
    timeout_ms(timeout_s)
    phase={} if purpose=='review' else None
    try:
        return _call(k,project_id=project_id,goal_id=goal_id,budget_id=budget_id,purpose=purpose,driver=driver,prompt=prompt,
                     timeout_s=timeout_s,workspace=workspace,author_exclude=author_exclude,model=model,effort=effort,
                     owner_run=owner_run,review_authority=review_authority,_review_phase=phase)
    except BaseException as exc:
        if phase is not None:
            actual=phase.get('result')
            if actual is not None:
                # Storage recovery only: the same call-id observation is atomic and
                # idempotent, so an ambiguous commit never charges tokens twice.
                try:
                    _observe_review(k,phase['call_id'],budget_id,actual)
                    phase['usage_recorded']=True
                    with k.tx():
                        k.run("UPDATE service_call SET status='FAILED',error_class=?,ended_at=? WHERE call_id=?",
                              'recording_required' if isinstance(exc,Exception) else 'interrupted',k.now(),phase['call_id'])
                except Exception:pass
                except BaseException as salvage:
                    if isinstance(exc,Exception):
                        salvage.review_result=actual
                        salvage.review_recorded=bool(phase.get('usage_recorded') or getattr(exc,'recorded',False))
                        salvage.review_known_not_dispatched=False
                        raise
                    # The first cancellation remains authoritative if already interrupted.
                if isinstance(exc,Exception):
                    if isinstance(exc,ReviewResultError):
                        exc.recorded=bool(phase.get('usage_recorded') or exc.recorded)
                    else:
                        raise ReviewResultError('REVIEW_RECORDING_FAILED',actual,bool(phase.get('usage_recorded'))) from exc
                else:
                    exc.review_result=actual
                    exc.review_recorded=bool(phase.get('usage_recorded'))
            known=actual is None and not phase.get('authorized') and not phase.get('observed')
            exc.review_known_not_dispatched=known
            if known and phase.get('call_id'):
                try:
                    with k.tx():
                        values={name:0 for name in (*adapters.TOKEN_FIELDS,'calls')}
                        values.update(_coverage={name:True for name in (*adapters.TOKEN_FIELDS,'calls')},_token_lower_bound=0)
                        k.run('UPDATE service_call SET usage=? WHERE call_id=?',json.dumps(values),phase['call_id'])
                        k.emit('supervisor','service.not_dispatched','service_call',phase['call_id'],reason='exec_gate_not_released')
                except Exception:pass
        raise


def _call(k: Kernel, *, project_id: str, goal_id: str | None, budget_id: str, purpose: str, driver: str, prompt: str,
         timeout_s: float = 300, workspace: Path | None = None, author_exclude: tuple[str, ...] = (), model: str | None = None, effort: str | None = None, owner_run: tuple[str,int] | None = None, review_authority: dict | None = None, _review_phase=None) -> AdapterResult:
    project = projects.check_root(k, project_id)
    if purpose=='review':
        from . import review
        if goal_id is None:raise LupusError('SERVICE_OWNER_SCOPE','review requires its finished goal')
        review_authority=review_authority or review.authority(k,goal_id)
        if review_authority.get('goal_id')!=goal_id or review_authority.get('project_id')!=project_id:
            raise LupusError('SERVICE_OWNER_SCOPE','review authority belongs to another goal/project')
        review.check_authority(k,review_authority)
    if not projects.provider_allowed(project, driver):
        raise LupusError("PROVIDER_NOT_APPROVED", driver)
    unverified = projects.capability_blockers(k, driver)
    if unverified:
        raise LupusError("CAPABILITY_UNVERIFIED", ";".join(unverified))
    if workspace is not None:
        work, root = workspace.resolve(), Path(project["canonical_root"]).resolve()
        if work.is_relative_to(root) or root.is_relative_to(work):
            raise LupusError("CROSSCHECK_WORKSPACE_INVALID", "authoring requires a copy outside the project")
    api_options = {"home": k.home} if driver.startswith("api:") else {}
    if model is not None:api_options["model"] = model
    if effort is not None:
        adapters.validate_effort(driver,effort)
        if workspace is not None:raise LupusError('EFFORT_UNSUPPORTED','author effort mapping is not configured')
        api_options['effort']=effort
    adapter = adapter_for(driver, **api_options) if workspace is None else author_adapter(driver, workspace, (project["canonical_root"], *author_exclude), **api_options)
    if purpose in ('supervisor','review') and driver not in OVERRIDE:
        adapter.readonly=True
        adapter.shared_tmp=False
        adapter.read_exclude=tuple(sorted(set(adapter.read_exclude)|PROTECTED_STATE|{project['canonical_root'],str(k.home.resolve())}))
        if driver in ('native_codex','native_claude'):
            adapter.os_sandbox='codex' if driver=='native_codex' else 'claude'
            adapter.require_os_sandbox=True
    call_id = new_id("svc")
    with k.tx():
        k.run("INSERT INTO service_call(call_id, project_id, goal_id, budget_id, purpose, driver, status, started_at) "
              "VALUES (?,?,?,?,?,?, 'RUNNING', ?)", call_id, project_id, goal_id, budget_id, purpose, driver, k.now())
        k.emit('supervisor','service.requested','service_call',call_id,driver=driver,purpose=purpose,requested_model=getattr(adapter,'model',model),requested_effort=effort,effective_backend_confirmed=False,max_http_requests=getattr(adapter,'max_rounds',None))
    if _review_phase is not None:_review_phase['call_id']=call_id
    pids: list[int] = []
    api_interrupted = False
    result=None;observed_review=False

    def spawned(pid: int) -> None:
        if purpose=='review':
            with k.tx():
                review.check_authority(k,review_authority)
                current=projects.check_root(k,project_id)
                if not projects.provider_allowed(current,driver):raise LupusError('PROVIDER_NOT_APPROVED',driver)
                blocked=projects.capability_blockers(k,driver)
                if blocked:raise LupusError('CAPABILITY_UNVERIFIED',';'.join(blocked))
                runs.register_aux(k,project_id,pid,purpose)
        elif owner_run is None:
            runs.register_aux(k, project_id, pid, purpose)
        else:
            with k.tx():
                owner=runs.guard(k,*owner_run)
                if owner['project_id']!=project_id or owner['goal_id']!=goal_id:
                    raise LupusError('SERVICE_OWNER_SCOPE','service must belong to its owning run')
                current=projects.check_root(k,project_id)
                if not projects.provider_allowed(current,driver):raise LupusError('PROVIDER_NOT_APPROVED',driver)
                blocked=projects.capability_blockers(k,driver)
                if blocked:raise LupusError('CAPABILITY_UNVERIFIED',';'.join(blocked))
                runs.register_aux(k, project_id, pid, purpose)
        pids.append(pid)
        if _review_phase is not None:_review_phase['authorized']=True

    carried = None
    original_cleanup_error = None
    with (tempfile.TemporaryDirectory(prefix="lupus-svc-") if workspace is None else nullcontext(str(workspace))) as tmp:
        try:
            result = adapters.execute(adapter, prompt, Path(os.path.realpath(tmp)), spawned, timeout_s)
            if _review_phase is not None:_review_phase.update(observed=True,result=result)
            if purpose=='review':
                result.raw['service_call_id']=call_id
                try:
                    _observe_review(k,call_id,budget_id,result);observed_review=True
                    if _review_phase is not None:_review_phase['usage_recorded']=True
                except Exception as exc:raise ReviewResultError('REVIEW_RECORDING_FAILED',result) from exc
        except BaseException as exc:
            carried = adapters.cleanup_result(exc)
            if carried is not None:
                original_cleanup_error = exc
                result = carried
                result.raw['service_call_id'] = call_id
                try:
                    _observe_review(k,call_id,budget_id,result,purpose=purpose)
                    observed_review = True
                    if _review_phase is not None:_review_phase['usage_recorded'] = True
                except BaseException as storage_error:
                    if not isinstance(storage_error,Exception) and isinstance(exc,Exception):
                        storage_error.worker_result = result
                        raise
                    exc.add_note('Cleanup usage persistence failed: '+type(storage_error).__name__)
            if _review_phase is not None and result is not None:_review_phase.update(observed=True,result=result)
            if carried is None and not isinstance(exc, Exception) and not driver.startswith("api:") and purpose not in ('supervisor','review'):
                raise
            api_interrupted = (carried is not None or driver.startswith("api:") or purpose in ('supervisor','review')) and not isinstance(exc, Exception)
            try:
                with k.tx():
                    k.run("UPDATE service_call SET status = 'FAILED', error_class = ?, ended_at = ? WHERE call_id = ?",
                          "exception" if isinstance(exc, Exception) else "interrupted", k.now(), call_id)
            except Exception as storage_error:
                if carried is not None:exc.add_note('Failed service status persistence: '+type(storage_error).__name__)
                elif isinstance(exc, Exception) and not isinstance(exc,ReviewResultError):raise
                # Recovery owns any row whose interruption update could not be persisted.
                # Never replace the user's cancellation with a database diagnostic.
            raise
        finally:
            for pid in pids:
                try:
                    runs.clear_aux(k, pid)
                except Exception as exc:
                    if not api_interrupted:
                        if purpose=='review' and result is not None:
                            try:
                                with k.tx():k.run("UPDATE service_call SET status='FAILED',error_class='cleanup_required',ended_at=? WHERE call_id=?",k.now(),call_id)
                            except Exception:pass
                            raise ReviewResultError('REVIEW_CLEANUP_REQUIRED',result,observed_review) from exc
                        if carried is not None:
                            raise adapters.WorkerCleanupError(result) from exc
                        raise
                    # Keep the row/fence for recovery and propagate the original interrupt.
                except BaseException as exc:
                    if carried is not None:
                        if not isinstance(original_cleanup_error,Exception):
                            original_cleanup_error.add_note('Aux cleanup interrupted: '+type(exc).__name__)
                            raise original_cleanup_error
                        exc.worker_result=result
                    if purpose=='review' and result is not None:
                        exc.review_result=result
                        try:
                            with k.tx():k.run("UPDATE service_call SET status='FAILED',error_class='interrupted',ended_at=? WHERE call_id=?",k.now(),call_id)
                        except Exception:pass
                    raise

    info = adapters.usage_info(result.usage, result.raw)
    stored = dict(result.usage or {})
    stored.update(_coverage=info['fields'], _token_lower_bound=info['known_token_lower_bound'])
    with k.tx():
        k.run("UPDATE service_call SET status = ?, usage = ?, error_class = ?, ended_at = ? WHERE call_id = ?",
              "FAILED" if result.error_class else "DONE", json.dumps(stored) if stored else None,
              result.error_class, k.now(), call_id)
        if not observed_review:budget.charge_direct(k, budget_id, 'tokens', info['known_token_lower_bound'])
        k.emit('supervisor','service.result','service_call',call_id,requested_model=getattr(adapter,'model',model),reported_models=(result.raw or {}).get('models',[]),requested_effort=effort,effective_backend_confirmed=False)
    return result


def close_interrupted(k: Kernel) -> int:
    """Calls a dead supervisor left open. Their reservation is settled in full by the usual
    recovery; here the record is closed so it does not look as if a call were still running."""
    with k.tx():
        n = k.one("SELECT COUNT(*) FROM service_call WHERE status = 'RUNNING'")[0]
        k.run("UPDATE service_call SET status = 'FAILED', error_class = 'interrupted', ended_at = ? WHERE status = 'RUNNING'",
              k.now())
    return n


def totals(k: Kernel, goal_id: str, *, purpose: str | None = None) -> dict:
    out = {'calls': 0, 'tokens_in': 0, 'tokens_cached': 0, 'tokens_out': 0, 'not_observed': 0}
    lower = 0
    for row in k.q('SELECT usage FROM service_call WHERE goal_id = ?'+(' AND purpose = ?' if purpose else " AND purpose NOT IN ('supervisor','review')"), goal_id, *([purpose] if purpose else [])):
        out['calls'] += 1  # registered host service invocations, including failures
        values = json.loads(row['usage']) if row['usage'] is not None else {}
        info = adapters.usage_info(values)
        lower += info['known_token_lower_bound']
        if not isinstance(values.get('_coverage'), dict) or not info['complete']:
            out['not_observed'] += 1
        for key in adapters.TOKEN_FIELDS:
            if adapters.valid_counter(values.get(key)):
                out[key] += values[key]
    return {**out, 'known_token_lower_bound': lower,
            'usage_complete': bool(out['calls']) and out['not_observed'] == 0,
            'complete_tokens': lower if out['calls'] and not out['not_observed'] else None}
