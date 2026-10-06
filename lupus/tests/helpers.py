"""Shared fixtures. Every test runs against a throw-away runtime, project and vault."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lupus import adapters, goals, projects
from lupus.kernel import Kernel

SRC = str(Path(__file__).resolve().parent.parent / "src")
CAPS = {"calls": 200, "attempts": 20, "active_ms": 100_000_000}


class Clock:
    def __init__(self) -> None:
        self.t = int(time.time() * 1000)   # real 'now', so file mtimes and subprocesses agree

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def contains(cid: str, path: str, text: str) -> dict:
    return {"id": cid, "text": f"{path} contains {text}",
            "verifier": {"kind": "file_contains", "path": path, "text": text}}


# Worker script: "write <file> <text>" tasks are carried out literally; every execution is
# appended to calls.log so tests can count how often a "model" was really invoked.
WRITER = r'''
import sys, pathlib
prompt = sys.argv[1]
with open("calls.log", "a") as f:
    f.write(prompt.splitlines()[1] + "\n")
for line in prompt.splitlines():
    if line.startswith("write "):
        _, name, text = line.split(" ", 2)
        pathlib.Path(name).write_text(text)
print("done")
'''
LIAR = 'import sys\nopen("calls.log","a").write("lie\\n")\nprint("done, everything works")'


class Env(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lupus-test-")
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.clock = Clock()
        self.home = self.tmp / "home"
        self.k = Kernel.init(self.home, self.clock)
        self.root = self.tmp / "proj"
        self.root.mkdir()
        self.project = projects.register(self.k, self.root, "demo", ["local"])
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.k.close()
        self._tmp.cleanup()

    def reopen(self) -> Kernel:
        self.k.close()
        self.k = Kernel(self.home, self.clock)
        return self.k

    def set_policy(self, **changes: int) -> None:
        with self.k.tx():
            self.k.set_meta("policy", json.dumps({**self.k.policy, **changes}))

    def goal(self, files: dict[str, str] | None = None, caps: dict | None = None, chain: bool = True) -> dict:
        """A goal with one criterion + one task per file ("write <file> <text>")."""
        files = files or {"a.txt": "A"}
        crits = [contains(f"c{i}", name, text) for i, (name, text) in enumerate(files.items())]
        goal = goals.submit(self.k, self.project["project_id"], "demo goal", crits, caps or CAPS)
        prev: list[str] = []
        for i, (name, text) in enumerate(files.items()):
            task = goals.add_task(self.k, goal["goal_id"], f"task {name}", f"write {name} {text}",
                                  [f"c{i}"], prev if chain else [])
            prev = [task["task_id"]]
        return goals.get(self.k, goal["goal_id"])

    def task_ids(self, goal_id: str) -> list[str]:
        return [t["task_id"] for t in goals.tasks(self.k, goal_id)]

    def calls(self) -> list[str]:
        log = self.root / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def assertRefused(self, code: str, fn, *args, **kwargs) -> None:
        from lupus.util import LupusError
        with self.assertRaises(LupusError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception)


def evidence(k: Kernel, goal_id: str, criterion_id: str, result: str, artifact_hash: str | None = None, **kw) -> str:
    """Record evidence the way the supervisor does: for the current revision and verifier."""
    from lupus import verify
    goal = goals.get(k, goal_id)
    criterion = next(c for c in goals.criteria(k, goal_id) if c["id"] == criterion_id)
    digest = artifact_hash or verify.artifact_hash(criterion["verifier"], goals.project_root(k, goal_id))
    return goals.record_evidence(k, goal_id, criterion_id, "1", digest, result,
                                 acceptance_revision=goal["acceptance_revision"],
                                 verifier=criterion["verifier"], **kw)


def fake(script: str = WRITER, driver: str = "fake", **kw) -> adapters.FakeAdapter:
    adapter = adapters.FakeAdapter(script, **kw)
    adapter.driver = driver
    return adapter


def run_script(home: Path, body: str, crash_at: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run supervisor code in a separate process so it can be SIGKILLed at a crash point."""
    env = {**os.environ, "PYTHONPATH": SRC}
    if crash_at:
        env["LUPUS_CRASH_AT"] = crash_at
    prelude = (
        "import json, sys\nfrom pathlib import Path\n"
        "from lupus.kernel import Kernel\n"
        "from lupus import actions, adapters, goals, handoff, projects, recovery, runs, supervisor, usage\n"
        f"k = Kernel(Path({str(home)!r}))\n"
    )
    return subprocess.run([sys.executable, "-c", prelude + body], env=env, capture_output=True,
                          text=True, timeout=timeout)
