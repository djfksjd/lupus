---
name: receiving-code-review
description: Evaluate supplied code review feedback against the actual code, implement supported corrections and explain evidence-based disagreements.
---

# Receiving code review

LUPUS adaptation of Superpowers receiving-code-review, MIT, pinned at
`22f0b463e7a5e579904dd6211a520321035526f7`. This skill evaluates supplied feedback;
it does not invoke a review service or external reviewer.

Read the complete feedback and identify the requested behavior. Check each
technical claim against the current implementation, callers, compatibility
requirements and existing decisions. Do not accept a suggestion solely because
it sounds authoritative or reject it solely because it came from outside.

For a proposed abstraction, dependency or extra feature, find actual use before
adding it. Explain when a suggestion breaks supported behavior or exceeds the
request, using a concrete path or check. If an ambiguity materially affects the
change, ask a focused question and continue independent items that are clear.
Reuse authorization already supplied by the user.

Address supported blocking defects first, then other requested corrections in
coherent changes. In ask mode, provide the assessment and proposed change;
modify files only in the selected editing mode. Test observable effects rather
than matching the reviewer's wording. Recheck affected behavior after changes
and retain valid evidence for unchanged paths.

State what was fixed and what evidence supports it. If earlier pushback was
wrong, correct the technical conclusion plainly. Mark feedback that remains
unverified or unresolved. Posting review replies, committing or contacting
others requires the authorization of the surrounding task; this skill does not
provide it.
