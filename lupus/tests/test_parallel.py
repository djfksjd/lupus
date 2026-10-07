"""`alpha-run --parallel`: several goals have a worker at the same time, under one supervisor,
one budget ledger and one writer per project."""

import threading
import time

from lupus import alpha, goals, projects, runs
from lupus.adapters import FakeAdapter
from lupus.kernel import SupervisorStopping
from lupus.util import group_alive

from .helpers import CAPS, WRITER, Env, contains

# Does the task, and records when it started and ended (appended: two goals of one project share the file).
SPAN = "import time\n_t0 = time.time()\ntime.sleep(1.0)\n" + WRITER + "\nopen('span.log', 'a').write(f'{_t0} {time.time()}\\n')\n"


def spans(*roots):
    return sorted(tuple(map(float, line.split())) for root in roots for line in (root / "span.log").read_text().splitlines())


def factory(script=WRITER):
    def make(driver):
        adapter = FakeAdapter(script)
        adapter.driver = driver
        return adapter
    return make


class Parallel(Env):
    def setUp(self):
        super().setUp()
        self.roots = [self.root]
        self.projects = [self.project]
        for name in ("two", "three"):
            root = self.tmp / name
            root.mkdir()
            self.roots.append(root)
            self.projects.append(projects.register(self.k, root, name, ["local"]))

    def goal_in(self, project, name="a.txt", text="A"):
        g = goals.submit(self.k, project["project_id"], f"goal {name}", [contains("c0", name, text)], CAPS)
        goals.add_task(self.k, g["goal_id"], f"task {name}", f"write {name} {text}", ["c0"])
        return g["goal_id"]

    def test_goals_of_different_projects_really_run_at_the_same_time(self):
        made = [self.goal_in(p) for p in self.projects]
        report = alpha.run(self.k, ["fake"], factory(SPAN), parallel=3)
        self.assertEqual(sorted(s["goal_id"] for s in report["steps"]), sorted(made))
        self.assertTrue(all(s["done"] for s in report["steps"]))
        took = spans(*self.roots)
        self.assertLess(max(start for start, _ in took), min(end for _, end in took))      # all three overlapped
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM run WHERE status <> 'STOPPED'")[0], 0)
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM reservation WHERE status = 'HELD'")[0], 0)
        # every claim got its own fencing token: nothing was shared between the threads
        tokens = [r[0] for r in self.k.q("SELECT fencing_token FROM run")]
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_two_goals_of_one_project_never_have_a_worker_at_the_same_time(self):
        first, second = self.goal_in(self.project), self.goal_in(self.project, "b.txt", "B")
        report = alpha.run(self.k, ["fake"], factory(SPAN), parallel=2)
        self.assertEqual([s["done"] for s in report["steps"]], [True, True])
        (one_start, one_end), (two_start, _) = spans(self.root)
        self.assertLessEqual(one_end, two_start)                    # the project's single writer slot held
        self.assertEqual({goals.get(self.k, g)["status"] for g in (first, second)}, {"DONE"})

    def test_a_shared_cap_is_not_overspent_by_goals_that_start_together(self):
        alpha.set_budget(self.k, None, {"calls": 12, "attempts": 1, "active_ms": 10_000_000}, "user")
        for p in self.projects:
            self.goal_in(p)
        report = alpha.run(self.k, ["fake"], factory(SPAN), parallel=3)
        self.assertEqual(sum(s["done"] for s in report["steps"]), 1)
        self.assertEqual(report["portfolio"]["global_budget"]["attempts"]["used"], 1)
        self.assertEqual(sum((root / "a.txt").exists() for root in self.roots), 1)      # the others never got a worker

    def test_the_step_limit_counts_what_is_in_flight(self):
        for p in self.projects:
            self.goal_in(p)
        report = alpha.run(self.k, ["fake"], factory(), max_steps=2, parallel=3)
        self.assertEqual(len(report["steps"]), 2)
        self.assertRefused("PARALLEL_INVALID", alpha.run, self.k, ["fake"], factory(), parallel=alpha.MAX_PARALLEL + 1)

    def test_one_at_a_time_gives_the_same_result(self):
        made = [self.goal_in(p) for p in self.projects]
        report = alpha.run(self.k, ["fake"], factory(), parallel=1)
        self.assertEqual([s["goal_id"] for s in report["steps"]], made)      # the user's order, as before

    def test_when_one_thread_fails_the_others_are_stopped_like_a_crash_and_recovered(self):
        first, second = self.goal_in(self.projects[0]), self.goal_in(self.projects[1])
        calls = []

        def make(driver):
            calls.append(driver)
            if len(calls) == 2:
                while not any((root / "started").exists() for root in self.roots):      # the other one's worker is running
                    time.sleep(0.05)
                raise RuntimeError("boom")
            adapter = FakeAdapter("import time\nopen('started', 'w').close()\ntime.sleep(60)\n")
            adapter.driver = driver
            return adapter
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            alpha.run(self.k, ["fake"], make, parallel=2)
        self.assertLess(time.monotonic() - started, 30)            # the sleeping worker was ended, not waited for
        left = self.k.q("SELECT pid FROM run WHERE status <> 'STOPPED' AND pid IS NOT NULL")
        self.assertEqual(len(left), 1)                              # what it left open is what a crash leaves
        self.assertFalse(group_alive(left[0]["pid"]))
        self.assertEqual(threading.active_count(), 1)
        # the next start closes it and the work goes on; the interrupted attempt stays counted
        report = alpha.run(self.k, ["fake"], factory(), parallel=2)
        self.assertEqual({goals.get(self.k, g)["status"] for g in (first, second)}, {"DONE"})
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM attempt WHERE outcome = 'ABANDONED'")[0], 1)
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM run WHERE status <> 'STOPPED'")[0], 0)
        self.assertTrue(all(s["done"] for s in report["steps"] if s["status"] == "DONE"))


class Fork(Env):
    def test_a_fork_exists_only_under_the_supervisors_lock_and_goes_quiet_when_told_to_stop(self):
        stop = threading.Event()
        self.assertRefused("SUPERVISOR_LOCK_REQUIRED", self.k.fork, stop)
        with self.k.supervisor_lock():
            own = self.k.fork(stop)
            self.addCleanup(own.close)
            with own.supervisor_lock():          # works under the owner's lock; no second lock is taken
                with own.tx():
                    own.emit("supervisor", "probe.recorded", "runtime", "x")
            stop.set()
            with self.assertRaises(SupervisorStopping):
                with own.tx():
                    pass
        self.assertRefused("SUPERVISOR_LOCK_REQUIRED", own.supervisor_lock().__enter__)      # its supervisor is gone
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM event WHERE type = 'probe.recorded'")[0], 1)
        self.assertEqual(runs.unstopped(self.k), [])
