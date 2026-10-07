"""A request that changes what existing tests pin: the user releases named files, the worker's
edits to them are a proposal, and only the exact diff the user approves becomes the new frozen
state. Nothing is ever allowed to fail."""

import json

from lupus import goals, quick, release, supervisor
from lupus.util import sha256_bytes

from .helpers import Env, fake

CALC = "def add(a, b):\n    return a + b\n"
OLD_TEST = ("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(2, 2), 4)\n    def test_zero(self): self.assertEqual(add(0, 0), 0)\n")
OTHER = "import unittest\nclass O(unittest.TestCase):\n    def test_other(self): self.assertTrue(True)\n"
NEW_CHECK = "import unittest\nimport calc\nclass N(unittest.TestCase):\n    def test_text(self): self.assertEqual(calc.add(2, 2), '4')\n"
IMPL = "def add(a, b):\n    return str(a + b)\n"
UPDATED = OLD_TEST.replace("add(2, 2), 4", "add(2, 2), '4'").replace("add(0, 0), 0", "add(0, 0), '0'")
W = "import pathlib\n"
REQUEST = "add 는 합을 문자열로 돌려준다"


class Release(Env):
    def setUp(self):
        super().setUp()
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(OLD_TEST)
        (self.root / "test_other.py").write_text(OTHER)
        d = quick.draft_check(self.k, self.project, REQUEST, "user")
        self.check = d["test_path"]
        script = W + f"pathlib.Path({self.check!r}).write_text({NEW_CHECK!r})\npathlib.Path('calc.py').write_text({IMPL!r})\n"
        self.assertTrue(supervisor.run_goal(self.k, d["goal_id"], fake(script))["done"])
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha256_bytes((self.root / self.check).read_bytes()), "user")
        self.goal_id = build["goal_id"]

    def run_with(self, script, steps=3):
        return supervisor.run_goal(self.k, self.goal_id, fake(W + script), max_steps=steps)

    def stuck(self):
        """The approved check passes with the proposed implementation; the old test cannot."""
        report = self.run_with("print('done')")
        self.assertFalse(report["done"])
        self.assertEqual(goals.tasks(self.k, self.goal_id)[0]["status"], "NO_PROGRESS")
        return report

    def proposed(self, edit=f"pathlib.Path('test_calc.py').write_text({UPDATED!r})\n"):
        self.stuck()
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        return self.run_with(edit, steps=1)

    def test_a_goal_stuck_on_existing_tests_says_so_and_names_the_way_out(self):
        self.stuck()
        said = release.hint(self.k, self.goal_id)
        self.assertIn("승인한 검사는 통과하지만 기존 테스트가 실패합니다", said)
        self.assertIn(f"lupus release {self.goal_id}", said)
        self.assertIn("의도한 변경이 아니면", said)                      # both answers are offered; no diagnosis is made

    def test_release_authorises_a_proposal_and_the_frozen_versions_are_still_what_is_verified(self):
        report = self.proposed()
        self.assertFalse(report["done"])
        task = goals.tasks(self.k, self.goal_id)[0]
        self.assertEqual((task["status"], task["no_progress_streak"]), ("NEEDS_APPROVAL", 0))     # waiting is not a failed try
        self.assertEqual((self.root / "test_calc.py").read_text(), OLD_TEST)       # put back: not adopted by being written
        found = release.proposal(self.k, self.goal_id)
        self.assertEqual([(e["path"], e["change"]) for e in found["entries"]], [("test_calc.py", "modified")])
        self.assertEqual(goals.latest_evidence(self.k, self.goal_id)["c1"]["result"], "FAIL")     # evidence is from the frozen versions
        shown = release.show(self.k, self.goal_id)
        self.assertIn("-    def test_add(self): self.assertEqual(add(2, 2), 4)", shown)
        self.assertIn("+    def test_add(self): self.assertEqual(add(2, 2), '4')", shown)
        self.assertIn("미리 돌려 본 결과(참고용이며 완료의 근거가 아닙니다): c0 PASS, c1 PASS", shown)
        self.assertIn(f"lupus release-approve {self.goal_id}", release.hint(self.k, self.goal_id))

    def test_the_worker_is_told_every_released_file_however_many_times_the_user_released(self):
        self.stuck()
        task = goals.tasks(self.k, self.goal_id)[0]
        self.assertNotIn("변경을 허용한 보호 파일", supervisor.build_prompt(self.k, task, root=self.root))
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        release.grant(self.k, self.goal_id, ["test_other.py"], "user")      # the task is no longer waiting: no second note
        prompt = supervisor.build_prompt(self.k, goals.tasks(self.k, self.goal_id)[0], root=self.root)
        self.assertIn("변경을 허용한 보호 파일(테스트·fixture): test_calc.py, test_other.py.", prompt)
        self.assertIn("테스트를 지워서 통과시키지 마라", prompt)

    def test_only_the_exact_diff_that_was_shown_can_be_approved_and_then_everything_is_checked_again(self):
        self.proposed()
        found = release.proposal(self.k, self.goal_id)
        self.assertRefused("USER_AUTHORITY_REQUIRED", release.approve, self.k, self.goal_id, found["digest"], "supervisor")
        self.assertRefused("RELEASE_CHANGED", release.approve, self.k, self.goal_id, "0" * 64, "user")
        before = goals.get(self.k, self.goal_id)["acceptance_revision"]
        out = release.approve(self.k, self.goal_id, found["digest"], "user")
        self.assertEqual((out["approved"], out["acceptance_revision"]), (["test_calc.py"], before + 1))
        self.assertEqual((self.root / "test_calc.py").read_text(), UPDATED)
        self.assertIsNone(release.proposal(self.k, self.goal_id))
        # the new frozen state is the old one with that file replaced: the other tests and the approved check are as before
        rows = {r["path"]: r["sha256"] for r in self.k.q(
            "SELECT path, sha256 FROM protected_file WHERE goal_id = ? AND acceptance_revision = ?", self.goal_id, before + 1)}
        self.assertEqual(rows["test_calc.py"], sha256_bytes(UPDATED.encode()))
        self.assertEqual((rows["test_other.py"], rows[self.check]), (sha256_bytes(OTHER.encode()), sha256_bytes(NEW_CHECK.encode())))
        self.assertTrue(all(ev is None for ev in goals.latest_evidence(self.k, self.goal_id).values()))      # old evidence no longer counts
        done = self.run_with("raise SystemExit('no model call is needed')")
        self.assertTrue(done["done"], done)
        self.assertEqual(done["steps"][-1]["outcome"], "ALREADY_SATISFIED")
        self.assertRefused("GOAL_TERMINAL", release.grant, self.k, self.goal_id, ["test_calc.py"], "user")

    def test_edits_outside_what_was_released_are_undone_as_always_and_never_enter_the_proposal(self):
        self.proposed(f"pathlib.Path('test_calc.py').write_text({UPDATED!r})\npathlib.Path('test_other.py').write_text('# gutted\\n')\n"
                      f"pathlib.Path({self.check!r}).write_text('# gutted\\n')\n")
        self.assertEqual([e["path"] for e in release.proposal(self.k, self.goal_id)["entries"]], ["test_calc.py"])
        self.assertEqual(((self.root / "test_other.py").read_text(), (self.root / self.check).read_text()), (OTHER, NEW_CHECK))

    def test_checking_an_approved_change_needs_no_attempt_and_a_file_to_be_added_is_released_by_name(self):
        self.proposed()
        goal = goals.get(self.k, self.goal_id)
        self.k.run("UPDATE budget_line SET cap = used WHERE budget_id = ? AND dimension = 'attempts'", goal["budget_id"])      # none left
        release.approve(self.k, self.goal_id, release.proposal(self.k, self.goal_id)["digest"], "user")
        done = self.run_with("raise SystemExit('no model call')")
        self.assertTrue(done["done"], done)                               # the user's decision is checked, not refused for budget
        self.assertEqual(done["steps"][-1]["outcome"], "ALREADY_SATISFIED")

    def test_a_folder_and_a_file_that_does_not_exist_yet_can_be_named(self):
        (self.root / "calc.py").write_text(CALC)
        other = self.tmp / "p2"
        (other / "tests" / "data").mkdir(parents=True)
        (other / "tests" / "data" / "a.json").write_text("{}\n")
        (other / "tests" / "test_x.py").write_text(OTHER)
        from lupus import projects, protect
        project = projects.register(self.k, other, "p2", ["local"])
        goal = goals.submit(self.k, project["project_id"], "g", [{"id": "c0", "text": "t", "verifier": {
            "kind": "command", "argv": ["/usr/bin/true"], "paths": ["."], "protect": ["tests"], "forbid_new_names": ["conftest.py"]}}], {"calls": 9, "attempts": 3, "active_ms": 99999}, actor="user")
        goals.add_task(self.k, goal["goal_id"], "t", "p", ["c0"])
        protect.freeze(self.k, goal["goal_id"], other)
        got = release.grant(self.k, goal["goal_id"], ["tests/data/new.json", "tests/data/a.json"], "user")["released"]
        self.assertEqual(got, ["tests/data/a.json", "tests/data/new.json"])        # files, not folders
        self.assertIn("tests/data/", release.grant(self.k, goal["goal_id"], ["tests/data"], "user")["released"])
        # inside a released folder, a file that changes what the runner collects still stays frozen
        self.assertTrue(release._stays_frozen("tests/data/conftest.py", release._not_releasable(self.k, goal["goal_id"])))
        (other / "tests" / "data" / "conftest.py").write_text("# a collection hook\n")
        (other / "tests" / "data" / "b.json").write_text("[]\n")
        captured = release.capture(self.k, goal["goal_id"], other)
        self.assertEqual([e["path"] for e in captured["entries"]], ["tests/data/b.json"])
        self.assertFalse(release._is_released("tests/test_x.py", release.granted(self.k, goal["goal_id"])))

    def test_what_cannot_be_released(self):
        self.stuck()
        for path in (self.check, "calc.py", "../outside.py", "nowhere/test_x.py", "."):
            self.assertRefused("RELEASE_REFUSED", release.grant, self.k, self.goal_id, [path], "user")
        self.assertRefused("USER_AUTHORITY_REQUIRED", release.grant, self.k, self.goal_id, ["test_calc.py"], "supervisor")
        self.assertEqual(release.granted(self.k, self.goal_id), [])

    def test_a_refused_proposal_leaves_the_frozen_tests_standing_and_tells_the_worker(self):
        self.proposed()
        release.reject(self.k, self.goal_id, "user", "0 은 숫자로 남아야 한다")
        task = goals.tasks(self.k, self.goal_id)[0]
        self.assertEqual(task["status"], "PENDING")
        self.assertIn("0 은 숫자로 남아야 한다", goals.resolutions(self.k, task["task_id"])[-1])
        self.assertIsNone(release.proposal(self.k, self.goal_id))
        self.assertEqual((self.root / "test_calc.py").read_text(), OLD_TEST)
        self.assertFalse(self.run_with("print('done')", steps=1)["done"])            # and the old suite still has to pass

    def test_a_proposal_made_against_an_earlier_state_or_altered_while_held_is_refused(self):
        self.proposed()
        found = release.proposal(self.k, self.goal_id)
        held = self.k.runtime / "released" / self.goal_id / "files" / "test_calc.py"
        good = held.read_bytes()
        held.write_text("# swapped while it waited\n")
        self.assertRefused("RELEASE_CHANGED", release.approve, self.k, self.goal_id, found["digest"], "user")
        held.write_bytes(good)
        goal = goals.get(self.k, self.goal_id)
        goals.revise_acceptance(self.k, self.goal_id, goals.criteria(self.k, self.goal_id), "user", "something else changed",
                                goal["acceptance_revision"])
        self.assertRefused("RELEASE_STALE", release.approve, self.k, self.goal_id, found["digest"], "user")
        self.assertEqual((self.root / "test_calc.py").read_text(), OLD_TEST)

    def test_tests_that_would_disappear_are_named_and_approval_never_means_allowed_to_fail(self):
        gutted = OLD_TEST.replace("    def test_zero(self): self.assertEqual(add(0, 0), 0)\n", "")      # drops a test, fixes nothing
        self.proposed(f"pathlib.Path('test_calc.py').write_text({gutted!r})\n")
        shown = release.show(self.k, self.goal_id)
        self.assertIn("!! 없어지는 테스트 1개: test_zero", shown)
        self.assertIn("c1 FAIL", shown)                                   # tried with the proposal: the suite still fails
        release.approve(self.k, self.goal_id, release.proposal(self.k, self.goal_id)["digest"], "user")
        report = self.run_with("print('done')", steps=1)
        self.assertFalse(report["done"])                                  # approved, and still not done: the suite must pass
        self.assertEqual(goals.latest_evidence(self.k, self.goal_id)["c1"]["result"], "FAIL")

    def test_a_released_file_the_worker_deleted_or_added_is_part_of_the_diff(self):
        self.stuck()
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        self.run_with("pathlib.Path('test_calc.py').unlink()\n", steps=1)
        entry = release.proposal(self.k, self.goal_id)["entries"][0]
        self.assertEqual((entry["path"], entry["change"], entry["new_sha256"]), ("test_calc.py", "deleted", None))
        self.assertIn("=== 삭제: test_calc.py", release.show(self.k, self.goal_id))
        self.assertIn("!! 없어지는 테스트 2개", release.show(self.k, self.goal_id))
        self.assertTrue((self.root / "test_calc.py").exists())           # still there until the user decides
        release.approve(self.k, self.goal_id, release.proposal(self.k, self.goal_id)["digest"], "user")
        self.assertFalse((self.root / "test_calc.py").exists())
        self.assertTrue(self.run_with("raise SystemExit('not needed')")["done"])

    def test_recovery_of_a_cut_off_approval_uses_only_what_was_committed(self):
        self.proposed()
        found = release.proposal(self.k, self.goal_id)
        real = release.apply
        release.apply = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("killed here"))
        self.addCleanup(setattr, release, "apply", real)
        with self.assertRaises(RuntimeError):
            release.approve(self.k, self.goal_id, found["digest"], "user")
        release.apply = real
        # while it lay there, the held copy and the list were changed (the digest left as it was)
        held = self.k.runtime / "released" / self.goal_id
        (held / "files" / "test_calc.py").write_text("# not what was approved\n")
        manifest = json.loads((held / "proposal.json").read_text())
        manifest["entries"].append({"path": "test_other.py", "change": "deleted", "old_sha256": "x", "new_sha256": None, "mode": None})
        (held / "proposal.json").write_text(json.dumps(manifest))
        self.assertTrue(release.complete(self.k, self.goal_id, self.root))
        self.assertEqual((self.root / "test_calc.py").read_text(), UPDATED)        # the approved bytes, from the frozen state
        self.assertEqual((self.root / "test_other.py").read_text(), OTHER)         # and nothing the approval did not name
        self.assertFalse(release.complete(self.k, self.goal_id, self.root))

    def test_the_whole_diff_is_shown_and_nothing_in_it_can_steer_the_terminal(self):
        padding = "".join(f"    # line {i}\n" for i in range(700))
        sneaky = OLD_TEST.replace("class T", "# \x1b[2J\x1b[H cleared \u202e\nclass T") + padding + "    def test_zero(self): self.assertTrue(True)\n"
        self.proposed(f"pathlib.Path('test_calc.py').write_text({sneaky!r})\n")
        shown = release.show(self.k, self.goal_id)
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\u202e", shown)
        self.assertIn("<U+001B>[2J", shown)                               # printed as what it is, not obeyed
        self.assertIn("+    def test_zero(self): self.assertTrue(True)", shown)      # the weakened line, 700 lines down
        self.assertIn("+    # line 699", shown)

    def test_a_change_that_is_only_a_control_character_is_still_a_visible_change(self):
        self.proposed(f"pathlib.Path('test_calc.py').write_text({OLD_TEST!r}.replace('(add(2, 2), 4)', '(add(2, 2),\\x0b4)'))\n")
        shown = release.show(self.k, self.goal_id)
        self.assertIn("+    def test_add(self): self.assertEqual(add(2, 2),<U+000B>4)", shown)

    def test_a_deleted_file_stays_deleted_and_a_trial_of_another_proposal_is_not_shown(self):
        self.stuck()
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        self.run_with("pathlib.Path('test_calc.py').unlink()\n", steps=1)
        first = release.proposal(self.k, self.goal_id)
        self.assertIn("참고용이며", release.show(self.k, self.goal_id))
        # a different proposal for which no trial was recorded must not borrow this one's result
        held = self.k.runtime / "released" / self.goal_id / "proposal.json"
        other = {**first, "digest": "f" * 64}
        held.write_text(json.dumps(other))
        self.assertIn("미리 돌려 본 결과가 없습니다", release.show(self.k, self.goal_id))
        held.write_text(json.dumps(first))
        release.approve(self.k, self.goal_id, first["digest"], "user")
        row = self.k.one("SELECT sha256 FROM protected_file WHERE goal_id = ? AND path = 'test_calc.py' ORDER BY acceptance_revision DESC",
                         self.goal_id)
        self.assertEqual(row["sha256"], "absent")                         # the deletion is frozen too
        self.k.run("UPDATE budget_line SET cap = cap + 5 WHERE dimension = 'attempts'")
        (self.root / "calc.py").write_text("def add(a, b):\n    return 'wrong'\n")      # so that another worker is needed
        self.run_with(f"pathlib.Path('test_calc.py').write_text({OLD_TEST!r})\npathlib.Path('calc.py').write_text({IMPL!r})\n", steps=1)
        self.assertFalse((self.root / "test_calc.py").exists())           # a worker cannot bring it back unapproved

    def test_a_file_whose_deletion_was_approved_can_be_released_again_and_brought_back_by_approval(self):
        self.stuck()
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        self.run_with("pathlib.Path('test_calc.py').unlink()\n", steps=1)
        release.approve(self.k, self.goal_id, release.proposal(self.k, self.goal_id)["digest"], "user")
        self.k.run("UPDATE budget_line SET cap = cap + 5 WHERE dimension = 'attempts'")
        (self.root / "calc.py").write_text("def add(a, b):\n    return 'wrong'\n")      # not done yet: the approved check fails
        self.assertRefused("RELEASE_REFUSED", release.grant, self.k, self.goal_id, ["pytest.ini"], "user")      # an absent runner config is not that
        release.grant(self.k, self.goal_id, ["test_calc.py"], "user")
        self.run_with(f"pathlib.Path('test_calc.py').write_text({UPDATED!r})\n", steps=1)
        back = release.proposal(self.k, self.goal_id)
        self.assertEqual([(e["path"], e["change"], e["old_sha256"]) for e in back["entries"]], [("test_calc.py", "added", None)])
        self.assertFalse((self.root / "test_calc.py").exists())
        release.approve(self.k, self.goal_id, back["digest"], "user")
        self.assertEqual((self.root / "test_calc.py").read_text(), UPDATED)

    def test_the_approval_survives_a_crash_between_the_record_and_the_files(self):
        self.proposed()
        found = release.proposal(self.k, self.goal_id)
        real = release.apply
        release.apply = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("killed here"))
        self.addCleanup(setattr, release, "apply", real)
        with self.assertRaises(RuntimeError):
            release.approve(self.k, self.goal_id, found["digest"], "user")
        release.apply = real
        self.assertEqual((self.root / "test_calc.py").read_text(), OLD_TEST)       # the files were not written…
        self.assertTrue(self.run_with("raise SystemExit('not needed')")["done"])    # …and the frozen state put them right
        self.assertEqual((self.root / "test_calc.py").read_text(), UPDATED)
