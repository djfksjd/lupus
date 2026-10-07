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
        self.assertEqual(goals.get_task(self.k, self.task_id)["status"], "NO_PROGRESS")
        # the next worker is told so, in the verifier's place
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM evidence WHERE goal_id = ? AND result = 'FAIL'", self.goal_id)[0], 2)

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
