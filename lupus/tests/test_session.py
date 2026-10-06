"""Interactive sessions: the terminal is handed over, the frozen tests stay frozen, and only the
supervisor's own run at the end decides."""

import json
import os
import pty
import select
import subprocess
import sys
import time

from lupus import adapters, goals, hook, quick, session, supervisor, verify
from lupus.kernel import Kernel

from .helpers import SRC, Env, fake

TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 2), 4)\n"
# Stands in for a CLI's interactive screen: needs a real terminal, reads what the "user" types.
TUI = r'''
import os, sys, pathlib
assert sys.stdin.isatty() and sys.stdout.isatty(), "no terminal"
assert os.tcgetpgrp(0) == os.getpgrp(), "not the foreground job"
assert not any(k.startswith(("ANTHROPIC_", "OPENAI_")) for k in os.environ), "provider key leaked"
print("ready>", flush=True)
line = sys.stdin.readline().strip()
if line == "fix":
    pathlib.Path("calc.py").write_text("def add(a, b):\n    return a + b\n")
elif line == "cheat":
    pathlib.Path("test_calc.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n")
pathlib.Path("test_new.py").write_text("import unittest\nclass N(unittest.TestCase):\n    def test_n(self): self.assertTrue(True)\n")
print("bye", flush=True)
'''
BODY = '''
from lupus import session
class Tui(adapters.InteractiveAdapter):
    def argv(self, prompt, cwd):
        return [sys.executable, "-c", {tui!r}]
project = projects.resolve(k, {root!r})
made = session.start(k, project, "user")
a = Tui("claude"); a.driver = "fake"
report = supervisor.run_goal(k, made["goal_id"], a, max_steps=1, timeout_s=30)
print("REPORT " + json.dumps({{"done": report["done"], "steps": report["steps"], "foreground": os.tcgetpgrp(0) == os.getpgrp()}}))
'''


def in_terminal(home, body: str, typed: bytes) -> str:
    """Run supervisor code in a child that owns a pseudo-terminal, typing `typed` when prompted."""
    prelude = ("import json, os, sys\nfrom pathlib import Path\nfrom lupus.kernel import Kernel\n"
               "from lupus import adapters, goals, projects, supervisor\n" f"k = Kernel(Path({str(home)!r}))\n")
    pid, master = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-c", prelude + body],
                  {**os.environ, "PYTHONPATH": SRC, "ANTHROPIC_API_KEY": "sk-should-not-reach-the-cli"})
    out, sent, deadline = b"", False, time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"ready>" in out:
                os.write(master, typed)
                sent = True
    os.waitpid(pid, 0)
    os.close(master)
    return out.decode("utf-8", errors="replace")


