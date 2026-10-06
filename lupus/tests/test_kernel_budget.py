import json
import sqlite3

from lupus import budget, goals
from lupus.kernel import Kernel
from lupus.util import LupusError

from .helpers import CAPS, Env


class KernelTests(Env):
    def test_durability_pragmas_are_set_and_verified(self):
        self.assertEqual(self.k.one("PRAGMA journal_mode")[0], "wal")
        self.assertEqual(self.k.one("PRAGMA synchronous")[0], 2)
        self.assertEqual(self.k.one("PRAGMA fullfsync")[0], 1)
        self.assertEqual(self.k.one("PRAGMA foreign_keys")[0], 1)

    def test_database_from_a_newer_lupus_is_refused(self):
        self.k.conn.execute("INSERT INTO schema_migration VALUES (99, '0099_future.sql', 'x', 0)")
        self.k.close()
        with self.assertRaises(LupusError) as ctx:
            Kernel(self.home)
        self.assertEqual(ctx.exception.code, "SCHEMA_TOO_NEW")
        self._repair("DELETE FROM schema_migration WHERE version = 99")

    def test_edited_applied_migration_is_refused(self):
        self.k.conn.execute("UPDATE schema_migration SET checksum = 'tampered' WHERE version = 1")
        self.k.close()
        with self.assertRaises(LupusError) as ctx:
            Kernel(self.home)
        self.assertEqual(ctx.exception.code, "SCHEMA_CHECKSUM_MISMATCH")
        from lupus.kernel import migrations
        self._repair(f"UPDATE schema_migration SET checksum = '{migrations()[0][3]}' WHERE version = 1")

    def _repair(self, sql):
        raw = sqlite3.connect(self.home / "runtime" / "lupus.db")
        raw.execute(sql)
        raw.commit()
        raw.close()
        self.k = Kernel(self.home, self.clock)

    def test_older_database_is_backed_up_and_migrated_with_its_data(self):
        from lupus.kernel import SCHEMA_VERSION
        goal = self.goal()
        # Rebuild this database as it looked before the memory graph existed (schema v1).
        for table in ("protected_file", "protected_dir", "recall", "edge", "node_tombstone", "node_fts", "node"):
            self.k.conn.execute(f"DROP TABLE {table}")
        self.k.conn.execute("ALTER TABLE project DROP COLUMN allow_unconfined_reads")
        self.k.conn.execute("DELETE FROM schema_migration WHERE version > 1")
        self.reopen()
        self.assertEqual(self.k.one("SELECT MAX(version) FROM schema_migration")[0], SCHEMA_VERSION)
        self.assertEqual(self.k.one("SELECT objective FROM goal WHERE goal_id = ?", goal["goal_id"])[0], "demo goal")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM node")[0], 0)
        backup = sqlite3.connect(self.home / "runtime" / "lupus.db.before-v2")
        self.assertEqual(backup.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0], 1)
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM goal").fetchone()[0], 1)
        backup.close()

    def test_audit_log_is_append_only(self):
        goal = self.goal()
        for sql in ("UPDATE event SET actor='x'", "DELETE FROM event"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.k.conn.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):
            self.k.conn.execute("UPDATE acceptance SET criteria='[]' WHERE goal_id=?", (goal["goal_id"],))

    def test_failed_command_leaves_no_partial_state(self):
        before = self.k.one("SELECT COUNT(*) FROM event")[0]
        with self.assertRaises(RuntimeError):
            with self.k.tx():
                self.k.emit("t", "x", "a", "b")
                self.k.next_counter("next_fencing_token")
                raise RuntimeError("boom")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM event")[0], before)
        self.assertEqual(self.k.meta("next_fencing_token"), "1")

    def test_same_request_returns_same_result_and_different_payload_is_refused(self):
        calls = []
        fn = lambda: calls.append(1) or {"n": len(calls)}
        self.assertEqual(self.k.idempotent("r1", "op", {"a": 1}, fn), {"n": 1})
        self.assertEqual(self.k.idempotent("r1", "op", {"a": 1}, fn), {"n": 1})
        self.assertEqual(len(calls), 1)
        self.assertRefused("IDEMPOTENCY_CONFLICT", self.k.idempotent, "r1", "op", {"a": 2}, fn)

    def test_time_never_moves_backwards(self):
        with self.k.tx():
            first = self.k.now()
        self.clock.advance(-3_600_000)   # wall clock set back one hour
        with self.k.tx():
            self.assertGreaterEqual(self.k.now(), first)


