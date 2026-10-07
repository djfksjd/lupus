"""What a drafted check asserts, said by the worker that wrote it, shown to the user before
they approve it.

Why it exists: in `lupus do` one call writes both the test and the implementation, so the test
encodes that worker's own reading of the request, and a test that passes with its own
implementation says nothing about whether the reading was right. On the hidden-test pilot the
wrong results that Lupus called DONE were mostly of one kind: the request did not state a
value, a wording or a boundary; the worker picked one; nothing on the approval screen said so.
The user approves the test, so the user has to be able to see what was picked.

What it is: a small JSON block at the end of the drafting worker's reply (not a file in the
project: it is not part of what ships). One entry per behavioural assertion, each saying where
its rule comes from: the request (with the words quoted), something already in the repository,
or the worker's own choice (with one alternative and an input on which the two differ).

What Lupus does with it, and does not:
  * It is a claim by a model and may be wrong or incomplete. It is shown as such, above the test
    source, never instead of it.
  * What can be checked mechanically is: a rule said to come from the request must quote words
    that are in the request, a request clause must be words of the request, and a named test
    must occur in the test file. A failed check is shown, not hidden.
  * It is bound to the exact test it was written for (by hash). If the user edits the test, the
    description no longer applies and the screen says so.
  * Absent or unreadable is shown as "not disclosed", never as "nothing was assumed".
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import memory
from .kernel import Kernel
from .util import find_secret, sha256_json

SOURCES = ("explicit_request", "existing_contract", "worker_choice")
MAX_ASSERTIONS, MAX_CLAUSES, MAX_UNTESTED, MAX_TEXT = 20, 15, 10, 300
REQUEST = (
    "마지막으로, 답의 끝에 네가 쓴 테스트를 설명하는 JSON 객체 하나를 출력하라(사용자가 테스트를 승인하기 전에 읽는다. "
    "파일로 만들지 말고 답으로만 출력하라). 형식:\n"
    '{"assertions": [{"test": "테스트 함수 이름", "input": "구체적인 입력", "expected": "기대 결과", "rule": "이 단언이 요구하는 규칙", '
    '"source": "explicit_request | existing_contract | worker_choice", "quote": "…", "where": "…", "choice": "…", '
    '"alternative": "…", "differs_on": "…"}], '
    '"clauses": [{"clause": "요청의 한 구절(요청문 그대로)", "covered_by": ["테스트 함수 이름"]}], '
    '"untested": [{"tested": "검사한 입력의 종류", "not_tested": "가장 가까운데 검사하지 않은 종류"}]}\n'
    "- 동작을 단언하는 곳마다 assertions 항목 하나를 쓴다.\n"
    "- source 는 그 규칙의 출처다. 요청에 적혀 있으면 explicit_request 이고 quote 에 요청의 해당 구절을 글자 그대로 옮긴다. "
    "저장소의 기존 코드·문서·테스트가 이미 정한 것이면 existing_contract 이고 where 에 그 위치를 쓴다. "
    "둘 다 아니면(값, 한계, 오류 문구, 경계에서의 동작 등을 네가 정했으면) worker_choice 이고, choice 에 네가 정한 것을, "
    "alternative 에 그럴듯한 다른 선택 하나를, differs_on 에 두 선택의 결과가 갈리는 구체적인 입력을 쓴다.\n"
    "- clauses 에는 요청의 각 구절을 요청문 그대로 옮기고, 그것을 확인하는 테스트를 covered_by 에 쓴다. 확인하는 테스트가 없으면 빈 배열로 둔다.\n"
    "- untested 에는 검사한 입력 종류마다 가장 가까운데 검사하지 않은 종류를 쓴다(없으면 생략).\n"
    "모르는 것을 아는 것처럼 쓰지 마라. 요청에 없는 것을 요청에 있다고 쓰면 인용 확인에서 드러난다."
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _text(value: Any) -> str:
    """One line of plain text: nothing that moves a terminal's cursor or hides from the eye."""
    return " ".join(_CONTROL.sub(" ", memory.visible(str(value if value is not None else ""))).split())[:MAX_TEXT]


def _within(span: str, whole: str) -> bool:
    squeeze = lambda s: " ".join(s.split())      # noqa: E731  (letter case kept: `FAIL` is not `fail`)
    return bool(span.strip()) and squeeze(span) in squeeze(whole)


