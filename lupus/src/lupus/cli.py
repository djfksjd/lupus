"""`lupus` command line. A tool for the USER and the foreground supervisor.

It is not an API for models: workers get a prompt and a directory, never this CLI, the database
path or a fencing token. Commands that carry user authority (new goals, pause/cancel, budget
raises, resolving a wait, revocation) ask for a typed confirmation on a real terminal. That
stops an agent in a headless tool call from granting itself authority by accident; it is NOT a
security boundary against a process running as the same user (see docs/CONTRACT.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import adapters, budget, goals, graph, memory, probe, projects, protect, quick, recovery, supervisor, vault
from .kernel import Kernel
from .util import LupusError


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home or os.environ.get("LUPUS_HOME") or Path.home() / ".lupus")


def _out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _confirm_user(what: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LupusError("USER_PRESENCE_REQUIRED", f"'{what}' needs an interactive terminal")
    if input(f"{what}\n진행하려면 yes 입력: ").strip() != "yes":
        raise LupusError("USER_DECLINED", what)


def _adapter(name: str, model: str | None) -> adapters.Adapter:
    return adapters.native({"claude": "native_claude", "codex": "native_codex"}[name], model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lupus", description="Lupus supervisor")
    parser.add_argument("--home", help="runtime directory (default: $LUPUS_HOME or ~/.lupus)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the runtime directory and database")

    p = sub.add_parser("project-add", help="register an existing project folder")
    p.add_argument("root")
    p.add_argument("--name", required=True)
    p.add_argument("--providers", default="", help="comma list: anthropic,openai")
    sub.add_parser("project-list")
    p = sub.add_parser("project-allow-unconfined",
                       help="consent: workers of a driver without read confinement may run on this project")
    p.add_argument("project_id")
    p.add_argument("--revoke", action="store_true")

    p = sub.add_parser("goal-submit", help="create a goal from a JSON file")
    p.add_argument("project_id")
    p.add_argument("file")
    sub.add_parser("goal-list")
    for name in ("status", "goal-pause", "goal-resume", "goal-cancel", "refreeze"):
        sub.add_parser(name).add_argument("goal_id")

    p = sub.add_parser("run", help="advance a goal in the foreground")
    p.add_argument("goal_id")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--stop-stale-writer", action="store_true",
                   help="terminate a worker left over from a dead supervisor before continuing")
    p.add_argument("--cheap-first", action="store_true",
                   help="try a lighter model/effort first; use the default only if verification fails")

    p = sub.add_parser("fix-tests", help="one command, no goal file: make this folder's failing tests pass")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("do", help="one line, any request: draft a failing test, you approve it, then implement")
    p.add_argument("request")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("recover", help="close runs left by a crash and reconcile recovery objects")
    p.add_argument("--stop-stale-writer", action="store_true")

    p = sub.add_parser("resolve", help="return a waiting task to PENDING with new input")
    p.add_argument("task_id")
    p.add_argument("--note", required=True)

    p = sub.add_parser("budget-raise")
    p.add_argument("goal_id")
    p.add_argument("dimension")
    p.add_argument("new_cap", type=int)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("revoke", help="withdraw access for a project (stops runs, voids approvals)")
    p.add_argument("project_id")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("vault-sync", help="refresh the Markdown vault (default: <home>/vault)")
    p.add_argument("vault", nargs="?")
    p = sub.add_parser("graph", help="write the graph viewer page and print its path")
    p.add_argument("--open", action="store_true", help="open it in the default browser")

    p = sub.add_parser("note-add", help="record knowledge as a graph node")
    p.add_argument("--project", help="project id; omit for global knowledge")
    p.add_argument("--kind", choices=memory.KINDS, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p = sub.add_parser("note-list")
    p.add_argument("--project")
    p = sub.add_parser("note-search", help="what a worker in this project would be shown for a query")
    p.add_argument("query")
    p.add_argument("--project")
    p = sub.add_parser("note-link")
    p.add_argument("src")
    p.add_argument("type", choices=memory.EDGE_TYPES)
    p.add_argument("dst")
    for name, text in (("note-verify", "vouch for a node"), ("note-retire", "stop recalling a node"),
                       ("note-promote", "copy a project node into global knowledge"),
                       ("note-forget", "delete a node for good")):
        p = sub.add_parser(name, help=text)
        p.add_argument("node_id")
        p.add_argument("--reason", default="")

    p = sub.add_parser("probe", help="measure what the installed CLIs support (P0.5)")
    p.add_argument("--live", action="store_true", help="make one small subscription call per CLI")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except LupusError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False), file=sys.stderr)
        return 2


def _run(k: Kernel, args: argparse.Namespace) -> dict:
    with k.supervisor_lock():     # held from recovery through the whole run
        report = supervisor.recover(k, args.stop_stale_writer)
        if report["writers_alive"]:
            raise LupusError("WRITER_NOT_STOPPED", json.dumps(report["writers_alive"]))
        adapter = _adapter(args.driver, args.model)
        installed = probe._version(args.driver, adapters.worker_env())
        measured = {r["cli_version"] for r in k.q(
            "SELECT cli_version FROM capability WHERE adapter = ? AND status = 'verified'", adapter.driver)}
        if measured and measured != {installed}:
            # Capabilities are measurements of one CLI version, not promises about the next.
            raise LupusError("CAPABILITY_STALE", f"measured {sorted(measured)}, installed {installed}: "
                             "run `lupus probe --live`")
        tiers = adapters.cheap_first(adapter.driver) if args.cheap_first and not args.model else None
        return supervisor.run_goal(k, args.goal_id, adapter, max_steps=args.max_steps, timeout_s=args.timeout,
                                   tiers=tiers)


def _dispatch(args: argparse.Namespace) -> int:
    home = _home(args)
    if args.cmd == "init":
        Kernel.init(home).close()
        _out({"home": str(home)})
        return 0
    k = Kernel(home)
    try:
        if args.cmd == "project-add":
            _confirm_user(f"프로젝트 등록: {os.path.realpath(args.root)} (제공자: {args.providers or '없음'})")
            _out(projects.register(k, args.root, args.name, [p for p in args.providers.split(",") if p]))
        elif args.cmd == "project-allow-unconfined":
            _confirm_user(
                f"{'허용 철회' if args.revoke else '허용'}: 읽기를 프로젝트 안으로 가둘 수 없는 CLI(현재 Codex)의 worker는 이 사용자가 읽을 수 "
                f"있는 모든 파일(SSH 키, 토큰, 다른 프로젝트)을 읽을 수 있습니다. 프로젝트 {args.project_id}")
            projects.allow_unconfined_reads(k, args.project_id, "user", not args.revoke)
        elif args.cmd == "project-list":
            _out([dict(r) for r in k.q("SELECT project_id, name, canonical_root, approved_providers, "
                                       "allow_unconfined_reads FROM project")])
        elif args.cmd == "goal-submit":
            spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
            _confirm_user(f"새 목표 등록: {spec['objective']} · 예산 {spec['budget']}")
            with k.tx():
                goal = goals.submit(k, args.project_id, spec["objective"], spec["criteria"], spec["budget"])
                ids: list[str] = []
                for t in spec.get("tasks", []):
                    task = goals.add_task(k, goal["goal_id"], t["title"], t["prompt"], t["criteria"],
                                          [ids[i] for i in t.get("depends_on", [])])
                    ids.append(task["task_id"])
            _out(goals.view(k, goal["goal_id"]))
        elif args.cmd == "goal-list":
            _out([dict(r) for r in k.q("SELECT goal_id, project_id, status, objective FROM goal ORDER BY created_at")])
        elif args.cmd == "status":
            _out({"goal": goals.view(k, args.goal_id), "resume": recovery.readiness(k, args.goal_id)})
        elif args.cmd == "goal-pause":
            _confirm_user(f"목표 일시정지: {args.goal_id}")
            goals.pause(k, args.goal_id, "user")
        elif args.cmd == "goal-resume":
            _confirm_user(f"목표 재개 허용: {args.goal_id}")
            _out({"status": goals.resume_paused(k, args.goal_id, "user")})
        elif args.cmd == "refreeze":
            _confirm_user(f"보호된 검증 파일의 현재 상태를 기준으로 확정: {args.goal_id}")
            _out({"files": protect.refreeze(k, args.goal_id, goals.project_root(k, args.goal_id), "user")})
        elif args.cmd == "goal-cancel":
            _confirm_user(f"목표 취소(되돌릴 수 없음): {args.goal_id}")
            goals.cancel(k, args.goal_id, "user")
        elif args.cmd == "recover":
            _out(supervisor.recover(k, args.stop_stale_writer))
        elif args.cmd == "fix-tests":
            cwd = os.getcwd()
            project = projects.resolve(k, cwd)
            provider = {"claude": "anthropic", "codex": "openai"}[args.driver]
            if project is None:
                _confirm_user(f"이 폴더를 프로젝트로 등록하고 실패하는 테스트를 고칩니다: {os.path.realpath(cwd)} ({args.driver})")
                project = projects.register(k, cwd, os.path.basename(os.path.realpath(cwd)), ["anthropic", "openai"])
            elif provider not in project["approved_providers"]:
                raise LupusError("PROVIDER_NOT_APPROVED", provider)
            else:
                _confirm_user(f"실패하는 테스트를 고칩니다: {project['canonical_root']} ({args.driver})")
            made = quick.fix_tests(k, project, "user")
            if "goal_id" not in made:
                _out(made)
                return 0
            args.goal_id = made["goal_id"]
            report = _run(k, args)
            report["vault"] = vault.sync(k)
            _out(report)
        elif args.cmd == "do":
            cwd = os.getcwd()
            project = projects.resolve(k, cwd)
            if project is None:
                _confirm_user(f"이 폴더를 프로젝트로 등록합니다: {os.path.realpath(cwd)}")
                project = projects.register(k, cwd, os.path.basename(os.path.realpath(cwd)), ["anthropic", "openai"])
            _confirm_user(f"요청: {args.request}\n먼저 이 요청을 판정할 테스트를 작성합니다 ({args.driver})")
            draft = quick.draft_check(k, project, args.request, "user")
            args.goal_id = draft["goal_id"]
            first = _run(k, args)
            if not first["done"]:
                kept = quick.discard_draft(k, project, draft["goal_id"], "user")
                _out({"stage": "check", "result": "유효한 검사를 만들지 못했습니다", "draft_kept_in": kept, **first})
                return 1
            test_file = Path(project["canonical_root"]) / draft["test_path"]
            content = test_file.read_bytes()
            print(f"\n----- {draft['test_path']} (현재 코드에서 실패함을 확인했습니다) -----\n"
                  f"{content.decode('utf-8', errors='replace')}\n-----")
            try:
                _confirm_user("이 테스트가 통과하면 요청이 완료된 것으로 보겠습니까?")
            except (LupusError, KeyboardInterrupt, EOFError):     # declined, Ctrl-C or closed input
                kept = quick.discard_draft(k, project, draft["goal_id"], "user")
                _out({"stage": "approval", "result": "승인하지 않아 검사를 치웠습니다", "draft_kept_in": kept})
                return 1
            build = quick.approve_check(k, project, draft["goal_id"], draft["request"],
                                        hashlib.sha256(content).hexdigest(), "user")
            args.goal_id = build["goal_id"]
            report = _run(k, args)
            report["check"] = draft["test_path"]
            report["vault"] = vault.sync(k)
            _out(report)
        elif args.cmd == "run":
            report = _run(k, args)
            try:
                report["vault"] = vault.sync(k)
            except LupusError as exc:        # a projection problem never fails the work itself
                report["vault"] = {"error": exc.code, "detail": exc.detail}
            _out(report)
        elif args.cmd == "resolve":
            _confirm_user(f"대기 해제: {args.task_id} · {args.note}")
            goals.resolve_wait(k, args.task_id, "user", args.note)
        elif args.cmd == "budget-raise":
            _confirm_user(f"예산 상한 변경: {args.goal_id} {args.dimension} -> {args.new_cap}")
            budget.raise_cap(k, goals.get(k, args.goal_id)["budget_id"], args.dimension, args.new_cap,
                             "user", args.reason)
        elif args.cmd == "revoke":
            _confirm_user(f"권한 철회: {args.project_id} · {args.reason}")
            _out({"revocation_epoch": projects.revoke(k, args.project_id, "user", args.reason)})
        elif args.cmd == "vault-sync":
            _out(vault.sync(k, args.vault))
        elif args.cmd == "graph":
            vault.sync(k)
            page = graph.write(k)
            if args.open:
                import webbrowser
                webbrowser.open(page.as_uri())
            _out({"viewer": str(page)})
        elif args.cmd == "note-add":
            _confirm_user(f"지식 기록({args.project or '전역'}): [{args.kind}] {args.title}")
            _out(memory.add(k, project_id=args.project, kind=args.kind, title=args.title, body=args.body,
                            origin="user", actor="user"))
        elif args.cmd == "note-list":
            _out([{key: n[key] for key in ("node_id", "kind", "status", "origin", "title", "recalled", "helped",
                                           "unhelped")} for n in memory.nodes(k, args.project)])
        elif args.cmd == "note-search":
            _out([{key: n[key] for key in ("node_id", "kind", "status", "title", "score")}
                  for n in memory.search(k, args.project, args.query)])
        elif args.cmd == "note-link":
            _confirm_user(f"연결: {args.src} --{args.type}--> {args.dst}")
            memory.link(k, args.src, args.dst, args.type, "user")
        elif args.cmd == "note-verify":
            _confirm_user(f"지식 확인: {args.node_id}")
            _out(memory.set_status(k, args.node_id, "verified", "user"))
        elif args.cmd == "note-retire":
            _confirm_user(f"지식 은퇴: {args.node_id}")
            _out(memory.set_status(k, args.node_id, "retired", "user", args.reason))
        elif args.cmd == "note-promote":
            _confirm_user(f"전역 지식으로 복사(모든 프로젝트에서 회상됨): {args.node_id}")
            _out(memory.promote_global(k, args.node_id, "user"))
        elif args.cmd == "note-forget":
            _confirm_user(f"지식 삭제(되돌릴 수 없음): {args.node_id}")
            memory.forget(k, args.node_id, "user", args.reason or "deleted by user")
        elif args.cmd == "probe":
            _out(probe.run(k, live=args.live))
        return 0
    finally:
        k.close()


if __name__ == "__main__":
    sys.exit(main())
