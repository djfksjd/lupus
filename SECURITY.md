# Security

Lupus runs AI coding CLIs as workers on your machine. Read this before using it on anything you care about.

## What Lupus does NOT protect against

- **There is no VM.** The headless Claude worker and every verifier run inside a macOS sandbox applied by Lupus; Codex runs its commands in its own sandbox under a profile Lupus sets; verifiers can run in a Docker container instead (`--container`). The shared temp directory, the worker's open network, and interactive sessions (`lupus session`, which are your own unsandboxed session) are outside that. Lupus is not a boundary against a process determined to misbehave; it guards against mistakes, shortcuts and text planted in project files.
- **Tests are executed by the code they test.** A verdict is read from the runner's output. That resists accidents and cheap tricks, not code that deliberately forges a whole summary from inside the test process.
- **A judge's verdict on a document is an opinion** with one mechanical check (its quotes exist). Completion of a document also needs your approval of the exact version.
- **Read confinement is measured, not assumed.** With its default sandbox a Codex worker could read any file your user can (canary test, codex-cli 0.160.0). Lupus therefore launches Codex with a permission profile that makes the home directory unreadable and gives commands no network; with it the canary in the home directory was not readable (one attempt). Claude Code 2.1.289 denied the same read. The temp directory stays readable. `lupus probe --live` repeats the test on your machine, and a driver whose confinement is not verified can only run on a project after you allow it: `lupus project-allow-unconfined <project_id>`.
- Whatever a worker reads can be sent to that CLI's provider. Do not point Lupus at projects that contain secrets or customer data.
- Text already sent to a provider cannot be recalled by deleting it locally.

## What it does enforce

| Risk | Measure |
|---|---|
| Credentials inherited from your shell | Workers **and verifiers** start with an allow-listed environment. Provider keys, cloud credentials and tokens are not passed. A criterion can name the variables its tests need (`env_pass`); only the user can add such a criterion. |
| Model-written code doing damage while it is being tested | Verifier commands run in the macOS sandbox: no outbound network (loopback only), writes only inside the project and temp directories, home directory unreadable except the project and language toolchains. Opt out per criterion with `sandbox: false` (user only). |
| A worker reading your files or planting code for later | The Claude worker runs in a macOS sandbox: of your home directory only the CLI's own files are readable, writes go only to the project, temp and the CLI's state, never to settings, hooks, commands, plugins or MCP definitions, and Lupus's own database is out of reach. Checked with the CLI's own permission system switched off. |
| The test runner itself being replaced | For jest/vitest the `node_modules` tree is fingerprinted and must not change; links leading out of it are refused. |
| A document passing by telling the judge to pass it | The judge runs without tools in an empty directory, must quote the document for every item (quotes are checked), a built-in item rejects documents that address the judge, and your approval of the exact bytes is required. |
| A background job outliving its record | A job starts only after its pid and start time are stored; `lupus stop` signals only that process. |
| A worker writing outside the project | Measured with a canary by `lupus probe --live` (`write_confinement`): neither Claude Code 2.1.289 nor codex-cli 0.160.0 could create a file in the home directory (one attempt each). |
| A file in the project shadowing `claude`, `codex` or `python3` | Relative and empty `PATH` entries are removed; the CLI is resolved to an absolute path outside the project. |
| A worker passing by changing the tests | Tests and runner configuration are frozen; changes are undone before verification; symlinked tests are refused. |
| A worker granting itself authority | Workers get a prompt and a directory, never the database or the CLI. Goals, approvals, budget raises, criteria that execute commands, and consent to unconfined reads require the user (and an interactive terminal on the CLI). |
| Planted instructions surviving in memory | Worker-written lessons that contain links, tool invocations or anything phrased as an instruction to run something are not stored (a conservative heuristic, not a guarantee). Recalled notes are shown as reference data, after the instructions. |
| Secrets written to disk by Lupus | Recovery objects, memory, vault pages, verifier output and frozen-file copies are scanned for common credential formats and refused or redacted. This is pattern matching, not a guarantee. |
| Script injection in the graph viewer | Node text is embedded as inert JSON and rendered as text only; a Content-Security-Policy allows nothing but the two local scripts. No network requests. |
| Path tricks | Verifier, protected and inlined paths must be project-relative; symlinks out of the project and out of the vault are refused. |
| SQL injection | Every query is parameterised. |

The measurements behind these claims and the rules themselves are in [`lupus/docs/CONTRACT.md`](./lupus/docs/CONTRACT.md); the tests are in [`lupus/tests/test_security.py`](./lupus/tests/test_security.py) and `test_protect.py`.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository (Security → Report a vulnerability) instead of a public issue. Include the Lupus commit, the CLI versions (`lupus probe` output) and steps to reproduce. This is an alpha project maintained on a best-effort basis; there is no SLA.
