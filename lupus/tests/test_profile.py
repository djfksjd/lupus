import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager, nullcontext
from unittest import mock

from evaluations import issues, profile
from lupus import goals, quick, supervisor, timing
from lupus.kernel import SupervisorStopping
from lupus.util import LupusError, sandbox_available, sha256_bytes

from .helpers import Env, WRITER, fake
from .test_quick import CALC, GOOD_TEST, TEST


class TimingTests(unittest.TestCase):
    def kernel(self):
        k = mock.Mock()
        k._depth = 0
        k.conn.in_transaction = False
        k.tx.side_effect = nullcontext
        return k

    def test_timing_write_failure_preserves_outcome(self):
        for original in (None, LupusError("REFUSED"), RuntimeError("crash point"),
                         SupervisorStopping(), KeyboardInterrupt(), SystemExit(1)):
            for where in ("tx", "emit", "commit"):
                for write_error in (sqlite3.OperationalError("database is locked"),
                                    sqlite3.ProgrammingError("closed database"), SupervisorStopping()):
                    with self.subTest(original=original, where=where, write_error=write_error):
                        k = self.kernel()
                        if where == "commit":
                            @contextmanager
                            def tx():
                                yield
                                raise write_error
                            k.tx.side_effect = tx
                        else:
                            getattr(k, where).side_effect = write_error

                        @timing.operation("supervisor_bookkeeping")
                        def call(k, goal_id):
                            if original is not None:
                                raise original
                            return {"done": True}

                        if original is None:
                            self.assertEqual(call(k, "goal"), {"done": True})
                        else:
                            with self.assertRaises(type(original)) as caught:
                                call(k, "goal")
                            self.assertIs(caught.exception, original)
                            if not isinstance(original, Exception):
                                k.tx.assert_not_called()
                                k.emit.assert_not_called()
                        self.assertIsNone(timing._current.get())

    def test_timing_write_interrupt_propagates(self):
        for original in (None, RuntimeError("original failure")):
            for where in ("tx", "emit", "commit"):
                for interrupt in (KeyboardInterrupt(), SystemExit(7)):
                    with self.subTest(original=original, where=where, interrupt=interrupt):
                        k = self.kernel()
                        if where == "commit":
                            @contextmanager
                            def tx():
                                yield
                                raise interrupt
                            k.tx.side_effect = tx
                        else:
                            getattr(k, where).side_effect = interrupt

                        @timing.operation("supervisor_bookkeeping")
                        def call(k, goal_id):
                            if original is not None:
                                raise original
                            return {"done": True}

                        with self.assertRaises(type(interrupt)) as caught:
                            call(k, "goal")
                        self.assertIs(caught.exception, interrupt)
                        self.assertIsNone(timing._current.get())

    def test_timing_never_joins_an_open_transaction(self):
        for depth, in_transaction in ((1, True), (0, True)):
            for original in (None, LupusError("REFUSED")):
                with self.subTest(depth=depth, original=original):
                    k = self.kernel()

                    @timing.operation("acceptance")
                    def call(k, goal_id):
                        # The wrapped call leaves a managed or raw SQLite transaction open.
                        k._depth = depth
                        k.conn.in_transaction = in_transaction
                        if original is not None:
                            raise original
                        return {"done": True}

                    if original is None:
                        self.assertEqual(call(k, "goal"), {"done": True})
                    else:
                        with self.assertRaises(LupusError) as caught:
                            call(k, "goal")
                        self.assertIs(caught.exception, original)
                    k.tx.assert_not_called()
                    k.emit.assert_not_called()
                    self.assertIsNone(timing._current.get())

    def test_exact_accounting_with_scheduler_gap(self):
        k = self.kernel()

        @timing.operation("supervisor_bookkeeping")
        def call(k, goal_id):
            with timing.span("trial"):
                with timing.span("workspace"):
                    pass
            return {"done": True}

        ticks = [0, 1, 2, 3, 7, 10, 12, 13, 100, 101, 102, 103, 107, 110, 112, 113]
        with mock.patch.object(timing.time, "perf_counter_ns", side_effect=ticks), \
             mock.patch.object(timing.time, "time_ns", side_effect=[0, 100]):
            call(k, "goal")
            call(k, "goal")
        operations = [event.kwargs for event in k.emit.call_args_list]
        self.assertEqual(len(operations), 2)
        for event in operations:
            self.assertEqual(set(event["stages_ns"]), set(timing.STAGES))
            self.assertEqual(event["stages_ns"]["workspace"], 4)
            self.assertEqual(event["stages_ns"]["trial"], 4)
            self.assertEqual(event["stages_ns"]["supervisor_bookkeeping"], 5)
            self.assertEqual(sum(event["stages_ns"].values()), event["elapsed_ns"])
        result = profile.breakdown(operations)
        self.assertEqual(set(result["stages"]), set(timing.STAGES))
        self.assertAlmostEqual(result["unattributed_seconds"] * 1e9, 87)
        self.assertAlmostEqual(result["unattributed_share"], 87 / 113)
        self.assertFalse(result["target_met"])
        self.assertEqual(profile.breakdown(operations, 26 / 1e9)["unattributed_seconds"], 0)
        self.assertTrue(profile.breakdown(operations, 26 / 1e9)["target_met"])


