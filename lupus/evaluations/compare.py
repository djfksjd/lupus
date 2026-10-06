"""Does Lupus beat the plain CLI? Same tasks, same verifiers, fresh directory per run.

Conditions
  A0  the CLI exactly as the user has it configured (plugins, hooks, MCP, skills, own model)
  A   the CLI with Lupus's launch flags but no Lupus (settings/plugins/MCP switched off)
  C   Lupus: lean launch + task files in the prompt + single pass + external verification
  CT  C with the lighter setting first, default only if verification fails

Part 1: three one-shot tasks.  Part 2: a four-step project, uninterrupted and interrupted
after step 2 with the other AI taking over.

`list_cost_usd` is Claude Code's own list-price estimate. It is not what a subscription is
charged; it is used only as one number that weighs models and cache classes together.
Usage: PYTHONPATH=src python3 evaluations/compare.py simple|project out.json [repeats]
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pilot import CAPS, PY, TASKS, cmd, fresh   # noqa: E402

from lupus import adapters, goals, probe, projects, supervisor, verify   # noqa: E402
from lupus.kernel import Kernel   # noqa: E402


class Recording:
    """Mixin: keep every call's parsed result so totals can be reported per condition."""

    def parse(self, exit_code, stdout, stderr):
        result = super().parse(exit_code, stdout, stderr)
        self.log.append(result)
        return result


class Claude(Recording, adapters.ClaudeAdapter):
    pass


class Codex(Recording, adapters.CodexAdapter):
    pass


class ClaudeAsConfigured(Recording, adapters.ClaudeAdapter):
    variant = "user-config"

    def argv(self, prompt, cwd):
        return ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
                "--no-session-persistence"]


class CodexAsConfigured(Recording, adapters.CodexAdapter):
    variant = "user-config"

    def argv(self, prompt, cwd):
        return ["codex", "exec", "--json", "--color", "never", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "workspace-write", "-C", str(cwd), prompt]


def make(kind: str, driver: str, log: list) -> list[adapters.Adapter]:
    """Adapters for a condition, all recording into `log`. Returns the tier list."""
    claude = driver == "native_claude"
    if kind == "A0":
        tiers = [ClaudeAsConfigured() if claude else CodexAsConfigured()]
    elif kind == "CT":
        tiers = [Claude("haiku"), Claude()] if claude else [Codex(effort="low"), Codex()]
    else:
        tiers = [Claude() if claude else Codex()]
    for adapter in tiers:
        adapter.log = log
    return tiers


def totals(log: list, started: float, passed: bool, **extra) -> dict:
    get = lambda key: sum((r.usage or {}).get(key, 0) for r in log)
    costs = [r.raw.get("estimated_list_cost_usd") for r in log if r.raw.get("estimated_list_cost_usd") is not None]
    return {
        "passed": passed, "calls": len(log), "new_input": get("tokens_in"), "cached_input": get("tokens_cached"),
        "output": get("tokens_out"), "turns": get("calls"), "seconds": round(time.monotonic() - started, 1),
        "list_cost_usd": round(sum(costs), 4) if costs else None,
        "models": sorted({m for r in log for m in r.raw.get("models", [])}),
        "errors": [r.error_class for r in log if r.error_class], **extra,
    }


def plain_prompt(text: str, criteria: list[dict]) -> str:
    return "\n".join([text, "완료 조건:", *[f"- {c['text']}" for c in criteria], "현재 디렉터리 안의 파일만 읽고 수정하라."])


def all_pass(criteria: list[dict], root: Path) -> bool:
    return all(verify.run(c["verifier"], root)[0] == "PASS" for c in criteria)


def direct(kind: str, driver: str, prompt: str, criteria: list[dict], root: Path) -> dict:
    log: list = []
    started = time.monotonic()
    adapters.execute(make(kind, driver, log)[0], prompt, root, lambda pid: None, timeout_s=420)
    return totals(log, started, all_pass(criteria, root), human_prompt_chars=len(prompt))


