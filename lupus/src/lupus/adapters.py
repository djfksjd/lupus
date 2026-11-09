"""Execution drivers. Each adapter turns a task prompt into one worker process and reports
exit status and whatever usage the host actually exposes.

Native adapters call the installed `claude` / `codex` binaries with the user's existing
subscription login. They never read, copy or forward credentials, and never call a provider
endpoint directly (§10.3). API keys and provider overrides inherited from the environment are
removed so a native run cannot silently bill an API account (AUTH-01, COST-02).

Explicit APIAdapter profiles are separate: the selected public endpoint and environment-key
reference opt into that API. No native-to-API or cross-provider fallback occurs. The trusted
local worker owns bounded workspace tools and HTTP rounds; credentials are never put in argv.

Flags below were measured on this machine by `lupus probe` (Claude Code 2.1.289,
codex-cli 0.160.0); re-run the probe after a CLI upgrade.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import timing, process_lifetime
from .util import (
    GATE, GATE_EXEC_FAILED, SANDBOX_EXEC, LupusError, timeout_ms, crash_point, group_alive, safe_path, sandbox_available,
    sandbox_profile_worker, scrubbed_env, stop_group, diagnostic_tail, active_python_env, active_python_read_roots, gate_argv,
)

MAX_OUTPUT_BYTES = 20 * 1024 * 1024    # a runaway worker must not exhaust the supervisor's memory
MAX_REPLY = 32_000      # characters of a worker's final message that are kept (lessons, the description of a drafted test)


def worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    selected = dict(extra or {})
    if python_env := active_python_env():
        selected.setdefault("VIRTUAL_ENV", python_env)
        selected.setdefault("PATH", os.path.join(python_env, "bin") + os.pathsep + safe_path())
    return scrubbed_env(selected)


def resolve_cli(name: str, project: Path | None = None) -> str:
    """Absolute path of a CLI found on the sanitized PATH, never a file inside the project: PATH
    entries that are relative, empty, or point into the project directory are skipped."""
    root = os.path.realpath(project) if project is not None else None
    inside = lambda p: root is not None and (os.path.realpath(p) == root or os.path.realpath(p).startswith(root + os.sep))
    entries = [e for e in safe_path().split(os.pathsep) if not inside(e)]
    found = shutil.which(name, path=os.pathsep.join(entries) or "/usr/bin:/bin")
    if found is None or inside(found):
        # Fail closed: a bare name would be looked up again at exec time, possibly in the project.
        # A path that cannot exist makes the exec gate report "unavailable" instead.
        return f"/nonexistent/lupus-cli-not-found/{name}"
    return found


@dataclass
class AdapterResult:
    exit_code: int | None
    text: str = ""
    usage: dict[str, int] | None = None     # None = the host reported nothing
    session_id: str = ""
    error_class: str | None = None          # quota | rate_limit | auth | timeout | crash | None
    duration_ms: int = 0
    leftover_processes: bool = False        # children that outlived the worker and were killed
    raw: dict = field(default_factory=dict)
    stderr_tail: str = ""                   # bounded, inert, credential-screened process diagnostic


TOKEN_FIELDS = ('tokens_in', 'tokens_cached', 'tokens_out')


def valid_counter(value):
    return type(value) is int and value >= 0


def usage_info(usage, raw=None):
    """Known numeric lowerbounds are separate from complete token/turn measurements."""
    usage, raw = usage or {}, raw or {}
    declared = raw.get('usage_coverage', usage.get('_coverage'))
    fields = {key: valid_counter(usage.get(key)) and
              (declared is None or isinstance(declared, dict) and declared.get(key) is True)
              for key in (*TOKEN_FIELDS, 'calls')}
    if raw.get('stdout_truncated') is True:
        fields = dict.fromkeys(fields, False)
    known = sum(usage.get(key, 0) for key in TOKEN_FIELDS if valid_counter(usage.get(key)))
    lower = raw.get('known_token_lower_bound', usage.get('_token_lower_bound'))
    lower = max(known, lower) if valid_counter(lower) else known
    complete = all(fields[key] for key in TOKEN_FIELDS)
    return {'fields': fields, 'complete': complete, 'tokens': known if complete else None,
            'turns': usage.get('calls') if fields['calls'] else None, 'known_token_lower_bound': lower}


def _counter(mapping, key):
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if valid_counter(value) else None


def _claude_usage(data, failed):
    u = data.get('usage')
    incoming, creation = _counter(u, 'input_tokens'), _counter(u, 'cache_creation_input_tokens')
    cached, output, turns = _counter(u, 'cache_read_input_tokens'), _counter(u, 'output_tokens'), _counter(data, 'num_turns')
    usage = {}
    if incoming is not None or creation is not None:
        usage['tokens_in'] = (incoming or 0) + (creation or 0)
    for key, value in (('tokens_cached', cached), ('tokens_out', output), ('calls', turns)):
        if value is not None:
            usage[key] = value
    complete = {'tokens_in': incoming is not None and creation is not None,
                'tokens_cached': cached is not None, 'tokens_out': output is not None, 'calls': turns is not None}
    if failed:
        complete = dict.fromkeys(complete, False)
    return usage or None, complete, sum(usage.get(k, 0) for k in TOKEN_FIELDS)


class Adapter:
    driver = "abstract"
    auth_mode = "none"
    variant = "default"     # model / effort tier; part of an attempt's identity
    batch = False           # True when one call has a large fixed input, so fewer calls is cheaper
    interactive = False     # True: the user works in the CLI's own screen (see execute_interactive)
    shared_tmp = True
    require_os_sandbox = False
    private_tmp: Path | None = None
    read_exclude: tuple[str, ...] = ()
    os_sandbox: str | None = None   # name of an OS sandbox profile to run the CLI under (util.sandbox_profile_worker)

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        raise NotImplementedError

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return worker_env()


class WorkerCleanupError(LupusError):
    """Failed ownership cleanup with bounded observed result; never a successful return."""
    def __init__(self, result):
        self.result = self.worker_result = result
        self.executed = True
        super().__init__("WORKER_CLEANUP_FAILED", "worker group cleanup was not certified; observed usage is retained")


def cleanup_result(exc):
    """Trust only our typed failure or an interrupted adapter's explicit result carrier."""
    value = exc.result if isinstance(exc, WorkerCleanupError) else (getattr(exc, 'worker_result', None)
            if isinstance(exc, BaseException) and not isinstance(exc, Exception) else None)
    if not isinstance(value, AdapterResult) or not isinstance(value.raw, dict):return None
    state = value.raw.get('process_lifetime')
    return value if isinstance(state, dict) and type(state.get('cleanup_complete')) is bool else None


