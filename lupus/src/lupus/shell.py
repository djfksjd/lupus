"""`lupus` on its own: the Lupus interactive shell.

A home screen (the wolf mark, where you are, what is waiting) and one prompt. A line of text is a
request sent through `lupus auto --shell`: questions get read-only answers, exact document
replacements run locally, and failing-test repairs use the existing frozen suite. Other changes
use `lupus do`: the worker drafts the test, you approve it, it builds, and
the worker may run the project's own tests inside the sandbox while it works (the plain `lupus do`
keeps that off unless asked; user decision 2026-10-26). Slash commands open a session, fix red
tests, switch the driver, or run any other `lupus` command. `/view` opens a full-screen browser with
tabs over the same data the web viewer shows: overview, goals, knowledge and the vault documents.

Nothing here decides anything: every action is an ordinary `lupus` command with its own
confirmations, run in this process.
"""

from __future__ import annotations

import json
import io
import os
import re
import select
import shlex
import shutil
import sys
import termios
import tty
import unicodedata
from pathlib import Path
from typing import Callable
from dataclasses import dataclass, field

from . import supervision, adapters, graph, projects, providers, runners, skills, vault, wolfmark, prompt
from .kernel import Kernel
from .util import LupusError, atomic_write, language

# ------------------------------------------------------------------ words

WORDS = {
    "en": {
        "tagline": "A I   W O R K S   T O G E T H E R",
        "project": "project", "tests": "tests", "goals": "goals", "memory": "memory", "worker": "provider", "mode": "mode", "model": "model",
        "not_here": "no project here", "not_registered": "not registered yet (the first request registers it)",
        "no_tests": "none found", "tests_found": "{runner}", "none": "none yet",
        "goal_line": "{done} done · {active} working · {wait} waiting",
        "mem_line": "{n} notes · {t} trusted",
        "probe": "first run: /probe measures your {cli} CLI once (2 tiny calls)",
        "intro": "Describe a task. Auto chooses a short path; /mode selects ask, agent or verify.",
        "commands": "/provider  /model  /mode  /skills  /parallel  /help",
        "tab_hint": "/ opens suggestions · Tab inserts · /view opens memory",
        "prompt_hint": "describe a change, or /help",
        "help": [
            ("/help", "show commands, modes and local controls"),
            ("<text>", "use the selected mode; auto keeps simple requests short"),
            ("/mode auto|ask|agent|verify", "ask reads; agent edits without a test draft; verify uses approved checks"),
            ("/ask TEXT", "a read-only request with the selected provider and model"),
            ("/agent TEXT", "bounded coding without freezing ordinary tests; result is unverified"),
            ("/verify TEXT", "implement against a check you approve"),
            ("/provider [ID]", "list providers or switch; selection makes no network request"),
            ("/provider allow ID", "approve that endpoint for this project's future requests"),
            ("/supervisor off|PROVIDER MODEL [EFFORT]", "optional advice after a verified failure; separate from the primary model"),
            ("/skill-mode auto|off", "API primary workers: discover useful skills on demand; off keeps explicit selections only"),
            ("/profile lean|user", "agent only: Codex user settings/tools inside the required OS sandbox"),
            ("/model [ID|default]", "select a model; provider/model also switches provider"),
            ("/models [--discover]", "configured models; --discover explicitly queries the provider"),
            ("/skills [FILTER]", "find local skills and see their source"),
            ("/skill ID [TEXT]", "load selected SKILL.md instructions in the current mode; /skill show ID previews"),
            ("/parallel [auto|1..4]", "show the queue or start existing goals in the background; one writer per project"),
            ("/jobs · /logs ID · /stop ID", "inspect, follow or cancel background work"),
            ("/auto … --check CMD", "use your acceptance command without drafting a new test"),
            ("/session", "your usual interactive claude/codex, tests frozen, verified when you leave"),
            ("/probe", "explicitly measure only the selected native CLI; uses model calls"),
            ("/fix", "make the failing tests pass"),
            ("/view", "tabs: overview · goals · knowledge · documents"),
            ("/status", "this screen again"),
            ("/lang en|ko", "language of this shell"),
            ("/graph", "write and open the web viewer"),
            ("/<command> …", "any other lupus command, e.g. /diff goal_x, /accept goal_x, /jobs"),
            ("/quit", "leave (also Ctrl-D)"),
        ],
        "bye": "The pack rests.", "unknown_driver": "Unknown provider. Use /providers to see available IDs.",
        "host_default": "host default", "model_required": "select a model", "selected": "selected", "unverified": "edits are unverified",
        "no_models": "No configured models. /model MODEL selects an ID; /models --discover queries the endpoint.",
        "requested_model": "requested ID; execution not verified",
        "skill_empty": "No matching skills. Add .lupus/skills/<name>/SKILL.md or ~/.lupus/skills/<name>/SKILL.md.",
        "parallel_hint": "Existing goals · auto: conservative run-start cap · max 4 workers · one writer per project. /parallel 2 starts a background job; /jobs lists it. Use isolated projects for parallel work on the same repository.",
        "tabs": ["Overview", "Goals", "Knowledge", "Documents"],
        "keys": "Tab ←→ switch · ↑↓ move · ↵ open · q close",
        "phase": {"done": "done", "active": "working", "wait": "waiting", "stop": "stopped"},
        "status": {"verified": "verified", "confirmed": "confirmed", "candidate": "candidate", "retired": "retired"},
        "nothing": "Nothing recorded yet.", "trust": "trust", "kinds": "by kind",
        "global": "Global", "home": "Home", "map": "Map", "folder_goals": "Goals", "folder_nodes": "Knowledge",
        "too_small": "Make the terminal at least 60×16 for the tabbed view.",
        "use_line": "recalled {r} · helped {h} · did not help {u}",
        "legend": "● verified  ◉ confirmed  ○ candidate  · retired",
        "projects": "projects", "project_row": "{g} goals ({d} done) · {n} notes",
    },
    "ko": {
        "tagline": "A I   W O R K S   T O G E T H E R",
        "project": "프로젝트", "tests": "테스트", "goals": "목표", "memory": "기억", "worker": "제공자", "mode": "작업 방식", "model": "모델",
        "not_here": "여기는 프로젝트가 아닙니다", "not_registered": "아직 등록 전 (첫 요청 때 등록됩니다)",
        "no_tests": "찾지 못함", "tests_found": "{runner}", "none": "아직 없음",
        "goal_line": "완료 {done} · 진행 {active} · 대기 {wait}",
        "mem_line": "지식 {n} · 신뢰 {t}",
        "probe": "처음 실행: /probe 가 {cli} CLI를 한 번 측정합니다 (작은 호출 2번)",
        "intro": "할 일을 적으세요. auto가 간단한 경로를 고릅니다. /mode로 질문·자유 작업·검증 작업을 선택합니다.",
        "commands": "/provider  /model  /mode  /skills  /parallel  /help",
        "tab_hint": "/로 후보 보기 · Tab으로 채우기 · /view로 기억 열기",
        "prompt_hint": "바꿀 내용을 적거나 /help",
        "help": [
            ("/help", "명령·작업 방식·로컬 설정 안내 보기"),
            ("/supervisor off|PROVIDER MODEL [EFFORT]", "검증 실패 뒤 필요할 때만 조언 · 주 모델과 별도 선택"),
            ("/skill-mode auto|off", "API 작업에서 필요한 스킬을 요청할 때 탐색합니다. off는 명시적으로 선택한 자료만 사용합니다"),
            ("/profile lean|user", "agent 전용: 필수 OS 샌드박스 안에서 Codex 사용자 설정과 도구 사용"),
            ("<글>", "선택한 방식으로 실행. auto는 간단한 요청에 짧은 경로를 사용"),
            ("/mode auto|ask|agent|verify", "ask는 읽기 전용, agent는 테스트 초안 없이 편집, verify는 승인한 검사로 검증"),
            ("/ask 내용", "선택한 제공자·모델에 읽기 전용으로 질문"),
            ("/agent 내용", "기존 테스트를 고정하지 않고 편집. 결과는 미검증으로 표시"),
            ("/verify 내용", "승인한 완료 조건에 맞춰 구현·검증"),
            ("/provider [ID]", "제공자 목록·전환. 선택만으로 네트워크 요청하지 않음"),
            ("/provider allow ID", "이 프로젝트의 향후 요청을 해당 엔드포인트로 보내도록 승인"),
            ("/model [ID|default]", "모델 선택. 제공자/모델 형식으로 함께 전환 가능"),
            ("/models [--discover]", "설정된 모델 목록. --discover는 제공자에 목록 요청"),
            ("/skills [검색어]", "로컬 스킬과 출처 찾기"),
            ("/skill ID [내용]", "현재 모드에서 SKILL.md 지침 사용. /skill show ID로 미리 보기"),
            ("/parallel [auto|1..4]", "대기 목표 확인·백그라운드 실행. 프로젝트마다 작성자 한 명"),
            ("/jobs · /logs ID · /stop ID", "백그라운드 작업 조회·로그·중단"),
            ("/auto … --check CMD", "내 검사 명령으로 바로 구현·검증 (테스트 초안 생략)"),
            ("/session", "평소 쓰는 claude/codex 대화형 세션, 테스트 고정, 끝날 때 검증"),
            ("/probe", "선택한 네이티브 CLI만 명시적으로 측정. 모델 호출 발생"),
            ("/fix", "실패하는 테스트를 통과시키기"),
            ("/view", "탭: 개요 · 목표 · 지식 · 문서"),
            ("/status", "이 화면 다시 보기"),
            ("/lang en|ko", "이 셸의 언어"),
            ("/graph", "웹 뷰어를 만들고 열기"),
            ("/<명령> …", "그 밖의 lupus 명령, 예: /diff goal_x, /accept goal_x, /jobs"),
            ("/quit", "나가기 (Ctrl-D 도 됨)"),
        ],
        "bye": "무리가 쉽니다.", "unknown_driver": "알 수 없는 제공자입니다. /providers로 ID를 확인하세요.",
        "host_default": "호스트 기본값", "model_required": "모델 선택 필요", "selected": "선택", "unverified": "편집 결과는 미검증",
        "no_models": "설정된 모델이 없습니다. /model ID로 선택하거나 /models --discover로 서버 목록을 요청하세요.",
        "requested_model": "선택한 ID · 실행 미확인",
        "skill_empty": "스킬이 없습니다. .lupus/skills/<이름>/SKILL.md 또는 ~/.lupus/skills/<이름>/SKILL.md를 추가하세요.",
        "parallel_hint": "기존 목표 실행 · auto: 시작 시 보수적 상한 선택 · 최대 4명 · 프로젝트마다 작성자 한 명. /parallel 2로 백그라운드 실행, /jobs로 조회합니다. 같은 저장소에서 병렬 작업하려면 격리 프로젝트를 사용하세요.",
        "tabs": ["개요", "목표", "지식", "문서"],
        "keys": "Tab ←→ 전환 · ↑↓ 이동 · ↵ 열기 · q 닫기",
        "phase": {"done": "완료", "active": "진행", "wait": "대기", "stop": "중단"},
        "status": {"verified": "사용자 확인", "confirmed": "반복 사용", "candidate": "미검증", "retired": "은퇴"},
        "nothing": "아직 기록된 것이 없습니다.", "trust": "신뢰", "kinds": "종류별",
        "global": "전역", "home": "처음", "map": "지도", "folder_goals": "목표", "folder_nodes": "지식",
        "too_small": "탭 화면은 터미널이 최소 60×16 이어야 합니다.",
        "use_line": "회상 {r} · 도움 {h} · 도움 안 됨 {u}",
        "legend": "● 사용자 확인  ◉ 반복 사용  ○ 미검증  · 은퇴",
        "projects": "프로젝트", "project_row": "목표 {g}개 (완료 {d}) · 지식 {n}",
    },
}
KIND_NAMES = {
    "en": {"project": "project", "goal": "goal", "lesson": "lesson", "decision": "decision", "fact": "fact",
           "procedure": "procedure", "preference": "preference"},
    "ko": {"project": "프로젝트", "goal": "목표", "lesson": "교훈", "decision": "결정", "fact": "사실",
           "procedure": "절차", "preference": "선호"},
}
NOTES = ("lesson", "decision", "fact", "procedure", "preference")
PHASE = {"DONE": "done", "ACTIVE": "active", "RECONCILING": "active", "CANCELLED": "stop", "FAILED": "stop"}
MOON = {"done": "●", "active": "◐", "wait": "◔", "stop": "○"}      # the goal's state, as a phase of the moon


