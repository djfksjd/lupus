"""Runners other than Python: detection, frozen inputs, reading the runner's own summary."""

import shutil
import sys
import unittest
import unittest.mock

from lupus import goals, quick, runners, supervisor, verify

from .helpers import Env, fake

NODE = shutil.which("node")
JS_TEST = ("import test from 'node:test'; import assert from 'node:assert';\nimport { add } from '../calc.mjs';\n"
           "test('adds', () => assert.equal(add(2, 2), 4));\n")
JS_FIX = ("import pathlib\npathlib.Path('calc.mjs').write_text('export const add = (a, b) => a + b;\\n')\n"
          "open('calls.log','a').write('x\\n')")


@unittest.skipUnless(NODE, "node is not installed")
class NodeTests(Env):
    def setUp(self):
        super().setUp()
        (self.root / "package.json").write_text('{"name": "demo", "type": "module"}\n')
        (self.root / "test").mkdir()
        (self.root / "test" / "calc.test.mjs").write_text(JS_TEST)
        (self.root / "calc.mjs").write_text("export const add = (a, b) => a - b;\n")

    def test_failing_node_tests_become_the_goal_and_are_frozen(self):
        made = quick.fix_tests(self.k, self.project, "user")
        self.assertEqual((made["runner"], made["protect"]), ("node", ["test", "package.json"]))
        task = goals.tasks(self.k, made["goal_id"])[0]
        self.assertIn("# fail 1", task["spec"]["prompt"])
        self.assertEqual(task["spec"]["inputs"], ["test/calc.test.mjs", "calc.mjs"])
        cheat = "import pathlib\npathlib.Path('test/calc.test.mjs').write_text(\"import test from 'node:test'; test('x', () => {});\\n\")"
        self.assertFalse(supervisor.run_goal(self.k, made["goal_id"], fake(cheat))["done"])
        self.assertEqual((self.root / "test" / "calc.test.mjs").read_text(), JS_TEST)

    def test_fixing_the_code_finishes_and_green_is_not_work(self):
        made = quick.fix_tests(self.k, self.project, "user")
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(JS_FIX))["done"])
        self.assertEqual(quick.fix_tests(self.k, self.project, "user")["nothing_to_do"], "tests already pass")

    def test_exit_zero_with_no_tests_is_not_a_pass(self):
        (self.root / "test" / "calc.test.mjs").unlink()
        v = {"kind": "command", "argv": [NODE, "--test", "--test-reporter=tap"], "paths": ["."], "require_tests": "node"}
        self.assertEqual(verify.run(v, self.root)[::2], ("FAIL", "no tests ran"))

    def test_a_request_gets_a_red_test_that_really_ran(self):
        (self.root / "calc.mjs").write_text("export const add = (a, b) => a + b;\n")
        draft = quick.draft_check(self.k, self.project, "곱셈 mul 추가", "user")
        self.assertRegex(draft["test_path"], r"^test/lupus_[0-9a-f]{8}\.test\.mjs$")
        # a top-level import of something missing: the file never loads, so nothing was shown
        top = (f"import pathlib\npathlib.Path({draft['test_path']!r}).write_text(\"import test from 'node:test';"
               "import { mul } from '../calc.mjs';\\ntest('m', () => {});\\n\")")
        report = supervisor.run_goal(self.k, draft["goal_id"], fake(top), max_steps=1)
        self.assertFalse(report["done"])
        ev = goals.latest_evidence(self.k, draft["goal_id"])["c0"]
        self.assertIn("failed to load", ev["detail"])
        good = (f"import pathlib\npathlib.Path({draft['test_path']!r}).write_text(\"import test from 'node:test';"
                "import assert from 'node:assert';\\ntest('m', async () => { const m = await import('../calc.mjs');"
                " assert.equal(m.mul(2, 3), 6); });\\n\")\nopen('calls.log','a').write('y\\n')")
        self.assertTrue(supervisor.run_goal(self.k, draft["goal_id"], fake(good))["done"])