def lifetime_diagnostics(result):
    """Allowlisted historical diagnostics only; never ownership or signal authority."""
    if not isinstance(result, AdapterResult) or not isinstance(result.raw, dict):return None
    state = result.raw.get("process_lifetime")
    if not isinstance(state, dict):return None
    return {**{name: process_lifetime._payload_pid(state.get(name))
               for name in ("anchor_pid", "payload_pid", "monitor_pid")},
            **{name: state.get(name) if type(state.get(name)) is bool else None
               for name in ("cleanup_complete", "fallback_group_gone")},
            "scope": "anchored process group only; no setsid/container guarantee"}


@timing.measured("model_execution")
def execute(
    adapter: Adapter,
    prompt: str,
    cwd: Path,
    on_spawn: Callable[[int], None],
    timeout_s: float,
) -> AdapterResult:
    """Run one worker in its own process group behind the exec gate.

    `on_spawn(pid)` must durably record the pid; the worker starts only after it returns.
    After the worker exits, anything still alive in its group is killed and reported, so a
    finished run leaves no writer behind (STOP-01).
    """
    timeout_ms(timeout_s)
    if adapter.interactive:
        return execute_interactive(adapter, prompt, cwd, on_spawn, timeout_s)
    started = time.monotonic()
    if isinstance(adapter, APIAdapter):
        adapter.timeout_s = timeout_s
    primary_instructions = getattr(adapter, 'primary_instructions', None)
    if primary_instructions:
        prompt = "Selected skill instructions (within this request and existing permissions):\n" + primary_instructions + "\n\n" + prompt
    argv = adapter.argv(prompt, cwd)
    confined = bool(adapter.os_sandbox) and sandbox_available()
    if adapter.require_os_sandbox and not confined:
        raise LupusError("CROSSCHECK_SANDBOX_UNAVAILABLE", "author OS sandbox is unavailable")
    if confined:
        # Enforced by the OS, whatever the CLI's own permission system decides.
        configured_options = {"private_tmp": adapter.private_tmp} if getattr(adapter, "configured_native", False) else {}
        argv = [SANDBOX_EXEC, "-p", sandbox_profile_worker(cwd, adapter.os_sandbox, adapter.read_exclude,
                adapter.shared_tmp, readonly=getattr(adapter, "readonly", False), **configured_options), *argv]
    env = adapter.env()
    if adapter.private_tmp is not None:
        env["TMPDIR"] = str(adapter.private_tmp)
        env["TMP"] = str(adapter.private_tmp)
        env["TEMP"] = str(adapter.private_tmp)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = process_lifetime.spawn(
            argv,
            cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err,
            start_new_session=True, env=env,
        )
        timed_out = False
        try:
            crash_point("adapter.after_spawn")
            on_spawn(proc.pid)
            crash_point("adapter.before_go")
            proc.stdin.write(b"g")
            proc.stdin.close()
            _wait_showing_progress(proc, timeout_s, adapter.driver)
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException as exc:
            try:process_lifetime.finish(proc, cancel=True)
            except BaseException as cleanup_error:
                exc.add_note("Worker lifetime cleanup failed: " + type(cleanup_error).__name__)
            if isinstance(exc, OSError):   # the gate died before it could be released
                raise LupusError("WORKER_SPAWN_FAILED", str(exc)) from exc
            raise
        finally:
            # Registration can fail before the gate is released. The stopped child
            # is reaped above; close its parent-side pipe on that path as well.
            if proc.stdin is not None and not proc.stdin.closed:
                try:proc.stdin.close()
                except OSError:pass
        cleanup_error = None
        try:
            lifetime = process_lifetime.finish(proc, cancel=timed_out)
        except BaseException as exc:
            cleanup_error = exc
            lifetime = proc._lupus_lifetime.cleanup or {}
        cleanup_confirmed = lifetime.get("complete") is True
        leftover = bool(lifetime.get("leftover", False)) if not timed_out else False
        target_code = lifetime.get("exit_code", proc._lupus_lifetime.actual)
        stdout_bytes = os.fstat(out.fileno()).st_size
        stderr_bytes = os.fstat(err.fileno()).st_size
        out.seek(0)
        err.seek(0, os.SEEK_END)
        err.seek(max(0, err.tell() - MAX_OUTPUT_BYTES))
        stderr = err.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
        streamed = cleanup_confirmed and stdout_bytes > MAX_OUTPUT_BYTES and _codex_stream_capable(adapter)
        if streamed:
            result = adapter.parse_stream(None if timed_out else target_code, out, stderr)
            stdout_complete = result.raw.get('stdout_parse_complete') is True
        else:
            stdout = out.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
            result = adapter.parse(None if timed_out else target_code, stdout, stderr)
            stdout_complete = cleanup_confirmed and stdout_bytes <= MAX_OUTPUT_BYTES
    if timed_out:
        result.error_class = "timeout"
    elif target_code == GATE_EXEC_FAILED:
        result.error_class = "unavailable"   # the CLI binary is missing or not executable
    if result.raw is None:
        result.raw = {}
    result.raw.update(stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
                      stdout_truncated=not stdout_complete,
                      stderr_truncated=not cleanup_confirmed or stderr_bytes > MAX_OUTPUT_BYTES,
                      stdout_aggregate_limit_exceeded=stdout_bytes > MAX_OUTPUT_BYTES,
                      stdout_capture_mode='cleanup_snapshot' if not cleanup_confirmed else ('codex_jsonl_frames' if streamed else 'legacy_prefix'),
                      stdout_transcript_retained_by_executor=False)
    result.stderr_tail = diagnostic_tail(stderr)
    if result.raw['stdout_truncated']:
        # Numeric prefix observations remain a lower bound, never a complete turn/cost report.
        lower = usage_info(result.usage, result.raw)['known_token_lower_bound']
        result.raw.update(usage_coverage=dict.fromkeys((*TOKEN_FIELDS, 'calls'), False),
                          known_token_lower_bound=lower, host_execute_calls=1)
        cost = result.raw.get('estimated_list_cost_usd')
        try:
            if type(cost) in (int, float) and cost >= 0 and math.isfinite(cost):
                result.raw['partial_estimated_list_cost_usd'] = cost
        except OverflowError:
            pass
        result.raw['estimated_list_cost_usd'] = None
        result.text = ''
        if result.error_class is None:
            result.error_class = 'capture_incomplete'
        result.stderr_tail = diagnostic_tail(result.stderr_tail +
            ('\nWorker ownership cleanup unconfirmed: bounded stdout snapshot is not final.' if not cleanup_confirmed else
             (f'\nWorker stdout capture incomplete: bounded JSONL processing stopped before a complete valid stream.'
              if streamed else f'\nWorker stdout capture incomplete: first {MAX_OUTPUT_BYTES} of {stdout_bytes} bytes retained.')))
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.leftover_processes = leftover
    result.raw["os_sandbox"] = confined
    result.raw["process_lifetime"] = {"anchor_pid": proc.pid, "payload_pid": proc._lupus_lifetime.payload_pid,
        "cleanup_complete": cleanup_confirmed, "monitor_pid": proc._lupus_lifetime.monitor.pid,
        "fallback_group_gone": getattr(proc._lupus_lifetime, "fallback_group_gone", None),
        "scope": "anchored process group only; no setsid/container guarantee"}
    if cleanup_error is not None:
        result.text = ''
        if result.error_class in (None, 'capture_incomplete'):result.error_class = 'cleanup_failed'
        if not isinstance(cleanup_error, Exception):
            cleanup_error.result = cleanup_error.worker_result = result
            raise cleanup_error
        raise WorkerCleanupError(result) from cleanup_error
    return result




