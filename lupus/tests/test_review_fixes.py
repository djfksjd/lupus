"""Regression tests for the defects found in the Codex second-opinion review (2026-10-05) and in
the author's own review pass. Each test reproduces the reported failure path."""

import os
import signal
import subprocess
import sys
import time
from unittest import mock

from lupus import actions, adapters, budget, goals, handoff, projects, recovery, runs, supervisor, usage, verify
from lupus.util import sha256_bytes

from .helpers import CAPS, Env, WRITER, contains, evidence, fake

ACTION = {"kind": "publish", "destination": "blog:prod", "inputs_hash": "i1", "artifact_hash": "a1",
          "permissions": ["post"], "cost_cap": 0}


class CountingBroker:
    def __init__(self):
        self.effects, self.on_lookup = {}, None

    def perform(self, key, action):
        self.effects[key] = self.effects.get(key, 0) + 1
        return {"receipt": key}

    def lookup(self, key):
        if self.on_lookup:
            return self.on_lookup(key)
        return {"receipt": key} if key in self.effects else None


class ActionReviewTests(Env):
    def setUp(self):
        super().setUp()
        self.g = self.goal()
        self.gid = self.g["goal_id"]
        self.tid = self.task_ids(self.gid)[0]
        run = runs.claim(self.k, self.tid, "fake", "none")
        self.rt = (run["run_id"], run["fencing_token"])
        self.broker = CountingBroker()

    def approve(self):
        return actions.issue_approval(self.k, self.gid, ACTION, "one publish", 60_000, "user")

    def stop(self):
        runs.begin_stop(self.k, self.rt[0])
        runs.confirm_stopped(self.k, self.rt[0], {"kind": "never_spawned"})

    def test_caller_cannot_declare_an_action_internal_to_skip_approval(self):
        sneaky = {**ACTION, "external_effect": False}
        self.assertRefused("NEEDS_APPROVAL", actions.execute, self.k, self.broker, *self.rt, sneaky, "k1")
        apr = self.approve()
        actions.execute(self.k, self.broker, *self.rt, ACTION, "k1", apr)
        self.assertRefused("APPROVAL_CONSUMED", actions.execute, self.k, self.broker, *self.rt, sneaky, "k2", apr)
        self.assertEqual(self.broker.effects, {"k1": 1})          # one approval, one publish
        self.assertEqual(self.k.one("SELECT consumed_by_intent FROM approval")[0],
                         self.k.one("SELECT intent_id FROM action_intent")[0])

    def test_dispatch_inside_a_callers_transaction_is_refused(self):
        apr = self.approve()
        with self.assertRaises(Exception):
            with self.k.tx():
                self.assertRefused("NESTED_DISPATCH", actions.execute, self.k, self.broker, *self.rt, ACTION,
                                   "k1", apr)
                raise RuntimeError("outer rollback")
        self.assertEqual(self.broker.effects, {})
        self.assertIsNone(self.k.one("SELECT consumed_at FROM approval")[0])

    def test_slow_not_found_lookup_cannot_overwrite_a_confirmed_result(self):
        apr = self.approve()

        class LostResponse(CountingBroker):
            def perform(self, key, action):
                super().perform(key, action)
                raise ConnectionError("response lost")
        broker = LostResponse()
        self.assertRefused("EXECUTION_UNKNOWN", actions.execute, self.k, broker, *self.rt, ACTION, "k1", apr)
        self.stop()
        intent_id = self.k.one("SELECT intent_id FROM action_intent")[0]

        def racing_lookup(key):
            broker.on_lookup = None
            actions.reconcile(self.k, broker, intent_id)     # another reconciler confirms first
            return None                                      # …then this one's stale "not found" arrives
        broker.on_lookup = racing_lookup
        result = actions.reconcile(self.k, broker, intent_id)
        self.assertEqual(result["status"], "CONFIRMED")
        new = runs.claim(self.k, self.tid, "fake", "none")
        self.assertRefused("INTENT_NOT_RETRYABLE", actions.redispatch, self.k, broker, new["run_id"],
                           new["fencing_token"], intent_id, ACTION)
        self.assertEqual(broker.effects, {"k1": 1})

    def test_reconcile_waits_for_confirmed_stop_of_the_dispatching_run(self):
        apr = self.approve()

        class Lost(CountingBroker):
            def perform(self, key, action):
                raise ConnectionError("down")
        self.assertRefused("EXECUTION_UNKNOWN", actions.execute, self.k, Lost(), *self.rt, ACTION, "k1", apr)
        intent_id = self.k.one("SELECT intent_id FROM action_intent")[0]
        goals.pause(self.k, self.gid, "user")                    # run is now STOPPING, not proven stopped
        self.assertRefused("WRITER_NOT_STOPPED", actions.reconcile, self.k, self.broker, intent_id)
        self.assertEqual(self.k.one("SELECT status FROM action_intent")[0], "UNKNOWN")


