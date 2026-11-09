"""Deterministic request routing. Choosing a path never costs a model call.

Only recognisable information requests use the read-only answer path. Shortness alone
never makes a code change safe to leave unchecked. Unknown requests keep the do contract.
"""

from __future__ import annotations

import re
import os
import time
from pathlib import Path

from . import adapters, arithmetic, definitions, readtext, textedit, memory, auto_learning
from .util import LupusError, diagnostic_tail, find_secret, timeout_ms

_CHANGE = re.compile(
    r"\b(?:add|implement|fix|change|edit|modify|create|write|delete|remove|rename|refactor|"
    r"replace|install|deploy|commit|apply|update|upgrade|save|execute|run)\b(?!\s*\()|"
    r"고쳐|고치|바꿔|만들|지워|교체|리팩터|리팩토|"
    r"(?:수정|변경|추가|구현|생성|작성|삭제|저장|적용|배포|설치|커밋|반영|고도화|실행|진행|업데이트|복구)"
    r"(?:\s*(?:해|하|부탁|바라|요청)|$)", re.I)
_READ = re.compile(
    r"^(?:what|why|how|where|explain|summari[sz]e|describe|compare|show|list|review|inspect|find|read|hi|hello|hey|thanks|thank you)\b|"
    r"설명|요약|분석|비교|알려|읽어|보여|찾아|검토|무엇|어떻게|왜|^안녕|^고마워|^감사", re.I)
# These forms ask for instructions, even when their subject contains an action word.
_HOW_TO = re.compile(r"^(?:please\s+)?(?:(?:how\s+(?:do|can|should|would)\s+(?:i|we)\s+)|"
                     r"(?:(?:explain|describe)\s+how\s+(?:to\s+)?))\S", re.I)
_METHOD_KO = re.compile(r".+(?:방법|법)(?:을|를)?\s*(?:알려\s*(?:줘|주세요)|설명해\s*(?:줘|주세요))[.!?]?$")
_HOW_KO = re.compile(r".*어떻게\s+.+\?$")
_INFO_CLAUSES = re.compile(r"[.!?,:;–—\r\n]|\s+-\s+|\b(?:and|then|also|but|while)\b|그리고|그런\s*다음|(?:하고|고|면서)(?=\s)|(?:한|친)\s*(?:뒤|후)", re.I)
_DO_IT = re.compile(r"\b(?:do\s+(?:it|this|that)|make\s+(?:it|this|that)\s+(?:work|happen))\b", re.I)
_OTHER_CHANGE = re.compile(r"\b(?:make|repair|correct|resolve|perform)\b", re.I)
_POLITE_ACTION = re.compile(r"\b(?:please|(?:can|could|would|will)\s+you)\b", re.I)
_KO_ACTION = re.compile(r"(?:해|고쳐|바꿔|만들어|지워)\s*(?:줘|주세요|주고|라|서)")
_POLITE_INFO = re.compile(
    r"^(?:please\s+|(?:can|could|would)\s+you\s+(?:please\s+)?)(?=(?:explain|describe|summari[sz]e)\b)", re.I)


def informational_howto(request: str) -> bool | None:
    """True: narrow advice; False: mixed candidate; None: use the existing read heuristic."""
    request = re.sub(r"[^\S\r\n]+", " ", request.strip())
    flat = " ".join(request.split())
    english = _HOW_TO.match(request)
    if not english and not (_METHOD_KO.fullmatch(flat) or _HOW_KO.fullmatch(flat)):
        return None
    if not english and any(re.match(r"(?:고|면서)", request[change.end():])
                           for change in _CHANGE.finditer(request)):
        return False
    clauses = [clause.strip() for clause in _INFO_CLAUSES.split(request) if clause.strip()]
    if not english and len(clauses) > 1 and not all(
            (_READ.search(clause) or "읽" in clause) and not _CHANGE.search(clause) for clause in clauses):
        return False
    for clause in clauses[1:]:
        if _CHANGE.search(clause) or _DO_IT.search(clause) or _OTHER_CHANGE.search(clause):
            return False
    # Asking a person to act is different from asking how I/we would act.
    for marker in _POLITE_ACTION.finditer(request):
        suffix = request[marker.end():]
        if (not english or marker.start() >= english.end()) and (
                _CHANGE.search(suffix) or _DO_IT.search(suffix) or _OTHER_CHANGE.search(suffix)):
            return False
    korean = re.sub(r"(?:설명해|알려)\s*(?:줘|주세요)", "", request)
    return not _KO_ACTION.search(korean) and not _DO_IT.search(request)


_STATUS_EN = re.compile(
    r"(?:(?:please\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:check|report(?:\s+on)?)\s+(?:(?:the|current)\s+)*"
    r"(?:(?P<before>.+?)\s+status|status\s+of\s+(?P<after>.+?))|"
    r"(?:current\s+)?status\s+of\s+(?P<label>.+?))", re.I)
