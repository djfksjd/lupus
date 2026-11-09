---
name: minimal-code-discipline
description: Keep an implementation or bounded fix within the requested scope by reusing existing code and choosing the smallest correct change.
---

# Minimal code discipline

LUPUS adaptation of OMC's minimal-code-discipline, MIT, pinned at
`fd829431e9199f27714af65e5f79a82ff203d5f7`. This is a standalone workflow;
it does not require the OMC runtime.

Use the original request to decide what needs to exist. For a small question,
answer directly; do not create a plan, tests, goals or infrastructure merely
because this skill is selected. A broad build still needs the full requested
scope.

Before adding a helper, find the existing implementation and follow its callers.
Prefer the standard library, platform facilities and dependencies already used
by the project. A new dependency should solve a concrete need that those cannot
meet, within the user's authorized scope.

Fix the shared cause of a defect rather than adding separate workarounds to each
caller. Keep unrelated formatting, refactoring and speculative abstractions out
of the change. Prefer clear control flow over clever compression. Record a
deliberate limit only when a future maintainer needs it to understand the choice.

Keep validation at trust boundaries, data-loss protection, security controls,
accessibility and explicitly requested behavior. A smaller diff that drops one
of these is not a correct diff.

Use checks that can catch a plausible regression in the changed behavior. Reuse
valid results while their source, inputs and environment remain unchanged;
recheck when a later change invalidates them. Report what changed, the evidence
and any unresolved limitation. Preserve the selected LUPUS mode and its tool
permissions; this skill grants no extra execution authority.
