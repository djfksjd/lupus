"""Git: nothing a worker leaves in .git gets to run, and isolated work never touches the user's
working tree until they accept it."""

import os
import shutil
import subprocess
import unittest

from lupus import gitx, goals, projects, quick, supervisor
from lupus.kernel import Kernel
from lupus.util import LupusError

from .helpers import Env, fake

GIT = shutil.which("git")
TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 2), 4)\n"
FIX = "import pathlib\npathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')\n"


def sh(root, *args):
    return subprocess.run([GIT, "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@unittest.skipUnless(GIT, "git is not installed")
class GitEnv(Env):
    def setUp(self):
        super().setUp()
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "user.name", "Test")
        sh(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.root / "test_calc.py").write_text(TEST)
        (self.root / "notes.txt").write_text("committed\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "start")
        self.addCleanup(shutil.rmtree, str(self.home) + "-worktrees", True)


class GuardTests(GitEnv):
    def test_a_hook_or_config_a_worker_plants_in_dot_git_is_removed_before_anything_runs_it(self):
        made = quick.fix_tests(self.k, self.project, "user")
        before = (self.root / ".git" / "config").read_bytes()
        plant = FIX + (
            "h = pathlib.Path('.git/hooks/pre-commit'); h.write_text('#!/bin/sh\\ntouch PWNED\\n'); h.chmod(0o755)\n"
            "c = pathlib.Path('.git/config'); c.write_text(c.read_text() + '[core]\\n\\tfsmonitor = touch PWNED-BY-STATUS\\n')\n")
        report = supervisor.run_goal(self.k, made["goal_id"], fake(plant))
        self.assertTrue(report["done"])
        self.assertFalse((self.root / ".git" / "hooks" / "pre-commit").exists())
        self.assertEqual((self.root / ".git" / "config").read_bytes(), before)
        sh(self.root, "status")
        sh(self.root, "commit", "-q", "-am", "after")
        self.assertFalse(any(p.name.startswith("PWNED") for p in self.root.iterdir()))
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM event WHERE type = 'git.meta_restored'")[0], 1)

    def test_lupus_own_git_calls_do_not_run_hooks_either(self):
        hook = self.root / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/HOOK-RAN\"\n")
        hook.chmod(0o755)
        isolated = gitx.isolate(self.k, self.project, "user")          # `git worktree add` would trigger post-checkout
        self.assertFalse((self.root / "HOOK-RAN").exists())
        self.assertFalse(os.path.exists(os.path.join(isolated["canonical_root"], "HOOK-RAN")))


class IsolationTests(GitEnv):
    def dirty(self):
        (self.root / "notes.txt").write_text("my uncommitted edit\n")          # unstaged
        (self.root / "scratch.txt").write_text("untracked\n")
        (self.root / "staged.txt").write_text("staged\n")
        sh(self.root, "add", "staged.txt")
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, sh(self.root, "status", "--porcelain"), sh(self.root, "rev-parse", "HEAD")

    def start(self):
        isolated = gitx.isolate(self.k, self.project, "user")
        made = quick.fix_tests(self.k, isolated, "user")
        return isolated, made

    def test_success_touches_nothing_of_the_users_until_accepted_and_then_adds_one_commit(self):
        files, status, head = self.dirty()
        isolated, made = self.start()
        self.assertTrue(isolated["origin_has_uncommitted_changes"])
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        self.assertEqual(({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, sh(self.root, "status", "--porcelain"),
                          sh(self.root, "rev-parse", "HEAD")), (files, status, head))
        patch = gitx.diff(self.k, made["goal_id"])
        self.assertIn("+    return a + b", patch)
        self.assertNotIn("my uncommitted edit", patch)
        self.assertRefused("USER_AUTHORITY_REQUIRED", gitx.accept, self.k, made["goal_id"], "supervisor")
        result = gitx.accept(self.k, made["goal_id"], "user")
        self.assertEqual(result["files"], ["calc.py"])
        self.assertEqual(sh(self.root, "rev-list", "--count", "HEAD").strip(), "2")
        self.assertIn("return a + b", (self.root / "calc.py").read_text())
        self.assertEqual((self.root / "notes.txt").read_text(), "my uncommitted edit\n")      # the user's edits survived
        self.assertIn("staged.txt", sh(self.root, "diff", "--cached", "--name-only"))
        self.assertIn("Verified by:", sh(self.root, "log", "-1", "--format=%B"))
        self.assertFalse(os.path.exists(isolated["canonical_root"]))
        self.assertEqual(sh(self.root, "branch", "--list", "lupus/*").strip(), "")
        self.assertRefused("ISOLATION_CLOSED", gitx.accept, self.k, made["goal_id"], "user")

    def test_failure_and_discard_leave_the_users_tree_byte_for_byte(self):
        files, status, head = self.dirty()
        isolated, made = self.start()
        report = supervisor.run_goal(self.k, made["goal_id"], fake("print('no fix')"))
        self.assertFalse(report["done"])
        self.assertRefused("GOAL_NOT_COMPLETE", gitx.accept, self.k, made["goal_id"], "user")      # only a verified result
        self.assertEqual(gitx.discard(self.k, made["goal_id"], "user")["discarded"], True)
        self.assertEqual(({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, sh(self.root, "status", "--porcelain"),
                          sh(self.root, "rev-parse", "HEAD")), (files, status, head))
        self.assertFalse(os.path.exists(isolated["canonical_root"]))
        self.assertEqual(sh(self.root, "branch", "--list", "lupus/*").strip(), "")
        self.assertEqual(goals.get(self.k, made["goal_id"])["status"], "CANCELLED")

    def test_accept_refuses_a_conflicting_uncommitted_file_and_a_branch_that_moved_in_the_same_place(self):
        isolated, made = self.start()
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b  # my own work in progress\n")
        self.assertRefused("ACCEPT_REFUSED_BY_GIT", gitx.accept, self.k, made["goal_id"], "user")
        self.assertIn("my own work in progress", (self.root / "calc.py").read_text())             # not overwritten
        sh(self.root, "checkout", "-q", "calc.py")
        (self.root / "other.txt").write_text("x\n")
        sh(self.root, "add", "other.txt")
        (self.root / "calc.py").write_text("def add(a, b):\n    return b + a\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "the user moved on, in the same place")
        head = sh(self.root, "rev-parse", "HEAD")
        with self.assertRaises(LupusError) as refused:
            gitx.accept(self.k, made["goal_id"], "user")
        self.assertEqual(refused.exception.code, "BASELINE_CONFLICT")
        self.assertIn("calc.py", refused.exception.detail)
        self.assertEqual((sh(self.root, "rev-parse", "HEAD"), sh(self.root, "status", "--porcelain")), (head, ""))      # nothing half-merged
        self.assertIn("return b + a", (self.root / "calc.py").read_text())
        gitx.discard(self.k, made["goal_id"], "user")

    def test_two_results_of_one_repository_are_accepted_one_after_the_other(self):
        # two goals worked on side by side, each in a checkout of the same commit
        (self.root / "text.py").write_text("def shout(s):\n    return s.lower()\n")
        (self.root / "test_text.py").write_text("import unittest\nfrom text import shout\nclass T(unittest.TestCase):\n"
                                                 "    def test_shout(self): self.assertEqual(shout('a'), 'A')\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "two things are broken")
        (_, one), (_, two) = self.start(), self.start()
        both = "import pathlib\npathlib.Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n')\n"
        self.assertTrue(supervisor.run_goal(self.k, one["goal_id"], fake(both + "pathlib.Path('text.py').write_text('def shout(s):\\n    return s.upper()\\n')\n"))["done"])
        self.assertTrue(supervisor.run_goal(self.k, two["goal_id"], fake(both + "pathlib.Path('text.py').write_text('def shout(s):\\n    return s.upper()\\n')\npathlib.Path('extra.py').write_text('X = 1\\n')\n"))["done"])
        first = gitx.accept(self.k, one["goal_id"], "user")
        self.assertNotIn("combined_with", first)
        second = gitx.accept(self.k, two["goal_id"], "user")           # the branch has moved: combined, checked again, accepted
        self.assertEqual(second["combined_with"], first["commit"])
        self.assertEqual(second["files"], ["extra.py"])                # the identical edits merged to nothing new
        self.assertEqual(sh(self.root, "rev-list", "--count", "HEAD").strip(), "4")
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")
        self.assertEqual(sh(self.root, "branch", "--list", "lupus/*").strip(), "")

    def test_a_result_the_branch_already_contains_adds_no_empty_commit(self):
        (_, one), (isolated, two) = self.start(), self.start()
        for made in (one, two):                      # both make exactly the same edit
            self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        first = gitx.accept(self.k, one["goal_id"], "user")
        second = gitx.accept(self.k, two["goal_id"], "user")
        self.assertEqual((second["commit"], second["files"]), (first["commit"], []))
        self.assertEqual(sh(self.root, "rev-list", "--count", "HEAD").strip(), "2")
        self.assertFalse(os.path.exists(isolated["canonical_root"]))
        self.assertRefused("ISOLATION_CLOSED", gitx.accept, self.k, two["goal_id"], "user")

    def test_a_branch_that_contains_the_result_but_no_longer_passes_is_not_called_accepted(self):
        isolated, made = self.start()
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        # the user makes the same edit themselves, and also commits a test that it does not pass
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.root / "test_calc.py").write_text(TEST + "    def test_more(self): self.assertEqual(add(1, 1), 3)\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "same fix, stricter test")
        self.assertRefused("ACCEPT_UNVERIFIED", gitx.accept, self.k, made["goal_id"], "user")
        self.assertTrue(os.path.exists(isolated["canonical_root"]))

    def test_a_checkouts_fingerprint_sees_the_repositorys_refs_and_a_branch_that_moves_mid_check_is_refused(self):
        from lupus import review
        isolated, made = self.start()
        root = __import__("pathlib").Path(isolated["canonical_root"])
        before = review.fingerprint(root)
        sh(self.root, "tag", "v1")                                      # shared metadata: no file of the checkout changed
        self.assertNotEqual(review.fingerprint(root), before)
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "the same fix by hand")
        real = gitx._verify_commit

        def moves_meanwhile(*args):
            real(*args)
            (self.root / "later.txt").write_text("x\n")
            sh(self.root, "add", "-A")
            sh(self.root, "commit", "-q", "-m", "and one more")
        gitx._verify_commit = moves_meanwhile
        self.addCleanup(setattr, gitx, "_verify_commit", real)
        self.assertRefused("BASELINE_CHANGED", gitx.accept, self.k, made["goal_id"], "user")
        self.assertTrue(os.path.exists(isolated["canonical_root"]))    # the result is still there
        gitx._verify_commit = real
        self.assertTrue(gitx.accept(self.k, made["goal_id"], "user")["accepted"])      # asked again, on the branch as it now is

    def test_a_combination_that_no_longer_passes_is_not_accepted(self):
        isolated, made = self.start()
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(FIX))["done"])
        # meanwhile the user commits something that merges cleanly but breaks the same tests
        (self.root / "test_calc.py").write_text(TEST + "    def test_more(self): self.assertEqual(add(1, 1), 3)\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "a stricter test")
        head = sh(self.root, "rev-parse", "HEAD")
        self.assertRefused("ACCEPT_UNVERIFIED", gitx.accept, self.k, made["goal_id"], "user")
        self.assertEqual(sh(self.root, "rev-parse", "HEAD"), head)
        self.assertNotIn("return a + b", (self.root / "calc.py").read_text())
        self.assertTrue(os.path.exists(isolated["canonical_root"]))    # the result is still there to look at

    def test_a_crash_mid_run_still_leaves_the_origin_alone_and_the_checkout_recoverable(self):
        from .helpers import run_script
        files, status, head = self.dirty()
        isolated, made = self.start()
        self.k.close()
        body = f"a = adapters.FakeAdapter({FIX!r})\nsupervisor.run_goal(k, {made['goal_id']!r}, a)\n"
        self.assertEqual(run_script(self.home, body, crash_at="supervisor.after_worker").returncode, -9)
        self.reopen()
        self.assertEqual(({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, sh(self.root, "status", "--porcelain"),
                          sh(self.root, "rev-parse", "HEAD")), (files, status, head))
        supervisor.recover(self.k)
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake("print('already on disk')"))["done"])
        self.assertTrue(gitx.accept(self.k, made["goal_id"], "user")["accepted"])

    def test_only_a_repository_top_folder_can_be_isolated_and_plain_goals_have_no_accept(self):
        plain = self.goal()
        self.assertRefused("NOT_ISOLATED", gitx.accept, self.k, plain["goal_id"], "user")
        self.assertIn("격리되지 않은", gitx.diff(self.k, plain["goal_id"]))
        other = self.tmp / "nogit"
        other.mkdir()
        self.assertRefused("ISOLATION_UNSUPPORTED", gitx.isolate, self.k, projects.register(self.k, other, "n", ["local"]), "user")
        self.assertRefused("USER_AUTHORITY_REQUIRED", gitx.isolate, self.k, self.project, "worker")


class GuardEdgeTests(GitEnv):
    def worker(self, script):
        made = quick.fix_tests(self.k, self.project, "user")
        return made, supervisor.run_goal(self.k, made["goal_id"], fake(FIX + script))

    def test_an_included_config_file_and_a_new_environment_are_covered_too(self):
        (self.root / ".git" / "extra-config").write_text("[user]\n\tnickname = x\n")
        sh(self.root, "config", "--local", "include.path", "extra-config")
        made, report = self.worker(
            "pathlib.Path('.git/extra-config').write_text('[core]\\n\\tfsmonitor = touch PWNED\\n')\n"
            "pathlib.Path('.venv/bin').mkdir(parents=True); pathlib.Path('.venv/pyvenv.cfg').write_text('home = /tmp\\n')\n"
            "p = pathlib.Path('.venv/bin/python'); p.write_text('#!/bin/sh\\necho Ran 9 tests; echo OK\\n'); p.chmod(0o755)\n")
        self.assertTrue(report["done"])
        self.assertEqual((self.root / ".git" / "extra-config").read_text(), "[user]\n\tnickname = x\n")
        self.assertFalse((self.root / ".venv").exists())               # not the project's interpreter next time
        sh(self.root, "status")
        self.assertFalse((self.root / "PWNED").exists())

    def test_a_link_put_in_the_way_is_removed_not_followed_and_checks_are_guarded_too(self):
        outside = self.tmp / "other-repo-info"
        outside.mkdir()
        (outside / "exclude").write_text("theirs\n")
        made, report = self.worker(
            "import shutil, os\nshutil.rmtree('.git/info')\n" f"os.symlink({str(outside)!r}, '.git/info')\n")
        self.assertEqual((outside / "exclude").read_text(), "theirs\n")          # the other repository was not written to
        self.assertTrue((self.root / ".git" / "info").is_dir() and not (self.root / ".git" / "info").is_symlink())
        # a check (not a worker) that leaves a hook behind: also undone
        from lupus import goals as g
        goal = g.get(self.k, made["goal_id"])
        crit = [{"id": "x", "text": "t", "verifier": {"kind": "command", "paths": ["."], "argv": [
            "/bin/sh", "-c", "printf '#!/bin/sh\\n' > .git/hooks/pre-push; chmod +x .git/hooks/pre-push"]}}]
        supervisor._timed_verify(self.k, goal, crit, self.root)
        self.assertFalse((self.root / ".git" / "hooks" / "pre-push").exists())

    def test_only_what_was_created_is_removed_and_only_a_real_environment_is_set_aside(self):
        (self.root / ".git" / "backup-config").write_text("[user]\n\tnote = mine\n")           # exists, unused
        (self.root / "settings.json").write_text("{}")
        made, report = self.worker(
            "c = pathlib.Path('.git/config'); c.write_text(c.read_text() + '[include]\\n\\tpath = backup-config\\n')\n"
            "pathlib.Path('env').mkdir(); pathlib.Path('settings.json').rename('env/settings.json')\n")
        self.assertTrue((self.root / ".git" / "backup-config").exists())                          # not ours to delete
        self.assertNotIn("backup-config", (self.root / ".git" / "config").read_text())            # the include is gone
        self.assertTrue((self.root / "env" / "settings.json").exists())                           # a folder called env is not a virtualenv

    def test_workers_and_checks_cannot_write_dot_git_in_the_first_place(self):
        from lupus import adapters
        from lupus.util import sandbox_profile, sandbox_profile_worker
        git_dir = str(self.root / ".git")
        for profile in (sandbox_profile(self.root), sandbox_profile_worker(self.root, "claude")):
            last = [line for line in profile.splitlines() if "deny file-write*" in line and git_dir in line]
            self.assertTrue(last and profile.index(last[-1]) > profile.index("(allow file-write*"))
        self.assertEqual(adapters.codex_filesystem_profile()[":project_roots"], {".": "write", ".git": "read"})

    def test_metadata_that_could_not_be_put_back_faithfully_is_refused_up_front(self):
        hooks = self.root / ".git" / "hooks"
        shutil.rmtree(hooks)
        real = self.tmp / "shared-hooks"
        real.mkdir()
        hooks.symlink_to(real)
        self.assertRefused("GIT_META_UNSUPPORTED", gitx.guard_snapshot, self.root)
        self.assertTrue(hooks.is_symlink())                                       # the user's own setup is left as it is


class AcceptBindingTests(GitEnv):
    def done(self, script=FIX):
        isolated = gitx.isolate(self.k, self.project, "user")
        made = quick.fix_tests(self.k, isolated, "user")
        self.assertTrue(supervisor.run_goal(self.k, made["goal_id"], fake(script))["done"])
        return isolated, made

    def test_what_is_accepted_is_checked_as_the_exact_commit(self):
        isolated, made = self.done()
        calc = os.path.join(isolated["canonical_root"], "calc.py")
        with open(calc, "w") as f:
            f.write("def add(a, b):\n    return 0  # edited after verification\n")
        self.assertRefused("ACCEPT_UNVERIFIED", gitx.accept, self.k, made["goal_id"], "user")      # the commit does not pass
        self.assertNotIn("return 0", (self.root / "calc.py").read_text())
        with open(calc, "w") as f:
            f.write("def add(a, b):\n    return b + a  # a later edit that still passes\n")
        with open(os.path.join(isolated["canonical_root"], "test_calc.py"), "a") as f:
            f.write("# the frozen test, touched after verification\n")
        self.assertRefused("RESULT_CHANGED", gitx.accept, self.k, made["goal_id"], "user")         # the judge itself was changed
        with open(os.path.join(isolated["canonical_root"], "test_calc.py"), "w") as f:
            f.write(TEST)
        self.assertTrue(gitx.accept(self.k, made["goal_id"], "user")["accepted"])
        self.assertIn("return b + a", (self.root / "calc.py").read_text())

    def test_a_deliverable_git_ignores_is_not_silently_dropped(self):
        (self.root / ".gitignore").write_text("report.md\n")
        sh(self.root, "add", ".gitignore")
        sh(self.root, "commit", "-q", "-m", "ignore report")
        isolated = gitx.isolate(self.k, self.project, "user")
        g = goals.submit(self.k, isolated["project_id"], "report", [
            {"id": "c0", "text": "report.md says done", "verifier": {"kind": "file_contains", "path": "report.md", "text": "done"}}],
            {"calls": 50, "attempts": 3, "active_ms": 9_000_000})
        goals.add_task(self.k, g["goal_id"], "t", "p", ["c0"])
        script = "import pathlib\npathlib.Path('report.md').write_text('done')\npathlib.Path('calc.py').write_text('x = 1\\n')\n"
        self.assertTrue(supervisor.run_goal(self.k, g["goal_id"], fake(script))["done"])
        self.assertRefused("ACCEPT_UNVERIFIED", gitx.accept, self.k, g["goal_id"], "user")
        self.assertTrue(os.path.exists(os.path.join(isolated["canonical_root"], "report.md")))      # still there

    def test_a_result_that_needs_a_file_git_ignores_is_not_accepted(self):
        (self.root / ".gitignore").write_text("helper.py\n")
        sh(self.root, "add", ".gitignore")
        sh(self.root, "commit", "-q", "-m", "ignore")
        script = ("import pathlib\npathlib.Path('helper.py').write_text('def plus(a, b):\\n    return a + b\\n')\n"
                  "pathlib.Path('calc.py').write_text('from helper import plus\\ndef add(a, b):\\n    return plus(a, b)\\n')\n")
        isolated, made = self.done(script)
        self.assertIn("무시하는 파일", gitx.diff(self.k, made["goal_id"]))
        self.assertRefused("ACCEPT_UNVERIFIED", gitx.accept, self.k, made["goal_id"], "user")
        self.assertIn("return a - b", (self.root / "calc.py").read_text())        # nothing reached the user's repository
        self.assertTrue(os.path.exists(os.path.join(isolated["canonical_root"], "helper.py")))   # and nothing was thrown away
        self.assertEqual(sh(self.root, "rev-list", "--count", "HEAD").strip(), "2")

    def test_an_ignored_file_of_the_users_is_not_overwritten(self):
        (self.root / ".gitignore").write_text(".env\n")
        sh(self.root, "add", ".gitignore")
        sh(self.root, "commit", "-q", "-m", "ignore env")
        (self.root / ".env").write_text("MY_LOCAL_SETTING=1\n")
        script = FIX + "pathlib.Path('.gitignore').write_text('')\npathlib.Path('.env').write_text('REPLACED=1\\n')\n"
        isolated, made = self.done(script)
        self.assertRefused("ACCEPT_REFUSED_BY_GIT", gitx.accept, self.k, made["goal_id"], "user")
        self.assertEqual((self.root / ".env").read_text(), "MY_LOCAL_SETTING=1\n")

    def test_the_checkouts_git_locator_cannot_be_redirected(self):
        script = FIX + "pathlib.Path('.git').write_text('gitdir: /tmp/attacker-controlled-metadata\\n')\n"
        isolated, made = self.done(script)
        locator = open(os.path.join(isolated["canonical_root"], ".git")).read()
        self.assertNotIn("attacker", locator)
        self.assertEqual(gitx.accept(self.k, made["goal_id"], "user")["files"], ["calc.py"])
