"""Small shared helpers: errors, canonical hashing, durable file writes, crash points."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


# Exec gate used for every process the supervisor starts inside a project (see gate.py).
GATE = str(Path(__file__).resolve().with_name("gate.py"))
GATE_EXEC_FAILED = 127


def gate_argv(command: list[str]) -> list[str]:
    """Start only the trusted gate before authority/confinement is established.

    The target keeps its environment after exec. The gate itself must not import a
    project's PYTHONPATH/sitecustomize or a virtualenv's executable .pth files.
    """
    return [sys.executable, "-I", "-S", GATE, *command]


class LupusError(Exception):
    """A refused command. `code` is the stable machine-readable reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# SQLite INTEGER is signed 64-bit. Do not clamp user durations or derived lease totals.
MAX_ACCOUNTING_MS = (1 << 63) - 1
# CPython socket/lock waits share a finite platform timeout representation.
MAX_TIMEOUT_S = min(threading.TIMEOUT_MAX, MAX_ACCOUNTING_MS / 1000)


def checked_ms_sum(*values: int) -> int:
    if any(type(value) is not int or value < 0 for value in values):
        raise LupusError("TIMEOUT_INVALID", "duration accounting must use nonnegative integer milliseconds")
    total = sum(values)
    if total > MAX_ACCOUNTING_MS:
        raise LupusError("TIMEOUT_INVALID", "duration accounting exceeds signed 64-bit milliseconds")
    return total


def timeout_ms(seconds: float, *, extra_ms: int = 0, clock_ms: int = 0) -> int:
    """Validate finite positive seconds and exact existing millisecond accounting."""
    if (type(seconds) not in (int, float) or seconds <= 0 or
            seconds > MAX_TIMEOUT_S or not math.isfinite(seconds)):
        raise LupusError("TIMEOUT_INVALID", "timeout must be finite, positive and representable in milliseconds")
    milliseconds = int(seconds * 1000)
    total = checked_ms_sum(milliseconds, extra_ms)
    checked_ms_sum(clock_ms, total)
    return total


