"""Kernel: the operational database, transactions, clock, audit log.

The supervisor process is the only writer of this database. Models, hooks and the Vault
exporter never receive a handle to it (docs/CONTRACT.md, "Authority").
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterator

from .util import PROTECTED_STATE, LupusError, canonical_json, crash_point, sha256_bytes, sha256_json

# SQLite <= 3.51.2 can corrupt a WAL database when connections write/checkpoint concurrently
# (https://www.sqlite.org/wal.html#walresetbug). Refuse to run on an affected library.
MIN_SQLITE = (3, 51, 3)

DEFAULT_POLICY = {
    "max_attempts_per_hypothesis": 2,   # §6.3 initial policy draft
    "no_progress_limit": 2,
    "max_interrupted_retries": 2,       # retries of an attempt that crashed before any verdict
    "lease_ttl_ms": 120_000,
    "handoff_ttl_ms": 3_600_000,
    "orphan_grace_ms": 3_600_000,
    "memory_recall_tokens": 1200,       # §11.1: added memory stays well under 3,000 tokens; 0 = off
    "memory_max_nodes": 6,
    "batch_max_tasks": 3,               # tasks one worker call may cover on drivers that batch; 1 = off
    "context_inline_bytes": 12_000,     # task files handed over in the prompt; 0 = off
    "memory_min_overlap": 2,            # distinct topic tokens a node must share with the task
    "session_stop_blocks": 3,           # times an interactive session's own check may send the model back to work
}


def migrations() -> list[tuple[int, str, str, str]]:
    """(version, name, sql, checksum) in order. The numbered files under migrations/ are the
    schema; an applied file never changes, new schema arrives as a new file."""
    out = []
    for entry in sorted(resources.files("lupus").joinpath("migrations").iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".sql"):
            sql = entry.read_text(encoding="utf-8")
            out.append((int(entry.name[:4]), entry.name, sql, sha256_bytes(sql.encode("utf-8"))))
    if [m[0] for m in out] != list(range(1, len(out) + 1)):
        raise LupusError("MIGRATIONS_INVALID", str([m[1] for m in out]))
    return out


SCHEMA_VERSION = len(migrations())


class Kernel:
    def __init__(self, home: Path, clock: Callable[[], int] | None = None):
        self.home = Path(home)
        self.runtime = self.home / "runtime"
        self.db_path = self.runtime / "lupus.db"
        self.objects_dir = self.runtime / "objects"
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._depth = 0
        self._now: int | None = None
        self._lock_fd: int | None = None
        PROTECTED_STATE.add(os.path.realpath(self.home))    # no verifier may touch this, see util.sandbox_profile
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise LupusError(
                "SQLITE_TOO_OLD",
                f"{sqlite3.sqlite_version} is affected by the WAL-reset bug; need >= 3.51.3",
            )
        if not self.db_path.exists():
            raise LupusError("NOT_INITIALIZED", str(self.home))
        self.conn = self._connect()
        self._check_schema()

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def init(cls, home: Path, clock: Callable[[], int] | None = None) -> "Kernel":
        home = Path(home)
        runtime = home / "runtime"
        for d in (home, runtime, runtime / "objects", runtime / "objects" / "tmp"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        db_path = runtime / "lupus.db"
        (home / "vault").mkdir(mode=0o700, exist_ok=True)   # the dedicated knowledge vault
        if not db_path.exists():
            conn = sqlite3.connect(db_path, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(_MIGRATION_TABLE)
                now = int(time.time() * 1000)
                for version, name, sql, checksum in migrations():
                    _apply(conn, version, name, sql, checksum, now)
                meta = {
                    "recovery_epoch": "1",
                    "next_fencing_token": "1",
                    "next_order_seq": "1",
                    "clock_high_water": "0",
                    "policy": canonical_json(DEFAULT_POLICY),
                }
                conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta.items())
                conn.execute("COMMIT")
            finally:
                conn.close()
        return cls(home, clock)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        got = {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        }
        if got != {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}:
            conn.close()
            raise LupusError("DB_PRAGMA_REJECTED", str(got))
        return conn

    def _check_schema(self) -> None:
        """Refuse a database from a newer Lupus or one whose applied schema was edited; bring an
        older one forward, after a consistent backup, one transaction per migration."""
        applied = {r["version"]: r["checksum"] for r in self.q("SELECT version, checksum FROM schema_migration")}
        known = migrations()
        if applied and max(applied) > len(known):
            raise LupusError("SCHEMA_TOO_NEW", f"db={max(applied)} code={len(known)}")
        for version, name, _, checksum in known:
            if version in applied and applied[version] != checksum:
                raise LupusError("SCHEMA_CHECKSUM_MISMATCH", f"{name} changed after it was applied")
        pending = [m for m in known if m[0] not in applied]
        if not pending:
            return
        backup = self.db_path.with_name(f"lupus.db.before-v{pending[0][0]}")
        if not backup.exists():
            target = sqlite3.connect(backup)
            try:
                self.conn.backup(target)     # consistent copy including WAL contents
            finally:
                target.close()
        for version, name, sql, checksum in pending:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                _apply(self.conn, version, name, sql, checksum, int(time.time() * 1000))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ transactions

    @contextmanager
    def tx(self) -> Iterator[None]:
        """BEGIN IMMEDIATE … COMMIT. Nested use joins the outer transaction, so every public
        command is atomic: state, revision counters, audit events and idempotency results
        commit together or not at all."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            self._now = self._advance_clock()
            yield
            crash_point("db.before_commit")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0
            self._now = None

    def _advance_clock(self) -> int:
        """Time never moves backwards inside the DB, even if the wall clock does."""
        high = int(self.meta("clock_high_water"))
        now = max(self._clock(), high)
        if now > high:
            self.set_meta("clock_high_water", str(now))
        return now

    @contextmanager
    def supervisor_lock(self) -> Iterator[None]:
        """One supervisor per runtime. Startup recovery closes runs and settles holds it finds
        open; that is only sound if no other supervisor is alive and using them. The lock is an
        flock, so it disappears with the process that held it."""
        if self._lock_fd is not None:      # re-entrant within this kernel
            yield
            return
        fd = os.open(self.runtime / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LupusError("SUPERVISOR_BUSY", "another supervisor is running on this runtime") from None
        self._lock_fd = fd
        try:
            yield
        finally:
            self._lock_fd = None
            os.close(fd)

    @property
    def in_tx(self) -> bool:
        return self._depth > 0

    def now(self) -> int:
        if self._now is not None:
            return self._now
        return max(self._clock(), int(self.meta("clock_high_water")))

    # ------------------------------------------------------------ helpers

    def q(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, *params: Any) -> int:
        return self.conn.execute(sql, params).rowcount

    def meta(self, key: str) -> str:
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        if row is None:
            raise LupusError("META_MISSING", key)
        return row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.run(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key, value,
        )

    def next_counter(self, key: str) -> int:
        """Monotonic counter; must be called inside a transaction."""
        value = int(self.meta(key))
        self.set_meta(key, str(value + 1))
        return value

    @property
    def policy(self) -> dict[str, int]:
        return {**DEFAULT_POLICY, **json.loads(self.meta("policy"))}

    @property
    def recovery_epoch(self) -> int:
        return int(self.meta("recovery_epoch"))

    def emit(self, actor: str, type_: str, aggregate: str, aggregate_id: str, **payload: Any) -> None:
        self.run(
            "INSERT INTO event(ts, actor, type, aggregate, aggregate_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            self.now(), actor, type_, aggregate, aggregate_id, canonical_json(payload),
        )

    def idempotent(self, request_id: str, op: str, payload: Any, fn: Callable[[], Any]) -> Any:
        """Run `fn` once per request_id. A replay returns the stored result; the same id with a
        different payload is refused."""
        digest = sha256_json(payload)
        with self.tx():
            row = self.one("SELECT op, payload_hash, result FROM request WHERE request_id = ?", request_id)
            if row is not None:
                if row["op"] != op or row["payload_hash"] != digest:
                    raise LupusError("IDEMPOTENCY_CONFLICT", request_id)
                return json.loads(row["result"])
            result = fn()
            self.run(
                "INSERT INTO request(request_id, op, payload_hash, result, created_at) VALUES (?,?,?,?,?)",
                request_id, op, digest, canonical_json(result), self.now(),
            )
            return result


_MIGRATION_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migration ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at INTEGER NOT NULL) STRICT"
)


def _apply(conn: sqlite3.Connection, version: int, name: str, sql: str, checksum: str, now: int) -> None:
    # executescript would commit implicitly; run statements inside the caller's transaction.
    for stmt in _split_statements(sql):
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_migration(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                 (version, name, checksum, now))


def _split_statements(sql: str) -> list[str]:
    """Split schema.sql into statements, keeping CREATE TRIGGER … END; bodies intact."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.endswith("END;"):
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        raise LupusError("SCHEMA_PARSE", "unterminated statement")
    return statements