class EvidenceReviewTests(Env):
    def test_contract_changed_while_the_worker_ran_does_not_inherit_old_pass(self):
        g = self.goal()
        gid = g["goal_id"]
        real = adapters.execute

        def execute_then_user_revises(*args, **kwargs):
            result = real(*args, **kwargs)
            goals.revise_acceptance(self.k, gid, [contains("c0", "a.txt", "STRICTER")], "user", "changed mind", 1)
            return result

        with mock.patch.object(supervisor.adapters, "execute", execute_then_user_revises):
            report = supervisor.run_goal(self.k, gid, fake())
        self.assertFalse(report["done"])
        self.assertEqual(report["steps"][0]["reason"], "ACCEPTANCE_CHANGED")
        self.assertEqual(goals.latest_evidence(self.k, gid), {"c0": None})
        self.assertRefused("ACCEPTANCE_CHANGED", goals.record_evidence, self.k, gid, "c0", "1", "h", "PASS",
                           acceptance_revision=1, verifier=contains("c0", "a.txt", "A")["verifier"])
        self.assertRefused("ACCEPTANCE_CHANGED", goals.record_evidence, self.k, gid, "c0", "1", "h", "PASS",
                           acceptance_revision=2, verifier=contains("c0", "a.txt", "A")["verifier"])
        self.assertEqual(budget.snapshot(self.k, g["budget_id"])["attempts"]["used"], 1)   # cost still counted

    def test_complete_refuses_evidence_whose_artifact_changed(self):
        g = self.goal()
        gid = g["goal_id"]
        self.assertTrue(supervisor.run_task(self.k, gid, fake())["status"] == "DONE")
        (self.root / "a.txt").write_text("edited afterwards")
        self.assertRefused("GOAL_NOT_COMPLETE", goals.complete, self.k, gid)   # enforced by the kernel itself
        self.assertIn("EVIDENCE_STALE:c0", goals.completion_blockers(self.k, gid))

    def cmd(self, cid, path, dep):
        check = f"import sys; sys.exit(0 if open({dep!r}).read() == 'ok' else 1)"
        return {"id": cid, "text": f"{path} check", "verifier": {
            "kind": "command", "argv": [sys.executable, "-c", check], "paths": [path], "timeout_s": 20}}

    def test_command_verifier_must_declare_its_inputs(self):
        bad = {"id": "c", "text": "t", "verifier": {"kind": "command", "argv": ["true"]}}
        self.assertRefused("CRITERION_INVALID", goals.submit, self.k, self.project["project_id"], "o", [bad], CAPS)

    def test_command_pass_is_not_reused_after_later_runs_changed_undeclared_inputs(self):
        crits = [self.cmd("c0", "a.txt", "dep.txt"), contains("c1", "b.txt", "B")]
        g = goals.submit(self.k, self.project["project_id"], "o", crits, CAPS)
        gid = g["goal_id"]
        t0 = goals.add_task(self.k, gid, "first", "write dep.txt ok\nwrite a.txt A", ["c0"])
        goals.add_task(self.k, gid, "second", "write b.txt B\nwrite dep.txt broken", ["c1"], [t0["task_id"]])
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertEqual([s["status"] for s in report["steps"]], ["DONE", "DONE"])   # both tasks "passed"
        self.assertFalse(report["done"])                                             # final re-check caught it
        self.assertIn("EVIDENCE_FAIL:c0", report["completion_blockers"])

    def test_verifier_children_do_not_outlive_verification(self):
        script = ("import subprocess, sys\n"
                  "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                  "open('vchild.pid', 'w').write(str(c.pid))\n")
        v = {"kind": "command", "argv": [sys.executable, "-c", script], "paths": ["a.txt"]}
        self.assertEqual(verify.run(v, self.root)[0], "PASS")
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "vchild.pid").read_text()), 0)

    def test_verification_time_is_reserved_up_front_and_charged(self):
        slow = {"id": "c0", "text": "slow check", "verifier": {
            "kind": "command", "argv": [sys.executable, "-c", "import time; time.sleep(0.4)"],
            "paths": ["a.txt"], "timeout_s": 30}}
        caps = {"calls": 50, "attempts": 5, "active_ms": 20_000}
        g = goals.submit(self.k, self.project["project_id"], "o", [slow], caps)
        goals.add_task(self.k, g["goal_id"], "t", "write a.txt A", ["c0"])
        # worker 5s + verifier bound 30s does not fit under 20s: refused before any call
        step = supervisor.run_task(self.k, g["goal_id"], fake(), timeout_s=5)
        self.assertEqual((step["status"], self.calls()), ("BUDGET_EXHAUSTED", []))
        budget.raise_cap(self.k, g["budget_id"], "active_ms", 200_000, "user", "ok")
        goals.resolve_wait(self.k, self.task_ids(g["goal_id"])[0], "user", "raised")
        self.assertEqual(supervisor.run_task(self.k, g["goal_id"], fake(), timeout_s=5)["status"], "DONE")
        safety = self.k.one("SELECT actual FROM reservation WHERE kind='safety' AND status='SETTLED'")[0]
        self.assertGreaterEqual(__import__("json").loads(safety)["active_ms"], 400)


