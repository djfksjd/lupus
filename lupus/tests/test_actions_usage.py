import json

from lupus import actions, goals, runs, usage, budget, supervisor

from .helpers import Env, run_script

BROKER = r'''
import json, pathlib
class Broker:
    """File-backed stand-in for an external service with idempotency keys."""
    def __init__(self, path, fail=None):
        self.path, self.fail = pathlib.Path(path), fail
    def _load(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}
    def perform(self, key, action):
        if self.fail == "before":
            raise ConnectionError("down")
        ledger = self._load()
        ledger.setdefault(key, {"count": 0, "kind": action["kind"]})["count"] += 1
        self.path.write_text(json.dumps(ledger))
        if self.fail == "after":
            raise ConnectionError("response lost")
        return {"receipt": key, "count": ledger[key]["count"]}
    def lookup(self, key):
        return self._load().get(key)
'''
exec(BROKER)   # defines Broker for in-process tests

ACTION = {"kind": "publish", "destination": "blog:prod", "inputs_hash": "i1", "artifact_hash": "a1",
          "permissions": ["post"], "cost_cap": 0}


class ActionTests(Env):
    def setUp(self):
        super().setUp()
        self.g = self.goal()
        self.gid = self.g["goal_id"]
        self.tid = self.task_ids(self.gid)[0]
        self.new_run()
        self.ledger = self.tmp / "ledger.json"

    def new_run(self):
        run = runs.claim(self.k, self.tid, "fake", "none")
        self.rt = (run["run_id"], run["fencing_token"])

    def stop_run(self):
        runs.begin_stop(self.k, self.rt[0])
        runs.confirm_stopped(self.k, self.rt[0], {"kind": "never_spawned"})

    def approve(self, action=ACTION, ttl=60_000):
        return actions.issue_approval(self.k, self.gid, action, "one publish", ttl, "user")

    def effects(self, key="k1"):
        return (Broker(self.ledger).lookup(key) or {"count": 0})["count"]

    def test_only_the_user_can_approve(self):
        for actor in ("supervisor", "alpha", "worker"):
            self.assertRefused("USER_AUTHORITY_REQUIRED", actions.issue_approval, self.k, self.gid, ACTION,
                               "s", 1000, actor)

    def test_external_effect_without_approval_waits(self):
        self.assertRefused("NEEDS_APPROVAL", actions.execute, self.k, Broker(self.ledger), *self.rt, ACTION, "k1")
        self.assertEqual(self.effects(), 0)

    def test_artifact_changed_after_approval_is_refused(self):
        apr = self.approve()
        changed = {**ACTION, "artifact_hash": "a2"}
        self.assertRefused("APPROVAL_DIGEST_MISMATCH", actions.execute, self.k, Broker(self.ledger), *self.rt,
                           changed, "k1", apr)
        self.assertEqual(self.effects(), 0)

    def test_approval_is_single_use_and_bound_to_one_intent(self):
        apr = self.approve()
        done = actions.execute(self.k, Broker(self.ledger), *self.rt, ACTION, "k1", apr)
        self.assertEqual(done["status"], "CONFIRMED")
        self.assertRefused("APPROVAL_CONSUMED", actions.execute, self.k, Broker(self.ledger), *self.rt, ACTION,
                           "k2", apr)
        self.assertEqual((self.effects("k1"), self.effects("k2")), (1, 0))

    def test_expired_revoked_and_stale_revision_approvals_are_refused(self):
        apr = self.approve(ttl=1000)
        self.clock.advance(2000)
        self.assertRefused("APPROVAL_EXPIRED", actions.execute, self.k, Broker(self.ledger), *self.rt, ACTION,
                           "k1", apr)
        apr = self.approve()
        goals.revise_acceptance(self.k, self.gid, goals.criteria(self.k, self.gid) + [
            {"id": "c9", "text": "t", "verifier": {"kind": "file_contains", "path": "z", "text": "z"}}],
            "user", "scope grew", 1)
        self.assertRefused("APPROVAL_STALE_REVISION", actions.execute, self.k, Broker(self.ledger), *self.rt,
                           ACTION, "k1", apr)
        self.assertEqual(self.effects(), 0)

    def test_replayed_request_does_not_execute_twice(self):
        apr = self.approve()
        first = actions.execute(self.k, Broker(self.ledger), *self.rt, ACTION, "k1", apr)
        again = actions.execute(self.k, Broker(self.ledger), *self.rt, ACTION, "k1", apr)
        self.assertEqual(first["intent_id"], again["intent_id"])
        self.assertEqual(self.effects(), 1)
        self.assertRefused("IDEMPOTENCY_CONFLICT", actions.execute, self.k, Broker(self.ledger), *self.rt,
                           {**ACTION, "destination": "elsewhere"}, "k1", apr)

    def test_lost_response_becomes_unknown_and_is_reconciled_not_retried(self):
        apr = self.approve()
        self.assertRefused("EXECUTION_UNKNOWN", actions.execute, self.k, Broker(self.ledger, "after"), *self.rt,
                           ACTION, "k1", apr)
        intent = dict(self.k.one("SELECT * FROM action_intent"))
        self.assertEqual(intent["status"], "UNKNOWN")
        self.assertEqual(self.effects(), 1)                       # it DID happen
        # blind retry paths are closed
        self.assertEqual(actions.execute(self.k, Broker(self.ledger), *self.rt, ACTION, "k1", apr)["status"],
                         "UNKNOWN")
        self.assertRefused("INTENT_NOT_RETRYABLE", actions.redispatch, self.k, Broker(self.ledger), *self.rt,
                           intent["intent_id"], ACTION)
        self.assertEqual(self.effects(), 1)
        self.stop_run()
        self.assertEqual(goals.get_task(self.k, self.tid)["status"], "EXECUTION_UNKNOWN")
        self.assertEqual(goals.get(self.k, self.gid)["status"], "EXECUTION_UNKNOWN")
        self.assertRefused("GOAL_NOT_ACTIVE", runs.claim, self.k, self.tid, "fake_alt", "none")
        resolved = actions.reconcile(self.k, Broker(self.ledger), intent["intent_id"])
        self.assertEqual(resolved["status"], "CONFIRMED")
        self.assertEqual(goals.get_task(self.k, self.tid)["status"], "PENDING")
        self.assertEqual(self.effects(), 1)

    def test_unreachable_destination_keeps_the_intent_unknown(self):
        apr = self.approve()
        self.assertRefused("EXECUTION_UNKNOWN", actions.execute, self.k, Broker(self.ledger, "after"), *self.rt,
                           ACTION, "k1", apr)
        self.stop_run()
        intent_id = self.k.one("SELECT intent_id FROM action_intent")["intent_id"]

        class Down:
            def lookup(self, key):
                raise ConnectionError("still down")
        self.assertRefused("EXECUTION_UNKNOWN", actions.reconcile, self.k, Down(), intent_id)
        self.assertEqual(self.k.one("SELECT status FROM action_intent")["status"], "UNKNOWN")

    def test_confirmed_not_done_continues_the_same_intent_once(self):
        apr = self.approve()
        self.assertRefused("EXECUTION_UNKNOWN", actions.execute, self.k, Broker(self.ledger, "before"), *self.rt,
                           ACTION, "k1", apr)
        self.stop_run()
        intent_id = self.k.one("SELECT intent_id FROM action_intent")["intent_id"]
        self.assertEqual(actions.reconcile(self.k, Broker(self.ledger), intent_id)["status"], "NOT_DONE")
        self.new_run()
        self.assertRefused("APPROVAL_DIGEST_MISMATCH", actions.redispatch, self.k, Broker(self.ledger), *self.rt,
                           intent_id, {**ACTION, "artifact_hash": "other"})
        done = actions.redispatch(self.k, Broker(self.ledger), *self.rt, intent_id, ACTION)
        self.assertEqual((done["status"], done["dispatch_count"]), ("CONFIRMED", 2))
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM action_intent")[0], 1)   # no second intent

    def _crash(self, point):
        apr = self.approve()
        body = BROKER + (
            f"actions.execute(k, Broker({str(self.ledger)!r}), {self.rt[0]!r}, {self.rt[1]}, "
            f"{json.dumps(ACTION)}, 'k1', {apr!r})\n")
        proc = run_script(self.home, body, crash_at=point)
        self.assertEqual(proc.returncode, -9, proc.stderr)
        self.reopen()
        supervisor.recover(self.k)
        return apr

    def test_kill_right_after_approval_is_spent_leaves_a_traceable_unknown(self):
        apr = self._crash("action.after_intent_commit")
        self.assertEqual(self.effects(), 0)
        self.assertEqual(self.k.one("SELECT status FROM action_intent")["status"], "UNKNOWN")
        self.assertIsNotNone(self.k.one("SELECT consumed_by_intent FROM approval WHERE approval_id=?", apr)[0])
        self.assertEqual(goals.get_task(self.k, self.tid)["status"], "EXECUTION_UNKNOWN")
        intent_id = self.k.one("SELECT intent_id FROM action_intent")["intent_id"]
        self.assertEqual(actions.reconcile(self.k, Broker(self.ledger), intent_id)["status"], "NOT_DONE")

    def test_kill_after_the_effect_is_found_by_reconcile_and_not_repeated(self):
        self._crash("action.after_effect")
        self.assertEqual(self.effects(), 1)
        intent_id = self.k.one("SELECT intent_id FROM action_intent")["intent_id"]
        self.assertEqual(actions.reconcile(self.k, Broker(self.ledger), intent_id)["status"], "CONFIRMED")
        self.assertEqual(self.effects(), 1)

    def test_kill_before_the_intent_commit_spends_nothing(self):
        apr = self._crash("db.before_commit")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM action_intent")[0], 0)
        self.assertIsNone(self.k.one("SELECT consumed_at FROM approval WHERE approval_id=?", apr)[0])
        self.assertEqual(self.effects(), 0)


