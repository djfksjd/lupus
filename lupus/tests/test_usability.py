"""Flows a person runs into: every wait has a command that resolves it, refusals leave nothing
behind and say what to do next, and bad input is an error message, not a traceback."""

import contextlib
import io
import json
import os
import shutil
import sys
import time
import unittest
from unittest import mock

from lupus import author, cli, goals, jobs, projects, quick, recovery, runners, service, supervisor
from lupus.adapters import FakeAdapter

from .helpers import CAPS, Env, contains, fake
from .test_author import GOOD, HONEST, RUBRIC, WRITE


def run_cli(env: Env, *argv: str, cwd=None) -> tuple[int, dict | list | None, dict | None]:
    """Run a lupus command as the user (terminal confirmation answered yes). Returns
    (exit code, what it printed as JSON, the error it printed)."""
    env.k.close()
    out, err = io.StringIO(), io.StringIO()
    here = os.getcwd()
    try:
        os.chdir(cwd or env.root)
        with mock.patch.object(cli, "_confirm_user", lambda what: None), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", str(env.home), *argv])
    finally:
        os.chdir(here)
        env.reopen()
    def last_json(text):
        start = text.find("{") if "{" in text else text.find("[")
        try:
            return json.loads(text[start:]) if start >= 0 else None
        except ValueError:
            return None
    return code, last_json(out.getvalue()), last_json(err.getvalue())