class LoopReviewTests(Env):
    def test_no_progress_limit_survives_a_crash_before_the_task_was_parked(self):
        g = self.goal()
        tid = self.task_ids(g["goal_id"])[0]
        run = runs.claim(self.k, tid, "fake", "none")
        self.k.run("UPDATE task SET no_progress_streak = 2 WHERE task_id = ?", tid)   # limit reached, then crash
        supervisor.recover(self.k)
        self.assertEqual(goals.get_task(self.k, tid)["status"], "PENDING")
        new = runs.claim(self.k, tid, "fake", "none")
        self.assertRefused("NO_PROGRESS_LIMIT", runs.start_attempt, self.k, new["run_id"], new["fencing_token"],
                           hypothesis_id="a-new-name", baseline_hash="b", change_scope="s", verifier_version="1",
                           env_hash="e", new_evidence="new idea", work={"calls": 1}, safety={"calls": 1})
        runs.finish(self.k, new["run_id"], new["fencing_token"], "PENDING", "x")
        report = supervisor.run_goal(self.k, g["goal_id"], fake())
        self.assertEqual((report["goal_status"], self.calls()), ("NO_PROGRESS", []))

    def test_failed_branch_can_be_resolved_by_the_user(self):
        g = self.goal()
        tid = self.task_ids(g["goal_id"])[0]
        run = runs.claim(self.k, tid, "fake", "none")
        runs.finish(self.k, run["run_id"], run["fencing_token"], "FAILED", "failed", "tooling broke")
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "FAILED")
        goals.resolve_wait(self.k, tid, "user", "tooling fixed")
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "ACTIVE")
        self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake())["done"])

    def test_same_ai_resume_after_revocation_needs_user_revalidation(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        gid = g["goal_id"]
        supervisor.run_goal(self.k, gid, fake(), max_steps=1)
        projects.revoke(self.k, self.project["project_id"], "user", "scope reduced")
        report = supervisor.run_goal(self.k, gid, fake())            # same driver: no handoff involved
        self.assertEqual(report["steps"], [{"status": "NOT_READY", "blockers": ["REVOCATION_EPOCH_CHANGED"]}])
        self.assertEqual(len(self.calls()), 1)
        recovery.revalidate(self.k, gid, "user", "remaining files are fine")
        self.assertTrue(supervisor.run_goal(self.k, gid, fake())["done"])

    def test_missing_cli_is_an_environment_block_not_a_failed_idea(self):
        g = self.goal()

        class Missing(adapters.FakeAdapter):
            def argv(self, prompt, cwd):
                return ["/nonexistent/lupus-cli", prompt]
        report = supervisor.run_goal(self.k, g["goal_id"], Missing(""))
        task = goals.tasks(self.k, g["goal_id"])[0]
        self.assertEqual((task["status"], task["no_progress_streak"]), ("EXTERNAL_BLOCKED", 0))
        self.assertIn("could not be started", task["wait_reason"])

    def test_native_driver_needs_measured_capabilities_before_any_lease(self):
        self.k.run("UPDATE project SET approved_providers = '[\"anthropic\",\"local\"]'")
        g = self.goal()
        self.assertRefused("CAPABILITY_UNVERIFIED", runs.claim, self.k, self.task_ids(g["goal_id"])[0],
                           "native_claude", "subscription")

    def test_task_whose_criteria_were_removed_is_cancelled_not_run(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, chain=False)
        gid = g["goal_id"]
        goals.revise_acceptance(self.k, gid, [contains("c0", "a.txt", "A")], "user", "b no longer needed", 1)
        self.assertEqual([t["status"] for t in goals.tasks(self.k, gid)], ["PENDING", "CANCELLED"])
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertTrue(report["done"])
        self.assertEqual(len(self.calls()), 1)                       # nothing spent on the dropped branch

    def test_revised_contract_is_verified_before_done(self):
        g = self.goal()
        gid = g["goal_id"]
        supervisor.run_task(self.k, gid, fake())                     # task DONE under revision 1
        goals.revise_acceptance(self.k, gid, [contains("c0", "a.txt", "A"), contains("c9", "a.txt", "ZZZ")],
                                "supervisor", "found a gap", 1)
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertFalse(report["done"])                             # c9 really checked, and it fails
        self.assertIn("EVIDENCE_FAIL:c9", report["completion_blockers"])
        self.assertEqual(len(self.calls()), 1)

    def test_recover_never_signals_a_group_it_cannot_prove_is_its_worker(self):
        g = self.goal()
        run = runs.claim(self.k, self.task_ids(g["goal_id"])[0], "fake", "none")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            runs.attach_process(self.k, run["run_id"], run["fencing_token"], other.pid)
            self.k.run("UPDATE run SET proc_start = 'Thu Jan  1 00:00:00 1970' WHERE run_id = ?", run["run_id"])
            report = supervisor.recover(self.k, stop_stale_writer=True)   # pid looks reused
            self.assertEqual(len(report["writers_alive"]), 1)
            self.assertIn("attestation", report["writers_alive"][0]["leader"])
            self.assertIsNone(other.poll())                               # the unrelated process is untouched
            runs.confirm_stopped(self.k, run["run_id"], {"kind": "user_attested"}, actor="user")
        finally:
            os.killpg(other.pid, signal.SIGKILL)
            other.wait()


class SupervisorLockTests(Env):
    def test_only_one_supervisor_per_runtime(self):
        from lupus.kernel import Kernel
        g = self.goal()
        other = Kernel(self.home, self.clock)
        try:
            with other.supervisor_lock():
                self.assertRefused("SUPERVISOR_BUSY", supervisor.run_goal, self.k, g["goal_id"], fake())
                self.assertRefused("SUPERVISOR_BUSY", supervisor.recover, self.k)
            self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake())["done"])
        finally:
            other.close()

    def test_recover_settles_holds_left_by_a_dead_supervisor(self):
        g = self.goal()
        budget.reserve(self.k, g["budget_id"], "safety", "final verification", {"active_ms": 1000})
        self.assertEqual(supervisor.recover(self.k)["holds_settled"], 1)
        line = budget.snapshot(self.k, g["budget_id"])["active_ms"]
        self.assertEqual((line["safety_reserved"], line["used"]), (0, 1000))   # charged, not leaked or refunded

    def test_evidence_from_the_closing_run_itself_is_not_stale(self):
        import sys
        crit = {"id": "c0", "text": "check", "verifier": {
            "kind": "command", "argv": [sys.executable, "-c", "open('runs.log','a').write('v\\n')"],
            "paths": ["a.txt"]}}
        g = goals.submit(self.k, self.project["project_id"], "o", [crit], CAPS)
        goals.add_task(self.k, g["goal_id"], "t", "write a.txt A", ["c0"])
        self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake())["done"])
        self.assertEqual((self.root / "runs.log").read_text().count("v"), 1)   # verified once, not twice


