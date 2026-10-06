"""Harder tasks with hidden holdout tests: does the cheaper path cost correctness?

For each task the worker sees the task text and a VISIBLE test file. A stricter HOLDOUT test
file is never shown; it is run afterwards on a copy of the result. A run that passes the visible
tests but fails the holdout is a FALSE PASS: the thing this benchmark exists to count.

Conditions (order shuffled per task and repetition, fixed seed)
  A    CLI with Lupus's launch flags, plain prompt, free to self-check as it likes
  A+   same CLI, no supervisor, but the Lupus prompt technique (task files inlined, single pass)
  C    Lupus: lean first attempt, frozen tests, external verification, careful retry
Human input is counted for every condition: the prompt typed (A, A+) or the goal definition
(C). Usage: PYTHONPATH=src python3 evaluations/bench.py <native_claude|native_codex> out.json [reps]
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import Recording, lupus_goal, totals   # noqa: E402
from pilot import CAPS, PY, fresh   # noqa: E402

from lupus import adapters, probe, supervisor, verify   # noqa: E402
from lupus.kernel import Kernel   # noqa: E402

TASKS = {
    "intervals": {
        "module": "intervals",
        "prompt": "intervals.py 를 구현하라. merge_intervals(items): (start, end) 튜플 목록을 받아 겹치거나 맞닿은 구간을 합쳐 "
                  "시작 순으로 정렬한 새 목록을 돌려준다(입력은 바꾸지 않는다, 빈 입력은 빈 목록). "
                  "free_slots(busy, day_start, day_end, min_len): [day_start, day_end) 안에서 busy 구간을 뺀 빈 구간 중 길이가 "
                  "min_len 이상인 것만 시작 순으로 돌려준다. busy 가 범위를 벗어나면 범위 안쪽만 고려한다. "
                  "test_intervals.py 는 수정하지 마라.",
        "visible": '''import unittest
from intervals import merge_intervals, free_slots

class T(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(merge_intervals([(5, 8), (1, 3), (2, 4)]), [(1, 4), (5, 8)])
    def test_free(self):
        self.assertEqual(free_slots([(10, 12)], 9, 17, 1), [(9, 10), (12, 17)])
''',
        "holdout": '''import unittest
from intervals import merge_intervals, free_slots

class H(unittest.TestCase):
    def test_touching_and_nested(self):
        self.assertEqual(merge_intervals([(1, 3), (3, 5), (10, 20), (12, 13)]), [(1, 5), (10, 20)])
    def test_empty_and_no_mutation(self):
        self.assertEqual(merge_intervals([]), [])
        items = [(4, 6), (1, 2)]
        merge_intervals(items)
        self.assertEqual(items, [(4, 6), (1, 2)])
    def test_min_len_and_clipping(self):
        self.assertEqual(free_slots([(8, 10), (10, 11), (16, 20)], 9, 17, 2), [(11, 16)])
        self.assertEqual(free_slots([], 9, 17, 1), [(9, 17)])
        self.assertEqual(free_slots([(0, 30)], 9, 17, 1), [])
    def test_unsorted_overlapping_busy(self):
        self.assertEqual(free_slots([(14, 15), (9, 11), (10, 12)], 9, 17, 1), [(12, 14), (15, 17)])
''',
    },
    "duration": {
        "module": "duration",
        "prompt": "duration.py 를 구현하라. parse_duration(text)->int 는 '1h30m' 같은 문자열을 초로 바꾼다. 단위는 d,h,m,s 이고 "
                  "반드시 d>h>m>s 순서로 한 번씩만 나올 수 있으며 부분 사이의 공백은 허용한다('1h 30m'). 값은 음이 아닌 정수다. "
                  "빈 문자열, 모르는 단위, 순서 위반, 단위 중복, 음수, 소수, 단위 없는 숫자는 ValueError. "
                  "format_duration(seconds)->str 은 그 반대이며 0인 단위는 생략하고 0은 '0s', 음수는 ValueError. "
                  "test_duration.py 는 수정하지 마라.",
        "visible": '''import unittest
from duration import parse_duration, format_duration

class T(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("45s"), 45)
    def test_format(self):
        self.assertEqual(format_duration(5400), "1h30m")
    def test_invalid(self):
        with self.assertRaises(ValueError): parse_duration("1x")
''',
        "holdout": '''import unittest
from duration import parse_duration, format_duration

class H(unittest.TestCase):
    def test_spaces_days_zero(self):
        self.assertEqual(parse_duration("2d 3h 4m 5s"), 2 * 86400 + 3 * 3600 + 245)
        self.assertEqual(parse_duration("0s"), 0)
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(90061), "1d1h1m1s")
        self.assertEqual(format_duration(86400), "1d")
    def test_rejects(self):
        for bad in ("", "  ", "1m1h", "1h1h", "-1h", "1.5h", "90", "h", "1h30", "1H"):
            with self.assertRaises(ValueError, msg=repr(bad)): parse_duration(bad)
        with self.assertRaises(ValueError): format_duration(-1)
    def test_roundtrip(self):
        for n in (1, 59, 60, 3599, 3600, 86399, 86400, 123456):
            self.assertEqual(parse_duration(format_duration(n)), n)
''',
    },
    "ttlcache": {
        "module": "ttlcache",
        "prompt": "ttlcache.py 에 TTLCache(capacity, ttl, clock) 를 구현하라. clock 은 현재 시각(초)을 돌려주는 함수다. "
                  "capacity 가 1보다 작으면 ValueError. set(key, value) 는 값을 저장하고 만료 시각(now+ttl)과 최근 사용 순서를 갱신한다. "
                  "get(key) 는 없거나 만료됐으면 None(만료된 항목은 제거), 있으면 값을 돌려주고 최근 사용 순서만 갱신한다(만료 시각은 그대로). "
                  "새 key 를 넣을 때 가득 찼으면 먼저 만료된 항목을 모두 지우고, 그래도 가득 찼으면 가장 오래 사용하지 않은 항목 하나를 지운다. "
                  "len(cache) 는 만료되지 않은 항목 수다. now >= 만료 시각이면 만료다. test_ttlcache.py 는 수정하지 마라.",
        "visible": '''import unittest
from ttlcache import TTLCache

class T(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.c = TTLCache(2, 10, lambda: self.now)
    def test_set_get(self):
        self.c.set("a", 1)
        self.assertEqual(self.c.get("a"), 1)
        self.assertIsNone(self.c.get("zz"))
    def test_expiry(self):
        self.c.set("a", 1)
        self.now = 10
        self.assertIsNone(self.c.get("a"))
    def test_capacity(self):
        self.c.set("a", 1); self.c.set("b", 2); self.c.set("c", 3)
        self.assertIsNone(self.c.get("a"))
''',
        "holdout": '''import unittest
from ttlcache import TTLCache

class H(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.c = TTLCache(2, 10, lambda: self.now)
    def test_get_refreshes_recency_not_ttl(self):
        self.c.set("a", 1); self.c.set("b", 2)
        self.now = 5
        self.assertEqual(self.c.get("a"), 1)
        self.c.set("c", 3)                      # b is least recently used
        self.assertIsNone(self.c.get("b"))
        self.assertEqual(self.c.get("a"), 1)
        self.now = 10                           # a's ttl was NOT extended by get
        self.assertIsNone(self.c.get("a"))
        self.assertEqual(self.c.get("c"), 3)
    def test_update_existing_does_not_evict(self):
        self.c.set("a", 1); self.c.set("b", 2); self.c.set("a", 9)
        self.assertEqual((self.c.get("a"), self.c.get("b")), (9, 2))
    def test_expired_purged_before_lru_eviction(self):
        self.c.set("a", 1)
        self.now = 6
        self.c.set("b", 2)
        self.now = 10                           # a expired, b alive
        self.c.set("c", 3)                      # must drop expired a, keep b
        self.assertEqual((self.c.get("b"), self.c.get("c")), (2, 3))
    def test_len_and_capacity_check(self):
        self.c.set("a", 1)
        self.now = 3
        self.c.set("b", 2)
        self.now = 10
        self.assertEqual(len(self.c), 1)
        with self.assertRaises(ValueError): TTLCache(0, 10, lambda: 0)
''',
    },
}


class Claude(Recording, adapters.ClaudeAdapter):
    pass


class Codex(Recording, adapters.CodexAdapter):
    pass


def adapter(driver: str, log: list) -> adapters.Adapter:
    a = Claude() if driver == "native_claude" else Codex()
    a.log = log
    return a


def unit(cid: str, module: str, protect: bool) -> dict:
    v = {"kind": "command", "argv": [PY, "-m", "unittest", "-q", f"test_{module}"], "timeout_s": 120,
         "paths": [f"{module}.py", f"test_{module}.py"], "require_tests": "unittest"}
    if protect:
        v["protect"] = [f"test_{module}.py"]
    return {"id": cid, "text": f"python3 -m unittest test_{module} 가 통과한다", "verifier": v}


def score(task: dict, root: Path) -> dict:
    module = task["module"]
    visible = verify.run(unit("c0", module, False)["verifier"], root)[0] == "PASS"
    with tempfile.TemporaryDirectory(prefix="lupus-holdout-") as tmp:
        copy = Path(tmp) / "w"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / f"test_{module}.py").write_text(task["visible"], encoding="utf-8")   # score against the original
        (copy / f"holdout_{module}.py").write_text(task["holdout"], encoding="utf-8")
        p = subprocess.run([PY, "-B", "-m", "unittest", "-q", f"test_{module}", f"holdout_{module}"], cwd=copy,
                           capture_output=True, text=True, timeout=120)
    correct = p.returncode == 0
    return {"visible_pass": visible, "holdout_pass": correct, "false_pass": visible and not correct}


def run_cell(k: Kernel, base: Path, driver: str, name: str, task: dict, kind: str, rep: int) -> dict:
    module = task["module"]
    root = fresh(base, f"{kind}-{driver}-{name}-{rep}".replace("+", "p"), {f"test_{module}.py": task["visible"]})
    criteria = [unit("c0", module, kind == "C")]
    log: list = []
    started = time.monotonic()
    plain = "\n".join([task["prompt"], "완료 조건:", f"- {criteria[0]['text']}", "현재 디렉터리 안의 파일만 읽고 수정하라."])
    if kind == "A":
        adapters.execute(adapter(driver, log), plain, root, lambda pid: None, timeout_s=420)
        extra = {"human_chars": len(plain), "attempts": 1}
    elif kind == "A+":
        prompt = "\n".join([plain, supervisor.SINGLE_PASS, *supervisor._inline_files(k, root, [f"test_{module}.py"])])
        adapters.execute(adapter(driver, log), prompt, root, lambda pid: None, timeout_s=420)
        extra = {"human_chars": len(plain), "attempts": 1}
    else:
        spec = [{"title": name, "prompt": task["prompt"], "criteria": ["c0"]}]
        goal_id = lupus_goal(k, root, name, spec, criteria)
        report = supervisor.run_goal(k, goal_id, adapter(driver, log), timeout_s=420)
        extra = {"human_chars": len(json.dumps({"objective": name, "tasks": spec, "criteria": criteria}, ensure_ascii=False)),
                 "attempts": report["budget"]["attempts"]["used"], "lupus_done": report["done"]}
    row = totals(log, started, True, **extra)
    row.pop("passed")
    return {"driver": driver, "task": name, "condition": kind, "rep": rep, **score(task, root), **row}


def main(driver: str, out_path: str, reps: int = 2) -> None:
    rng = random.Random(20261005)
    rows = []
    with tempfile.TemporaryDirectory(prefix="lupus-bench-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "home")
        versions = {n: a["version"] for n, a in probe.run(k, live=True)["adapters"].items()}
        for name, task in TASKS.items():
            for rep in range(reps):
                order = ["A", "A+", "C"]
                rng.shuffle(order)
                for kind in order:
                    rows.append(run_cell(k, base, driver, name, task, kind, rep))
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
        k.close()
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "driver": driver, "cli_versions": versions,
                                          "reps": reps, "rows": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
