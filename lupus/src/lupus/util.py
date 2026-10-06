"""Small shared helpers: errors, canonical hashing, durable file writes, crash points."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# Exec gate used for every process the supervisor starts inside a project (see gate.py).
GATE = str(Path(__file__).resolve().with_name("gate.py"))
GATE_EXEC_FAILED = 127


class LupusError(Exception):
    """A refused command. `code` is the stable machine-readable reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---------------------------------------------------------------- durability

def durable_fsync(fd: int) -> None:
    """Flush to stable storage. On macOS plain fsync() does not flush the drive cache, so use
    F_FULLFSYNC when the filesystem supports it."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        durable_fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write-temp, fsync, rename, fsync-dir. Readers never observe a half-written file."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            durable_fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


# ---------------------------------------------------------------- fault injection

def crash_point(name: str) -> None:
    """Die abruptly (SIGKILL, no cleanup handlers) when LUPUS_CRASH_AT names this point.

    This simulates process death at a commit boundary. It does not simulate power loss: data
    the OS already accepted survives, so fsync correctness is not proven by these tests.
    """
    if os.environ.get("LUPUS_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


# ---------------------------------------------------------------- processes

def proc_start(pid: int) -> str | None:
    """Start time of a live pid, or None when no such process exists (a zombie counts as gone:
    it can no longer write anything). Paired with the pid it distinguishes "our worker is still
    alive" from "the pid was reused"."""
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stat, _, start = out.stdout.strip().partition(" ")
    if not start.strip() or stat.startswith("Z"):
        return None
    return start.strip()


