"""A failed check is not run again over files that have not changed since it failed (adapted
from Prime Agent's quality gates), and only then."""

import sys

from lupus import goals, supervisor

from .helpers import CAPS, Env, fake

# A check that leaves a trace of every time it really ran, and fails. The trace is kept outside
# the project (first argument), or inside it for the check that changes what it checks.
COUNT = "import sys\nopen(sys.argv[1], 'a').write('x')\nraise SystemExit(1)"
NOTHING = "print('done')"
EDIT = "import pathlib, time\npathlib.Path('a.py').write_text(str(time.time()))\n"


class Unchanged(Env):
    def setUp(self):
        super().setUp()
        (self.root / "a.py").write_text("x = 1\n")
        self.set_policy(no_progress_limit=3, max_attempts_per_hypothesis=3)     # room for an attempt that is not the last
        self.log = self.tmp / "ran.log"
        self.make(str(self.log))

    def make(self, log: str) -> None:
        goal = goals.submit(self.k, self.project["project_id"], "demo", [
            {"id": "c0", "text": "the check passes",
             "verifier": {"kind": "command", "argv": [sys.executable, "-c", COUNT, log], "paths": ["a.py"]}}], CAPS)
        self.goal_id = goal["goal_id"]
        self.task_id = goals.add_task(self.k, self.goal_id, "t", "fix it", ["c0"])["task_id"]

    def ran(self) -> int:
        return len(self.log.read_text())

    def detail(self) -> str:
        return goals.latest_evidence(self.k, self.goal_id)["c0"]["detail"]

    def test_a_worker_that_changed_nothing_does_not_get_the_same_check_run_again(self):
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 1)
        self.assertTrue(self.detail().startswith("exit 1"))
        report = supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 1)                                   # not run again
        self.assertTrue(self.detail().startswith(supervisor.UNCHANGED + "exit 1"))
        self.assertEqual(report["steps"][-1]["outcome"], "NO_PROGRESS")   # and it still counts as an attempt
        self.assertEqual(goals.get_task(self.k, self.task_id)["status"], "PENDING")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM evidence WHERE goal_id = ? AND result = 'FAIL'", self.goal_id)[0], 2)

    def test_the_attempt_that_would_park_the_task_is_always_checked_for_real(self):
        for _ in range(3):
            supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 2)                                   # first and last for real, the one between not
        self.assertTrue(self.detail().startswith(supervisor.STILL + "exit 1"))      # and it says the files were the same
        self.assertEqual(goals.get_task(self.k, self.task_id)["status"], "NO_PROGRESS")

    def test_every_other_way_of_refusing_a_further_try_also_gets_a_real_check_first(self):
        # the number of tries allowed for one approach is reached before the no-progress limit
        self.set_policy(no_progress_limit=3, max_attempts_per_hypothesis=2)
        for _ in range(3):
            report = supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 2)                                   # both tries that were allowed ran the check
        self.assertFalse(self.detail().startswith(supervisor.UNCHANGED))
        self.assertEqual(goals.get_task(self.k, self.task_id)["status"], "NO_PROGRESS")
        self.assertIn(report["steps"][-1]["reason"], ("HYPOTHESIS_EXHAUSTED", "DUPLICATE_ATTEMPT"))      # refused before any worker

    def test_a_remembered_failure_is_used_once_per_task_so_no_retry_after_it_repeats_an_earlier_one(self):
        self.set_policy(no_progress_limit=6, max_attempts_per_hypothesis=6)
        seen = []
        for _ in range(6):
            report = supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
            seen.append((self.ran(), self.detail().startswith(supervisor.UNCHANGED), (report["steps"] or [{}])[-1].get("reason")))
        # real, reused, then real every time. The try that is finally refused as identical to an
        # earlier one comes after a REAL verdict, never after the reused one.
        self.assertEqual([s[:2] for s in seen[:4]], [(1, False), (1, True), (2, False), (3, False)])
        refused = [i for i, s in enumerate(seen) if s[2] == "DUPLICATE_ATTEMPT"]
        self.assertTrue(refused and all(not seen[i - 1][1] for i in refused), seen)
        self.assertFalse(self.detail().startswith(supervisor.UNCHANGED))

    def test_output_that_is_stored_redacted_is_never_remembered_and_a_try_after_a_reuse_is_never_a_repeat(self):
        criteria = goals.criteria(self.k, self.goal_id)
        with self.k.tx():
            supervisor._remember_failure(self.k, self.task_id, criteria, 1, "m" * 64,
                                         [(criteria[0], "FAIL", "d", "exit 1: token AKIAABCDEFGHIJKLMNOP leaked")])
        self.assertIsNone(supervisor._same_failure(self.k, self.task_id, criteria, 1, "m" * 64))
        # whatever words a reused verdict leaves behind, the next try has an identity of its own
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(supervisor._reuses(self.k, self.task_id), 1)
        self.k.run("UPDATE evidence SET detail = 'the same words as before' WHERE goal_id = ?", self.goal_id)
        report = supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(report["steps"][-1]["outcome"], "NO_PROGRESS")      # it ran (and was checked for real), not refused
        self.assertEqual(self.ran(), 2)
        prints = [r[0] for r in self.k.q("SELECT fingerprint FROM attempt WHERE task_id = ?", self.task_id)]
        self.assertEqual(len(set(prints)), 3)

    def test_with_the_default_two_attempts_the_project_is_never_read_through_for_this(self):
        from lupus import review
        self.set_policy(no_progress_limit=2, max_attempts_per_hypothesis=2)
        calls, real = [], review.fingerprint
        review.fingerprint = lambda root: calls.append(root) or real(root)
        self.addCleanup(setattr, review, "fingerprint", real)
        for _ in range(2):
            supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual((calls, self.ran()), ([], 2))        # no cost, and both attempts checked for real
        self.assertEqual(goals.get_task(self.k, self.task_id)["status"], "NO_PROGRESS")

    def test_a_check_that_fails_by_chance_or_for_an_outside_reason_is_not_what_parks_a_task(self):
        # Fails the first time it runs and passes afterwards, on the very same files.
        goals.cancel(self.k, self.goal_id, "user")
        flaky = "import sys, os\np = sys.argv[1]\nfirst = not os.path.exists(p)\nopen(p, 'a').write('x')\nraise SystemExit(1 if first else 0)"
        self.log = self.tmp / "flaky.log"
        goal = goals.submit(self.k, self.project["project_id"], "demo", [{"id": "c0", "text": "the check passes", "verifier": {
            "kind": "command", "argv": [sys.executable, "-c", flaky, str(self.log)], "paths": ["a.py"]}}], CAPS)
        self.goal_id = goal["goal_id"]
        goals.add_task(self.k, self.goal_id, "t", "fix it", ["c0"])
        self.set_policy(no_progress_limit=2, max_attempts_per_hypothesis=2)      # the defaults
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        report = supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 2)
        self.assertTrue(report["done"])                                   # the remembered failure did not stand in for the real answer

    def test_any_change_to_a_file_runs_the_check_for_real(self):
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        supervisor.run_goal(self.k, self.goal_id, fake(EDIT), max_steps=1)
        self.assertEqual(self.ran(), 2)
        self.assertFalse(self.detail().startswith(supervisor.UNCHANGED))

    def test_a_check_that_changes_the_project_is_run_every_time(self):
        goals.cancel(self.k, self.goal_id, "user")
        self.log = self.root / "ran.log"               # the check writes into what it checks
        self.make("ran.log")
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 2)
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM event WHERE type = 'verify.failed_at'")[0], 0)

    def test_what_is_staged_in_git_counts_as_a_change(self):
        import shutil, subprocess
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        from lupus import review
        subprocess.run([git, "-C", str(self.root), "init", "-q"], check=True)
        before = review.fingerprint(self.root)
        self.assertEqual(review.fingerprint(self.root), before)
        subprocess.run([git, "-C", str(self.root), "add", "a.py"], check=True)      # no file of the project changed
        self.assertNotEqual(review.fingerprint(self.root), before)
        (self.root / "__pycache__").mkdir()
        staged = review.fingerprint(self.root)
        (self.root / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"planted")
        self.assertNotEqual(review.fingerprint(self.root), staged)

    def test_another_supervisor_process_checks_for_itself(self):
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        real = supervisor._ME
        supervisor._ME = lambda: "another:process"
        self.addCleanup(setattr, supervisor, "_ME", real)
        supervisor.run_goal(self.k, self.goal_id, fake(NOTHING), max_steps=1)
        self.assertEqual(self.ran(), 2)       # what changed in between may be outside the project

    def test_a_check_that_did_not_produce_a_result_is_never_remembered(self):
        criteria = goals.criteria(self.k, self.goal_id)
        with self.k.tx():
            supervisor._remember_failure(self.k, self.task_id, criteria, 1, "m" * 64, [(criteria[0], "FAIL", "d", "verifier timed out")])
            supervisor._remember_failure(self.k, self.task_id, criteria, 1, None, [(criteria[0], "FAIL", "d", "exit 1: x")])
            supervisor._remember_failure(self.k, self.task_id, criteria, 1, "m" * 64, [(criteria[0], "PASS", "d", "exit 0")])
        self.assertIsNone(supervisor._same_failure(self.k, self.task_id, criteria, 1, "m" * 64))
        with self.k.tx():
            supervisor._remember_failure(self.k, self.task_id, criteria, 1, "m" * 64, [(criteria[0], "FAIL", "d", "exit 1: x")])
        self.assertEqual(supervisor._same_failure(self.k, self.task_id, criteria, 1, "m" * 64)[0][1:],
                         ("FAIL", "d", supervisor.UNCHANGED + "exit 1: x"))
        self.assertIsNone(supervisor._same_failure(self.k, self.task_id, criteria, 2, "m" * 64))      # the contract changed
        self.assertIsNone(supervisor._same_failure(self.k, self.task_id, criteria, 1, "n" * 64))      # the files changed
