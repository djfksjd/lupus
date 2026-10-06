"""Small pilot: the same task, model, CLI flags and verifier, with and without Lupus (§11.3, §23).

    A  baseline   one direct headless CLI call with the task text a user would type
    C  lupus      the same task as a Lupus goal (attempt accounting, checkpoints, verification)
    M  memory     a task that needs a project convention not present in any file:
                  Lupus with memory off vs. with the convention stored as a user node

Every run starts from an identical fresh directory. Results are single runs: they show what
happened once, not a rate. Usage: PYTHONPATH=src python3 evaluations/pilot.py out.json
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from lupus import adapters, goals, memory, probe, projects, supervisor, usage, verify
from lupus.kernel import Kernel

PY = sys.executable
CAPS = {"calls": 80, "attempts": 4, "active_ms": 1_800_000, "tokens": 3_000_000}

TEST_TEXTUTIL = '''import unittest
from textutil import slugify

class T(unittest.TestCase):
    def test_basic(self): self.assertEqual(slugify("Hello, World!"), "hello-world")
    def test_collapse(self): self.assertEqual(slugify("  a  --  b__c  "), "a-b-c")
    def test_korean_kept(self): self.assertEqual(slugify("문의 페이지 v2"), "문의-페이지-v2")
    def test_empty(self): self.assertEqual(slugify("!!!"), "")

if __name__ == "__main__":
    unittest.main()
'''
INVENTORY = '''def paginate(items, page, size):
    """Return the items of 1-based `page`."""
    start = page * size
    return items[start:start + size]


def total_value(stock):
    """Sum of qty * price, rounded to 2 decimals."""
    return round(sum(q * p for q, p in stock), 0)
'''
TEST_INVENTORY = '''import unittest
from inventory import paginate, total_value

class T(unittest.TestCase):
    def test_first_page(self): self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])
    def test_last_page(self): self.assertEqual(paginate(list(range(10)), 4, 3), [9])
    def test_total(self): self.assertEqual(total_value([(3, 1.25), (2, 0.4)]), 4.55)

if __name__ == "__main__":
    unittest.main()
'''
SALES = "region,item,qty,price\nseoul,pen,10,1.5\nbusan,pen,4,1.5\nseoul,book,2,12.0\nbusan,cup,5,3.2\njeju,book,1,12.0\n"
CHECK_SUMMARY = (
    "import json,sys; d=json.load(open('summary.json')); "
    "ok = abs(d['total']-73.0)<1e-6 and d['by_region']=={'seoul':39.0,'busan':22.0,'jeju':12.0} and d['top_item']=='book'; "
    "sys.exit(0 if ok else 1)"
)
CHECK_RELEASE = (
    "import sys; l=open('RELEASE.md',encoding='utf-8').read().splitlines(); "
    "sys.exit(0 if l and l[0].strip()=='# 변경 기록' and any('2026.10.05' in x for x in l) "
    "and not any('2026-10-05' in x for x in l) else 1)"
)


def cmd(cid, text, code_or_argv, paths):
    argv = code_or_argv if isinstance(code_or_argv, list) else [PY, "-c", code_or_argv]
    return {"id": cid, "text": text, "verifier": {"kind": "command", "argv": argv, "paths": paths, "timeout_s": 60}}


TASKS = {
    "slugify": {
        "files": {"test_textutil.py": TEST_TEXTUTIL},
        "objective": "문자열을 URL 조각으로 바꾸는 slugify 함수 구현",
        "prompt": "textutil.py 에 slugify(s) 를 구현하라. 소문자로 바꾸고, 글자·숫자가 아닌 문자의 연속은 하이픈 하나로 "
                  "바꾸고, 앞뒤 하이픈은 제거한다. 한글은 그대로 둔다. test_textutil.py 는 수정하지 마라.",
        "criteria": [cmd("c0", "python3 -m unittest test_textutil 이 통과한다", [PY, "-m", "unittest", "-q", "test_textutil"],
                         ["textutil.py", "test_textutil.py"])],
    },
    "bugfix": {
        "files": {"inventory.py": INVENTORY, "test_inventory.py": TEST_INVENTORY},
        "objective": "inventory.py 의 버그 수정",
        "prompt": "test_inventory.py 가 모두 통과하도록 inventory.py 의 버그를 고쳐라. 테스트 파일은 수정하지 마라.",
        "criteria": [cmd("c0", "python3 -m unittest test_inventory 가 통과한다", [PY, "-m", "unittest", "-q", "test_inventory"],
                         ["inventory.py", "test_inventory.py"])],
    },
    "report": {
        "files": {"sales.csv": SALES},
        "objective": "판매 CSV 요약",
        "prompt": "sales.csv 를 읽어 summary.json 을 만들어라. 키: total(전체 매출=qty*price 합), by_region(지역별 매출 객체), "
                  "top_item(매출이 가장 큰 품목 이름). 숫자는 소수로 쓴다.",
        "criteria": [cmd("c0", "summary.json 의 total, by_region, top_item 값이 sales.csv 와 일치한다", CHECK_SUMMARY,
                         ["summary.json", "sales.csv"])],
    },
}
RELEASE = {
    "files": {"CHANGES.txt": "- 문의 저장 오류 수정\n- 모바일 레이아웃 개선\n"},
    "objective": "릴리스 노트 작성",
    "prompt": "CHANGES.txt 의 내용으로 오늘(2026년 10월 5일) 릴리스 노트 RELEASE.md 를 작성하라. 이 프로젝트의 릴리스 노트 규칙을 따르라.",
    "criteria": [cmd("c0", "RELEASE.md 가 이 프로젝트의 릴리스 노트 규칙을 지킨다", CHECK_RELEASE, ["RELEASE.md"])],
}
CONVENTION = ("릴리스 노트 규칙", "릴리스 노트 RELEASE.md 의 첫 줄은 '# 변경 기록' 이고, 날짜는 2026.10.05 처럼 점으로 구분해 쓴다. "
              "하이픈 날짜는 쓰지 않는다.")


def fresh(base: Path, name: str, files: dict[str, str]) -> Path:
    root = base / name
    root.mkdir(parents=True)
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


def verdict(task: dict, root: Path) -> bool:
    return all(verify.run(c["verifier"], root)[0] == "PASS" for c in task["criteria"])


def baseline(driver: str, task: dict, root: Path) -> dict:
    prompt = "\n".join([task["prompt"], "완료 조건:", *[f"- {c['text']}" for c in task["criteria"]],
                        "현재 디렉터리 안의 파일만 읽고 수정하라."])
    result = adapters.execute(adapters.native(driver), prompt, root, lambda pid: None, timeout_s=300)
    u = result.usage or {}
    return {"passed": verdict(task, root), "attempts": 1, "error": result.error_class, "duration_ms": result.duration_ms,
            "tokens_in": u.get("tokens_in"), "tokens_cached": u.get("tokens_cached"), "tokens_out": u.get("tokens_out"),
            "turns": u.get("calls"), "prompt_chars": len(prompt)}


def with_lupus(k: Kernel, driver: str, task: dict, root: Path, note: tuple[str, str] | None = None) -> dict:
    project = projects.register(k, root, root.name, ["anthropic", "openai"])
    projects.allow_unconfined_reads(k, project["project_id"], "user")     # throw-away benchmark directory
    if note:
        memory.add(k, project_id=project["project_id"], kind="decision", title=note[0], body=note[1],
                   origin="user", actor="user")
    goal = goals.submit(k, project["project_id"], task["objective"], task["criteria"], CAPS)
    goals.add_task(k, goal["goal_id"], task["objective"], task["prompt"], [c["id"] for c in task["criteria"]])
    started = time.monotonic()
    report = supervisor.run_goal(k, goal["goal_id"], adapters.native(driver), timeout_s=300)
    t = usage.totals(k, goal["goal_id"])["totals"]
    return {"passed": report["done"], "goal_status": report["goal_status"],
            "attempts": report["budget"]["attempts"]["used"], "duration_ms": int((time.monotonic() - started) * 1000),
            "tokens_in": t["tokens_in"], "tokens_cached": t["tokens_cached"], "tokens_out": t["tokens_out"],
            "turns": t["calls"], "memory": report["memory"],
            "error": next((s.get("error_class") for s in report["steps"] if s.get("error_class")), None)}


def main(out_path: str) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="lupus-pilot-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "home")
        caps = probe.run(k, live=True)
        versions = {name: a["version"] for name, a in caps["adapters"].items()}
        for driver in ("native_claude", "native_codex"):
            for name, task in TASKS.items():
                rows.append({"driver": driver, "task": name, "condition": "A_baseline",
                             **baseline(driver, task, fresh(base, f"A-{driver}-{name}", task["files"]))})
                rows.append({"driver": driver, "task": name, "condition": "C_lupus",
                             **with_lupus(k, driver, task, fresh(base, f"C-{driver}-{name}", task["files"]))})
                print(json.dumps(rows[-2:], ensure_ascii=False), flush=True)
            k.conn.execute("UPDATE meta SET value = json_set(value, '$.memory_recall_tokens', 0) WHERE key = 'policy'")
            rows.append({"driver": driver, "task": "release", "condition": "M_memory_off",
                         **with_lupus(k, driver, RELEASE, fresh(base, f"Moff-{driver}", RELEASE["files"]))})
            k.conn.execute("UPDATE meta SET value = json_set(value, '$.memory_recall_tokens', 1200) WHERE key = 'policy'")
            rows.append({"driver": driver, "task": "release", "condition": "M_memory_on",
                         **with_lupus(k, driver, RELEASE, fresh(base, f"Mon-{driver}", RELEASE["files"]), CONVENTION)})
            print(json.dumps(rows[-2:], ensure_ascii=False), flush=True)
        k.close()
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "cli_versions": versions,
                                          "runs_per_cell": 1, "rows": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
