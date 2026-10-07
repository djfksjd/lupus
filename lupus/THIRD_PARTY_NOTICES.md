# Third-party notices

Lupus itself has no runtime dependencies. Two third-party files are distributed with it,
unmodified: a library for the graph viewer (`lupus graph`) and a guidance text (`--lean`).
Three places in Lupus's own code are adaptations of other projects' code; they are listed at
the end.

## vis-network 10.1.2

- Upstream: https://github.com/visjs/vis-network (commit f2584c8d1264d8591e6c4d8f09e3a81d599ce1b8)
- File: `src/lupus/viewer/vendor/vis-network.min.js`, copied byte-for-byte from
  `standalone/umd/vis-network.min.js` in the npm package `vis-network@10.1.2`
- Licence: dual-licensed Apache-2.0 OR MIT. Lupus uses it under the MIT licence. Both licence
  texts are kept next to the file (`LICENSE-MIT`, `LICENSE-APACHE-2.0`); the copyright header
  inside the file is intact.
- Copyright (c) 2011-2017 Almende B.V., (c) 2017-2019 visjs contributors
- The standalone bundle includes its own bundled dependencies (vis-data, vis-util, hammer.js,
  keycharm, component-emitter, uuid and core-js polyfills), each under MIT-compatible terms as
  stated by their authors. Lupus did not audit those bundled sources.

Pinned source, tarball integrity (sha512, checked against the npm registry on 2026-10-05) and
per-file sha256 are recorded in `src/lupus/viewer/vendor/upstream-lock.json`; a test fails if
the vendored file no longer matches the lock.

Updating: download the new package tarball, verify its `dist.integrity` against the registry,
replace the three files, update the lock, re-run the tests and re-render the viewer once.
Do not edit the vendored file; changes belong in `viewer.js`.

## Ponytail (guidance text, bundled unmodified)

- Upstream: https://github.com/DietrichGebert/ponytail (commit c982cd411abb53323c4baa1baa3c2f020b8d0b08)
- Files: `src/lupus/vendor/ponytail/SKILL.md`, copied byte-for-byte from `skills/ponytail/SKILL.md`,
  and `src/lupus/vendor/ponytail/LICENSE`
- Licence: MIT. Copyright (c) 2026 DietrichGebert
- Pinned commit and per-file sha256: `src/lupus/vendor/upstream-lock.json`; a test fails if a
  bundled file no longer matches the lock.
- Do not edit the bundled file. What a worker is shown is selected from it by
  `src/lupus/economy.py` (which sections, which lines are left out, and why).

## Adapted code

These are not copies. The source was read at the commit given, and the part named was
rewritten in Python for Lupus's supervisor; the docstring at each place says what was taken
and what was changed.

| Lupus | Adapted from | Licence |
|---|---|---|
| `economy.py`: `_filter_for_mode` | Ponytail @ c982cd4, `hooks/ponytail-instructions.js` (`filterSkillBodyForMode`) | MIT, (c) 2026 DietrichGebert |
| `alpha.py`: `_run_parallel` | Ruflo @ f1c52f7, `v3/@claude-flow/codex/src/dual-mode/orchestrator.ts` (bounded workers, one working tree per concurrent writer) | MIT, (c) 2024-2026 ruvnet |
| `gitx.py`: `_onto` | Ruflo @ f1c52f7, `v3/@claude-flow/codex/src/worktrees/coordinator.ts` (`integrate`) | MIT, (c) 2024-2026 ruvnet |
| `supervisor.py`: `_remember_failure`, `_same_failure` | Prime Agent @ bc57309, `crates/pa-core/src/autonomous/gates.rs` (`run_autonomous_quality_gates`) | MIT, (c) 2025-2026 Prime Intellect Ltd. |
| `learn.py`: `undo` | Prime Agent @ bc57309, `crates/pa-core/src/refinement/planner.rs` (`apply_refinement_proposal`, `rollback_proposal`) | MIT, (c) 2025-2026 Prime Intellect Ltd. |

The MIT licence text (identical for the three, apart from the copyright line above):

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software
> and associated documentation files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
> Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
> BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
