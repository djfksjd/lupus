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
import time
import tempfile
from pathlib import Path

from . import supervision, auto_learning, skills, adapters, adaptive, alpha, author, budget, contract, crosscheck, gitx, goals, graph, jobs, judging, learn, memory, probe, projects, protect, quick, textedit, recovery, release, review, runners, session, shell, supervisor, vault, providers, runs
from .kernel import Kernel
from .util import LupusError, diagnostic_tail, timeout_ms


def _parallel_value(value: str) -> int | str:
    if value == "auto":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("choose 'auto' or an integer from 1 to 4") from None


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home or os.environ.get("LUPUS_HOME") or Path.home() / ".lupus")


JSON_OUTPUT = False


def _out(data) -> None:
    """A person at a terminal gets a few lines; a pipe, a log file or `--json` gets everything."""
    auto_learning.finish(data)
    if isinstance(data, dict) and "steps" in data and sys.stdout.isatty() and not JSON_OUTPUT:
        print(_summary(data))
        return
    if isinstance(data, dict) and sys.stdout.isatty() and not JSON_OUTPUT:
        from . import presentation
        rendered = presentation.answer(data)
        if rendered is not None:
            print(rendered)
            return
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _summary(report: dict) -> str:
    done = report.get("done")
    lines = ["완료 이력 · 이번 실행은 상태 조회이며 새로 검증하지 않았습니다" if report.get("already_completed") else
             "✔ 완료: 모든 완료 조건을 Lupus가 직접 검증했습니다" if done else
             f"✘ 미완료 (목표 상태: {report.get('goal_status', '?')})"]
    for key, label in (("result", ""), ("check", "승인한 검사"), ("implementation", "구현"), ("review", "리뷰"), ("out", "결과물"), ("tests", "테스트")):
        if report.get(key):
            lines.append(f"  {label + ': ' if label else ''}{report[key]}")
    if report.get("next") and not done:
        lines.append("  다음: " + report["next"].replace("\n", "\n        "))
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
        advisory=step.get("supervision")
        if advisory:
            lines.append("    "+diagnostic_tail(f"감독 / Supervisor: {advisory.get('requested_provider','?')} / {advisory.get('requested_model','?')} · {advisory.get('status','unknown')}"+(f" · {advisory['reason']}" if advisory.get('reason') else "")))
    used = (report.get("budget") or {})
    totals = ((report.get("usage") or {}).get("totals") or {}) if isinstance(report.get("usage"), dict) else {}
    tokens = sum(totals.get(key, 0) for key in ("tokens_in", "tokens_cached", "tokens_out"))
    parts = []
    if "attempts" in used:
        parts.append(f"시도 {used['attempts']['used']}/{used['attempts']['cap']}")
    usage_report = report.get('usage') if isinstance(report.get('usage'), dict) else {}
    levels = usage_report.get('events_by_observation') or {}
    if (levels.get('unknown', 0) or levels.get('estimated', 0) or
            usage_report.get('token_totals_complete') is False and (used.get('calls') or {}).get('used', 0) > 0):
        lower = usage_report.get('known_token_lower_bound', tokens)
        parts.append(f"토큰 ≥{lower:,} (총량 미관측)")
    elif tokens:
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


def _adapter(name: str, model: str | None, shell: bool = False, effort: str | None = None, *, home=None) -> adapters.Adapter:
    selected = {"effort": effort} if effort is not None else {}
    if name.startswith("native_"): name = name[7:]
    if name == "claude" and shell:
        return adapters.claude_worker(model, shell=True, **selected)
    if name in ("codex", "claude"):
        return adapters.native({"claude": "native_claude", "codex": "native_codex"}[name], model, **selected)
    return adapters.APIAdapter(name, model, effort, home=home, shell=shell)


def _api_skill_access(args: argparse.Namespace, home: Path) -> dict | None:
    """Only CLI primary workers get this capability; creating it never scans a library."""
    mode=getattr(args,"skills",None) or "auto"
    explicit=[args.skill_binding] if getattr(args,"skill_binding",None) else []
    if mode=="off" and not explicit:return None
    return {"mode":mode,"home":str(home.resolve()),"user_home":str(Path.home().resolve()),"explicit":explicit}


def _isolate(k: Kernel, project: dict, args: argparse.Namespace) -> dict:
    if not getattr(args, "isolated", False):
        return project
    isolated = gitx.isolate(k, project, "user")
    print(f"격리된 체크아웃에서 작업합니다(커밋 {isolated['base'][:8]} 기준). 지금 작업 폴더의 파일은 건드리지 않습니다."
          + (" 커밋하지 않은 변경은 이 작업에 포함되지 않습니다." if isolated["origin_has_uncommitted_changes"] else ""), file=sys.stderr)
    return isolated