def parse(reply: str, request: str, test_source: str) -> dict | None:
    """The description in a worker's reply, cleaned and checked; None when there is none."""
    found = None
    for match in re.finditer(r"\{", reply):
        try:
            candidate, _ = json.JSONDecoder().raw_decode(reply[match.start():])
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("assertions"), list):
            found = candidate      # the last one wins: a worker may quote the format before filling it in
    if found is None:
        return None
    assertions = []
    for item in found["assertions"][:MAX_ASSERTIONS]:
        if not isinstance(item, dict):
            continue
        entry = {key: _text(item.get(key)) for key in ("test", "input", "expected", "rule", "quote", "where", "choice",
                                                        "alternative", "differs_on")}
        source = _text(item.get("source"))
        entry["source"] = source if source in SOURCES else "worker_choice"      # unstated origin is the worker's own
        # A rule "from the request" has to show the words. Without them it is the worker's reading.
        entry["quote_in_request"] = entry["source"] != "explicit_request" or _within(entry["quote"], request)
        entry["test_in_file"] = bool(entry["test"]) and entry["test"] in test_source
        if entry["rule"] or entry["expected"]:
            assertions.append(entry)
    clauses, not_in_request = [], 0
    for item in (found.get("clauses") if isinstance(found.get("clauses"), list) else [])[:MAX_CLAUSES]:
        if not isinstance(item, dict):
            continue
        clause = _text(item.get("clause"))
        if not _within(clause, request):
            not_in_request += 1
            continue
        covered = item.get("covered_by") if isinstance(item.get("covered_by"), list) else []
        clauses.append({"clause": clause, "covered_by": [_text(c) for c in covered[:8] if _text(c) and _text(c) in test_source]})
    untested = [{"tested": _text(i.get("tested")), "not_tested": _text(i.get("not_tested"))}
                for i in (found.get("untested") if isinstance(found.get("untested"), list) else [])[:MAX_UNTESTED]
                if isinstance(i, dict) and _text(i.get("not_tested"))]
    packet = {"assertions": assertions, "clauses": clauses, "untested": untested, "clauses_not_in_request": not_in_request}
    if not assertions or find_secret(json.dumps(packet, ensure_ascii=False)):
        return None
    return packet


def capture(k: Kernel, goal_id: str, attempt_id: str, reply: str, request: str, test_bytes: bytes | None) -> dict | None:
    """Store what the drafting worker said about the test it just wrote, bound to that test."""
    if test_bytes is None:
        return None
    from .util import sha256_bytes
    packet = parse(reply, request, test_bytes.decode("utf-8", errors="replace"))
    if packet is None:
        return None
    with k.tx():
        k.emit("supervisor", "do.contract", "goal", goal_id, attempt=attempt_id, test_sha256=sha256_bytes(test_bytes), packet=packet)
    return packet


def latest(k: Kernel, goal_id: str, test_sha256: str) -> dict | None:
    """The description that belongs to exactly this version of the test, if there is one."""
    row = k.one("SELECT payload FROM event WHERE type = 'do.contract' AND aggregate_id = ? ORDER BY seq DESC LIMIT 1", goal_id)
    if row is None:
        return None
    seen = json.loads(row["payload"])
    return seen["packet"] if seen["test_sha256"] == test_sha256 else None


def digest(packet: dict | None) -> str:
    return sha256_json(packet) if packet else ""


def render(packet: dict | None) -> str:
    """The block printed above the test source at approval."""
    if packet is None:
        return ("[가정 공개 없음] worker가 이 검사에서 무엇을 스스로 정했는지 밝히지 않았습니다(또는 검사를 고쳐서 설명이 더 이상 "
                "맞지 않습니다). 정한 것이 없다는 뜻이 아닙니다. 아래 테스트를 직접 읽어 판단하세요.")
    lines = ["이 검사가 확인하는 것 (worker가 밝힌 내용이며 틀리거나 빠질 수 있습니다. 아래 테스트 원문이 기준입니다)"]
    chosen = []
    for i, a in enumerate(packet["assertions"], 1):
        mark = "" if a["test_in_file"] else "  (이 이름을 테스트 원문에서 찾지 못함)"
        lines.append(f"  {i}. [{a['test'] or '?'}] {a['input'] or '-'} → {a['expected'] or '-'}{mark}")
        if a["source"] == "worker_choice":
            chosen.append(f"  - ({i}번) 정한 것: {a['choice'] or a['rule'] or a['expected']}"
                          + (f" / 다른 선택지: {a['alternative']}" if a["alternative"] else "")
                          + (f" / 갈리는 입력: {a['differs_on']}" if a["differs_on"] else ""))
        elif not a["quote_in_request"]:
            chosen.append(f"  - ({i}번) worker는 요청에 있다고 했으나 그 인용이 요청문에 없습니다: {a['rule'] or a['expected']}")
    lines.append("요청에 없어서 worker가 정한 것 — 승인 전에 여기를 보세요" + ("" if chosen else ": worker는 없다고 했습니다(확인된 것은 아닙니다)"))
    lines += chosen
    unchecked = [c["clause"] for c in packet["clauses"] if not c["covered_by"]]
    if unchecked:
        lines += ["요청 문구 중 이 검사가 확인하지 않는 것", *[f'  - "{c}"' for c in unchecked]]
    if packet["untested"]:
        lines += ["가까운데 검사하지 않은 경우", *[f"  - 검사함: {u['tested'] or '-'} / 검사 안 함: {u['not_tested']}" for u in packet["untested"]]]
    if packet["clauses_not_in_request"]:
        lines.append(f"(요청문에 없는 구절을 요청의 구절이라고 한 항목 {packet['clauses_not_in_request']}개는 뺐습니다)")
    return "\n".join(lines)
