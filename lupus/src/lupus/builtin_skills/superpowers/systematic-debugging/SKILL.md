---
name: systematic-debugging
description: Trace a reproducible bug, failing check or unexpected behavior to its cause before making a focused fix.
---

# Systematic debugging

LUPUS adaptation of Superpowers systematic-debugging and its tracing, validation
and waiting references, MIT, pinned at
`22f0b463e7a5e579904dd6211a520321035526f7`. The relevant techniques are included
below; no plugin, script or other skill is required.

Start with the user's failing action and expected result. Read the complete
error and reproduce the smallest relevant case when feasible. Inspect recent
changes and compare a working path before proposing a fix. If the failure is
intermittent, collect the missing observation rather than assuming a cause.

Trace a wrong value backward from its use through callers to the original input
or state transition. Across process, UI, configuration or storage boundaries,
record enough input, output and timing to locate the first divergence. Do not
record secrets. Instrument only the boundary needed to distinguish hypotheses.

State one concrete hypothesis and test the smallest change that distinguishes
it. Avoid stacking unrelated guesses. If repeated fixes reveal new coupling,
reassess the architecture and explain the evidence before expanding the change.
Continue independent work when it does not depend on missing information.

Implement the confirmed cause. Add a meaningful regression for a behavior defect
when appropriate; preserve existing acceptance criteria. Validate a dangerous
input at the trust boundary and at independently reachable sensitive operations,
without adding duplicate checks everywhere by ritual. For asynchronous behavior,
wait for the observable state with a bounded timeout and useful diagnostics;
use a fixed delay only when the delay itself is under test.

Verify the original action and related affected paths. Separate observed causes
from hypotheses and external limitations. Existing valid evidence may be reused
until source, inputs or environment change. Investigation can run in ask mode;
editing follows the user's selected agent or verify mode and permissions. Do
not create a goal, spawn workers or call a provider just to satisfy a process.
