"""The dedicated Lupus vault (§3.2, §8, FOLDER-01/02).

A folder of plain Markdown pages that mirrors the memory graph: one page per knowledge node,
linked to the nodes it relates to and to the goals that recorded or used it. Plain relative
links, so any Markdown viewer can walk the graph; nothing depends on a particular app.

    index.md                         all scopes, recent changes
    global/map.md                    global knowledge by kind
    global/nodes/<node>.md
    projects/<project>/map.md        this project's knowledge by kind, and its goals
    projects/<project>/nodes/<node>.md
    projects/<project>/goals/<goal>.md   live status, evidence, budget, knowledge recorded/used

One-way: DB -> pages. Nothing here is read back, so editing a page cannot complete a goal,
approve an action, change a budget or alter knowledge (use `lupus note-*` for that). Only .md
files are written: no project code, patches, credentials or transcripts. Pages that no longer
correspond to anything are removed, but only if Lupus wrote them and nobody changed them since.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import goals, memory, projects, recovery, usage
from .kernel import Kernel
from .util import LupusError, atomic_write, find_secret, sha256_bytes

MANIFEST = ".lupus-vault.json"
BANNER = "> Lupus가 운영 DB에서 생성한 읽기 전용 문서입니다. 이 파일을 고쳐도 상태·승인·예산·지식은 바뀌지 않습니다.\n"
KIND_KO = {"lesson": "교훈", "decision": "결정", "fact": "사실", "procedure": "절차", "preference": "선호"}
STATUS_KO = {"verified": "사용자 확인", "confirmed": "반복 사용됨", "candidate": "미검증", "retired": "은퇴"}
ORIGIN_KO = {"user": "사용자", "supervisor": "supervisor", "worker": "작업 AI"}
EDGE_OUT = {"supersedes": "대체함", "derived_from": "여기서 파생됨", "contradicts": "상충", "part_of": "상위 항목", "relates": "관련"}
EDGE_IN = {"supersedes": "대체됨", "derived_from": "파생된 항목", "contradicts": "상충", "part_of": "세부 항목", "relates": "관련"}


def default_root(k: Kernel) -> Path:
    return k.home / "vault"


def _ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


_MD_ACTIVE = re.compile(r"([\\`*_\[\]()!#|~>])")


def _text(value: str) -> str:
    """Stored text is data. Every character Markdown could read as markup is escaped, so text
    written by a worker cannot become a link, an image that loads from the network, HTML, a
    heading or a table cell boundary."""
    return _MD_ACTIVE.sub(r"\\\1", value.replace("\n", " ")).replace("<", "&lt;")


def node_page(node: dict) -> str:
    scope = f"projects/{node['project_id']}" if node["project_id"] else "global"
    return f"{scope}/nodes/{node['node_id']}.md"


def goal_page(project_id: str, goal_id: str) -> str:
    return f"projects/{project_id}/goals/{goal_id}.md"


def _link(from_page: str, to_page: str, label: str) -> str:
    rel = os.path.relpath(to_page, os.path.dirname(from_page))
    return f"[{_text(label)}]({rel})"


# ---------------------------------------------------------------- pages

def _node_md(k: Kernel, node: dict) -> str:
    page = node_page(node)
    lines = [f"# {_text(node['title'])}", "", BANNER,
             f"- 종류: {KIND_KO[node['kind']]} · 상태: **{STATUS_KO[node['status']]}** · 기록: {ORIGIN_KO[node['origin']]}"
             f" · 범위: {'전역' if node['project_id'] is None else '프로젝트'}",
             f"- 회상 {node['recalled']}회 · 도움 {node['helped']} · 도움 안 됨 {node['unhelped']}"
             " (도움 = 이 기록이 주어진 시도가 검증을 통과함. 인과의 증명은 아님)"]
    if node["status"] == "retired":
        lines.append(f"- 은퇴 사유: {_text(node['retired_reason'] or '')}")
    if node["source_goal_id"] and node["project_id"]:
        lines.append("- 출처: " + _link(page, goal_page(node["project_id"], node["source_goal_id"]), "기록한 목표"))
    lines += ["", "## 내용", "", "> " + _text(node["body"]), ""]
    related = memory.edges(k, [node["node_id"]])
    if related:
        lines += ["## 연결", ""]
        for edge in related:
            outgoing = edge["src"] == node["node_id"]
            other = memory.get(k, edge["dst"] if outgoing else edge["src"])
            label = (EDGE_OUT if outgoing else EDGE_IN)[edge["type"]]
            lines.append(f"- {label}: {_link(page, node_page(other), other['title'])}")
        lines.append("")
    uses = k.q("SELECT r.goal_id, r.outcome, r.via, r.created_at, g.project_id FROM recall r JOIN goal g "
               "ON g.goal_id = r.goal_id WHERE r.node_id = ? ORDER BY r.recall_id DESC LIMIT 8", node["node_id"])
    if uses:
        lines += ["## 사용 이력 (최근)", ""]
        for use in uses:
            lines.append(f"- {_ts(use['created_at'])} · {_link(page, goal_page(use['project_id'], use['goal_id']), '목표')}"
                         f" · 시도 결과 {use['outcome'] or '진행 중'} · {'직접 일치' if use['via'] == 'match' else '연결로 포함'}")
        lines.append("")
    return "\n".join(lines)


def _map_md(k: Kernel, title: str, page: str, project_id: str | None) -> str:
    lines = [f"# {_text(title)} — 지식 지도", "", BANNER]
    all_nodes = memory.nodes(k, project_id)
    if not all_nodes:
        lines += ["아직 기록된 지식이 없습니다.", ""]
    for kind in memory.KINDS:
        group = [n for n in all_nodes if n["kind"] == kind]
        if not group:
            continue
        lines += [f"## {KIND_KO[kind]}", "", "| 항목 | 상태 | 기록 | 회상 / 도움 / 안 됨 |", "|---|---|---|---|"]
        for node in group:
            lines.append(f"| {_link(page, node_page(node), node['title'])} | {STATUS_KO[node['status']]} |"
                         f" {ORIGIN_KO[node['origin']]} | {node['recalled']} / {node['helped']} / {node['unhelped']} |")
        lines.append("")
    if project_id is not None:
        rows = k.q("SELECT goal_id, objective, status FROM goal WHERE project_id = ? ORDER BY created_at", project_id)
        if rows:
            lines += ["## 목표", ""]
            lines += [f"- {_link(page, goal_page(project_id, r['goal_id']), r['objective'])} — {r['status']}" for r in rows]
            lines.append("")
    return "\n".join(lines)


def _goal_md(k: Kernel, project_id: str, goal_id: str) -> str:
    page = goal_page(project_id, goal_id)
    view = goals.view(k, goal_id)
    ready = recovery.readiness(k, goal_id)
    cp = recovery.latest(k, goal_id)
    used = usage.totals(k, goal_id)
    last_run = k.one("SELECT execution_driver, status FROM run WHERE goal_id = ? ORDER BY fencing_token DESC LIMIT 1",
                     goal_id)
    handoff = k.one("SELECT source_adapter, target_adapter, status, created_at FROM handoff WHERE goal_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", goal_id)
    intents = k.q("SELECT intent_id, status FROM action_intent WHERE goal_id = ? AND status IN ('DISPATCHED','UNKNOWN')",
                  goal_id)
    lines = [
        f"# {_text(view['objective'])}", "", BANNER,
        f"- 목표 `{goal_id}` · 상태 **{view['status']}** · 완료 조건 revision {view['acceptance_revision']}",
        f"- 마지막 checkpoint: revision {cp['revision'] if cp else 0} · {_ts(cp['created_at']) if cp else '-'}"
        f" · {cp['state'] if cp else '-'} · 다음 행동: {_text(cp['payload']['next_action']) if cp else '-'}",
        f"- 최근 실행 AI: {last_run['execution_driver'] if last_run else '-'} ({last_run['status'] if last_run else '-'})",
        f"- 재개 가능: **{'예' if ready['ready'] else '아니오'}**"
        + ("" if ready["ready"] else f" · 차단 사유: {', '.join(ready['blockers'])}"),
        f"- 구독 잔량: {ready['subscription_remaining']} (관측 불가)",
    ]
    if handoff:
        lines.append(f"- 최근 인계: {handoff['source_adapter']} → {handoff['target_adapter']} · {handoff['status']} ·"
                     f" {_ts(handoff['created_at'])} (인증 정보는 인계에 포함되지 않음)")
    lines += ["", "## 완료 조건과 검증", "", "| 조건 | 최신 증거 |", "|---|---|"]
    lines += [f"| {_text(c['text'])} | {view['evidence'][c['id']] or '미검증'} |" for c in view["criteria"]]
    lines += ["", "## 작업", "", "| 작업 | 상태 | 시도 | 무진전 연속 | 대기 사유 |", "|---|---|---|---|---|"]
    lines += [f"| {_text(t['title'])} | {t['status']} | {t['attempt_count']} | {t['no_progress_streak']} |"
              f" {_text(t['wait_reason'] or '')} |" for t in view["tasks"]]
    lines += ["", "## 예산 (정책상 상한 기준)", "", "| 항목 | 사용 | 예약 | 안전 예약 | 상한 |", "|---|---|---|---|---|"]
    lines += [f"| {dim} | {v['used']} | {v['reserved']} | {v['safety_reserved']} | {v['cap']} |"
              for dim, v in view["budget"].items()]
    lines += ["", f"- 관측된 사용량: {used['totals']} · 관측 수준별 이벤트: {used['events_by_observation'] or '없음'}"]
    recorded = k.q("SELECT * FROM node WHERE source_goal_id = ? ORDER BY created_at", goal_id)
    recalled = k.q("SELECT DISTINCT n.* FROM recall r JOIN node n ON n.node_id = r.node_id WHERE r.goal_id = ?", goal_id)
    if recorded:
        lines += ["", "## 이 목표에서 기록한 지식", ""]
        lines += [f"- {_link(page, node_page(dict(n)), n['title'])} ({STATUS_KO[n['status']]})" for n in recorded]
    if recalled:
        mem = memory.summary(k, goal_id)
        lines += ["", "## 이 목표에서 참고한 지식", "",
                  f"추정 추가 입력 {mem['estimated_prompt_tokens']} 토큰 · 시도 결과별 {mem['by_attempt_outcome']}", ""]
        lines += [f"- {_link(page, node_page(dict(n)), n['title'])}" for n in recalled]
    if ready["notes"]:
        lines += ["", "## 확인 필요", "", *[f"- {n}" for n in ready["notes"]],
                  f"- 변경됨: {ready['workspace']['changed']} · 사라짐: {ready['workspace']['missing']}"]
    if intents:
        lines += ["", "## 미확정 외부 동작 (재실행 금지, 영수증 대조 필요)", "",
                  *[f"- `{i['intent_id']}` {i['status']}" for i in intents]]
    changed_at = max(view["updated_at"], cp["created_at"] if cp else 0)
    lines += ["", f"마지막 상태 변경: {_ts(changed_at)} · goal revision {view['revision']}", ""]
    return "\n".join(lines)


def _index_md(k: Kernel) -> str:
    page = "index.md"
    lines = ["# Lupus 지식 Vault", "", BANNER,
             "지식은 작은 노드와 그 사이의 연결로 저장됩니다. 각 문서의 링크를 따라가면 그래프를 걸을 수 있고, "
             "`lupus graph`로 전체를 한 화면에서 볼 수 있습니다. 지식을 추가·수정하려면 `lupus note-*` 명령을 씁니다.", "",
             "## 범위", "",
             f"- {_link(page, 'global/map.md', '전역 지식')} — {len(memory.nodes(k, None, False))}개"]
    for project in k.q("SELECT project_id, name FROM project ORDER BY created_at"):
        count = len(memory.nodes(k, project["project_id"], False))
        lines.append(f"- {_link(page, 'projects/' + project['project_id'] + '/map.md', project['name'])} — {count}개")
    recent = k.q("SELECT * FROM node ORDER BY updated_at DESC, rowid DESC LIMIT 10")
    if recent:
        lines += ["", "## 최근 변경", ""]
        lines += [f"- {_link(page, node_page(dict(n)), n['title'])} ({STATUS_KO[n['status']]})" for n in recent]
    lines.append("")
    return "\n".join(lines)


def render(k: Kernel) -> dict[str, str]:
    """Every page the vault should contain right now: {relative path: markdown}."""
    pages = {"index.md": _index_md(k), "global/map.md": _map_md(k, "전역", "global/map.md", None)}
    for node in memory.nodes(k, None):
        pages[node_page(node)] = _node_md(k, node)
    for project in k.q("SELECT project_id, name FROM project ORDER BY created_at"):
        pid = project["project_id"]
        pages[f"projects/{pid}/map.md"] = _map_md(k, project["name"], f"projects/{pid}/map.md", pid)
        for node in memory.nodes(k, pid):
            pages[node_page(node)] = _node_md(k, node)
        for goal in k.q("SELECT goal_id FROM goal WHERE project_id = ? ORDER BY created_at", pid):
            pages[goal_page(pid, goal["goal_id"])] = _goal_md(k, pid, goal["goal_id"])
    return pages


# ---------------------------------------------------------------- writing

def _check_root(k: Kernel, vault_root: str | Path) -> Path:
    root = Path(os.path.realpath(vault_root))
    if not root.is_dir():
        raise LupusError("VAULT_NOT_FOUND", str(root))
    for row in k.q("SELECT canonical_root FROM project"):
        if projects._within(str(root), row["canonical_root"]):
            # A vault inside a project would mix memory pages into the real work tree.
            raise LupusError("VAULT_INSIDE_PROJECT", row["canonical_root"])
    if projects._within(str(root), os.path.realpath(k.runtime)):
        raise LupusError("VAULT_INSIDE_RUNTIME", str(k.runtime))
    return root


def _target(root: Path, rel: str, create: bool) -> Path:
    """Destination of a page. Every path component is checked BEFORE anything is created, so a
    symlink planted inside the vault can never make Lupus create or write something outside."""
    parts = Path(rel).parts
    if not rel.endswith(".md") or any(part in ("", ".", "..") for part in parts):
        raise LupusError("VAULT_ONLY_MARKDOWN", rel)
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise LupusError("VAULT_PATH_ESCAPE", rel)
        if create and not current.exists():
            current.mkdir()
    target = current / parts[-1]
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise LupusError("VAULT_PATH_ESCAPE", rel)
    return target


def sync(k: Kernel, vault_root: str | Path | None = None) -> dict[str, int]:
    """Bring the vault in line with the database. Idempotent; unchanged pages are not rewritten."""
    root = _check_root(k, vault_root if vault_root is not None else default_root(k))
    pages = render(k)
    for rel, text in pages.items():
        leak = find_secret(text)
        if leak:
            raise LupusError("VAULT_CONTENT_SECRET", f"{leak} in {rel}")
    manifest_path = root / MANIFEST
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        previous = {}
    # Nothing is written until every destination is known to be safe and ours: a page may only
    # replace a file Lupus itself wrote earlier (it is in the manifest), never somebody's file.
    foreign = [rel for rel in pages if _target(root, rel, create=False).exists() and rel not in previous]
    if foreign:
        raise LupusError("VAULT_FOREIGN_FILE", f"{len(foreign)} existing file(s) not written by Lupus, e.g. {foreign[0]}")
    written = unchanged = removed = kept = 0
    current: dict[str, str] = {}
    for rel, text in pages.items():
        data = text.encode("utf-8")
        target = _target(root, rel, create=True)
        current[rel] = sha256_bytes(data)
        if target.is_file() and target.read_bytes() == data:
            unchanged += 1
        else:
            atomic_write(target, data, mode=0o644)
            written += 1
    for rel, digest in previous.items():
        if rel in current or not isinstance(rel, str):
            continue
        try:
            target = _target(root, rel, create=False)
        except LupusError:
            continue
        if not target.is_file():
            continue
        if sha256_bytes(target.read_bytes()) == digest:
            target.unlink()          # e.g. the page of knowledge the user asked to forget
            removed += 1
        else:
            kept += 1                # someone edited it: not ours to delete
    atomic_write(manifest_path, json.dumps({"files": current}, sort_keys=True, indent=1).encode("utf-8"), mode=0o644)
    return {"written": written, "unchanged": unchanged, "removed": removed, "kept_modified": kept}