def group_alive(pgid: int) -> bool:
    """True if any non-zombie process remains in the process group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass    # macOS reports EPERM for a group that only holds zombies; decide from ps below
    # A group that only holds zombies still "exists"; zombies cannot write any more.
    try:
        out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return True
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid) and not parts[1].startswith("Z"):
            return True
    return False


# Only these variables reach a process started inside a project (workers AND verifiers).
# Everything else — provider keys, cloud credentials, base-URL overrides, tokens — is dropped,
# because code written by a model runs in both.
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
             "__CF_USER_TEXT_ENCODING")


def safe_path(value: str | None = None) -> str:
    """PATH without relative or empty entries. With '.' (or an empty entry) on PATH, a file named
    `claude`, `codex` or `python3` inside the project would be executed instead of the real one."""
    parts = (os.environ.get("PATH", "") if value is None else value).split(os.pathsep)
    # Never empty: an empty PATH makes exec look in the current directory, i.e. in the project.
    return os.pathsep.join(p for p in parts if p and os.path.isabs(p)) or "/usr/bin:/bin"


def scrubbed_env(extra: dict[str, str] | None = None, passthrough: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
    for name in passthrough:        # explicitly named by the user in an acceptance criterion
        if isinstance(name, str) and name in os.environ:
            env[name] = os.environ[name]
    env["PATH"] = safe_path(env.get("PATH"))
    env.setdefault("TERM", "dumb")
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- verifier sandbox (macOS)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Where a worker may leave a proposed implementation that is not yet part of the project (`lupus
# do`). No verifier can read it: a check must describe the code as it is, not as proposed.
STAGE_DIR = ".lupus-staged"
# Read-only parts of language toolchains that commonly live in the home directory. Only the
# directories that hold programs and packages: NOT ~/.cargo, ~/.gradle or ~/.m2 as a whole, which
# also hold registry tokens and repository passwords.
_HOME_TOOLCHAINS = (".pyenv", ".local/lib", ".local/share/uv", "Library/Python", ".nvm", ".cargo/bin", ".cargo/registry",
                    ".rustup", ".asdf", "go/pkg", ".gradle/caches", ".gradle/wrapper", ".m2/repository")
# Directories no verifier may read or write, whatever else the profile allows: Lupus's own state
# (database, recovery objects, frozen copies). Kernel registers its home here.
PROTECTED_STATE: set[str] = set()
_sandbox_ok = False


def sandbox_available() -> bool:
    """True when the OS sandbox can actually be applied here (checked by running it). Only a
    success is remembered; a failure is checked again next time."""
    global _sandbox_ok
    if not _sandbox_ok:
        try:
            _sandbox_ok = os.path.exists(SANDBOX_EXEC) and subprocess.run(
                [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _sandbox_ok = False
    return _sandbox_ok


def _sb(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _python_prefix(executable: str | None, home: str) -> list[str]:
    """The environment of the interpreter a verifier is about to run, when it lives under the
    home directory (a virtualenv, pyenv…). Only a directory that looks like a Python prefix and
    is strictly inside home is returned, never home itself."""
    out = []
    for exe in filter(None, [executable, sys.executable]):
        found = shutil.which(exe, path=safe_path()) or exe
        for candidate in {os.path.dirname(os.path.dirname(os.path.abspath(found))),
                          os.path.dirname(os.path.dirname(os.path.realpath(found)))}:
            inside = candidate.startswith(home + os.sep)
            if inside and (os.path.isfile(os.path.join(candidate, "pyvenv.cfg")) or os.path.isdir(os.path.join(candidate, "lib"))):
                out.append(candidate)
    return sorted(set(out))


def sandbox_profile(project: Path, executable: str | None = None) -> str:
    """Seatbelt profile for code that a model wrote and a verifier is about to run:
      * no network except this machine (localhost), so nothing can be sent out
      * writes only inside the project and the temp directories
      * the home directory is unreadable except the project itself, language toolchains and the
        interpreter's own environment
      * Lupus's own state is unreadable and unwritable, wherever it lives
    Later rules win, so each broad deny is followed by the narrow allows."""
    home = os.path.realpath(Path.home())
    root = os.path.realpath(project)
    tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    writable = [root, tmp, "/private/tmp", "/private/var/folders", "/dev"]
    readable = [root] + [os.path.join(home, t) for t in _HOME_TOOLCHAINS if os.path.isdir(os.path.join(home, t))]
    readable += _python_prefix(executable, home)
    state = sorted(os.path.realpath(p) for p in PROTECTED_STATE)
    return "\n".join([
        "(version 1)", "(allow default)",
        "(deny network*)",
        # Loopback only. (A combined `network*` rule with a local filter also matches outbound
        # connections to anywhere; these three do not — checked by a test.)
        '(allow network-bind (local ip "localhost:*"))', '(allow network-inbound (local ip "localhost:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
        "(deny file-write*)", "(allow file-write* " + " ".join(f"(subpath {_sb(p)})" for p in writable) + ")",
        f"(deny file-read* (subpath {_sb(home)}))",
        f"(allow file-read-metadata (subpath {_sb(home)}))",
        "(allow file-read* " + " ".join(f"(subpath {_sb(p)})" for p in readable) + ")",
        *[f"(deny file-read* file-write* (subpath {_sb(p)}))" for p in [*state, os.path.join(root, STAGE_DIR)]],
    ])


def _docker() -> str | None:
    return shutil.which("docker", path=safe_path())


def container_alive(name: str) -> bool:
    """Whether a container Lupus started still exists. If Docker cannot be asked, the answer is
    "yes": a writer that cannot be shown to be gone keeps the project's slot."""
    docker = _docker()
    if docker is None:
        return True
    try:
        proc = subprocess.run([docker, "ps", "-aq", "--filter", f"name=^{name}$"], capture_output=True, text=True,
                              timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return proc.returncode != 0 or bool(proc.stdout.strip())


def container_stop(name: str) -> bool:
    """Kill and remove the container; True once it is confirmed gone."""
    docker = _docker()
    if docker is not None:
        try:
            subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return not container_alive(name)


# What each CLI itself must reach in the home directory to run and to use its own login.
_WORKER_HOME = {
    "claude": {"read": (".claude", ".claude.json", ".local/bin", ".local/share/claude", ".local/state/claude", ".nvm",
                        ".npm-global", "Library/Keychains", "Library/Preferences", "Library/Application Support/Claude",
                        ".config/claude", ".cache/claude", "Library/Caches/claude-cli-nodejs"),
               "write": (".claude", ".local/state/claude", ".cache/claude", "Library/Caches/claude-cli-nodejs"),
               # …but never what decides which code a LATER, ordinary session of the user runs: settings and
               # hooks, commands, agents, skills, plugins, standing instructions, MCP server definitions.
               "never_write": (".claude/settings.json", ".claude/settings.local.json", ".claude/CLAUDE.md", ".claude/hooks",
                               ".claude/commands", ".claude/agents", ".claude/skills", ".claude/plugins",
                               ".claude/output-styles", ".claude/keybindings.json", ".claude/projects", ".claude.json")},
}


def sandbox_profile_worker(project: Path, cli: str) -> str:
    """Seatbelt profile for a headless worker CLI. The network stays open (the CLI talks to its
    provider), everything else follows the verifier profile: writes only in the project, the
    temp directories and the CLI's own state; of the home directory only the project, the CLI's
    own files and its login are readable; Lupus's state is out of reach. This holds whatever the
    CLI's own permission system decides, including for commands the model runs through it."""
    home = os.path.realpath(Path.home())
    root = os.path.realpath(project)
    tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    own = _WORKER_HOME[cli]
    literal = lambda rel: os.path.join(home, rel)
    writable = [root, tmp, "/private/tmp", "/private/var/folders", "/dev"] + [literal(r) for r in own["write"]]
    readable = [root] + [literal(r) for r in own["read"]] + [literal(t) for t in _HOME_TOOLCHAINS if os.path.isdir(literal(t))]
    state = sorted(os.path.realpath(p) for p in PROTECTED_STATE)
    rule = lambda paths: " ".join(f"(subpath {_sb(p)})" for p in paths)
    return "\n".join([
        "(version 1)", "(allow default)",
        "(deny file-write*)", f"(allow file-write* {rule(writable)})",
        f"(deny file-read* (subpath {_sb(home)}))",
        f"(allow file-read-metadata (subpath {_sb(home)}))",
        f"(allow file-read* (literal {_sb(home)}) {rule(readable)})",
        f"(deny file-write* {rule([literal(r) for r in own['never_write']])})",
        *[f"(deny file-read* file-write* (subpath {_sb(p)}))" for p in state],
    ])


def stop_group(pgid: int, grace_s: float = 5.0) -> bool:
    """Terminate a whole process group: SIGTERM, wait, SIGKILL. Returns True once it is empty."""
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return True
            time.sleep(0.05)
    return not group_alive(pgid)


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


# ---------------------------------------------------------------- content gate

# High-confidence credential shapes only. This is an additional defence in front of durable
# stores (recovery objects, events, Vault notes); it does not prove absence of secrets (§10.1).
_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]


def find_secret(text: str) -> str | None:
    """Name of the first credential pattern found, else None."""
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def find_secret_bytes(data: bytes) -> str | None:
    return find_secret(data.decode("utf-8", errors="ignore"))