_STATUS_KO = re.compile(
    r"(?P<subject>.+?)\s*(?:상태|현황)(?:을|를)?\s*"
    r"(?:확인|점검)\s*(?:해\s*(?:줘|주세요|줄래)|해주세요|부탁해(?:요)?|부탁드립니다)")
_STATUS_CLAUSE = re.compile(
    r"[,;:!?。\r\n]|&&|\|\||[=<>|&]|\$\(|\.(?=\s|$)|\s+-\s+|[–—]|\b(?:and|then|also|but|while)\b|"
    r"그리고|그런\s*다음|(?:하고|주고|면서)(?=\s)|(?:한|친)\s*(?:뒤|후)", re.I)
_STATUS_EXTRA_ACTION = re.compile(
    r"(?:^|[,;:.!?\r\n]|&&|\|\||\b(?:and|then|also|but|while)\s+|그리고\s*|(?:하고|주고)\s*)"
    r"\s*(?:(?:please|(?:can|could|would|will)\s+you)\s+)?"
    r"(?:checkout|set|assign|mark|reset|start|stop|restart|enable|disable|push|pull|merge|switch)\b|"
    r"(?:설정|할당|표시)\s*해|돌려\s*(?:줘|주세요)|돌리(?:고|면서)", re.I)
_STATUS_TEMPORAL_EXECUTION = re.compile(r"\b(?:after|before|by)\s+(?:running|executing)\b|돌린\s*(?:뒤|후)", re.I)


def informational_status(request: str) -> bool | None:
    """Narrow status inquiries; only clear mixed actions veto the older read heuristic."""
    request = request.strip()
    # A balanced quoted subject is data, including action words in a filename.
    syntax = re.sub(r"`[^`\r\n]*`|\"[^\"\r\n]*\"|'[^'\r\n]*'", "subject", request)
    status = re.search(r"\bstatus\b|상태|현황", syntax, re.I)
    inquiry = re.search(r"\b(?:check|report|show|inspect|review)\b|확인|점검|보여|알려", syntax, re.I)
    if not status or not (inquiry or re.match(r"(?:current\s+)?status\s+of\b", request, re.I)):
        return None
    if _STATUS_EXTRA_ACTION.search(syntax):
        return False
    if informational_howto(request) is True:
        return None  # Preserve instruction questions whose subject contains action words.
    if _STATUS_TEMPORAL_EXECUTION.search(syntax) or _CHANGE.search(syntax) or _OTHER_CHANGE.search(syntax) or _DO_IT.search(syntax):
        return False
    clause = re.sub(r"[.!?。]$", "", request).strip()
    if _STATUS_CLAUSE.search(re.sub(r"[.!?。]$", "", syntax).strip()):
        return None
    match = _STATUS_EN.fullmatch(clause) or _STATUS_KO.fullmatch(clause)
    if match is None:
        return None
    subject = next(value for value in match.groupdict().values() if value is not None).strip()
    # Quoted paths are literal subjects, never shell input; unmatched delimiters stay uncertain.
    if not subject or any(subject.count(quote) % 2 for quote in ('"', "'", '`')):
        return None
    if subject.lower() in ('the', 'current', 'this', '현재', '지금', '이', '그'):
        return None
    return True


_FIX_TESTS = re.compile(
    r"(?:(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?)(?:(?:fix|repair)\s+(?:the\s+)?(?:currently\s+|current\s+)?failing\s+tests(?:\s+please)?|"
    r"make\s+(?:the\s+)?(?:currently\s+|current\s+)?failing\s+tests\s+pass)[.!?]?|"
    r"(?:현재\s*)?실패(?:하는|한|\s*중인)\s*테스트(?:를|들을)?\s*(?:고쳐\s*줘|고쳐\s*줄래|수정해\s*줘|복구해\s*줘|통과시켜\s*줘)[.!?]?", re.I)
_ARITHMETIC = re.compile(r"(?=.*\d)[\d\s()+*/%.=?×÷-]+\Z")


def plan(request: str, mode: str = "auto", check: str | None = None) -> dict:
    raw_request = request
    request = " ".join(request.split())
    if not request or len(request) > 2000 or find_secret(request):
        raise LupusError("REQUEST_INVALID", "1..2000 characters, no credentials")
    if mode not in ("auto", "answer", "verified"):
        raise LupusError("ROUTE_INVALID", mode)
    if check is not None:
        if mode == "answer":
            raise LupusError("ROUTE_INVALID", "--check cannot be combined with read-only answer mode")
        return {"route": "checked", "reason": "user-supplied acceptance command; no test-authoring call",
                **({"execution": "literal-edit"} if mode == "auto" and textedit.parse(raw_request, source=True) is not None else {})}
    if mode == "auto" and textedit.parse(raw_request) is not None:
        return {"route": "text-edit", "reason": "exact single document replacement; fixed result and scope, no model call"}
    status = informational_status(raw_request) if mode == "auto" else None
    howto = informational_howto(raw_request) if mode == "auto" else None
    if mode == "auto" and howto is None:
        polite = _POLITE_INFO.match(request)
        if polite:
            # Preserve newlines for the existing mixed-clause safeguards.
            body = _POLITE_INFO.sub("", raw_request.strip(), count=1)
            howto = informational_howto(body)
            if howto is None:
                howto = not (_CHANGE.search(body) or _OTHER_CHANGE.search(body) or
                             _DO_IT.search(body) or _KO_ACTION.search(body))
    if mode == "answer" or (mode == "auto" and (
            definitions.parse(raw_request) is not None or readtext.parse(raw_request) is not None or
            (status is not False and (status is True or howto is True or
             (howto is None and (_READ.search(request) or _ARITHMETIC.fullmatch(request) or arithmetic.expression(request))
              and not _CHANGE.search(request)))))):
        return {"route": "answer", "reason": "read-only answer; bounded worker, no goal or test authoring"}
    if mode == "auto" and _FIX_TESTS.fullmatch(request):
        return {"route": "fix-tests", "reason": "repair observed failing tests; existing frozen suite, no test authoring"}
    return {"route": "verified", "reason": "change or uncertain intent; approved-test workflow"}


