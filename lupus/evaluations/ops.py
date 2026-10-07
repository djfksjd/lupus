"""What Lupus is for, on the scoreboard: an interrupted run, its recovery, and bringing the
result into a branch that has moved. (The hidden-test pilot only scores the final diff.)

For each chosen instance and phase, `lupus do` runs in an isolated checkout of a real clone:

    worker   the supervisor process is killed while the drafting worker runs
    after    it is killed right after the worker finished, before anything was verified
    verify   it is killed while the build goal's checks run
    none     not interrupted (the comparison)

Then: a new supervisor must refuse to start while the dead one's worker is alive, stop it only
through the authorised path, and finish the goal. The user's branch gets an unrelated commit in
the meantime; `lupus accept` must bring the result in on top of it. Recorded per run:

    refused_while_writer_alive   the restart was refused before the stale worker was stopped
    stale_stopped                …and nothing of it was left afterwards
    calls_after_resume           model calls made after the restart (recovery cost)
    redone                       a step that was durably finished before the kill was done again
    done, accepted, combined     the goal finished; the result reached the moved branch
    hidden_tests_pass            the upstream tests, on the user's branch
    users_commit_kept            the unrelated commit is still there, byte for byte
    stray_changes                anything in the user's working tree that neither commit explains

Two controls on the last instance: a branch edit that conflicts must make accept refuse and
change nothing; `lupus discard` must leave the user's repository exactly as it was.

Usage: PYTHONPATH=src python3 evaluations/ops.py <workdir> instances.json out.json <driver> <commit,commit,…> [phases]
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from lupus import adapters, gitx, goals, probe, projects, quick, supervisor
from lupus.kernel import Kernel
from lupus.util import LupusError, group_alive, sha256_bytes

from issues import PY, git, score

SRC = str(Path(__file__).resolve().parent.parent / "src")
CHILD = ("import sys\nfrom pathlib import Path\nfrom lupus.kernel import Kernel\nfrom lupus import adapters, supervisor\n"
         "k = Kernel(Path(sys.argv[1]))\nsupervisor.run_goal(k, sys.argv[2], adapters.native(sys.argv[3]), timeout_s=420, max_steps=3)\n")


def run_child(home: Path, goal_id: str, driver: str, kill_when, crash_at: str | None = None) -> bool:
    """Run the goal in a supervisor process of its own. Returns True if that process was killed."""
    env = {**os.environ, "PYTHONPATH": SRC, **({"LUPUS_CRASH_AT": crash_at} if crash_at else {})}
    proc = subprocess.Popen([PY, "-c", CHILD, str(home), goal_id, driver], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    killed = threading.Event()

    def watch() -> None:
        k = Kernel(home)
        try:
            while proc.poll() is None:
                if kill_when is not None and kill_when(k):
                    time.sleep(3.0)                    # well inside the phase, not at its first instant
                    if proc.poll() is None:
                        os.kill(proc.pid, signal.SIGKILL)
                        killed.set()
                    return
                time.sleep(0.2)
        finally:
            k.close()
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    proc.wait()
    watcher.join(5)
    return killed.is_set() or (crash_at is not None and proc.returncode not in (0, None))


def attempts(k: Kernel) -> int:
    return k.one("SELECT COUNT(*) FROM attempt")[0]


def resume(k: Kernel, goal_id: str, driver: str, row: dict) -> dict:
    """What a user does after a crash: start again; if a worker of the dead supervisor is still
    there, stop it through `recover --stop-stale-writer`; then go on."""
    first = supervisor.recover(k)
    stale = [w.get("pid") or w.get("aux_pid") for w in first["writers_alive"] if w.get("pid") or w.get("aux_pid")]
    if first["writers_alive"]:
        row["left_alive"] = [("worker" if w.get("pid") else "verifier" if w.get("aux_pid") else "run") for w in first["writers_alive"]]
        before = attempts(k)
        refused = supervisor.run_goal(k, goal_id, adapters.native(driver), timeout_s=420, max_steps=1)
        row["refused_while_writer_alive"] = not refused["done"] and attempts(k) == before
        row["refusal"] = [s.get("blockers") or s.get("reason") or s.get("status") for s in refused["steps"]][:2]
        for _ in range(20):                      # what a user does: ask again until nothing of the old run is left
            if not supervisor.recover(k, stop_stale_writer=True)["writers_alive"]:
                break
            time.sleep(1.0)
        row["stale_stopped"] = not any(group_alive(pid) for pid in stale)
    before = attempts(k)
    report = supervisor.run_goal(k, goal_id, adapters.native(driver), timeout_s=420, max_steps=3)
    row["calls_after_resume"] = row.get("calls_after_resume", 0) + attempts(k) - before
    return report


def one(base: Path, template: Path, clone: Path, inst: dict, driver: str, phase: str, control: str | None = None) -> dict:
    row = {"repo": inst["repo"], "commit": inst["commit"][:8], "phase": phase, **({"control": control} if control else {})}
    name = f"{inst['commit'][:8]}-{phase}{'-' + control if control else ''}"
    home, origin = base / f"home-{name}", base / f"origin-{name}"
    shutil.copytree(template, home)
    subprocess.run(["git", "clone", "-q", str(clone), str(origin)], check=True)
    git(origin, "checkout", "-q", "-B", "main", inst["commit"] + "~1")
    git(origin, "config", "user.name", "User"); git(origin, "config", "user.email", "user@example.invalid")
    k = Kernel(home)
    started = time.monotonic()
    try:
        project = projects.register(k, origin, name, ["anthropic", "openai"])
        isolated = gitx.isolate(k, project, "user")
        draft = quick.draft_check(k, isolated, inst["request"], "user", allow_failing=True)
        worker_running = lambda kk: kk.one("SELECT 1 FROM run WHERE goal_id = ? AND status = 'ACTIVE' AND pid IS NOT NULL", draft["goal_id"]) is not None      # noqa: E731
        killed = run_child(home, draft["goal_id"], driver, worker_running if phase == "worker" else None,
                           "supervisor.after_worker" if phase == "after" else None)
        row["killed"] = killed if phase in ("worker", "after") else False
        durable_before = attempts(k) if phase == "after" else None
        first = resume(k, draft["goal_id"], driver, row) if killed else supervisor.run_goal(k, draft["goal_id"], adapters.native(driver), timeout_s=420, max_steps=1)
        if goals.get(k, draft["goal_id"])["status"] != "DONE":      # (a goal the child already finished reports no new step here)
            row.update(done=False, stopped_at="draft", steps=[s.get("outcome") or s.get("status") for s in first["steps"]])
            return row
        test = Path(isolated["canonical_root"]) / draft["test_path"]
        build = quick.approve_check(k, isolated, draft["goal_id"], draft["request"], sha256_bytes(test.read_bytes()), "user")
        checking = lambda kk: kk.one("SELECT 1 FROM aux_process WHERE project_id = ?", isolated["project_id"]) is not None      # noqa: E731
        killed = run_child(home, build["goal_id"], driver, checking if phase == "verify" else None)
        if phase == "verify":
            row["killed"] = killed
        report = resume(k, build["goal_id"], driver, row) if killed else supervisor.run_goal(k, build["goal_id"], adapters.native(driver), timeout_s=420, max_steps=3)
        row["done"] = goals.get(k, build["goal_id"])["status"] == "DONE"
        if phase == "after":      # the worker had finished everything before the kill: was any of it done again, in either step?
            row["redone"] = attempts(k) > durable_before
        row["open_runs"] = k.one("SELECT COUNT(*) FROM run WHERE status <> 'STOPPED'")[0]
        if not row["done"]:
            row["steps"] = [s.get("outcome") or s.get("status") for s in report["steps"]]
            return row
        # meanwhile the user went on working on their branch
        if control == "conflict":
            changed = git(Path(isolated["canonical_root"]), "status", "--porcelain").split("\n")[0][3:].strip()
            (origin / changed).write_text("# the user rewrote this file meanwhile\n")
        else:
            (origin / "USERS_NOTE.md").write_text("an unrelated commit by the user\n")
        git(origin, "add", "-A"); git(origin, "commit", "-q", "-m", "the user's own commit")
        users_commit, users_tree = git(origin, "rev-parse", "HEAD").strip(), git(origin, "rev-parse", "HEAD^{tree}").strip()
        if control == "discard":
            gitx.discard(k, build["goal_id"], "user")
            row.update(discarded=True, users_repo_untouched=git(origin, "rev-parse", "HEAD").strip() == users_commit
                       and not git(origin, "status", "--porcelain").strip() and not git(origin, "branch", "--list", "lupus/*").strip())
            return row
        try:
            got = gitx.accept(k, build["goal_id"], "user")
            row.update(accepted=got["accepted"], combined="combined_with" in got)
        except LupusError as exc:
            row.update(accepted=False, refused=exc.code)
        row["users_commit_kept"] = users_commit in git(origin, "rev-list", "HEAD").split()
        row["stray_changes"] = git(origin, "status", "--porcelain").split()
        if control == "conflict":
            row["nothing_changed_on_refusal"] = git(origin, "rev-parse", "HEAD^{tree}").strip() == users_tree
            return row
        row["hidden_tests_pass"] = score(clone, inst, origin)[0]
        return row
    except LupusError as exc:
        row["error"] = f"{exc.code}: {exc.detail[:160]}"
        return row
    finally:
        row["seconds"] = round(time.monotonic() - started, 1)
        row["model_calls"] = attempts(k)
        k.close()


def main(work: Path, instances_path: str, out_path: str, driver: str, commits: list[str], phases: list[str]) -> None:
    instances = [i for i in json.loads(Path(instances_path).read_text()) if i["commit"][:8] in commits]
    base = Path(tempfile.mkdtemp(prefix="lupus-ops-")).resolve()
    k = Kernel.init(base / "template")
    probe.run(k, live=True)
    k.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    k.close()
    rows = []
    for inst in instances:
        for phase in phases:
            rows.append(one(base, base / "template", work / "clones" / inst["repo"], inst, driver, phase))
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
            Path(out_path).write_text(json.dumps({"driver": driver, "date": time.strftime("%Y-%m-%d"), "rows": rows}, ensure_ascii=False, indent=1) + "\n")
    for control in ("conflict", "discard"):
        rows.append(one(base, base / "template", work / "clones" / instances[-1]["repo"], instances[-1], driver, "none", control))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    Path(out_path).write_text(json.dumps({"driver": driver, "date": time.strftime("%Y-%m-%d"), "rows": rows}, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5].split(","),
         sys.argv[6].split(",") if len(sys.argv) > 6 else ["none", "worker", "after", "verify"])
