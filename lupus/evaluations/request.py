"""`lupus do` on a feature request vs. a plain CLI call, scored by a hidden holdout test.

The approval step is simulated here (the drafted test is approved automatically); in real use a
person reads it first. Usage: PYTHONPATH=src python3 evaluations/request.py out.json
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare import Recording, totals   # noqa: E402
from pilot import PY, fresh   # noqa: E402

from lupus import adapters, probe, projects, quick, supervisor   # noqa: E402
from lupus.kernel import Kernel   # noqa: E402
from lupus.util import sha256_bytes   # noqa: E402

FILES = {
    "inventory.py": "def total_value(stock):\n    \"\"\"stock: list of (qty, price).\"\"\"\n    return round(sum(q * p for q, p in stock), 2)\n",
    "test_inventory.py": "import unittest\nfrom inventory import total_value\n\nclass T(unittest.TestCase):\n"
                         "    def test_total(self): self.assertEqual(total_value([(3, 1.25), (2, 0.4)]), 4.55)\n",
}
REQUEST = ("inventory.py 에 low_stock(items, threshold) 함수를 추가해줘. items 는 (이름, 수량) 튜플 목록이고, 수량이 threshold 이하인 "
           "품목의 이름을 수량 오름차순(수량이 같으면 이름순)으로 돌려준다. threshold 가 음수면 ValueError.")
HOLDOUT = '''import unittest
from inventory import low_stock, total_value

class H(unittest.TestCase):
    def test_order_and_ties(self):
        items = [("pen", 3), ("cup", 0), ("book", 3), ("bag", 10), ("ink", 1)]
        self.assertEqual(low_stock(items, 3), ["cup", "ink", "book", "pen"])
    def test_boundary_and_empty(self):
        self.assertEqual(low_stock([("a", 5)], 5), ["a"])
        self.assertEqual(low_stock([("a", 6)], 5), [])
        self.assertEqual(low_stock([], 0), [])
    def test_negative_threshold(self):
        with self.assertRaises(ValueError): low_stock([("a", 1)], -1)
    def test_old_behaviour_kept(self):
        self.assertEqual(total_value([(3, 1.25), (2, 0.4)]), 4.55)
'''


class Claude(Recording, adapters.ClaudeAdapter):
    pass


class Codex(Recording, adapters.CodexAdapter):
    pass


def holdout(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="lupus-holdout-") as tmp:
        copy = Path(tmp) / "w"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "holdout_inventory.py").write_text(HOLDOUT, encoding="utf-8")
        return subprocess.run([PY, "-B", "-m", "unittest", "-q", "holdout_inventory"], cwd=copy, capture_output=True,
                              timeout=120).returncode == 0


def main(out_path: str) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="lupus-request-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "home")
        probe.run(k, live=True)
        for driver, cls in (("native_claude", Claude), ("native_codex", Codex)):
            # plain CLI: the request as a person would type it
            root = fresh(base, f"A-{driver}", FILES)
            log: list = []
            a = cls(); a.log = log
            started = time.monotonic()
            adapters.execute(a, REQUEST + "\n현재 디렉터리 안의 파일만 읽고 수정하라.", root, lambda pid: None, timeout_s=300)
            rows.append({"driver": driver, "condition": "A_plain_cli", "holdout_pass": holdout(root),
                         **totals(log, started, True, human_chars=len(REQUEST))})
            # lupus do: draft a red test, (simulated) approval, implement, verify
            root = fresh(base, f"D-{driver}", FILES)
            project = projects.register(k, root, root.name, ["anthropic", "openai"])
            log = []
            a = cls(); a.log = log
            started = time.monotonic()
            draft = quick.draft_check(k, project, REQUEST, "user")
            first = supervisor.run_goal(k, draft["goal_id"], a, timeout_s=300)
            row = {"driver": driver, "condition": "D_lupus_do", "check_drafted": first["done"]}
            if first["done"]:
                test = root / draft["test_path"]
                row["check_lines"] = len(test.read_text().splitlines())
                build = quick.approve_check(k, project, draft["goal_id"], draft["request"],
                                            sha256_bytes(test.read_bytes()), "user")
                second = supervisor.run_goal(k, build["goal_id"], a, timeout_s=300)
                row.update(lupus_done=second["done"], attempts=first["budget"]["attempts"]["used"] + second["budget"]["attempts"]["used"])
            rows.append({**row, "holdout_pass": holdout(root), **totals(log, started, True, human_chars=len(REQUEST))})
            print(json.dumps(rows[-2:], ensure_ascii=False), flush=True)
        k.close()
    for r in rows:
        r.pop("passed", None)
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "approval": "simulated", "rows": rows},
                                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
