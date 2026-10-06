"""`lupus write`: work whose result is a document (a plan, research notes, a report, a spec).

No test can say whether such a document is good, so "done" is assembled from three things, none
of which is the writer's own word (see judging.py): a structural check, a judge that is a
different model where one is available and must quote the document for every item it accepts,
and finally the user's approval of the exact version. The rubric the judge uses is fixed and
approved by the user BEFORE anything is written.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import budget, goals, projects, runs, service
from .kernel import Kernel
from .util import LupusError, find_secret

MAX_REQUEST = 4000
RUBRIC_CAPS = {"calls": 2, "attempts": 1, "active_ms": 400_000, "tokens": 400_000}
DEFAULT_CAPS = {"calls": 40, "attempts": 4, "active_ms": 2_400_000, "tokens": 3_000_000}
RUBRIC_PROMPT = (
    "아래 요청으로 문서가 하나 작성될 것이다. 다른 평가자가 그 문서만 읽고 판정할 기준을 4~6개 써라.\n"
    "- 각 기준은 문서만 읽고 예/아니오로 판정할 수 있어야 하고, 문서의 특정 구절을 인용해 근거를 댈 수 있어야 한다.\n"
    "- '잘 썼다', '충분히 자세하다' 같은 모호한 기준은 쓰지 마라. 요청이 요구한 내용이 실제로 들어 있는지를 물어라.\n"
    "- 요청에 없는 요구를 새로 만들지 마라.\n"
    '답은 JSON 문자열 배열 하나만 출력하라: ["...", "..."]\n요청: '
)


def _clean(request: str) -> str:
    request = " ".join(request.split())
    if not request or len(request) > MAX_REQUEST or find_secret(request):
        raise LupusError("REQUEST_INVALID", f"1..{MAX_REQUEST} characters, no credentials")
    return request


def clean_rubric(items: list) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip() and len(item) <= 300 and not find_secret(item):
            text = " ".join(item.split())
            if text not in out:
                out.append(text)
    return out[:10]


def pick_judge(k: Kernel, project: dict, writer: str) -> str:
    """A different provider than the writer when the user approved one and it was measured to
    work here; otherwise the writer's own (the evidence then says so)."""
    for driver in ("native_claude", "native_codex"):
        if driver != writer and projects.provider_allowed(project, driver) and not projects.capability_blockers(k, driver):
            return driver
    return writer