class ReadingTests(unittest.TestCase):
    def test_counts_and_red_judgement_from_recorded_runner_output(self):
        self.assertEqual(runners.passed("jest", "Tests:       1 failed, 2 passed, 3 total\n"), 2)
        self.assertEqual(runners.passed("vitest", " Test Files  1 passed (1)\n      Tests  4 passed (4)\n"), 4)
        self.assertEqual(runners.passed("go", "=== RUN   TestA\n--- PASS: TestA (0.00s)\n    --- PASS: TestA/x (0.00s)\nok  \tdemo\t0.1s\n"), 2)
        self.assertEqual(runners.passed("cargo", "test result: ok. 2 passed; 0 failed;\n\ntest result: ok. 1 passed; 0 failed;\n"), 3)
        self.assertEqual(runners.passed("cargo", "running 0 tests\n\ntest result: ok. 0 passed; 0 failed;\n"), 0)
        self.assertIsNone(runners.passed("custom", "anything"))
        # compiled: a build failure is red only when the requested name is what is missing
        self.assertIsNone(runners.judge_red("go", "lupus_x_test.go", "# demo [demo.test]\n./lupus_x_test.go:6:10: undefined: Mul\nFAIL\tdemo [build failed]\n"))
        self.assertIn("does not build", runners.judge_red("go", "lupus_x_test.go", "# demo [demo.test]\n./lupus_x_test.go:3:8: \"os\" imported and not used\nFAIL\tdemo [build failed]\n"))
        rs = "error[E0425]: cannot find function `mul` in crate `demo`\n --> tests/lupus_x.rs:2:20\n  |\nerror: could not compile `demo`\n"
        self.assertIsNone(runners.judge_red("cargo", "tests/lupus_x.rs", rs))
        self.assertIn("does not build", runners.judge_red("cargo", "tests/lupus_x.rs", rs.replace("E0425", "E0308")))
        # text that merely looks like the diagnostic (compile_error!, a second unrelated error, another file) is not enough
        self.assertIn("does not build", runners.judge_red("cargo", "tests/lupus_x.rs",
                      "error: error[E0425]: intentional failure\n --> tests/lupus_x.rs:1:1\nerror: could not compile `demo`\n"))
        self.assertIn("does not build", runners.judge_red("cargo", "tests/lupus_x.rs", rs.replace("tests/lupus_x.rs", "src/lib.rs")))
        self.assertIn("does not build", runners.judge_red("go", "lupus_x_test.go",
                      "# demo [demo.test]\n./lupus_x_test.go:6:10: undefined: Mul\n./lupus_x_test.go:3:8: \"os\" imported and not used\nFAIL\tdemo [build failed]\n"))
        self.assertIsNone(runners.judge_red("go", "lupus_x_test.go",
                          "# demo [demo.test]\n./lupus_x_test.go:7:7: c.Mul undefined (type Calculator has no field or method Mul)\nFAIL\tdemo [build failed]\n"))
        self.assertIsNone(runners.judge_red("go", "x_test.go", "=== RUN   TestLupusMul\n--- FAIL: TestLupusMul (0.00s)\nFAIL\n"))
        self.assertIn("no test", runners.judge_red("jest", "a.test.js", "Tests:       2 passed, 2 total\n"))
        self.assertIn("failed to load", runners.judge_red("jest", "a.test.js", "FAIL ./a.test.js\n  ● Test suite failed to run\n"))

    def test_test_files_and_counts_per_language(self):
        self.assertTrue(runners.is_test_file("node", "src/a.spec.ts") and runners.is_test_file("go", "pkg/a_test.go"))
        self.assertTrue(runners.is_test_file("rust", "tests/api.rs") and not runners.is_test_file("rust", "src/lib.rs"))
        self.assertFalse(runners.is_test_file("node", "src/contest.js"))
        self.assertEqual(runners.count_tests("node", "test('a', () => {});\n  it('b', () => {});\n"), 2)
        self.assertEqual(runners.count_tests("go", "func TestA(t *testing.T) {}\nfunc helper() {}\nfunc TestB(t *testing.T) {}\n"), 2)
        self.assertEqual(runners.count_tests("cargo", "#[test]\nfn a() {}\n#[ test ]\nfn b() {}\n#[tokio::test(flavor = \"x\")]\nasync fn c() {}\n"), 3)


