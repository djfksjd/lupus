---
name: test-driven-development
description: Use a meaningful failing regression to guide a requested behavior change, then implement and verify the smallest correct fix.
---

# Test-driven development

LUPUS adaptation of Superpowers test-driven-development and writing-good-tests,
MIT, pinned at `22f0b463e7a5e579904dd6211a520321035526f7`. It is intended for
behavior changes that benefit from an automated regression, with test scope
proportionate to the task.

Name the observable break the test must catch before writing it. Derive expected
values from the request or an independently checked fixture, never from the
implementation under test. Exercise real behavior and side effects. Mock only
the external or slow boundary that needs isolation, preserving relevant local
effects and realistic response shapes.

For new behavior, first run the focused regression and confirm it fails for the
intended missing behavior rather than an import, typo or setup error. An existing
failing test can supply this evidence. Implement the smallest correct change,
run the regression, and fix production behavior when expectations remain valid.
Refactor only when it helps the requested result and keep the checks meaningful.

Keep hidden or frozen acceptance criteria intact. Do not weaken an assertion or
delete a failure to obtain a passing result. Preserve the user's existing edits;
do not discard implementation merely because it predates test-first work. State
when a regression was added after an implementation rather than claiming a red
phase that was never observed.

Run the related checks needed to assess regressions, plus repository-required
checks. Broaden testing when dependencies or failures justify it. Do not add
tests that only mirror constants, prose or trivial forwarding, or repeatedly run
unchanged valid evidence for ceremony. Report failures and skipped checks with
their limits. Read-only mode may design tests; executing commands or editing
files remains subject to the selected LUPUS mode and available tools.