class ResolveTests(Env):
    def test_what_the_user_says_when_resolving_reaches_the_next_attempt(self):
        g = self.goal()
        task_id = self.task_ids(g["goal_id"])[0]
        supervisor.run_goal(self.k, g["goal_id"], fake("print('no')"))
        self.assertEqual(goals.get_task(self.k, task_id)["status"], "NO_PROGRESS")
        goals.resolve_wait(self.k, task_id, "user", "a.txt 에 대문자 A 한 글자만 써라")
        prompt = supervisor.build_prompt(self.k, goals.get_task(self.k, task_id), root=self.root)
        self.assertIn("a.txt 에 대문자 A 한 글자만 써라", prompt)
        # nothing on disk changed, yet this is not a repeat: the user authorised a new approach
        report = supervisor.run_goal(self.k, g["goal_id"], fake())
        self.assertTrue(report["done"], report["steps"])
        self.assertEqual(goals.get_task(self.k, task_id)["attempt_count"], 3)        # cumulative, never reset
        self.assertEqual(goals.resolutions(self.k, task_id), ["a.txt 에 대문자 A 한 글자만 써라"])

    def test_an_explicit_run_retries_a_branch_that_waited_on_a_provider(self):
        g = self.goal()
        blocked = FakeAdapter("raise SystemExit(1)", error_class="quota")
        blocked.driver = "fake"
        supervisor.run_goal(self.k, g["goal_id"], blocked)
        self.assertEqual(goals.get(self.k, g["goal_id"])["status"], "EXTERNAL_BLOCKED")
        report = supervisor.run_goal(self.k, g["goal_id"], fake(driver="fake_alt"))      # the other AI, as the README says
        self.assertTrue(report["done"], report["steps"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM handoff WHERE status = 'ACCEPTED'")[0], 1)

    def test_refreeze_resumes_the_branch_it_was_recommended_for(self):
        (self.root / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('x')\n")
        made = quick.fix_tests(self.k, self.project, "user")
        supervisor.run_goal(self.k, made["goal_id"], fake("print(1)"), max_steps=1)
        (self.root / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")   # the user's own edit
        report = supervisor.run_goal(self.k, made["goal_id"], fake("print(1)"), max_steps=1)
        self.assertEqual(report["steps"][0]["reason"], "PROTECTED_CHANGED_OUTSIDE")
        code, out, _ = run_cli(self, "refreeze", made["goal_id"])
        self.assertEqual((code, len(out["tasks_resumed"])), (0, 1))
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake("print(1)"))["done"])

    def test_after_a_revocation_there_is_a_command_to_continue(self):
        g = self.goal({"a.txt": "A", "b.txt": "B"})
        supervisor.run_goal(self.k, g["goal_id"], fake(), max_steps=1)
        code, out, _ = run_cli(self, "revoke", self.project["project_id"], "--reason", "test")
        self.assertIn("lupus revalidate", out["next"])
        self.assertIn("REVOCATION_EPOCH_CHANGED", recovery.readiness(self.k, g["goal_id"])["blockers"])
        code, out, _ = run_cli(self, "revalidate", g["goal_id"], "--note", "scope checked")
        self.assertEqual(code, 0)
        self.assertNotIn("REVOCATION_EPOCH_CHANGED", recovery.readiness(self.k, g["goal_id"])["blockers"])


class DocumentDeadEndTests(Env):
    def setUp(self):
        super().setUp()
        adapter = FakeAdapter(HONEST)
        adapter.driver = "fake_alt"
        service.OVERRIDE["fake_alt"] = adapter
        self.addCleanup(service.OVERRIDE.clear)

    def test_a_document_waiting_for_sign_off_can_be_approved_later(self):
        made = author.submit(self.k, self.project, "계획", "plan.md", RUBRIC, "fake_alt", "user")
        code, out, _ = run_cli(self, "approve", made["goal_id"])
        self.assertEqual(code, 1)
        self.assertIn("lupus run", out["next"])                       # not written yet: says so
        supervisor.run_goal(self.k, made["goal_id"], fake(WRITE))
        code, out, _ = run_cli(self, "approve", made["goal_id"])
        self.assertEqual((code, out["done"]), (0, True))
        self.assertEqual(goals.get(self.k, made["goal_id"])["status"], "DONE")
        other = self.goal()
        code, _, err = run_cli(self, "approve", other["goal_id"])
        self.assertEqual(err["error"], "NOT_A_DOCUMENT_GOAL")

    def test_write_resolves_the_output_where_the_user_stands_and_shows_it(self):
        (self.root / "docs").mkdir()
        seen = []
        self.k.close()
        here = os.getcwd()
        try:
            os.chdir(self.root / "docs")
            with mock.patch.object(cli, "_confirm_user", seen.append), mock.patch.object(cli, "_run", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["--home", str(self.home), "write", "계획", "--out", "plan.md", "--driver", "claude", "--judge", "codex",
                                 "--no-draft", "--must", "목표가 있다"])
        finally:
            os.chdir(here)
            self.reopen()
        self.assertEqual(code, 130)
        self.assertIn(str(self.root / "docs" / "plan.md"), seen[0])
        row = self.k.one("SELECT goal_id FROM goal ORDER BY created_at DESC LIMIT 1")
        self.assertEqual(goals.criteria(self.k, row["goal_id"])[0]["verifier"]["path"], os.path.join("docs", "plan.md"))
        code, _, err = run_cli(self, "write", "x", "--out", "../../elsewhere.md", "--driver", "claude", "--no-draft", "--must", "y")
        self.assertEqual(err["error"], "PATH_ESCAPES_PROJECT")


class InputTests(Env):
    def test_a_bad_goal_file_is_an_error_message_naming_the_problem(self):
        path = self.tmp / "goal.json"
        cases = {
            "{}": "objective", "not json": "goal.json", '{"objective": "o", "budget": {"calls": "many"}, "criteria": []}': "budget",
            '{"objective": "o", "budget": {"calls": 1, "attempts": 1, "active_ms": 1}, "criteria": [], "tasks": [{"title": "t", "prompt": "p", "criteria": ["c0"], "depends_on": [3]}]}': "depends_on",
            '{"objective": "o", "budget": {"calls": 1, "attempts": 1, "active_ms": 1}, "criteria": [{"id": "c0", "text": "t", "verifier": {"kind": "file_contains"}}]}': "file_contains",
            '{"objective": "o", "budget": {"calls": 1, "attempts": 1, "active_ms": 1}, "criteria": [{"id": "c0", "text": "t", "verifier": {"kind": "command", "argv": ["true"], "paths": ["."], "timeout_s": "forever"}}]}': "timeout_s",
        }
        for text, expect in cases.items():
            path.write_text(text)
            code, _, err = run_cli(self, "goal-submit", self.project["project_id"], str(path))
            self.assertEqual(code, 2, text)
            self.assertIn(expect, err["detail"], text)
        code, _, err = run_cli(self, "goal-submit", self.project["project_id"], str(self.tmp / "missing.json"))
        self.assertEqual(err["error"], "GOAL_FILE_INVALID")
        path.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(run_cli(self, "goal-submit", self.project["project_id"], str(path))[2]["error"], "GOAL_FILE_INVALID")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM goal")[0], 0)

    def test_a_program_error_is_reported_without_a_traceback_and_refusals_say_what_next(self):
        with mock.patch.object(cli.alpha, "portfolio", side_effect=KeyError("boom")):
            code, _, err = run_cli(self, "alpha-status")
        self.assertEqual((code, err["error"]), (3, "INTERNAL_ERROR"))
        self.assertIn("lupus recover", err["next"])
        (self.root / "empty").mkdir()
        code, _, err = run_cli(self, "fix-tests", "--driver", "claude")
        self.assertEqual(err["error"], "PROVIDER_NOT_APPROVED")
        self.assertIn("project-add", err["next"])


class RegistrationTests(Env):
    def test_a_folder_registered_by_a_command_that_then_refuses_is_not_left_registered(self):
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        code, _, err = run_cli(self, "fix-tests", "--driver", "claude", cwd=repo / "src")       # no tests anywhere
        self.assertEqual(err["error"], "NO_TESTS_FOUND")
        self.assertIn("--check", err["next"])
        self.assertIsNone(projects.resolve(self.k, repo))                 # nothing registered, the root is still free
        self.assertEqual(cli._repo_root(str(repo / "src")), str(repo))    # and it would have been the repository, not src/

    def test_a_mistaken_registration_can_be_removed_but_history_cannot(self):
        (self.tmp / "other-proj").mkdir()
        other = projects.register(self.k, self.tmp / "other-proj", "o", ["local"])
        self.assertRefused("USER_AUTHORITY_REQUIRED", projects.remove, self.k, other["project_id"], "supervisor")
        projects.remove(self.k, other["project_id"], "user")
        self.assertRefused("PROJECT_NOT_FOUND", projects.get, self.k, other["project_id"])
        self.goal()
        self.assertRefused("PROJECT_IN_USE", projects.remove, self.k, self.project["project_id"], "user")


class RunnerChoiceTests(Env):
    def test_plain_pytest_functions_are_not_handed_to_unittest(self):
        (self.root / "test_feature.py").write_text("def test_feature():\n    assert False\n")
        try:
            found = runners.detect(self.root)
        except Exception as exc:
            self.assertEqual(getattr(exc, "code", ""), "TEST_RUNNER_UNAVAILABLE")      # pytest not installed here
            return
        self.assertEqual(found["runner"], "pytest")

    def test_a_test_scripts_own_options_are_kept_and_a_shell_pipeline_is_not_guessed_at(self):
        self.assertEqual(runners._node_argv("node", "jest --config config/custom.json")[2],
                         ["node", "node_modules/jest/bin/jest.js", "--ci", "--config", "config/custom.json"])
        self.assertEqual(runners._node_argv("node", "vitest --dir packages/a")[2],
                         ["node", "node_modules/vitest/vitest.mjs", "run", "--dir", "packages/a"])
        self.assertEqual(runners._node_argv("node", "node --test --experimental-strip-types test/")[2],
                         ["node", "--test", "--test-reporter=tap", "--experimental-strip-types", "test/"])
        self.assertEqual(runners._node_argv("node", "")[0], "node")
        for script in ("tsc && jest", "NODE_ENV=test jest", "mocha", "jest $(cat list)"):
            self.assertRefused("TEST_RUNNER_UNKNOWN", runners._node_argv, "node", script)

    def test_container_mode_names_programs_and_keeps_the_projects_own_environment(self):
        (self.root / "src" / "pkg").mkdir(parents=True)
        (self.root / "src" / "pkg" / "__init__.py").write_text("x = 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_p.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        found = runners.detect(self.root, host=False)
        self.assertEqual((found["argv"][0], found["env"]), ("python3", {"PYTHONPATH": "src"}))
        with mock.patch.object(runners.shutil, "which", return_value=None):      # nothing installed on this machine
            (self.root / "package.json").write_text("{}")
            (self.root / "a.test.js").write_text("")
            shutil.rmtree(self.root / "tests")
            self.assertEqual(runners.detect(self.root, host=False)["argv"][0], "node")
            self.assertRefused("TEST_RUNNER_UNAVAILABLE", runners.detect, self.root)

    def test_build_output_does_not_count_towards_what_is_watched(self):
        from lupus import verify
        (self.root / "target" / "debug").mkdir(parents=True)
        (self.root / "target" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")   # as cargo writes it
        (self.root / "dist").mkdir()
        (self.root / "dist" / "app.js").write_text("v1")
        for i in range(60):
            (self.root / "target" / "debug" / f"f{i}").write_text("x")
        before = verify.artifact_hash({"paths": ["."]}, self.root)
        (self.root / "target" / "debug" / "f0").write_text("changed")
        self.assertEqual(verify.artifact_hash({"paths": ["."]}, self.root), before)
        (self.root / "dist" / "app.js").write_text("v2")                  # an output folder may be the product: still watched
        self.assertNotEqual(verify.artifact_hash({"paths": ["."]}, self.root), before)

    def test_files_a_test_scripts_options_point_at_are_frozen(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        (self.root / "config").mkdir()
        (self.root / "config" / "custom.json").write_text("{}")
        (self.root / "node_modules" / "jest" / "bin").mkdir(parents=True)
        (self.root / "node_modules" / "jest" / "bin" / "jest.js").write_text("")
        (self.root / "package.json").write_text('{"scripts": {"test": "jest --config config/custom.json"}}')
        (self.root / "a.test.js").write_text("")
        found = runners.detect(self.root)
        self.assertIn("config/custom.json", found["protect"])
        self.assertEqual(found["argv"][-2:], ["--config", "config/custom.json"])


class PruneTests(Env):
    def test_old_leftovers_go_and_anything_live_stays(self):
        g = self.goal()
        live = self.k.runtime / "displaced" / g["goal_id"]
        old = self.k.runtime / "displaced" / "goal_finished_long_ago"
        for folder in (live, old):
            folder.mkdir(parents=True)
            (folder / "f").write_text("x")
            os.utime(folder, (time.time() - 90 * 86400,) * 2)
        log = self.tmp / "old.log"
        log.write_text("x")
        self.k.run("INSERT INTO job(job_id, command, log_path, status, started_at, ended_at) VALUES ('job_old', '[]', ?, 'EXITED', 1, 1)", str(log))
        removed = jobs.prune(self.k, 30)
        self.assertEqual((removed["displaced"], removed["jobs"]), (1, 1))
        self.assertTrue(live.exists() and not old.exists() and not log.exists())
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM goal")[0], 1)


class SummaryOutputTests(unittest.TestCase):
    def test_a_person_gets_a_few_lines_that_say_what_happened_and_what_next(self):
        done = cli._summary({"goal_id": "goal_x", "done": True, "goal_status": "DONE", "check": "tests/test_lupus_ab.py",
                             "steps": [{"status": "DONE", "outcome": "ALREADY_SATISFIED", "verdicts": {"c0": "PASS", "c1": "PASS"}}],
                             "budget": {"attempts": {"used": 0, "cap": 3}, "active_ms": {"used": 5377, "cap": 9}},
                             "usage": {"totals": {"tokens_in": 4358, "tokens_out": 180, "tokens_cached": 8169}}})
        self.assertIn("✔ 완료", done)
        self.assertIn("c0=PASS c1=PASS", done)
        self.assertIn("토큰 12,707", done)
        self.assertLess(len(done.splitlines()), 8)
        self.assertIn("goal_x", done)
        stuck = cli._summary({"goal_id": "goal_x", "done": False, "goal_status": "NO_PROGRESS",
                              "steps": [{"status": "NO_PROGRESS", "outcome": "NO_PROGRESS", "verdicts": {"c0": "FAIL"}}],
                              "waiting": [{"status": "NO_PROGRESS", "reason": "no valid progress in 2 consecutive attempts"}],
                              "completion_blockers": ["EVIDENCE_FAIL:c0"], "budget": {}, "usage": {"totals": {}}})
        self.assertIn("✘ 미완료", stuck)
        self.assertIn("lupus status goal_x", stuck)
        self.assertIn("EVIDENCE_FAIL:c0", stuck)
