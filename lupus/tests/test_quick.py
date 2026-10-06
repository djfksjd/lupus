import sys

from lupus import goals, projects, quick, supervisor

from .helpers import Env, fake

TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 2), 4)\n"
FIX = "import pathlib\npathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')\nopen('calls.log','a').write('x\\n')"


class FixTestsTests(Env):
    def setUp(self):
        super().setUp()
        (self.root / "test_calc.py").write_text(TEST)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def test_observed_failure_becomes_the_goal_with_frozen_tests(self):
        made = quick.fix_tests(self.k, self.project, "user")
        self.assertEqual((made["runner"], made["protect"]), ("unittest", ["test_calc.py"]))
        task = goals.tasks(self.k, made["goal_id"])[0]
        self.assertIn("AssertionError: 0 != 4", task["spec"]["prompt"])       # what the supervisor itself saw
        self.assertEqual(task["spec"]["inputs"], ["test_calc.py", "calc.py"])      # tests + the module they import
        report = supervisor.run_goal(self.k, made["goal_id"], fake(FIX))
        self.assertTrue(report["done"])
        self.assertEqual(len(self.calls()), 1)

    def test_green_suite_is_not_reported_as_work_done(self):
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        made = quick.fix_tests(self.k, self.project, "user")
        self.assertEqual(made["nothing_to_do"], "tests already pass")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM goal")[0], 0)

    def test_no_tests_is_refused(self):
        (self.root / "test_calc.py").unlink()
        self.assertRefused("NO_TESTS_FOUND", quick.fix_tests, self.k, self.project, "user")
        (self.root / "test_empty.py").write_text("x = 1\n")
        self.assertRefused("NO_TESTS_FOUND", quick.fix_tests, self.k, self.project, "user")
        self.assertRefused("USER_AUTHORITY_REQUIRED", quick.fix_tests, self.k, self.project, "worker")

    def test_cheating_on_the_tests_cannot_finish_the_goal(self):
        made = quick.fix_tests(self.k, self.project, "user")
        cheat = "import pathlib\npathlib.Path('test_calc.py').write_text('import unittest\\nclass T(unittest.TestCase):\\n    def test_x(self): pass\\n')"
        report = supervisor.run_goal(self.k, made["goal_id"], fake(cheat))
        self.assertFalse(report["done"])
        self.assertEqual((self.root / "test_calc.py").read_text(), TEST)


