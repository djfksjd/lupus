"""Lean path: files handed over in the prompt, single-pass instruction, lesson request only
after a failure, and lighter-setting-first tiers guarded by verification."""

import os

from lupus import goals, supervisor
from lupus.util import LupusError

from .helpers import LIAR, WRITER, Env, fake

ECHO = "import sys\nopen('prompt.txt','w').write(sys.argv[1])\n" + WRITER


class PromptTests(Env):
    def prompt(self):
        return (self.root / "prompt.txt").read_text()

    def test_declared_task_files_are_inlined_within_limits(self):
        (self.root / "a.txt").write_text("현재 내용 한 줄\n")
        g = self.goal({"a.txt": "A"})
        supervisor.run_task(self.k, g["goal_id"], fake(ECHO))
        text = self.prompt()
        self.assertIn("--- 현재 파일 a.txt ---\n현재 내용 한 줄\n--- a.txt 끝 ---", text)
        self.assertIn("직접 테스트나 검증 명령을 실행하지 말고", text)

    def test_large_secret_binary_symlinked_and_outside_files_are_not_inlined(self):
        cases = {"big.txt": b"x" * 7000, "key.txt": b"-----BEGIN RSA PRIVATE KEY-----\nabc", "bin.dat": b"\xff\xfe\x00"}
        for name, data in cases.items():
            (self.root / name).write_bytes(data)
        (self.root / "ok.txt").write_text("SAFE-CONTENT")
        outside = self.tmp / "outside.txt"
        outside.write_text("OUTSIDE-CONTENT")
        os.symlink(outside, self.root / "link.txt")
        text = "\n".join(supervisor._inline_files(self.k, self.root, [*cases, "link.txt", "../outside.txt", "ok.txt"]))
        self.assertIn("SAFE-CONTENT", text)                        # the one legitimate file is there
        for marker in ("PRIVATE KEY", "OUTSIDE-CONTENT", "xxxxxxxx", "big.txt", "bin.dat", "link.txt"):
            self.assertNotIn(marker, text)

    def test_total_inline_size_is_capped(self):
        for i in range(4):
            (self.root / f"f{i}.txt").write_text(f"FILE{i}-" + "y" * 4990)
        text = "\n".join(supervisor._inline_files(self.k, self.root, [f"f{i}.txt" for i in range(4)]))
        self.assertEqual([f"FILE{i}-" in text for i in range(4)], [True, True, False, False])   # 12 KB cap

    def test_inlining_can_be_switched_off(self):
        (self.root / "a.txt").write_text("현재 내용")
        self.set_policy(context_inline_bytes=0)
        g = self.goal({"a.txt": "A"})
        supervisor.run_task(self.k, g["goal_id"], fake(ECHO))
        self.assertNotIn("현재 파일", self.prompt())

    def test_lesson_is_requested_only_after_a_failed_attempt(self):
        g = self.goal({"a.txt": "A"})
        supervisor.run_task(self.k, g["goal_id"], fake("import sys\nopen('prompt.txt','w').write(sys.argv[1])\n"
                                                      "open('a.txt','w').write('wrong')"))
        self.assertNotIn("LESSON", self.prompt())                 # first try: no extra output asked for
        supervisor.run_task(self.k, g["goal_id"], fake(ECHO))
        self.assertIn("LESSON", self.prompt())                    # retry after a verified failure


class RetryTests(Env):
    def test_retry_gets_the_verifiers_real_failure_output_and_may_self_check(self):
        import sys
        from lupus import goals as G
        check = "import sys; print('AssertionError: expected 42 got 41'); sys.exit(1 if open('a.txt').read() != '42' else 0)"
        crit = {"id": "c0", "text": "a.txt 가 42 다", "verifier": {"kind": "command", "argv": [sys.executable, "-c", check],
                                                               "paths": ["a.txt"]}}
        g = G.submit(self.k, self.project["project_id"], "o", [crit], {"calls": 200, "attempts": 20, "active_ms": 10**8})
        G.add_task(self.k, g["goal_id"], "t", "write a.txt 41", ["c0"])
        echo = "import sys\nopen('prompt.txt','w').write(sys.argv[1])\nopen('a.txt','w').write('41')"
        supervisor.run_task(self.k, g["goal_id"], fake(echo))
        first = (self.root / "prompt.txt").read_text()
        self.assertIn("직접 테스트나 검증 명령을 실행하지 말고", first)
        supervisor.run_task(self.k, g["goal_id"], fake(echo.replace("'41'", "'42'")))
        second = (self.root / "prompt.txt").read_text()
        self.assertIn("AssertionError: expected 42 got 41", second)        # the verifier's own words
        self.assertNotIn("직접 테스트나 검증 명령을 실행하지 말고", second)   # careful pass: self-check allowed
        self.assertEqual(G.get_task(self.k, self.task_ids(g["goal_id"])[0])["status"], "DONE")

    def test_secret_in_verifier_output_is_never_stored_or_fed_back(self):
        import sys
        from lupus import goals as G
        check = "import sys; print('token AKIAABCDEFGHIJKLMNOP leaked'); sys.exit(1)"
        crit = {"id": "c0", "text": "check", "verifier": {"kind": "command", "argv": [sys.executable, "-c", check],
                                                         "paths": ["a.txt"]}}
        g = G.submit(self.k, self.project["project_id"], "o", [crit], {"calls": 200, "attempts": 20, "active_ms": 10**8})
        G.add_task(self.k, g["goal_id"], "t", "write a.txt 1", ["c0"])
        supervisor.run_task(self.k, g["goal_id"], fake())
        self.assertNotIn("AKIA", self.k.one("SELECT detail FROM evidence")[0])


