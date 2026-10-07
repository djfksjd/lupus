"""Does Lupus change how often a request is done RIGHT? A small benchmark in the SWE-bench style.

  mine   From real repositories, collect upstream commits that changed both source and tests,
         and keep those that validate: with the commit's tests on the PARENT's source at least
         one changed test file fails, and on the commit itself they pass ("fail to pass").
  run    For each instance the worker gets the repository at the parent commit and ONLY the commit
         message as the request. It never sees the upstream tests. Afterwards the upstream test
         files are put in place and run; that hidden run is the score.
         Arms: plain (one lean CLI call) and do (`lupus do`, approval simulated). With a reviewer
         named, the do arm continues with `--review` and is scored a second time.

Usage:
  PYTHONPATH=src python3 evaluations/issues.py mine <workdir> instances.json [per_repo]
  PYTHONPATH=src python3 evaluations/issues.py run <workdir> instances.json out.json <driver> [limit] [reviewer] [arms]
  PYTHONPATH=src python3 evaluations/issues.py run <workdir> instances.json out.json <driver> [limit] [reviewer] [arms] [commits]
  PYTHONPATH=src python3 evaluations/issues.py validate <workdir> instances.json     (the scorer on known answers)
  (arms: "plain,do" by default; "do" alone repeats only the Lupus arm; "lean" is `lupus do --lean`; "release" is
   `lupus do` followed, when it stops on existing tests, by a scripted user who releases the test assets the
   upstream commit changed and approves the worker's diff. commits: comma list of commit prefixes to run)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupus import adapters, contract, goals, probe, projects, quick, release, review, supervisor, verify
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


def added_lines(pristine: Path, root: Path) -> int:
    """Lines the arm added to the project, not counting test files (the size `--lean` is about)."""
    out = subprocess.run(["git", "diff", "--no-index", "--numstat", str(pristine), str(root)], capture_output=True, text=True).stdout
    total = 0
    for line in out.splitlines():
        added, _, name = line.split("\t", 2)
        if added.isdigit() and "test" not in name.lower() and "__pycache__" not in name and ".pytest_cache" not in name:
            total += int(added)
    return total


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
        install_tests(clone, inst, copy)
        return run_tests(copy, inst["tests"])


def install_tests(clone: Path, inst: dict, copy: Path) -> None:
    """The upstream commit's WHOLE test directory in place of whatever is there: test files,
    fixtures and data, additions and deletions alike. (Until 2026-10-07 only the changed test
    *.py files were copied, so an instance whose upstream change also touched fixtures was scored
    against a mixture of the worker's fixtures and upstream's tests.) Product code is not touched."""
    tests_dir = REPOS[inst["repo"]][2]
    shutil.rmtree(copy / tests_dir, ignore_errors=True)
    archive = subprocess.run(["git", "-C", str(clone), "archive", "--format=tar", inst["commit"], tests_dir], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(copy)], input=archive.stdout, check=True)


def validate(work: Path, instances_path: str) -> None:
    """The scorer itself, checked on known answers: the parent commit must fail, the upstream
    commit must pass. An instance where that does not hold cannot be scored and is reported."""
    bad = []
    for inst in json.loads(Path(instances_path).read_text()):
        clone = work / "clones" / inst["repo"]
        seen = {}
        for label, rev in (("parent", inst["commit"] + "~1"), ("upstream", inst["commit"])):
            with tempfile.TemporaryDirectory(prefix="lupus-validate-") as tmp:
                root = Path(tmp).resolve() / "r"
                checkout(clone, rev, root)
                seen[label] = score(clone, inst, root)[0]
        ok = seen == {"parent": False, "upstream": True}
        print(inst["repo"], inst["commit"][:8], seen, "" if ok else "<-- scorer does not separate them", flush=True)
        if not ok:
            bad.append(inst["commit"][:8])
    print("unscorable:", bad or "none")


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


def scripted_release(k: Kernel, clone: Path, inst: dict, goal_id: str, adapter) -> dict:
    """A simulated user for a goal stuck on existing tests. It releases the test assets the UPSTREAM
    commit changed (that is reference information about scope, and is reported as such) and approves
    whatever diff the worker proposes inside them: an upstream-scoped, permissive-approval run. It
    shows whether the flow can recover such a goal, not whether a person would have approved."""
    tests_dir = REPOS[inst["repo"]][2]
    changed = [line.split("\t")[-1] for line in git(clone, "show", "--name-status", "--format=", inst["commit"], "--", tests_dir).splitlines() if line.strip()]
    out = {"asked": changed, "released": [], "refused": [], "rounds": []}
    wanted: list[str] = []
    for path in changed:
        candidate = path
        while candidate and candidate != ".":
            try:
                release.grant(k, goal_id, [candidate], "user")      # (asked one by one only to find what is releasable)
                wanted.append(candidate)
                break
            except LupusError as exc:
                if exc.code != "RELEASE_REFUSED":
                    out["refused"].append(f"{path}: {exc.code}")
                    break
                candidate = str(Path(candidate).parent)      # a file upstream added: the frozen folder it goes into
        else:
            out["refused"].append(path)
    out["released"] = release.granted(k, goal_id)
    report = None
    for _ in range(3):
        report = supervisor.run_goal(k, goal_id, adapter, timeout_s=420, max_steps=2)
        found = release.proposal(k, goal_id)
        seen = k.one("SELECT payload FROM event WHERE type = 'release.provisional' AND aggregate_id = ? ORDER BY seq DESC LIMIT 1", goal_id)
        out["rounds"].append({"steps": [s.get("outcome") or s.get("status") for s in report["steps"]], "done": report["done"],
                              "proposed": [[e["path"], e["change"]] for e in found["entries"]] if found else [],
                              "trial": json.loads(seen["payload"])["verdicts"] if seen and found else None})
        if report["done"] or found is None:
            break
        release.approve(k, goal_id, found["digest"], "user")
    out["done"] = bool(report and report["done"])
    return out


def run(work: Path, instances_path: str, out_path: str, driver: str, limit: int, reviewer: str | None = None,
        arms: tuple[str, ...] = ("plain", "do"), only: tuple[str, ...] = ()) -> None:
    instances = [i for i in json.loads(Path(instances_path).read_text()) if not only or i["commit"].startswith(only)][:limit]
    base = Path(tempfile.mkdtemp(prefix="lupus-issues-")).resolve()
    k = Kernel.init(base / "home")
    probe.run(k, live=True)
    out = {"driver": driver, "reviewer": reviewer, "arms": list(arms), "date": time.strftime("%Y-%m-%d"), "rows": []}
    for i, inst in enumerate(instances):
        clone = work / "clones" / inst["repo"]
        pristine = base / f"{i}-pristine"
        checkout(clone, inst["commit"] + "~1", pristine)
        for arm in arms:
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
                    draft = quick.draft_check(k, project, inst["request"], "user", allow_failing=True, lean=arm == "lean")
                    row["already_failing"] = len(draft["already_failing"])
                    first = supervisor.run_goal(k, draft["goal_id"], adapter, timeout_s=420, max_steps=3)
                    row["check_drafted"] = first["done"]
                    if first["done"]:
                        test = root / draft["test_path"]
                        # what a user would have been shown at approval, kept for later reading
                        row["approval"] = {"test": test.read_text(errors="replace")[:6000],
                                           "described": contract.latest(k, draft["goal_id"], sha256_bytes(test.read_bytes())),
                                           "red": ((goals.latest_evidence(k, draft["goal_id"]).get("c0") or {}).get("detail") or "")[-400:]}
                        build = quick.approve_check(k, project, draft["goal_id"], draft["request"],
                                                    sha256_bytes(test.read_bytes()), "user", keep_base=bool(reviewer))
                        second = supervisor.run_goal(k, build["goal_id"], adapter, timeout_s=420, max_steps=3)
                        row.update(lupus_done=second["done"], staged=bool(build["staged_applied"]),
                                   build_steps=[s.get("outcome") or s.get("status") for s in second["steps"]])
                        if arm == "release" and not second["done"]:
                            row["stuck_on_existing_tests"] = bool(release.hint(k, build["goal_id"]))
                            row["release"] = scripted_release(k, clone, inst, build["goal_id"], adapter)
                            row["lupus_done"] = row["release"]["done"]
                        if reviewer and second["done"] and "review_base" in build:
                            row["hidden_before_review"] = score(clone, inst, root)[0]
                            row["seconds_before_review"] = round(time.monotonic() - started, 1)
                            row["tokens_before_review"] = usage_of(log)["tokens"]
                            seen = k.one("SELECT COUNT(*) FROM service_call")[0]
                            outcome = review.cycle(
                                k, build["goal_id"], draft["request"], reviewer, Path(build["review_base"]),
                                lambda gid: supervisor.run_goal(k, gid, adapter, timeout_s=420, max_steps=3),
                                quick.DEFAULT_CAPS, skip={draft["test_path"]})
                            calls = [json.loads(r["usage"]) for r in k.q(
                                "SELECT usage FROM service_call WHERE usage IS NOT NULL ORDER BY rowid") ][seen:]
                            row["review"] = {
                                "skipped": outcome.get("skipped"), "objections": outcome.get("objections", []),
                                "dropped": outcome.get("dropped", 0), "revised": outcome["revised"],
                                "revision_done": outcome.get("revision_done"), "remaining": outcome.get("remaining"),
                                "not_put_back": outcome.get("not_put_back", []),
                                "reviewer_tokens": sum(u.get("tokens_in", 0) + u.get("tokens_cached", 0) + u.get("tokens_out", 0)
                                                       for u in calls)}
                    else:
                        quick.discard_draft(k, project, draft["goal_id"], "user")
            except LupusError as exc:
                row["refused"] = f"{exc.code}: {exc.detail[:120]}"
            row["seconds"] = round(time.monotonic() - started, 1)
            row["added_lines"] = added_lines(pristine, root)
            passed, detail = score(clone, inst, root)
            row.update(hidden_tests_pass=passed, **usage_of(log), **({} if passed else {"hidden_detail": detail[-160:]}))
            out["rows"].append(row)
            print(json.dumps(row, ensure_ascii=False)[:330], flush=True)
            summary = {}
            for a in arms:
                sel = [r for r in out["rows"] if r["arm"] == a]
                if sel:
                    summary[a] = {"n": len(sel), "hidden_pass": sum(r["hidden_tests_pass"] for r in sel),
                                  "added_lines_mean": round(sum(r["added_lines"] for r in sel) / len(sel), 1),
                                  "tokens_mean": round(sum(r["tokens"] for r in sel) / len(sel)),
                                  "seconds_mean": round(sum(r["seconds"] for r in sel) / len(sel), 1)}
                    if a != "plain":
                        said_done = [r for r in sel if r.get("lupus_done")]
                        summary[a].update(lupus_said_done=len(said_done),
                                          said_done_but_hidden_fail=sum(not r["hidden_tests_pass"] for r in said_done))
                        seen_by_reviewer = [r for r in sel if "review" in r and not r["review"]["skipped"]]
                        if seen_by_reviewer:
                            clean = [r for r in seen_by_reviewer if not r["review"]["objections"]
                                     or (r["review"]["revision_done"] and r["review"]["remaining"] == [])]
                            summary[a]["review"] = {
                                "reviewed": len(seen_by_reviewer),
                                "hidden_pass_before": sum(r["hidden_before_review"] for r in seen_by_reviewer),
                                "hidden_pass_after": sum(r["hidden_tests_pass"] for r in seen_by_reviewer),
                                "objected": sum(bool(r["review"]["objections"]) for r in seen_by_reviewer),
                                "ended_clean": len(clean),
                                "clean_but_hidden_fail": sum(not r["hidden_tests_pass"] for r in clean),
                                "objections_left_and_hidden_fail": sum(not r["hidden_tests_pass"] for r in seen_by_reviewer
                                                                       if r not in clean),
                                "reviewer_tokens_mean": round(sum(r["review"]["reviewer_tokens"] for r in seen_by_reviewer)
                                                              / len(seen_by_reviewer))}
            out["summary"] = summary
            Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    k.close()


if __name__ == "__main__":
    if sys.argv[1] == "mine":
        mine(Path(sys.argv[2]).resolve(), sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 3)
    elif sys.argv[1] == "validate":
        validate(Path(sys.argv[2]).resolve(), sys.argv[3])
    else:
        run(Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6]) if len(sys.argv) > 6 else 99,
            (sys.argv[7] if len(sys.argv) > 7 else None) or None,
            tuple(sys.argv[8].split(",")) if len(sys.argv) > 8 else ("plain", "do"),
            tuple(sys.argv[9].split(",")) if len(sys.argv) > 9 else ())