def lupus_goal(k: Kernel, root: Path, objective: str, tasks: list[dict], criteria: list[dict]) -> str:
    project = projects.register(k, root, root.name, ["anthropic", "openai"])
    projects.allow_unconfined_reads(k, project["project_id"], "user")     # throw-away benchmark directory
    goal = goals.submit(k, project["project_id"], objective, criteria, CAPS)
    previous: list[str] = []
    for task in tasks:
        row = goals.add_task(k, goal["goal_id"], task["title"], task["prompt"], task["criteria"], previous)
        previous = [row["task_id"]]
    return goal["goal_id"]


def lupus(k: Kernel, kind: str, driver: str, goal_id: str, log: list, **kw) -> dict:
    tiers = make(kind, driver, log)
    return supervisor.run_goal(k, goal_id, tiers[-1], timeout_s=420, tiers=tiers if len(tiers) > 1 else None, **kw)


# ---------------------------------------------------------------- part 1: one-shot tasks

def simple(k: Kernel, base: Path, repeats: int) -> list[dict]:
    rows = []
    for driver in ("native_claude", "native_codex"):
        for name, task in TASKS.items():
            for kind, n in (("A0", 1), ("A", repeats), ("C", repeats), ("CT", repeats)):
                for i in range(n):
                    root = fresh(base, f"{kind}-{driver}-{name}-{i}", task["files"])
                    if kind in ("A0", "A"):
                        row = direct(kind, driver, plain_prompt(task["prompt"], task["criteria"]), task["criteria"], root)
                    else:
                        log: list = []
                        started = time.monotonic()
                        goal_id = lupus_goal(k, root, task["objective"], [
                            {"title": task["objective"], "prompt": task["prompt"], "criteria": ["c0"]}], task["criteria"])
                        report = lupus(k, kind, driver, goal_id, log)
                        row = totals(log, started, report["done"], attempts=report["budget"]["attempts"]["used"],
                                     variants=[s.get("variant") for s in report["steps"]])
                    rows.append({"driver": driver, "task": name, "condition": kind, "run": i, **row})
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


# ---------------------------------------------------------------- part 2: a four-step project

TEST_VALIDATORS = '''import unittest
from validators import validate_email, normalize_phone

class T(unittest.TestCase):
    def test_email(self):
        self.assertTrue(validate_email("kim@example.com"))
        for bad in ("kim@", "@example.com", "kim example.com", "kim@example", ""):
            self.assertFalse(validate_email(bad), bad)
    def test_phone(self):
        for raw in ("01012345678", "010-1234-5678", "010 1234 5678", "+82 10-1234-5678"):
            self.assertEqual(normalize_phone(raw), "010-1234-5678", raw)
        for bad in ("0101234", "02-123-4567", "abc"):
            with self.assertRaises(ValueError): normalize_phone(bad)

if __name__ == "__main__":
    unittest.main()
'''
TEST_STORAGE = '''import os, tempfile, unittest
from storage import InquiryStore

class T(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "db.jsonl")
    def test_add_and_reload(self):
        s = InquiryStore(self.path)
        self.assertEqual(s.add({"email": "a@b.co", "message": "hi"}), 1)
        self.assertEqual(s.add({"email": "c@d.co", "message": "yo"}), 2)
        again = InquiryStore(self.path)
        self.assertEqual([r["id"] for r in again.all()], [1, 2])
        self.assertEqual(again.add({"email": "e@f.co", "message": "x"}), 3)
    def test_duplicate_rejected(self):
        s = InquiryStore(self.path)
        s.add({"email": "a@b.co", "message": "hi"})
        with self.assertRaises(ValueError): s.add({"email": "a@b.co", "message": "hi"})
        self.assertEqual(len(s.all()), 1)

if __name__ == "__main__":
    unittest.main()
'''
TEST_CLI = '''import json, os, subprocess, sys, tempfile, unittest

class T(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "cli.py", *args], capture_output=True, text=True)
    def test_add_list(self):
        db = os.path.join(tempfile.mkdtemp(), "db.jsonl")
        r = self.run_cli("add", "--email", "kim@example.com", "--phone", "01012345678", "--message", "문의", "--db", db)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"), r.stderr)
        r = self.run_cli("list", "--db", db)
        rows = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
        self.assertEqual(rows[0]["phone"], "010-1234-5678")
        self.assertEqual(rows[0]["email"], "kim@example.com")
    def test_invalid_email_exit_2(self):
        db = os.path.join(tempfile.mkdtemp(), "db.jsonl")
        r = self.run_cli("add", "--email", "nope", "--phone", "01012345678", "--message", "x", "--db", db)
        self.assertEqual(r.returncode, 2)

if __name__ == "__main__":
    unittest.main()
'''
unit = lambda cid, module, paths: cmd(cid, f"python3 -m unittest {module} 가 통과한다", [PY, "-m", "unittest", "-q", module], paths)
contains = lambda cid, path, text: {"id": cid, "text": f"{path} 에 '{text}' 사용 예가 있다",
                                    "verifier": {"kind": "file_contains", "path": path, "text": text}}
