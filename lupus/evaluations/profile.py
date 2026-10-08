"""Offline wall profile: profile.py <home> [goal-id]. Reads SQLite without changing it.

The envelope of recorded operations includes gaps between calls (including user wait).
Stages measure active operations only; gaps and missing instrumentation remain unattributed.
Old logs cannot be retroactively divided into these stages.
The 95% attribution target is reported for real runs; scheduler gaps in short
unit-test runs are not an accounting failure.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from lupus.timing import STAGES


def breakdown(operations: list[dict], total_s: float | None = None) -> dict:
    stages = {stage: sum(op["stages_ns"].get(stage, 0) for op in operations) / 1e9 for stage in STAGES}
    envelope = (max(op["end_ns"] for op in operations) - min(op["start_ns"] for op in operations)) / 1e9 if operations else 0.0
    total = envelope if total_s is None else total_s
    attributed = sum(stages.values())
    if attributed > total + 1e-6:
        raise ValueError("overlapping operations or total shorter than recorded stages")
    unknown = max(0.0, total - attributed)
    return {"total_seconds": total, "stages": {name: {"seconds": seconds, "share": seconds / total if total else 0.0}
                                             for name, seconds in stages.items()},
            "unattributed_seconds": unknown, "unattributed_share": unknown / total if total else 1.0,
            "target_met": bool(total and unknown / total <= 0.05), "operations": len(operations)}


def read(home: Path, goal_id: str | None = None) -> list[dict]:
    path = home / "runtime" / "lupus.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        goals = conn.execute("SELECT goal_id FROM goal" + (" WHERE goal_id = ?" if goal_id else ""),
                             (goal_id,) if goal_id else ()).fetchall()
        if goal_id and not goals:
            raise ValueError("goal not found: " + goal_id)
        out = []
        for goal in goals:
            gid = goal["goal_id"]
            ops = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM event WHERE type = 'timing.operation' AND aggregate = 'goal' AND aggregate_id = ? ORDER BY seq", (gid,))]
            if ops:
                result = breakdown(ops)
            else:
                row = conn.execute("SELECT MIN(ts), MAX(ts) FROM event WHERE aggregate = 'goal' AND aggregate_id = ?", (gid,)).fetchone()
                result = breakdown([], ((row[1] or 0) - (row[0] or 0)) / 1000)
            out.append({"goal_id": gid, **result, "basis": "recorded operation envelope" if ops else "legacy goal event envelope; no stage instrumentation"})
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", type=Path)
    parser.add_argument("goal_id", nargs="?")
    args = parser.parse_args()
    print(json.dumps(read(args.home, args.goal_id), indent=2))
