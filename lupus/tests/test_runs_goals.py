import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from lupus import budget, goals, projects, runs
from lupus.util import group_alive

from .helpers import CAPS, Env, contains, evidence

ATT = dict(hypothesis_id="h1", baseline_hash="b0", change_scope="s", verifier_version="1", env_hash="e",
           new_evidence="first try", work={"calls": 2, "active_ms": 1000}, safety={"calls": 1})


class WriterAndLeaseTests(Env):
    def claim(self, goal, i=0):
        return runs.claim(self.k, self.task_ids(goal["goal_id"])[i], "fake", "none")

    def test_single_writer_per_project_even_across_goals(self):
        g1, g2 = self.goal(), self.goal({"b.txt": "B"})
        self.claim(g1)
        self.assertRefused("WRITER_NOT_STOPPED", self.claim, g2)

    def test_single_writer_is_enforced_by_the_database_itself(self):
        g1, g2 = self.goal(), self.goal({"b.txt": "B"})
        run = self.claim(g1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.k.conn.execute(
                "INSERT INTO run(run_id, project_id, goal_id, task_id, execution_driver, auth_mode, fencing_token,"
                " status, lease_expires_at, revocation_epoch, recovery_epoch, policy_version, started_at)"
                " VALUES ('run_x', ?, ?, ?, 'fake', 'none', 999, 'ACTIVE', 0, 1, 1, 1, 0)",
                (run["project_id"], g2["goal_id"], self.task_ids(g2["goal_id"])[0]))

    def test_expired_lease_is_not_proof_the_writer_stopped(self):
        g1, g2 = self.goal(), self.goal({"b.txt": "B"})
        run = self.claim(g1)
        self.clock.advance(10 * 60_000)   # e.g. the Mac slept
        self.assertRefused("LEASE_EXPIRED", runs.guard, self.k, run["run_id"], run["fencing_token"])
        self.assertRefused("LEASE_EXPIRED", runs.heartbeat, self.k, run["run_id"], run["fencing_token"])
        self.assertRefused("WRITER_NOT_STOPPED", self.claim, g2)   # slot stays taken

    def test_writer_slot_is_released_only_with_checked_stop_evidence(self):
        goal = self.goal()
        run = self.claim(goal)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            runs.attach_process(self.k, run["run_id"], run["fencing_token"], worker.pid)
            runs.begin_stop(self.k, run["run_id"])
            self.assertRefused("WRITER_STILL_ALIVE", runs.confirm_stopped, self.k, run["run_id"],
                               {"kind": "process_exited"})
            self.assertRefused("STOP_EVIDENCE_INVALID", runs.confirm_stopped, self.k, run["run_id"],
                               {"kind": "never_spawned"})
            self.assertRefused("USER_AUTHORITY_REQUIRED", runs.confirm_stopped, self.k, run["run_id"],
                               {"kind": "user_attested"})
        finally:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()
        for _ in range(100):
            if not group_alive(worker.pid):
                break
            time.sleep(0.02)
        runs.confirm_stopped(self.k, run["run_id"], {"kind": "process_exited"})
        self.assertEqual(goals.get_task(self.k, run["task_id"])["status"], "PENDING")
        self.claim(goal)

    def test_stale_fencing_token_is_rejected_after_a_new_run(self):
        goal = self.goal()
        old = self.claim(goal)
        runs.begin_stop(self.k, old["run_id"])
        runs.confirm_stopped(self.k, old["run_id"], {"kind": "never_spawned"})
        new = self.claim(goal)
        self.assertGreater(new["fencing_token"], old["fencing_token"])
        self.assertRefused("RUN_NOT_ACTIVE", runs.guard, self.k, old["run_id"], old["fencing_token"])
        self.assertRefused("STALE_FENCING", runs.guard, self.k, new["run_id"], old["fencing_token"])

    def test_revocation_stops_the_next_integration(self):
        goal = self.goal()
        run = self.claim(goal)
        self.assertRefused("USER_AUTHORITY_REQUIRED", projects.revoke, self.k, self.project["project_id"],
                           "supervisor", "x")
        projects.revoke(self.k, self.project["project_id"], "user", "data withdrawn")
        self.assertRefused("RUN_NOT_ACTIVE", runs.guard, self.k, run["run_id"], run["fencing_token"])
        self.assertRefused("RUN_NOT_ACTIVE", runs.start_attempt, self.k, run["run_id"], run["fencing_token"], **ATT)

    def test_unapproved_provider_cannot_claim(self):
        goal = self.goal()
        self.assertRefused("PROVIDER_NOT_APPROVED", runs.claim, self.k, self.task_ids(goal["goal_id"])[0],
                           "native_codex", "subscription")

    def test_moved_or_replaced_project_root_is_not_trusted(self):
        goal = self.goal()
        shutil.move(self.root, self.tmp / "moved")
        self.root.mkdir()   # same path, different directory
        self.assertRefused("PROJECT_ROOT_CHANGED", self.claim, goal)
        self.assertRefused("PROJECT_ROOT_CHANGED", projects.resolve, self.k, self.root)

    def test_project_binding_rules(self):
        self.assertRefused("PROJECT_ROOT_TOO_BROAD", projects.register, self.k, os.path.expanduser("~"), "h", [])
        (self.root / "sub").mkdir()
        self.assertRefused("PROJECT_ROOT_OVERLAP", projects.register, self.k, self.root / "sub", "s", [])
        self.assertEqual(projects.resolve(self.k, self.root / "sub")["project_id"], self.project["project_id"])
        self.assertIsNone(projects.resolve(self.k, self.tmp))   # unregistered folder: not bound


class AttemptTests(Env):
    def setUp(self):
        super().setUp()
        self.g = self.goal()
        self.tid = self.task_ids(self.g["goal_id"])[0]
        self.run_ = runs.claim(self.k, self.tid, "fake", "none")
        self.rt = (self.run_["run_id"], self.run_["fencing_token"])

    def start(self, **over):
        return runs.start_attempt(self.k, *self.rt, **{**ATT, **over})

    def close(self, attempt, outcome="NO_PROGRESS", **kw):
        return runs.finish_attempt(self.k, *self.rt, attempt["attempt_id"], outcome, "n",
                                   kw.get("work", {"calls": 1, "active_ms": 10}), {"calls": 0}, "measured")

    def used(self, dim):
        return budget.snapshot(self.k, self.g["budget_id"])[dim]["used"]

    def test_change_does_not_start_without_verification_and_rollback_budget(self):
        self.assertRefused("SAFETY_RESERVATION_REQUIRED", self.start, safety={})
        budget.raise_cap(self.k, self.g["budget_id"], "calls", 2, "user", "tight")
        self.assertRefused("BUDGET_EXHAUSTED", self.start)   # work 2 + safety 1 > 2
        self.assertEqual(goals.get_task(self.k, self.tid)["attempt_count"], 0)
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM reservation")[0], 0)   # nothing half-reserved

    def test_same_attempt_without_change_or_new_evidence_is_refused(self):
        self.close(self.start())
        self.assertRefused("DUPLICATE_ATTEMPT", self.start, new_evidence="trying again")
        self.assertRefused("NO_NEW_EVIDENCE", self.start, baseline_hash="b1", new_evidence="first try")

    def test_declared_resampling_is_bounded(self):
        self.set_policy(no_progress_limit=99)   # isolate the rule under test
        for _ in range(2):
            self.close(self.start(retry={"reason": "flaky network test", "cap": 2}))
        self.assertRefused("DUPLICATE_ATTEMPT", self.start)

    def test_hypothesis_limit_and_renaming_does_not_reset_totals(self):
        self.set_policy(no_progress_limit=99)   # isolate the rule under test
        self.close(self.start())
        self.close(self.start(baseline_hash="b1", new_evidence="changed parser"))
        self.assertRefused("HYPOTHESIS_EXHAUSTED", self.start, baseline_hash="b2", new_evidence="third idea")
        self.close(self.start(hypothesis_id="h2", baseline_hash="b2", new_evidence="different cause"))
        self.assertEqual(goals.get_task(self.k, self.tid)["attempt_count"], 3)
        self.assertEqual(self.used("attempts"), 3)   # the shared attempts cap keeps counting

    def test_progress_needs_evidence_not_confidence(self):
        attempt = self.start()
        self.assertRefused("PROGRESS_WITHOUT_EVIDENCE", self.close, attempt, "PROGRESS")
        (self.root / "a.txt").write_text("A")
        evidence(self.k, self.g["goal_id"], "c0", "PASS", task_id=self.tid, attempt_id=attempt["attempt_id"])
        self.assertEqual(self.close(attempt, "PROGRESS")["outcome"], "PROGRESS")

    def test_two_no_progress_attempts_stop_the_branch(self):
        self.assertFalse(self.close(self.start())["must_stop"])
        self.assertTrue(self.close(self.start(baseline_hash="b1", new_evidence="second"))["must_stop"])
        runs.finish(self.k, *self.rt, "NO_PROGRESS", "no_progress", "ruled out h1")
        self.assertEqual(goals.get(self.k, self.g["goal_id"])["status"], "NO_PROGRESS")
        self.assertRefused("GOAL_NOT_ACTIVE", runs.claim, self.k, self.tid, "fake", "none")
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.resolve_wait, self.k, self.tid, "supervisor", "retry")
        goals.resolve_wait(self.k, self.tid, "user", "new requirement detail from user")
        task = goals.get_task(self.k, self.tid)
        self.assertEqual((task["status"], task["attempt_count"]), ("PENDING", 2))   # totals survive

    def test_crash_and_new_run_do_not_reset_attempts_or_spend(self):
        self.start()
        runs.begin_stop(self.k, self.rt[0])
        runs.confirm_stopped(self.k, self.rt[0], {"kind": "never_spawned"})
        row = self.k.one("SELECT outcome FROM attempt")
        self.assertEqual(row["outcome"], "ABANDONED")
        self.assertEqual(self.used("calls"), 3)       # work 2 + safety 1, charged as estimated
        self.assertEqual(self.used("attempts"), 1)
        new = runs.claim(self.k, self.tid, "fake_alt", "none")   # a different driver takes over
        self.assertEqual(goals.get_task(self.k, self.tid)["attempt_count"], 1)
        runs.start_attempt(self.k, new["run_id"], new["fencing_token"], **{**ATT, "env_hash": "e2",
                                                                         "new_evidence": "resume after crash"})
        self.assertEqual(goals.get_task(self.k, self.tid)["attempt_count"], 2)

    def test_interrupted_retries_are_bounded(self):
        for i in range(2):
            self.start()
            runs.begin_stop(self.k, self.rt[0])
            runs.confirm_stopped(self.k, self.rt[0], {"kind": "never_spawned"})
            self.run_ = runs.claim(self.k, self.tid, "fake", "none")
            self.rt = (self.run_["run_id"], self.run_["fencing_token"])
        self.assertRefused("DUPLICATE_ATTEMPT", self.start)

    def test_stale_result_is_rejected_but_its_cost_is_settled(self):
        attempt = self.start()
        self.clock.advance(10 * 60_000)   # lease expired while the worker ran
        out = runs.finish_attempt(self.k, *self.rt, attempt["attempt_id"], "PROGRESS", "late success",
                                  {"calls": 2, "active_ms": 500}, {"calls": 0}, "measured")
        self.assertEqual((out["outcome"], out["stale"]), ("ABANDONED", "LEASE_EXPIRED"))
        self.assertEqual(self.used("calls"), 2)
        self.assertEqual(self.used("active_ms"), 500)