PROJECT_FILES = {"test_validators.py": TEST_VALIDATORS, "test_storage.py": TEST_STORAGE, "test_cli.py": TEST_CLI}
PROJECT_CRITERIA = [
    unit("c1", "test_validators", ["validators.py", "test_validators.py"]),
    unit("c2", "test_storage", ["storage.py", "test_storage.py"]),
    unit("c3", "test_cli", ["cli.py", "test_cli.py", "validators.py", "storage.py"]),
    contains("c4", "README.md", "python3 cli.py add"), contains("c5", "README.md", "python3 cli.py list"),
]
STEPS = [
    {"title": "1단계 검증 함수", "criteria": ["c1"],
     "prompt": "validators.py 에 validate_email(s)->bool 과 normalize_phone(s)->str 을 구현하라. normalize_phone 은 한국 휴대전화 "
               "번호를 010-1234-5678 형식으로 돌려주고(공백·하이픈·+82 표기 허용) 그 밖에는 ValueError 를 낸다. 테스트 파일은 수정하지 마라."},
    {"title": "2단계 저장소", "criteria": ["c2"],
     "prompt": "storage.py 에 InquiryStore(path) 를 구현하라. add(dict)->int 는 1부터 증가하는 id 를 붙여 JSON Lines 파일에 추가하고, "
               "email 과 message 가 모두 같은 문의가 이미 있으면 ValueError 를 낸다. all()->list 는 저장된 문의를 순서대로 돌려준다. "
               "테스트 파일은 수정하지 마라."},
    {"title": "3단계 명령줄", "criteria": ["c3"],
     "prompt": "cli.py 를 구현하라. 'add --email --phone --message --db' 는 validators 로 검증·정규화한 뒤 storage 에 저장하고 id 를 "
               "출력한다. 이메일이 잘못되면 종료 코드 2. 'list --db' 는 문의를 한 줄에 JSON 하나씩 출력한다. 기존 validators.py, "
               "storage.py 를 사용하고 테스트 파일은 수정하지 마라."},
    {"title": "4단계 사용 문서", "criteria": ["c4", "c5"],
     "prompt": "README.md 에 이 문의 접수 도구의 사용법을 쓰라. 'python3 cli.py add' 와 'python3 cli.py list' 의 실제 사용 예를 포함한다."},
]
OTHER = {"native_claude": "native_codex", "native_codex": "native_claude"}


def steps_text(steps: list[dict]) -> str:
    return "\n".join(f"[{s['title']}] {s['prompt']}" for s in steps)


def crit(ids: list[str]) -> list[dict]:
    return [c for c in PROJECT_CRITERIA if c["id"] in ids]


