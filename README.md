<div align="center">

<img src="./asset/logo/lupuslogo2.png" alt="LUPUS — AI works together" width="380" />

# LUPUS

**AI WORKS TOGETHER**

### A local supervisor that makes Claude Code and Codex CLI finish verifiable work —<br/>with evidence, budgets, crash recovery and Claude ↔ Codex handoff

[English](./README.md) · [한국어](./README.ko.md) · [简体中文](./README.zh-CN.md) · [日本語](./README.ja.md)

[![License](https://img.shields.io/badge/license-Apache--2.0-1f2937?style=flat-square)](./LICENSE)
![Stage](https://img.shields.io/badge/stage-v0.2%20alpha-d69526?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square)
![Dependencies](https://img.shields.io/badge/runtime%20deps-0-2ea043?style=flat-square)
![Tests](https://img.shields.io/badge/offline%20tests-318%20passing-2ea043?style=flat-square)

</div>

Lupus runs the `claude` and `codex` CLIs you already have (with your existing subscription login) as workers, and decides "done" itself: a goal is complete only when checks the worker cannot influence pass, never because a model said so.

**This is `v0.2 alpha`.** A single-user tool for macOS. Workers and verifiers run in OS sandboxes, not in a VM. The measurements below are small. Do not use it on sensitive material.

## What it does

| | |
|---|---|
| **Evidence-only completion** | DONE requires passing checks bound to the current acceptance criteria. A worker's "I fixed it" is not evidence. |
| **Frozen tests** | Test files and runner config are frozen before a worker starts. Edited, deleted or skipped tests are put back before verification. |
| **Python, Node, Go, Rust — or any command** | unittest, pytest, node:test, jest, vitest, `go test`, `cargo test` are recognised from the project's files. Anything else: `--check "<your test command>"`. |
| **One line for any request** | `lupus fix-tests` turns the red tests it observes into the goal. `lupus do "<request>"` drafts a failing test (and, kept aside, a proposed implementation) in one call; you approve the test, then the proposal is applied and verified. |
| **Documents, plans, research** | `lupus write`: a rubric you approve first, a judge that is a different AI and must quote the document for every item it accepts, then your sign-off on the exact version. |
| **Your normal interactive session** | `lupus session` starts your usual `claude` / `codex` screen with your own configuration, freezes the tests, and verifies by itself when you exit. |
| **Claude ↔ Codex handoff** | When one AI stops (quota, crash, your choice), the other continues from a validated checkpoint. Finished steps are not redone; budgets and attempt counts are not reset. |
| **Several projects, in the background** | `lupus alpha-run` advances every open goal in turn under shared budgets and switches AI when one runs out of quota. `--background` keeps it running after the terminal closes. |
| **Budgets and loop control** | Calls, attempts, time and tokens are reserved before work starts. Repeating the same failed attempt is refused before any model is called. |
| **Crash-safe checkpoints** | Recovery objects are written durably before the database commit; tested by killing the process at every boundary. |
| **Your working tree stays yours** | `--isolated` does the work in a separate checkout of your committed HEAD. `lupus diff` shows the result, `lupus accept` brings it over as one commit (fast-forward only, re-checked as that exact commit), `lupus discard` drops it. |
| **OS-level confinement** | The Claude worker and every verifier run inside a macOS sandbox applied by Lupus; verifiers can run in a Docker container instead. |
| **Memory that has to earn its place** | Project knowledge as typed, linked nodes. `lupus learn` proposes procedures from recorded failures; they stay candidates until later verified outcomes promote or retire them. |

## Measured on one Mac (2026-10-06)

Same tasks, same checks, fresh directory per run, 3 runs per cell, every run passed. Claude Code 2.1.290, codex-cli 0.160.0. Tokens = new input + cached input + output, summed over the 3 tasks; mean of 3 runs.

| 3 one-shot coding tasks | Your CLI as configured | Same CLI, plugins/MCP off | Lupus |
|---|---|---|---|
| Claude | 400,640 tokens · 53.9 s | 55,290 · 33.5 s | **37,216 · 18.0 s** |
| Codex | 244,458 tokens · 58.7 s | 203,780 · 50.6 s | **107,931 · 28.5 s** |

| Other situations | Without Lupus | Lupus |
|---|---|---|
| 4-step project, Claude (1–2 runs, 2026-10-05) | 298,327 tokens · 52.0 s | 70,768 · 42.5 s |
| Same project interrupted, other AI takes over (2026-10-05) | 337,133 tokens · 82.0 s · 1,139 chars of re-explanation | 138,989 · 64.1 s · none |
| 4-step project, Codex, one call per step vs batched | 185,278 tokens · 89.0 s | 122,595 · 53.1 s |
| Feature request via `lupus do` vs one plain lean call, Claude (3 runs, 2026-10-07) | 17,493 tokens · 10.1 s | **15,629 · 15.1 s** |
| Same, Codex (3 runs, 2026-10-07) | 72,083 tokens · 24.4 s | **37,502 · 24.8 s** |

**On a real project.** Three changes the maintainers of [hukkin/tomli](https://github.com/hukkin/tomli) really made (a bug fix, a TOML 1.1 feature, a hardening change): source as it was before the commit, tests as they were after it, nothing else given. `lupus fix-tests` finished all three on the first attempt with both Claude (36k–64k tokens, 10–21 s) and Codex (82k–119k tokens, 16–21 s), judged by the upstream tests, which no run tried to edit.

**Does it get more requests right? It now matches a plain call; it does not beat one.** 20 real upstream commits from five projects (sqlparse, more-itertools, tomli, packaging, click): the worker got the repository before the commit and only the commit message; the upstream tests were kept hidden and used as the score. The check `lupus do` drafts was approved automatically, which is not how it is meant to be used.

| Hidden upstream tests passed (2026-10-07, same run, same day) | Plain call | `lupus do` |
|---|---|---|
| Codex, 20 instances | 13 | 13 |
| Claude, first 12 instances | 10 | 11 |
| Earlier the same day, before the fix below: Codex 20 / Claude 7 | 13 / 7 | 11 / 4 |

The earlier loss had a measured cause. `lupus do` asked the worker to write its proposed implementation as complete files in a side folder; on large files that ran into the time limit (two more-itertools instances: 850 s, failed). The worker now edits the sources in place, Lupus takes those edits out again before it judges and shows you the test, and puts them back after you approve. The same two instances now take 54 s and 100 s and pass. `lupus do` still costs more time than a plain call (per instance, before the optional review: Codex 77 s vs 55 s, Claude 85 s vs 46 s).

When Lupus reported DONE, the hidden tests still failed in 7 of 18 cases on Codex and 1 of 10 on Claude (in 7 of those 8 a plain call failed too). **DONE means the check you approved passed. It does not mean the request was understood.** In the other direction, Lupus said "not done" in 4 cases where the hidden tests passed: each time the request changed behaviour that the project's existing tests pin down, and those tests are frozen.

**An independent review did not help here.** `lupus do --review` has the other AI compare the request with the change after the checks pass (it must quote the request for every objection; objections go back to the worker once; a revision that breaks the approved checks is undone). Over 28 reviewed results it objected twice and changed the hidden-test outcome in none. Of the 8 results that were wrong it objected to one, and the revision that followed did not make it right. It is off by default and unproven.

**Judging documents.** A document missing a rubric item and a document that tells the judge to pass it were both rejected by both judges; the complete one was accepted (6 of 6 as expected, one run each).

Read these honestly:

- **Most of the saving against "your CLI as configured" comes from not loading plugins, MCP servers and skill descriptions**, which you can also get without Lupus (middle column). What Lupus adds on top is the prompt technique and the verification.
- **`lupus do` now uses one model call instead of two.** The test and a proposed implementation come from the same call; the proposal is kept aside, the test is checked and shown to you against the code as it is, and only after your approval is the proposal applied and verified. That brought it from 29,104 to 15,629 tokens on Claude and from 74,245 to 37,502 on Codex, below a plain call in tokens. It is still slower than a plain call on Claude (15.1 s vs 10.1 s) and, because more of its tokens are output, its estimated list price there is about 1.6× (subscription use is not billed per token). All holdout tests passed in every arm; `--two-step` restores the old behaviour.
- `--cheap-first` did not save tokens on Claude in this measurement (107,738 vs 37,216).
- Three runs per cell, one machine, small tasks. Timings in the first table were taken while other CLI calls were running.
- On three harder tasks with hidden holdout tests (2026-10-05), all 36 runs passed with and without Lupus, so that benchmark could not show that frozen tests reduce false completion.

Raw data and scripts: [`lupus/docs/`](./lupus/docs) · [`lupus/evaluations/`](./lupus/evaluations) · full record in [docs/design/LUPUS-IMPLEMENTATION-REVIEW.md](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md).

## When to use it, and when not

**Use it** for failing tests, feature work that deserves a check you have read, multi-step or long jobs that may be interrupted or moved between Claude and Codex, documents that someone other than the writer should judge, and interactive sessions in which you want the tests to stay untouched.

**Use the plain CLI** for one-off questions and quick exploration: there Lupus only adds steps.

**It does not replace** an editor-integrated assistant (Cline, Cursor), a tool with its own agent loop and wide model choice (Aider, OpenHands), or a multi-agent workspace (Claude Squad, claude-flow). It is a supervisor for bounded Claude Code / Codex tasks on macOS: reproducible checks, reviewable changes, interruption recovery.

## Quick start

Requirements: macOS, Python **3.12+** whose SQLite is **3.51.3+ with FTS5** (Homebrew Python works), and `claude` and/or `codex` installed and logged in.

```bash
git clone https://github.com/djfksjd/lupus.git
cd lupus/lupus
python3 -m pip install -e .          # or: export PYTHONPATH=src and use `python3 -m lupus`

lupus init                           # creates ~/.lupus (database + vault)
lupus probe --live                   # measures what your installed CLIs support (2 tiny calls)

cd ~/work/my-project
lupus fix-tests --driver claude      # observe red -> freeze tests -> fix -> verify
lupus do "add a --json flag to the export command" --driver claude
                                     # one call: failing test + proposal kept aside -> you approve the test -> applied and verified
lupus do "…" --driver claude --review     # after the checks pass, the other AI compares the change with the request (opt-in; see the pilot)
lupus do "…" --driver claude --isolated   # same, in a separate checkout; then: lupus diff | accept | discard <goal>
lupus session --driver claude        # your usual interactive Claude Code, tests frozen, verified on exit
lupus write "migration plan for the billing tables" --out docs/plan.md --driver claude
                                     # rubric you approve -> written -> judged by the other AI -> your sign-off
```

Several goals, unattended:

```bash
lupus alpha-budget --calls 300 --attempts 40 --minutes 600     # one cap for everything
lupus alpha-run --drivers claude,codex --background            # all open goals in turn; switches AI on quota
lupus jobs        # what is running          lupus logs <job>        lupus stop <job>
lupus alpha-status                                             # every project at a glance
lupus learn --driver claude                                    # candidate procedures from recorded failures
```

A goal with your own checks, other languages, containers, the knowledge graph and every command: [`lupus/README.md`](./lupus/README.md).

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
- Nothing global is modified: no PATH changes, no edits to `~/.claude` or `~/.codex`. The hooks of `lupus session` are passed to that one process.

<div align="center">
<img src="./asset/screenshots/graph.png" alt="Lupus knowledge graph viewer" width="760" />
<br/><sub>Knowledge graph view (<code>lupus graph</code>): nodes, typed links, where knowledge was recorded and recalled</sub>
</div>

## Limits you should know

- **It does not make the model smarter.** On hidden tests it did as well as a plain call, not better, and an independent review by the other AI changed nothing (pilot above). What it gives you is a result that was checked the way you agreed to, in a place that is not your working tree, and that survives interruption.
- **Not a VM.** The Claude worker and verifiers run in a macOS sandbox (home directory unreadable except what the CLI itself needs, no writes outside the project and temp, Lupus's own state out of reach); Codex uses its own sandbox with a profile Lupus sets; verifiers can use Docker. The temp directory is shared, the worker's network is open, and an interactive session is not sandboxed at all. Do not give it protected or customer data.
- **You start it.** `lupus session` wraps your interactive CLI; nothing activates Lupus when you type `claude` yourself. Token usage of an interactive session is not reported by the CLIs, so it is charged at its full reservation.
- **A judge is an opinion.** The quote check stops unsupported passes, not wrong facts. That is why your sign-off is the last condition. Images and visual design cannot be judged.
- **Tests are run by the code they test.** Output parsing resists accidents and cheap tricks; code that sets out to forge a runner's whole summary from inside the test process is not something Lupus can detect. Rust unit tests inside source files cannot be frozen (their names are pinned, their bodies are not).
- **One worker at a time.** `alpha-run` takes goals in turn; it does not run projects in parallel.
- Learning proposes candidates and lets later verified outcomes decide; there is no fixed evaluation set, and Prime itself is not connected.
- jest and vitest were checked with real installs; Go and Rust with toolchains installed temporarily for the check. Linux and Windows have no OS sandbox support here.
- A wait always has a way out: `lupus status <goal>` says why, and `resolve`, `refreeze`, `approve`, `revise`, `revalidate`, `budget-raise` continue from there. `lupus prune` clears old leftovers.
- Young code. Seventeen external review rounds found 118 defects, 117 of them fixed, and a usability audit another 16; assume more remain.

## Documentation

- [Machine contract](./lupus/docs/CONTRACT.md) — the rules the code enforces, and what is out of scope
- [Implementation record](./docs/design/LUPUS-IMPLEMENTATION-REVIEW.md) — decisions, review rounds, every measurement and its limits (Korean)
- [Design](./docs/design/LUPUS-PLAN.md) — the full design the implementation is a slice of (Korean)
- [Third-party notices](./lupus/THIRD_PARTY_NOTICES.md) — vis-network is bundled unmodified for the graph view

## License

[Apache-2.0](./LICENSE). The bundled vis-network is used under MIT; see the notices.
