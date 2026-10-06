"""`lupus fix-tests` on a real open-source project, against changes its maintainers really made.

For each chosen upstream commit that changed both the source and the tests: take the source as it
was BEFORE the commit and the tests as they are AFTER it. The upstream tests now fail, exactly as
they did for the maintainer before writing the change. Lupus is given nothing but the folder.
Success is the supervisor's own run of those frozen upstream tests.

Usage: PYTHONPATH=src python3 evaluations/real.py <git clone of hukkin/tomli> out.json <driver>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupus import adapters, goals, probe, projects, quick, supervisor
from lupus.kernel import Kernel

COMMITS = {
    "4e245a4": "loads(): raise TypeError, not AttributeError, for a non-str argument",
    "9eb2125": "TOML 1.1: seconds are optional in times",
    "e1fdb94": "limit the number of parts of a key",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def main(repo: str, out_path: str, driver: str) -> None:
    repo_path = Path(repo)
    base = Path(tempfile.mkdtemp(prefix="lupus-real-")).resolve()
    k = Kernel.init(base / "home")
    probe.run(k, live=True)
    out = {"project": "hukkin/tomli", "driver": driver, "rows": []}
    for commit, what in COMMITS.items():
        root = base / f"{commit}-{driver}"
        subprocess.run(["git", "clone", "-q", str(repo_path), str(root)], check=True)
        git(root, "checkout", "-q", commit + "~1")
        git(root, "checkout", commit, "--", "tests")              # the maintainer's tests for the change
        upstream = git(repo_path, "show", "--numstat", "--format=", commit, "--", "src")
        shutil.rmtree(root / ".git")
        project = projects.register(k, root, root.name, ["anthropic", "openai"])
        row = {"commit": commit, "change": what, "upstream_src_numstat": upstream.split()[:2]}
        started = time.monotonic()
        made = quick.fix_tests(k, project, "user")
        if "goal_id" not in made:
            row["refused"] = made
        else:
            task = goals.tasks(k, made["goal_id"])[0]
            row.update(runner=made["runner"], frozen=len(made["protect"]), failure_seen=task["spec"]["prompt"][-260:])
            report = supervisor.run_goal(k, made["goal_id"], adapters.native(driver), max_steps=3, timeout_s=420)
            totals = report["usage"]["totals"]
            row.update(done=report["done"], attempts=[s.get("outcome") or s.get("status") for s in report["steps"]],
                       tokens=totals["tokens_in"] + totals["tokens_cached"] + totals["tokens_out"],
                       seconds=round(time.monotonic() - started, 1),
                       os_sandbox=[s.get("os_sandbox") for s in report["steps"]],
                       frozen_tests_touched=k.one("SELECT COUNT(*) FROM event WHERE type = 'protect.restored' AND aggregate_id = ?",
                                                  made["goal_id"])[0])
        out["rows"].append(row)
        print(json.dumps(row, ensure_ascii=False)[:500], flush=True)
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    k.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