class SessionTests(Env):
    def setUp(self):
        super().setUp()
        (self.root / "test_calc.py").write_text(TEST)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def run_session(self, typed: bytes) -> dict:
        self.k.close()
        out = in_terminal(self.home, BODY.format(tui=TUI, root=str(self.root)), typed)
        self.reopen()
        line = next((l for l in out.splitlines() if l.startswith("REPORT ")), None)
        self.assertIsNotNone(line, out)
        return json.loads(line[7:])

    def test_work_done_in_the_terminal_is_verified_by_the_supervisor(self):
        report = self.run_session(b"fix\n")
        self.assertTrue(report["done"], report)
        self.assertTrue(report["foreground"])                     # the terminal was handed back
        self.assertTrue((self.root / "test_new.py").is_file())    # adding a test next to the frozen ones is allowed
        self.assertFalse(report["steps"][0]["usage_observed"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM run WHERE status <> 'STOPPED'")[0], 0)

    def test_editing_a_frozen_test_in_the_session_does_not_finish_it(self):
        report = self.run_session(b"cheat\n")
        self.assertFalse(report["done"])
        self.assertEqual((self.root / "test_calc.py").read_text(), TEST)
        self.assertEqual(report["steps"][0]["verdicts"], {"c0": "FAIL"})

    def test_no_terminal_no_session_and_each_session_is_its_own_goal(self):
        made = session.start(self.k, self.project, "user")
        self.assertEqual((made["baseline"], made["frozen"]), ("FAIL", 1))
        a = adapters.InteractiveAdapter("claude")
        a.driver = "fake"
        report = supervisor.run_goal(self.k, made["goal_id"], a, max_steps=1)
        self.assertEqual(report["steps"][0]["reason"], "USER_PRESENCE_REQUIRED")
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM run WHERE status <> 'STOPPED'")[0], 0)
        again = session.start(self.k, self.project, "user")
        self.assertEqual(goals.get(self.k, made["goal_id"])["status"], "CANCELLED")
        self.assertNotEqual(again["goal_id"], made["goal_id"])
        self.assertRefused("USER_AUTHORITY_REQUIRED", session.start, self.k, self.project, "worker")

    def test_claude_gets_hooks_for_this_process_only_and_codex_gets_none(self):
        made = session.start(self.k, self.project, "user")
        claude = session.adapter(self.k, made["goal_id"], "claude", ["--model", "opus"])
        argv = claude.argv("", self.root)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(sorted(settings["hooks"]), ["PreToolUse", "Stop"])
        self.assertIn(str(self.home), settings["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(argv[-2:], ["--model", "opus"])
        self.assertIn("고정", argv[argv.index("--append-system-prompt") + 1])
        codex = session.adapter(self.k, made["goal_id"], "codex")
        self.assertNotIn("--settings", codex.argv("", self.root))
        quiet = session.adapter(self.k, made["goal_id"], "claude", stop_check=False)
        self.assertNotIn("Stop", json.loads(quiet.argv("", self.root)[2])["hooks"])


class HookTests(Env):
    def setUp(self):
        super().setUp()
        (self.root / "test_calc.py").write_text(TEST)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        self.goal_id = session.start(self.k, self.project, "user")["goal_id"]
        session.adapter(self.k, self.goal_id, "claude")
        from lupus import protect
        protect.freeze(self.k, self.goal_id, self.root)

    def test_an_edit_to_a_frozen_file_is_refused_while_it_happens(self):
        code, why = hook.pre_edit(self.k, self.goal_id, {"tool_input": {"file_path": str(self.root / "test_calc.py")}})
        self.assertEqual(code, 2)
        self.assertIn("고정된 검증 파일", why)
        for path in (str(self.root / "calc.py"), "calc.py", "/etc/hosts", str(self.root / "conftest.py")):
            expect = 2 if path.endswith("conftest.py") else 0       # a new runner config may not appear either
            self.assertEqual(hook.pre_edit(self.k, self.goal_id, {"tool_input": {"file_path": path}, "cwd": str(self.root)})[0], expect, path)

    def test_stop_check_speaks_only_after_a_change_and_a_limited_number_of_times(self):
        code, why = hook.stop(self.k, self.goal_id, {})
        self.assertEqual(code, 2)                                   # tests fail: back to work, with the real output
        self.assertIn("AssertionError", why)
        self.assertEqual(hook.stop(self.k, self.goal_id, {})[0], 0)   # nothing changed since: a conversation is not interrupted
        for i in range(5):
            (self.root / "calc.py").write_text(f"def add(a, b):\n    return a - b - {i}\n")
            code, _ = hook.stop(self.k, self.goal_id, {})
        self.assertEqual(code, 0)                                   # the limit was reached; the final run still decides
        self.assertEqual(self.k.one("SELECT stop_blocks FROM session")[0], self.k.policy["session_stop_blocks"])
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        self.assertEqual(hook.stop(self.k, self.goal_id, {})[0], 0)

    def test_the_hook_program_never_blocks_when_it_cannot_run(self):
        proc = subprocess.run([sys.executable, session.HOOK, "stop", str(self.tmp / "no-such-home"), "goal_x"],
                              input="{}", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("skipped", proc.stderr)
        proc = subprocess.run([sys.executable, session.HOOK, "pre-edit", str(self.home), self.goal_id],
                              input=json.dumps({"tool_input": {"file_path": str(self.root / "test_calc.py")}}),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)


class SessionFreezeTests(Env):
    def test_new_tests_may_be_added_but_not_a_collection_hook_anywhere(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("")
        (self.root / "tests" / "test_calc.py").write_text(TEST)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        made = session.start(self.k, self.project, "user")
        session.adapter(self.k, made["goal_id"], "claude")
        sneaky = ("import pathlib\npathlib.Path('tests/conftest.py').write_text('collect_ignore = [\"test_calc.py\"]\\n')\n"
                  "pathlib.Path('tests/test_extra.py').write_text('import unittest\\nclass E(unittest.TestCase):\\n    def test_e(self): pass\\n')")
        report = supervisor.run_goal(self.k, made["goal_id"], fake(sneaky), max_steps=1)
        self.assertFalse(report["done"])
        self.assertFalse((self.root / "tests" / "conftest.py").exists())
        self.assertTrue((self.root / "tests" / "test_extra.py").exists())
        code, _ = hook.pre_edit(self.k, made["goal_id"], {"tool_input": {"file_path": str(self.root / "tests" / "sub" / "conftest.py")}})
        self.assertEqual(code, 2)
