"""Deterministic verifiers (§12.2). They run in the supervisor, cost no model call, and are the
only source of completion evidence. The verifier of a criterion is part of the acceptance
revision, so a worker cannot swap it for an easier one."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from .util import (
    GATE, GATE_EXEC_FAILED, SANDBOX_EXEC, LupusError, sandbox_available, sandbox_profile, scrubbed_env, sha256_file,
    sha256_json, stop_group,
)

VERSION = "1"


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
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            item = Path(current) / name
            rel = os.path.relpath(item, path)
            if item.is_symlink():
                entries.append([rel, "symlink:" + os.readlink(item)])   # a link appearing/retargeting is a change
            elif item.is_file():
                entries.append([rel, sha256_file(item)])
            if len(entries) > _MAX_DIR_FILES:
                raise LupusError("VERIFIER_PATH_TOO_LARGE", f"{path.name}: more than {_MAX_DIR_FILES} files")
    return sha256_json(entries)


_RAN = {"unittest": re.compile(r"^Ran (\d+) tests?", re.M), "pytest": re.compile(r"(\d+) passed")}


def artifact_hash(verifier: Mapping, root: Path) -> str:
    """Hash of what the verifier looks at. Evidence is reusable only while this is unchanged."""
    paths = [verifier["path"]] if "path" in verifier else list(verifier.get("paths", []))
    return sha256_json([[p, _hash(_inside(root, p))] for p in sorted(paths)])


def timeout_s(verifier: Mapping) -> float:
    """Upper bound of one verification; this is what gets reserved before a change starts."""
    return float(verifier.get("timeout_s", 120)) if verifier["kind"] in ("command", "red_test") else 5.0


_NOT_COLLECTED = re.compile(r"_FailedTest|Failed to import test module|ERROR collecting|errors? during collection")
_BROKEN_TEST = re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b")
_RED = {"unittest": re.compile(r"^Ran ([1-9]\d*) tests?", re.M), "pytest": re.compile(r"([1-9]\d*) (?:failed|errors?)")}


def _judge_red(verifier: Mapping, root: Path, code: int, output: str, digest: str, tail: str) -> tuple[str, str, str]:
    """A newly written acceptance test is useful only if it can tell "not done" from "done".
    PASS means: the file exists, at least one of its tests really ran, and it FAILS on the code
    as it is now, for a reason other than the test file itself being broken. It does not mean
    the test captures the request; that is what the user's approval is for."""
    if not _inside(root, verifier["path"]).is_file():
        return "FAIL", digest, f"the test file {verifier['path']} was not created"
    if code == 0:
        return "FAIL", digest, "the new test already passes on the current code, so it cannot show that the request was done"
    if _BROKEN_TEST.search(output):
        return "FAIL", digest, f"the test file does not parse: {tail}"
    if _NOT_COLLECTED.search(output):
        # The file could not even be loaded (failed import at module level, collection error):
        # no behaviour was exercised, so nothing was shown to be missing.
        return "FAIL", digest, ("the test file failed to load, so no test body ran. Import what does not exist yet "
                                f"inside the test functions, not at the top of the file: {tail}")
    ran = _RED.get(verifier.get("require_tests", ""))
    if ran is not None and not ran.search(output):
        return "FAIL", digest, f"no test from the new file ran: {tail}"
    return "PASS", digest, "red on the current code: " + tail[-300:]


def _run_gated(verifier: Mapping, root: Path, on_spawn, on_exit) -> tuple[int | None, str]:
    """Run a verifier command the way a worker is run: own process group behind the exec gate,
    recorded before it starts, emptied before returning, scrubbed environment, bytecode compiled
    from source. Returns (exit code or None on timeout, captured output)."""
    argv = list(verifier["argv"])
    sandboxed = verifier.get("sandbox", True)
    if sandboxed:
        # The command runs code a model wrote. Confine it: no outbound network, no writes outside
        # the project/temp, no reading of the home directory (see util.sandbox_profile). If the
        # sandbox cannot be applied the check does NOT run: dropping the protection silently is
        # not an option; only the user can switch it off for a criterion (`sandbox: false`).
        if not sandbox_available():
            raise LupusError("VERIFIER_SANDBOX_UNAVAILABLE", "the OS sandbox cannot be applied on this machine")
        argv = [SANDBOX_EXEC, "-p", sandbox_profile(root, argv[0]), *argv]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryDirectory(prefix="lupus-pyc-") as pyc:
        proc = subprocess.Popen(
            [sys.executable, GATE, *argv], cwd=root, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, start_new_session=True,
            env=scrubbed_env({"PYTHONPYCACHEPREFIX": pyc, "PYTHONDONTWRITEBYTECODE": "1"},
                             verifier.get("env_pass", ())),
        )
        timed_out = False
        try:
            if on_spawn:
                on_spawn(proc.pid)
            proc.stdin.write(b"g")
            proc.stdin.close()
            proc.wait(timeout=timeout_s(verifier))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            emptied = stop_group(proc.pid, grace_s=1.0)
            proc.wait()
        if not emptied:
            raise LupusError("VERIFIER_CLEANUP_FAILED", str(proc.pid))
        if on_exit:
            on_exit(proc.pid)
        out.seek(0)
        output = out.read(65536).decode("utf-8", errors="replace")
        if sandboxed and not timed_out and output.lstrip().startswith("sandbox-exec:"):
            # The sandbox itself failed to start the command: that is not a test result.
            raise LupusError("VERIFIER_SANDBOX_FAILED", output.strip()[:200])
        return (None if timed_out else proc.returncode), output


def run(
    verifier: Mapping,
    root: Path,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> tuple[str, str, str]:
    """Returns (PASS|FAIL, artifact_hash, short detail).

    A `command` verifier executes code inside the project, so it is treated like a worker: it
    runs in its own process group behind the exec gate, `on_spawn(pid)` durably records that
    group before the command may start, the group is emptied before returning, and `on_exit`
    is called only after that was confirmed. If the group cannot be emptied this raises, and
    the writer slot stays taken.
    """
    kind = verifier["kind"]
    digest = artifact_hash(verifier, root)
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
    if kind in ("command", "red_test"):
        code, output = _run_gated(verifier, root, on_spawn, on_exit)
        if code is None:
            return "FAIL", digest, "verifier timed out"
        if code == GATE_EXEC_FAILED:
            return "FAIL", digest, "verifier command could not be started"
        pattern = _RAN.get(verifier.get("require_tests", ""))
        tail = "\n".join(output.strip().splitlines()[-15:])[-700:]
        if kind == "red_test":
            return _judge_red(verifier, root, code, output, digest, tail)
        if code == 0:
            # "exit 0" from a runner that found nothing to run is not a pass.
            if pattern is not None:
                counts = [int(n) for n in pattern.findall(output)]
                if not counts or max(counts) == 0:
                    return "FAIL", digest, "no tests ran"
                if max(counts) < int(verifier.get("min_tests", 1)):
                    # fewer tests ran than the approved check contains: something deselected them
                    return "FAIL", digest, f"only {max(counts)} of at least {verifier['min_tests']} tests ran"
            return "PASS", digest, "exit 0"
        # Enough of the failure for the next attempt to act on, not the whole log.
        return "FAIL", digest, f"exit {code}: {tail}"
    raise LupusError("VERIFIER_UNKNOWN", str(kind))
