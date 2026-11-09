---
name: writing-plans
description: Turn agreed requirements for a substantial multi-step change into concrete, reviewable implementation tasks and validation.
---

# Writing implementation plans

LUPUS adaptation of Superpowers writing-plans, MIT, pinned at
`22f0b463e7a5e579904dd6211a520321035526f7`. No orchestration plugin or additional
skill is required.

Use this for work whose dependencies or design choices warrant a plan. A simple
question or one-step fix needs a direct answer or action, not a plan document.
Preserve the user's product, scope, execution method, existing branch and file
location preferences.

State the intended result and relevant constraints. Inspect the affected code
and identify the concrete files, interfaces and existing patterns a worker needs.
Split tasks where each produces an independently reviewable behavior; include
setup and documentation with the behavior that requires them. Avoid restructuring
the repository merely to make the plan symmetrical.

For each task, specify the behavior, dependencies, exact known file locations,
important interface choices and a check that would detect a plausible defect.
Use exact signatures or fixture values when the requirements determine them;
leave routine implementation choices open. Flag material unknowns and expected
failure conditions rather than filling the plan with generic placeholders.

Check that every requirement has an owner, later tasks use consistent interfaces,
and likely input or lifecycle failures have suitable validation. Keep the plan
short enough to reveal the decisions rather than transcribing the future code.

Present the plan in the conversation in ask mode. Save a file only when the task
and selected mode permit it, using the requested location or repository practice.
When implementation is already authorized, continue using the agreed execution
method; request input only for material missing decisions. Do not automatically
create worktrees, start subagents, commit, publish or install a runtime. A plan
does not certify completion; implementation evidence must support that claim.