def language() -> str:
    """The language people read Lupus in: "ko" when LUPUS_LANG, LC_ALL, LC_MESSAGES or LANG (the
    first one set) is Korean, otherwise "en"."""
    for name in ("LUPUS_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            return "ko" if value.lower().startswith("ko") else "en"
    return "en"


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
# Read-only parts of language toolchains that commonly live in the home directory. Only the
# directories that hold programs and packages: NOT ~/.cargo, ~/.gradle or ~/.m2 as a whole, which
# also hold registry tokens and repository passwords.
_HOME_TOOLCHAINS = (".pyenv", ".local/lib", ".local/share/uv", "Library/Python", ".nvm", ".cargo/bin", ".cargo/registry",
                    ".rustup", ".asdf", "go/pkg", ".gradle/caches", ".gradle/wrapper", ".m2/repository")
# Directories no verifier may read or write, whatever else the profile allows: Lupus's own state
# (database, recovery objects, frozen copies). Kernel registers its home here.
CONTAINER_CONTROL_ROOT = str(Path.home() / ".lupus-container-lifetimes")
PROTECTED_STATE: set[str] = {CONTAINER_CONTROL_ROOT}
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


def active_python_env() -> str | None:
    """The controller's selected virtualenv, never a worker-created environment.

    An explicit virtualenv interpreter wins over an inherited VIRTUAL_ENV. A global
    entrypoint can still honour the user's activated environment. Whole home/root grants
    are never inferred from either signal.
    """
    candidates = ([sys.prefix] if sys.prefix != sys.base_prefix else []) + [os.environ.get("VIRTUAL_ENV", "")]
    if not any(candidates):
        return None
    shared = {os.path.realpath(p) for p in ("/tmp", "/private/tmp", "/private/var/folders", tempfile.gettempdir())}
    for candidate in filter(None, candidates):
        prefix = os.path.realpath(candidate)
        python = os.path.join(prefix, "bin", "python")
        if (not Path.home().resolve().is_relative_to(prefix) and prefix not in shared
                and os.path.isabs(candidate) and os.path.isfile(os.path.join(prefix, "pyvenv.cfg"))
                and os.path.isfile(python) and os.access(python, os.X_OK)):
            return prefix
    return None


def active_python_read_roots() -> list[str]:
    prefix = active_python_env()
    if not prefix:
        return []
    base = os.path.realpath(sys.base_prefix)
    shared = {os.path.realpath(p) for p in ("/tmp", "/private/tmp", "/private/var/folders", tempfile.gettempdir())}
    roots = [prefix]
    if not Path.home().resolve().is_relative_to(base) and base not in shared and os.path.isdir(base):
        roots.append(base)
    return sorted(set(roots))


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


def sandbox_profile(project: Path, executable: str | None = None, extra_read: tuple[str, ...] = ()) -> str:
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
    readable += _python_prefix(executable, home) + [os.path.realpath(p) for p in extra_read]
    state = sorted(os.path.realpath(p) for p in PROTECTED_STATE)
    return "\n".join([
        "(version 1)", "(allow default)",
        # Descendants inherit this sandbox instance; independent peers and the unsandboxed
        # controller/lifetime anchors must not receive signals from untrusted code.
        "(deny signal)", "(allow signal (target same-sandbox))",
        "(deny network*)",
        # Loopback only. (A combined `network*` rule with a local filter also matches outbound
        # connections to anywhere; these three do not — checked by a test.)
        '(allow network-bind (local ip "localhost:*"))', '(allow network-inbound (local ip "localhost:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
        "(deny file-write*)", "(allow file-write* " + " ".join(f"(subpath {_sb(p)})" for p in writable) + ")",
        f"(deny file-read* (subpath {_sb(home)}))",
        f"(allow file-read-metadata (subpath {_sb(home)}))",
        "(allow file-read* " + " ".join(f"(subpath {_sb(p)})" for p in readable) + ")",
        *[f"(deny file-read* file-write* (subpath {_sb(p)}))" for p in state],
        # Git metadata can make git run things (hooks, config). Code under test does not get to write it.
        f"(deny file-write* (subpath {_sb(os.path.join(root, '.git'))}))",
    ])


def _docker() -> str | None:
    return shutil.which("docker", path=safe_path())


def container_alive(name: str) -> bool:
    """Whether a container Lupus started still exists. If Docker cannot be asked, the answer is
    "yes": a writer that cannot be shown to be gone keeps the project's slot."""
    if name.startswith('v2.'):
        from .container_lifetime import state
        return state(name)!='gone'
    # A historical name carries neither daemon identity nor immutable CID/nonce.
    # Querying today's default daemon cannot prove the old resource absent.
    return True


def container_stop(name: str) -> bool:
    """Remove only bound CID/nonce ownership; legacy names remain unknown."""
    if name.startswith('v2.'):
        from .container_lifetime import cleanup
        return cleanup(name)
    # Historical random names alone are not durable destructive ownership.
    # Keep their conservative observation/fence; never remove a reused name.
    return not container_alive(name)


# What each CLI itself must reach in the home directory to run and to use its own login.
_WORKER_HOME = {
    "codex": {"read": (".codex", ".agents/skills", ".local/bin"), "write": (),
              "never_write": (".codex", ".agents/skills")},
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


def sandbox_profile_worker(project: Path, cli: str, read_exclude: tuple[str, ...] = (), shared_tmp: bool = True,
                           readonly: bool = False, private_tmp: Path | None = None) -> str:
    """Seatbelt profile for a headless worker CLI. The network stays open (the CLI talks to its
    provider), everything else follows the verifier profile: writes only in the project, the
    temp directories and the CLI's own state; of the home directory only the project, the CLI's
    own files and its login are readable; Lupus's state is out of reach. This holds whatever the
    CLI's own permission system decides, including for commands the model runs through it.
    Authors set shared_tmp=False: shared temp trees are denied except the workspace and
    Claude's fixed per-user scratch directory (which may contain other sessions)."""
    home = os.path.realpath(Path.home())
    root = os.path.realpath(project)
    tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    own = _WORKER_HOME[cli]
    literal = lambda rel: os.path.join(home, rel)
    writable = [root, tmp, "/private/tmp", "/private/var/folders", "/dev"] + [literal(r) for r in own["write"]]
    if not shared_tmp:
        writable = [root, "/dev"] + [literal(r) for r in own["write"]]
        if cli == "claude":
            writable += [f"/tmp/claude-{os.getuid()}", f"/private/tmp/claude-{os.getuid()}"]
    if private_tmp is not None:
        writable.append(os.path.realpath(private_tmp))
    readable = [root] + [literal(r) for r in own["read"]] + [literal(t) for t in _HOME_TOOLCHAINS if os.path.isdir(literal(t))]
    if private_tmp is not None:
        readable.append(os.path.realpath(private_tmp))
    python_env = active_python_env()
    if python_env:
        readable += active_python_read_roots()
    if not shared_tmp and cli == "claude":
        readable += [f"/tmp/claude-{os.getuid()}", f"/private/tmp/claude-{os.getuid()}"]
    state = sorted(os.path.realpath(p) for p in PROTECTED_STATE | set(read_exclude))
    rule = lambda paths: " ".join(f"(subpath {_sb(p)})" for p in paths)
    temp_rules = [] if shared_tmp else ["(deny file-read* " + rule([tmp, "/tmp", "/private/tmp", "/private/var/folders"]) + ")"]
    if not shared_tmp:
        # Measured under the real sandbox: recursive mkdir needs to stat these roots.
        # Metadata alone permits mkdir/write while listing stays denied; root data access
        # would permit listing /private/tmp, so it is not granted.
        temp_rules.append('(allow file-read-metadata (literal "/tmp") (literal "/private/tmp") (literal "/private"))')
    return "\n".join([
        "(version 1)", "(allow default)",
        # Descendants inherit this sandbox instance; independent peers and the unsandboxed
        # controller/lifetime anchors must not receive signals from untrusted code.
        "(deny signal)", "(allow signal (target same-sandbox))",
        "(deny file-write*)",
        f"(deny file-read* (subpath {_sb(home)}))",
        f"(allow file-read-metadata (subpath {_sb(home)}))",
        *temp_rules,
        # Exceptions follow the temp deny, including workspaces inside the system temp tree.
        f"(allow file-write* {rule(writable)})",
        f"(allow file-read* (literal {_sb(home)}) {rule(readable)})",
        *([f"(deny file-write* (subpath {_sb(python_env)}))"] if python_env else []),
        f"(deny file-write* {rule([literal(r) for r in own['never_write']] + [os.path.join(root, '.git')])})",
        *[f"(deny file-read* file-write* (subpath {_sb(p)}))" for p in state],
        *([f"(deny file-write* (subpath {_sb(root)}))"] if readonly else []),
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


MAX_DIAGNOSTIC_TAIL = 2000
_DIAGNOSTIC_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff]")
_DIAGNOSTIC_SECRET = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*\S+|bearer\s+\S+|"
    r"(?:password|secret|token|api[_-]?key)\s*[\"']?\s*[:=]\s*\S+)")


def diagnostic_tail(text: str, *, limit: int = MAX_DIAGNOSTIC_TAIL, multiline: bool = False) -> str:
    """Bounded inert stderr for durable records; credential screening is heuristic.

    Screen before truncating so a recognised credential cannot be cut into an unrecognised
    suffix. Withhold the whole diagnostic if a known credential shape or assignment occurs.
    """
    if find_secret(text) or _DIAGNOSTIC_SECRET.search(text):
        return "[failure text withheld: credential pattern]"
    safe = _DIAGNOSTIC_CONTROLS.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)
    return (safe if multiline else safe.replace("\n", " ").replace("\t", " "))[-limit:]
