"""Does the recall order matter to a real worker? (the memory changes adapted from Ruflo)

One task that can only be done right with a project convention that is in no file (the release
note task of pilot.py). The project's memory holds that convention and several notes about the
same subject that say nearly the same thing as each other and do not contain the rule. Only two
notes fit in a prompt.

    before   relevance order only                       (how recall ranked until v0.11)
    after    the current order: relevance against likeness to what is already picked (MMR)
    floor    the current order plus the coverage floor  (tried in v0.12, switched off: this is why)

The convention is written the way such a note tends to be: short, in its own words, not in the
words of the task. For each arm: which notes were shown, and whether the real CLI got the task
right on its first attempt. The store is constructed to contain repeats; this shows what
follows when it does, not how often a real store looks like this. Usage:
  PYTHONPATH=src python3 evaluations/recall.py out.json <driver>[,<driver>] [runs]     (--dry: no model call)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from lupus import adapters, goals, memory, probe, projects, supervisor
from lupus.kernel import Kernel

from pilot import CAPS, RELEASE, fresh

CONVENTION = ("문서 형식", "첫 줄 '# 변경 기록'. 날짜는 점으로 구분(2026.10.05), 하이픈 금지.")

REPEATS = [("릴리스 노트 작성 %d" % i, "릴리스 노트 RELEASE.md 는 CHANGES.txt 의 내용으로 오늘 날짜의 릴리스 노트를 작성한다. "
            "프로젝트의 릴리스 노트 규칙을 따른다. 메모 %d" % i) for i in range(1, 5)]
ARMS = {"before": (1.0, False), "after": (0.7, False), "floor": (0.7, True)}


def one(k: Kernel, base: Path, name: str, arm: str, driver: str | None) -> dict:
    memory.MMR_RELEVANCE, memory.COVERAGE_FLOOR = ARMS[arm]
    root = fresh(base, name, RELEASE["files"])
    project = projects.register(k, root, name, ["anthropic", "openai"])
    projects.allow_unconfined_reads(k, project["project_id"], "user")
    for title, body in [*REPEATS, CONVENTION]:
        memory.add(k, project_id=project["project_id"], kind="decision", title=title, body=body, origin="user", actor="user")
    goal = goals.submit(k, project["project_id"], RELEASE["objective"], RELEASE["criteria"], CAPS)
    task = goals.add_task(k, goal["goal_id"], RELEASE["objective"], RELEASE["prompt"], ["c0"])
    query = "\n".join([task["title"], task["spec"]["prompt"], RELEASE["criteria"][0]["text"]])
    ranked = [n["title"] for n in memory.diverse(memory.search(k, project["project_id"], query))][:k.policy["memory_max_nodes"]]
    row = {"arm": arm, "driver": driver, "would_show": ranked, "convention_shown": CONVENTION[0] in ranked}
    if driver:
        started = time.monotonic()
        report = supervisor.run_goal(k, goal["goal_id"], adapters.native(driver), timeout_s=300, max_steps=1)
        shown = [r["title"] for r in k.q("SELECT n.title FROM recall r JOIN node n ON n.node_id = r.node_id WHERE r.goal_id = ?", goal["goal_id"])]
        row.update(shown=shown, right_first_time=report["done"], seconds=round(time.monotonic() - started, 1))
    return row


def main(out_path: str, drivers: list[str | None], runs: int) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="lupus-recall-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "template")
        if drivers != [None]:
            probe.run(k, live=True)
        k.conn.execute("UPDATE meta SET value = json_set(value, '$.memory_max_nodes', 2) WHERE key = 'policy'")
        k.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        k.close()
        for driver in drivers:
            for run in range(runs):
                for arm in ARMS:      # alternated, so the arms see the same conditions
                    # A runtime of its own for every row: the search weights are computed over the whole
                    # store, so notes left by an earlier row would change the ranking of a later one.
                    home = base / f"home-{arm}-{driver}-{run}"
                    shutil.copytree(base / "template", home)
                    k = Kernel(home)
                    rows.append(one(k, base, f"{arm}-{driver}-{run}", arm, driver))
                    k.close()
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    summary = {f"{arm}/{driver}": {"n": len(sel), "convention_shown": sum(r["convention_shown"] for r in sel),
                                    **({"right_first_time": sum(r["right_first_time"] for r in sel)} if driver else {})}
               for arm in ARMS for driver in drivers if (sel := [r for r in rows if r["arm"] == arm and r["driver"] == driver])}
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "rows": rows, "summary": summary}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry"]
    main(args[0], [None] if dry else args[1].split(","), 1 if dry else int(args[2]) if len(args) > 2 else 1)