class GoalTests(Env):
    def test_done_requires_current_passing_evidence_for_every_criterion(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, chain=False)
        gid = g["goal_id"]
        self.assertRefused("GOAL_NOT_COMPLETE", goals.complete, self.k, gid)
        for i, tid in enumerate(self.task_ids(gid)):
            run = runs.claim(self.k, tid, "fake", "none")
            evidence(self.k, gid, f"c{i}", "PASS" if i == 0 else "FAIL", task_id=tid)
            runs.finish(self.k, run["run_id"], run["fencing_token"], "DONE", "ok")   # worker says done
        self.assertRefused("GOAL_NOT_COMPLETE", goals.complete, self.k, gid)        # c1 evidence is FAIL
        evidence(self.k, gid, "c1", "PASS")
        goals.complete(self.k, gid)
        self.assertEqual(goals.get(self.k, gid)["status"], "DONE")

    def test_worker_cannot_relax_acceptance_and_old_evidence_stops_counting(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, chain=False)
        gid = g["goal_id"]
        ev = evidence(self.k, gid, "c0", "PASS")
        ev_b = evidence(self.k, gid, "c1", "PASS")
        easier = [contains("c0", "a.txt", "A")]
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.revise_acceptance, self.k, gid, easier, "worker",
                           "b is hard", 1)
        changed = [contains("c0", "a.txt", "A"), contains("c1", "b.txt", "B2")]
        self.assertRefused("REVISION_CONFLICT", goals.revise_acceptance, self.k, gid, changed, "user", "x", 7)
        self.assertEqual(goals.revise_acceptance(self.k, gid, changed, "user", "new text for b", 1), 2)
        self.assertEqual(goals.latest_evidence(self.k, gid), {"c0": None, "c1": None})
        goals.relink_evidence(self.k, ev)                                # c0 unchanged: reusable
        self.assertRefused("EVIDENCE_NOT_REUSABLE", goals.relink_evidence, self.k, ev_b)   # c1 changed
        self.assertEqual(goals.latest_evidence(self.k, gid)["c0"]["result"], "PASS")

    def test_supervisor_may_add_criteria_but_not_change_them(self):
        g = self.goal()
        more = [contains("c0", "a.txt", "A"), contains("c9", "z.txt", "Z")]
        self.assertEqual(goals.revise_acceptance(self.k, g["goal_id"], more, "supervisor", "found gap", 1), 2)

    def test_terminal_goal_is_not_reopened_and_late_success_cannot_flip_cancel(self):
        g = self.goal()
        tid = self.task_ids(g["goal_id"])[0]
        run = runs.claim(self.k, tid, "fake", "none")
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.cancel, self.k, g["goal_id"], "supervisor")
        goals.cancel(self.k, g["goal_id"], "user", "not needed")
        self.assertRefused("RUN_NOT_ACTIVE", runs.finish, self.k, run["run_id"], run["fencing_token"], "DONE", "ok")
        runs.confirm_stopped(self.k, run["run_id"], {"kind": "never_spawned"})
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "CANCELLED")
        self.assertEqual(goals.get_task(self.k, tid)["status"], "CANCELLED")
        self.assertRefused("GOAL_TERMINAL", goals.revise_acceptance, self.k, g["goal_id"],
                           [contains("c0", "a.txt", "A")], "user", "again", 1)
        self.assertRefused("GOAL_TERMINAL", goals.add_task, self.k, g["goal_id"], "t", "p", ["c0"])

    def test_waiting_branch_does_not_stop_independent_branch(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, chain=False)
        t_a, t_b = self.task_ids(g["goal_id"])
        run = runs.claim(self.k, t_a, "fake", "none")
        runs.finish(self.k, run["run_id"], run["fencing_token"], "NEEDS_ANSWER", "blocked", "which colour?")
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "ACTIVE")       # b can still run
        self.assertEqual(goals.next_runnable(self.k, g["goal_id"])["task_id"], t_b)
        run = runs.claim(self.k, t_b, "fake", "none")
        runs.finish(self.k, run["run_id"], run["fencing_token"], "DONE", "ok")
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "NEEDS_ANSWER")  # nothing left to run
        self.assertEqual(len(goals.wait_reasons(self.k, g["goal_id"])), 1)
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.resolve_wait, self.k, t_a, "supervisor", "assume blue")
        goals.resolve_wait(self.k, t_a, "user", "blue")
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "ACTIVE")

    def test_every_task_must_serve_an_acceptance_criterion(self):
        g = self.goal()
        self.assertRefused("TASK_WITHOUT_PURPOSE", goals.add_task, self.k, g["goal_id"], "polish", "p", [])
        self.assertRefused("TASK_WITHOUT_PURPOSE", goals.add_task, self.k, g["goal_id"], "polish", "p", ["nope"])

    def test_pause_is_sticky_and_only_the_user_resumes(self):
        g = self.goal()
        goals.pause(self.k, g["goal_id"], "user")
        self.assertRefused("GOAL_NOT_ACTIVE", runs.claim, self.k, self.task_ids(g["goal_id"])[0], "fake", "none")
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.resume_paused, self.k, g["goal_id"], "supervisor")
        self.assertEqual(goals.resume_paused(self.k, g["goal_id"], "user"), "ACTIVE")

    def test_goals_are_submitted_by_the_user(self):
        self.assertRefused("USER_AUTHORITY_REQUIRED", goals.submit, self.k, self.project["project_id"], "o",
                           [contains("c", "a", "A")], CAPS, actor="alpha")
