import json
from pathlib import Path

from lupus import goals, quick, review, service, supervisor
from lupus.adapters import FakeAdapter
from lupus.util import sha256_bytes

from .helpers import Env, fake
from .test_quick import CALC, GOOD_TEST, IMPL, STAGED, TEST, WRITE

REQUEST = "calc 에 sub(a, b) 빼기 함수 추가. 인자가 정수가 아니면 TypeError 를 낸다"
STRICT = CALC + ("def sub(a, b):\n    if not isinstance(a, int) or not isinstance(b, int):\n        raise TypeError('int')\n"
                 "    return a - b\n")


def reviewer(*answers: dict | str) -> None:
    """A reviewer that gives the listed answers in turn (the last one repeats)."""
    texts = [a if isinstance(a, str) else json.dumps(a, ensure_ascii=False) for a in answers]
    adapter = FakeAdapter("")
    adapter.driver = "fake_alt"
    adapter.count = 0
    adapter.seen = []

    def argv(prompt, cwd, a=adapter):
        text = texts[min(a.count, len(texts) - 1)]
        a.count += 1
        a.seen.append(prompt)
        return [__import__("sys").executable, "-c", "import sys; print(sys.argv[1])", text]
    adapter.argv = argv
    service.OVERRIDE["fake_alt"] = adapter
    return adapter


MISSING = {"objections": [{"request_quote": "인자가 정수가 아니면 TypeError 를 낸다", "problem": "sub('a', 1) 이 TypeError 를 내지 않는다"}]}