def _wait_showing_progress(proc: subprocess.Popen, timeout_s: float, label: str) -> None:
    """Wait for the worker; at a terminal, show that something is happening and for how long."""
    if not sys.stderr.isatty() or threading.current_thread() is not threading.main_thread():
        process_lifetime.wait(proc, timeout=timeout_s)      # (several workers at once would overwrite each other's line)
        return
    started = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                raise subprocess.TimeoutExpired(proc.args, timeout_s)
            try:
                process_lifetime.wait(proc, timeout=min(1.0, timeout_s - elapsed))
                return
            except subprocess.TimeoutExpired:
                sys.stderr.write(f"\r  {label} 작업 중… {int(time.monotonic() - started)}초 ")
                sys.stderr.flush()
    finally:
        sys.stderr.write("\r" + " " * 48 + "\r")
        sys.stderr.flush()


def _interactive_error(pending, exc, stage):
    """Keep the first cancellation; ordinary cleanup errors never replace it."""
    kind = next((kind.__name__ for kind in (KeyboardInterrupt, SystemExit, OSError, RuntimeError, Exception)
                 if isinstance(exc, kind)), "BaseException")
    if pending is None:
        exc.add_note("Interactive ownership failure at " + stage + ": " + kind)
        return exc
    if isinstance(pending, Exception) and not isinstance(exc, Exception):
        exc.add_note("Earlier interactive failure at " + stage)
        return exc
    pending.add_note("Additional interactive failure at " + stage + ": " + kind)
    return pending


