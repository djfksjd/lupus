<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### A local supervisor that makes Claude Code and Codex CLI finish verifiable work —<br/>with evidence, budgets, crash recovery and Claude ↔ Codex handoff

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.1%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-224%20passing-2ea043?style=flat-square)

</div>

Lupus runs the `claude` and `codex` CLIs you already have (with your existing subscription login) as workers, and decides "done" itself: a goal is complete only when deterministic checks pass, never because a model says so. It keeps goals, budgets, attempts, checkpoints and project knowledge in a local SQLite database, so work survives crashes, quota limits and switching from one AI to the other.

**This is `v0.1 alpha`.** It is a single-user tool for macOS, it has no sandbox (workers run with your user's permissions), and the measurements below are small. Do not use it on sensitive material.

## What it does

| | |
|---|---|
| **Evidence-only completion** | DONE requires passing checks bound to the current acceptance criteria. A worker's "I fixed it" is not evidence. |
| **Frozen tests** | Test files and runner config are frozen before a worker starts. Edited, deleted or skipped tests are put back before verification. |
| **Claude ↔ Codex handoff** | When one AI stops (quota, crash, your choice), the other continues from a validated checkpoint. Finished steps are not redone; budgets and attempt counts are not reset. |
| **Budgets and loop control** | Calls, attempts, time and tokens are reserved before work starts. Repeating the same failed attempt is refused before any model is called. |
| **Crash-safe checkpoints** | Recovery objects are written durably before the database commit; tested by killing the process at every boundary. |
| **Lean launches** | Workers start without the plugins, hooks, MCP servers and skill descriptions a task does not need; task files are placed in the prompt. |
| **Memory graph** | Project knowledge as typed, linked nodes with provenance. Each recall is tied to an attempt and scored by that attempt's verified outcome. |
| **One command for failing tests** | `lupus fix-tests` needs no goal file: the supervisor observes the red tests itself and that failure becomes the goal. |

## Measured on one Mac (2026-10-05)

Same tasks, same checks, fresh directory per run. Claude Code 2.1.289, codex-cli 0.160.0. Every run passed its checks. Tokens = new input + cached input + output.

| Situation | Your CLI as configured | Lupus |
|---|---|---|
| 3 one-shot coding tasks, Claude | 135,729 tokens · 15.0 s | 12,166 tokens · 6.2 s |
| 3 one-shot coding tasks, Codex | 88,530 tokens · 26.2 s | 36,298 tokens · 12.2 s |
| 4-step project, Claude | 298,327 tokens · 52.0 s | 70,768 tokens · 42.5 s |
| Same project, interrupted, other AI takes over | 337,133 tokens · 82.0 s · 1,139 chars of re-explanation | 138,989 tokens · 64.1 s · none |
| Fix failing tests, Claude | 136,712 tokens · 16.2 s | 12,817 tokens · 5.7 s · one command |

Read these honestly:

- **Most of the saving comes from not loading unneeded configuration and from the prompt technique** (files in the prompt, single pass). A plain CLI call using the same technique without Lupus used the same tokens on one-shot tasks. What the supervisor adds there is verified completion, not cheaper tokens.
- Defining a goal with checks costs about 1.9× the typing of a plain prompt (except `fix-tests`).
- In the interrupted scenario the Codex leg used 33% **more** than the plain CLI, because Lupus calls once per step and Codex has a large fixed input per call.
- 1–2 runs per cell. On three harder tasks with hidden holdout tests, all 36 runs passed and there were no false passes, so the benchmark could not show that frozen tests reduce false completion.

Raw data and scripts: [`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · full record in [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md).

## When to use it, and when not

**Use it** for work that has (or can have) a machine check: failing tests, multi-step tasks with tests, long jobs that may be interrupted or moved between Claude and Codex, projects with conventions worth recording once.

**Use the plain CLI** for one-off questions, exploratory back-and-forth, anything that needs your plugins/MCP servers, and work that cannot be checked by a program (planning, research, prose, design).

## Quick start

Requirements: macOS, Python **3.12+** whose SQLite is **3.51.3+ with FTS5** (Homebrew Python works), and `claude` and/or `codex` installed and logged in.

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # or: export PYTHONPATH=src and use `python3 -m lupus`

lupus init                           # creates ~/.lupus (database + vault)
lupus probe --live                   # measures what your installed CLIs support (2 tiny calls)

cd ~/work/my-project                 # a project with failing tests
lupus fix-tests --driver claude      # observe red -> freeze tests -> fix -> verify
lupus do "add a --json flag to the export command" --driver claude
                                     # any request: drafts a failing test, you approve it, then implements
```

A goal with your own checks:

```bash
lupus project-add ~/work/site --name site --providers anthropic,openai
lupus goal-submit <project_id> goal.json
lupus run <goal_id> --driver claude
lupus status <goal_id>               # state, budget, can it resume, and why not
lupus run <goal_id> --driver codex   # continue with the other AI (validated handoff)
lupus graph --open                   # knowledge graph view
```

`goal.json` format and all commands: [`lupus/README.md`](./lupus/README.md).

## How it works

```text
 you ──► goal + acceptance checks ──► ┌──────────────── Lupus supervisor (plain program) ───────────────┐
                                      │ budget · attempts · leases · checkpoints · frozen tests · memory │
                                      └───────┬───────────────────────────────────────────────┬─────────┘
                                   lean prompt│                                               │verify (deterministic)
                                              ▼                                               ▼
                                   claude  ◄──handoff──►  codex                    PASS evidence ──► DONE
```

- The supervisor is ordinary code. No model call decides routing, budgets, retries or completion.
- Workers get a prompt and a directory. They never get the database, the CLI or any authority.
- Nothing global is modified: no hooks, no PATH changes, no edits to `~/.claude` or `~/.codex`.

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus knowledge graph viewer" width="760" />
<br/><sub>Knowledge graph view (<code>lupus graph</code>): nodes, typed links, where knowledge was recorded and recalled</sub>
</div>

## Limits you should know

- **No VM or container.** Verifiers run in the macOS sandbox and workers are confined by each CLI's own controls, but the CLIs run as you. Protected or customer data must not be given to it. Reads are confined as far as each CLI allows and this is measured with a canary file: Codex is launched with a permission profile that hides your home directory and gives commands no network. A driver whose confinement is not verified needs explicit per-project consent. See [SECURITY.md](./SECURITY.md).
- Not connected to your normal `claude` / `codex` sessions; you run `lupus` explicitly. No interactive mode.
- Only Python `unittest`/`pytest` projects are supported by `fix-tests`.
- Young code. Eight external review rounds found and fixed 60 defects; assume more remain.
- Subscription quota actually consumed cannot be observed; token counts are what the CLIs report.

## Documentation

- [Machine contract](./lupus/docs/CONTRACT.md) — the rules the code enforces, and what is out of scope
- [Implementation record](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — decisions, review rounds, every measurement and its limits (Korean)
- [Design](./docs/design/LUPUS-PLAN.md) — the full design the implementation is a slice of (Korean)
- [Third-party notices](./lupus/THIRD_PARTY_NOTICES.md) — vis-network is bundled unmodified for the graph view

## License

[Apache-2.0](./LICENSE). The bundled vis-network is used under MIT; see the notices.
