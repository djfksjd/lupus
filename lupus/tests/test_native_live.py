"""Real Claude Code <-> Codex CLI continuation on a synthetic project.

Skipped unless LUPUS_LIVE=1: it makes a handful of small requests through the user's
subscriptions. Nothing global is modified; the project and runtime are temporary.
"""

import os
import unittest

from lupus import adapters, budget, goals, probe, projects, recovery, supervisor

from .helpers import Env

CAPS = {"calls": 60, "attempts": 6, "active_ms": 1_200_000, "tokens": 2_000_000}


@unittest.skipUnless(os.environ.get("LUPUS_LIVE") == "1", "set LUPUS_LIVE=1 to call the real CLIs")
class NativeHandoffTests(Env):
    _probe = None

    def setUp(self):
        super().setUp()
        self.k.run("UPDATE project SET approved_providers = '[\"anthropic\",\"local\",\"openai\"]'")
        cls = type(self)
        if cls._probe is None:      # one live probe for the whole class
            report = probe.run(self.k, live=True)
            cls._probe = (report, [tuple(r) for r in self.k.q("SELECT * FROM capability")])
        else:
            self.k.conn.executemany("INSERT INTO capability VALUES (?,?,?,?,?,?,?,?)", cls._probe[1])
        self.report = cls._probe[0]
        projects.allow_unconfined_reads(self.k, self.project["project_id"], "user")   # synthetic temp project
        self.set_policy(batch_max_tasks=1)     # this test is about handoff: one task per call

    def two_step_goal(self):
        crits = [
            {"id": "c0", "text": "notes.txt 파일의 첫 줄이 정확히 LUPUS-STEP-1 이다",
             "verifier": {"kind": "file_contains", "path": "notes.txt", "text": "LUPUS-STEP-1"}},
            {"id": "c1", "text": "notes.txt 파일의 둘째 줄이 정확히 LUPUS-STEP-2 이고 첫 줄은 그대로다",
             "verifier": {"kind": "command", "paths": ["notes.txt"], "argv": [
                 "python3", "-c",
                 "import sys; l=open('notes.txt').read().split(); sys.exit(0 if l[:2]==['LUPUS-STEP-1','LUPUS-STEP-2'] else 1)"]}},
        ]
        goal = goals.submit(self.k, self.project["project_id"], "두 단계 메모 파일 작성", crits, CAPS)
        t1 = goals.add_task(self.k, goal["goal_id"], "1단계", "notes.txt 파일을 만들고 첫 줄에 LUPUS-STEP-1 을 써라.", ["c0"])
        goals.add_task(self.k, goal["goal_id"], "2단계",
                       "기존 notes.txt 의 첫 줄은 그대로 두고 둘째 줄에 LUPUS-STEP-2 를 추가하라.", ["c1"], [t1["task_id"]])
        return goal

    def handoff(self, first, second):
        goal = self.two_step_goal()
        gid = goal["goal_id"]
        one = supervisor.run_goal(self.k, gid, adapters.native(first), max_steps=1, timeout_s=300)
        self.assertEqual([s["status"] for s in one["steps"]], ["DONE"], one)
        self.assertFalse(one["done"])
        mid = budget.snapshot(self.k, goal["budget_id"])
        two = supervisor.run_goal(self.k, gid, adapters.native(second), timeout_s=300)
        self.assertTrue(two["done"], two)
        self.assertEqual((self.root / "notes.txt").read_text().split(), ["LUPUS-STEP-1", "LUPUS-STEP-2"])
        row = self.k.one("SELECT source_adapter, target_adapter, status FROM handoff WHERE goal_id=?", gid)
        self.assertEqual(tuple(row), (first, second, "ACCEPTED"))
        self.assertEqual([t["attempt_count"] for t in goals.tasks(self.k, gid)], [1, 1])   # step 1 not redone
        end = budget.snapshot(self.k, goal["budget_id"])
        self.assertEqual(end["attempts"]["used"], 2)
        self.assertGreater(end["tokens"]["used"], mid["tokens"]["used"])                   # carried on, not reset
        self.assertEqual(two["usage"]["events_by_observation"], {"measured": 2})
        (self.root / "notes.txt").unlink()
        return two

    def test_probe_measured_both_clis(self):
        for name in ("native_claude", "native_codex"):
            caps = {(c["name"], c["mode"]): c["status"] for c in self.report["adapters"][name]["capabilities"]}
            self.assertEqual(caps[("subscription_auth", "default_auth_stripped")], "verified")
            self.assertEqual(caps[("headless_exec", "default_auth_stripped")], "verified")
            self.assertEqual(caps[("subscription_auth", "isolated_profile")], "unsupported")
            self.assertEqual(caps[("isolation", "default_auth_stripped")], "unsupported")

    def test_claude_to_codex(self):
        self.handoff("native_claude", "native_codex")

    def test_codex_to_claude(self):
        self.handoff("native_codex", "native_claude")