def execute_interactive(adapter: Adapter, prompt: str, cwd: Path, on_spawn: Callable[[int], None],
                        timeout_s: float) -> AdapterResult:
    """Hand the terminal to the CLI's own interactive screen, under the same rules as a headless
    worker: its process group is recorded before it may start (exec gate), it is the foreground
    job of this terminal while it runs, and the group is emptied before this returns.

    Nothing the user or the model types is seen or stored here, and the host reports no usage
    for such a session; the caller charges the reservation in full."""
    timeout_ms(timeout_s)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LupusError("USER_PRESENCE_REQUIRED", "an interactive session needs a terminal")
    tty = sys.stdin.fileno()
    saved = termios.tcgetattr(tty)
    read_fd = write_fd = None
    proc = None
    ignored = None
    pending = None
    lifetime = None
    timed_out = False
    started = time.monotonic()
    try:
        read_fd, write_fd = os.pipe()
        proc = process_lifetime.spawn(
            adapter.argv(prompt, cwd), cwd=cwd, pass_fds=(read_fd,), process_group=0,
            env={**adapter.env(), "LUPUS_GATE_FD": str(read_fd)},
        )
        # Detach before close: an exception may arrive after the OS closed it.
        # Never retry a numeric FD that could now belong to another thread.
        fd, read_fd = read_fd, None
        try:os.close(fd)
        except BaseException as exc:
            exc.add_note("Interactive read gate close unconfirmed; descriptor is not retried")
            raise
        try:
            ignored = signal.getsignal(signal.SIGTTOU)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            on_spawn(proc.pid)
            os.tcsetpgrp(tty, proc.pid)
            os.write(write_fd, b"g")
            process_lifetime.wait(proc, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            raise LupusError("WORKER_SPAWN_FAILED", str(exc)) from exc
    except BaseException as exc:
        pending = exc
    finally:
        for stage, fd in (("read_gate_close", read_fd), ("write_gate_close", write_fd)):
            # Every acquired descriptor is attempted once, even after another close fails.
            if fd is not None:
                try:os.close(fd)
                except BaseException as exc:
                    exc.add_note("Interactive gate close unconfirmed; descriptor is not retried")
                    pending = _interactive_error(pending, exc, stage)
        if proc is not None:
            try:
                lifetime = process_lifetime.finish(proc, cancel=timed_out or pending is not None)
                if not isinstance(lifetime, dict) or lifetime.get("complete") is not True:
                    raise LupusError("WORKER_CLEANUP_FAILED", "interactive group cleanup is not confirmed")
            except BaseException as exc:
                if isinstance(exc, Exception) and pending is None:
                    failure = LupusError("WORKER_CLEANUP_FAILED", "interactive group cleanup is not confirmed")
                    failure.__cause__ = exc
                    exc = failure
                pending = _interactive_error(pending, exc, "lifetime_finish")
            # Each restoration is independent: a foreground failure must not skip attributes.
            try:os.tcsetpgrp(tty, os.getpgrp())
            except BaseException as exc:pending = _interactive_error(pending, exc, "foreground_restore")
            try:termios.tcsetattr(tty, termios.TCSADRAIN, saved)
            except BaseException as exc:pending = _interactive_error(pending, exc, "attributes_restore")
            if ignored is not None:
                try:signal.signal(signal.SIGTTOU, ignored)
                except BaseException as exc:pending = _interactive_error(pending, exc, "signal_restore")
    if pending is not None:
        raise pending
    code = None if timed_out else lifetime["exit_code"]
    leftover = bool(lifetime["leftover"]) if not timed_out else False
    return AdapterResult(
        exit_code=code, usage=None, leftover_processes=leftover, duration_ms=int((time.monotonic() - started) * 1000),
        error_class="timeout" if timed_out else ("unavailable" if code == GATE_EXEC_FAILED else None),
        raw={"process_lifetime": {"anchor_pid": proc.pid, "payload_pid": proc._lupus_lifetime.payload_pid,
             "cleanup_complete": lifetime.get("complete") is True, "monitor_pid": proc._lupus_lifetime.monitor.pid,
             "scope": "anchored process group only; no setsid/container guarantee"}})


PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_API", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN_BEDROCK",
                         "CLAUDE_CODE_OAUTH_TOKEN", "ZAI_", "GLM_API_", "OLLAMA_API_", "LUPUS_API_", "OPENROUTER_", "DEEPSEEK_", "GEMINI_", "LMSTUDIO_")


class InteractiveAdapter(Adapter):
    """The user's own interactive `claude` / `codex`, with their normal configuration, started by
    Lupus so that the project's writer slot, the frozen verification files and the final
    verification apply to the session. Provider keys in the environment are still dropped, so a
    session cannot quietly bill an API account instead of the subscription."""

    interactive = True
    auth_mode = "subscription"

    def __init__(self, cli: str, extra: list[str] | None = None, notice: str = "", settings: dict | None = None):
        self.cli = cli
        self.driver = {"claude": "native_claude", "codex": "native_codex"}[cli]
        self.extra = list(extra or [])
        self.notice = notice
        self.settings = settings
        self.variant = "interactive"

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        argv = [resolve_cli(self.cli, cwd)]
        if self.cli == "claude":
            if self.settings:      # passed for this one process; no settings file of the user is touched
                argv += ["--settings", json.dumps(self.settings)]
            if self.notice:
                argv += ["--append-system-prompt", self.notice]
        else:
            argv += ["-C", str(cwd)]
        return argv + self.extra

    def env(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if not key.startswith(PROVIDER_ENV_PREFIXES)}


def classify_error(text: str) -> str | None:
    """Coarse classification of a failed call from the host's own message. Heuristic: the hosts
    do not expose a stable machine-readable quota signal, so an unrecognised failure stays
    'crash' and is handled conservatively."""
    low = text.lower()
    if any(s in low for s in ("usage limit", "quota", "limit reached", "out of credits", "limit will reset")) or re.search(
            r"\bhit (?:your |the )?(?:(?:daily|weekly|monthly|session) )?limit\b", low):
        return "quota"
    if any(s in low for s in ("rate limit", "429", "overloaded", "too many requests")):
        return "rate_limit"
    if any(s in low for s in ("not logged in", "unauthorized", "401", "login", "authentication")):
        return "auth"
    return None