class RecoveryReviewTests(Env):
    def test_superseding_checkpoint_keeps_earlier_recovery_objects_pinned(self):
        g = self.goal({"a.txt": "A", "b.txt": "B", "c.txt": "C"})
        gid = g["goal_id"]
        supervisor.run_goal(self.k, gid, fake(), max_steps=2)
        cp = recovery.latest(self.k, gid)
        self.assertEqual([o["label"] for o in recovery.pinned_objects(self.k, cp["checkpoint_id"])],
                         ["a.txt", "b.txt"])
        self.set_policy(orphan_grace_ms=0)
        self.clock.advance(5000)
        self.assertEqual(recovery.reconcile(self.k)["orphans_removed"], [])
        self.assertEqual(recovery.read_object(self.k, sha256_bytes(b"A")), b"A")

    def test_tombstone_only_accepts_real_content_hashes(self):
        victim = self.tmp / "unrelated-user-file"
        victim.write_text("keep me")
        for bad in (str(victim), "../../" + victim.name, "zz", sha256_bytes(b"x").upper()):
            self.assertRefused("OBJECT_HASH_INVALID", recovery.tombstone, self.k, bad, "user", "x")
        self.assertEqual(victim.read_text(), "keep me")

    def test_new_shard_directory_entry_is_synced(self):
        with mock.patch.object(recovery, "fsync_dir") as synced:
            recovery.put_object(self.k, b"first object in a new shard")
        self.assertIn(mock.call(self.k.objects_dir), synced.call_args_list)

    def test_handoff_packet_is_void_after_the_acceptance_contract_changes(self):
        g = self.goal()
        gid = g["goal_id"]
        recovery.commit(self.k, gid, expected_revision=0, done=[], remaining=[], next_action="go")
        packet = handoff.prepare(self.k, gid, "fake_alt")
        goals.revise_acceptance(self.k, gid, [contains("c0", "a.txt", "A"), contains("c5", "e.txt", "E")],
                                "supervisor", "added", 1)
        self.assertRefused("HANDOFF_STALE", handoff.accept, self.k, "r", packet,
                           {"checkpoint_revision": 1, "next_task_id": packet["work"]["next_task_id"]}, "none")