class TierTests(Env):
    def tier(self, script, name):
        adapter = fake(script)
        adapter.variant = name
        return adapter

    def variants(self, goal_id):
        return [r["note"] for r in self.k.q("SELECT outcome_note AS note FROM attempt WHERE goal_id = ? ORDER BY rowid",
                                            goal_id)]

    def test_light_tier_alone_when_it_passes_verification(self):
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(), tiers=[self.tier(WRITER, "light"),
                                                                           self.tier(LIAR, "default")])
        self.assertTrue(report["done"])
        self.assertEqual([s["variant"] for s in report["steps"]], ["light"])

    def test_default_tier_is_used_only_after_the_light_one_failed_verification(self):
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(), tiers=[self.tier(LIAR, "light"),
                                                                           self.tier(WRITER, "default")])
        self.assertTrue(report["done"])
        self.assertEqual([s["variant"] for s in report["steps"]], ["light", "default"])
        self.assertEqual(report["budget"]["attempts"]["used"], 2)          # escalation is an ordinary attempt

    def test_escalation_does_not_buy_extra_attempts(self):
        g = self.goal()
        report = supervisor.run_goal(self.k, g["goal_id"], fake(), tiers=[self.tier(LIAR, "light"),
                                                                           self.tier(LIAR, "default")])
        self.assertEqual(report["goal_status"], "NO_PROGRESS")
        self.assertEqual(goals.tasks(self.k, g["goal_id"])[0]["attempt_count"], 2)
        self.assertEqual(len(self.calls()), 2)

    def test_tiers_must_be_the_same_driver(self):
        g = self.goal()
        with self.assertRaises(LupusError) as ctx:
            supervisor.run_goal(self.k, g["goal_id"], fake(), tiers=[fake(driver="fake_alt")])
        self.assertEqual(ctx.exception.code, "TIERS_INVALID")


class BatchTests(Env):
    def batching(self, script=WRITER):
        adapter = fake(script)
        adapter.batch = True
        return adapter

    ALL = ("import sys, pathlib\nopen('calls.log','a').write('call\\n')\n"
           "for line in sys.argv[1].splitlines():\n    if line.startswith('write '):\n"
           "        _, name, text = line.split(' ', 2); pathlib.Path(name).write_text(text)\n")

    def test_one_call_covers_a_chain_and_each_task_is_verified_on_its_own(self):
        g = self.goal({"a.txt": "A", "b.txt": "B", "c.txt": "C"})
        report = supervisor.run_goal(self.k, g["goal_id"], self.batching(self.ALL))
        self.assertTrue(report["done"])
        self.assertEqual(len(self.calls()), 1)                               # three tasks, one worker call
        self.assertEqual([s["outcome"] for s in report["steps"]], ["PROGRESS", "ALREADY_SATISFIED", "ALREADY_SATISFIED"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM evidence WHERE result = 'PASS'")[0], 3)   # per-task evidence
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM checkpoint")[0] >= 4, True)                # per-task checkpoints
        self.assertEqual(report["budget"]["attempts"]["used"], 1)

    def test_follower_the_call_did_not_finish_gets_its_own_attempt_with_feedback(self):
        only_first = ("import sys, pathlib\nopen('calls.log','a').write('call\\n')\n"
                      "lines = [l for l in sys.argv[1].splitlines() if l.startswith('write ')]\n"
                      "_, name, text = lines[0].split(' ', 2); pathlib.Path(name).write_text(text)\n"
                      "open('prompt.txt','w').write(sys.argv[1])\n")
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        report = supervisor.run_goal(self.k, g["goal_id"], self.batching(only_first))
        self.assertTrue(report["done"])
        self.assertEqual(len(self.calls()), 2)                               # b needed its own call
        self.assertIn("이전 시도는 아래 검증에 실패했다", (self.root / "prompt.txt").read_text())

    def test_batch_size_is_capped_and_non_batching_drivers_do_not_batch(self):
        g = self.goal({f"{n}.txt": n for n in "abcde"})
        supervisor.run_goal(self.k, g["goal_id"], self.batching(self.ALL))
        self.assertEqual(len(self.calls()), 2)                               # 3 + 2 with the default cap of 3
        (self.root / "calls.log").unlink()
        g2 = self.goal({f"{n}2.txt": n for n in "abc"})
        supervisor.run_goal(self.k, g2["goal_id"], fake(self.ALL))
        self.assertEqual(len(self.calls()), 3)

    def test_follower_rides_along_once_and_then_answers_to_its_own_limits(self):
        # a passes on its second call; b never passes. b may ride along once, then answers to its own limits.
        script = ("import sys, pathlib\nlog = pathlib.Path('calls.log')\nn = len(log.read_text().splitlines()) if log.exists() else 0\n"
                  "log.open('a').write('call\\n')\n"
                  "if n >= 1: pathlib.Path('a.txt').write_text('A')\n")
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        report = supervisor.run_goal(self.k, g["goal_id"], self.batching(script))
        tasks = {t["title"]: t for t in goals.tasks(self.k, g["goal_id"])}
        self.assertEqual(tasks["task a.txt"]["status"], "DONE")
        # b rode along once, then got its own attempt; an identical second try is refused before any call
        self.assertEqual((tasks["task b.txt"]["status"], tasks["task b.txt"]["attempt_count"]), ("NO_PROGRESS", 1))
        self.assertEqual(len(self.calls()), 3)          # a (+b riding), a retry alone, b alone — bounded
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM event WHERE type = 'batch.planned'")[0], 1)