class ClaudeAdapter(Adapter):
    """`claude -p` with the default profile's subscription login, user/project settings, hooks,
    plugins, MCP servers and skills switched off. `--bare` is not used: it does not read the
    subscription login."""

    driver = "native_claude"
    auth_mode = "subscription"
    os_sandbox = "claude"

    # Read-only search tools are included: in a repository of real size the worker has to find the
    # code before it can change it. No shell: checks are run by the supervisor, not by the worker.
    def __init__(self, model: str | None = None, tools: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep"),
                 web: bool = False, shell: bool = False, effort: str | None = None):
        self.model = model
        self.effort = effort
        self.tools = tools + (("WebSearch", "WebFetch") if web else ()) + (("Bash",) if shell else ())
        self.web = web
        self.shell = shell      # the user allowed the worker to run commands (inside the OS sandbox)
        self.variant = ((model or "default") + (f"/{effort}" if effort else "") + ("+web" if web else "")
                        + ("+shell" if shell else ""))

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        argv = [
            resolve_cli("claude", cwd), "-p", prompt,
            "--output-format", "json",
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--permission-mode", "acceptEdits",
            "--no-session-persistence",
            "--tools", *(self.tools or ("",)),
        ]
        allowed = (["WebSearch", "WebFetch"] if self.web else []) + (["Bash"] if self.shell else [])
        if allowed:      # asked for by the user: looking things up, or running commands to check its own work
            argv += ["--allowedTools", *allowed]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        return argv

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                data = next((d for d in reversed(data) if d.get("type") == "result"), {})
        except (json.JSONDecodeError, AttributeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        failed = exit_code != 0 or bool(data.get("is_error"))
        usage, coverage, lower = _claude_usage(data, failed)
        text = str(data.get("result", ""))
        return AdapterResult(
            exit_code=exit_code, text=text[-MAX_REPLY:], usage=usage, session_id=str(data.get("session_id", "")),
            error_class=(classify_error(text + stderr) or "crash") if failed else None,
            # total_cost_usd is the host's list-price estimate, not a charge on a subscription.
            raw={"usage_coverage": coverage, "known_token_lower_bound": lower, "host_execute_calls": 1,
                 "estimated_list_cost_usd": data.get("total_cost_usd"),
                 "models": sorted(data.get("modelUsage") or {}),
                 "permission_denials": len(data.get("permission_denials") or [])},
        )


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}={_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def codex_filesystem_profile(readonly: bool = False, shared_tmp: bool = True,
                             read_exclude: tuple[str, ...] = ()) -> dict:
    """What a Codex worker may touch: read the system minimum, the Codex program itself and the
    language toolchains; write only the project and the temp directory. The user's home is NOT
    readable, so keys, tokens and other projects stay out of reach. Measured with a canary by
    `lupus probe --live` (codex-cli 0.160.0: default sandbox read it, this profile did not)."""
    # `readonly` is for calls that only think (a judge): no explicit writable roots.
    # The CLI may add runtime temp reads; author exclusions need explicit denies.
    # `.git` stays read-only inside the writable project: a hook or config entry planted there would
    # run, unsandboxed, at the user's next git command (measured: without this entry it can be written).
    profile: dict = ({":minimal": "read", ":project_roots": {".": "read"}} if readonly
                     else {":minimal": "read", ":project_roots": {".": "write", ".git": "read"}, ":tmpdir": "write"})
    if not shared_tmp:
        profile.pop(":tmpdir", None)      # no explicit shared temp grant for authors
    cli = shutil.which("codex", path=safe_path())
    if cli:
        real = os.path.realpath(cli)
        packages = real.split("/packages/")[0] + "/packages" if "/packages/" in real else os.path.dirname(real)
        # The sandbox helper re-execs the lexical CLI path; its symlink parent must
        # be readable as well as the package collection used by the normal worker.
        # Keep these grants for authors, but never restore the shared :tmpdir grant.
        for path in (os.path.dirname(cli), packages):
            profile[path] = "read"
    for toolchain in () if readonly else ("/opt/homebrew", "/usr/local"):     # interpreters a worker may run when self-checking
        if os.path.isdir(toolchain):
            profile[toolchain] = "read"
    for python_root in active_python_read_roots():
        profile[python_root] = "read"
    # Codex accepts explicit `deny` entries (FileSystemAccessMode). Omitting :tmpdir
    # alone does not revoke the CLI-added /tmp, /private/tmp and /private/var/tmp
    # readable roots (observed in the installed policy builder). Deny protected
    # trees explicitly, including when nested inside an installation/runtime grant.
    for path in read_exclude:
        profile[os.path.realpath(path)] = "deny"
    return profile


class CodexAdapter(Adapter):
    """`codex exec` with the default profile's ChatGPT login and config.toml (hooks, MCP servers,
    plugins) ignored. A permission profile confines writes to the task directory and reads to the
    project plus what the CLI needs to run."""

    driver = "native_codex"
    auth_mode = "subscription"
    batch = True            # measured: ~30k tokens of fixed input per call
    shell = True            # Codex always has its own sandboxed shell

    def __init__(self, model: str | None = None, effort: str | None = None, readonly: bool = False, shared_tmp: bool = True):
        self.model = model
        self.effort = effort
        self.readonly = readonly
        self.shared_tmp = shared_tmp
        self.variant = f"{model or 'default'}/{effort or 'default'}"

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        argv = [
            resolve_cli("codex", cwd), "exec", "--json", "--color", "never",
            "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config", "--ignore-rules",
            # A named permission profile instead of `--sandbox workspace-write`: that mode lets
            # commands read every file the user can.
            "-c", 'default_permissions="lupus"',
            "-c", f"permissions.lupus.filesystem={_toml(codex_filesystem_profile(self.readonly, self.shared_tmp, self.read_exclude))}",
            "-C", str(cwd),
        ]
        if self.model:
            argv += ["-m", self.model]
        if self.effort:
            argv += ["-c", f'model_reasoning_effort="{self.effort}"']
        return argv + [prompt]

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        state = _CodexJSONL()
        for line in stdout.split('\n'):
            state.feed(line)
        return state.result(exit_code, stderr)

    def parse_stream(self, exit_code: int | None, stream, stderr: str) -> AdapterResult:
        state, complete, frames = _CodexJSONL(), True, 0
        while True:
            frame = stream.readline(MAX_OUTPUT_BYTES + 1)
            if not frame:
                break
            # The per-frame bound includes a delimiter when present; final LF is optional.
            if len(frame) > MAX_OUTPUT_BYTES:
                complete = False
                break
            try:
                line = frame.decode('utf-8')
            except UnicodeDecodeError:
                complete = False
                break
            frames += 1
            state.feed(line)
            if state.malformed:
                complete = False
                break
        result = state.result(exit_code, stderr)
        result.raw.update(stdout_parse_complete=complete, stdout_frames_processed=frames,
                          stdout_frame_limit_bytes=MAX_OUTPUT_BYTES)
        if not complete:
            result.raw.update(usage_coverage=dict.fromkeys((*TOKEN_FIELDS, 'calls'), False),
                              stdout_truncated=True)
            result.text = ''
            if result.error_class is None:
                result.error_class = 'capture_incomplete'
        return result


def _codex_stream_capable(adapter: Adapter) -> bool:
    """Unknown parse hooks retain prefix behavior, including beneath known recording layers."""
    if not isinstance(adapter, CodexAdapter) or 'parse' in vars(adapter) or 'parse_stream' in vars(adapter):
        return False
    for owner in type(adapter).__mro__:
        if 'parse' in owner.__dict__ and 'parse_stream' not in owner.__dict__:
            return False
        if owner is CodexAdapter:
            return True
    return False


class _CodexJSONL:
    """Shared legacy/stream semantics with bounded last answer and error classification state."""
    def __init__(self):
        self.usage, self.session_id, self.text = {}, '', ''
        self.errors, self.turns, self.lower = 0, 0, 0
        self.malformed = False
        self.coverage = dict.fromkeys((*TOKEN_FIELDS, 'calls'), True)
        self.error_tail, self.error_class = '', None

    def _classify(self, text):
        category = classify_error(text)
        priority = {None: 0, 'auth': 1, 'rate_limit': 2, 'quota': 3}
        if priority[category] > priority[self.error_class]:
            self.error_class = category

    def feed(self, line):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.malformed = self.malformed or bool(line.strip())
            return
        if not isinstance(event, dict):
            self.malformed = True
            return
        kind = event.get('type')
        if kind == 'thread.started':
            self.session_id = str(event.get('thread_id', ''))
        elif kind == 'turn.completed':
            self.turns += 1
            u = event.get('usage')
            incoming, cached, output = (_counter(u, k) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens'))
            compatible = incoming is None or cached is None or cached <= incoming
            new = incoming - cached if incoming is not None and cached is not None and compatible else None
            values = {'tokens_in': new, 'tokens_cached': cached if compatible else None, 'tokens_out': output}
            for key, value in values.items():
                self.coverage[key] = self.coverage[key] and value is not None
                if value is not None:
                    self.usage[key] = self.usage.get(key, 0) + value
            self.lower += ((incoming if incoming is not None else cached or 0) if compatible else 0) + (output or 0)
            self.usage['calls'] = self.turns
        elif kind == 'item.completed' and isinstance(event.get('item'), dict) and event['item'].get('type') == 'agent_message':
            self.text = str(event['item'].get('text', ''))[-MAX_REPLY:]
        elif kind in ('error', 'turn.failed'):
            fragment = (' ' if self.errors else '') + json.dumps(event, ensure_ascii=False)[:500]
            self.errors += 1
            combined = self.error_tail + fragment
            self._classify(combined)
            # Longer than every bounded classify_error pattern, including cross-fragment context.
            self.error_tail = combined[-256:]

    def result(self, exit_code, stderr):
        self._classify(self.error_tail + stderr)
        failed = exit_code != 0 or bool(self.errors)
        coverage = dict(self.coverage)
        if failed or self.malformed or self.turns == 0:
            coverage = dict.fromkeys(coverage, False)
        return AdapterResult(exit_code=exit_code, text=self.text, usage=self.usage or None,
            session_id=self.session_id, error_class=(self.error_class or 'crash') if failed else None,
            raw={'errors': self.errors, 'usage_coverage': coverage, 'known_token_lower_bound': self.lower,
                 'completed_turns': self.turns, 'host_execute_calls': 1})


class APIAdapter(Adapter):
    """A trusted local tool conversation worker, using an explicitly configured HTTP provider."""
    auth_mode = 'api_key'
    shell = False

    def __init__(self, driver, model=None, effort=None, *, home=None, readonly=False, shell=False,
                 tools=True, max_rounds=8, write_paths=None):
        from . import providers
        self.home = Path(home or os.environ.get("LUPUS_HOME") or Path.home()/".lupus").resolve()
        self.spec = providers.resolve(driver, self.home)
        if self.spec.protocol != 'openai-chat' or not self.spec.base_url:
            raise LupusError('PROVIDER_CONFIG_REQUIRED', 'configure this provider endpoint first')
        if effort is not None:
            raise LupusError('EFFORT_UNSUPPORTED', 'this API profile has no validated effort mapping; omit --effort')
        if model is None and self.spec.default_model is None:
            raise LupusError('MODEL_REQUIRED', 'select --model <id> or set default_model in providers.json')
        self.model = providers.validate_model(model or self.spec.default_model)
        self.effort = effort
        self.driver, self.variant = self.spec.driver, self.spec.id + '/' + self.model
        self.auth_mode = 'api_key' if self.spec.api_key_env else 'explicit_endpoint'
        self.readonly, self.shell, self.tools = readonly, shell, tools
        self.max_rounds = min(8, max(1, int(max_rounds)))
        self.timeout_s = 300
        self.write_paths = write_paths

    def argv(self, prompt, cwd):
        from .util import PROTECTED_STATE
        config = {'model':self.model, 'base_url':self.spec.base_url, 'prompt':prompt,
                  'readonly':self.readonly, 'shell':self.shell, 'tools':self.tools,
                  'max_rounds':self.max_rounds, 'timeout_s':self.timeout_s, 'write_paths':self.write_paths,
                  'excluded_paths':sorted(set(self.read_exclude) | PROTECTED_STATE | {str(self.home)})}
        if getattr(self,'skill_access',None):config['skill_access']=self.skill_access
        script = 'import sys,runpy;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parent.parent)) + ');runpy.run_module("lupus.api_worker",run_name="__main__")'
        return [sys.executable, '-I', '-c', script, json.dumps(config)]

    def env(self):
        from . import providers
        env = worker_env()
        for spec in providers.catalog(self.home):
            if spec.get('api_key_env'):env.pop(spec['api_key_env'], None)
        key = os.environ.get(self.spec.api_key_env) if self.spec.api_key_env else None
        if self.spec.api_key_env and not key:
            raise LupusError('API_AUTH_MISSING', 'set the selected provider API key environment variable')
        if key:env['LUPUS_API_AUTH_TOKEN'] = key
        return env

    def parse(self, exit_code, stdout, stderr):
        started, completed, text, error, malformed, models = set(), {}, '', None, False, set()
        answer_incomplete = False
        tools = {'started':0,'completed':0,'failed':0,'errors':[],'counts_capped':False,'invalid_events':False}
        tool_started, tool_completed = set(), set()
        skill_reads=[];skill_reads_limited=False
        for line in stdout.split('\n'):
            if not line.strip():continue
            try:
                event=json.loads(line);kind=event.get('type')
                if kind=='request.started':started.add(event['index'])
                elif kind=='request.completed':
                    completed[event['index']]=event.get('usage')
                    if isinstance(event.get('reported_model'),str):models.add(event['reported_model'])
                elif kind=='answer':
                    value=event.get('text')
                    if not isinstance(value,str) or len(value)>MAX_REPLY:answer_incomplete=True
                    else:text=value
                elif kind=='skill.read':
                    sid=event.get('id');digest=event.get('sha256');resource=event.get('resource')
                    if (not isinstance(sid,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}',sid)
                            or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest)
                            or not valid_counter(event.get('bytes')) or event['bytes']>65536
                            or resource is not None and (not isinstance(resource,str) or len(resource)>1024 or not resource.isprintable())):
                        skill_reads_limited=True
                    elif len(skill_reads)>=24:skill_reads_limited=True
                    else:skill_reads.append({'id':sid,'source':diagnostic_tail(str(event.get('source','')),limit=80),
                                            'sha256':digest,'bytes':event['bytes'],'resource':resource})
                elif kind in ('tool.started','tool.completed'):
                    field='started' if kind=='tool.started' else 'completed'
                    if tools[field]>=32:tools['counts_capped']=True
                    tools[field]=min(32,tools[field]+1)
                    index=event.get('index')
                    if type(index) is not int or not 1<=index<=32:
                        tools['invalid_events']=True;index=None
                    elif field=='started':tool_started.add(index)
                    else:tool_completed.add(index)
                    if field=='completed' and event.get('error') is not None:
                        if tools['failed']>=32:tools['counts_capped']=True
                        tools['failed']=min(32,tools['failed']+1)
                        name=event.get('name');code=event.get('error')
                        name=name if isinstance(name,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',name) else 'unknown_tool'
                        code=code if isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',code) else 'TOOL_ERROR'
                        if len(tools['errors'])<32:tools['errors'].append({'index':index,'name':name,'error_code':code})
                elif kind=='error':error=str(event.get('error_class','api_error'))
            except (ValueError,TypeError,KeyError,AttributeError):malformed=True
        tools.update(pending_started_indices=sorted(tool_started-tool_completed),scope='observed worker tool events, capped at 32; errors may have been recovered')
        usage,coverage,lower={'calls':len(started)},dict.fromkeys((*TOKEN_FIELDS,'calls'),True),0
        if not started:coverage=dict.fromkeys(coverage,False)
        for index in started:
            reported=completed.get(index)
            incoming=_counter(reported,'prompt_tokens');output=_counter(reported,'completion_tokens')
            detail=reported.get('prompt_tokens_details') if isinstance(reported,dict) else None
            cached=_counter(detail,'cached_tokens')
            compatible=incoming is None or cached is None or cached<=incoming
            values={'tokens_in':incoming-cached if incoming is not None and cached is not None and compatible else None,
                    'tokens_cached':cached if compatible else None,'tokens_out':output}
            for field,value in values.items():
                coverage[field]=coverage[field] and value is not None
                if value is not None:usage[field]=usage.get(field,0)+value
            lower+=((incoming if incoming is not None else cached or 0) if compatible else 0)+(output or 0)
        failed=exit_code!=0 or bool(error) or malformed or answer_incomplete
        if answer_incomplete and error is None:error='capture_incomplete'
        if failed:
            if malformed:coverage=dict.fromkeys(coverage,False)
            text=''
        self.last_skill_reads=skill_reads
        return AdapterResult(exit_code,text=text,usage=usage,error_class=(error or 'api_error') if failed else None,
            raw={'usage_coverage':coverage,'known_token_lower_bound':lower,'host_execute_calls':1,
                 'http_request_attempts':len(started),'http_completed_responses':len(completed),
                 'calls_scope':'HTTP request attempts, including failed attempts; not provider billing',
                 'models':sorted(models),'requested_model':self.model,'requested_provider':self.spec.id,
                 'answer_complete':not failed,'effective_backend_confirmed':False,'tool_diagnostics':tools,
                 **({'skill_reads':skill_reads,'skill_reads_limited':skill_reads_limited} if skill_reads or skill_reads_limited else {})})


def validate_native_config(driver: str, profile: str = "lean", native_env=()) -> tuple[str, ...]:
    """Pure preflight: no CLI/config/auth file reads, and never log environment values."""
    if profile not in ("lean", "user"):
        raise LupusError("NATIVE_CONFIG_INVALID", "choose lean or user")
    if not isinstance(native_env,(list,tuple)) or any(not isinstance(name,str) for name in native_env):
        raise LupusError("NATIVE_ENV_INVALID", "native environment selections must be names")
    names=tuple(dict.fromkeys(native_env))
    if profile == "lean":
        if names:raise LupusError("NATIVE_ENV_INVALID", "--native-env requires --native-config user")
        return names
    if driver not in ("codex", "native_codex"):
        raise LupusError("NATIVE_CONFIG_UNSUPPORTED", "user native configuration is currently supported only for Codex agent mode")
    if not sandbox_available():
        raise LupusError("NATIVE_CONFIG_SANDBOX_REQUIRED", "user configuration requires the OS sandbox; lean remains available")
    base=Path.home()/".codex"
    try:
        supported_root=not base.is_symlink() and base.is_dir()
    except OSError:
        supported_root=False
    if not supported_root:
        raise LupusError("NATIVE_CONFIG_HOME_UNSUPPORTED", "the default Codex configuration directory must be an existing nonsymlink directory")
    configured=os.environ.get("CODEX_HOME")
    if configured and Path(configured).resolve() != (Path.home()/".codex").resolve():
        raise LupusError("NATIVE_CONFIG_HOME_UNSUPPORTED", "user profile currently supports only the default Codex configuration directory")
    if len(names)>32:raise LupusError("NATIVE_ENV_INVALID", "at most 32 named variables")
    forbidden={"HOME","PATH","SHELL","BASH_ENV","ENV","NODE_OPTIONS","NODE_PATH","CLAUDE_CONFIG_DIR","ZDOTDIR","RUBYOPT","PERL5OPT","GCONV_PATH","IFS"}
    for name in names:
        if not isinstance(name,str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}",name) or name in forbidden or name.startswith(("TMP","TEMP","CODEX","PYTHON","LD_","DYLD_","LUPUS_")):
            raise LupusError("NATIVE_ENV_INVALID", "environment name is invalid or controls the worker runtime")
        if name not in os.environ:
            raise LupusError("NATIVE_ENV_MISSING", "a selected native environment variable is not set: " + name)
    return names


class CodexUserConfigAdapter(CodexAdapter):
    """Explicit user configuration inside the required outer OS sandbox.

    The inner named filesystem profile is requested, not confirmed effective: loaded legacy
    sandbox settings may take precedence. The outer home configuration/authentication read
    grant is inherited by CLI descendants; read-only does not mean hidden from their tools.
    Only controller-owned transient state is writable outside the project.
    """
    auth_mode = "native_user_configuration_unknown"
    os_sandbox = "codex"
    require_os_sandbox = True
    configured_native = True

    def __init__(self, model=None, effort=None, *, private_tmp: Path, native_env=(), read_exclude=()):
        self.native_env_names=validate_native_config("native_codex","user",native_env)
        validate_effort("native_codex",effort)
        super().__init__(model,effort,shared_tmp=False)
        self.private_tmp=private_tmp.resolve()
        self.read_exclude=tuple(read_exclude)
        self.variant += "/user-config"

    def argv(self,prompt,cwd):
        argv=super().argv(prompt,cwd)
        # Remove each known header flag once, never matching the prompt/model payload.
        payload=argv[-1]
        argv=argv[:-1]
        argv.remove("--ignore-user-config")
        argv.remove("--ignore-rules")
        policy=codex_filesystem_profile(self.readonly,self.shared_tmp,self.read_exclude)
        policy[str(self.private_tmp)]="write"
        # Request narrow native skill reads in the inner profile. Its effectiveness
        # is unconfirmed; the outer CLI/auth read grant also applies to descendants.
        for root in (Path.home()/".codex/skills",Path.home()/".agents/skills"):
            policy[str(root)]="read"
        for path in self.read_exclude:policy[os.path.realpath(path)]="deny"
        for index,value in enumerate(argv):
            if index and argv[index-1] == "-c" and value.startswith("permissions.lupus.filesystem="):
                argv[index]="permissions.lupus.filesystem="+_toml(policy)
        return [*argv,"-c","log_dir="+_toml(str(self.private_tmp/"log")),
                "-c","sqlite_home="+_toml(str(self.private_tmp/"sqlite")),payload]

    def env(self):
        validate_native_config("native_codex","user",self.native_env_names)
        env=super().env()
        for name in self.native_env_names:env[name]=os.environ[name]
        return env


class FakeAdapter(Adapter):
    """Scripted worker for tests and fault injection. `script` is Python source executed in the
    task directory; it stands in for whatever a model would have done."""

    driver = "fake"
    auth_mode = "none"

    def __init__(self, script: str, usage: dict[str, int] | None = None, error_class: str | None = None):
        self.script = script
        self.usage = usage
        self.error_class = error_class

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        return [sys.executable, "-c", self.script, prompt]

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        failed = exit_code != 0
        return AdapterResult(
            exit_code=exit_code, text=stdout[-MAX_REPLY:], usage=self.usage, session_id="fake-session",
            error_class=(self.error_class or classify_error(stdout + stderr) or "crash") if failed else None,
        )


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Documented configuration vocabulary, not a claim of model/account support:
# https://learn.chatgpt.com/docs/config-file/config-reference#model_reasoning_effort
CODEX_EFFORTS = (*CLAUDE_EFFORTS, "ultra")


def validate_effort(driver: str, effort: str | None) -> None:
    if effort is None:
        return
    supported = {"claude": CLAUDE_EFFORTS, "native_claude": CLAUDE_EFFORTS,
                 "codex": CODEX_EFFORTS, "native_codex": CODEX_EFFORTS}.get(driver, ())
    if effort not in supported:
        raise LupusError("EFFORT_INVALID", f"{driver}: expected one of {', '.join(supported)}")

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")


def claude_preference() -> tuple[str | None, str | None]:
    """The model and effort the user chose for Claude Code (`model`, `effortLevel` in their user
    settings). The worker runs with every settings file switched off, so that plugins, hooks and
    MCP servers stay out; these two choices are the user's own and are carried over by flag.
    Measured 2026-10-26: without them the worker ran at the CLI's default effort while the user's
    plain `claude` ran at the `medium` they had chosen."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        settings = json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(settings, dict):
        return None, None
    model, effort = settings.get("model"), settings.get("effortLevel")
    return (model if isinstance(model, str) and _MODEL_NAME.match(model) else None,
            effort if effort in CLAUDE_EFFORTS else None)


def claude_worker(model: str | None = None, effort: str | None = None, **options) -> ClaudeAdapter:
    """Explicit model/effort overrides win; omitted choices keep the user's preference."""
    validate_effort("claude", effort)
    chosen, preferred_effort = claude_preference()
    return ClaudeAdapter(model or chosen, effort=preferred_effort if effort is None else effort, **options)


def native(driver: str, model: str | None = None, effort: str | None = None, *, home=None, readonly=False, shell=False) -> Adapter:
    if driver not in ("native_claude", "native_codex"):
        return APIAdapter(driver, model, effort, home=home, readonly=readonly, shell=shell)
    validate_effort(driver, effort)
    if driver == "native_claude":
        return claude_worker(model, effort=effort)
    if driver == "native_codex":
        return CodexAdapter(model, effort=effort)
    raise ValueError(driver)


def cheap_first(driver: str) -> list[Adapter]:
    """Tiers for work that a deterministic verifier will check anyway: try the lighter setting,
    and only if verification fails spend the default one. Same CLI, same login, same flags."""
    if driver == "native_claude":
        return [ClaudeAdapter("haiku"), ClaudeAdapter()]
    if driver == "native_codex":
        return [CodexAdapter(effort="low"), CodexAdapter()]
    raise ValueError(driver)
