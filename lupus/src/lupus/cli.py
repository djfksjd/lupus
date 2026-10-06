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
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import adapters, alpha, author, budget, gitx, goals, graph, jobs, judging, learn, memory, probe, projects, protect, quick, recovery, review, session, supervisor, vault
from .kernel import Kernel
from .util import LupusError


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home or os.environ.get("LUPUS_HOME") or Path.home() / ".lupus")


JSON_OUTPUT = False


def _out(data) -> None:
    """A person at a terminal gets a few lines; a pipe, a log file or `--json` gets everything."""
    if isinstance(data, dict) and "steps" in data and sys.stdout.isatty() and not JSON_OUTPUT:
        print(_summary(data))
        return
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _summary(report: dict) -> str:
    done = report.get("done")
    lines = ["✔ 완료: 모든 완료 조건을 Lupus가 직접 검증했습니다" if done else
             f"✘ 미완료 (목표 상태: {report.get('goal_status', '?')})"]
    for key, label in (("result", ""), ("check", "승인한 검사"), ("implementation", "구현"), ("review", "리뷰"), ("out", "결과물"), ("tests", "테스트")):
        if report.get(key):
            lines.append(f"  {label + ': ' if label else ''}{report[key]}")
    for check in report.get("checks") or []:
        mark = {"PASS": "✔", "FAIL": "✘"}.get(check["result"], "·")
        lines.append(f"  {mark} {check['text'][:110]}")
    if done and report.get("checks"):
        lines.append("  (확인한 것은 위 검사뿐입니다. 검사가 다루지 않는 동작은 확인하지 않았습니다)")
    for i, step in enumerate(report["steps"], 1):
        verdicts = " ".join(f"{cid}={v}" for cid, v in (step.get("verdicts") or {}).items())
        why = step.get("reason") or step.get("blockers") or ""
        lines.append(f"  단계 {i}: {step.get('outcome') or step.get('status')}"
                     + (f" · {step['variant']}" if step.get("variant") else "") + (f" · {verdicts}" if verdicts else "")
                     + (f" · {why}" if why else ""))
    used = (report.get("budget") or {})
    totals = ((report.get("usage") or {}).get("totals") or {}) if isinstance(report.get("usage"), dict) else {}
    tokens = sum(totals.get(key, 0) for key in ("tokens_in", "tokens_cached", "tokens_out"))
    parts = []
    if "attempts" in used:
        parts.append(f"시도 {used['attempts']['used']}/{used['attempts']['cap']}")
    if tokens:
        parts.append(f"토큰 {tokens:,}")
    elif isinstance(report.get("usage"), str):
        parts.append(f"사용량: {report['usage']}")
    if "active_ms" in used:
        parts.append(f"{used['active_ms']['used'] / 1000:.1f}초")
    if parts:
        lines.append("  " + " · ".join(parts))
    if report.get("detail"):
        lines.append("  " + str(report["detail"])[-600:])
    if not done:
        for wait in report.get("waiting") or []:
            lines.append(f"  대기: {wait.get('status', '')} {str(wait.get('reason', ''))[:200]}")
        blockers = report.get("completion_blockers") or []
        if blockers:
            lines.append("  남은 것: " + ", ".join(blockers[:6]))
        if report.get("final_verification_not_run"):
            why = str(report["final_verification_not_run"])
            lines.append(f"  마지막 검증을 실행하지 못했습니다: {why[:160]}"
                         + ("  → lupus budget-raise 로 상한을 올리면 이어집니다" if "BUDGET" in why else ""))
        goal_id = report.get("goal_id")
        lines.append(f"  다음: {report['next']}" if report.get("next") else
                     f"  다음: lupus status {goal_id}   (원인 확인 후 lupus run {goal_id} --driver <claude|codex>)")
    if done and report.get("next"):
        lines.append(f"  다음: {report['next']}")
    lines.append(f"  목표 {report.get('goal_id', '')} · 전체 내용은 같은 명령에 --json")
    return "\n".join(lines)


