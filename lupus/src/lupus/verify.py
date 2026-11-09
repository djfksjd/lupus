"""Deterministic verifiers (§12.2). They run in the supervisor, cost no model call, and are the
only source of completion evidence. The verifier of a criterion is part of the acceptance
revision, so a worker cannot swap it for an easier one."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

from . import runners, process_lifetime, container_lifetime
from .util import (
    GATE, GATE_EXEC_FAILED, SANDBOX_EXEC, LupusError, container_stop, safe_path, sandbox_available, sandbox_profile,
    scrubbed_env, sha256_file, PROTECTED_STATE,
    sha256_json, stop_group, gate_argv,
)

from contextvars import ContextVar

OBSERVATION = ContextVar("lupus_verifier_observation", default=None)

def observe(verifier, genuine, reason):
    callback=OBSERVATION.get()
    if callback is not None:callback({"verifier_hash":sha256_json(verifier),"genuine":genuine,"reason":reason})

VERSION = "5"
MAX_OUTPUT = 32 * 1024 * 1024
OUTPUT_TOO_LARGE = -32
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PLACEHOLDERS = ("TODO", "TBD", "lorem ipsum", "[citation needed]", "(작성 예정)")


def _inside(root: Path, rel: str) -> Path:
    root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(root / rel))
    if target != root and root not in target.parents:
        raise LupusError("PATH_ESCAPES_PROJECT", rel)
    return target


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_MAX_DIR_FILES = 5000


def _hash(path: Path) -> str:
    """Content hash of a file, or of everything under a directory (names and contents)."""
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        return "missing"
    entries: list[list[str]] = []
    for current, dirs, files in os.walk(path):
        for name in sorted(dirs):
            link = Path(current) / name
            if link.is_symlink():     # os.walk does not descend into it; its target is still an input
                entries.append([os.path.relpath(link, path), "symlink:" + os.readlink(link)])
        # Also skipped: a directory its own tool marks as a cache (cargo's target/ carries a
        # CACHEDIR.TAG). Other output folders such as dist/ may be the product and stay watched.
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not os.path.isfile(os.path.join(current, d, "CACHEDIR.TAG")))
        for name in sorted(files):
            item = Path(current) / name
            rel = os.path.relpath(item, path)
            if item.is_symlink():
                entries.append([rel, "symlink:" + os.readlink(item)])   # a link appearing/retargeting is a change
            elif item.is_file():
                entries.append([rel, sha256_file(item)])
            if len(entries) > _MAX_DIR_FILES:
                raise LupusError("VERIFIER_PATH_TOO_LARGE", f"{path.name}: more than {_MAX_DIR_FILES} files to watch; move "
                                 "generated files out of the project or describe the goal in a goal file with narrower `paths`")
    return sha256_json(entries)


def artifact_hash(verifier: Mapping, root: Path) -> str:
    """Hash of what the verifier looks at. Evidence is reusable only while this is unchanged."""
    paths = [verifier["path"]] if "path" in verifier else list(verifier.get("paths", []))
    return sha256_json([[p, _hash(_inside(root, p))] for p in sorted(paths)])


def timeout_s(verifier: Mapping) -> float:
    """Upper bound of one verification; this is what gets reserved before a change starts."""
    if verifier["kind"] == "judge":
        return float(verifier.get("timeout_s", 300))
    return float(verifier.get("timeout_s", 120)) if verifier["kind"] in ("command", "red_test") else 5.0


def _judge_red(verifier: Mapping, root: Path, code: int, output: str, digest: str, tail: str) -> tuple[str, str, str]:
    """A newly written acceptance test is useful only if it can tell "not done" from "done".
    PASS means: the file exists, at least one of its tests really ran, and it FAILS on the code
    as it is now, for a reason other than the test file itself being broken. It does not mean
    the test captures the request; that is what the user's approval is for."""
    if not _inside(root, verifier["path"]).is_file():
        return "FAIL", digest, f"the test file {verifier['path']} was not created"
    if code == 0:
        return "FAIL", digest, "the new test already passes on the current code, so it cannot show that the request was done"
    why = runners.judge_red(verifier.get("require_tests", ""), verifier["path"], output)
    if why is not None:
        return "FAIL", digest, f"{why}: {tail}"
    return "PASS", digest, "red on the current code: " + tail[-300:]