class BudgetTests(Env):
    def setUp(self):
        super().setUp()
        self.bid = budget.create(self.k, "t", {"calls": 10, "attempts": 3, "active_ms": 1000})

    def line(self, dim="calls", bid=None):
        return budget.snapshot(self.k, bid or self.bid)[dim]

    def test_goal_without_caps_cannot_exist(self):
        self.assertRefused("BUDGET_CAP_REQUIRED", budget.create, self.k, "x", {"calls": 1})

    def test_reservation_must_fit_and_unbounded_dimension_is_refused(self):
        budget.reserve(self.k, self.bid, "work", "w", {"calls": 7})
        self.assertRefused("BUDGET_EXHAUSTED", budget.reserve, self.k, self.bid, "work", "w", {"calls": 4})
        self.assertRefused("DIMENSION_NOT_RESERVABLE", budget.reserve, self.k, self.bid, "work", "w", {"tokens": 1})

    def test_safety_reservation_cannot_be_spent_as_work(self):
        budget.reserve(self.k, self.bid, "safety", "verify", {"calls": 4})
        budget.reserve(self.k, self.bid, "work", "w", {"calls": 6})
        self.assertRefused("BUDGET_EXHAUSTED", budget.reserve, self.k, self.bid, "work", "w", {"calls": 1})
        self.assertEqual(self.line()["safety_reserved"], 4)

    def test_unknown_usage_is_charged_in_full_never_refunded(self):
        rid = budget.reserve(self.k, self.bid, "work", "w", {"calls": 5}, owner_run_id="run_x")
        self.assertEqual(budget.settle_orphans(self.k, "run_x"), 1)
        self.assertEqual(self.line()["used"], 5)
        self.assertEqual(self.line()["reserved"], 0)
        row = self.k.one("SELECT observation FROM reservation WHERE reservation_id=?", rid)
        self.assertEqual(row["observation"], "estimated")

    def test_settle_is_idempotent(self):
        rid = budget.reserve(self.k, self.bid, "work", "w", {"calls": 5})
        budget.settle(self.k, rid, {"calls": 2}, "measured")
        budget.settle(self.k, rid, {"calls": 5}, "measured")   # late duplicate: ignored
        self.assertEqual(self.line()["used"], 2)

    def test_overrun_is_recorded_and_blocks_new_work(self):
        rid = budget.reserve(self.k, self.bid, "work", "w", {"calls": 5})
        budget.settle(self.k, rid, {"calls": 14}, "measured")
        self.assertEqual(self.line()["used"], 14)   # reported, not clamped to the cap
        self.assertRefused("BUDGET_EXHAUSTED", budget.reserve, self.k, self.bid, "work", "w", {"attempts": 1})
        self.assertTrue(self.k.one("SELECT 1 FROM event WHERE type='budget.overrun'"))

    def test_child_budget_shares_the_parent_cap(self):
        child_a = budget.create(self.k, "a", CAPS, parent_budget_id=self.bid)
        child_b = budget.create(self.k, "b", CAPS, parent_budget_id=self.bid)
        budget.reserve(self.k, child_a, "work", "w", {"calls": 8})
        self.assertRefused("BUDGET_EXHAUSTED", budget.reserve, self.k, child_b, "work", "w", {"calls": 3})
        self.assertEqual(self.line()["reserved"], 8)

    def test_only_the_user_raises_a_cap_and_it_is_recorded(self):
        self.assertRefused("USER_AUTHORITY_REQUIRED", budget.raise_cap, self.k, self.bid, "calls", 99,
                           "supervisor", "need more")
        budget.raise_cap(self.k, self.bid, "calls", 99, "user", "approved")
        self.assertEqual(self.line()["cap"], 99)
        self.assertEqual(self.k.one("SELECT old_cap, new_cap FROM budget_change")["new_cap"], 99)
