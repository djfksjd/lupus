"""Does Lupus change how often a request is done RIGHT? A small benchmark in the SWE-bench style.

  mine   From real repositories, collect upstream commits that changed both source and tests,
         and keep those that validate: with the commit's tests on the PARENT's source at least
         one changed test file fails, and on the commit itself they pass ("fail to pass").
  run    For each instance the worker gets the repository at the parent commit and ONLY the commit
         message as the request. It never sees the upstream tests. Afterwards the upstream test
         files are put in place and run; that hidden run is the score.
         Arms: plain (one lean CLI call) and do (`lupus do`, approval simulated).

Usage:
  PYTHONPATH=src python3 evaluations/issues.py mine <workdir> instances.json [per_repo]
  PYTHONPATH=src python3 evaluations/issues.py run <workdir> instances.json out.json <driver> [limit]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupus import adapters, probe, projects, quick, supervisor, verify
from lupus.kernel import Kernel
from lupus.util import LupusError, sha256_bytes

REPOS = {      # pure-Python projects whose tests need nothing but pytest/unittest
    "sqlparse": ("https://github.com/andialbrecht/sqlparse.git", "sqlparse", "tests"),
    "more-itertools": ("https://github.com/more-itertools/more-itertools.git", "more_itertools", "tests"),
    "tomli": ("https://github.com/hukkin/tomli.git", "src/tomli", "tests"),
    "packaging": ("https://github.com/pypa/packaging.git", "src/packaging", "tests"),
    "click": ("https://github.com/pallets/click.git", "src/click", "tests"),
}
PY = sys.executable


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


def run_tests(root: Path, files: list[str]) -> tuple[bool, str]:
    """The given test files, inside Lupus's verifier sandbox (this is other people's code)."""
    env = {"PYTHONPATH": "src"} if (root / "src").is_dir() else {}
    v = {"kind": "command", "argv": [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", *files], "paths": ["."],
         "timeout_s": 300, **({"env": env} if env else {})}
    verdict, _, detail = verify.run(v, root)
    return verdict == "PASS", detail


def checkout(clone: Path, commit: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(["git", "clone", "-q", str(clone), str(dest)], check=True)
    git(dest, "checkout", "-q", commit)
    shutil.rmtree(dest / ".git")


def mine(work: Path, out_path: str, per_repo: int) -> None:
    work.mkdir(parents=True, exist_ok=True)
    instances = []
    for name, (url, src, tests) in REPOS.items():
        clone = work / "clones" / name
        if not clone.exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", url, str(clone)], check=True)
        kept = 0
        for commit in git(clone, "log", "--no-merges", "--format=%H", "-n", "400", "--", tests).split():
            if kept >= per_repo:
                break
            changed = git(clone, "show", "--name-only", "--format=", commit).split()
            src_files = [f for f in changed if f.startswith(src + "/") and f.endswith(".py")]
            test_files = [f for f in changed if f.startswith(tests + "/") and f.endswith(".py") and Path(f).name.startswith("test")]
            stat = git(clone, "show", "--numstat", "--format=", commit, "--", src).split("\n")
            lines = sum(int(x.split()[0]) + int(x.split()[1]) for x in stat if x.strip() and x.split()[0].isdigit())
            message = git(clone, "log", "-1", "--format=%B", commit).strip()
            if not src_files or not test_files or not 3 <= lines <= 120 or len(message) < 25 or len(changed) > 8:
                continue
            parent = work / "check" / f"{name}-{commit[:8]}"
            checkout(clone, commit + "~1", parent)
            for f in test_files:      # the commit's tests on the parent's source
                (parent / f).parent.mkdir(parents=True, exist_ok=True)
                (parent / f).write_text(git(clone, "show", f"{commit}:{f}"))
            red, detail = run_tests(parent, test_files)
            if red or "no tests ran" in detail or "error" in detail.lower() and "passed" not in detail and "failed" not in detail:
                shutil.rmtree(parent)
                continue                  # not a fail-to-pass change, or the tests cannot run here
            fixed = work / "check" / f"{name}-{commit[:8]}-fixed"
            checkout(clone, commit, fixed)
            green, _ = run_tests(fixed, test_files)
            shutil.rmtree(parent)
            shutil.rmtree(fixed)
            if not green:
                continue
            instances.append({"repo": name, "commit": commit, "request": message[:1500], "tests": test_files,
                              "source_files": src_files, "upstream_lines_changed": lines, "failure_before": detail[-200:]})
            kept += 1
            print(f"{name} {commit[:8]} ok ({lines} lines): {message.splitlines()[0][:70]}", flush=True)
    Path(out_path).write_text(json.dumps(instances, ensure_ascii=False, indent=1) + "\n")
    print(len(instances), "instances")


def score(clone: Path, inst: dict, root: Path) -> tuple[bool, str]:
    """Put the upstream tests in place, in a copy, and run them."""
    with tempfile.TemporaryDirectory(prefix="lupus-score-") as tmp:
        copy = Path(tmp).resolve() / "w"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__", ".lupus-staged"))
        for path in copy.rglob("test_lupus_*.py"):      # the check Lupus drafted is not part of the score
            path.unlink()
        for f in inst["tests"]:
            (copy / f).parent.mkdir(parents=True, exist_ok=True)
            (copy / f).write_text(git(clone, "show", f"{inst['commit']}:{f}"))
        return run_tests(copy, inst["tests"])


def usage_of(log: list) -> dict:
    get = lambda key: sum((r.usage or {}).get(key, 0) for r in log)
    return {"calls": len(log), "tokens": get("tokens_in") + get("tokens_cached") + get("tokens_out"),
            "errors": [r.error_class for r in log if r.error_class]}


def recording(adapter, log):
    cls = adapter.__class__

    class Rec(cls):
        def parse(self, exit_code, stdout, stderr):
            result = super().parse(exit_code, stdout, stderr)
            log.append(result)
            return result
    adapter.__class__ = Rec
    return adapter


def run(work: Path, instances_path: str, out_path: str, driver: str, limit: int) -> None:
    instances = json.loads(Path(instances_path).read_text())[:limit]
    base = Path(tempfile.mkdtemp(prefix="lupus-issues-")).resolve()
    k = Kernel.init(base / "home")
    probe.run(k, live=True)
    out = {"driver": driver, "date": time.strftime("%Y-%m-%d"), "rows": []}
    for i, inst in enumerate(instances):
        clone = work / "clones" / inst["repo"]
        for arm in ("plain", "do"):
            root = base / f"{i}-{arm}"
            checkout(clone, inst["commit"] + "~1", root)
            log: list = []
            adapter = recording(adapters.native(driver), log)
            started = time.monotonic()
            row = {"repo": inst["repo"], "commit": inst["commit"][:8], "arm": arm}
            try:
                if arm == "plain":
                    adapters.execute(adapter, inst["request"] + "\n\n위 변경을 이 저장소에 구현하라. 현재 디렉터리 안의 파일만 읽고 수정하라.",
                                     root, lambda pid: None, timeout_s=420)
                else:
                    project = projects.register(k, root, root.name, ["anthropic", "openai"])
                    draft = quick.draft_check(k, project, inst["request"], "user", allow_failing=True)
                    row["already_failing"] = len(draft["already_failing"])
                    first = supervisor.run_goal(k, draft["goal_id"], adapter, timeout_s=420, max_steps=3)
                    row["check_drafted"] = first["done"]
                    if first["done"]:
                        test = root / draft["test_path"]
                        build = quick.approve_check(k, project, draft["goal_id"], draft["request"],
                                                    sha256_bytes(test.read_bytes()), "user")
                        second = supervisor.run_goal(k, build["goal_id"], adapter, timeout_s=420, max_steps=3)
                        row.update(lupus_done=second["done"], staged=bool(build["staged_applied"]),
                                   build_steps=[s.get("outcome") or s.get("status") for s in second["steps"]])
                    else:
                        quick.discard_draft(k, project, draft["goal_id"], "user")
            except LupusError as exc:
                row["refused"] = f"{exc.code}: {exc.detail[:120]}"
            row["seconds"] = round(time.monotonic() - started, 1)
            passed, detail = score(clone, inst, root)
            row.update(hidden_tests_pass=passed, **usage_of(log), **({} if passed else {"hidden_detail": detail[-160:]}))
            out["rows"].append(row)
            print(json.dumps(row, ensure_ascii=False)[:330], flush=True)
            summary = {}
            for a in ("plain", "do"):
                sel = [r for r in out["rows"] if r["arm"] == a]
                if sel:
                    summary[a] = {"n": len(sel), "hidden_pass": sum(r["hidden_tests_pass"] for r in sel),
                                  "tokens_mean": round(sum(r["tokens"] for r in sel) / len(sel)),
                                  "seconds_mean": round(sum(r["seconds"] for r in sel) / len(sel), 1)}
                    if a == "do":
                        said_done = [r for r in sel if r.get("lupus_done")]
                        summary[a].update(lupus_said_done=len(said_done),
                                          said_done_but_hidden_fail=sum(not r["hidden_tests_pass"] for r in said_done))
            out["summary"] = summary
            Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    k.close()


if __name__ == "__main__":
    if sys.argv[1] == "mine":
        mine(Path(sys.argv[2]).resolve(), sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 3)
    else:
        run(Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6]) if len(sys.argv) > 6 else 99)