def _run_container(verifier,root,on_spawn,on_exit):
    project=Path(root).resolve()
    for protected in PROTECTED_STATE:
        control=Path(protected).resolve()
        if project==control or project in control.parents or control in project.parents:
            raise LupusError('VERIFIER_CONTAINER_SCOPE','project bind overlaps protected runtime/control state')
    docker=shutil.which('docker',path=safe_path())
    if docker is None:raise LupusError('VERIFIER_CONTAINER_UNAVAILABLE','docker was not found on PATH')
    args=['--network','none','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','512','--memory','2g',
          '-v',f'{os.path.realpath(root)}:/work','-w','/work','-e','PYTHONDONTWRITEBYTECODE=1',
          *[x for name,value in sorted(verifier.get('env',{}).items()) for x in ('-e',f'{name}={value}')],
          verifier['container'],*verifier['argv']]
    try:lease=container_lifetime.Lease(docker,args)
    except Exception as exc:raise LupusError('VERIFIER_CONTAINER_UNAVAILABLE',str(exc)[:200]) from exc
    proc=None;timed_out=False;overflow=False;target_code=None;gone=False;emptied=False;healthy=True
    with tempfile.TemporaryFile() as out:
        try:
            argv=lease.prepare(on_spawn)
            proc=process_lifetime.spawn(argv,cwd=root,stdout=out,stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,start_new_session=True,env=container_lifetime.SAFE_ENV)
            if on_spawn:on_spawn(proc.pid,':container:'+container_lifetime.encode(lease.desc))
            if not lease.healthy():raise LupusError('VERIFIER_CLEANUP_FAILED','container guardian lost before start')
            proc.stdin.write(b'g');proc.stdin.close()
            deadline=time.monotonic()+timeout_s(verifier)
            while True:
                if not lease.healthy():
                    healthy=False
                    raise LupusError('VERIFIER_CLEANUP_FAILED','container guardian lost')
                if os.fstat(out.fileno()).st_size>MAX_OUTPUT:overflow=True;break
                remaining=deadline-time.monotonic()
                if remaining<=0:timed_out=process_lifetime.poll(proc) is None;break
                try:
                    target_code=process_lifetime.wait(proc,timeout=min(.05,remaining));break
                except subprocess.TimeoutExpired:pass
        except LupusError:raise
        except Exception as exc:raise LupusError('VERIFIER_CONTAINER_FAILED',str(exc)[:200]) from exc
        finally:
            original=sys.exception();cleanup_error=None
            def remember(exc):
                nonlocal cleanup_error
                if cleanup_error is None or isinstance(cleanup_error,Exception) and not isinstance(exc,Exception):cleanup_error=exc
            try:gone=lease.finish()
            except BaseException as exc:remember(exc)
            if proc is not None:
                try:
                    certificate=process_lifetime.finish(proc,cancel=timed_out or overflow or original is not None)
                    emptied=certificate['complete']
                    if target_code is None and not timed_out:target_code=certificate['exit_code']
                except BaseException as exc:remember(exc)
                finally:
                    try:
                        if proc.stdin is not None and not proc.stdin.closed:proc.stdin.close()
                    except BaseException as exc:remember(exc)
            else:emptied=True
            if gone and emptied and cleanup_error is None and on_exit:
                try:
                    if proc is not None:on_exit(proc.pid)
                    for helper_pid in getattr(lease,'registered_pids',([lease.registered_pid] if lease.registered else [])):on_exit(helper_pid)
                except BaseException as exc:remember(exc)
            if cleanup_error is not None or not gone or not emptied:
                if original is not None and not isinstance(original,Exception):
                    original.add_note('Container lifetime cleanup unconfirmed');raise original
                if cleanup_error is not None and not isinstance(cleanup_error,Exception):raise cleanup_error
                detail='container cleanup ownership/absence unconfirmed'
                try:
                    stages=sorted({item['phase']+'/'+item['reason'] for item in container_lifetime.diagnostics(container_lifetime.encode(lease.desc))})[:3]
                    if stages:detail+='; recorded stages (first observations): '+', '.join(stages)
                except Exception:pass
                raise LupusError('VERIFIER_CLEANUP_FAILED',detail) from cleanup_error
        if not healthy:raise LupusError('VERIFIER_CLEANUP_FAILED','container guardian lost')
        if overflow or out.seek(0,os.SEEK_END)>MAX_OUTPUT:return OUTPUT_TOO_LARGE,''
        out.seek(0);output=_ANSI.sub('',out.read().decode('utf-8',errors='replace'))
        if not timed_out and target_code==125:raise LupusError('VERIFIER_CONTAINER_FAILED',output.strip()[-200:])
        if verifier.get('must_pass') and not timed_out and target_code==0:
            now=set(runners.cargo_tests(output)[0]);gone_tests=[name for name in verifier['must_pass'] if name not in now]
            if gone_tests:return 1,output+f"\ntests that passed before are missing or no longer pass: {', '.join(gone_tests[:5])}"
        return (None if timed_out else target_code),output