def _ask(what: str, choices: tuple[str, ...]) -> str:
    """A question only a person at the terminal can answer. Anything but a listed word is "no"."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LupusError("USER_PRESENCE_REQUIRED", f"'{what}' needs an interactive terminal")
    try:
        answer = input(f"{what}\n{' / '.join(choices)} 중 입력: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return "no"
    return answer if answer in choices else "no"


def _confirm_user(what: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LupusError("USER_PRESENCE_REQUIRED", f"'{what}' needs an interactive terminal")
    if input(f"{what}\n진행하려면 yes 입력: ").strip() != "yes":
        raise LupusError("USER_DECLINED", what)


def _adapter(name: str, model: str | None, shell: bool = False) -> adapters.Adapter:
    if name == "claude" and shell:
        return adapters.ClaudeAdapter(model, shell=True)
    return adapters.native({"claude": "native_claude", "codex": "native_codex"}[name], model)


def _isolate(k: Kernel, project: dict, args: argparse.Namespace) -> dict:
    if not getattr(args, "isolated", False):
        return project
    isolated = gitx.isolate(k, project, "user")
    print(f"격리된 체크아웃에서 작업합니다(커밋 {isolated['base'][:8]} 기준). 지금 작업 폴더의 파일은 건드리지 않습니다."
          + (" 커밋하지 않은 변경은 이 작업에 포함되지 않습니다." if isolated["origin_has_uncommitted_changes"] else ""), file=sys.stderr)
    return isolated


def _reviewed(k: Kernel, args: argparse.Namespace, report: dict, draft: dict, build: dict) -> dict:
    """`lupus do --review`: the independent read, one round of answers, and what is left."""
    reviewer = args.review if args.review != "other" else ("codex" if args.driver == "claude" else "claude")
    if "review_base" not in build:
        report["review"] = f"리뷰하지 못했습니다: {build.get('review_unavailable', '기준 상태를 보관하지 못함')}"
        return report
    print(f"검사가 통과했습니다. 구현을 쓰지 않은 AI({reviewer})가 요청과 변경을 대조합니다…", file=sys.stderr)

    def run(goal_id: str) -> dict:
        args.goal_id = goal_id
        return _run(k, args)

    outcome = review.cycle(k, report["goal_id"], draft["request"], DRIVER[reviewer], Path(build["review_base"]), run,
                           quick.DEFAULT_CAPS, skip={draft["test_path"]})
    said = lambda items: "; ".join(f"「{o['request_quote'][:60]}」 {o['problem'][:160]}" for o in items)
    if "skipped" in outcome:
        report["review"] = f"리뷰하지 못했습니다({reviewer}): {outcome['skipped']}"
    elif not outcome.get("objections"):
        report["review"] = f"독립 리뷰({reviewer}): 요청과 어긋나는 점을 찾지 못했습니다 (의견이며 증명이 아닙니다)"
    elif not outcome["revised"]:
        report["review"] = (f"독립 리뷰({reviewer}): 이의 {len(outcome['objections'])}건. 되돌아갈 백업을 만들 수 없어 worker에게 "
                            f"돌려보내지 않았습니다({outcome.get('not_revised')}). 이의: {said(outcome['objections'])}")
    else:
        revised = outcome["revision_report"]
        report["model_calls"] += len(revised["steps"])
        for key, value in revised["usage"]["totals"].items():
            report["usage"]["totals"][key] = report["usage"]["totals"].get(key, 0) + value
        if outcome["revision_done"]:
            report.update(goal_id=outcome["goal_id"], checks=revised["checks"])
            report["steps"] = report["steps"] + revised["steps"]
            left = outcome["remaining"]
            report["review"] = (f"독립 리뷰({reviewer}): 이의 {len(outcome['objections'])}건을 worker에게 돌려보냈고 검사는 다시 통과했습니다. "
                                + (f"다시 리뷰하지는 못했습니다({outcome['second_review_skipped']}): 이의가 해소됐는지는 확인되지 않았습니다"
                                   if left is None else
                                   f"리뷰어가 여전히 지적하는 것 {len(left)}건: {said(left)}" if left else "남은 이의 없음"))
        else:
            report["review"] = (f"독립 리뷰({reviewer}): 이의 {len(outcome['objections'])}건을 반영하려던 수정이 승인된 검사를 통과하지 못해 "
                                f"통과했던 상태로 되돌렸습니다(밀려난 파일은 {outcome['displaced_kept_in']} 에 보관). "
                                f"반영되지 않은 이의: {said(outcome['objections'])}")
            if outcome["not_put_back"]:
                # The project is not the state the checks passed on: this is no longer a verified result.
                report["done"] = False
                report["review"] += (f" ⚠ 되돌리지 못한 파일이 있어 검증된 상태가 아닙니다: {', '.join(outcome['not_put_back'][:10])}. "
                                     f"통과했던 상태의 사본: {outcome['backup_kept_in']} (`lupus prune`이 오래된 사본을 지우기 전에 확인하세요)")
    report["review_detail"] = {key: outcome[key] for key in (
        "reviewer", "objections", "remaining", "dropped", "revised", "revision_done", "skipped", "files",
        "second_review_skipped", "not_revised", "not_put_back", "displaced_kept_in", "backup_kept_in") if key in outcome}
    return report


def _after_isolated(k: Kernel, report: dict) -> None:
    if gitx.info(k, goals.get(k, report["goal_id"])["project_id"]):
        goal_id = report["goal_id"]
        report["next"] = (f"lupus diff {goal_id}   (결과 보기)   ·   lupus accept {goal_id}   (내 저장소에 커밋 하나로 반영)   ·   "
                          f"lupus discard {goal_id}   (버리기)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lupus", description="Lupus supervisor")
    parser.add_argument("--home", help="runtime directory (default: $LUPUS_HOME or ~/.lupus)")
    parser.add_argument("--json", action="store_true", help="print full reports as JSON even at a terminal")
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
    p.add_argument("--background", action="store_true", help="keep running after this terminal closes; see `lupus jobs`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    for name, text in (("diff", "what a goal changed"), ("accept", "bring an isolated goal's verified result into your repository as one commit"),
                       ("discard", "drop an isolated goal's checkout and branch")):
        sub.add_parser(name, help=text).add_argument("goal_id")

    sub.add_parser("alpha-status", help="every project at a glance: open goals, what they wait for, shared budgets")
    p = sub.add_parser("alpha-run", help="advance every unfinished goal in turn; switch AI when one is out of quota")
    p.add_argument("--drivers", default="claude,codex", help="comma list, in order of preference")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--background", action="store_true")
    p = sub.add_parser("alpha-budget", help="a cap shared by all goals of a project, or of everything (omit --project)")
    p.add_argument("--project")
    p.add_argument("--calls", type=int, required=True)
    p.add_argument("--attempts", type=int, required=True)
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--tokens", type=int)
    p.add_argument("--reason", default="")
    p = sub.add_parser("goal-priority", help="larger runs earlier under alpha-run")
    p.add_argument("goal_id")
    p.add_argument("priority", type=int)
    sub.add_parser("jobs", help="supervisors started with --background")
    p = sub.add_parser("logs", help="the end of a background job's output")
    p.add_argument("job_id")
    p.add_argument("--lines", type=int, default=40)
    sub.add_parser("stop", help="stop a background job (its worker is stopped with it)").add_argument("job_id")
    sub.add_parser("job-run", help=argparse.SUPPRESS).add_argument("job_id")
    p = sub.add_parser("learn", help="turn recorded failures of a project into candidate procedures (one small model call)")
    p.add_argument("--project", help="project id (default: this folder's project)")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)

    p = sub.add_parser("fix-tests", help="one command, no goal file: make this folder's failing tests pass")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--isolated", action="store_true",
                   help="work in a separate checkout of the committed HEAD; your files are untouched until `lupus accept`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    p.add_argument("--check", help="your own test command for a project Lupus does not recognise; its exit status decides")
    p.add_argument("--protect", action="append", default=[], help="with --check: a file or folder workers may not change")
    p.add_argument("--container", metavar="IMAGE", help="run the tests in a Docker container of this image (no network) "
                                                        "instead of the host sandbox")
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("write", help="a document (plan, research, report): a rubric you approve, a judge that must quote, your sign-off")
    p.add_argument("request")
    p.add_argument("--out", required=True, help="file to write, relative to this folder")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--judge", choices=("claude", "codex"), help="default: the other AI when it is approved and measured")
    p.add_argument("--must", action="append", default=[], help="a criterion the document must meet (repeatable)")
    p.add_argument("--no-draft", action="store_true", help="use only --must criteria; do not ask a model to propose any")
    p.add_argument("--web", action="store_true", help="let the writer search and read the web (claude only)")
    p.add_argument("--min-chars", type=int, default=400)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(goal_id=None, max_steps=4, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("session", help="your usual interactive claude/codex, with the tests frozen and verified by Lupus at the end")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--check", help="your own test command; its exit status decides")
    p.add_argument("--protect", action="append", default=[], help="with --check: a file or folder that may not change")
    p.add_argument("--container", metavar="IMAGE", help="run the tests in a Docker container of this image (no network)")
    p.add_argument("--max-minutes", type=float, default=240)
    p.add_argument("--no-stop-check", action="store_true", help="do not run the tests each time the model stops (claude)")
    p.add_argument("cli_args", nargs=argparse.REMAINDER, help="after --: passed to the CLI as they are")
    p.set_defaults(goal_id=None, max_steps=1, stop_stale_writer=False, cheap_first=False, model=None)

    p = sub.add_parser("do", help="one line, any request: draft a failing test, you approve it, then implement")
    p.add_argument("request")
    p.add_argument("--isolated", action="store_true",
                   help="work in a separate checkout of the committed HEAD; your files are untouched until `lupus accept`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    p.add_argument("--allow-failing", action="store_true",
                   help="start even though some existing tests fail now; those same tests may keep failing, no others")
    p.add_argument("--review", nargs="?", const="other", choices=("other", "claude", "codex"),
                   help="when the checks pass, have an AI that did not write the change compare it with the request; "
                        "objections go back to the worker once (default reviewer: the other AI)")
    p.add_argument("--two-step", action="store_true",
                   help="write the implementation in a separate call after you approved the test (default: proposed "
                        "in the same call, kept aside until you approve)")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("approve", help="read and sign off the document of a `write` goal that is waiting for you")
    p.add_argument("goal_id")
    p = sub.add_parser("revise", help="send the document of a `write` goal back with your feedback")
    p.add_argument("goal_id")
    p.add_argument("feedback")
    p.add_argument("--driver", choices=("claude", "codex"), required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(max_steps=4, stop_stale_writer=False, cheap_first=False)
    p = sub.add_parser("revalidate", help="after `revoke`: confirm that a goal may continue with what is left of its scope")
    p.add_argument("goal_id")
    p.add_argument("--note", required=True)
    sub.add_parser("project-remove", help="take back a registration that has no goals").add_argument("project_id")
    p = sub.add_parser("prune", help="delete old set-aside files, job logs and session scratch files")
    p.add_argument("--days", type=float, default=30)

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
    global JSON_OUTPUT
    JSON_OUTPUT = args.json
    try:
        if args.cmd == "job-run":
            return _job_run(_home(args), args.job_id)
        return _dispatch(args)
    except LupusError as exc:
        _forget_fresh(args)
        hint = NEXT_STEP.get(exc.code)
        print(json.dumps({"error": exc.code, "detail": exc.detail, **({"next": hint} if hint else {})}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        _forget_fresh(args)
        print(json.dumps({"error": "INTERRUPTED", "next": "lupus recover"}), file=sys.stderr)
        return 130
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        if os.environ.get("LUPUS_DEBUG"):
            raise
        # A program error, not something the user did wrong: say so plainly instead of a traceback.
        print(json.dumps({"error": "INTERNAL_ERROR", "detail": f"{type(exc).__name__}: {exc}"[:300],
                          "next": "state on disk is transactional; run `lupus recover`. Set LUPUS_DEBUG=1 for the traceback"},
                         ensure_ascii=False), file=sys.stderr)
        return 3


# What to do about a refusal, for the ones where it is not obvious.
NEXT_STEP = {
    "CAPABILITY_STALE": "lupus probe --live",
    "CAPABILITY_UNVERIFIED": "lupus probe --live",
    "WRITER_NOT_STOPPED": "lupus recover --stop-stale-writer   (after checking that nothing of yours is still running)",
    "SUPERVISOR_BUSY": "lupus jobs   (another lupus is running; `lupus stop <job>` or wait)",
    "NO_TESTS_FOUND": "name your test command: --check \"<command>\"",
    "TEST_RUNNER_UNKNOWN": "name your test command: --check \"<command>\"",
    "TEST_RUNNER_UNAVAILABLE": "install the test runner, or name your test command with --check, or run it in a container with --container <image>",
    "BASELINE_RED": "lupus fix-tests --driver <claude|codex>   or repeat the command with --allow-failing",
    "PROJECT_ROOT_OVERLAP": "lupus project-list, then `lupus project-remove <id>` for a registration made by mistake",
    "UNCONFINED_READS_NOT_ALLOWED": "lupus probe --live, or `lupus project-allow-unconfined <project_id>`",
    "PROVIDER_NOT_APPROVED": "register the project with that provider: lupus project-add <root> --name <n> --providers anthropic,openai",
    "CHECK_EXISTS": "remove the leftover file or folder named in the detail",
    "VERIFIER_SANDBOX_UNAVAILABLE": "run the checks in a container: --container <image>",
}


def _forget_fresh(args: argparse.Namespace) -> None:
    """A folder registered by this very command, which then did nothing with it: do not leave the
    registration behind (it would block registering the right folder later)."""
    project_id = getattr(args, "fresh_project", None)
    if not project_id:
        return
    try:
        k = Kernel(_home(args))
        try:
            projects.remove(k, project_id, "user")
        finally:
            k.close()
    except (LupusError, OSError, sqlite3.Error):
        pass


def _job_run(home: Path, job_id: str) -> int:
    """Body of a background job: the same command, in this detached process."""
    if not jobs.released():
        return 111              # started by hand, or the starter died before recording this process
    k = Kernel(home)
    command = jobs.command_of(k, job_id)
    k.close()

    def terminate(*_):          # `lupus stop`: unwind normally so the worker's process group is emptied
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    code: int | None = None
    try:
        code = main(["--home", str(home), *command])
        return code
    finally:
        k = Kernel(home)
        jobs.finished(k, job_id, code)
        k.close()
        jobs.notify("Lupus", f"{' '.join(command[:2])} 종료 (코드 {code})")


def _stale(k: Kernel, cli: str, driver: str) -> str | None:
    installed = probe._version(cli, adapters.worker_env())
    measured = {r["cli_version"] for r in k.q(
        "SELECT cli_version FROM capability WHERE adapter = ? AND status = 'verified'", driver)}
    if measured and measured != {installed}:
        # Capabilities are measurements of one CLI version, not promises about the next.
        return f"measured {sorted(measured)}, installed {installed}: run `lupus probe --live`"
    return None


def _run(k: Kernel, args: argparse.Namespace) -> dict:
    with k.supervisor_lock():     # held from recovery through the whole run
        report = supervisor.recover(k, args.stop_stale_writer)
        if report["writers_alive"]:
            raise LupusError("WRITER_NOT_STOPPED", json.dumps(report["writers_alive"]))
        adapter = getattr(args, "adapter", None) or _adapter(args.driver, args.model, getattr(args, "shell", False))
        stale = _stale(k, args.driver, adapter.driver)
        if stale:
            raise LupusError("CAPABILITY_STALE", stale)
        tiers = adapters.cheap_first(adapter.driver) if args.cheap_first and not args.model else None
        return supervisor.run_goal(k, args.goal_id, adapter, max_steps=args.max_steps, timeout_s=args.timeout,
                                   tiers=tiers)


DRIVER = {"claude": "native_claude", "codex": "native_codex"}


def _repo_root(cwd: str) -> str:
    """The folder to register: the enclosing git work tree if there is one (so that running from a
    subfolder does not register the subfolder), otherwise the current folder."""
    here = Path(os.path.realpath(cwd))
    for folder in (here, *here.parents):
        if (folder / ".git").exists():
            return str(folder) if folder != Path(os.path.realpath(Path.home())) else str(here)
    return str(here)


def _project_here(k: Kernel, what: str, args: argparse.Namespace | None = None) -> dict:
    cwd = os.getcwd()
    project = projects.resolve(k, cwd)
    if project is None:
        root = _repo_root(cwd)
        _confirm_user(f"이 폴더를 프로젝트로 등록합니다: {root} ({what})")
        project = projects.register(k, root, os.path.basename(root), ["anthropic", "openai"])
        if args is not None:
            args.fresh_project = project["project_id"]
    return project


def _started(args: argparse.Namespace) -> None:
    """Work exists under the project now; the registration stays whatever happens next."""
    args.fresh_project = None


def _goal_file(path: str) -> dict:
    """Read and check a goal file before anything is created from it."""
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LupusError("GOAL_FILE_INVALID", f"{path}: {exc}") from exc
    bad = lambda why: LupusError("GOAL_FILE_INVALID", f"{path}: {why}")
    if not isinstance(spec, dict):
        raise bad("the file must hold one JSON object")
    if not isinstance(spec.get("objective"), str) or not spec["objective"].strip():
        raise bad("`objective` (text) is missing")
    budget_ = spec.get("budget")
    if not isinstance(budget_, dict) or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in budget_.values()):
        raise bad("`budget` must map calls / attempts / active_ms (and optionally tokens) to positive whole numbers")
    if not isinstance(spec.get("criteria"), list):
        raise bad("`criteria` (a list) is missing")
    tasks = spec.get("tasks", [])
    if not isinstance(tasks, list):
        raise bad("`tasks` must be a list")
    for i, task in enumerate(tasks):
        if not isinstance(task, dict) or not all(isinstance(task.get(key), str) and task[key].strip() for key in ("title", "prompt")):
            raise bad(f"tasks[{i}] needs `title` and `prompt`")
        if not isinstance(task.get("criteria"), list) or not task["criteria"]:
            raise bad(f"tasks[{i}] needs `criteria`: the ids of the criteria it works towards")
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d < i for d in deps):
            raise bad(f"tasks[{i}].depends_on must list positions of EARLIER tasks (0..{i - 1})")
    return spec


def _approve(k: Kernel, goal_id: str) -> int:
    """Final sign-off of a document goal, whenever the user gets to it."""
    criterion = next((c for c in goals.criteria(k, goal_id) if c["verifier"]["kind"] == "user_approval"), None)
    if criterion is None:
        raise LupusError("NOT_A_DOCUMENT_GOAL", goal_id)
    state = supervisor.finish(k, goal_id)       # brings the mechanical check and the judge up to date first
    if state["done"]:
        _out(state)
        return 0
    if state["completion_blockers"] != [f"EVIDENCE_FAIL:{criterion['id']}"]:
        _out({"result": "아직 승인할 단계가 아닙니다(작성이나 평가가 끝나지 않았습니다)", **state,
              "next": f"lupus run {goal_id} --driver <claude|codex>"})
        return 1
    target = goals.project_root(k, goal_id) / criterion["verifier"]["path"]
    data = target.read_bytes()
    print(f"\n----- {criterion['verifier']['path']} ({len(data.decode('utf-8', errors='replace'))}자): 평가자가 모든 기준 충족으로 판정 -----\n"
          f"파일을 직접 읽어 보십시오: {target}")
    try:
        _confirm_user("이 판본을 완료로 승인하시겠습니까?")
    except LupusError:
        _out({"result": "승인하지 않았습니다. 목표는 완료되지 않은 채로 남습니다", "goal_id": goal_id,
              "next": f"lupus revise {goal_id} \"<고칠 점>\" --driver <claude|codex>   또는 나중에   lupus approve {goal_id}"})
        return 1
    judging.approve(k, goal_id, criterion["id"], hashlib.sha256(data).hexdigest(), "user")
    done = supervisor.finish(k, goal_id)
    _out({**done, "out": criterion["verifier"]["path"]})
    return 0 if done["done"] else 1


def _write(k: Kernel, args: argparse.Namespace) -> int:
    project = _project_here(k, "문서 작성", args)
    # `--out` is what the user typed where they are standing, not relative to wherever the project root is.
    target_abs = os.path.realpath(os.path.join(os.getcwd(), args.out))
    root_real = os.path.realpath(project["canonical_root"])
    if not target_abs.startswith(root_real + os.sep):
        raise LupusError("PATH_ESCAPES_PROJECT", f"{target_abs} is outside the project {root_real}")
    args.out = os.path.relpath(target_abs, root_real)
    writer = DRIVER[args.driver]
    judge = DRIVER[args.judge] if args.judge else author.pick_judge(k, project, writer)
    if args.web and args.driver != "claude":
        raise LupusError("WEB_NOT_SUPPORTED", "--web works with --driver claude")
    _confirm_user(f"요청: {args.request}\n결과물: {target_abs}" + (" (이미 있는 파일을 덮어씁니다)" if os.path.exists(target_abs) else "")
                  + f" · 작성 {writer} · 평가 {judge}"
                  + ("\n작성자가 웹을 검색하고 읽을 수 있습니다(프로젝트 파일을 읽은 뒤 외부로 요청을 보낼 수 있음)" if args.web else "")
                  + ("\n주의: 평가자가 작성자와 같은 AI입니다" if judge == writer else ""))
    rubric = list(args.must)
    if not args.no_draft:
        with k.supervisor_lock():
            supervisor.recover(k)
            rubric += author.draft_rubric(k, project, args.request, writer, "user")
    rubric = author.clean_rubric(rubric)
    print("\n----- 이 문서를 판정할 기준 -----\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rubric, 1)) + "\n-----")
    _confirm_user("이 기준으로 판정합니다. 기준은 작업 도중 바뀌지 않습니다")
    made = author.submit(k, project, args.request, args.out, rubric, judge, "user", min_chars=args.min_chars)
    _started(args)
    args.goal_id = made["goal_id"]
    if args.driver == "claude":
        args.adapter = adapters.ClaudeAdapter(args.model, tools=("Read", "Write", "Edit", "Glob", "Grep"), web=args.web)
    target = Path(project["canonical_root"]) / made["out"]
    for _ in range(3):
        report = _run(k, args)
        if report["completion_blockers"] != ["EVIDENCE_FAIL:c2"]:
            _out({"stage": "write", "result": "문서가 기계 검사 또는 평가를 통과하지 못했습니다", **report,
                  "next": f"lupus run {made['goal_id']} --driver {args.driver}   그다음   lupus approve {made['goal_id']}"})
            return 1
        data = target.read_bytes()
        print(f"\n----- {made['out']} ({len(data.decode('utf-8', errors='replace'))}자) · 평가자 {judge}: 모든 기준 충족 판정 -----\n"
              f"파일을 직접 읽어 보십시오: {target}")
        try:
            _confirm_user("이 판본을 완료로 승인하시겠습니까?")
        except (LupusError, KeyboardInterrupt, EOFError):
            try:
                feedback = input("고칠 점을 한 줄로 적으면 반영해 다시 씁니다(없으면 Enter): ").strip()
            except (KeyboardInterrupt, EOFError):
                feedback = ""
            if not feedback:
                _out({"stage": "approval", "result": "승인하지 않았습니다. 목표는 완료되지 않은 채로 남습니다", "goal_id": made["goal_id"],
                      "next": f"lupus approve {made['goal_id']}   또는   lupus revise {made['goal_id']} \"<고칠 점>\" --driver {args.driver}"})
                return 1
            author.revise(k, made["goal_id"], feedback, "user")
            continue
        judging.approve(k, made["goal_id"], "c2", hashlib.sha256(data).hexdigest(), "user")
        done = supervisor.finish(k, made["goal_id"])
        _out({**done, "out": made["out"], "judge": judge, "usage": report["usage"], "judge_usage": report["judge_usage"]})
        return 0 if done["done"] else 1
    _out({"stage": "approval", "result": "수정 횟수를 다 썼습니다", "goal_id": made["goal_id"]})
    return 1


def _learn(k: Kernel, args: argparse.Namespace) -> int:
    project = projects.get(k, args.project) if args.project else projects.resolve(k, os.getcwd())
    if project is None:
        raise LupusError("PROJECT_NOT_FOUND", os.getcwd())
    cases = learn.triggers(k, project["project_id"])
    if not cases:
        _out({"learned": [], "note": "배울 사건이 없습니다(재시도나 막힌 작업이 없었음). 모델을 호출하지 않았습니다"})
        return 0
    _confirm_user(f"프로젝트 {project['name']}의 실패 기록 {len(cases)}건에서 절차 후보를 만듭니다({args.driver}, 모델 호출 1회). "
                  "작업 제목과 검증기 출력만 전달되며 프로젝트 파일은 전달되지 않습니다")
    with k.supervisor_lock():
        supervisor.recover(k)
        _out(learn.refine(k, project, DRIVER[args.driver], "user"))
    return 0


def _session(k: Kernel, args: argparse.Namespace) -> int:
    project = _project_here(k, "대화형 세션", args)
    if {"claude": "anthropic", "codex": "openai"}[args.driver] not in project["approved_providers"]:
        raise LupusError("PROVIDER_NOT_APPROVED", args.driver)
    _confirm_user(f"대화형 세션을 시작합니다: {project['canonical_root']} ({args.driver}). 지금 있는 테스트와 테스트 설정은 "
                  "세션 동안 고정되고, 세션이 끝나면 Lupus가 직접 테스트를 실행해 판정합니다")
    with k.supervisor_lock():
        left = supervisor.recover(k)
        if left["writers_alive"]:
            raise LupusError("WRITER_NOT_STOPPED", json.dumps(left["writers_alive"]))
        made = session.start(k, project, "user", args.check, args.protect, container=args.container)
    print(f"고정한 파일 {made['frozen']}개 · 시작 시점 테스트: {'통과' if made['baseline'] == 'PASS' else '실패(이 세션의 목표가 됩니다)'}",
          file=sys.stderr)
    _started(args)
    args.goal_id, args.timeout = made["goal_id"], args.max_minutes * 60
    args.adapter = session.adapter(k, made["goal_id"], args.driver, [a for a in args.cli_args if a != "--"],
                                   not args.no_stop_check)
    report = _run(k, args)
    ev = goals.latest_evidence(k, made["goal_id"])["c0"]
    undone = [json.loads(r["payload"]).get("restored", []) for r in k.q(
        "SELECT payload FROM event WHERE type = 'protect.restored' AND aggregate_id = ?", made["goal_id"])]
    _out({"goal_id": made["goal_id"], "done": report["done"],
          "tests": ev["result"] if ev else "not run", "detail": (ev["detail"] if ev and ev["result"] != "PASS" else ""),
          "frozen_files_put_back": sorted({item for group in undone for item in group}),
          "steps": report["steps"], "usage": "not reported by the host for an interactive session",
          "budget": report["budget"]})
    return 0 if report["done"] else 1


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
            spec = _goal_file(args.file)
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
            files = protect.refreeze(k, args.goal_id, goals.project_root(k, args.goal_id), "user")
            resumed = []
            for task in goals.tasks(k, args.goal_id):      # the branches that were waiting for exactly this decision
                if task["status"] == "NEEDS_ANSWER" and (task["wait_reason"] or "").startswith(("보호된 검증 파일", "PROTECT")):
                    goals.resolve_wait(k, task["task_id"], "user", "보호된 검증 파일의 현재 상태를 확정함")
                    resumed.append(task["task_id"])
            _out({"files": files, "tasks_resumed": resumed})
        elif args.cmd == "goal-cancel":
            _confirm_user(f"목표 취소(되돌릴 수 없음): {args.goal_id}")
            goals.cancel(k, args.goal_id, "user")
        elif args.cmd == "recover":
            _out(supervisor.recover(k, args.stop_stale_writer))
        elif args.cmd == "fix-tests":
            project = _project_here(k, f"실패하는 테스트 고치기, {args.driver}", args)
            if {"claude": "anthropic", "codex": "openai"}[args.driver] not in project["approved_providers"]:
                raise LupusError("PROVIDER_NOT_APPROVED", args.driver)
            if not getattr(args, "fresh_project", None):
                _confirm_user(f"실패하는 테스트를 고칩니다: {project['canonical_root']} ({args.driver})")
            project = _isolate(k, project, args)
            made = quick.fix_tests(k, project, "user", check=args.check, protect_paths=args.protect,
                                   container=args.container)
            if "goal_id" not in made:
                if gitx.info(k, project["project_id"]):      # nothing to do: the checkout made for it goes again
                    found = gitx.info(k, project["project_id"])
                    gitx.run(found["origin_root"], "worktree", "remove", "--force", found["path"], check=False)
                    gitx.run(found["origin_root"], "branch", "-D", found["branch"], check=False)
                _out(made)
                return 0
            args.goal_id = made["goal_id"]
            _started(args)
            report = _run(k, args)
            report["vault"] = vault.sync(k)
            _after_isolated(k, report)
            _out(report)
        elif args.cmd == "diff":
            print(gitx.diff(k, args.goal_id))
        elif args.cmd == "accept":
            goal = goals.get(k, args.goal_id)
            _confirm_user(f"검증된 결과를 내 저장소에 커밋 하나로 반영합니다(빨리 감기 병합만, 강제 없음): {goal['objective'][:80]}")
            _out(gitx.accept(k, args.goal_id, "user"))
        elif args.cmd == "discard":
            _confirm_user(f"격리된 체크아웃과 브랜치를 버립니다(되돌릴 수 없음): {args.goal_id}")
            _out(gitx.discard(k, args.goal_id, "user"))
        elif args.cmd == "write":
            return _write(k, args)
        elif args.cmd == "session":
            return _session(k, args)
        elif args.cmd == "do":
            project = _project_here(k, "요청 구현", args)
            _confirm_user(f"요청: {args.request}\n먼저 이 요청을 판정할 테스트를 작성합니다 ({args.driver})")
            project = _isolate(k, project, args)
            draft = quick.draft_check(k, project, args.request, "user", stage=not args.two_step, allow_failing=args.allow_failing)
            if draft["already_failing"]:
                print(f"지금 실패하는 기존 테스트 {len(draft['already_failing'])}개는 그대로 실패해도 되는 것으로 봅니다: "
                      + ", ".join(draft["already_failing"][:5]) + (" …" if len(draft["already_failing"]) > 5 else ""), file=sys.stderr)
            _started(args)
            args.goal_id = draft["goal_id"]
            try:
                first = _run(k, args)
            except BaseException:
                # Interrupted (Ctrl-C) or refused before you approved anything: the half-written test
                # and proposal must not stay behind as a failing test in your project.
                try:
                    supervisor.recover(k)
                    quick.discard_draft(k, project, draft["goal_id"], "user")
                except LupusError:
                    pass
                raise
            if not first["done"]:
                kept = quick.discard_draft(k, project, draft["goal_id"], "user")
                if getattr(args, "isolated", False):
                    gitx.discard(k, draft["goal_id"], "user")
                _out({"stage": "check", "result": "유효한 검사를 만들지 못했습니다", "draft_kept_in": kept, **first})
                return 1
            test_file = Path(project["canonical_root"]) / draft["test_path"]
            while True:
                content = test_file.read_bytes()
                print(f"\n----- {draft['test_path']} (현재 코드에서 실패함을 확인했습니다) -----\n"
                      f"{content.decode('utf-8', errors='replace')}\n-----")
                answer = _ask("이 테스트가 통과하면 요청이 완료된 것으로 보겠습니까?", ("yes", "edit", "no"))
                if answer != "edit":
                    break
                # The check is yours: change it until it says what you mean. It is checked again
                # (it must still fail on the current code) and the version you approve is the one frozen.
                subprocess.call([*shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"), str(test_file)])
            if answer != "yes":
                kept = quick.discard_draft(k, project, draft["goal_id"], "user")
                if getattr(args, "isolated", False):
                    gitx.discard(k, draft["goal_id"], "user")
                _out({"stage": "approval", "result": "승인하지 않아 검사를 치웠습니다", "draft_kept_in": kept})
                return 1
            try:
                build = quick.approve_check(k, project, draft["goal_id"], draft["request"],
                                            hashlib.sha256(content).hexdigest(), "user", keep_base=bool(args.review))
            except LupusError:
                quick.discard_draft(k, project, draft["goal_id"], "user")      # nothing half-approved stays behind
                raise
            args.goal_id = build["goal_id"]
            report = _run(k, args)
            report["check"] = draft["test_path"]
            report["implementation"] = ("검사와 같은 호출에서 제안된 구현을 승인 뒤에 적용: " + ", ".join(build["staged_applied"])
                                        if build["staged_applied"] else "승인 뒤 별도 호출로 작성")
            for key, value in first["usage"]["totals"].items():      # the request cost both steps
                report["usage"]["totals"][key] = report["usage"]["totals"].get(key, 0) + value
            report["model_calls"] = len(first["steps"]) + sum(s.get("outcome") != "ALREADY_SATISFIED" for s in report["steps"])
            if args.review and report["done"]:
                report = _reviewed(k, args, report, draft, build)
            elif args.review:
                report["review"] = "검사가 통과하지 않아 리뷰하지 않았습니다"
            if build.get("review_base"):      # the copy of the project kept for the reviewer
                shutil.rmtree(build["review_base"], ignore_errors=True)
            _after_isolated(k, report)
            report["vault"] = vault.sync(k)
            _out(report)
        elif args.cmd == "run" and args.background:
            goals.get(k, args.goal_id)
            _out(jobs.start(k, ["run", args.goal_id, "--driver", args.driver, "--max-steps", str(args.max_steps),
                                "--timeout", str(args.timeout), *(["--model", args.model] if args.model else []),
                                *(["--cheap-first"] if args.cheap_first else []),
                                *(["--stop-stale-writer"] if args.stop_stale_writer else [])]))
        elif args.cmd == "alpha-status":
            _out(alpha.portfolio(k))
        elif args.cmd == "alpha-run":
            names = [n for n in args.drivers.split(",") if n]
            if not names or any(n not in DRIVER for n in names):
                raise LupusError("DRIVER_UNKNOWN", args.drivers)
            if args.background:
                _out(jobs.start(k, ["alpha-run", "--drivers", ",".join(names), "--max-steps", str(args.max_steps),
                                    "--timeout", str(args.timeout)]))
            else:
                stale = {DRIVER[n]: why for n in names if (why := _stale(k, n, DRIVER[n]))}
                report = alpha.run(k, [DRIVER[n] for n in names], adapters.native, max_steps=args.max_steps,
                                   timeout_s=args.timeout, unavailable=stale)
                try:
                    report["vault"] = vault.sync(k)
                except LupusError as exc:
                    report["vault"] = {"error": exc.code}
                _out(report)
        elif args.cmd == "alpha-budget":
            caps = {"calls": args.calls, "attempts": args.attempts, "active_ms": int(args.minutes * 60_000),
                    **({"tokens": args.tokens} if args.tokens else {})}
            _confirm_user(f"공유 예산 설정({args.project or '전체'}): {caps}. 이후에 만드는 목표에 적용되며, 다 쓰면 올릴 때까지 새 작업이 시작되지 않습니다")
            _out(alpha.set_budget(k, args.project, caps, "user", args.reason))
        elif args.cmd == "goal-priority":
            _confirm_user(f"우선순위 변경: {args.goal_id} -> {args.priority}")
            alpha.set_priority(k, args.goal_id, args.priority, "user")
        elif args.cmd == "jobs":
            _out(jobs.listing(k))
        elif args.cmd == "logs":
            print(jobs.log_tail(k, args.job_id, args.lines))
        elif args.cmd == "stop":
            _out(jobs.stop(k, args.job_id))
        elif args.cmd == "learn":
            return _learn(k, args)
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
            _out({"revocation_epoch": projects.revoke(k, args.project_id, "user", args.reason),
                  "next": "to let a goal of this project continue later: lupus revalidate <goal_id> --note \"...\""})
        elif args.cmd == "revalidate":
            _confirm_user(f"권한 철회 이후 남은 범위로 이 목표를 계속해도 됨을 확인: {args.goal_id} · {args.note}")
            _out({"checkpoint_revision": recovery.revalidate(k, args.goal_id, "user", args.note)["revision"]})
        elif args.cmd == "project-remove":
            _confirm_user(f"프로젝트 등록 취소(목표가 없는 경우만): {args.project_id}")
            projects.remove(k, args.project_id, "user")
        elif args.cmd == "prune":
            _confirm_user(f"{args.days:g}일보다 오래된 보관 파일·작업 로그·세션 임시 파일 삭제")
            _out(jobs.prune(k, args.days))
        elif args.cmd == "approve":
            return _approve(k, args.goal_id)
        elif args.cmd == "revise":
            _confirm_user(f"문서 수정 요청: {args.goal_id} · {args.feedback}")
            author.revise(k, args.goal_id, args.feedback, "user")
            report = _run(k, args)
            if report["completion_blockers"] == ["EVIDENCE_FAIL:c2"]:
                return _approve(k, args.goal_id)
            _out(report)
            return 1
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