class CheckCommandTests(Env):
    def test_any_project_with_a_user_named_command(self):
        (self.root / "check.sh").write_text("#!/bin/sh\ngrep -q fixed state.txt\n")
        (self.root / "check.sh").chmod(0o755)
        (self.root / "state.txt").write_text("broken\n")
        made = quick.fix_tests(self.k, self.project, "user", check="./check.sh", protect_paths=["check.sh"])
        self.assertEqual((made["runner"], made["protect"]), ("custom", ["check.sh"]))
        cheat = "import pathlib\npathlib.Path('check.sh').write_text('#!/bin/sh\\nexit 0\\n')"
        self.assertFalse(supervisor.run_goal(self.k, made["goal_id"], fake(cheat), max_steps=1)["done"])
        fix = "import pathlib\npathlib.Path('state.txt').write_text('fixed\\n')"
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(fix))["done"])

    def test_bare_program_names_never_resolve_into_the_project(self):
        (self.root / "sh").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "sh").chmod(0o755)
        self.assertEqual(quick.check_argv(self.root, "sh -c 'exit 1'")[0], shutil.which("sh"))
        self.assertRefused("CHECK_INVALID", quick.check_argv, self.root, "'unterminated")
        self.assertRefused("TEST_RUNNER_UNAVAILABLE", quick.check_argv, self.root, "no-such-program-xyz")


class SrcLayoutTests(Env):
    def test_a_package_under_src_is_tested_from_source_without_installing_it(self):
        (self.root / "src" / "pkg").mkdir(parents=True)
        (self.root / "src" / "pkg" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_pkg.py").write_text(
            "import unittest\nfrom pkg import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 2), 4)\n")
        made = quick.fix_tests(self.k, self.project, "user")
        task = goals.tasks(self.k, made["goal_id"])[0]
        self.assertIn("AssertionError: 0 != 4", task["spec"]["prompt"])            # it ran, it did not fail to import
        self.assertIn("src/pkg/__init__.py", task["spec"]["inputs"])
        fix = "import pathlib\npathlib.Path('src/pkg/__init__.py').write_text('def add(a, b):\\n    return a + b\\n')"
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(fix))["done"])