def _run_gated(verifier: Mapping, root: Path, on_spawn, on_exit) -> tuple[int | None, str]:
    """Run a verifier command the way a worker is run: own process group behind the exec gate,
    recorded before it starts, emptied before returning, scrubbed environment, bytecode compiled
    from source. Returns (exit code or None on timeout, captured output)."""
    if verifier.get("container"):
        return _run_container(verifier,root,on_spawn,on_exit)
    argv = list(verifier["argv"])
    container = None
    sandboxed = verifier.get("sandbox", True) and container is None
    if sandboxed:
        # The command runs code a model wrote. Confine it: no outbound network, no writes outside
        # the project/temp, no reading of the home directory (see util.sandbox_profile). If the
        # sandbox cannot be applied the check does NOT run: dropping the protection silently is
        # not an option; only the user can switch it off for a criterion (`sandbox: false`).
        if not sandbox_available():
            raise LupusError("VERIFIER_SANDBOX_UNAVAILABLE", "the OS sandbox cannot be applied on this machine")
        argv = [SANDBOX_EXEC, "-p", sandbox_profile(root, argv[0], tuple(verifier.get("sandbox_read", ()))), *argv]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryDirectory(prefix="lupus-pyc-") as pyc:
        proc = process_lifetime.spawn(
            argv, cwd=root, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, start_new_session=True,
            env=scrubbed_env({**verifier.get("env", {}), "PYTHONPYCACHEPREFIX": pyc, "PYTHONDONTWRITEBYTECODE": "1",
                              "NO_COLOR": "1", "FORCE_COLOR": "0"},
                             verifier.get("env_pass", ())),
        )
        timed_out = False
        overflow = False
        try:
            if on_spawn:
                # The container outlives its client: its name is recorded with the process group so
                # that recovery can stop it too (see runs.aux_recorder).
                on_spawn(proc.pid, f":container:{container}") if container else on_spawn(proc.pid)
            proc.stdin.write(b"g")
            proc.stdin.close()
            deadline = time.monotonic() + timeout_s(verifier)
            while True:
                # fstat does not move the descriptor offset shared with the child.
                # This is an early abort, not a hard disk cap between polls.
                if os.fstat(out.fileno()).st_size > MAX_OUTPUT:
                    overflow = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = process_lifetime.poll(proc) is None
                    break
                try:
                    process_lifetime.wait(proc, timeout=min(.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            original_error = sys.exception()
            cleanup_error = None
            gone = emptied = False
            try:
                gone = container is None or container_stop(container)
            except BaseException as exc:
                cleanup_error = exc
            try:
                lifetime = process_lifetime.finish(proc, cancel=timed_out or overflow or original_error is not None)
                emptied = lifetime["complete"]
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            finally:
                if proc.stdin is not None and not proc.stdin.closed:
                    try:proc.stdin.close()
                    except OSError:pass
            if cleanup_error is not None:
                if original_error is not None and not isinstance(original_error, Exception):
                    original_error.add_note("Verifier lifetime cleanup failed: " + type(cleanup_error).__name__)
                    raise original_error
                raise cleanup_error
        target_code = lifetime["exit_code"]
        if not emptied or not gone:
            raise LupusError("VERIFIER_CLEANUP_FAILED", container or str(proc.pid))
        if on_exit:
            on_exit(proc.pid)
        # The WHOLE output is read: a summary cut off at either end could be replaced by a look-alike
        # line. A run that prints more than can be read in full is not verified at all.
        if overflow or out.seek(0, os.SEEK_END) > MAX_OUTPUT:
            return OUTPUT_TOO_LARGE, ""
        out.seek(0)
        # Colour codes would sit between the words a summary is recognised by.
        output = _ANSI.sub("", out.read().decode("utf-8", errors="replace"))
        if container is not None and not timed_out and target_code == 125:
            raise LupusError("VERIFIER_CONTAINER_FAILED", output.strip()[-200:])      # docker itself failed, not the tests
        if verifier.get("must_pass") and not timed_out and target_code == 0:
            # Tests that passed when the goal was set must still be there and still pass (a failing
            # test inside a source file cannot be made to "pass" by deleting it).
            now = set(runners.cargo_tests(output)[0])
            gone_tests = [name for name in verifier["must_pass"] if name not in now]
            if gone_tests:
                return 1, output + f"\ntests that passed before are missing or no longer pass: {', '.join(gone_tests[:5])}"
        if sandboxed and not timed_out and output.lstrip().startswith("sandbox-exec:"):
            if "execvp() of" in output:
                return GATE_EXEC_FAILED, output        # the command itself is missing or not executable
            # The sandbox itself failed to start the command: that is not a test result.
            raise LupusError("VERIFIER_SANDBOX_FAILED", output.strip()[:200])
        return (None if timed_out else target_code), output


def run(
    verifier: Mapping,
    root: Path,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
    on_observation=None,
) -> tuple[str, str, str]:
    if on_observation is None:return _run(verifier,root,on_spawn,on_exit)
    token=OBSERVATION.set(on_observation)
    try:return _run(verifier,root,on_spawn,on_exit)
    finally:OBSERVATION.reset(token)

def _run(verifier,root,on_spawn,on_exit):
    """Returns (PASS|FAIL, artifact_hash, short detail).

    A `command` verifier executes code inside the project, so it is treated like a worker: it
    runs in its own process group behind the exec gate, `on_spawn(pid)` durably records that
    group before the command may start, the group is emptied before returning, and `on_exit`
    is called only after that was confirmed. If the group cannot be emptied this raises, and
    the writer slot stays taken.
    """
    kind = verifier["kind"]
    digest = artifact_hash(verifier, root)
    if kind in ("file_contains","file_sha256","document"):
        observe(verifier,True,"artifact_checked")
    if kind == "file_contains":
        path = _inside(root, verifier["path"])
        if not path.is_file():
            return "FAIL", digest, "file missing"
        ok = verifier["text"] in path.read_text(encoding="utf-8", errors="replace")
        return ("PASS" if ok else "FAIL"), digest, "" if ok else "text not found"
    if kind == "file_sha256":
        path = _inside(root, verifier["path"])
        ok = path.is_file() and sha256_file(path) == verifier["sha256"]
        return ("PASS" if ok else "FAIL"), digest, "" if ok else "hash differs"
    if kind == "document":
        path = _inside(root, verifier["path"])
        if not path.is_file() or path.is_symlink():
            return "FAIL", digest, "file missing"
        text = path.read_text(encoding="utf-8", errors="replace")
        problems = []
        if len(text.strip()) < int(verifier.get("min_chars", 1)):
            problems.append(f"shorter than {verifier.get('min_chars', 1)} characters ({len(text.strip())})")
        for heading in verifier.get("headings", []):
            if not re.search(r"^#{1,6}\s*" + re.escape(heading) + r"\b", text, re.M | re.I):
                problems.append(f"heading missing: {heading}")
        low = text.lower()
        for word in verifier.get("forbid", PLACEHOLDERS):
            if word.lower() in low:
                problems.append(f"placeholder text left in: {word}")
        return ("PASS" if not problems else "FAIL"), digest, "; ".join(problems)
    if kind in ("judge", "user_approval"):
        # These need the supervisor (a budgeted model call / the user); see judging.py.
        observe(verifier,False,"approval_or_judge_not_run")
        return "FAIL", digest, "not verified here"
    if kind in ("command", "red_test"):
        code, output = _run_gated(verifier, root, on_spawn, on_exit)
        return judge_command(verifier, root, code, output, digest)
    raise LupusError("VERIFIER_UNKNOWN", str(kind))


def judge_command(verifier: Mapping, root: Path, code: int | None, output: str, digest: str) -> tuple[str, str, str]:
    """Verdict for a command that already ran (so a caller that needs the full output for something
    else does not have to run it twice)."""
    kind = verifier["kind"]
    def observed(result,genuine,reason):
        observe(verifier,genuine,reason)
        return result
    if code is None:
        return observed(("FAIL", digest, "verifier timed out"),False,"timeout")
    if code == GATE_EXEC_FAILED:
        return observed(("FAIL", digest, "verifier command could not be started"),False,"unavailable")
    if code == OUTPUT_TOO_LARGE:
        return observed(("FAIL", digest, f"the check printed more than {MAX_OUTPUT // (1024 * 1024)} MB, too much to verify"),False,"capture_incomplete")
    runner = verifier.get("require_tests", "")
    tail = "\n".join(output.strip().splitlines()[-15:])[-700:]
    if kind == "red_test":
        return observed(_judge_red(verifier, root, code, output, digest, tail),code>=0,"red_check_completed" if code>=0 else "signalled")
    if verifier.get("require_no_skips"):
        omitted = runners.unexecuted(runner, output)
        if omitted is None:
            return observed(("FAIL", digest, "could not confirm that every acceptance test ran"),False,"test_execution_unknown")
        if omitted:
            return observed(("FAIL", digest, f"{omitted} acceptance tests were skipped, expected to fail, or deselected"),False,"tests_not_executed")
    if code == 0:
        # "exit 0" from a runner that found nothing to run is not a pass.
        count = runners.passed(runner, output) if runner else None
        if count is not None:
            if count == 0:
                return observed(("FAIL", digest, "no tests ran"),False,"tests_not_executed")
            if count < int(verifier.get("min_tests", 1)):
                # fewer tests ran than the approved check contains: something deselected them
                return observed(("FAIL", digest, f"only {count} of at least {verifier['min_tests']} tests ran"),False,"tests_not_executed")
        return observed(("PASS", digest, "exit 0"),True,"command_completed")
    allowed = verifier.get("allowed_failures")
    if allowed:
        # The project's tests were not all passing when the request was made. The same named
        # tests may still fail; any other failure, or a run that names none, is a failure.
        failed = runners.failed_ids(runner, output)
        if failed and failed <= set(allowed):
            return observed(("PASS", digest, f"exit {code}: only tests that already failed before the request still fail ({len(failed)})"),True,"command_completed")
    # Enough of the failure for the next attempt to act on, not the whole log.
    return observed(("FAIL", digest, f"exit {code}: {tail}"),code>=0,"command_completed" if code>=0 else "signalled")