def _reviewed(k: Kernel, args: argparse.Namespace, report: dict, draft: dict, build: dict) -> dict:
    """`lupus do --review`: the independent read, one round of answers, and what is left."""
    selected=getattr(args,"review_selection",None)
    reviewer = selected["provider"] if selected else (args.review if args.review != "other" else ("codex" if args.driver == "claude" else "claude"))
    review_driver=selected["driver"] if selected else providers.resolve(reviewer,k.home).driver
    review_options={name:selected[name] for name in ("model","effort") if selected and selected[name] is not None}
    review_label=reviewer+(" · model "+review_options["model"] if review_options.get("model") else "")+(" · effort "+review_options["effort"] if review_options.get("effort") else "")
    if "review_base" not in build:
        report["review"] = f"리뷰하지 못했습니다: {build.get('review_unavailable', '기준 상태를 보관하지 못함')}"
        return report
    print(f"검사가 통과했습니다. 별도 리뷰({review_label})가 요청과 변경을 대조합니다…", file=sys.stderr)

    def run(goal_id: str) -> dict:
        args.goal_id = goal_id
        return _run(k, args)

    outcome = review.cycle(k, report["goal_id"], draft["request"], review_driver, Path(build["review_base"]), run,
                           quick.DEFAULT_CAPS, skip={draft["test_path"]},**review_options)
    said = lambda items: "; ".join(f"「{o['request_quote'][:60]}」 {o['problem'][:160]}" for o in items)
    if outcome.get("recovery_required"):
        report.update(done=False,goal_id=outcome["goal_id"],goal_status=outcome["goal_status"])
        report["review"]=f"별도 리뷰({review_label}): 종료·권한 또는 복원 상태를 확인하지 못했습니다. 복구가 필요합니다. 사본: {outcome['backup_kept_in']}"
        if outcome.get("revision_report"):
            report["checks"]=outcome["revision_report"].get("checks",[])
    elif "skipped" in outcome:
        report["review"] = f"리뷰하지 못했습니다({review_label}): {outcome['skipped']}"
    elif not outcome.get("objections"):
        report["review"] = f"별도 리뷰({review_label}): 요청과 어긋나는 점을 찾지 못했습니다 (의견이며 증명이 아닙니다)"
    elif not outcome["revised"]:
        report["review"] = (f"별도 리뷰({review_label}): 이의 {len(outcome['objections'])}건. 되돌아갈 백업을 만들 수 없어 worker에게 "
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
            report["review"] = (f"별도 리뷰({review_label}): 이의 {len(outcome['objections'])}건을 worker에게 돌려보냈고 검사는 다시 통과했습니다. "
                                + (f"다시 리뷰하지는 못했습니다({outcome['second_review_skipped']}): 이의가 해소됐는지는 확인되지 않았습니다"
                                   if left is None else
                                   f"리뷰어가 여전히 지적하는 것 {len(left)}건: {said(left)}" if left else "남은 이의 없음"))
        else:
            report["review"] = (f"별도 리뷰({review_label}): 이의 {len(outcome['objections'])}건을 반영하려던 수정이 승인된 검사를 통과하지 못해 "
                                f"통과했던 상태로 되돌렸습니다(밀려난 파일은 {outcome['displaced_kept_in']} 에 보관). "
                                f"반영되지 않은 이의: {said(outcome['objections'])}")
            if outcome["not_put_back"]:
                # The project is not the state the checks passed on: this is no longer a verified result.
                report["done"] = False
                report["review"] += (f" ⚠ 되돌리지 못한 파일이 있어 검증된 상태가 아닙니다: {', '.join(outcome['not_put_back'][:10])}. "
                                     f"통과했던 상태의 사본: {outcome['backup_kept_in']} (확인이 끝날 때까지 지워지지 않습니다. 지우려면 `lupus prune --kept`)")
    report["review_detail"] = {key: outcome[key] for key in (
        "reviewer", "objections", "remaining", "dropped", "revised", "revision_done", "skipped", "files",
        "second_review_skipped", "not_revised", "not_put_back", "displaced_kept_in", "backup_kept_in",
        "requested_driver","requested_model","requested_effort","independence_confirmed","recovery_required","recovery_reason","goal_status","budget_overcommitted","review_phases") if key in outcome}
    current=goals.get(k,report["goal_id"])
    report["budget"]=budget.snapshot(k,current["budget_id"])
    report["review_usage"]={"phases":outcome.get("review_phases",[]),"scope":"separate recorded review calls; not added again to primary observations"}
    report["model_calls_scope"]="primary worker invocations; reviewer host/internal calls are reported separately"
    return report


def _after_isolated(k: Kernel, report: dict) -> None:
    if gitx.info(k, goals.get(k, report["goal_id"])["project_id"]):
        goal_id = report["goal_id"]
        report["next"] = (f"lupus diff {goal_id}   (결과 보기)   ·   lupus accept {goal_id}   (내 저장소에 커밋 하나로 반영)   ·   "
                          f"lupus discard {goal_id}   (버리기)")


GLOBAL_FLAGS = ("--json",)
GLOBAL_OPTIONS = ("--home",)
def _global_part(argv: list[str]) -> list[str] | None:
    """`argv` when it holds nothing but the global options (so no command was named), else None."""
    i = 0
    while i < len(argv):
        if argv[i] in GLOBAL_FLAGS or any(argv[i].startswith(o + "=") for o in GLOBAL_OPTIONS):
            i += 1
        elif argv[i] in GLOBAL_OPTIONS and i + 1 < len(argv):
            i += 2
        else:
            return None
    return argv


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _start(options: list[str]) -> int:
    """`lupus` on its own: the Lupus shell in the folder the user stands in (see shell.py). The
    runtime is created on first use; the driver is $LUPUS_DRIVER (default claude). Without a
    terminal there is no prompt: the home screen is printed once."""
    parser = argparse.ArgumentParser(prog="lupus", add_help=False)
    parser.add_argument("--home")
    parser.add_argument("--json", action="store_true")
    home = _home(parser.parse_args(options))
    driver = os.environ.get("LUPUS_DRIVER", "codex")
    if driver not in {spec["id"] for spec in providers.catalog(home)}:
        print(json.dumps({"error": "DRIVER_UNKNOWN", "detail": f"LUPUS_DRIVER={driver}", "next": "LUPUS_DRIVER=claude 또는 codex"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    if not (home / "runtime" / "lupus.db").exists():
        Kernel.init(home).close()
        print(f"처음 실행: Lupus 작업 공간을 만들었습니다 ({home})", file=sys.stderr)
    if not _interactive():
        print("\n".join(shell.home_screen(shell.facts(home, os.getcwd(), driver), shell.Paint(), shell.language(), 80)))
        return 0
    return shell.run(options, home, driver, main)


def main(argv: list[str] | None = None) -> int:
    actual=list(sys.argv[1:] if argv is None else argv)
    if _global_part(actual) is not None or auto_learning._CURRENT.get() is not None:
        return _main(actual)
    controller=auto_learning.Session();token=auto_learning._CURRENT.set(controller)
    try:return _main(actual)
    finally:
        try:controller.finish(False)
        finally:auto_learning._CURRENT.reset(token)


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    options = _global_part(argv)
    if options is not None:
        return _start(options)
    parser = argparse.ArgumentParser(prog="lupus", description="Lupus supervisor. `lupus` alone, in a project folder with tests, "
                                     "opens your usual claude (or $LUPUS_DRIVER) session under Lupus.")
    parser.add_argument("--home", help="runtime directory (default: $LUPUS_HOME or ~/.lupus)")
    parser.add_argument("--json", action="store_true", help="print full reports as JSON even at a terminal")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the runtime directory and database")
    sub.add_parser("providers", help="configured providers and credential references; no network")
    p = sub.add_parser("provider-allow", help="approve a provider endpoint for an existing project; no network")
    p.add_argument("provider")
    p.add_argument("--project")
    p = sub.add_parser("models", help="configured model IDs; discovery only when requested")
    p.add_argument("--driver", required=True)
    p.add_argument("--discover", action="store_true")
    for command in ("ask", "agent"):
        p = sub.add_parser(command, help="read-only answer" if command == "ask" else "bounded coding without test authoring or completion certification")
        p.add_argument("request")
        p.add_argument("--driver", default=os.environ.get("LUPUS_DRIVER", "codex"))
        p.add_argument("--model")
        p.add_argument("--effort")
        p.add_argument("--timeout", type=float, default=300)
        if command == "agent":
            p.add_argument("--shell", action="store_true")
            p.add_argument("--native-config", choices=("lean","user"), default="lean", help="user: Codex configuration/rules/tools inside the required OS sandbox; backend/auth/billing not confirmed")
            p.add_argument("--native-env", action="append", default=[], metavar="NAME", help="explicitly pass one environment variable by name to user-configured Codex; LUPUS records selected names only")

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
    p.add_argument("--supervisor", metavar="PROVIDER", help="optional on-failure advice; off by default, explicit model required")
    p.add_argument("--supervisor-model", metavar="MODEL")
    p.add_argument("--supervisor-effort", metavar="EFFORT")
    p.add_argument("goal_id")
    p.add_argument("--driver", required=True)
    p.add_argument("--model")
    p.add_argument("--effort", choices=adapters.CODEX_EFFORTS,
                   help="requested primary-worker effort; available levels depend on the model/client")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--stop-stale-writer", action="store_true",
                   help="terminate a worker left over from a dead supervisor before continuing")
    p.add_argument("--cheap-first", action="store_true",
                   help="try a lighter model/effort first; use the default only if verification fails")
    p.add_argument("--background", action="store_true", help="keep running after this terminal closes; see `lupus jobs`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    for name, text in (("diff", "what a goal changed"), ("accept", "bring an isolated goal's verified result into your repository as one commit"),
                       ("discard", "drop an isolated goal's checkout and branch"),
                       ("undo", "undo a recorded accepted commit at clean exact HEAD, adding a new commit")):
        sub.add_parser(name, help=text).add_argument("goal_id")

    sub.add_parser("alpha-status", help="every project at a glance: open goals, what they wait for, shared budgets")
    p = sub.add_parser("alpha-run", help="advance every unfinished goal in turn; switch AI when one is out of quota")
    p.add_argument("--drivers", default=os.environ.get("LUPUS_DRIVER", "codex"), help="comma list, in order of preference")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--background", action="store_true")
    p.add_argument("--model", help="explicit worker model for every selected provider")
    p.add_argument("--effort", help="explicit effort; unsupported API mappings refuse")
    p.add_argument("--parallel", type=_parallel_value, default=1,
                   choices=["auto", *range(1, alpha.MAX_PARALLEL + 1)], metavar=f"auto|1..{alpha.MAX_PARALLEL}",
                   help="goals worked on at the same time, each in a different project or isolated checkout; auto chooses a conservative run-start cap (default 1)")
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
    p=sub.add_parser("job-run", help=argparse.SUPPRESS)
    p.add_argument("job_id")
    p.add_argument("--guardian",action="store_true",help=argparse.SUPPRESS)
    p = sub.add_parser("learn", help="turn recorded failures of a project into candidate procedures (one small model call)")
    p.add_argument("--project", help="project id (default: this folder's project)")
    p.add_argument("--driver", choices=("claude", "codex"))
    p.add_argument("--undo", nargs="?", const="", metavar="PASS",
                   help="take back what a learning pass added (default: the latest one); no model call")

    p = sub.add_parser("fix-tests", help="one command, no goal file: make this folder's failing tests pass")
    p.add_argument("--supervisor", metavar="PROVIDER", help="optional on-failure advice; off by default, explicit model required")
    p.add_argument("--supervisor-model", metavar="MODEL")
    p.add_argument("--supervisor-effort", metavar="EFFORT")
    p.add_argument("--driver", required=True)
    p.add_argument("--model")
    p.add_argument("--effort", choices=adapters.CODEX_EFFORTS,
                   help="requested primary-worker effort; available levels depend on the model/client")
    p.add_argument("--max-attempts", type=int, choices=(1, 2, 3),
                   help="persist a worker-attempt cap for this request (default: existing bounded retry policy)")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--isolated", action="store_true",
                   help="work in a separate checkout of the committed HEAD; your files are untouched until `lupus accept`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    p.add_argument("--lean", action="store_true",
                   help="add implementation-economy guidance to the worker: reuse, standard library, smallest change "
                        "(adapted from Ponytail; off by default, it can cost more on some models)")
    p.add_argument("--check", help="your own test command for a project Lupus does not recognise; its exit status decides")
    p.add_argument("--protect", action="append", default=[], help="with --check: a file or folder workers may not change")
    p.add_argument("--container", metavar="IMAGE", help="run the tests in a Docker container of this image (no network) "
                                                        "instead of the host sandbox")
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("write", help="a document (plan, research, report): a rubric you approve, a judge that must quote, your sign-off")
    p.add_argument("request")
    p.add_argument("--out", required=True, help="file to write, relative to this folder")
    p.add_argument("--driver", required=True)
    p.add_argument("--judge", help="default: the other AI when it is approved and measured")
    p.add_argument("--must", action="append", default=[], help="a criterion the document must meet (repeatable)")
    p.add_argument("--no-draft", action="store_true", help="use only --must criteria; do not ask a model to propose any")
    p.add_argument("--web", action="store_true", help="let the writer search and read the web (claude only)")
    p.add_argument("--min-chars", type=int, default=400)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(goal_id=None, max_steps=4, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("session", help="your usual interactive claude/codex, with the tests frozen and verified by Lupus at the end")
    p.add_argument("--driver", required=True)
    p.add_argument("--check", help="your own test command; its exit status decides")
    p.add_argument("--protect", action="append", default=[], help="with --check: a file or folder that may not change")
    p.add_argument("--container", metavar="IMAGE", help="run the tests in a Docker container of this image (no network)")
    p.add_argument("--max-minutes", type=float, default=240)
    p.add_argument("--no-stop-check", action="store_true", help="do not run the tests each time the model stops (claude)")
    p.add_argument("cli_args", nargs=argparse.REMAINDER, help="after --: passed to the CLI as they are")
    p.set_defaults(goal_id=None, max_steps=1, stop_stale_writer=False, cheap_first=False, model=None)

    p = sub.add_parser("auto", help="read-only questions use one worker invocation (API: up to 8 HTTP requests); changes use supplied checks or approved tests")
    p.add_argument("--supervisor", metavar="PROVIDER", help="optional on-failure advice; off by default, explicit model required")
    p.add_argument("--supervisor-model", metavar="MODEL")
    p.add_argument("--supervisor-effort", metavar="EFFORT")
    p.add_argument("request")
    p.add_argument("--driver", default=os.environ.get("LUPUS_DRIVER", "codex"))
    p.add_argument("--mode", choices=("auto", "answer", "verified"), default="auto")
    p.add_argument("--max-attempts", type=int, choices=(1, 2, 3),
                   help="persist a worker-attempt cap for checked or failing-test repair requests")
    p.add_argument("--check", help="acceptance command supplied by you: skip drafting a new test")
    p.add_argument("--protect", action="append", default=[])
    p.add_argument("--isolated", action="store_true")
    p.add_argument("--shell", action="store_true")
    p.add_argument("--model")
    p.add_argument("--effort", choices=adapters.CODEX_EFFORTS,
                   help="requested primary-worker effort; available levels depend on the model/client")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("do", help="one line, any request: draft a failing test, you approve it, then implement")
    p.add_argument("--supervisor", metavar="PROVIDER", help="optional on-failure advice; off by default, explicit model required")
    p.add_argument("--supervisor-model", metavar="MODEL")
    p.add_argument("--supervisor-effort", metavar="EFFORT")
    p.add_argument("request")
    p.add_argument("--isolated", action="store_true",
                   help="work in a separate checkout of the committed HEAD; your files are untouched until `lupus accept`")
    p.add_argument("--shell", action="store_true", help="let the worker run commands to check its own work (inside the sandbox)")
    p.add_argument("--lean", action="store_true",
                   help="add implementation-economy guidance to the worker: reuse, standard library, smallest change "
                        "(adapted from Ponytail; off by default, it can cost more on some models)")
    p.add_argument("--allow-failing", action="store_true",
                   help="start even though some existing tests fail now; those same tests may keep failing, no others")
    p.add_argument("--review", nargs="?", const="other", metavar="PROVIDER",
                   help="separate read-only opinion after checks pass; one bounded revision, not independent-quality proof")
    p.add_argument("--review-model", help="requested reviewer model, separate from primary/supervisor/cross-check")
    p.add_argument("--review-effort", help="requested reviewer effort; unsupported API mappings refuse")
    p.add_argument("--cross-check", help="independently author behaviour tests after DONE (experimental)")
    p.add_argument("--cross-check-model", help="requested cross-check author model, separate from the primary worker")
    p.add_argument("--two-step", action="store_true",
                   help="write the implementation in a separate call after you approved the test (default: proposed "
                        "in the same call, kept aside until you approve)")
    p.add_argument("--driver", required=True)
    p.add_argument("--model")
    p.add_argument("--effort", choices=adapters.CODEX_EFFORTS,
                   help="requested primary-worker effort; available levels depend on the model/client")
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(goal_id=None, max_steps=6, stop_stale_writer=False, cheap_first=False)

    p = sub.add_parser("approve", help="read and sign off the document of a `write` goal that is waiting for you")
    p.add_argument("goal_id")
    p = sub.add_parser("revise", help="send the document of a `write` goal back with your feedback")
    p.add_argument("goal_id")
    p.add_argument("feedback")
    p.add_argument("--driver", required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(max_steps=4, stop_stale_writer=False, cheap_first=False)
    p = sub.add_parser("revalidate", help="after `revoke`: confirm that a goal may continue with what is left of its scope")
    p.add_argument("goal_id")
    p.add_argument("--note", required=True)
    sub.add_parser("project-remove", help="take back a registration that has no goals").add_argument("project_id")
    p = sub.add_parser("prune", help="delete old set-aside files, job logs and session scratch files")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--kept", action="store_true",
                   help="also delete the project copies a review left behind after an incomplete way back (listed as kept_for_you)")

    p = sub.add_parser("recover", help="close runs left by a crash and reconcile recovery objects")
    p.add_argument("--stop-stale-writer", action="store_true")

    p = sub.add_parser("release", help="let named frozen test or fixture files change for a goal whose request changes what they pin; "
                                       "the worker's edits to them need your approval of the exact diff")
    p.add_argument("goal_id")
    p.add_argument("paths", nargs="+", help="frozen files or folders, relative to the project")
    p = sub.add_parser("release-approve", help="look at the proposed change to released files and approve exactly that diff")
    p.add_argument("goal_id")
    p = sub.add_parser("release-reject", help="refuse the proposed change to released files; the frozen versions stand")
    p.add_argument("goal_id")
    p.add_argument("--note", default="")
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
    p.add_argument("--serve", action="store_true", help="serve the viewer with local knowledge chat")
    p.add_argument("--port", type=int, default=8767)

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

    p.add_argument("--driver", help="probe only this native provider")
    p = sub.add_parser("cross-check", help="independently check a DONE do goal")
    p.add_argument("goal")
    p.add_argument("--driver", required=True)
    p.add_argument("--model", help="requested independent author model")

    for command in ("ask", "agent", "auto", "do", "fix-tests"):
        sub.choices[command].add_argument("--skill", help="apply a selected local skill to the primary worker; routing and acceptance keep the original request")
    for command in ("ask", "agent", "auto", "do", "fix-tests", "run", "write", "alpha-run"):
        sub.choices[command].add_argument("--skills", choices=("auto","off"), help="API primary workers only: discover/read local skills on demand or disable discovery; explicit --skill remains selected")
    args = parser.parse_args(argv)
    global JSON_OUTPUT
    JSON_OUTPUT = args.json
    try:
        if args.cmd == "job-run":
            return _job_run(_home(args), args.job_id, args.guardian)
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
    "PROVIDER_NOT_APPROVED": "approve the selected endpoint: lupus provider-allow <provider-id> [--project <id>]",
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


def _job_run(home: Path, job_id: str, guardian: bool = False) -> int:
    """Trusted gated job controller; model tasks never enter the lifecycle protocol."""
    from . import job_guardian
    return job_guardian.run(home,job_id) if guardian else job_guardian.inner(home,job_id,main)


def _stale(k: Kernel, cli: str, driver: str) -> str | None:
    if driver not in ("native_codex", "native_claude"):
        return None
    measured = {r["cli_version"] for r in k.q(
        "SELECT cli_version FROM capability WHERE adapter = ? AND status = 'verified'", driver)}
    if not measured:
        return None
    installed = probe._version(cli, adapters.worker_env())
    if measured != {installed}:
        # Capabilities are measurements of one CLI version, not promises about the next.
        return f"measured {sorted(measured)}, installed {installed}: run `lupus probe --live`"
    return None



def _worker_preflight(args: argparse.Namespace, home: Path):
    """Validate selected worker settings only when a worker path is required."""
    spec = providers.resolve(args.driver, home)
    if getattr(args,"skills",None) is not None and spec.protocol == "native":
        raise LupusError("SKILL_POLICY_UNSUPPORTED", "--skills controls API primary workers only; native skills follow the native profile")
    if spec.protocol != "native":
        if getattr(args,"model",None) is not None:providers.validate_model(args.model)
        if not getattr(args,"model",None) and not spec.default_model:
            raise LupusError("MODEL_REQUIRED", "select --model <id> or configure default_model")
        if spec.api_key_env and not os.environ.get(spec.api_key_env):
            raise LupusError("API_AUTH_MISSING", "set the selected provider API key environment variable")
    return spec


def _run(k: Kernel, args: argparse.Namespace) -> dict:
    with k.supervisor_lock():     # held from recovery through the whole run
        report = supervisor.recover(k, args.stop_stale_writer)
        if report["writers_alive"]:
            raise LupusError("WRITER_NOT_STOPPED", json.dumps(report["writers_alive"]))
        if goals.get(k, args.goal_id)["status"] == "DONE":
            return supervisor.completed_report(k, args.goal_id)
        if getattr(args,"cmd",None) == "run":
            _worker_preflight(args,k.home)
        auto_learning.prepare(k,mutating=True)
        selected = {"effort": args.effort} if getattr(args, "effort", None) is not None else {}
        if args.driver not in ("claude", "codex"): selected["home"] = k.home
        adapter = getattr(args, "adapter", None) or _adapter(args.driver, args.model, getattr(args, "shell", False), **selected)
        if getattr(args, "primary_instructions", None):
            adapter.primary_instructions = args.primary_instructions
        if isinstance(adapter,adapters.APIAdapter):adapter.skill_access=_api_skill_access(args,k.home)
        stale = _stale(k, args.driver, adapter.driver)
        if stale:
            raise LupusError("CAPABILITY_STALE", stale)
        if args.cheap_first and adapter.driver.startswith("api:"): raise LupusError("TIERS_UNSUPPORTED", "API profiles require an explicit model; no silent fallback")
        tiers = adapters.cheap_first(adapter.driver) if args.cheap_first and not args.model else None
        if tiers and getattr(args, "primary_instructions", None):
            for tier in tiers:
                tier.primary_instructions = args.primary_instructions
        report = supervisor.run_goal(k, args.goal_id, adapter, max_steps=args.max_steps, timeout_s=args.timeout,
                                     tiers=tiers, **({"supervision":args.supervisor_role} if getattr(args,"supervisor_role",None) else {}))
        if getattr(args,"supervisor_role",None):report['requested_supervisor']={**args.supervisor_role,'scope':'verified retry only','effective_backend_confirmed':False}
        if isinstance(adapter,adapters.APIAdapter):
            report['api_skill_mode']=getattr(args,'skills',None) or 'auto'
            if getattr(adapter,'last_skill_reads',None):report['last_worker_skill_reads']=adapter.last_skill_reads
        if getattr(args, "skill_info", None):
            report["selected_skill"] = args.skill_info
        if getattr(args, "effort", None) is not None:
            report["requested_worker_effort"] = args.effort
        if getattr(args, "max_attempts", None) is not None:
            report["attempt_limit"] = report["budget"]["attempts"]["cap"]
            blocked_attempts = any("BUDGET_EXHAUSTED:attempts" in step.get("blockers", []) for step in report["steps"])
            if not report["done"] and blocked_attempts:
                report["stop_reason"] = "attempt_limit_reached"
        return report


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
        selected_spec = providers.resolve(args.driver, k.home) if args is not None and getattr(args, "driver", None) else None
        endpoint = f" · {selected_spec.name} {selected_spec.base_url}" if selected_spec and selected_spec.protocol != "native" else ""
        _confirm_user(f"이 폴더를 프로젝트로 등록합니다: {root} ({what}){endpoint}")
        project = projects.register(k, root, os.path.basename(root), ["anthropic", "openai"] + ([providers.resolve(args.driver, k.home).provider] if args is not None and getattr(args, "driver", None) not in (None, "claude", "codex") else []))
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
    writer = providers.resolve(args.driver, k.home).driver
    judge = providers.resolve(args.judge, k.home).driver if args.judge else author.pick_judge(k, project, writer)
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
        args.adapter = adapters.claude_worker(args.model, tools=("Read", "Write", "Edit", "Glob", "Grep"), web=args.web)
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
    if args.undo is not None:
        _confirm_user(f"프로젝트 {project['name']}의 학습 묶음 {args.undo or '(가장 최근)'}이 추가한 절차 후보를 철회합니다")
        _out(learn.undo(k, project, "user", args.undo or None))
        return 0
    if not args.driver:
        raise LupusError("DRIVER_REQUIRED", "lupus learn --driver claude|codex")
    cases = learn.triggers(k, project["project_id"])
    if not cases:
        _out({"learned": [], "note": "배울 사건이 없습니다(재시도나 막힌 작업이 없었음). 모델을 호출하지 않았습니다"})
        return 0
    _confirm_user(f"프로젝트 {project['name']}의 실패 기록 {len(cases)}건에서 절차 후보를 만듭니다({args.driver}, 모델 호출 1회). "
                  "작업 제목과 검증기 출력만 전달되며 프로젝트 파일은 전달되지 않습니다")
    with k.supervisor_lock():
        supervisor.recover(k)
        _out(learn.refine(k, project, providers.resolve(args.driver, k.home).driver, "user"))
    return 0


def _session(k: Kernel, args: argparse.Namespace) -> int:
    timeout_ms(args.max_minutes)
    # A session creates one bounded verifier; this is range-only, not a larger reservation.
    timeout_ms(args.max_minutes * 60, extra_ms=2 * 86_400_000 + 120_000, clock_ms=k.now())
    project = _project_here(k, "대화형 세션", args)
    if providers.resolve(args.driver, k.home).provider not in project["approved_providers"]:
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



def _agent(k: Kernel, args: argparse.Namespace) -> int:
    timeout_ms(args.timeout)
    timeout_ms(args.timeout, clock_ms=k.now())
    spec = providers.resolve(args.driver, k.home)
    adapter = None if args.native_config == "user" else _adapter(args.driver, args.model, args.shell, args.effort, **({'home':k.home} if spec.protocol!='native' else {}))
    if adapter is not None and getattr(args, "primary_instructions", None):
        adapter.primary_instructions = args.primary_instructions
    if isinstance(adapter,adapters.APIAdapter):adapter.skill_access=_api_skill_access(args,k.home)
    with k.supervisor_lock():
        project = _project_here(k, 'agent edits · unverified', args)
        if not projects.provider_allowed(project, adapter.driver if adapter is not None else spec.driver):
            raise LupusError('PROVIDER_NOT_APPROVED', 'approve selected provider endpoint: ' + spec.provider)
        if k.one("SELECT 1 FROM run WHERE project_id=? AND status<>'STOPPED'", project['project_id']) or runs.aux_alive(k, project['project_id']):
            raise LupusError('WRITER_NOT_STOPPED', 'another operation owns this project')
        auto_learning.prepare(k,mutating=True)
        reference, context_info = memory.primary_context(k,project['project_id'],args.request)
        primary_prompt = args.request + reference
        operation = 'agent_' + os.urandom(8).hex()
        native_runtime = None
        if args.native_config == "user":
            native_runtime = Path(tempfile.mkdtemp(prefix="lupus-native-agent-" )).resolve()
            root = Path(project['canonical_root']).resolve()
            if native_runtime.is_relative_to(root) or native_runtime.is_relative_to(k.home.resolve()):
                shutil.rmtree(native_runtime)
                raise LupusError("NATIVE_RUNTIME_LOCATION", "native scratch must be outside the project and Lupus state")
            try:
                for name in ("log","sqlite"):
                    (native_runtime/name).mkdir(mode=0o700)
                adapter = adapters.CodexUserConfigAdapter(args.model,args.effort,private_tmp=native_runtime,native_env=args.native_env,read_exclude=(str(k.home.resolve()),))
                if getattr(args,"primary_instructions",None):adapter.primary_instructions=args.primary_instructions
            except BaseException:
                shutil.rmtree(native_runtime)
                raise
        try:
            bid = budget.create(k, operation, {'calls':8,'attempts':1,'active_ms':int(args.timeout*1000),'tokens':200000}, parent_budget_id=alpha.parent_budget(k,project['project_id']))
            reservation = budget.reserve(k, bid, 'work', 'unverified agent edit', {'calls':8,'attempts':1,'active_ms':int(args.timeout*1000)})
            k.emit('user', 'agent.started', 'project', project['project_id'], operation_id=operation, budget_id=bid, provider=spec.id, requested_model=args.model or spec.default_model, verified=False,
                   **({'requested_native_config':'user','requested_native_permission_profile':'lupus','effective_native_permissions_confirmed':False,'outer_auth_read_scope':'cli_and_descendants','requested_native_env':list(args.native_env),'native_runtime':str(native_runtime),'effective_backend_confirmed':False,'authentication_source':'user configuration; unconfirmed','billing_scope':'observed host usage only; configured tool/server costs unknown'} if native_runtime else {}))
        except BaseException:
            if native_runtime is not None:
                shutil.rmtree(native_runtime)
            raise
        pids=[];result=None;started=time.monotonic();interrupted=False;cleanup_observed=False
        def record(pid):
            runs.register_aux(k, project['project_id'], pid, 'agent:'+operation);pids.append(pid)
        try:
            result=adapters.execute(adapter,primary_prompt,Path(project['canonical_root']),record,timeout_s=args.timeout)
        except BaseException as exc:
            observed=adapters.cleanup_result(exc)
            if observed is not None:result=observed;cleanup_observed=True
            interrupted=not isinstance(exc,Exception)
            raise
        finally:
            info=adapters.usage_info(result.usage,result.raw) if result is not None else None
            actual={'calls':max(1, (result.usage or {}).get('calls',1)) if info['fields']['calls'] and adapters.valid_counter((result.usage or {}).get('calls')) else max(8, (result.usage or {}).get('calls') if cleanup_observed and adapters.valid_counter((result.usage or {}).get('calls')) else 8), 'attempts':1,'active_ms':max(int(args.timeout*1000),int((time.monotonic()-started)*1000)) if cleanup_observed else int((time.monotonic()-started)*1000)} if result else None
            if native_runtime is None:
                budget.settle(k,reservation,actual,'measured' if info and info['complete'] else 'estimated')
                if info:budget.charge_direct(k,bid,'tokens',info['known_token_lower_bound'])
                for pid in pids:runs.clear_aux(k,pid)
            else:
                cleanup_error=None
                try:
                    budget.settle(k,reservation,actual,'measured' if info and info['complete'] else 'estimated')
                    if info:budget.charge_direct(k,bid,'tokens',info['known_token_lower_bound'])
                except Exception as exc:cleanup_error=exc
                for pid in pids:
                    try:runs.clear_aux(k,pid)
                    except Exception as exc:cleanup_error=cleanup_error or exc
                if cleanup_error is None:
                    try:shutil.rmtree(native_runtime)
                    except Exception as exc:cleanup_error=exc
                # Any live fence is retained by clear_aux; a failed cleanup keeps
                # private evidence, even if already-dead auxiliaries were cleared.
                # Never replace a user cancellation with an ordinary cleanup diagnostic.
                if cleanup_error is not None and not interrupted:raise cleanup_error
        _started(args)
        k.emit('supervisor','agent.finished','project',project['project_id'],operation_id=operation,error=result.error_class,verified=False,process_lifetime=adapters.lifetime_diagnostics(result))
        _out({'status':'UNVERIFIED','verified':False,'memory_context':context_info,'result':result.text,'error':result.error_class,
              'exit_code':result.exit_code,'operation_id':operation,'budget':budget.snapshot(k,bid),'process_lifetime':adapters.lifetime_diagnostics(result),
              **({'tool_diagnostics':result.raw['tool_diagnostics']} if 'tool_diagnostics' in result.raw else {}),
              'usage':result.usage,'usage_coverage':info['fields'],'known_token_lower_bound':info['known_token_lower_bound'],
              'requested_provider':spec.id,'requested_model':getattr(adapter,'model',None),'effective_backend_confirmed':False,
              **({'api_skill_mode':getattr(args,'skills',None) or 'auto'} if spec.protocol!='native' else {}),
              **({'skill_reads':result.raw['skill_reads'],'skill_reads_limited':result.raw.get('skill_reads_limited',False)} if 'skill_reads' in result.raw else {}),
              **({'selected_skill':args.skill_info} if getattr(args,'skill_info',None) else {}),
              **({'requested_native_config':'user','requested_native_permission_profile':'lupus','effective_native_permissions_confirmed':False,'outer_auth_read_scope':'cli_and_descendants','requested_native_env':list(args.native_env),'effective_config_confirmed':False,'authentication_source':'user configuration; unconfirmed','billing_scope':'observed host usage only; configured tool/server costs unknown',
                  'outer_os_sandbox':result.raw.get('os_sandbox'),
                  'native_state_paths':{'log_dir':str(native_runtime/'log'),'sqlite_home':str(native_runtime/'sqlite'),'retained':native_runtime.exists()},
                  **({'detail':diagnostic_tail(result.stderr_tail),'native_config_limits':'The outer OS sandbox makes home-directory configuration and authentication files read-only, with the read grant inherited by CLI descendants; inner tool permissions are unconfirmed. Custom config roots are unsupported. Hooks/MCP needing other writable locations may fail; no unconfined fallback.'} if result.error_class else {})} if native_runtime else {})})
        return 1 if result.error_class or result.exit_code != 0 else 0

def _dispatch(args: argparse.Namespace) -> int:
    if hasattr(args, "timeout"):
        timeout_ms(args.timeout)
    if args.cmd == "session":
        timeout_ms(args.max_minutes)
        timeout_ms(args.max_minutes * 60)
    if getattr(args, "effort", None) is not None:
        for driver in (args.drivers.split(",") if args.cmd == "alpha-run" else (args.driver,)):
            adapters.validate_effort(driver, args.effort)
        if getattr(args, "cheap_first", False):
            raise LupusError("EFFORT_CONFLICT", "--effort cannot be combined with --cheap-first")
    home = _home(args)
    args.supervisor_role=supervision.resolve(getattr(args,"supervisor",None),getattr(args,"supervisor_model",None),getattr(args,"supervisor_effort",None),home=home)
    if args.cmd in ("ask","agent","auto","do"):
        auto_learning.bind(home,args.request)
    if args.cmd == "agent":
        args.native_env = list(adapters.validate_native_config(args.driver, args.native_config, args.native_env))
    if getattr(args, "skill", None):
        selected_skill = skills.discover(Path.cwd(), home, Path(_repo_root(os.getcwd()))).get(args.skill)
        if selected_skill is None:
            raise LupusError("SKILL_UNKNOWN", "Use /skills to see local IDs")
        body = skills.load(selected_skill)
        args.primary_instructions = (f"Skill: {selected_skill.id}\nSkill source: {selected_skill.path}\n"
                                     f"Relative resources: {selected_skill.resolved_path.parent}\n"
                                     "Resources remain subject to the worker's file/tool permissions.\n\n" + body)
        args.skill_info = {"id":selected_skill.id,"source":selected_skill.source,"sha256":hashlib.sha256(body.encode()).hexdigest()}
        args.skill_binding = skills.binding(selected_skill,args.skill_info['sha256'])
    if getattr(args,"skills",None) is not None and args.cmd != "run":
        choices=args.drivers.split(',') if args.cmd=='alpha-run' else [args.driver]
        if not any(providers.resolve(driver,home).protocol!='native' for driver in choices):
            raise LupusError('SKILL_POLICY_UNSUPPORTED','--skills controls API primary workers only; native skills follow the native profile')
    if args.cmd == "providers":
        _out(providers.catalog(home)); return 0
    if args.cmd == "models":
        _out(providers.models(args.driver, home, discover=args.discover)); return 0
    if getattr(args, "driver", None) and args.cmd != "run":
        providers.resolve(args.driver, home)
    if getattr(args, 'cross_check_model', None) and not getattr(args, 'cross_check', None):
        raise LupusError('MODEL_INVALID', '--cross-check-model requires --cross-check')
    review_id=getattr(args,"review",None)
    if not review_id and (getattr(args,"review_model",None) is not None or getattr(args,"review_effort",None) is not None):
        raise LupusError("REVIEW_OPTIONS","--review-model/--review-effort requires --review")
    if review_id:
        primary=providers.resolve(args.driver,home)
        if review_id=="other":
            if primary.protocol!="native":raise LupusError("REVIEW_PROVIDER_REQUIRED","select --review PROVIDER explicitly for an API primary worker")
            review_id="codex" if primary.id=="claude" else "claude"
        reviewer=providers.resolve(review_id,home)
        review_model=getattr(args,"review_model",None)
        if review_model is not None:providers.validate_model(review_model)
        elif reviewer.protocol!="native":review_model=reviewer.default_model
        review_effort=getattr(args,"review_effort",None)
        if reviewer.protocol!="native" and review_model is None:raise LupusError("MODEL_REQUIRED","reviewer needs --review-model or configured default_model")
        if reviewer.protocol!="native" and review_effort is not None:raise LupusError("EFFORT_UNSUPPORTED","reviewer API effort has no validated mapping")
        adapters.validate_effort(reviewer.driver,review_effort)
        args.review_selection={"provider":reviewer.id,"driver":reviewer.driver,"model":review_model,"effort":review_effort}
    author_id = args.driver if args.cmd == 'cross-check' else getattr(args, 'cross_check', None)
    if author_id:
        author_spec = providers.resolve(author_id, home)
        author_model = args.model if args.cmd == 'cross-check' else args.cross_check_model
        if author_model is not None:providers.validate_model(author_model)
        if author_spec.protocol != 'native':
            if not author_spec.base_url:raise LupusError('PROVIDER_CONFIG_REQUIRED', 'configure the cross-check author endpoint first')
            if not author_model and not author_spec.default_model:raise LupusError('MODEL_REQUIRED', 'cross-check author needs --model or configured default_model')
            if author_spec.api_key_env and not os.environ.get(author_spec.api_key_env):raise LupusError('API_AUTH_MISSING', 'set the selected author API key environment variable')
    if getattr(args, 'driver', None) and args.cmd != "run":
        spec = providers.resolve(args.driver, home)
        if spec.protocol != 'native' and getattr(args, 'model', None) is not None:
            providers.validate_model(args.model)
        if spec.protocol != 'native' and args.cmd in ('agent','do','fix-tests','run','write'):
            if not getattr(args, 'model', None) and not spec.default_model:
                raise LupusError('MODEL_REQUIRED', 'select --model <id> or configure default_model')
            if spec.api_key_env and not os.environ.get(spec.api_key_env):
                raise LupusError('API_AUTH_MISSING', 'set the selected provider API key environment variable')
    if args.cmd == 'alpha-run':
        for selected in args.drivers.split(','):
            spec = providers.resolve(selected, home)
            if spec.protocol != 'native' and not args.model and not spec.default_model:
                raise LupusError('MODEL_REQUIRED', 'parallel API workers need an explicit model or configured default')
    if args.cmd == "session" and args.driver not in ("codex", "claude"):
        raise LupusError("SESSION_NATIVE_ONLY", "interactive sessions require codex or claude; use agent for API providers")
    if args.cmd == "ask":
        selected = {"home": home}
        if args.effort is not None: selected["effort"] = args.effort
        if getattr(args, "primary_instructions", None):selected["primary_instructions"] = args.primary_instructions
        if providers.resolve(args.driver,home).protocol!='native':selected['skill_access']=_api_skill_access(args,home)
        result = adaptive.answer(args.request, Path.cwd(), args.driver, args.model, args.timeout, **selected)
        if getattr(args,"skill_info",None):result["selected_skill"] = args.skill_info
        _out(result); return 1 if result["error"] or result["exit_code"] != 0 else 0
    if args.cmd == "auto":
        mode = "verified" if args.mode == "auto" and args.isolated else args.mode
        route = adaptive.plan(args.request, mode, args.check)
        if args.max_attempts is not None and route["route"] not in ("checked", "fix-tests"):
            raise LupusError("ROUTE_INVALID", "--max-attempts requires --check or an explicit failing-test repair request")
        if route["route"] == "answer" and (args.isolated or args.protect):
            raise LupusError("ROUTE_INVALID", "read-only answers do not use --isolated or --protect")
        print(f"Lupus: {route['route']} · {route['reason']}", file=sys.stderr)
        if route["route"] == "answer":
            selected = {"effort": args.effort} if args.effort is not None else {}
            selected["home"] = home
            if getattr(args, "primary_instructions", None):selected["primary_instructions"] = args.primary_instructions
            if providers.resolve(args.driver,home).protocol!='native':selected['skill_access']=_api_skill_access(args,home)
            result = adaptive.answer(args.request, Path.cwd(), args.driver, args.model, args.timeout, **selected)
            if getattr(args,"skill_info",None):result["selected_skill"] = args.skill_info
            auto_learning.finish(result)
            if sys.stdout.isatty() and not JSON_OUTPUT:
                print(result["result"])
                if result["error"]:
                    print(f"{result['error']}: {result['detail']}", file=sys.stderr)
            else:
                _out(result)
            return 1 if result["error"] or result["exit_code"] != 0 else 0
        if route["route"] in ("verified", "fix-tests"):
            forwarded = ["--home", str(home), *(["--json"] if JSON_OUTPUT else []), *(["do", args.request] if route["route"] == "verified" else ["fix-tests"]),
                         "--driver", args.driver, "--timeout", str(args.timeout)]
            if getattr(args,"skills",None) is not None:
                forwarded += ["--skills",args.skills]
            if getattr(args,"skill",None):
                forwarded += ["--skill",args.skill]
            if args.model:
                forwarded += ["--model", args.model]
            if args.effort is not None:
                forwarded += ["--effort", args.effort]
            if args.max_attempts is not None:
                forwarded += ["--max-attempts", str(args.max_attempts)]
            forwarded += supervision.flags(args.supervisor_role)
            forwarded += [flag for flag in ("--isolated", "--shell") if getattr(args, flag[2:])]
            if args.protect:
                raise LupusError("ROUTE_INVALID", "--protect requires --check")
            return main(forwarded)
    if args.cmd == "init":
        Kernel.init(home).close()
        _out({"home": str(home)})
        return 0
    k = Kernel.init(home) if args.cmd == "agent" else Kernel(home)
    try:
        if args.cmd == "agent":
            return _agent(k, args)
        if args.cmd == "provider-allow":
            spec=providers.resolve(args.provider,k.home)
            project=projects.get(k,args.project) if args.project else projects.resolve(k,os.getcwd())
            if project is None:raise LupusError('PROJECT_NOT_FOUND','register or select a project first')
            _confirm_user(f"제공자 허용: {project['name']} · {spec.name} · {spec.base_url or 'native CLI'} · key reference {spec.api_key_env or 'none'}")
            _out(projects.approve_provider(k,project['project_id'],spec.provider,'user'))
        elif args.cmd == "project-add":
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
        elif args.cmd == "release":
            _confirm_user(f"목표 {args.goal_id}에서 다음 보호 파일의 변경을 허용합니다(변경 내용은 diff를 따로 승인해야 반영됩니다): "
                          + ", ".join(args.paths))
            _out(release.grant(k, args.goal_id, args.paths, "user"))
        elif args.cmd == "release-approve":
            shown = release.proposal(k, args.goal_id)
            print(release.show(k, args.goal_id))
            _confirm_user("위 변경이 이 목표의 새 기준이 됩니다. 이후 모든 검사는 이 판본으로 다시 실행됩니다")
            if _ask("이 diff 그대로 승인하겠습니까?", ("yes", "no")) != "yes":
                _out({"approved": [], "note": f"승인하지 않았습니다. 거절하려면 lupus release-reject {args.goal_id} --note \"…\""})
                return 1
            _out(release.approve(k, args.goal_id, shown["digest"], "user"))
        elif args.cmd == "release-reject":
            _confirm_user(f"목표 {args.goal_id}의 보호 파일 변경 제안을 거절합니다(고정된 판본이 유지됩니다)")
            _out(release.reject(k, args.goal_id, "user", args.note))
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
            if providers.resolve(args.driver, k.home).provider not in project["approved_providers"]:
                raise LupusError("PROVIDER_NOT_APPROVED", args.driver)
            if not getattr(args, "fresh_project", None):
                _confirm_user(f"실패하는 테스트를 고칩니다: {project['canonical_root']} ({args.driver})")
            project = _isolate(k, project, args)
            caps = {**quick.DEFAULT_CAPS, "attempts": args.max_attempts} if args.max_attempts is not None else None
            made = quick.fix_tests(k, project, "user", caps=caps, check=args.check, protect_paths=args.protect,
                                   container=args.container, lean=args.lean)
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
            if args.max_attempts is not None:
                return 0 if report["done"] else 1
        elif args.cmd == "diff":
            print(gitx.diff(k, args.goal_id))
        elif args.cmd == "accept":
            goal = goals.get(k, args.goal_id)
            _confirm_user(f"검증된 결과를 내 저장소에 커밋 하나로 반영합니다(빨리 감기 병합만, 강제 없음): {goal['objective'][:80]}")
            _out(gitx.accept(k, args.goal_id, "user"))
        elif args.cmd == "undo":
            _out(gitx.undo(k, args.goal_id, "user"))
        elif args.cmd == "discard":
            _confirm_user(f"격리된 체크아웃과 브랜치를 버립니다(되돌릴 수 없음): {args.goal_id}")
            _out(gitx.discard(k, args.goal_id, "user"))
        elif args.cmd == "write":
            return _write(k, args)
        elif args.cmd == "session":
            return _session(k, args)
        elif args.cmd == "auto" and route["route"] == "text-edit":
            if args.protect:
                raise LupusError("ROUTE_INVALID", "--protect requires --check")
            project = _project_here(k, "정확한 문서 치환", args)
            _confirm_user(f"정확한 문서 치환: {args.request}")
            report = textedit.apply(k, project, args.request, "user")
            if args.effort is not None:
                report["requested_worker_effort"] = args.effort
            _started(args)
            _out(report)
            return 0 if report["done"] else 1
        elif args.cmd == "auto":  # supplied acceptance command: no drafting, rubric or extra model call
            project = _project_here(k, "지정 검사로 요청 구현", args)
            _confirm_user(f"요청: {args.request}\n완료 조건: {args.check} ({args.driver})")
            project = _isolate(k, project, args)
            caps = {**quick.DEFAULT_CAPS, "attempts": args.max_attempts} if args.max_attempts is not None else None
            if route.get("execution") == "literal-edit":
                report = textedit.apply(k, project, args.request, "user", args.check, args.protect, caps)
                if args.effort is not None:
                    report["requested_worker_effort"] = args.effort
                if args.max_attempts is not None:
                    report["attempt_limit"] = report["budget"]["attempts"]["cap"]
                _started(args)
                _out(report)
                return 0 if report["done"] else 1
            checked = quick.checked_request(k, project, args.request, args.check, "user", args.protect, caps=caps)
            _started(args)
            args.goal_id = checked["goal_id"]
            report = _run(k, args)
            report.update(route="checked", check=args.check)
            _after_isolated(k, report)
            _out(report)
            return 0 if report["done"] else 1
        elif args.cmd == "do":
            project = _project_here(k, "요청 구현", args)
            if args.cross_check and not projects.provider_allowed(project, providers.resolve(args.cross_check, k.home).driver):
                raise LupusError("PROVIDER_NOT_APPROVED", "approve the cross-check author provider for this project first")
            _confirm_user(f"요청: {args.request}\n먼저 이 요청을 판정할 테스트를 작성합니다 ({args.driver})")
            project = _isolate(k, project, args)
            draft = quick.draft_check(k, project, args.request, "user", stage=not args.two_step, allow_failing=args.allow_failing,
                                      lean=args.lean, cross_check=bool(args.cross_check))
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
                # First what the worker says the test asserts and what it decided on its own (bound to
                # this exact version of the test), then how it failed on the code as it is, then the test.
                described = contract.latest(k, draft["goal_id"], hashlib.sha256(content).hexdigest())
                red = goals.latest_evidence(k, draft["goal_id"]).get("c0") or {}
                print(f"\n요청: {draft['request']}\n\n{contract.render(described)}\n\n"
                      f"원래 코드에서 실패한 모습: {' '.join((red.get('detail') or '').split())[-400:] or '(기록 없음)'}")
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
                                            hashlib.sha256(content).hexdigest(), "user", keep_base=bool(args.review),
                                            contract_sha256=contract.digest(described))
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
            report["model_calls"] = len(first["steps"]) + sum(s.get("outcome") != "ALREADY_SATISFIED" for s in report["steps"]) + report.get("supervision_usage",{}).get("calls",0)
            if args.review and report["done"]:
                report = _reviewed(k, args, report, draft, build)
            elif args.review:
                report["review"] = "검사가 통과하지 않아 리뷰하지 않았습니다"
            if args.cross_check and report["done"]:
                report["crosscheck"] = crosscheck.shadow(k, build["goal_id"], providers.resolve(args.cross_check, k.home).driver, **({"model":args.cross_check_model} if args.cross_check_model is not None else {}))
                report["crosscheck_text"] = crosscheck.render(report["crosscheck"])
            if build.get("review_base"):      # the copy of the project kept for the reviewer
                shutil.rmtree(build["review_base"], ignore_errors=True)
            _after_isolated(k, report)
            report["next"] = release.hint(k, build["goal_id"])
            report["vault"] = vault.sync(k)
            _out(report)
        elif args.cmd == "run" and args.background:
            goals.get(k, args.goal_id)
            _out(jobs.start(k, ["run", args.goal_id, "--driver", args.driver, "--max-steps", str(args.max_steps),
                                "--timeout", str(args.timeout), *(["--model", args.model] if args.model else []),
                                *(["--effort", args.effort] if args.effort is not None else []),
                                *(["--skills",args.skills] if args.skills is not None else []),
                                *supervision.flags(args.supervisor_role),
                                *(["--cheap-first"] if args.cheap_first else []),
                                *(["--stop-stale-writer"] if args.stop_stale_writer else [])]))
        elif args.cmd == "alpha-status":
            _out(alpha.portfolio(k))
        elif args.cmd == "alpha-run":
            names = [n for n in args.drivers.split(",") if n]
            if not names:
                raise LupusError("DRIVER_UNKNOWN", args.drivers)
            if args.background:
                _out(jobs.start(k, ["alpha-run", "--drivers", ",".join(names), "--max-steps", str(args.max_steps),
                                    "--timeout", str(args.timeout), "--parallel", str(args.parallel),
                                    *(["--model", args.model] if args.model else []), *(["--effort", args.effort] if args.effort else []),
                                    *(["--skills",args.skills] if args.skills is not None else [])]))
            else:
                resolved = [providers.resolve(n, k.home).driver for n in names]
                def prepare_stale():
                    return {driver: why for n, driver in zip(names, resolved) if (why := _stale(k, n, driver))}
                def make(driver):
                    if args.model is None and args.effort is None and driver.startswith("native_"): return adapters.native(driver)
                    worker=adapters.native(driver, args.model, args.effort, home=k.home)
                    if isinstance(worker,adapters.APIAdapter):worker.skill_access=_api_skill_access(args,k.home)
                    return worker
                report = alpha.run(k, resolved, make, max_steps=args.max_steps,
                                   timeout_s=args.timeout, prepare_unavailable=prepare_stale, parallel=args.parallel)
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
            report["next"] = release.hint(k, args.goal_id)
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
            _confirm_user(f"{args.days:g}일보다 오래된 보관 파일·작업 로그·세션 임시 파일 삭제"
                          + (". 리뷰가 되돌리기를 끝내지 못해 남겨 둔 프로젝트 사본도 삭제" if args.kept else ""))
            _out(jobs.prune(k, args.days, kept=args.kept))
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
            if args.serve:
                from .viewer_server import serve
                serve(page.parent, args.port, args.open)
            elif args.open:
                import webbrowser
                try:
                    opened = webbrowser.open(page.as_uri())
                except Exception:
                    opened = False
                if sys.stdout.isatty() and not JSON_OUTPUT:
                    from .util import diagnostic_tail, language
                    label = ("브라우저에서 열었습니다: " if opened else "브라우저를 열지 못했습니다. 직접 열어주세요: ") if language() == "ko" else ("Opened in your browser: " if opened else "Browser unavailable; open this file: ")
                    print(label + diagnostic_tail(str(page)))
                    return 0
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
            if args.driver:
                spec = providers.resolve(args.driver, k.home)
                if spec.protocol != "native": raise LupusError("PROBE_NATIVE_ONLY", "API configuration is declared; use models --discover for an explicit endpoint request")
                _out(probe.run(k, live=args.live, drivers=(spec.driver,)))
            else:
                _out(probe.run(k, live=args.live))
        elif args.cmd == "cross-check":
            print(crosscheck.render(crosscheck.shadow(k, args.goal, providers.resolve(args.driver, k.home).driver, **({"model":args.model} if args.model is not None else {}))))
        return 0
    finally:
        k.close()


if __name__ == "__main__":
    sys.exit(main())