class UsageTests(Env):
    def setUp(self):
        super().setUp()
        self.g = self.goal(caps={"calls": 50, "attempts": 5, "active_ms": 10**8, "tokens": 10_000})
        run = runs.claim(self.k, self.task_ids(self.g["goal_id"])[0], "fake", "none")
        self.run_id = run["run_id"]

    def rec(self, event_id, seq, mode="cumulative", **vals):
        return usage.record(self.k, event_id=event_id, run_id=self.run_id, provider_session_id="s1", mode=mode,
                            sequence=seq, reported=vals, observation="measured", source="test")

    def tokens_used(self):
        return budget.snapshot(self.k, self.g["budget_id"])["tokens"]["used"]

    def test_resume_cumulative_totals_are_charged_once(self):
        self.rec("e1", 1, tokens_in=100, tokens_out=10)
        self.rec("e2", 2, tokens_in=250, tokens_out=30)     # resumed session reports the running total
        self.assertEqual(usage.totals(self.k, self.g["goal_id"])["totals"]["tokens_in"], 250)
        self.assertEqual(self.tokens_used(), 280)

    def test_replay_and_out_of_order_reports_add_nothing(self):
        self.rec("e1", 1, tokens_in=100)
        self.rec("e2", 2, tokens_in=250)
        self.assertTrue(self.rec("e2", 2, tokens_in=250)["replayed"])
        late = self.rec("e1-late", 1, tokens_in=100)
        self.assertEqual(late["anomaly"], "stale_sequence")
        self.assertEqual(self.tokens_used(), 250)

    def test_counter_going_down_is_charged_in_full_and_flagged(self):
        self.rec("e1", 1, tokens_in=500)
        out = self.rec("e2", 2, tokens_in=40)
        self.assertEqual((out["anomaly"], out["applied"]["tokens_in"]), ("counter_reset", 40))
        self.assertEqual(self.tokens_used(), 540)

    def test_usage_of_a_stopped_run_is_still_recorded(self):
        runs.begin_stop(self.k, self.run_id)
        runs.confirm_stopped(self.k, self.run_id, {"kind": "never_spawned"})
        self.rec("late", 1, mode="delta", tokens_in=70, tokens_out=5)
        self.assertEqual(self.tokens_used(), 75)