def reader(driver: str, model: str | None = None, effort: str | None = None, *, home=None) -> adapters.Adapter:
    adapters.validate_effort(driver, effort)
    if driver == "codex":
        return adapters.CodexAdapter(model=model, readonly=True, effort=effort)
    if driver == "claude":
        adapter = adapters.ClaudeAdapter(model=model, tools=("Read", "Glob", "Grep"), effort=effort)
        adapter.readonly = True
        adapter.require_os_sandbox = True
        return adapter
    return adapters.APIAdapter(driver, model, effort, home=home, readonly=True)


def answer(request: str, root: Path, driver: str, model: str | None = None, timeout_s: float = 300,
           effort: str | None = None, *, home=None, primary_instructions: str | None = None, skill_access: dict | None = None) -> dict:
    timeout_ms(timeout_s)
    adapters.validate_effort(driver, effort)
    requested = {"requested_worker_effort": effort} if effort is not None else {}
    plan(request, "answer")  # validate without creating a runtime, goal, snapshot or probe
    started = time.perf_counter()
    computed = arithmetic.calculate(request)
    if computed is not None:
        return {"route": "answer", "verified": False, "result": computed, "computed_locally": True,
                "calls": 0, "usage": {"tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "calls": 0},
                "seconds": time.perf_counter() - started, "error": None, "exit_code": 0, "detail": "", **requested}
    located = readtext.contents(request, root, max_reply=adapters.MAX_REPLY)
    if located is None:
        located = definitions.locate(request, root, max_reply=adapters.MAX_REPLY)
    if located is not None:
        return {"route": "answer", "verified": False, "computed_locally": True, **located,
                "calls": 0, "usage": {"tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "calls": 0},
                "seconds": time.perf_counter() - started, "error": None, "exit_code": 0, "detail": "", **requested}
    prompt = "Answer the request concisely. Read-only: do not modify files or run tests.\nRequest: " + request
    auto_learning.prepare()
    context, context_info = memory.readonly_context(Path(home or os.environ.get("LUPUS_HOME") or Path.home()/".lupus"), root, request)
    if context:prompt += context
    selected = {"home": home} if driver not in ("codex", "claude") else {}
    adapter = reader(driver, model, effort, **selected)
    if primary_instructions:
        adapter.primary_instructions = primary_instructions
    if isinstance(adapter,adapters.APIAdapter):adapter.skill_access=skill_access
    result = adapters.execute(adapter, prompt, root.resolve(), lambda pid: None, timeout_s=timeout_s)
    return {"route": "answer", "verified": False,
            "computed_locally": False, "memory_context": context_info,
            "process_lifetime": adapters.lifetime_diagnostics(result),
            **({"requested_provider": result.raw["requested_provider"], "requested_model": result.raw["requested_model"], "effective_backend_confirmed": False} if "requested_provider" in result.raw else {}),
            "result": diagnostic_tail(result.text, limit=adapters.MAX_REPLY, multiline=True),
            "calls": result.raw.get("http_request_attempts", 1), "host_calls": 1, "usage": result.usage,
            "usage_coverage": adapters.usage_info(result.usage, result.raw)["fields"],
            "known_token_lower_bound": adapters.usage_info(result.usage, result.raw)["known_token_lower_bound"], "seconds": result.duration_ms / 1000,
            **({'skill_reads':result.raw['skill_reads'],'skill_reads_limited':result.raw.get('skill_reads_limited',False)} if 'skill_reads' in result.raw else {}),
            **({'tool_diagnostics':result.raw['tool_diagnostics']} if 'tool_diagnostics' in result.raw else {}),
            "error": result.error_class, "exit_code": result.exit_code,
            "detail": result.stderr_tail if result.error_class else "",
            **({"capture": {key: result.raw[key] for key in
                            ('stdout_bytes', 'stderr_bytes', 'stdout_truncated', 'stderr_truncated')}}
               if 'stdout_bytes' in result.raw else {}), **requested}