def phase(state: str | None) -> str:
    return PHASE.get(state or "", "wait")




# ------------------------------------------------------------------ colour

class Paint:
    """ANSI styling that degrades: 24-bit when the terminal says so, 256 colours otherwise, nothing
    for NO_COLOR, TERM=dumb or a pipe. Text keeps the terminal's own foreground; colour is used for
    the mark, the accent and state, never for body text."""

    # the logo's ink ramp: on a light ground navy (dense) to slate (sparse); on a dark ground the same
    # mark lit by the moon, silver (dense) to steel (sparse)
    RAMP = {"light": ((20, 33, 58), (138, 155, 179)), "dark": ((222, 230, 240), (94, 116, 145))}
    ACCENT = {"light": (61, 95, 201), "dark": (157, 181, 255)}
    STATE = {"light": {"done": (35, 144, 109), "active": None, "wait": (185, 122, 29), "stop": (138, 151, 171)},
             "dark": {"done": (95, 207, 159), "active": None, "wait": (232, 180, 90), "stop": (116, 134, 162)}}

    def __init__(self, stream=None, theme: str | None = None):
        stream = stream or sys.stdout
        term = os.environ.get("TERM", "")
        self.on = stream.isatty() and "NO_COLOR" not in os.environ and term != "dumb"
        self.truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        self.theme = theme or "dark"

    def rgb(self, text: str, colour: tuple[int, int, int] | None, bold: bool = False) -> str:
        if not self.on or not text:
            return text
        codes = ["1"] if bold else []
        if colour:
            if self.truecolor:
                codes.append("38;2;%d;%d;%d" % colour)
            else:
                r, g, b = (round(v / 255 * 5) for v in colour)
                codes.append("38;5;%d" % (16 + 36 * r + 6 * g + b))
        return f"\033[{';'.join(codes)}m{text}\033[0m" if codes else text

    def accent(self, text: str, bold: bool = False) -> str:
        return self.rgb(text, self.ACCENT[self.theme], bold)

    def faint(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self.on and text else text

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.on and text else text

    def state(self, ph: str, text: str) -> str:
        colour = self.STATE[self.theme][ph] or self.ACCENT[self.theme]
        return self.rgb(text, colour)

    def mark(self) -> list[str]:
        dense, sparse = self.RAMP[self.theme]
        out = []
        for row, shade in zip(wolfmark.ROWS, wolfmark.SHADE):
            if not self.on:
                out.append(row)
                continue
            line = ""
            for ch, s in zip(row, shade):
                if ch == " ":
                    line += ch
                    continue
                t = min(1.0, int(s) / 6)      # density -> position on the ramp
                colour = tuple(round(sparse[i] + (dense[i] - sparse[i]) * t) for i in range(3))
                line += self.rgb(ch, colour)
            out.append(line)
        return out


def background(timeout: float = 0.12) -> str:
    """'light' or 'dark'. LUPUS_THEME decides when set; then COLORFGBG; then the terminal is asked
    for its background colour (OSC 11), which most macOS terminals answer; otherwise dark."""
    forced = os.environ.get("LUPUS_THEME", "").lower()
    if forced in ("light", "dark"):
        return forced
    fgbg = os.environ.get("COLORFGBG", "")
    if ";" in fgbg:
        try:
            return "light" if int(fgbg.rsplit(";", 1)[1]) in (7, 15) else "dark"
        except ValueError:
            pass
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "dark"
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return "dark"
    reply = b""
    try:
        tty.setcbreak(fd)
        os.write(sys.stdout.fileno(), b"\033]11;?\033\\")
        while select.select([fd], [], [], timeout)[0]:
            reply += os.read(fd, 64)
            if reply.endswith((b"\033\\", b"\a")):
                break
    except OSError:
        return "dark"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    found = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not found:
        return "dark"
    r, g, b = (int(v[:2], 16) for v in found.groups())
    return "light" if (0.2126 * r + 0.7152 * g + 0.0722 * b) > 140 else "dark"


# ------------------------------------------------------------------ what the home screen shows

def cells(text: str) -> int:
    """Terminal columns `text` takes: East Asian wide characters take two."""
    return sum(0 if unicodedata.combining(ch) or unicodedata.category(ch) in ("Cc", "Cf") else
               2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in re.sub(r"\033\[[0-9;]*m", "", text))


def pad(text: str, columns: int) -> str:
    return text + " " * max(0, columns - cells(text))


def wrap(text: str, columns: int) -> list[str]:
    """Word wrap by terminal cells, so a Korean sentence fits as well as an English one."""
    columns = max(2, columns)
    lines, line = [], ""
    for word in text.split(" "):
        if cells(word) > columns:
            if line:
                lines.append(line)
                line = ""
            for token in re.findall(r"\033\[[0-9;]*m|.", word):
                if cells(line) + cells(token) > columns:
                    lines.append(line)
                    line = ""
                line += token
            continue
        if line and cells(line) + 1 + cells(word) > columns:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + [line] if line else lines


def head(text: str, columns: int) -> str:
    """The start of `text` in at most `columns` cells."""
    if cells(text) <= columns:
        return text
    if columns <= 0:
        return ""
    out = ""
    for ch in re.findall(r"\033\[[0-9;]*m|.", text):
        if cells(out) + cells(ch) > columns - 1:
            break
        out += ch
    return out + "…" + ("\033[0m" if "\033[" in out else "")


def folder(path: str, columns: int) -> str:
    """A path in at most `columns` cells, shortened at folder boundaries (…/parent/name), never mid-name."""
    if cells(path) <= columns:
        return path
    parts = path.split("/")
    kept = parts[-1]
    for part in reversed(parts[:-1]):
        if cells("…/" + part + "/" + kept) > columns:
            break
        kept = part + "/" + kept
    return head("…/" + kept, columns)


def tail(text: str, columns: int) -> str:
    """The end of `text` in at most `columns` cells (a path is read from its end)."""
    if cells(text) <= columns:
        return text
    out = ""
    for ch in reversed(text):
        if cells(out) + cells(ch) > columns - 1:
            break
        out = ch + out
    return "…" + out


def facts(home: Path, cwd: str, driver: str) -> dict:
    """Everything the home screen states, read from files and the database; no model, no test run."""
    from .cli import _repo_root
    out: dict = {"root": _repo_root(cwd), "driver": driver}
    try:
        found = runners.detect(Path(out["root"]), need_tests=False)
        out["runner"], out["has_tests"] = found["runner"], found["has_tests"]
    except LupusError:
        out["runner"], out["has_tests"] = None, False
    model, effort = adapters.claude_preference() if driver == "claude" else (None, None)
    out["model"], out["effort"] = model, effort
    k = Kernel(home)
    try:
        project = projects.resolve(k, cwd)
        out["project"] = project["name"] if project else None
        spec = providers.resolve(driver, home)
        out["protocol"] = spec.protocol
        out["probe"] = spec.protocol == "native" and bool(projects.capability_blockers(k, spec.driver))
        phases = {"done": 0, "active": 0, "wait": 0, "stop": 0}
        notes = trusted = 0
        if project:
            for row in k.q("SELECT status FROM goal WHERE project_id = ?", project["project_id"]):
                phases[phase(row["status"])] += 1
            for row in k.q("SELECT status FROM node WHERE (project_id = ? OR project_id IS NULL) AND status <> 'retired'",
                           project["project_id"]):
                notes += 1
                trusted += row["status"] in ("verified", "confirmed")
        out["phases"], out["notes"], out["trusted"] = phases, notes, trusted
    finally:
        k.close()
    return out


def home_screen(f: dict, paint: Paint, lang: str, width: int) -> list[str]:
    w = WORDS[lang]
    label = lambda key: paint.accent(pad(w[key], 10))        # noqa: E731
    root = f["root"].replace(str(Path.home()), "~", 1)
    room = max(12, width - (36 if width >= 64 else 4) - 10)     # what is left right of the mark and the label
    if f["project"] or f["runner"] is not None:
        name = head(f["project"] or Path(f["root"]).name, max(8, room // 2))
        rest = room - cells(name) - 2
        # the name already says the last folder, so the path beside it says where that folder is
        where = root.rsplit("/", 1)[0] or "/" if root.rsplit("/", 1)[-1] == name else root
        project_line = paint.bold(name) + "  " + paint.faint(folder(where, rest) if f["project"] else head(w["not_registered"], rest))
    else:
        project_line = paint.faint(w["not_here"]) + "  " + paint.faint(folder(root, room - cells(w["not_here"]) - 2))
    tests = w["tests_found"].format(runner=f["runner"]) if f["runner"] and f["has_tests"] else w["no_tests"]
    ph = f["phases"]
    goals = (w["none"] if not sum(ph.values()) else "  ".join(
        paint.state(key, MOON[key]) + " " + str(ph[key]) + " " + w["phase"][key] for key in ("done", "active", "wait") if ph[key]))
    worker = f["driver"]
    info = [
        paint.accent("L U P U S", bold=True),
        paint.faint(w["tagline"]),
        "",
        label("project") + project_line,
        label("tests") + (tests if f["has_tests"] else paint.faint(tests)),
        label("goals") + goals,
        label("memory") + (w["mem_line"].format(n=f["notes"], t=f["trusted"]) if f["notes"] else paint.faint(w["none"])),
        label("worker") + worker,
        label("model") + (f.get("model") or w["model_required" if f.get("protocol") == "openai-chat" else "host_default"]),
        label("mode") + f.get("mode", "auto"),
    ]
    art = paint.mark() if width >= 64 else []
    gap = 3
    art_width = max((len(r) for r in wolfmark.ROWS), default=0) if art else 0
    lines = []
    top = max(0, (len(art) - len(info)) // 2)
    for i in range(max(len(art), len(info) + top)):
        left = art[i] if i < len(art) else ""
        right = info[i - top] if 0 <= i - top < len(info) else ""
        spacer = " " * (art_width - (len(wolfmark.ROWS[i]) if i < len(art) else 0))
        prefix = " " + left + spacer + " " * gap if art else "  "
        lines.append(prefix + head(right, max(1, width - cells(prefix))))
    if f["probe"] and f.get("mode") == "verify":
        lines += ["", *("  " + paint.faint(part) for part in wrap(
            "→ " + w["probe"].format(cli=f["driver"]), max(2, width - 4)))]
    lines += ["", *("  " + part for part in wrap(w["intro"], max(2, width - 4))),
              *("  " + paint.faint(part) for part in wrap(w["commands"], max(2, width - 4))),
              *("  " + paint.faint(part) for part in wrap(w["tab_hint"], max(2, width - 4))), ""]
    return [line.rstrip() for line in lines]


# ------------------------------------------------------------------ the tabbed view

def model(k: Kernel, lang: str) -> dict:
    """The same data the web viewer is built from: graph export plus the vault pages."""
    data = graph.export(k)
    data["pages"] = vault.render(k)
    return data


def page_title(data: dict, path: str, lang: str) -> str:
    w = WORDS[lang]
    by_page = {n["page"]: n for n in data["nodes"] if n.get("page")}
    if path == "index.md":
        return w["home"]
    node = by_page.get(path)
    if node and node["kind"] != "project":
        return node["title"]
    if path.endswith("map.md"):
        return w["map"]
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def document_tree(data: dict, lang: str) -> list[tuple[int, str, str | None]]:
    """(depth, label, page or None for a folder heading), in reading order."""
    w = WORDS[lang]
    pages = sorted(data.get("pages", {}))
    out: list[tuple[int, str, str | None]] = []
    if "index.md" in pages:
        out.append((0, w["home"], "index.md"))
    for prefix, name in [("global", w["global"])] + [(f"projects/{pid}", nm) for pid, nm in data["scopes"]]:
        mine = [p for p in pages if p.startswith(prefix + "/")]
        if not mine:
            continue
        out.append((0, f"{name}  ({len(mine)})", None))
        for p in mine:
            if p == prefix + "/map.md":
                out.append((1, w["map"], p))
        for sub, label in (("goals", w["folder_goals"]), ("nodes", w["folder_nodes"])):
            group = [p for p in mine if p.startswith(f"{prefix}/{sub}/")]
            if group:
                out.append((1, f"{label}  ({len(group)})", None))
                out += [(2, page_title(data, p, lang), p) for p in group]
    return out


def markdown_lines(text: str) -> list[tuple[str, str]]:
    """A vault page for the terminal, as (text, style) lines: style is "title", "heading", "note"
    (the page's quoted lines), "head" (a table's header row) or "" for body text. Links read as
    their words; escapes and emphasis marks are dropped; a table row becomes its cells joined by ·."""
    out: list[tuple[str, str]] = []
    in_table = False
    for line in text.splitlines():
        if re.fullmatch(r"\|[-| :]+\|", line):
            continue
        style = ""
        if line.startswith("|"):
            # spaces inside a cell do not break: a row wraps only between cells
            line = "  ·  ".join(cell.strip().replace(" ", "\u00a0") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|")))
            style, in_table = ("" if in_table else "head"), True
        else:
            in_table = False
            if line.startswith("# "):
                line, style = line[2:], "title"
            elif line.startswith("## "):
                line, style = line[3:], "heading"
            elif line.startswith(">"):
                line, style = line[1:].strip(), "note"
            elif line.startswith("- "):
                line = "• " + line[2:]
        line = re.sub(r"(?<!\\)\[([^\]]*)\]\([^)]*\)", r"\1", line)
        out.append((line.replace("**", "").replace("\\", "").replace("&lt;", "<"), style))
    return out


def browse(home: Path, lang: str, start: int = 0, theme: str = "dark") -> None:
    """Full-screen tabs over the pack's memory: Overview · Goals · Knowledge · Documents."""
    import curses
    k = Kernel(home)
    try:
        data = model(k, lang)
    finally:
        k.close()
    curses.wrapper(lambda screen: _browse(screen, data, lang, start, theme))


def _browse(screen, data: dict, lang: str, tab: int, theme: str = "dark") -> None:
    import curses
    w = WORDS[lang]
    kinds = KIND_NAMES[lang]
    curses.curs_set(0)
    try:
        curses.set_escdelay(30)
    except AttributeError:
        pass
    try:
        curses.use_default_colors()
        # the home screen's accent and state colours, nearest in the 256-colour cube, per terminal ground
        cube = (lambda rgb: 16 + 36 * round(rgb[0] / 51) + 6 * round(rgb[1] / 51) + round(rgb[2] / 51))
        rich = curses.COLORS >= 256
        curses.init_pair(1, cube(Paint.ACCENT[theme]) if rich else curses.COLOR_BLUE, -1)
        curses.init_pair(2, cube(Paint.STATE[theme]["done"]) if rich else curses.COLOR_GREEN, -1)
        curses.init_pair(3, cube(Paint.STATE[theme]["wait"]) if rich else curses.COLOR_YELLOW, -1)
        accent, ok, warn = curses.color_pair(1), curses.color_pair(2), curses.color_pair(3)
    except curses.error:
        accent = ok = warn = 0
    state_attr = {"done": ok, "active": accent, "wait": warn, "stop": curses.A_DIM}
    notes = [n for n in data["nodes"] if n["kind"] in NOTES]
    goals = [n for n in data["nodes"] if n["kind"] == "goal"]
    names = dict(data["scopes"])
    tree = document_tree(data, lang)
    cursor = [0, 0, 0, next((i for i, t in enumerate(tree) if t[2]), 0)]
    reading: list[str] | None = None
    scroll = 0

    def put(y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = screen.getmaxyx()
        if 0 <= y < height and x < width:
            try:
                screen.addnstr(y, x, text, max(0, width - x - 1), attr)
            except curses.error:
                pass

    def rows_for(t: int) -> list:
        if t == 1:
            return [g for g in reversed(goals)]
        if t == 2:
            return sorted(notes, key=lambda n: (n["status"] == "retired", -n.get("helped", 0), n["title"]))
        if t == 3:
            return tree
        return []

    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 16 or width < 60:
            put(0, 0, w["too_small"])
            if screen.getch() in (ord("q"), 27):
                return
            continue
        # header: the name, then the tabs, then the keys
        put(0, 2, "L U P U S", accent | curses.A_BOLD)
        x = 15
        for i, name in enumerate(w["tabs"]):
            label = f" {name} "
            put(0, x, label, (curses.A_REVERSE | curses.A_BOLD) if i == tab else curses.A_DIM)
            x += cells(label) + 2
        put(height - 1, 2, w["keys"], curses.A_DIM)
        put(1, 2, "─" * (width - 4), curses.A_DIM)
        top, body = 3, height - 5
        if tab == 0:
            phases = {"done": 0, "active": 0, "wait": 0, "stop": 0}
            for g in goals:
                phases[phase(g.get("state"))] += 1
            count = {s: sum(1 for n in notes if n["status"] == s) for s in ("verified", "confirmed", "candidate", "retired")}
            live = len(notes) - count["retired"]
            put(top, 4, w["memory"], accent)
            put(top, 14, w["mem_line"].format(n=live, t=count["verified"] + count["confirmed"]), curses.A_BOLD)
            bar_w = min(48, width - 30)
            total = max(1, sum(count.values()))
            put(top + 3, 4, w["trust"], accent)
            x = 14
            for s, ch in (("verified", "█"), ("confirmed", "▓"), ("candidate", "░"), ("retired", "·")):
                n = round(bar_w * count[s] / total)
                put(top + 3, x, ch * n, accent if s != "retired" else curses.A_DIM)
                x += n
            put(top + 4, 14, "  ".join(f"{w['status'][s]} {count[s]}" for s in count), curses.A_DIM)
            put(top + 6, 4, w["goals"], accent)
            put(top + 6, 14, "  ".join(f"{MOON[p]} {phases[p]} {w['phase'][p]}" for p in ("done", "active", "wait", "stop")))
            put(top + 8, 4, w["kinds"], accent)
            for i, kind in enumerate(NOTES):
                n = sum(1 for x2 in notes if x2["kind"] == kind and x2["status"] != "retired")
                put(top + 8 + i, 14, f"{kinds[kind]:<12} {'■' * n} {n}")
            put(top + 14, 4, w["projects"], accent)
            for i, (pid, pname) in enumerate(data["scopes"][: max(0, body - 15)]):
                mine = [g for g in goals if g["scope"] == pid]
                done = sum(phase(g.get("state")) == "done" for g in mine)
                count_notes = sum(1 for x2 in notes if x2["scope"] == pid and x2["status"] != "retired")
                put(top + 14 + i, 14, pad(head(pname, 22), 24) + w["project_row"].format(g=len(mine), d=done, n=count_notes))
            if not data["nodes"]:
                put(top + 15, 14, w["nothing"], curses.A_DIM)
        else:
            if tab == 2:
                put(height - 2, 2, w["legend"], curses.A_DIM)
            rows = rows_for(tab)
            pos = cursor[tab] = max(0, min(cursor[tab], len(rows) - 1)) if rows else 0
            split = width // 2 if tab in (2, 3) else width
            first = max(0, pos - body + 1)
            for i, row in enumerate(rows[first:first + body]):
                y, here = top + i, first + i == pos
                attr = curses.A_REVERSE if here else 0
                if tab == 1:
                    ph = phase(row.get("state"))
                    put(y, 2, MOON[ph], state_attr[ph])
                    put(y, 4, pad(head(row["title"], split - 31), split - 30), attr)
                    put(y, split - 24, pad(w["phase"][ph], 9) + head(names.get(row["scope"], w["global"]), 14), curses.A_DIM)
                elif tab == 2:
                    mark = {"verified": "●", "confirmed": "◉", "candidate": "○", "retired": "·"}[row["status"]]
                    put(y, 2, mark, accent if row["status"] != "retired" else curses.A_DIM)
                    put(y, 4, head(row["title"], split - 8), attr)
                else:
                    depth, label, page = row
                    put(y, 2 + depth * 2, head(label if page else label.upper(), split - 6 - depth * 2),
                        attr if page else (accent | curses.A_BOLD if not here else attr))
            if tab in (2, 3):
                for y in range(top, top + body):
                    put(y, split, "│", curses.A_DIM)
                pane = []
                if tab == 2 and rows:
                    n = rows[pos]
                    pane = [(n["title"], "title"), (f"{kinds[n['kind']]} · {w['status'][n['status']]} · "
                            f"{names.get(n['scope'], w['global'])}", "note"), ("", "")] + [
                        (line, "") for line in n.get("body", "").splitlines()] + [
                        ("", ""), (w["use_line"].format(r=n.get("recalled", 0), h=n.get("helped", 0), u=n.get("unhelped", 0)), "note")]
                if tab == 3 and rows:
                    page = rows[pos][2]
                    pane = markdown_lines(data["pages"].get(page, "")) if page else []
                    if reading is not None:
                        pane = reading
                look = {"title": curses.A_BOLD, "heading": accent | curses.A_BOLD, "note": curses.A_DIM, "head": curses.A_BOLD, "": 0}
                wrapped = [(part, style) for line, style in pane for part in (wrap(line, width - split - 4) or [""])]
                for i, (line, style) in enumerate(wrapped[scroll:scroll + body]):
                    put(top + i, split + 2, line, look[style])
        screen.refresh()
        key = _key(screen, curses)
        if key in (ord("q"), 27):
            return
        if key in (9, curses.KEY_RIGHT):
            tab, scroll, reading = (tab + 1) % 4, 0, None
        elif key in (curses.KEY_BTAB, curses.KEY_LEFT):
            tab, scroll, reading = (tab - 1) % 4, 0, None
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor[tab] += 1
            scroll, reading = 0, None
        elif key in (curses.KEY_UP, ord("k")):
            cursor[tab] = max(0, cursor[tab] - 1)
            scroll, reading = 0, None
        elif key in (curses.KEY_NPAGE, ord(" ")):
            scroll += max(1, height - 8)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - (height - 8))
        elif key in (10, 13, curses.KEY_ENTER) and tab == 1:
            g = rows_for(1)[cursor[1]] if goals else None
            if g and g.get("page"):
                tab, reading, scroll = 3, markdown_lines(data["pages"].get(g["page"], "")), 0
        elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
            tab, scroll, reading = key - ord("1"), 0, None


def _key(screen, curses) -> int:
    """One key. Arrow keys come as ESC [ X or ESC O X depending on the terminal's cursor mode; a
    lone ESC (nothing follows within the escape delay) is ESC."""
    key = screen.getch()
    if key != 27:
        return key
    screen.nodelay(True)
    try:
        first = screen.getch()
        if first not in (ord("["), ord("O")):
            return 27 if first == -1 else first
        last = screen.getch()
        return {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN, ord("C"): curses.KEY_RIGHT,
                ord("D"): curses.KEY_LEFT, ord("Z"): curses.KEY_BTAB}.get(last, -1)
    finally:
        screen.nodelay(False)


# ------------------------------------------------------------------ the prompt

COMMANDS = ("/provider", "/providers", "/model", "/models", "/mode", "/ask", "/agent", "/verify",
            "/skills", "/skill", "/parallel", "/jobs", "/logs", "/stop", "/session", "/fix", "/view",
            "/status", "/driver", "/lang", "/graph", "/probe", "/help", "/quit", "/profile", "/skill-mode", "/supervisor")
MODES = ("auto", "ask", "agent", "verify")
# The ordinary CLI remains reachable even when a project has a skill of the same name.
# Keep all subcommands reserved, including commands intentionally absent from the short help.
RESERVED = frozenset(command[1:] for command in COMMANDS) | frozenset({
    "alpha-budget", "alpha-run", "alpha-status", "approve", "auto", "budget-raise", "cross-check", "do", "provider-allow",
    "fix-tests", "goal-list", "goal-priority", "goal-submit", "goal-pause", "goal-resume", "goal-cancel",
    "refreeze", "init", "job-run", "learn", "note-add", "note-link", "note-list", "note-search",
    "note-verify", "note-retire", "note-promote", "note-forget", "project-add", "project-allow-unconfined", "project-list",
    "project-remove", "prune", "recover", "release", "release-approve", "release-reject", "resolve",
    "revalidate", "revise", "revoke", "run", "vault-sync", "write", "diff", "accept", "discard", "undo",
    "tabs", "goals", "knowledge", "docs", "exit", "q",
})
_complete_for: Callable[[str, str], list[str]] | None = None


@dataclass
class Selection:
    driver: str
    mode: str = "auto"
    models: dict[str, str] = field(default_factory=dict)
    native_profiles: dict[str, str] = field(default_factory=dict)
    skill_modes: dict[str, str] = field(default_factory=dict)
    supervisor_role: dict | None = None

    @property
    def skill_mode(self) -> str:
        return self.skill_modes.get(self.driver, "auto")

    @property
    def native_profile(self) -> str:
        return self.native_profiles.get(self.driver, "lean")

    @property
    def model(self) -> str | None:
        return self.models.get(self.driver)

    @classmethod
    def read(cls, home: Path, driver: str) -> Selection:
        state = cls(driver)
        path = home / "shell_preferences.json"
        try:
            if path.is_symlink() or path.stat().st_size > 16_384:
                return state
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return state
            preferred = data.get("driver")
            if not os.environ.get("LUPUS_DRIVER") and isinstance(preferred, str):
                state.driver = providers.resolve(preferred, home).id
            if data.get("mode") in MODES:
                state.mode = data["mode"]
            profiles = data.get("native_profiles", {})
            if isinstance(profiles, dict) and profiles.get("codex") in ("lean", "user"):
                state.native_profiles["codex"] = profiles["codex"]
            policies = data.get("skill_modes", {})
            if isinstance(policies,dict) and len(policies)<=64:
                for key,value in policies.items():
                    try:
                        if value in ("auto","off") and providers.resolve(key,home).protocol!="native":state.skill_modes[key]=value
                    except LupusError:pass
            role=data.get("supervisor")
            if isinstance(role,dict):
                try:state.supervisor_role=supervision.resolve(role.get("provider"),role.get("model"),role.get("effort"),home=home)
                except LupusError:pass
            models = data.get("models", {})
            if isinstance(models, dict) and len(models) <= 64:
                for key, value in models.items():
                    try:
                        providers.resolve(key, home)
                        state.models[key] = providers.validate_model(value)
                    except LupusError:
                        continue
        except (OSError, ValueError, LupusError):
            pass
        return state

    def save(self, home: Path) -> None:
        atomic_write(home / "shell_preferences.json", json.dumps(
            {"driver": self.driver, "mode": self.mode, "models": self.models, "native_profiles": self.native_profiles, "skill_modes": self.skill_modes,"supervisor":self.supervisor_role}, ensure_ascii=False).encode())

    def flags(self) -> list[str]:
        return ["--driver", self.driver, *(["--model", self.model] if self.model else [])]


def _wrapper_option(args: list[str], name: str) -> tuple[bool, str | None]:
    """Inspect only the caller-supplied wrapper prefix, never the native payload."""
    found, value = False, None
    for index, arg in enumerate(args):
        if arg == name:
            found = True
            value = args[index+1] if index+1 < len(args) and not args[index+1].startswith("--") else None
        elif arg.startswith(name + "="):
            found, value = True, arg[len(name)+1:]
    return found, value


def _session_wrapper_end(args: list[str]) -> int:
    """Formal long wrapper options end at -- or argparse.REMAINDER's first positional."""
    values = {"--driver", "--check", "--protect", "--container", "--max-minutes"}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--" or not arg.startswith("-") or arg == "-":return index
        name, equals, _ = arg.partition("=")
        index += 2 if name in values and not equals else 1
    return len(args)


def completion_candidates(line: str, text: str, provider_ids=(), model_ids=(), skill_ids=()) -> list[str]:
    """Only local metadata is used for Tab. No model discovery or provider call."""
    words = line.split()
    cmd = words[0] if words else ""
    argument = len(words) > 1 or bool(line) and line[-1].isspace()
    position = len(words) if line and line[-1].isspace() else len(words)-1
    if cmd in ("/provider", "/providers", "/driver") and argument:
        if position == 1:
            choices = [*provider_ids, "allow"]
        elif position == 2 and len(words) > 1 and words[1] == "allow":
            choices = list(provider_ids)
        else:
            return []
    elif cmd == "/supervisor" and argument:
        choices=["off",*provider_ids] if position==1 else list(model_ids) if position==2 else list(adapters.CODEX_EFFORTS) if position==3 else []
    elif cmd == "/mode" and argument:
        choices = list(MODES)
    elif cmd == "/skill-mode" and argument:
        choices = ["auto", "off"] if position == 1 else []
    elif cmd == "/profile" and argument:
        choices = ["lean", "user"] if position == 1 else []
    elif cmd in ("/model", "/models") and argument:
        choices = ["default", *model_ids, *(ident + "/" for ident in provider_ids)]
    elif cmd in ("/skill", "/skills") and argument:
        if position == 1:
            choices = list(skill_ids) + (["show"] if cmd == "/skill" else [])
        elif cmd == "/skill" and position == 2 and len(words) > 1 and words[1] == "show":
            choices = list(skill_ids)
        else:
            return []
    elif cmd == "/parallel" and argument:
        choices = ["auto", "1", "2", "3", "4"]
    elif cmd == "/lang" and argument:
        choices = ["en", "ko"]
    elif not line.strip() or text.startswith("/") and not argument:
        choices = [*COMMANDS, *("/" + sid for sid in skill_ids if sid not in RESERVED)]
    else:
        return []
    matches = list(dict.fromkeys(choice for choice in choices if choice.startswith(text)))
    return sorted(matches, key=lambda choice: choice != text)


def _readline(history: Path) -> None:
    try:
        import readline
    except ImportError:
        return

    def complete(text: str, state: int):
        line = readline.get_line_buffer()
        options = _complete_for(line, text) if _complete_for else completion_candidates(line, text)
        return options[state] if state < len(options) else None

    readline.set_completer(complete)
    readline.set_completer_delims(" ")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(500)
    import atexit
    atexit.register(lambda: _save_history(readline, history))


def _save_history(readline, history: Path) -> None:
    try:
        readline.write_history_file(history)
    except OSError:
        pass


def run(options: list[str], home: Path, driver: str, main: Callable[[list[str]], int]) -> int:
    """The interactive shell. `main` is the CLI entry, so every action is an ordinary command."""
    lang = language()
    paint = Paint(theme=background())
    width = shutil.get_terminal_size((80, 24)).columns
    state = Selection.read(home, driver)
    cached_skills: dict[str, skills.Skill] | None = None

    def local_skills(refresh: bool = False) -> dict[str, skills.Skill]:
        nonlocal cached_skills
        if cached_skills is None or refresh:
            from .cli import _repo_root
            cached_skills = skills.discover(Path.cwd(), home, Path(_repo_root(os.getcwd())))
        return cached_skills

    def query_catalog(metadata: dict) -> dict:
        if "catalog" not in metadata:
            metadata["catalog"] = {item["id"]: item for item in providers.catalog(home)}
        return metadata["catalog"]

    def query_provider(driver: str, metadata: dict) -> dict:
        catalog = query_catalog(metadata)
        ident = driver[7:] if driver in ("native_codex","native_claude") else driver
        selected = next((item for item in catalog.values() if item["driver"] == ident),None) if isinstance(ident,str) and ident.startswith("api:") else catalog.get(ident)
        if selected is None:
            raise LupusError("DRIVER_UNKNOWN", "unknown provider; use lupus providers")
        return selected

    def query_models(driver: str, metadata: dict) -> list[str]:
        spec = query_provider(driver,metadata)
        return list(dict.fromkeys([*spec["models"], *([spec["default_model"]] if spec["default_model"] else [])]))

    def complete(line: str, text: str, metadata: dict | None = None) -> list[str]:
        metadata = {} if metadata is None else metadata
        try:
            catalog = query_catalog(metadata)
            words = line.split()
            command = words[0] if words else ""
            argument = len(words)>1 or bool(line) and line[-1].isspace()
            position = len(words) if line[-1:].isspace() else len(words)-1
            if line.startswith("/supervisor ") and position>=2 and words[1]!="off":
                role_provider=words[1]
                if position>=3:
                    spec=query_provider(role_provider,metadata)
                    efforts=adapters.CODEX_EFFORTS if spec['driver']=='native_codex' else adapters.CLAUDE_EFFORTS if spec['driver']=='native_claude' else ()
                    return [value for value in efforts if value.startswith(text)] if position==3 else []
                role_models=query_models(role_provider,metadata)
                if state.supervisor_role and state.supervisor_role["provider"]==role_provider:role_models += [state.supervisor_role["model"]]
                return completion_candidates(line,text,catalog,role_models)
            query_provider(state.driver,metadata)  # Preserve unknown-selection fallback without another configuration read.
            needs_models = argument and (command in ("/model","/models") or command=="/supervisor" and position==2)
            needs_skills = (argument and command in ("/skill","/skills")) or not line.strip() or text.startswith("/") and not argument
            return completion_candidates(line, text, catalog,
                                         [*query_models(state.driver,metadata), *([state.model] if state.model else [])] if needs_models else (),
                                         local_skills() if needs_skills else ())
        except LupusError:
            return completion_candidates(line, text)

    def suggest(line: str, cursor: int) -> list[prompt.Candidate]:
        before = line[:cursor]
        text = before.split()[-1] if before and not before[-1].isspace() else ""
        metadata = {}
        values = complete(before, text, metadata)
        command = before.split()[0] if before.split() else ""
        descriptions = {}
        for name, description in WORDS[lang]["help"]:
            if name.startswith("/"):
                descriptions.setdefault(name.split()[0], description)
        for alias, original in (("/providers", "/provider"), ("/driver", "/provider"), ("/logs", "/jobs"), ("/stop", "/jobs")):
            descriptions.setdefault(alias, descriptions.get(original, ""))
        catalog = query_catalog(metadata)
        found = local_skills() if not any(c.isspace() for c in before) or command in ("/skill","/skills") else {}
        if command in ("/model", "/models") and "/" in text:
            provider, _ = text.split("/", 1)
            if provider in catalog:
                models = query_models(provider,metadata) + ([state.models[provider]] if provider in state.models else [])
                values = list(dict.fromkeys(provider + "/" + model for model in models if (provider + "/" + model).startswith(text)))
        result = []
        for value in values:
            kind, description = "command", descriptions.get(value, "")
            if value.startswith("/") and value[1:] in found and value[1:] not in RESERVED:
                selected = found[value[1:]]
                kind, description = "skill", selected.description + " · " + selected.source
            elif command in ("/skill", "/skills") and value in found:
                selected = found[value]
                kind, description = "skill", selected.description + " · " + selected.source
            elif command in ("/provider", "/providers", "/driver") and value in catalog:
                kind, description = "provider", catalog[value]["name"] + " · " + catalog[value]["protocol"]
            elif command in ("/model", "/models") and not value.startswith("/"):
                kind, description = "model", "configured model" if value != "default" else "provider default"
            elif command == "/mode" and value in MODES:
                kind = "mode"
                description = {"auto":"short path when possible", "ask":"read-only answer", "agent":"edit · unverified", "verify":"approved checks"}[value]
            action = "insert"
            replacement = value
            command_position = not any(c.isspace() for c in before)
            if command_position:
                replacement = {"/models":"/model", "/providers":"/provider", "/driver":"/provider"}.get(value,value)
                if replacement in ("/mode", "/provider", "/model", "/lang", "/skill", "/profile", "/skill-mode", "/supervisor"):
                    action = "expand"
                elif replacement in ("/help", "/status", "/view", "/jobs", "/graph", "/skills", "/parallel", "/quit"):
                    action = "submit"
            elif command == "/supervisor":
                position=len(before.split()) if before.endswith(" ") else len(before.split())-1
                action="expand" if position==1 and value!="off" else "submit"
                kind="supervisor"
                description=("검증 실패 뒤에만 조언 · 주 모델 유지" if lang=="ko" else "advice after verified failure only · primary unchanged")
            elif command in ("/provider", "/providers", "/driver"):
                if value == "allow":
                    action, description = "expand", "request endpoint approval · no network request"
                elif value in catalog:
                    action = "submit"
                    if len(before.split()) >= 2 and before.split()[1] == "allow":
                        description = "request explicit endpoint approval"
            elif command in ("/mode", "/lang", "/profile", "/skill-mode"):
                action = "submit"
                if command == "/skill-mode":
                    description = (("API 주 작업 · 필요한 스킬만 탐색" if value=="auto" else "API 주 작업 · 명시적으로 선택한 자료만") if lang=="ko" else ("API primary only · on-demand skills" if value=="auto" else "API primary only · explicit selected skills remain available"))
                if command == "/profile":
                    description = (("agent 전용 · 외부 OS 샌드박스 · 내부 권한 미확인" if lang == "ko" else "agent only · outer OS sandbox · inner permissions unconfirmed") if value == "user" else "agent default · controlled native settings")
            elif command in ("/model", "/models"):
                action = "expand" if value.endswith("/") else "submit"
            elif command == "/skill" and value == "show":
                action = "expand"
            elif command == "/skill" and len(before.split()) >= 2 and before.split()[1] == "show":
                action = "submit"
            elif command == "/skills" and value in found:
                action = "submit"
            elif command == "/parallel":
                description = (("시작 시 보수적 상한 선택 · Enter로 명시 실행" if lang == "ko" else "conservative run-start cap · Enter again starts background work") if value == "auto" else "fill job count · Enter again explicitly starts background work")
            result.append(prompt.Candidate(replacement, value, description, kind, action))
        return result

    global _complete_for
    _complete_for = complete
    _readline(home / "shell_history")
    history = []
    try:
        import readline
        history = [readline.get_history_item(i) for i in range(1, readline.get_current_history_length()+1)]
    except ImportError:
        readline = None

    def say(text: str, *, faint: bool = False) -> None:
        for part in wrap(skills.plain(text), max(2, width - 4)):
            print("  " + (paint.faint(part) if faint else part))

    def chosen() -> None:
        w = WORDS[lang]
        spec = providers.resolve(state.driver, home)
        model_name = state.model or spec.default_model or w["host_default" if spec.protocol == "native" else "model_required"]
        say(f"{w['selected']}: {state.driver} · {w['model']}: {model_name} · {w['mode']}: {state.mode}")
        if state.supervisor_role:
            role=state.supervisor_role
            say(("필요 시 감독: " if lang=="ko" else "On-failure supervisor: ")+role["provider"]+" / "+role["model"],faint=True)
        if state.mode == "agent":
            say(w["unverified"], faint=True)
        if state.native_profile == "user":
            say("agent profile: user · agent only · configuration/backend not confirmed", faint=True)

    def save() -> None:
        try:
            state.save(home)
        except OSError:
            say("Selection is active; preferences could not be saved.", faint=True)
        chosen()

    def show() -> None:
        f = facts(home, os.getcwd(), state.driver)
        spec = providers.resolve(state.driver, home)
        f["model"] = state.model or spec.default_model or f["model"]
        f["mode"] = state.mode
        print("\n".join(home_screen(f, paint, lang, width)))

    def request(text: str, mode: str | None = None, skill_id: str | None = None) -> None:
        selected = mode or state.mode
        command = {"auto": "auto", "ask": "ask", "agent": "agent", "verify": "do"}[selected]
        _call(main, [*options, command, text, *state.flags(), *(supervision.flags(state.supervisor_role) if command in ("auto","do") else []), *(["--skills",state.skill_mode] if state.driver in state.skill_modes else []), *(["--native-config", "user"] if selected == "agent" and state.native_profile == "user" else []), *(["--skill",skill_id] if skill_id else []), *([] if selected == "ask" else ["--shell"])])

    def skill_request(sid: str, text: str) -> None:
        selected = local_skills().get(sid)
        if selected is None:
            raise LupusError("SKILL_UNKNOWN", "Use /skills to see local IDs.")
        say(f"{selected.id} · {selected.source} · {selected.path}", faint=True)
        request(text, skill_id=selected.id)

    show()
    while True:
        width = shutil.get_terminal_size((80, 24)).columns
        try:
            if prompt.available():
                line = prompt.read_line("› ", suggest, paint, history)
                if line.strip():
                    history.append(line)
                    history[:] = history[-500:]
                    if readline is not None:
                        readline.add_history(line)
                line = line.strip()
            else:
                line = input(paint.accent("› ", bold=True)).strip()
        except EOFError:
            print("\n" + paint.faint(WORDS[lang]["bye"]))
            return 0
        except prompt.InputCancelled:
            continue
        except KeyboardInterrupt:
            print("\n" + paint.faint("중단했습니다. Lupus를 종료합니다." if lang == "ko" else "Interrupted. Exiting Lupus."))
            return 130
        if not line:
            continue
        w = WORDS[lang]
        if not line.startswith("/"):
            request(line)
            continue
        try:
            raw_parts=line[1:].split(None,1)
            raw_cmd=raw_parts[0] if raw_parts else "help"
            remainder=raw_parts[1] if len(raw_parts)>1 else ""
            free_task=raw_cmd in ("ask","agent","verify") or (raw_cmd not in RESERVED and raw_cmd in local_skills())
            if free_task:
                words=[raw_cmd,*([remainder] if remainder else [])]
            elif raw_cmd=="skill" and remainder and remainder.split(None,1)[0]!="show":
                stream=io.StringIO(remainder)
                lexer=shlex.shlex(stream,posix=True);lexer.whitespace_split=True;lexer.commenters=''
                sid=lexer.get_token()
                task=stream.read().lstrip()
                words=[raw_cmd,sid,*([task] if task else [])]
            else:
                words = shlex.split(line[1:])
        except ValueError as exc:
            say(str(exc))
            continue
        cmd, args = (words[0], words[1:]) if words else ("help", [])
        if cmd in ("quit", "exit", "q"):
            print(paint.faint(w["bye"]))
            return 0
        try:
            if cmd == "help":
                chosen()
                for name, text in w["help"]:
                    print("\n  " + paint.accent(head(name, max(2, width - 4))))
                    for part in wrap(text, max(2, width - 6)):
                        print("    " + part)
            elif cmd == "status":
                if args:
                    _call(main, [*options, "status", *args])
                else:
                    show()
            elif cmd in ("provider", "providers", "driver"):
                if not args:
                    chosen()
                    for item in providers.catalog(home):
                        marker = "›" if item["id"] == state.driver else "·"
                        say(f"{marker} {item['id']} · {item['name']}")
                        if lang == "ko":
                            status = ("CLI 있음" if item["available"] else "CLI 설치 필요") if item["protocol"] == "native" else (
                                "연결 주소 설정 필요" if not item["base_url"] else
                                f"{item['api_key_env']} 설정 필요" if not item["available"] else "설정 있음")
                            say(status + " · 실행 미확인", faint=True)
                        else:
                            say(("endpoint configuration required" if item["protocol"] != "native" and not item["base_url"]
                                 else item["availability_reason"]) + " · execution not verified", faint=True)
                elif len(args) == 2 and args[0] == "allow":
                    ident = providers.resolve(args[1], home).id
                    _call(main, [*options, "provider-allow", ident])
                elif len(args) == 1:
                    state.driver = providers.resolve(args[0], home).id
                    save()
                else:
                    say("/provider ID")
            elif cmd == "supervisor":
                if not args:
                    role=state.supervisor_role
                    say(("감독: " if lang=="ko" else "Supervisor: ")+(role["provider"]+" / "+role["model"] if role else "off"))
                    say("/supervisor off | PROVIDER MODEL [EFFORT]",faint=True)
                elif args==["off"]:
                    state.supervisor_role=None;save()
                elif len(args) in (2,3):
                    state.supervisor_role=supervision.resolve(args[0],args[1],args[2] if len(args)==3 else None,home=home)
                    save()
                else:say("/supervisor off | PROVIDER MODEL [EFFORT]")
            elif cmd == "skill-mode":
                if providers.resolve(state.driver,home).protocol=="native":
                    say("/skill-mode는 API 주 작업에만 적용됩니다. 네이티브 스킬은 네이티브 프로필을 따릅니다" if lang=="ko" else "/skill-mode applies to API primary workers only; native skills follow the native profile")
                elif not args:
                    say(("/skill-mode auto|off · API 주 작업 전용 · 현재: " if lang=="ko" else "/skill-mode auto|off · API primary only · current: ") + state.skill_mode)
                elif len(args)==1 and args[0] in ("auto","off"):
                    state.skill_modes[state.driver]=args[0]
                    save()
                    say(("API 스킬 접근: " if lang=="ko" else "API skill access: ") + state.skill_mode + (" · 주 작업에만 적용" if lang=="ko" else " · primary workers only"),faint=True)
                else:say("/skill-mode auto|off")
            elif cmd == "profile":
                if not args:
                    say("/profile lean|user · agent only · Codex user profile requires the OS sandbox")
                elif len(args) == 1 and args[0] in ("lean", "user"):
                    adapters.validate_native_config(state.driver, args[0])
                    state.native_profiles[state.driver] = args[0]
                    save()
                else:
                    say("/profile lean|user")
            elif cmd == "mode":
                if not args:
                    chosen()
                    say("/mode " + "|".join(MODES))
                elif len(args) == 1 and args[0] in MODES:
                    state.mode = args[0]
                    save()
                else:
                    say("/mode " + "|".join(MODES))
            elif cmd == "model":
                if not args:
                    chosen()
                    say("/model MODEL · /models · /model default", faint=True)
                elif len(args) == 1:
                    candidate_driver, candidate_model = state.driver, args[0]
                    if "/" in candidate_model:
                        prefix, remainder = candidate_model.split("/", 1)
                        if prefix in {item["id"] for item in providers.catalog(home)}:
                            candidate_driver, candidate_model = prefix, remainder
                    if candidate_model != "default":
                        candidate_model = providers.validate_model(candidate_model)
                    state.driver = candidate_driver
                    if candidate_model == "default":
                        state.models.pop(state.driver, None)
                    else:
                        state.models[state.driver] = candidate_model
                    save()
                else:
                    say("/model MODEL")
            elif cmd == "models":
                if args not in ([], ["--discover"]):
                    say("/models [--discover]")
                    continue
                result = providers.models(state.driver, home, discover=bool(args))
                chosen()
                model_ids = list(dict.fromkeys([*result["models"], *([state.model] if state.model else [])]))
                for item in model_ids:
                    suffix = " · " + w["requested_model"] if item not in result["models"] else ""
                    say(("› " if item == state.model else "· ") + item + suffix)
                if not model_ids:
                    say(w["no_models"], faint=True)
            elif cmd == "skills":
                query = " ".join(args).casefold()
                matches = [s for s in local_skills(True).values()
                           if query in (s.id + " " + s.name + " " + s.description).casefold()]
                if not matches:
                    say(w["skill_empty"])
                for item in matches:
                    alias = "/skill " + item.id if item.id in RESERVED else "/" + item.id
                    say(f"{alias} · {item.source}" + (f" · overrides {len(item.shadowed)}" if item.shadowed else ""))
                    if item.description:
                        say(item.description, faint=True)
                if matches:
                    say("/skill show ID · /skill ID TASK", faint=True)
            elif cmd == "skill":
                if len(args) == 2 and args[0] == "show":
                    selected = local_skills(True).get(args[1])
                    if selected is None:
                        raise LupusError("SKILL_UNKNOWN", "Use /skills to see local IDs.")
                    say(str(selected.path), faint=True)
                    for part in skills.load(selected).splitlines():
                        say(part)
                elif len(args) > 1:
                    skill_request(args[0], args[1])
                else:
                    say("/skill ID TASK · /skills")
            elif cmd in ("ask", "agent", "verify"):
                if args:
                    request(line.split(None,1)[1], cmd)
                else:
                    say("/" + cmd + " TASK · /mode " + cmd)
            elif cmd == "parallel":
                say(w["parallel_hint"], faint=True)
                if not args:
                    _call(main, [*options, "alpha-status"])
                elif args[0] in ("auto", "1", "2", "3", "4"):
                    rest = args[1:]
                    boundary = rest.index("--") if "--" in rest else len(rest)
                    prefix = rest[:boundary]
                    explicit_driver = _wrapper_option(prefix, "--drivers")[0]
                    forwarded = []
                    if not explicit_driver:
                        if not _wrapper_option(prefix, "--model")[0] and state.model:forwarded += ["--model",state.model]
                        if state.driver in state.skill_modes and not _wrapper_option(prefix, "--skills")[0]:forwarded += ["--skills",state.skill_mode]
                    _call(main, [*options, "alpha-run", *([] if explicit_driver else ["--drivers",state.driver]), "--parallel", args[0],
                                 "--background", *forwarded, *rest])
                else:
                    say("/parallel [auto|1..4]")
            elif cmd in ("view", "tabs", "goals", "knowledge", "docs"):
                start = {"goals": 1, "knowledge": 2, "docs": 3}.get(cmd, 0)
                try:
                    browse(home, lang, start, paint.theme)
                except Exception as exc:
                    say(f"view unavailable: {type(exc).__name__}: {exc}")
            elif cmd == "lang":
                if not args:
                    show()
                elif len(args) == 1 and args[0] in WORDS:
                    lang = args[0]
                    os.environ["LUPUS_LANG"] = lang
                    show()
                else:
                    say("/lang en|ko")
            elif cmd == "session":
                at = args.index("--") if "--" in args else len(args)
                prefix = args[:_session_wrapper_end(args)]
                explicit_driver, selected_driver = _wrapper_option(prefix, "--driver")
                effective_driver = selected_driver if explicit_driver else state.driver
                if effective_driver is not None and providers.resolve(effective_driver, home).protocol != "native":
                    say("/session requires a native CLI. Use /agent for API providers.")
                    continue
                if not explicit_driver and not _wrapper_option(prefix, "--model")[0] and state.model:
                    args = [*args[:at], "--", "--model", state.model, *args[at + 1:]]
                _call(main, [*options, "session", *([] if explicit_driver else ["--driver",state.driver]), *args])
            elif cmd == "fix":
                boundary = args.index("--") if "--" in args else len(args)
                prefix = args[:boundary]
                forwarded = []
                if not _wrapper_option(prefix, "--driver")[0]:
                    forwarded += ["--driver",state.driver]
                    if not _wrapper_option(prefix, "--model")[0] and state.model:forwarded += ["--model",state.model]
                    if state.driver in state.skill_modes and not _wrapper_option(prefix, "--skills")[0]:forwarded += ["--skills",state.skill_mode]
                role = [] if _wrapper_option(prefix, "--supervisor")[0] else supervision.flags(state.supervisor_role)
                _call(main, [*options, "fix-tests", *forwarded, *role, "--shell", *args])
            elif cmd == "probe":
                _call(main, [*options, "probe", "--live", "--driver", state.driver, *args])
                show()
            elif cmd == "graph":
                _call(main, [*options, "graph", *([] if "--open" in args or "--serve" in args else ["--open"]), *args])
            elif cmd not in RESERVED and cmd in local_skills():
                parts = line.split(None,1)
                task = parts[1] if len(parts)>1 else ""
                if task:
                    skill_request(cmd, task)
                else:
                    say("/" + cmd + " TASK")
            else:
                if cmd in ("auto", "do", "run", "fix-tests", "write"):
                    boundary=args.index("--") if "--" in args else len(args)
                    prefix,suffix=args[:boundary],args[boundary:]
                    has_driver = any(arg == "--driver" or arg.startswith("--driver=") for arg in prefix)
                    has_model = any(arg == "--model" or arg.startswith("--model=") for arg in prefix)
                    if not has_driver:
                        prefix += ["--driver",state.driver]
                        if not has_model and state.model:prefix += ["--model",state.model]
                        if state.driver in state.skill_modes and not any(arg=="--skills" or arg.startswith("--skills=") for arg in prefix):prefix += ["--skills",state.skill_mode]
                    if cmd in ("auto","do","run","fix-tests") and not any(arg=="--supervisor" or arg.startswith("--supervisor=") for arg in prefix):prefix += supervision.flags(state.supervisor_role)
                    args=prefix+suffix
                    # An explicit provider keeps its own CLI/default model; never attach
                    # the currently selected provider's remembered model to it.
                _call(main, [*options, cmd, *args])
        except LupusError as exc:
            say(str(exc))


def _call(main: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return main(argv)
    except SystemExit as exc:          # argparse refuses bad arguments by exiting; the shell stays
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    except KeyboardInterrupt:
        print()
        return 130