class ProfileTests(Env):
    def test_fake_do_is_attributed_offline(self):
        if not sandbox_available():
            self.skipTest("OS verifier sandbox unavailable")
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(TEST)
        started = time.monotonic()
        draft = quick.draft_check(self.k, self.project, "Add sub(a, b)", "user")
        script = (f"from pathlib import Path\nPath({draft['test_path']!r}).write_text({GOOD_TEST!r})\n"
                  f"Path('calc.py').write_text({(CALC + 'def sub(a, b): return a - b' + chr(10))!r})")
        self.assertTrue(supervisor.run_goal(self.k, draft["goal_id"], fake(script))["done"])
        build = quick.approve_check(self.k, self.project, draft["goal_id"], draft["request"],
                                    sha256_bytes((self.root / draft["test_path"]).read_bytes()), "user")
        self.assertTrue(supervisor.run_goal(self.k, build["goal_id"], fake("raise RuntimeError('unexpected call')"))["done"])
        elapsed = time.monotonic() - started
        operations = [json.loads(r[0]) for r in self.k.q("SELECT payload FROM event WHERE type = 'timing.operation' ORDER BY seq")]
        result = profile.breakdown(operations, elapsed)
        for operation in operations:
            self.assertEqual(sum(operation["stages_ns"].values()), operation["elapsed_ns"])
        self.assertEqual(set(result["stages"]), set(timing.STAGES))
        self.assertLessEqual(sum(s["seconds"] for s in result["stages"].values()), elapsed)
        for stage in ("model_execution", "workspace", "baseline_red", "verification", "acceptance", "supervisor_bookkeeping"):
            self.assertGreater(result["stages"][stage]["seconds"], 0, stage)
        self.assertEqual(result["stages"]["trial"]["seconds"], 0)
        rows = profile.read(self.home)
        self.assertEqual({r["goal_id"] for r in rows}, {draft["goal_id"], build["goal_id"]})
        cli = subprocess.run([sys.executable, "evaluations/profile.py", str(self.home), build["goal_id"]],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(cli.stdout), profile.read(self.home, build["goal_id"]))
        summary = issues.profile_means([{"profile": result}, {"profile": result}])
        self.assertEqual(summary["stage_seconds_mean"]["model_execution"], result["stages"]["model_execution"]["seconds"])

    def test_small_fake_goal_accounting_tolerates_scheduler_gap(self):
        g = self.goal({"a.txt": "A"})
        def identity(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            return "fixture-process"
        started = time.monotonic()
        # Timing does not depend on process-table access; the real fake worker still runs.
        with mock.patch("lupus.runs.proc_start", side_effect=identity):
            self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake("import time\ntime.sleep(0.05)\n" + WRITER))["done"])
        elapsed = time.monotonic() - started
        # Model a one-second scheduler pause outside the operation without sleeping.
        # Accounting must still pass even when wall-clock coverage falls below 95%.
        elapsed += 1.0
        rows = profile.read(self.home, g["goal_id"])
        operations = [json.loads(r[0]) for r in self.k.q(
            "SELECT payload FROM event WHERE type = 'timing.operation' AND aggregate_id = ?", g["goal_id"])]
        self.assertTrue(operations)
        for operation in operations:
            self.assertEqual(sum(operation["stages_ns"].values()), operation["elapsed_ns"])
        stages = rows[0]["stages"]
        self.assertEqual(set(stages), set(timing.STAGES))
        for name in ("model_execution", "workspace", "verification", "supervisor_bookkeeping"):
            self.assertGreater(stages[name]["seconds"], 0, name)
        attributed = sum(s["seconds"] for s in stages.values())
        self.assertLessEqual(attributed, elapsed)
        cli = subprocess.run([sys.executable, "evaluations/profile.py", str(self.home), g["goal_id"]],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(cli.stdout), rows)
        means = issues.profile_means([{"profile": rows[0]}, {"profile": rows[0]}])
        self.assertEqual(means["stage_seconds_mean"]["model_execution"], stages["model_execution"]["seconds"])

    def test_nested_spans_are_exclusive_and_exception_is_recorded(self):
        g = self.goal()
        @timing.operation("supervisor_bookkeeping")
        def fail(k, goal_id):
            with timing.span("trial"):
                with timing.span("workspace"):
                    time.sleep(0.002)
                time.sleep(0.002)
            raise ValueError("original")
        with self.assertRaisesRegex(ValueError, "original"):
            fail(self.k, g["goal_id"])
        event = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'timing.operation'")[0])
        self.assertTrue(event["failed"])
        self.assertGreater(event["stages_ns"]["workspace"], 0)
        self.assertGreater(event["stages_ns"]["trial"], 0)
        self.assertEqual(sum(event["stages_ns"].values()), event["elapsed_ns"])
        self.assertIsNone(timing._current.get())

    def test_legacy_and_wait_are_visible_as_unattributed(self):
        g = self.goal()
        self.clock.advance(1000)
        goals.pause(self.k, g["goal_id"], "user")
        legacy = profile.read(self.home, g["goal_id"])[0]
        self.assertEqual(legacy["unattributed_share"], 1)
        self.assertFalse(legacy["target_met"])
        stages = dict.fromkeys(timing.STAGES, 0)
        stages["model_execution"] = 1000000
        ops = [{"start_ns": 0, "end_ns": 1000000, "stages_ns": stages},
               {"start_ns": 10000000, "end_ns": 11000000, "stages_ns": stages}]
        self.assertAlmostEqual(profile.breakdown(ops)["unattributed_share"], 900 / 1100)
        with self.assertRaises(ValueError):
            profile.breakdown(ops, 1e-5)
