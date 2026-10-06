"""A worker must not be able to pass by changing what is checked."""

import sys

from lupus import goals, protect, supervisor, verify

from .helpers import Env, fake

CAPS = {"calls": 200, "attempts": 20, "active_ms": 10**8}
TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 2), 4)\n"
RUN = [sys.executable, "-m", "unittest", "-q", "tests.test_calc"]


class ProtectTests(Env):
    def setUp(self):
        super().setUp()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_calc.py").write_text(TEST)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        crit = {"id": "c0", "text": "tests.test_calc 통과", "verifier": {
            "kind": "command", "argv": RUN, "paths": ["calc.py", "tests"], "protect": ["tests"],
            "require_tests": "unittest"}}
        self.g = goals.submit(self.k, self.project["project_id"], "add 고치기", [crit], CAPS)
        goals.add_task(self.k, self.g["goal_id"], "fix", "calc.py 의 add 를 고쳐라", ["c0"])
        self.gid = self.g["goal_id"]

    def worker(self, body):
        return fake("import pathlib, sys\nopen('prompt.txt','w').write(sys.argv[1])\n" + body)

    def test_editing_the_test_to_pass_is_undone_and_reported_to_the_retry(self):
        cheat = "pathlib.Path('tests/test_calc.py').write_text('import unittest\\nclass T(unittest.TestCase):\\n    def test_ok(self): pass\\n')"
        step = supervisor.run_task(self.k, self.gid, self.worker(cheat))
        self.assertEqual(step["verdicts"], {"c0": "FAIL"})                    # the real test ran, not the fake one
        self.assertEqual((self.root / "tests" / "test_calc.py").read_text(), TEST)
        kept = list((self.home / "runtime" / "displaced").rglob("test_calc.py"))
        self.assertEqual(len(kept), 1)                                         # the displaced version is not lost
        self.assertIn("def test_ok", kept[0].read_text())
        fix = "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')"
        step = supervisor.run_task(self.k, self.gid, self.worker(fix))
        self.assertIn("보호된 검증 파일을 건드려 되돌렸다", (self.root / "prompt.txt").read_text())
        self.assertEqual(step["status"], "DONE")

    def test_deleting_tests_or_adding_a_conftest_does_not_help(self):
        cheat = ("pathlib.Path('tests/test_calc.py').unlink()\n"
                 "pathlib.Path('tests/conftest.py').write_text('collect_ignore_glob = [\"*\"]')\n"
                 "pathlib.Path('tests/test_easy.py').write_text('import unittest')")
        step = supervisor.run_task(self.k, self.gid, self.worker(cheat))
        self.assertEqual(step["verdicts"], {"c0": "FAIL"})
        self.assertEqual(sorted(p.name for p in (self.root / "tests").iterdir() if p.is_file()),
                         ["__init__.py", "test_calc.py"])

    def test_change_made_outside_lupus_is_not_overwritten_and_needs_the_user(self):
        supervisor.run_task(self.k, self.gid, self.worker("pass"))            # freezes; attempt fails
        (self.root / "tests" / "test_calc.py").write_text(TEST + "# the user edited this\n")
        step = supervisor.run_task(self.k, self.gid, self.worker("pass"))
        self.assertEqual(step["reason"], "PROTECTED_CHANGED_OUTSIDE")
        self.assertIn("the user edited this", (self.root / "tests" / "test_calc.py").read_text())   # untouched
        tid = self.task_ids(self.gid)[0]
        self.assertEqual(goals.get_task(self.k, tid)["status"], "NEEDS_ANSWER")
        self.assertRefused("USER_AUTHORITY_REQUIRED", protect.refreeze, self.k, self.gid, self.root, "supervisor")
        protect.refreeze(self.k, self.gid, self.root, "user")
        goals.resolve_wait(self.k, tid, "user", "test edit is intended")
        fix = "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')"
        self.assertEqual(supervisor.run_task(self.k, self.gid, self.worker(fix))["status"], "DONE")

    def test_runner_that_finds_no_tests_is_not_a_pass(self):
        v = {"kind": "command", "argv": [sys.executable, "-m", "unittest", "-q", "nonexistent_pkg_zz"], "paths": ["calc.py"],
             "require_tests": "unittest"}
        self.assertEqual(verify.run(v, self.root)[0], "FAIL")
        v = {"kind": "command", "argv": [sys.executable, "-c", "print('Ran 0 tests in 0.0s')"], "paths": ["calc.py"],
             "require_tests": "unittest"}
        self.assertEqual(verify.run(v, self.root)[:3:2], ("FAIL", "no tests ran"))

    def test_directory_evidence_tracks_the_files_inside(self):
        v = {"kind": "command", "argv": RUN, "paths": ["tests"]}
        first = verify.artifact_hash(v, self.root)
        (self.root / "tests" / "test_calc.py").write_text(TEST + "\n")
        self.assertNotEqual(first, verify.artifact_hash(v, self.root))
        self.assertNotEqual(verify.artifact_hash({"paths": ["nope"]}, self.root), first)

    def test_credential_like_protected_file_is_detected_but_never_stored(self):
        (self.root / "tests" / "fixture.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        protect.freeze(self.k, self.gid, self.root)
        row = self.k.one("SELECT content FROM protected_file WHERE path = 'tests/fixture.pem'")
        self.assertIsNone(row["content"])
        (self.root / "tests" / "fixture.pem").write_text("changed")
        self.assertEqual(protect.restore(self.k, self.gid, self.root)["unrestorable"], ["tests/fixture.pem"])

    def test_paths_must_be_project_relative(self):
        for bad in (str(self.root / "tests"), "../tests", "~/tests"):
            crit = {"id": "c0", "text": "t", "verifier": {"kind": "command", "argv": RUN, "paths": ["calc.py"],
                                                         "protect": [bad]}}
            self.assertRefused("CRITERION_INVALID", goals.submit, self.k, self.project["project_id"], "o", [crit], CAPS)

    def test_restore_never_writes_through_a_symlinked_directory(self):
        import os, shutil
        protect.freeze(self.k, self.gid, self.root)
        outside = self.tmp / "outside"
        outside.mkdir()
        shutil.rmtree(self.root / "tests")
        os.symlink(outside, self.root / "tests")                     # worker swapped the directory for a link
        undone = protect.restore(self.k, self.gid, self.root, keep_dir=self.home / "runtime" / "displaced" / "x")
        self.assertEqual(list(outside.iterdir()), [])                 # nothing written outside the project
        self.assertIn("tests/test_calc.py", undone["unrestorable"])

    def test_tests_left_modified_by_an_interrupted_worker_are_put_back(self):
        from .helpers import run_script
        cheat = "import pathlib\npathlib.Path('tests/test_calc.py').write_text('import unittest')"
        self.k.close()
        body = f"a = adapters.FakeAdapter({cheat!r})\nsupervisor.run_goal(k, {self.gid!r}, a)\n"
        proc = run_script(self.home, body, crash_at="supervisor.after_worker")
        self.assertEqual(proc.returncode, -9, proc.stderr)
        self.reopen()
        supervisor.recover(self.k)
        self.assertNotEqual((self.root / "tests" / "test_calc.py").read_text(), TEST)   # still tampered on disk
        fix = "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')"
        step = supervisor.run_task(self.k, self.gid, self.worker(fix))
        self.assertEqual((self.root / "tests" / "test_calc.py").read_text(), TEST)      # restored, not asked
        self.assertEqual(step["status"], "DONE")

    def test_goal_cannot_complete_while_protected_files_differ(self):
        fix = "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')"
        self.assertEqual(supervisor.run_task(self.k, self.gid, self.worker(fix))["status"], "DONE")
        (self.root / "tests" / "test_calc.py").write_text(TEST + "# changed after the task finished\n")
        report = supervisor.run_goal(self.k, self.gid, self.worker("pass"))
        self.assertFalse(report["done"])
        self.assertIn("PROTECTED_CHANGED_OUTSIDE", report["completion_blockers"])

    def test_symlinked_tests_are_refused_before_any_worker_runs(self):
        import os
        (self.root / "fixture_real.py").write_text(TEST)
        os.symlink(self.root / "fixture_real.py", self.root / "tests" / "test_link.py")
        self.assertRefused("PROTECTED_SYMLINK", protect.freeze, self.k, self.gid, self.root)
        step = supervisor.run_task(self.k, self.gid, self.worker("pass"))
        self.assertEqual((step["status"], step["reason"]), ("NEEDS_ANSWER", "PROTECTED_SYMLINK"))
        self.assertFalse((self.root / "prompt.txt").exists())          # no worker was dispatched
        self.assertEqual(self.k.one("SELECT status FROM run")[0], "STOPPED")   # writer slot released

    def test_directory_symlink_retarget_changes_the_evidence_hash(self):
        import os
        for name in ("good", "bad"):
            (self.root / name).mkdir()
        (self.root / "src").mkdir()
        os.symlink(self.root / "good", self.root / "src" / "shared")
        v = {"kind": "command", "argv": RUN, "paths": ["src"]}
        first = verify.artifact_hash(v, self.root)
        os.unlink(self.root / "src" / "shared")
        os.symlink(self.root / "bad", self.root / "src" / "shared")
        self.assertNotEqual(first, verify.artifact_hash(v, self.root))