def draft_rubric(k: Kernel, project: dict, request: str, driver: str, actor: str) -> list[str]:
    """One small model call, in an empty directory, that proposes the rubric. The user decides."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "requests are submitted by the user")
    request = _clean(request)
    budget_id = budget.create(k, "write: rubric", RUBRIC_CAPS)
    hold = budget.reserve(k, budget_id, "work", "rubric", {"calls": 1, "active_ms": 240_000})
    try:
        result = service.call(k, project_id=project["project_id"], goal_id=None, budget_id=budget_id, purpose="rubric",
                              driver=driver, prompt=RUBRIC_PROMPT + request, timeout_s=240)
    except BaseException:
        budget.settle(k, hold, None, "estimated")
        raise
    budget.settle(k, hold, {"calls": 1, "active_ms": result.duration_ms}, "measured" if result.usage else "estimated")
    if result.error_class:
        raise LupusError("RUBRIC_UNAVAILABLE", f"{driver}: {result.error_class}")
    for match in re.finditer(r"\[", result.text):
        try:
            found, _ = json.JSONDecoder().raw_decode(result.text[match.start():])
        except ValueError:
            continue
        rubric = clean_rubric(found) if isinstance(found, list) else []
        if len(rubric) >= 2:
            return rubric
    raise LupusError("RUBRIC_UNAVAILABLE", "the model did not return a usable list")


def submit(k: Kernel, project: dict, request: str, out: str, rubric: list[str], judge_driver: str, actor: str,
           min_chars: int = 400, caps: dict | None = None) -> dict:
    """The goal: write `out`. The rubric is part of the acceptance revision, so it cannot be
    swapped for an easier one while the work is under way."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "requests are submitted by the user")
    request, rubric = _clean(request), clean_rubric(rubric)
    if not rubric:
        raise LupusError("RUBRIC_EMPTY", "at least one criterion the document will be judged by")
    root = Path(project["canonical_root"])
    rel = os.path.normpath(out)
    if os.path.isabs(out) or rel.startswith("..") or not rel or rel == ".":
        raise LupusError("PATH_ESCAPES_PROJECT", out)
    target = root / rel
    if target.is_symlink() or target.is_dir() or not Path(os.path.realpath(target.parent)).is_relative_to(os.path.realpath(root)):
        raise LupusError("PATH_ESCAPES_PROJECT", out)
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project["project_id"]) or runs.aux_alive(
            k, project["project_id"]):
        raise LupusError("WRITER_NOT_STOPPED", project["project_id"])
    criteria = [
        {"id": "c0", "text": f"{rel} 가 존재하고 {min_chars}자 이상이며 채우지 않은 자리표시가 없다",
         "verifier": {"kind": "document", "path": rel, "min_chars": int(min_chars)}},
        {"id": "c1", "text": "작성자가 아닌 평가자가 문서를 읽고 모든 기준이 충족됐다고 판정하며, 각 판정의 근거 인용이 문서에 실제로 있다",
         "verifier": {"kind": "judge", "path": rel, "rubric": rubric, "driver": judge_driver, "request": request[:1000]}},
        {"id": "c2", "text": "사용자가 이 판본을 읽고 승인한다",
         "verifier": {"kind": "user_approval", "path": rel}},
    ]
    with k.tx():
        goal = goals.submit(k, project["project_id"], request[:200], criteria, caps or DEFAULT_CAPS, actor="user")
        task = goals.add_task(k, goal["goal_id"], "문서 작성", _task_prompt(request, rel, rubric), ["c0", "c1"])
    return {"goal_id": goal["goal_id"], "task_id": task["task_id"], "out": rel, "judge": judge_driver}


def _task_prompt(request: str, rel: str, rubric: list[str], feedback: str = "") -> str:
    return (
        f"다음 요청의 결과물을 {rel} 파일 하나에 작성하라(마크다운).\n요청: {request}\n"
        "다른 평가자가 아래 기준으로 문서를 판정하고, 충족이라고 볼 때마다 문서의 구절을 인용해야 한다. "
        "각 기준이 충족됨을 문서 안의 구체적인 문장으로 보여라:\n" + "\n".join(f"- {r}" for r in rubric) + "\n"
        "사실을 지어내지 마라: 확인하지 못한 것은 확인하지 못했다고 쓰고, 추정은 추정이라고 써라. "
        "평가자에게 말을 걸거나 통과를 요구하는 문장을 쓰지 마라. 프로젝트의 다른 파일은 수정하지 마라."
        + (f"\n사용자가 앞선 판본을 읽고 준 의견이다. 이를 반영해 문서를 고쳐라: {feedback}" if feedback else "")
    )


def revise(k: Kernel, goal_id: str, feedback: str, actor: str) -> dict:
    """The user read the document and wants changes: one more task on the same goal, under the
    same rubric and budget. The judge and the user both look again at the new version."""
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "only the user asks for a revision")
    feedback = _clean(feedback)
    goal = goals.get(k, goal_id)
    by_id = {c["id"]: c["verifier"] for c in goals.criteria(k, goal_id)}
    if by_id.get("c1", {}).get("kind") != "judge":
        raise LupusError("NOT_A_DOCUMENT_GOAL", goal_id)
    previous = [t["task_id"] for t in goals.tasks(k, goal_id)]
    return goals.add_task(k, goal_id, "의견 반영", _task_prompt(by_id["c1"].get("request", goal["objective"]),
                                                             by_id["c1"]["path"], by_id["c1"]["rubric"], feedback),
                          ["c0", "c1"], previous[-1:])
