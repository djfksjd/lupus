"""Execution drivers. Each adapter turns a task prompt into one worker process and reports
exit status and whatever usage the host actually exposes.

Native adapters call the installed `claude` / `codex` binaries with the user's existing
subscription login. They never read, copy or forward credentials, and never call a provider
endpoint directly (§10.3). API keys and provider overrides inherited from the environment are
removed so a run cannot silently bill an API account (AUTH-01, COST-02).

Flags below were measured on this machine by `lupus probe` (Claude Code 2.1.289,
codex-cli 0.160.0); re-run the probe after a CLI upgrade.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import (
    GATE, GATE_EXEC_FAILED, SANDBOX_EXEC, LupusError, crash_point, group_alive, safe_path, sandbox_available,
    sandbox_profile_worker, scrubbed_env, stop_group,
)

MAX_OUTPUT_BYTES = 20 * 1024 * 1024    # a runaway worker must not exhaust the supervisor's memory


def worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return scrubbed_env(extra)


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


class Adapter:
    driver = "abstract"
    auth_mode = "none"
    variant = "default"     # model / effort tier; part of an attempt's identity
    batch = False           # True when one call has a large fixed input, so fewer calls is cheaper
    interactive = False     # True: the user works in the CLI's own screen (see execute_interactive)
    os_sandbox: str | None = None   # name of an OS sandbox profile to run the CLI under (util.sandbox_profile_worker)

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        raise NotImplementedError

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return worker_env()


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
    if adapter.interactive:
        return execute_interactive(adapter, prompt, cwd, on_spawn, timeout_s)
    started = time.monotonic()
    argv = adapter.argv(prompt, cwd)
    confined = bool(adapter.os_sandbox) and sandbox_available()
    if confined:
        # Enforced by the OS, whatever the CLI's own permission system decides.
        argv = [SANDBOX_EXEC, "-p", sandbox_profile_worker(cwd, adapter.os_sandbox), *argv]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [sys.executable, GATE, *argv],
            cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err,
            start_new_session=True, env=adapter.env(),
        )
        timed_out = False
        try:
            crash_point("adapter.after_spawn")
            on_spawn(proc.pid)
            crash_point("adapter.before_go")
            proc.stdin.write(b"g")
            proc.stdin.close()
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException as exc:
            stop_group(proc.pid)
            proc.wait()
            if isinstance(exc, OSError):   # the gate died before it could be released
                raise LupusError("WORKER_SPAWN_FAILED", str(exc)) from exc
            raise
        leftover = group_alive(proc.pid) if not timed_out else False
        stop_group(proc.pid)
        proc.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
        stderr = err.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
    result = adapter.parse(None if timed_out else proc.returncode, stdout, stderr)
    if timed_out:
        result.error_class = "timeout"
    elif proc.returncode == GATE_EXEC_FAILED:
        result.error_class = "unavailable"   # the CLI binary is missing or not executable
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.leftover_processes = leftover
    result.raw["os_sandbox"] = confined
    return result




def execute_interactive(adapter: Adapter, prompt: str, cwd: Path, on_spawn: Callable[[int], None],
                        timeout_s: float) -> AdapterResult:
    """Hand the terminal to the CLI's own interactive screen, under the same rules as a headless
    worker: its process group is recorded before it may start (exec gate), it is the foreground
    job of this terminal while it runs, and the group is emptied before this returns.

    Nothing the user or the model types is seen or stored here, and the host reports no usage
    for such a session; the caller charges the reservation in full."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LupusError("USER_PRESENCE_REQUIRED", "an interactive session needs a terminal")
    tty = sys.stdin.fileno()
    saved = termios.tcgetattr(tty)
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, GATE, *adapter.argv(prompt, cwd)], cwd=cwd, pass_fds=(read_fd,), process_group=0,
        env={**adapter.env(), "LUPUS_GATE_FD": str(read_fd)},
    )
    os.close(read_fd)
    ignored = signal.signal(signal.SIGTTOU, signal.SIG_IGN)      # we are about to become a background job
    timed_out = False
    try:
        on_spawn(proc.pid)
        os.tcsetpgrp(tty, proc.pid)
        os.write(write_fd, b"g")
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        raise LupusError("WORKER_SPAWN_FAILED", str(exc)) from exc
    finally:
        os.close(write_fd)
        leftover = group_alive(proc.pid) if not timed_out and proc.returncode is not None else False
        stop_group(proc.pid)
        proc.wait()
        try:      # only now, with nothing of the session left to change it again: take the terminal
            os.tcsetpgrp(tty, os.getpgrp())      # back and undo whatever screen mode the CLI left behind
            termios.tcsetattr(tty, termios.TCSADRAIN, saved)
        except OSError:
            pass
        signal.signal(signal.SIGTTOU, ignored)
    code = None if timed_out else proc.returncode
    return AdapterResult(
        exit_code=code, usage=None, leftover_processes=leftover, duration_ms=int((time.monotonic() - started) * 1000),
        error_class="timeout" if timed_out else ("unavailable" if code == GATE_EXEC_FAILED else None))


PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_API", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN_BEDROCK",
                         "CLAUDE_CODE_OAUTH_TOKEN")


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
    if any(s in low for s in ("usage limit", "quota", "limit reached", "out of credits", "limit will reset")):
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

    def __init__(self, model: str | None = None, tools: tuple[str, ...] = ("Read", "Write", "Edit"),
                 web: bool = False):
        self.model = model
        self.tools = tools + (("WebSearch", "WebFetch") if web else ())
        self.web = web
        self.variant = (model or "default") + ("+web" if web else "")

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
        if self.web:      # the user asked for a task that needs to look things up
            argv += ["--allowedTools", "WebSearch", "WebFetch"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                data = next((d for d in reversed(data) if d.get("type") == "result"), {})
        except (json.JSONDecodeError, AttributeError):
            data = {}
        u = data.get("usage") or {}
        usage = None
        if u:
            usage = {
                "tokens_in": int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
                "tokens_cached": int(u.get("cache_read_input_tokens", 0)),
                "tokens_out": int(u.get("output_tokens", 0)),
                "calls": int(data.get("num_turns", 1)),
            }
        failed = exit_code != 0 or bool(data.get("is_error"))
        text = str(data.get("result", ""))
        return AdapterResult(
            exit_code=exit_code, text=text[-8000:], usage=usage, session_id=str(data.get("session_id", "")),
            error_class=(classify_error(text + stderr) or "crash") if failed else None,
            # total_cost_usd is the host's list-price estimate, not a charge on a subscription.
            raw={"estimated_list_cost_usd": data.get("total_cost_usd"),
                 "models": sorted(data.get("modelUsage") or {}),
                 "permission_denials": len(data.get("permission_denials") or [])},
        )


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}={_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def codex_filesystem_profile(readonly: bool = False) -> dict:
    """What a Codex worker may touch: read the system minimum, the Codex program itself and the
    language toolchains; write only the project and the temp directory. The user's home is NOT
    readable, so keys, tokens and other projects stay out of reach. Measured with a canary by
    `lupus probe --live` (codex-cli 0.160.0: default sandbox read it, this profile did not)."""
    # `readonly` is for calls that only think (a judge): no write access anywhere, and no access to the
    # shared temp directory, where other projects may live.
    profile: dict = ({":minimal": "read", ":project_roots": {".": "read"}} if readonly
                     else {":minimal": "read", ":project_roots": {".": "write"}, ":tmpdir": "write"})
    cli = shutil.which("codex", path=safe_path())
    if cli:
        real = os.path.realpath(cli)
        packages = real.split("/packages/")[0] + "/packages" if "/packages/" in real else os.path.dirname(real)
        for path in (os.path.dirname(cli), packages):
            profile[path] = "read"
    for toolchain in () if readonly else ("/opt/homebrew", "/usr/local"):     # interpreters a worker may run when self-checking
        if os.path.isdir(toolchain):
            profile[toolchain] = "read"
    return profile


class CodexAdapter(Adapter):
    """`codex exec` with the default profile's ChatGPT login and config.toml (hooks, MCP servers,
    plugins) ignored. A permission profile confines writes to the task directory and reads to the
    project plus what the CLI needs to run."""

    driver = "native_codex"
    auth_mode = "subscription"
    batch = True            # measured: ~30k tokens of fixed input per call

    def __init__(self, model: str | None = None, effort: str | None = None, readonly: bool = False):
        self.model = model
        self.effort = effort
        self.readonly = readonly
        self.variant = f"{model or 'default'}/{effort or 'default'}"

    def argv(self, prompt: str, cwd: Path) -> list[str]:
        argv = [
            resolve_cli("codex", cwd), "exec", "--json", "--color", "never",
            "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config", "--ignore-rules",
            # A named permission profile instead of `--sandbox workspace-write`: that mode lets
            # commands read every file the user can.
            "-c", 'default_permissions="lupus"',
            "-c", f"permissions.lupus.filesystem={_toml(codex_filesystem_profile(self.readonly))}",
            "-C", str(cwd),
        ]
        if self.model:
            argv += ["-m", self.model]
        if self.effort:
            argv += ["-c", f'model_reasoning_effort="{self.effort}"']
        return argv + [prompt]

    def parse(self, exit_code: int | None, stdout: str, stderr: str) -> AdapterResult:
        usage, session_id, text, errors, turns = None, "", "", [], 0
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "thread.started":
                session_id = str(event.get("thread_id", ""))
            elif kind == "turn.completed":
                turns += 1
                u = event.get("usage") or {}
                usage = usage or {"tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "calls": 0}
                cached = int(u.get("cached_input_tokens", 0))
                usage["tokens_in"] += max(0, int(u.get("input_tokens", 0)) - cached)
                usage["tokens_cached"] += cached
                usage["tokens_out"] += int(u.get("output_tokens", 0))
                usage["calls"] = turns
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                text = str(event["item"].get("text", ""))
            elif kind in ("error", "turn.failed"):
                errors.append(json.dumps(event, ensure_ascii=False)[:500])
        failed = exit_code != 0 or bool(errors)
        return AdapterResult(
            exit_code=exit_code, text=text[-8000:], usage=usage, session_id=session_id,
            error_class=(classify_error(" ".join(errors) + stderr) or "crash") if failed else None,
            raw={"errors": len(errors)},
        )


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
            exit_code=exit_code, text=stdout[-8000:], usage=self.usage, session_id="fake-session",
            error_class=(self.error_class or classify_error(stdout + stderr) or "crash") if failed else None,
        )


def native(driver: str, model: str | None = None) -> Adapter:
    if driver == "native_claude":
        return ClaudeAdapter(model)
    if driver == "native_codex":
        return CodexAdapter(model)
    raise ValueError(driver)


def cheap_first(driver: str) -> list[Adapter]:
    """Tiers for work that a deterministic verifier will check anyway: try the lighter setting,
    and only if verification fails spend the default one. Same CLI, same login, same flags."""
    if driver == "native_claude":
        return [ClaudeAdapter("haiku"), ClaudeAdapter()]
    if driver == "native_codex":
        return [CodexAdapter(effort="low"), CodexAdapter()]
    raise ValueError(driver)