class DetectTests(Env):
    def test_pytest_settings_file_is_frozen_too(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        (self.root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = \"-q\"\n")
        try:
            found = quick.detect(self.root)
        except Exception as exc:                      # pytest not installed on this machine
            self.assertEqual(getattr(exc, "code", ""), "TEST_RUNNER_UNAVAILABLE")
            return
        self.assertEqual(found["runner"], "pytest")
        self.assertIn("pyproject.toml", found["protect"])


class SkipSatisfiedTests(Env):
    def test_work_finished_before_a_crash_is_verified_not_redone(self):
        from .helpers import WRITER, run_script
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        self.k.close()
        body = (f"a = adapters.FakeAdapter({WRITER!r})\nsupervisor.run_goal(k, {g['goal_id']!r}, a)\n")
        proc = run_script(self.home, body, crash_at="supervisor.after_worker")   # worker wrote a.txt, then we died
        self.assertEqual(proc.returncode, -9, proc.stderr)
        self.reopen()
        supervisor.recover(self.k)
        self.assertEqual(len(self.calls()), 1)
        report = supervisor.run_goal(self.k, g["goal_id"], fake())
        self.assertTrue(report["done"])
        self.assertEqual(report["steps"][0]["outcome"], "ALREADY_SATISFIED")
        self.assertEqual(len(self.calls()), 2)          # a.txt was NOT redone; only b.txt needed a call
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM evidence WHERE attempt_id IS NULL AND result='PASS'")[0], 1)


CALC = "def add(a, b):\n    return a + b\n"
GOOD_TEST = ("import unittest\nimport calc\nclass T(unittest.TestCase):\n"
             "    def test_sub(self): self.assertEqual(calc.sub(5, 3), 2)\n")
TOP_IMPORT = "import unittest\nfrom calc import sub\nclass T(unittest.TestCase):\n    def test_sub(self): self.assertEqual(sub(5, 3), 2)\n"
WRITE = "import pathlib, sys\nopen('calls.log','a').write('x\\n')\n"


class RequestTests(Env):
    """`lupus do`: draft a failing check, the user approves that exact check, then implement."""

    def setUp(self):
        super().setUp()
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(TEST)                    # existing, green

    def draft(self, body, request="calc 에 sub(a, b) 빼기 함수 추가"):
        from lupus.util import sha256_bytes
        d = quick.draft_check(self.k, self.project, request, "user")
        script = WRITE + body.replace("{T}", d["test_path"])
        report = supervisor.run_goal(self.k, d["goal_id"], fake(script))
        path = self.root / d["test_path"]
        return d, report, (sha256_bytes(path.read_bytes()) if path.exists() else "")

    def test_full_flow_red_then_approved_then_green(self):
        d, report, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % GOOD_TEST)
        self.assertTrue(report["done"])                                   # the supervisor saw it fail
        self.assertRefused("USER_AUTHORITY_REQUIRED", quick.approve_check, self.k, self.project, d["goal_id"],
                           d["request"], sha, "worker")
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        impl = WRITE + "pathlib.Path('calc.py').write_text(%r)" % (CALC + "def sub(a, b):\n    return a - b\n")
        done = supervisor.run_goal(self.k, build["goal_id"], fake(impl))
        self.assertTrue(done["done"], done)
        self.assertEqual(done["budget"]["attempts"]["used"], 1)
        self.assertRefused("CHECK_ALREADY_APPROVED", quick.approve_check, self.k, self.project, d["goal_id"],
                           d["request"], sha, "user")
        self.assertRefused("CHECK_IN_USE", quick.discard_draft, self.k, self.project, d["goal_id"], "user")
        self.assertTrue((self.root / d["test_path"]).exists())
        # frozen copies of finished goals are not kept in the database
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM protected_file WHERE content IS NOT NULL")[0], 0)

    def test_check_that_already_passes_or_does_not_parse_is_not_accepted(self):
        passing = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n"
        d, report, _ = self.draft("pathlib.Path('{T}').write_text(%r)" % passing)
        self.assertFalse(report["done"])
        self.assertIn("already passes", self.k.one("SELECT detail FROM evidence ORDER BY seq DESC LIMIT 1")[0])
        quick.discard_draft(self.k, self.project, d["goal_id"], "user")
        d, report, _ = self.draft("pathlib.Path('{T}').write_text('def broken(:\\n')", request="다른 요청 하나")
        self.assertFalse(report["done"])
        kept = quick.discard_draft(self.k, self.project, d["goal_id"], "user")
        self.assertFalse((self.root / d["test_path"]).exists())           # no broken test left in the project
        self.assertIn("def broken", open(kept).read())                    # but not lost
        quick.discard_draft(self.k, self.project, d["goal_id"], "user")
        d, report, _ = self.draft("pass", request="아무것도 안 쓰는 경우")
        self.assertFalse(report["done"])
        quick.discard_draft(self.k, self.project, d["goal_id"], "user")
        d, report, _ = self.draft("pathlib.Path('{T}').write_text(%r)" % TOP_IMPORT, request="맨 위 import 가 실패하는 경우")
        self.assertFalse(report["done"])                                  # failed to load: no behaviour was exercised
        self.assertIn("failed to load", self.k.one("SELECT detail FROM evidence ORDER BY seq DESC LIMIT 1")[0])

    def test_whole_project_freeze_refuses_files_it_could_not_put_back(self):
        (self.root / "secrets.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
        d = quick.draft_check(self.k, self.project, "sub 함수 추가", "user")
        report = supervisor.run_goal(self.k, d["goal_id"], fake(WRITE))
        self.assertEqual(self.calls(), [])                                # no worker was started
        self.assertEqual(report["steps"][0]["reason"], "PROTECT_UNRESTORABLE")

    def test_draft_step_cannot_touch_product_code_or_existing_tests(self):
        body = ("pathlib.Path('{T}').write_text(%r)\n"
                "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return 0\\n')\n"
                "pathlib.Path('test_calc.py').write_text('import unittest')\n"
                "pathlib.Path('extra_helper.py').write_text('x = 1')\n") % GOOD_TEST
        d, report, _ = self.draft(body)
        self.assertEqual((self.root / "calc.py").read_text(), CALC)       # put back
        self.assertEqual((self.root / "test_calc.py").read_text(), TEST)
        self.assertFalse((self.root / "extra_helper.py").exists())
        self.assertTrue((self.root / d["test_path"]).exists())            # the one allowed file stays
        self.assertTrue(report["done"])

    def test_only_the_exact_version_the_user_read_can_be_approved(self):
        d, _, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % GOOD_TEST)
        (self.root / d["test_path"]).write_text(GOOD_TEST.replace("2)", "999)"))     # swapped after review
        self.assertRefused("CHECK_CHANGED", quick.approve_check, self.k, self.project, d["goal_id"], d["request"],
                           sha, "user")

    def test_implementation_cannot_pass_by_editing_the_approved_check(self):
        d, _, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % GOOD_TEST)
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        cheat = WRITE + "pathlib.Path(%r).write_text('import unittest\\nclass T(unittest.TestCase):\\n    def test_x(self): pass\\n')" % d["test_path"]
        report = supervisor.run_goal(self.k, build["goal_id"], fake(cheat))
        self.assertFalse(report["done"])
        self.assertEqual((self.root / d["test_path"]).read_text(), GOOD_TEST)

    def test_new_collection_file_cannot_be_introduced_to_hide_the_check(self):
        d, _, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % GOOD_TEST)
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        sneaky = WRITE + ("pathlib.Path('conftest.py').write_text('collect_ignore_glob = [\"test_lupus_*\"]')\n"
                          "pathlib.Path('sitecustomize.py').write_text('import sys')\n")
        supervisor.run_goal(self.k, build["goal_id"], fake(sneaky))
        self.assertFalse((self.root / "conftest.py").exists())
        self.assertFalse((self.root / "sitecustomize.py").exists())

    def test_approved_check_must_run_all_its_tests(self):
        two = GOOD_TEST + "    def test_sub_neg(self): self.assertEqual(calc.sub(1, 3), -2)\n"
        d, _, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % two)
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        c0 = goals.criteria(self.k, build["goal_id"])[0]
        self.assertEqual(c0["verifier"]["min_tests"], 2)

    def test_discarding_an_interrupted_draft_restores_what_it_changed(self):
        d = quick.draft_check(self.k, self.project, "sub 함수 추가", "user")
        from lupus import protect
        protect.freeze(self.k, d["goal_id"], self.root)                  # the draft step had started
        (self.root / "calc.py").write_text("def add(a, b):\n    return 0\n")   # changed, never restored (crash)
        (self.root / d["test_path"]).write_text("broken draft")
        quick.discard_draft(self.k, self.project, d["goal_id"], "user")
        self.assertEqual((self.root / "calc.py").read_text(), CALC)
        self.assertFalse((self.root / d["test_path"]).exists())

    def test_regression_in_existing_tests_blocks_completion(self):
        d, _, sha = self.draft("pathlib.Path('{T}').write_text(%r)" % GOOD_TEST)
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        breaks_add = WRITE + "pathlib.Path('calc.py').write_text('def add(a, b):\\n    return 0\\ndef sub(a, b):\\n    return a - b\\n')"
        report = supervisor.run_goal(self.k, build["goal_id"], fake(breaks_add))
        self.assertFalse(report["done"])                                  # new check passes, old test broke
        self.assertIn("EVIDENCE_FAIL:c1", report["completion_blockers"])

    def test_red_baseline_and_bad_requests_are_refused(self):
        self.assertRefused("REQUEST_INVALID", quick.draft_check, self.k, self.project, "   ", "user")
        self.assertRefused("REQUEST_INVALID", quick.draft_check, self.k, self.project, "키 AKIAABCDEFGHIJKLMNOP 사용", "user")
        self.assertRefused("USER_AUTHORITY_REQUIRED", quick.draft_check, self.k, self.project, "x 추가", "alpha")
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        self.assertRefused("BASELINE_RED", quick.draft_check, self.k, self.project, "sub 추가", "user")
