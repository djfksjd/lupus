"""Exclusive wall durations; operational timestamps cannot separate nested checks and I/O."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .kernel import SupervisorStopping

STAGES = ("model_execution", "workspace", "baseline_red", "verification", "trial", "acceptance", "supervisor_bookkeeping")
_current = ContextVar("lupus_timing", default=None)


@contextmanager
def span(stage: str):
    state = _current.get()
    if state is None:
        yield
        return
    started = time.perf_counter_ns()
    frame = [0]
    state["stack"].append(frame)
    try:
        yield
    finally:
        elapsed = time.perf_counter_ns() - started
        state["stack"].pop()
        state["stages"][stage] += elapsed - frame[0]
        if state["stack"]:
            state["stack"][-1][0] += elapsed


def measured(stage: str):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def operation(stage: str, *, result_goal: bool = False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(k, *args, **kwargs):
            if _current.get() is not None:
                with span(stage):
                    return fn(k, *args, **kwargs)
            state = {"stack": [], "stages": dict.fromkeys(STAGES, 0)}
            token = _current.set(state)
            start_ns = time.time_ns()
            started = time.perf_counter_ns()
            result = None
            record = True
            try:
                with span(stage):
                    result = fn(k, *args, **kwargs)
                return result
            except BaseException as exc:
                # A crash or shutdown (non-Exception BaseException) gets no timing event,
                # just as a real process crash could not write one.
                record = isinstance(exc, Exception)
                raise
            finally:
                _current.reset(token)
                if record:
                    try:
                        elapsed_ns = time.perf_counter_ns() - started
                        # Attribute the outer span's entry/exit overhead to its own stage.
                        state["stages"][stage] += elapsed_ns - sum(state["stages"].values())
                        # Kernel.tx() joins an open transaction; timing must never join one
                        # the caller left open, including a raw SQLite transaction.
                        if not k._depth and not k.conn.in_transaction:
                            goal_id = (result or {}).get("goal_id") if result_goal else (args[0] if args else kwargs.get("goal_id"))
                            aggregate = "goal" if goal_id else "project"
                            aggregate_id = goal_id or (args[0]["project_id"] if args and isinstance(args[0], dict) else "unknown")
                            with k.tx():
                                k.emit("supervisor", "timing.operation", aggregate, aggregate_id, operation=fn.__name__,
                                       start_ns=start_ns, end_ns=start_ns + elapsed_ns, elapsed_ns=elapsed_ns,
                                       stages_ns=state["stages"], failed=result is None)
                    except (Exception, SupervisorStopping):
                        # Best effort, including write refusal during supervisor shutdown.
                        # KeyboardInterrupt and SystemExit must still reach the caller.
                        pass
        return wrapped
    return decorate