class UsageReviewTests(Env):
    def setUp(self):
        super().setUp()
        self.g = self.goal(caps={**CAPS, "tokens": 10_000})
        self.run_id = runs.claim(self.k, self.task_ids(self.g["goal_id"])[0], "fake", "none")["run_id"]

    def rec(self, event_id, mode, seq, **vals):
        return usage.record(self.k, event_id=event_id, run_id=self.run_id, provider_session_id="s", mode=mode,
                            sequence=seq, reported=vals, observation="measured", source="t")

    def test_delta_then_cumulative_in_one_session_is_not_charged_twice(self):
        self.rec("e1", "delta", 1, tokens_in=100)
        self.rec("e2", "cumulative", 2, tokens_in=150)
        self.assertEqual(budget.snapshot(self.k, self.g["budget_id"])["tokens"]["used"], 150)

    def test_cached_input_tokens_count_against_the_token_cap(self):
        self.rec("e1", "delta", 1, tokens_cached=1000, tokens_in=5, tokens_out=5)
        self.assertEqual(budget.snapshot(self.k, self.g["budget_id"])["tokens"]["used"], 1010)


class RoundTwoTests(Env):
    """Second Codex review round."""

    def cmd_goal(self, script="open('runs.log','a').write('v\\n')"):
        crit = {"id": "c0", "text": "check", "verifier": {
            "kind": "command", "argv": [sys.executable, "-c", script], "paths": ["a.txt"], "timeout_s": 60}}
        g = goals.submit(self.k, self.project["project_id"], "o", [crit], CAPS)
        goals.add_task(self.k, g["goal_id"], "t", "write a.txt A", ["c0"])
        return g

    def verifier_runs(self):
        log = self.root / "runs.log"
        return log.read_text().count("v") if log.exists() else 0

    def test_counter_reset_is_charged_once_then_becomes_the_baseline(self):
        g = self.goal(caps={**CAPS, "tokens": 10_000})
        run_id = runs.claim(self.k, self.task_ids(g["goal_id"])[0], "fake", "none")["run_id"]
        for i, total in enumerate((500, 40, 50), 1):
            usage.record(self.k, event_id=f"e{i}", run_id=run_id, provider_session_id="s", mode="cumulative",
                         sequence=i, reported={"tokens_in": total}, observation="measured", source="t")
        self.assertEqual(budget.snapshot(self.k, g["budget_id"])["tokens"]["used"], 550)

    def test_cumulative_snapshot_older_than_a_charged_delta_adds_nothing(self):
        g = self.goal(caps={**CAPS, "tokens": 10_000})
        run_id = runs.claim(self.k, self.task_ids(g["goal_id"])[0], "fake", "none")["run_id"]
        rec = lambda eid, mode, seq, n: usage.record(
            self.k, event_id=eid, run_id=run_id, provider_session_id="s", mode=mode, sequence=seq,
            reported={"tokens_in": n}, observation="measured", source="t")
        rec("d", "delta", 2, 100)
        self.assertEqual(rec("c", "cumulative", 1, 80)["anomaly"], "stale_sequence")
        self.assertEqual(budget.snapshot(self.k, g["budget_id"])["tokens"]["used"], 100)

    def test_removing_a_prerequisites_criterion_does_not_strand_its_dependents(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})            # task b depends on task a
        gid = g["goal_id"]
        goals.revise_acceptance(self.k, gid, [contains("c1", "b.txt", "B")], "user", "a not needed", 1)
        self.assertEqual([t["status"] for t in goals.tasks(self.k, gid)], ["CANCELLED", "PENDING"])
        self.assertIsNotNone(goals.next_runnable(self.k, gid))
        self.assertTrue(supervisor.run_goal(self.k, gid, fake())["done"])
        self.assertEqual(self.calls(), ["현재 작업: task b.txt"])

    def test_branch_whose_criteria_vanished_mid_run_is_cancelled_when_its_run_stops(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"}, chain=False)
        gid = g["goal_id"]
        real, state = adapters.execute, {"first": True}

        def execute_then_user_drops_a(*args, **kwargs):
            result = real(*args, **kwargs)
            if state.pop("first", False):
                goals.revise_acceptance(self.k, gid, [contains("c1", "b.txt", "B")], "user", "drop a", 1)
            return result

        with mock.patch.object(supervisor.adapters, "execute", execute_then_user_drops_a):
            report = supervisor.run_goal(self.k, gid, fake())
        self.assertEqual(report["steps"][0]["reason"], "ACCEPTANCE_CHANGED")
        self.assertEqual([t["status"] for t in goals.tasks(self.k, gid)], ["CANCELLED", "PENDING"])
        report = supervisor.run_goal(self.k, gid, fake())       # the retained branch is not blocked
        self.assertTrue(report["done"], report)
        self.assertEqual(len(self.calls()), 2)

    def test_verifier_group_is_recorded_and_blocks_the_project_until_gone(self):
        g = self.goal()
        tid = self.task_ids(g["goal_id"])[0]
        verifier = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            runs.register_aux(self.k, self.project["project_id"], verifier.pid, "verifier")
            self.assertRefused("WRITER_NOT_STOPPED", runs.claim, self.k, tid, "fake", "none")
            self.assertRefused("WRITER_STILL_ALIVE", runs.clear_aux, self.k, verifier.pid)
            recovery.commit(self.k, g["goal_id"], expected_revision=0, done=[], remaining=[tid], next_action="go")
            self.assertIn("WRITER_NOT_STOPPED", recovery.readiness(self.k, g["goal_id"])["blockers"])
            report = supervisor.recover(self.k)
            self.assertEqual(report["writers_alive"], [{"aux_pid": verifier.pid, "purpose": "verifier"}])
            self.assertEqual(supervisor.recover(self.k, stop_stale_writer=True)["writers_alive"], [])
            self.assertIsNotNone(verifier.wait(timeout=10))
        finally:
            if verifier.poll() is None:
                os.killpg(verifier.pid, signal.SIGKILL)
                verifier.wait()
        runs.claim(self.k, tid, "fake", "none")

    def test_supervisor_killed_during_verification_leaves_a_recorded_verifier(self):
        from .helpers import run_script
        kill_parent = ("import os, signal, time\nopen('verifier.pid','w').write(str(os.getpid()))\n"
                       "os.kill(os.getppid(), signal.SIGKILL)\ntime.sleep(120)\n")
        g = self.cmd_goal(kill_parent)
        self.k.close()
        body = (f"a = adapters.FakeAdapter({WRITER!r})\nsupervisor.run_goal(k, {g['goal_id']!r}, a)\n")
        proc = run_script(self.home, body)
        self.assertEqual(proc.returncode, -9, proc.stderr)
        pid = int((self.root / "verifier.pid").read_text())
        try:
            self.reopen()
            self.assertEqual(self.k.one("SELECT pid FROM aux_process")[0], pid)   # recorded before it ran
            report = supervisor.recover(self.k)
            self.assertIn({"aux_pid": pid, "purpose": "verifier"}, report["writers_alive"])
            self.assertEqual(self.k.one("SELECT status FROM run")[0], "ACTIVE")   # slot NOT released
            report = supervisor.recover(self.k, stop_stale_writer=True)
            self.assertEqual(report["writers_alive"], [])
            self.assertEqual(self.k.one("SELECT status FROM run")[0], "STOPPED")
        finally:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_revocation_after_the_last_task_blocks_final_verification_and_done(self):
        g = self.cmd_goal()
        gid = g["goal_id"]
        self.assertEqual(supervisor.run_task(self.k, gid, fake())["status"], "DONE")
        other = self.goal({"z.txt": "Z"})
        supervisor.run_task(self.k, other["goal_id"], fake())        # a later run: c0's evidence is stale now
        ran = self.verifier_runs()
        projects.revoke(self.k, self.project["project_id"], "user", "scope reduced")
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertFalse(report["done"])
        self.assertIn("REVOCATION_EPOCH_CHANGED", report["completion_blockers"])
        self.assertEqual(self.verifier_runs(), ran)                  # no verifier executed without authority
        self.assertRefused("GOAL_NOT_COMPLETE", goals.complete, self.k, gid)
        recovery.revalidate(self.k, gid, "user", "fine")
        self.assertTrue(supervisor.run_goal(self.k, gid, fake())["done"])
        self.assertEqual(self.verifier_runs(), ran + 1)

    def test_final_verification_does_not_run_while_another_goal_holds_the_project(self):
        g = self.cmd_goal()
        gid = g["goal_id"]
        supervisor.run_task(self.k, gid, fake())
        other = self.goal({"z.txt": "Z"})
        supervisor.run_task(self.k, other["goal_id"], fake())        # makes c0's evidence stale
        other2 = self.goal({"y.txt": "Y"})
        runs.claim(self.k, self.task_ids(other2["goal_id"])[0], "fake", "none")   # project writer is busy
        ran = self.verifier_runs()
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertFalse(report["done"])
        self.assertEqual(self.verifier_runs(), ran)
        self.assertIn("EVIDENCE_STALE:c0", report["completion_blockers"])

    # ---- third round

    def test_relinked_command_evidence_does_not_become_fresh(self):
        g = self.cmd_goal()
        gid = g["goal_id"]
        supervisor.run_task(self.k, gid, fake())
        other = self.goal({"z.txt": "Z"})
        supervisor.run_task(self.k, other["goal_id"], fake())       # c0's evidence is now stale
        old = goals.latest_evidence(self.k, gid)["c0"]
        goals.revise_acceptance(self.k, gid, goals.criteria(self.k, gid) + [contains("c7", "a.txt", "A")],
                                "supervisor", "added", 1)
        goals.relink_evidence(self.k, old["evidence_id"])
        self.assertEqual(goals.latest_evidence(self.k, gid)["c0"]["seq"], old["seq"])
        self.assertIn("EVIDENCE_STALE:c0", goals.completion_blockers(self.k, gid))

    def test_late_delta_already_covered_by_a_cumulative_total_adds_nothing(self):
        g = self.goal(caps={**CAPS, "tokens": 10_000})
        run_id = runs.claim(self.k, self.task_ids(g["goal_id"])[0], "fake", "none")["run_id"]
        rec = lambda eid, mode, seq, n: usage.record(
            self.k, event_id=eid, run_id=run_id, provider_session_id="s", mode=mode, sequence=seq,
            reported={"tokens_in": n}, observation="measured", source="t")
        rec("c", "cumulative", 5, 150)
        self.assertEqual(rec("late", "delta", 2, 100)["anomaly"], "stale_sequence")
        rec("new", "delta", 6, 10)                                  # a genuinely newer delta still counts
        self.assertEqual(budget.snapshot(self.k, g["budget_id"])["tokens"]["used"], 160)

    def test_final_verification_refuses_a_replaced_project_directory(self):
        import shutil
        g = self.cmd_goal()
        gid = g["goal_id"]
        supervisor.run_task(self.k, gid, fake())
        other = self.goal({"z.txt": "Z"})
        supervisor.run_task(self.k, other["goal_id"], fake())
        shutil.move(self.root, self.tmp / "moved")
        shutil.copytree(self.tmp / "moved", self.root)              # same path and files, different directory
        ran = self.verifier_runs()
        report = supervisor.run_goal(self.k, gid, fake())
        self.assertFalse(report["done"])
        self.assertEqual(self.verifier_runs(), ran)
        self.assertIn("PROJECT_ROOT_CHANGED", report["completion_blockers"])
