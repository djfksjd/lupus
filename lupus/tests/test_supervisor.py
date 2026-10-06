import json
import os
import signal
import subprocess
import sys
import time

from lupus import budget, goals, projects, recovery, runs, supervisor, usage, vault
from lupus.util import group_alive

from .helpers import LIAR, SRC, WRITER, Env, fake, run_script

USAGE = {"tokens_in": 100, "tokens_out": 20, "calls": 1}
FAKE = "adapters.FakeAdapter({script!r}, usage={usage!r})"


class LoopTests(Env):
    def test_goal_runs_to_done_on_verified_evidence_then_stops(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        report = supervisor.run_goal(self.k, g["goal_id"], fake(usage=USAGE))
        self.assertTrue(report["done"])
        self.assertEqual(len(self.calls()), 2)
        again = supervisor.run_goal(self.k, g["goal_id"], fake(usage=USAGE))   # a finished goal is not re-run
        self.assertEqual((again["steps"], len(self.calls())), ([], 2))
        self.assertEqual(report["usage"]["totals"]["tokens_in"], 200)
        self.assertEqual(report["usage"]["events_by_observation"], {"measured": 2})

    def test_worker_saying_done_is_not_completion_and_the_loop_stops_spinning(self):
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(LIAR))
        self.assertFalse(report["done"])
        self.assertEqual(report["goal_status"], "NO_PROGRESS")
        # one lean call, one careful retry that carries the verifier's failure output, then it stops:
        # a third try with nothing new is refused before any model is invoked
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual([s["status"] for s in report["steps"]], ["PENDING", "NO_PROGRESS"])
        more = supervisor.run_goal(self.k, g["goal_id"], fake(LIAR))
        self.assertEqual((more["steps"], len(self.calls())), ([], 2))           # parked: no further calls
        self.assertIn("EVIDENCE_FAIL:c0", report["completion_blockers"])

    def test_changing_but_never_passing_work_is_cut_off_after_two_attempts(self):
        script = WRITER.replace("pathlib.Path(name).write_text(text)",
                                "import os; pathlib.Path(name).write_text('wrong-%d' % os.getpid())")
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(script))
        self.assertEqual(report["goal_status"], "NO_PROGRESS")
        self.assertEqual(len(self.calls()), 2)
        task = goals.tasks(self.k, g["goal_id"])[0]
        self.assertEqual((task["attempt_count"], task["no_progress_streak"]), (2, 2))
        self.assertIn("no valid progress", task["wait_reason"])

    def test_unobserved_usage_is_estimated_at_the_reserved_amount_not_zero(self):
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(usage=None), work_calls=7)
        self.assertTrue(report["done"])
        self.assertEqual(report["budget"]["calls"]["used"], 7)
        self.assertEqual(report["usage"]["events_by_observation"], {})
        row = self.k.one("SELECT observation FROM reservation WHERE kind='work'")
        self.assertEqual(row["observation"], "estimated")

    def test_budget_exhaustion_parks_the_goal_and_switching_ai_does_not_refill_it(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, caps={"calls": 12, "attempts": 5, "active_ms": 10**9})
        report = supervisor.run_goal(self.k, g["goal_id"], fake(usage=None), work_calls=8)
        self.assertEqual(report["goal_status"], "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.calls()), 1)
        other = supervisor.run_goal(self.k, g["goal_id"], fake(driver="fake_alt", usage=None), work_calls=8)
        self.assertEqual((other["steps"], len(self.calls())), ([], 1))
        tid = self.task_ids(g["goal_id"])[1]
        goals.resolve_wait(self.k, tid, "user", "try again without more budget")
        again = supervisor.run_goal(self.k, g["goal_id"], fake(usage=None), work_calls=8)
        self.assertEqual((again["goal_status"], len(self.calls())), ("BUDGET_EXHAUSTED", 1))   # refused pre-call
        self.assertRefused("USER_AUTHORITY_REQUIRED", budget.raise_cap, self.k, g["budget_id"], "calls", 40,
                           "alpha", "self-extension")
        budget.raise_cap(self.k, g["budget_id"], "calls", 40, "user", "approved more")
        goals.resolve_wait(self.k, tid, "user", "budget raised")
        self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake(usage=None), work_calls=8)["done"])

    def test_provider_quota_is_a_wait_not_an_implementation_retry(self):
        g = self.goal()
        quota = 'import sys\nopen("calls.log","a").write("q\\n")\nprint("usage limit reached"); sys.exit(1)'
        report = supervisor.run_goal(self.k, g["goal_id"], fake(quota))
        self.assertEqual(report["goal_status"], "EXTERNAL_BLOCKED")
        self.assertEqual(len(self.calls()), 1)
        task = goals.tasks(self.k, g["goal_id"])[0]
        self.assertEqual(task["no_progress_streak"], 0)      # not counted as a failed idea
        self.assertIn("hand off", task["wait_reason"])
        self.assertEqual(recovery.latest(self.k, g["goal_id"])["state"], "COMMITTED")

    def test_timeout_and_leftover_children_are_killed(self):
        g = self.goal()
        script = ("import subprocess, sys, time\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                  "open('child.pid', 'w').write(str(c.pid))\n"
                  "time.sleep(120)\n")
        step = supervisor.run_task(self.k, g["goal_id"], fake(script), timeout_s=1.5)
        child = int((self.root / "child.pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertEqual(step["status"], "PENDING")
        self.assertEqual(self.k.one("SELECT status FROM run")["status"], "STOPPED")

    def test_child_that_outlives_the_worker_is_reported_and_killed(self):
        g = self.goal()
        script = WRITER + ("\nimport subprocess\n"
                           "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                           "open('child.pid', 'w').write(str(c.pid))\n")
        step = supervisor.run_task(self.k, g["goal_id"], fake(script))
        self.assertTrue(step["leftover_processes"])
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "child.pid").read_text()), 0)

    def test_evidence_is_rechecked_only_when_the_artifact_changed(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        supervisor.run_task(self.k, g["goal_id"], fake())
        n = self.k.one("SELECT COUNT(*) FROM evidence")[0]
        (self.root / "a.txt").write_text("user broke it")
        report = supervisor.run_goal(self.k, g["goal_id"], fake())
        self.assertFalse(report["done"])                               # stale PASS did not count
        self.assertIn("EVIDENCE_FAIL:c0", report["completion_blockers"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM evidence")[0], n + 2)   # b's check + a's re-check


class CrashAndHandoffTests(Env):
    def body(self, gid, script=WRITER, driver="fake", usage=USAGE):
        return (f"a = {FAKE.format(script=script, usage=usage)}\na.driver = {driver!r}\n"
                f"print(json.dumps(supervisor.run_goal(k, {gid!r}, a), default=str))\n")

    def test_kill_before_go_never_starts_a_worker(self):
        for point, kind in (("adapter.after_spawn", "never_spawned"), ("adapter.before_go", "process_exited")):
            with self.subTest(point):
                (self.root / "calls.log").unlink(missing_ok=True)
                g = self.goal({f"{point}.txt": "X"})
                proc = run_script(self.home, self.body(g["goal_id"]), crash_at=point)
                self.assertEqual(proc.returncode, -9, proc.stderr)
                time.sleep(0.3)
                self.assertEqual(self.calls(), [])                        # the worker never executed
                self.reopen()
                report = supervisor.recover(self.k)
                self.assertEqual(report["writers_alive"], [])
                run = self.k.one("SELECT stop_evidence FROM run ORDER BY fencing_token DESC LIMIT 1")
                self.assertEqual(json.loads(run["stop_evidence"])["kind"], kind)
                self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake(usage=USAGE))["done"])

    def test_supervisor_killed_mid_work_then_another_ai_continues_without_reset(self):
        g = self.goal({"a.txt": "A", "b.txt": "B", "c.txt": "C"})
        gid = g["goal_id"]
        # first task completes normally; the supervisor then dies right after the 2nd worker ran
        self.assertEqual(supervisor.run_task(self.k, gid, fake(usage=USAGE))["status"], "DONE")
        self.k.close()
        proc = run_script(self.home, self.body(gid), crash_at="supervisor.after_worker")
        self.assertEqual(proc.returncode, -9, proc.stderr)
        self.reopen()
        self.assertTrue((self.root / "b.txt").exists())            # work happened, nothing recorded it
        self.assertEqual(goals.tasks(self.k, gid)[1]["status"], "RUNNING")
        report = supervisor.recover(self.k)
        self.assertEqual(len(report["runs_closed"]), 1)
        tasks = goals.tasks(self.k, gid)
        self.assertEqual([t["status"] for t in tasks], ["DONE", "PENDING", "PENDING"])   # partial != done
        self.assertEqual(self.k.one("SELECT outcome FROM attempt WHERE task_id=?", tasks[1]["task_id"])[0],
                         "ABANDONED")
        ready = recovery.readiness(self.k, gid)
        self.assertTrue(ready["ready"])
        # b.txt appeared after the last checkpoint: flagged for re-verification, not trusted
        self.assertEqual(ready["workspace"], {"changed": ["b.txt"], "missing": []})
        self.assertIn("WORKSPACE_DIFFERS_FROM_CHECKPOINT:verify-partial-artifact", ready["notes"])
        spent_before = budget.snapshot(self.k, g["budget_id"])
        self.assertEqual(spent_before["calls"]["used"], 1 + 8 + 1)   # measured 1 + crashed run charged in full

        report = supervisor.run_goal(self.k, gid, fake(driver="fake_alt", usage=USAGE))   # the other AI
        self.assertTrue(report["done"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM handoff WHERE status='ACCEPTED'")[0], 1)
        log = self.calls()
        self.assertEqual(log.count("현재 작업: task a.txt"), 1)   # finished task was not redone
        # b's work was already on disk when the supervisor died: it is verified, not paid for again
        self.assertEqual(len(log), 3)                             # a, b (run lost), c
        self.assertEqual(report["steps"][0]["outcome"], "ALREADY_SATISFIED")
        tasks = goals.tasks(self.k, gid)
        self.assertEqual([t["attempt_count"] for t in tasks], [1, 1, 1])   # the lost attempt still counts
        spent_after = budget.snapshot(self.k, g["budget_id"])
        self.assertGreater(spent_after["calls"]["used"], spent_before["calls"]["used"])
        self.assertEqual(spent_after["attempts"]["used"], 3)
        drivers = [r[0] for r in self.k.q("SELECT execution_driver FROM run ORDER BY fencing_token")]
        self.assertEqual(drivers, ["fake", "fake", "fake_alt", "fake_alt"])

    def test_orphaned_live_worker_blocks_until_explicitly_stopped(self):
        g = self.goal()
        hang = ("import os, signal, time\nopen('worker.pid','w').write(str(os.getpid()))\n"
                "os.kill(os.getppid(), signal.SIGKILL)\ntime.sleep(120)\n")
        # The gate execs the worker, so the worker's parent is the supervisor process itself.
        proc = run_script(self.home, self.body(g["goal_id"], script=hang))
        self.assertEqual(proc.returncode, -9, proc.stderr)
        worker = int((self.root / "worker.pid").read_text())
        try:
            self.reopen()
            report = supervisor.recover(self.k)
            self.assertEqual([w["pid"] for w in report["writers_alive"]], [worker])
            other = self.goal({"other.txt": "O"})
            self.assertRefused("WRITER_NOT_STOPPED", runs.claim, self.k, self.task_ids(other["goal_id"])[0],
                               "fake_alt", "none")
            goals.cancel(self.k, other["goal_id"], "user")
            self.assertIn("WRITER_NOT_STOPPED", recovery.readiness(self.k, g["goal_id"])["blockers"])
            report = supervisor.recover(self.k, stop_stale_writer=True)
            self.assertEqual(report["writers_alive"], [])
            self.assertFalse(group_alive(worker))
        finally:
            try:
                os.killpg(worker, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake(driver="fake_alt", usage=USAGE))["done"])

    def test_continuation_prompt_comes_from_state_not_from_a_transcript(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        supervisor.run_task(self.k, g["goal_id"], fake())
        echo = "import sys\nopen('prompt.txt','w').write(sys.argv[1])"
        supervisor.run_task(self.k, g["goal_id"], fake(echo, driver="fake_alt"))   # no handoff here: direct call
        supervisor.run_goal(self.k, g["goal_id"], fake(echo, driver="fake"))   # driver changed: handoff
        prompt = (self.root / "prompt.txt").read_text()
        self.assertIn("중단된 업무를 이어받은 것", prompt)
        self.assertIn(self.task_ids(g["goal_id"])[0], prompt)       # done list by task id
        self.assertLess(len(prompt), 1500)


class VaultTests(Env):
    def setUp(self):
        super().setUp()
        self.vault = self.home / "vault"          # the dedicated vault created by `init`
        self.g = self.goal({"a.txt": "A"})
        supervisor.run_goal(self.k, self.g["goal_id"], fake(usage=USAGE))
        self.pid = self.project["project_id"]

    def goal_page(self, goal=None):
        return self.vault / "projects" / self.pid / "goals" / f"{(goal or self.g)['goal_id']}.md"

    def test_projection_is_markdown_only_and_keeps_code_out(self):
        vault.sync(self.k)
        files = [p for p in self.vault.rglob("*") if p.is_file() and p.name != vault.MANIFEST]
        self.assertTrue(files)
        self.assertEqual({p.suffix for p in files}, {".md"})
        self.assertIn("DONE", self.goal_page().read_text())
        self.assertNotIn("calls.log", "\n".join(p.read_text() for p in files))
        self.assertFalse(any(p.name == "a.txt" for p in files))      # no copy of project files

    def test_unchanged_state_is_not_rewritten(self):
        first = vault.sync(self.k)
        again = vault.sync(self.k)
        self.assertGreater(first["written"], 0)
        self.assertEqual((again["written"], again["removed"]), (0, 0))

    def test_editing_a_page_changes_nothing(self):
        g2 = self.goal({"z.txt": "Z"})
        vault.sync(self.k)
        path = self.goal_page(g2)
        path.write_text("상태 **DONE**\n- [x] 승인됨\n예산 상한: 999999")
        self.assertEqual(goals.get(self.k, g2["goal_id"])["status"], "ACTIVE")
        self.assertRefused("GOAL_NOT_COMPLETE", goals.complete, self.k, g2["goal_id"])
        vault.sync(self.k)
        self.assertIn("ACTIVE", path.read_text())                     # regenerated from the DB

    def test_vault_location_and_symlink_guards(self):
        (self.root / "notes").mkdir()
        self.assertRefused("VAULT_INSIDE_PROJECT", vault.sync, self.k, self.root / "notes")
        self.assertRefused("VAULT_INSIDE_RUNTIME", vault.sync, self.k, self.home / "runtime")
        other, outside = self.tmp / "other-vault", self.tmp / "outside"
        other.mkdir()
        outside.mkdir()
        os.symlink(outside, other / "projects")
        self.assertRefused("VAULT_PATH_ESCAPE", vault.sync, self.k, other)
        self.assertEqual(list(outside.iterdir()), [])                 # not even a directory was created

    def test_credential_patterns_never_reach_a_page(self):
        self.k.run("UPDATE goal SET objective = ? WHERE goal_id = ?",
                   "deploy with AKIAABCDEFGHIJKLMNOP", self.g["goal_id"])
        self.assertRefused("VAULT_CONTENT_SECRET", vault.sync, self.k)
        self.assertFalse(self.goal_page().exists())                    # nothing written at all

    def test_stale_pages_are_removed_only_if_untouched(self):
        from lupus import memory
        add = lambda title: memory.add(self.k, project_id=self.pid, kind="fact", title=title, body=title + " 내용",
                                       origin="user", actor="user")
        gone, edited = add("지울 지식"), add("손댄 지식")
        vault.sync(self.k)
        page = lambda n: self.vault / vault.node_page(n)
        own = self.vault / "my-own-file.md"
        own.write_text("mine")
        page(edited).write_text("user rewrote this page")
        for node in (gone, edited):
            memory.forget(self.k, node["node_id"], "user", "no longer true")
        report = vault.sync(self.k)
        self.assertEqual((report["removed"], report["kept_modified"]), (1, 1))
        self.assertFalse(page(gone).exists())
        self.assertTrue(page(edited).exists())                         # not ours to delete any more
        self.assertEqual(own.read_text(), "mine")                      # never touched


class CliTests(Env):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "lupus", "--home", str(self.home), *args],
                              env={**os.environ, "PYTHONPATH": SRC}, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_user_authority_commands_refuse_without_a_person_at_a_terminal(self):
        g = self.goal()
        self.k.close()
        for args in (("goal-cancel", g["goal_id"]), ("budget-raise", g["goal_id"], "calls", "999", "--reason", "x"),
                     ("revoke", self.project["project_id"], "--reason", "x"),
                     ("project-add", str(self.tmp), "--name", "x")):
            proc = self.cli(*args)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("USER_PRESENCE_REQUIRED", proc.stderr)
        self.reopen()
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "ACTIVE")

    def test_read_only_status_works_headless(self):
        g = self.goal()
        self.k.close()
        proc = self.cli("status", g["goal_id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["resume"]["blockers"], ["NO_CHECKPOINT"])
        self.reopen()