def project(k: Kernel, base: Path) -> list[dict]:
    rows = []
    first = "native_claude"
    # uninterrupted
    root = fresh(base, "R1-A0", PROJECT_FILES)
    rows.append({"scenario": "uninterrupted", "condition": "A0", **direct(
        "A0", first, plain_prompt("문의 접수 도구를 다음 네 단계로 완성하라.\n" + steps_text(STEPS), PROJECT_CRITERIA),
        PROJECT_CRITERIA, root)})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    for kind in ("C", "CT"):
        root = fresh(base, f"R1-{kind}", PROJECT_FILES)
        log: list = []
        started = time.monotonic()
        report = lupus(k, kind, first, lupus_goal(k, root, "문의 접수 도구 완성", STEPS, PROJECT_CRITERIA), log)
        rows.append({"scenario": "uninterrupted", "condition": kind, **totals(
            log, started, report["done"], attempts=report["budget"]["attempts"]["used"],
            variants=[s.get("variant") for s in report["steps"]], human_prompt_chars=0)})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    # interrupted after step 2; the other AI continues
    root = fresh(base, "R2-A0", PROJECT_FILES)
    part1 = direct("A0", first, plain_prompt("문의 접수 도구를 만드는 중이다. 다음 두 단계만 수행하라.\n" + steps_text(STEPS[:2]),
                                             crit(["c1", "c2"])), crit(["c1", "c2"]), root)
    # What a person has to type to the second AI: the rest of the job plus what is already done.
    handover = plain_prompt("문의 접수 도구를 만들던 작업을 이어받아라. validators.py 와 storage.py 는 이미 구현되어 테스트를 통과하니 "
                            "수정하지 마라. 남은 두 단계를 수행하라.\n" + steps_text(STEPS[2:]), crit(["c3", "c4", "c5"]))
    part2 = direct("A0", OTHER[first], handover, PROJECT_CRITERIA, root)
    rows.append({"scenario": "interrupted_then_other_ai", "condition": "A0", "first_ai": part1, "second_ai": part2,
                 "passed": part2["passed"], "human_prompt_chars": part1["human_prompt_chars"] + part2["human_prompt_chars"]})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    root = fresh(base, "R2-C", PROJECT_FILES)
    goal_id = lupus_goal(k, root, "문의 접수 도구 완성", STEPS, PROJECT_CRITERIA)
    log1: list = []
    started = time.monotonic()
    lupus(k, "C", first, goal_id, log1, max_steps=2)
    part1 = totals(log1, started, all_pass(crit(["c1", "c2"]), root))
    log2: list = []
    started = time.monotonic()
    report = lupus(k, "C", OTHER[first], goal_id, log2)
    part2 = totals(log2, started, report["done"])
    redone = k.one("SELECT COUNT(*) FROM attempt a JOIN task t ON t.task_id = a.task_id WHERE a.goal_id = ? "
                   "AND t.title IN ('1단계 검증 함수', '2단계 저장소')", goal_id)[0]
    rows.append({"scenario": "interrupted_then_other_ai", "condition": "C", "first_ai": part1, "second_ai": part2,
                 "passed": report["done"], "human_prompt_chars": 0, "attempts_on_finished_steps": redone,
                 "handoffs": k.one("SELECT COUNT(*) FROM handoff WHERE goal_id = ? AND status = 'ACCEPTED'", goal_id)[0]})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


def main(part: str, out_path: str, repeats: int = 2) -> None:
    with tempfile.TemporaryDirectory(prefix="lupus-compare-") as tmp:
        base = Path(tmp).resolve()
        k = Kernel.init(base / "home")
        versions = {name: a["version"] for name, a in probe.run(k, live=True)["adapters"].items()}
        rows = simple(k, base, repeats) if part == "simple" else project(k, base)
        k.close()
    Path(out_path).write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "part": part, "cli_versions": versions,
                                          "repeats": repeats, "rows": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