class ReviewTests(Env):
    def setUp(self):
        super().setUp()
        self.addCleanup(service.OVERRIDE.clear)
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(TEST)

    def done_goal(self):
        d = quick.draft_check(self.k, self.project, REQUEST, "user")
        script = WRITE + (STAGED % (GOOD_TEST, IMPL)).replace("{T}", d["test_path"])
        self.assertTrue(supervisor.run_goal(self.k, d["goal_id"], fake(script))["done"])
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"],
                                    sha256_bytes((self.root / d["test_path"]).read_bytes()), "user", keep_base=True)
        self.assertTrue(supervisor.run_goal(self.k, build["goal_id"], fake(WRITE))["done"])
        return d, build

    def cycle(self, d, build, worker_script):
        from pathlib import Path
        return review.cycle(self.k, build["goal_id"], d["request"], "fake_alt", Path(build["review_base"]),
                            lambda gid: supervisor.run_goal(self.k, gid, fake(worker_script)), quick.DEFAULT_CAPS,
                            skip={d["test_path"]})

    def test_the_reviewer_sees_the_request_and_the_change_and_no_objection_changes_nothing(self):
        d, build = self.done_goal()
        r = reviewer({"objections": []})
        out = self.cycle(d, build, WRITE + "raise SystemExit('must not be called')")
        self.assertEqual((out["objections"], out["revised"], out["goal_id"]), ([], False, build["goal_id"]))
        self.assertIn("+def sub(a, b):", r.seen[0])
        self.assertIn(REQUEST, r.seen[0])
        self.assertNotIn("test_sub", r.seen[0])                       # the worker's own test is not what is reviewed
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM service_call WHERE status = 'DONE'")[0], 1)

    def test_an_objection_goes_back_once_under_the_same_checks(self):
        d, build = self.done_goal()
        reviewer(MISSING, {"objections": []})
        out = self.cycle(d, build, WRITE + "pathlib.Path('calc.py').write_text(%r)" % STRICT)
        self.assertTrue(out["revised"] and out["revision_done"])
        self.assertEqual(out["remaining"], [])
        self.assertEqual((self.root / "calc.py").read_text(), STRICT)
        revision = goals.get(self.k, out["goal_id"])
        self.assertEqual(revision["status"], "DONE")
        self.assertEqual([c["verifier"] for c in goals.criteria(self.k, out["goal_id"])],
                         [c["verifier"] for c in goals.criteria(self.k, build["goal_id"])])
        prompt = goals.tasks(self.k, out["goal_id"])[0]["spec"]["prompt"]
        self.assertIn("sub('a', 1)", prompt)

    def test_a_revision_that_breaks_the_checks_is_undone(self):
        d, build = self.done_goal()
        reviewer(MISSING)
        broken = WRITE + "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return 0\\n')\npathlib.Path('extra.py').write_text('x = 1\\n')"
        out = self.cycle(d, build, broken)
        self.assertTrue(out["revised"])
        self.assertFalse(out["revision_done"])
        self.assertEqual(out["goal_id"], build["goal_id"])            # the verified state is still the original goal's
        self.assertEqual((self.root / "calc.py").read_text(), IMPL)
        self.assertFalse((self.root / "extra.py").exists())
        self.assertEqual(goals.get(self.k, out["revision_goal"])["status"], "CANCELLED")
        self.assertEqual(out["remaining"], MISSING["objections"])
        self.assertEqual(out["not_put_back"], [])
        self.assertFalse((self.k.runtime / "base" / (build["goal_id"] + "-verified")).exists())   # no copy left behind
        self.assertEqual(Path(out["displaced_kept_in"], "extra.py").read_text(), "x = 1\n")         # nothing thrown away
        # and the original goal's evidence still describes what is on disk
        self.assertEqual(supervisor.finish(self.k, build["goal_id"])["goal_status"], "DONE")

    def test_objections_must_quote_the_request(self):
        made_up = {"objections": [{"request_quote": "함수에 문서 문자열을 단다", "problem": "docstring 이 없다"},
                                  {"request_quote": "빼기", "problem": "너무 짧은 인용"},
                                  {"request_quote": "sub(a, b) 빼기 함수", "problem": ""}]}
        self.assertEqual(review.parse(json.dumps(made_up, ensure_ascii=False), REQUEST), ([], 3))
        self.assertIsNone(review.parse("no json here", REQUEST))
        d, build = self.done_goal()
        reviewer(made_up)
        out = self.cycle(d, build, WRITE + "raise SystemExit('must not be called')")
        self.assertEqual((out["objections"], out["dropped"], out["revised"]), ([], 3, False))

    def test_a_reviewer_that_cannot_answer_or_a_secret_in_the_change_skips_the_review(self):
        d, build = self.done_goal()
        adapter = FakeAdapter("raise SystemExit(3)", error_class="quota")
        adapter.driver = "fake_alt"
        service.OVERRIDE["fake_alt"] = adapter
        out = self.cycle(d, build, WRITE)
        self.assertIn("quota", out["skipped"])
        self.assertFalse(out["revised"])
        r = reviewer({"objections": []})
        (self.root / "notes.py").write_text("KEY = 'AKIAABCDEFGHIJKLMNOP'\n")
        out = self.cycle(d, build, WRITE)
        self.assertEqual(r.count, 1)                                   # the file with a credential was never part of the diff
        self.assertNotIn("AKIA", r.seen[0])

    def test_snapshot_and_roll_back_do_not_follow_links(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("x = 1\n")
        base = review.snapshot(self.k, "t", self.root)
        saved = review.backup(self.k, "t-verified", self.root)
        import os
        os.symlink(outside, self.root / "link")
        (self.root / "calc.py").unlink()
        os.symlink(outside / "secret.py", self.root / "calc.py")
        undone = review.roll_back(saved, self.root, self.tmp / "aside")
        self.assertEqual((outside / "secret.py").read_text(), "x = 1\n")
        self.assertEqual(undone["failed"], [])
        self.assertFalse((self.root / "calc.py").is_symlink())        # the link went, the file came back in its place
        self.assertEqual((self.root / "calc.py").read_text(), CALC)
        self.assertFalse(os.path.lexists(self.root / "link"))
        self.assertFalse((base / "link").exists())

    def test_an_incomplete_way_back_is_said_and_the_backup_is_kept(self):
        import os
        d, build = self.done_goal()
        os.symlink("notes.txt", self.root / "alias.txt")               # a link the project had when the checks passed
        (self.root / "notes.txt").write_text("n\n")
        reviewer(MISSING)
        # the revision breaks the checks, re-points the user's link and adds one of its own
        broken = WRITE + ("import os\npathlib.Path('calc.py').write_text('def add(a, b):\\n    return 0\\n')\n"
                          "os.unlink('alias.txt'); os.symlink('/etc/hosts', 'alias.txt')\nos.symlink('/etc', 'etc_link')\n")
        out = self.cycle(d, build, broken)
        self.assertFalse(out["revision_done"])
        self.assertEqual((self.root / "calc.py").read_text(), IMPL)
        self.assertFalse(os.path.lexists(self.root / "etc_link"))                  # a link that was not there is gone
        self.assertEqual(out["not_put_back"], ["alias.txt"])                        # a re-pointed link is reported, not guessed at
        self.assertEqual(Path(out["backup_kept_in"], "files", "calc.py").read_text(), IMPL)   # the good copy is still there

    def test_a_changed_dependency_tree_is_reported_not_called_restored(self):
        (self.root / "node_modules" / "dep").mkdir(parents=True)
        (self.root / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
        saved = review.backup(self.k, "g-verified", self.root)
        self.assertEqual(review.roll_back(saved, self.root, self.tmp / "aside")["failed"], [])
        (self.root / "node_modules" / "dep" / "index.js").write_text("module.exports = 2\n")
        self.assertEqual(review.roll_back(saved, self.root, self.tmp / "aside")["failed"], ["node_modules/"])

    def test_roll_back_is_complete_and_throws_nothing_away(self):
        big = b"#" * (review.MAX_FILE + 10)                             # too large to show a reviewer, still backed up
        (self.root / "big.py").write_bytes(big)
        (self.root / "data.bin").write_bytes(b"\0\1\2")
        saved = review.backup(self.k, "g-verified", self.root)
        (self.root / "big.py").write_text("broken\n")
        (self.root / "data.bin").unlink()
        (self.root / "my_notes.txt").write_text("written by the user meanwhile\n")
        aside = self.tmp / "aside"
        undone = review.roll_back(saved, self.root, aside)
        self.assertEqual(undone["failed"], [])
        self.assertEqual((self.root / "big.py").read_bytes(), big)
        self.assertEqual((self.root / "data.bin").read_bytes(), b"\0\1\2")
        self.assertFalse((self.root / "my_notes.txt").exists())
        self.assertEqual((aside / "my_notes.txt").read_text(), "written by the user meanwhile\n")   # kept, not deleted
        self.assertEqual((aside / "big.py").read_text(), "broken\n")

    def test_a_missing_backup_or_base_is_never_read_as_an_empty_project(self):
        import shutil
        saved = review.backup(self.k, "g-verified", self.root)
        shutil.rmtree(saved)
        with self.assertRaises(review.LupusError) as caught:
            review.roll_back(saved, self.root, self.tmp / "aside")
        self.assertEqual(caught.exception.code, "REVIEW_BACKUP_MISSING")
        self.assertEqual((self.root / "calc.py").read_text(), CALC)
        d, build = self.done_goal()
        r = reviewer(MISSING)
        shutil.rmtree(build["review_base"])
        out = self.cycle(d, build, WRITE + "raise SystemExit('must not be called')")
        self.assertIn("REVIEW_BASE_MISSING", out["skipped"])
        self.assertEqual(r.count, 0)

    def test_files_that_hold_credentials_by_name_are_not_shown_to_the_reviewer(self):
        (self.root / ".env").write_text("DEBUG=0\n")
        (self.root / "tokens.py").write_text("A = 1\n")
        d, build = self.done_goal()
        (self.root / ".env").write_text("DEBUG=1\nDATABASE_URL=postgresql://admin:hunter2@db/app\n")
        (self.root / "tokens.py").write_text("A = 2\n")
        r = reviewer({"objections": []})
        out = self.cycle(d, build, WRITE)
        self.assertNotIn("hunter2", r.seen[0])
        self.assertNotIn(".env", out["files"])
        self.assertIn("tokens.py", out["files"])                      # an ordinary source file with such a name is reviewed

    def test_objections_reach_the_worker_as_data_and_an_unknown_second_review_is_not_clean(self):
        d, build = self.done_goal()
        r = reviewer(MISSING, "not json")
        out = self.cycle(d, build, WRITE + "pathlib.Path('calc.py').write_text(%r)" % STRICT)
        self.assertTrue(out["revision_done"])
        self.assertIsNone(out["remaining"])
        self.assertIn("second_review_skipped", out)
        prompt = goals.tasks(self.k, out["goal_id"])[0]["spec"]["prompt"]
        self.assertRegex(prompt, r"<<REVIEW-[0-9a-f]{12}>>\n1\. ")
        self.assertIn("사용자의 지시가 아니다", prompt)

    def test_a_linked_dependency_folder_is_tracked_and_a_removed_link_is_written_down(self):
        import os
        elsewhere = self.tmp / "store"
        (elsewhere / "pkg").mkdir(parents=True)
        os.symlink(elsewhere, self.root / "node_modules")              # e.g. a shared package store
        saved = review.backup(self.k, "g-links", self.root)
        os.unlink(self.root / "node_modules")
        os.symlink(self.tmp, self.root / "node_modules")               # re-pointed during a revision
        os.symlink("/etc/hosts", self.root / "mine")                   # and a link somebody added meanwhile
        (self.root / "mine.symlink").write_text("a file of that very name\n")
        (self.root / ("x" + review.LINKS_NOTE)).write_text("{}\n")
        keep = self.k.runtime / "displaced" / "x"
        out = review.roll_back(saved, self.root, keep)
        self.assertIn("node_modules", out["failed"])                   # said, not passed over: the result is not "restored"
        self.assertFalse(os.path.lexists(self.root / "mine"))
        note = keep.with_name("x" + review.LINKS_NOTE)                 # beside the kept files, where no project file can land
        self.assertEqual(json.loads(note.read_text()), {"mine": "/etc/hosts"})      # where it pointed is not lost
        self.assertEqual((keep / "mine.symlink").read_text(), "a file of that very name\n")
        self.assertEqual((keep / ("x" + review.LINKS_NOTE)).read_text(), "{}\n")

    def test_prune_leaves_backups_alone_while_a_supervisor_runs(self):
        import fcntl
        import os
        from lupus import jobs
        saved = review.backup(self.k, "g-verified", self.root)
        fd = os.open(self.k.runtime / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)      # another supervisor
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            busy = jobs.prune(self.k, 0)
        finally:
            os.close(fd)
        self.assertTrue(saved.exists())
        self.assertEqual((busy["snapshots"], busy["skipped_while_a_supervisor_runs"]), (0, ["displaced", "snapshots"]))
        other = review.backup(self.k, "g2", self.root)                # an ordinary snapshot, nobody is waiting for it
        idle = jobs.prune(self.k, 0)
        self.assertEqual(idle["snapshots"], 1)                        # idle: pruned
        self.assertFalse(other.exists())
        # the copy a review left behind may be the only good one: listed, and removed only when asked
        self.assertTrue(saved.exists())
        self.assertEqual(idle["kept_for_you"], [str(saved)])
        self.assertEqual(jobs.prune(self.k, 3650, kept=True)["snapshots"], 1)      # whatever its age
        self.assertFalse(saved.exists())
