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
from pathlib import Path
from typing import Callable, Mapping

from . import runners
from .util import (
    GATE, GATE_EXEC_FAILED, SANDBOX_EXEC, LupusError, container_stop, safe_path, sandbox_available, sandbox_profile,
    scrubbed_env, sha256_file,
    sha256_json, stop_group,
)

VERSION = "1"
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


def _run_gated(verifier: Mapping, root: Path, on_spawn, on_exit) -> tuple[int | None, str]:
    """Run a verifier command the way a worker is run: own process group behind the exec gate,
    recorded before it starts, emptied before returning, scrubbed environment, bytecode compiled
    from source. Returns (exit code or None on timeout, captured output)."""
    argv = list(verifier["argv"])
    container = None
    if verifier.get("container"):
        # A separate kernel namespace instead of the host sandbox: no network, the project mounted
        # as the only host directory, no extra privileges. The image is the user's choice.
        docker = shutil.which("docker", path=safe_path())
        if docker is None:
            raise LupusError("VERIFIER_CONTAINER_UNAVAILABLE", "docker was not found on PATH")
        container = "lupus-" + secrets.token_hex(8)
        argv = [docker, "run", "--rm", "--name", container, "--label", "lupus=verifier", "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "2g",
                "-v", f"{os.path.realpath(root)}:/work", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
                *[x for name, value in sorted(verifier.get("env", {}).items()) for x in ("-e", f"{name}={value}")],
                verifier["container"], *argv]
    sandboxed = verifier.get("sandbox", True) and container is None
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
            env=scrubbed_env({**verifier.get("env", {}), "PYTHONPYCACHEPREFIX": pyc, "PYTHONDONTWRITEBYTECODE": "1",
                              "NO_COLOR": "1", "FORCE_COLOR": "0"},
                             verifier.get("env_pass", ())),
        )
        timed_out = False
        try:
            if on_spawn:
                # The container outlives its client: its name is recorded with the process group so
                # that recovery can stop it too (see runs.aux_recorder).
                on_spawn(proc.pid, f":container:{container}") if container else on_spawn(proc.pid)
            proc.stdin.write(b"g")
            proc.stdin.close()
            proc.wait(timeout=timeout_s(verifier))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                gone = container is None or container_stop(container)      # killing the client does not stop the container
            finally:
                emptied = stop_group(proc.pid, grace_s=1.0)
                proc.wait()
        if not emptied or not gone:
            raise LupusError("VERIFIER_CLEANUP_FAILED", container or str(proc.pid))
        if on_exit:
            on_exit(proc.pid)
        # The WHOLE output is read: a summary cut off at either end could be replaced by a look-alike
        # line. A run that prints more than can be read in full is not verified at all.
        if out.seek(0, os.SEEK_END) > MAX_OUTPUT:
            return OUTPUT_TOO_LARGE, ""
        out.seek(0)
        # Colour codes would sit between the words a summary is recognised by.
        output = _ANSI.sub("", out.read().decode("utf-8", errors="replace"))
        if container is not None and not timed_out and proc.returncode == 125:
            raise LupusError("VERIFIER_CONTAINER_FAILED", output.strip()[-200:])      # docker itself failed, not the tests
        if verifier.get("must_pass") and not timed_out and proc.returncode == 0:
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
        return "FAIL", digest, "not verified here"
    if kind in ("command", "red_test"):
        code, output = _run_gated(verifier, root, on_spawn, on_exit)
        if code is None:
            return "FAIL", digest, "verifier timed out"
        if code == GATE_EXEC_FAILED:
            return "FAIL", digest, "verifier command could not be started"
        if code == OUTPUT_TOO_LARGE:
            return "FAIL", digest, f"the check printed more than {MAX_OUTPUT // (1024 * 1024)} MB, too much to verify"
        runner = verifier.get("require_tests", "")
        tail = "\n".join(output.strip().splitlines()[-15:])[-700:]
        if kind == "red_test":
            return _judge_red(verifier, root, code, output, digest, tail)
        if code == 0:
            # "exit 0" from a runner that found nothing to run is not a pass.
            count = runners.passed(runner, output) if runner else None
            if count is not None:
                if count == 0:
                    return "FAIL", digest, "no tests ran"
                if count < int(verifier.get("min_tests", 1)):
                    # fewer tests ran than the approved check contains: something deselected them
                    return "FAIL", digest, f"only {count} of at least {verifier['min_tests']} tests ran"
            return "PASS", digest, "exit 0"
        # Enough of the failure for the next attempt to act on, not the whole log.
        return "FAIL", digest, f"exit {code}: {tail}"
    raise LupusError("VERIFIER_UNKNOWN", str(kind))