class SummaryTests(Env):
    """What the code under test prints must not be read as the runner's verdict."""

    @unittest.skipUnless(NODE, "node is not installed")
    def test_a_printed_pass_count_is_not_the_runners_summary(self):
        (self.root / "package.json").write_text('{"type": "module"}\n')
        (self.root / "t.test.mjs").write_text(
            "import test from 'node:test';\nconsole.log('pass 20');\ntest('skipped', {skip: true}, () => {});\n")
        v = {"kind": "command", "argv": [NODE, "--test", "--test-reporter=tap"], "paths": ["."], "require_tests": "node", "min_tests": 1}
        self.assertEqual(verify.run(v, self.root)[::2], ("FAIL", "no tests ran"))
        self.assertEqual(runners.passed("node", "# pass 20\n# tests 1\n# suites 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 1\n# todo 0\n# duration_ms 3.1\n"), 0)

    def test_a_run_in_which_every_test_was_skipped_ran_nothing(self):
        (self.root / "test_s.py").write_text("import unittest\nprint('Ran 99 tests')\n@unittest.skip('x')\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        v = {"kind": "command", "argv": [sys.executable, "-m", "unittest", "-q", "test_s"], "paths": ["."], "require_tests": "unittest"}
        self.assertEqual(verify.run(v, self.root)[::2], ("FAIL", "no tests ran"))
        self.assertEqual(runners.passed("unittest", "Ran 5 tests in 0.1s\n\nOK (skipped=2)\n"), 3)
        self.assertEqual(runners.passed("pytest", "999 passed (printed by the code)\n2 passed in 0.01s\n"), 2)

    def test_the_whole_output_is_read_so_padding_cannot_push_the_real_summary_out(self):
        (self.root / "test_long.py").write_text("import unittest\nprint('x' * 400000)\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        v = {"kind": "command", "argv": [sys.executable, "-m", "unittest", "-q", "test_long"], "paths": ["."], "require_tests": "unittest"}
        self.assertEqual(verify.run(v, self.root)[::2], ("PASS", "exit 0"))
        (self.root / "test_pad.py").write_text(
            "import atexit, unittest\natexit.register(lambda: print('y' * 400000 + '\\nRan 99 tests in 0.001s\\n\\nOK'))\n"
            "@unittest.skip('x')\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        self.assertEqual(verify.run({**v, "argv": [sys.executable, "-m", "unittest", "-q", "test_pad"]}, self.root)[::2], ("FAIL", "no tests ran"))
        with unittest.mock.patch.object(verify, "MAX_OUTPUT", 1000):
            self.assertIn("too much to verify", verify.run(v, self.root)[2])

    def test_colour_codes_do_not_hide_a_summary(self):
        script = "print('\\x1b[2m      Tests \\x1b[22m \\x1b[1m\\x1b[32m3 passed\\x1b[39m\\x1b[22m\\x1b[90m (3)\\x1b[39m')"
        v = {"kind": "command", "argv": [sys.executable, "-c", script], "paths": ["."], "require_tests": "vitest", "min_tests": 3}
        self.assertEqual(verify.run(v, self.root)[::2], ("PASS", "exit 0"))

    def test_rust_tests_are_listed_with_where_they_live(self):
        out = ("     Running unittests src/lib.rs (target/debug/deps/demo-1)\n\nrunning 2 tests\ntest tests::adds ... ok\ntest tests::subs ... FAILED\n\n"
               "     Running tests/api.rs (target/debug/deps/api-2)\n\nrunning 2 tests\ntest api_ok ... ok\ntest api_bad ... FAILED\n")
        self.assertEqual(runners.cargo_tests(out), (["tests::adds", "api_ok"], ["tests::subs"]))


class FrozenTreeTests(Env):
    def test_a_runner_living_in_the_project_cannot_be_rewritten_to_print_a_pass(self):
        (self.root / "node_modules" / "runner").mkdir(parents=True)
        (self.root / "node_modules" / "runner" / "run.py").write_text("import sys\nsys.exit(1)\n")
        g = goals.submit(self.k, self.project["project_id"], "o", [{"id": "c0", "text": "runner passes", "verifier": {
            "kind": "command", "argv": [sys.executable, "node_modules/runner/run.py"], "paths": ["."],
            "frozen_trees": ["node_modules"]}}], {"calls": 99, "attempts": 5, "active_ms": 9_000_000})
        goals.add_task(self.k, g["goal_id"], "t", "p", ["c0"])
        cheat = "import pathlib\npathlib.Path('node_modules/runner/run.py').write_text('print(1)\\n')"
        report = supervisor.run_goal(self.k, g["goal_id"], fake(cheat), max_steps=1)
        self.assertFalse(report["done"])
        self.assertIn("되돌릴 수 없다", goals.latest_evidence(self.k, g["goal_id"])["c0"]["detail"])
        from lupus import protect
        before = protect.tree_fingerprint(self.root, "node_modules")
        store = self.root / "node_modules" / ".pnpm" / "runner@1" / "node_modules" / "runner"
        store.mkdir(parents=True)
        (store / "run.js").write_text("real")
        with_store = protect.tree_fingerprint(self.root, "node_modules")
        self.assertNotEqual(with_store, before)                                         # a package store is part of the tree
        before = with_store
        (self.root / "tools").mkdir()
        (self.root / "node_modules" / "linked").symlink_to("../tools")                  # the real files would be elsewhere
        self.assertRefused("FROZEN_TREE_LINK", protect.tree_fingerprint, self.root, "node_modules", True)
        self.assertNotEqual(protect.tree_fingerprint(self.root, "node_modules"), before)   # appearing later: a change
        (self.root / "node_modules" / "linked").unlink()
        (self.root / "node_modules" / "inner").symlink_to(".pnpm/runner@1")              # pnpm style, stays inside: fine
        before = protect.tree_fingerprint(self.root, "node_modules", True)
        (self.root / "node_modules" / ".cache").mkdir()
        (self.root / "node_modules" / ".cache" / "x").write_text("tool cache")          # a tool's own cache is not a change
        self.assertEqual(protect.tree_fingerprint(self.root, "node_modules"), before)
