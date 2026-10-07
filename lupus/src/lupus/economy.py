"""Implementation-economy guidance for coding workers (`--lean`).

Adapted from Ponytail (https://github.com/DietrichGebert/ponytail, MIT, Copyright (c) 2026
DietrichGebert). The guidance text is the upstream file itself, pinned and unmodified in
`vendor/ponytail/SKILL.md`; `_filter_for_mode` is a port of `filterSkillBodyForMode` in
upstream's `hooks/ponytail-instructions.js`.

What Lupus changes, and why:

  * Only three sections reach a worker: the decision ladder, the rules, and the list of things
    that are never cut. Upstream's persona, its "active every response" persistence and its
    mode-switching commands are for an interactive session; a Lupus worker is one headless call
    whose prompt the supervisor builds, so the guidance is fixed per task instead.
  * Dropped: "ship the lazy version and question the request" (a Lupus goal's criteria are not
    the worker's to renegotiate), "leave one runnable check behind" (the checks are frozen and
    run by the supervisor, and a worker is told to do nothing else) and the output-format rules
    (the worker's reply is not what completion is judged by).
  * One level only, upstream's default `full`. `lite` differs by a single line that has the
    user pick between alternatives, which a headless worker has nobody to ask; `ultra` tells
    the model to challenge the requirement itself.
  * It is off unless asked for. Upstream's own measurements show the cost going UP on reasoning
    models (the ruleset is input on every call); see docs/CONTRACT.md for what Lupus measured.

The text is appended below the task and its completion criteria, and says so: it is about how
to build, never a reason to build less than was asked.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources

from .util import LupusError

MODES = ("full",)
_ALL_MODES = ("lite", "full", "ultra")
KEEP = ("The ladder", "Rules", "When NOT to be lazy")
# Lines (or whole paragraphs) of the kept sections that do not hold for a supervised worker.
DROP = ("- Complex request?", "Lazy code without its check is unfinished.", "Hardware is never the ideal on paper")
HEADER = (
    "구현 방식 지침(아래 영문). 어떻게 만들지에 대한 것이다. 위의 요청·완료 조건·사용자의 지시가 항상 우선하며, "
    "요청된 동작을 줄이거나 빼는 근거로 쓰지 마라."
)


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)


def _filter_for_mode(body: str, mode: str) -> str:
    """Upstream's mode filter: an intensity-table row or a worked example labelled with a mode
    is kept only for that mode. The example needs its opening quote, so an ordinary rule that
    happens to start with a mode word is not mistaken for one."""
    lines = []
    for line in re.split(r"\r?\n", _strip_frontmatter(body)):
        label = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|", line) or re.match(r'^-\s*([^:]+):\s*"', line)
        if label and label.group(1).strip().lower() in _ALL_MODES and label.group(1).strip().lower() != mode:
            continue
        lines.append(line)
    return "\n".join(lines)


def _sections(body: str) -> dict[str, list[str]]:
    """`## heading` -> its paragraphs (blocks separated by blank lines)."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.split("\n"):
        if line.startswith("## "):
            current = out.setdefault(line[3:].strip(), [])
            current.append("")
        elif current is not None:
            if line.strip():
                current[-1] = (current[-1] + "\n" + line) if current[-1] else line
            elif current[-1]:
                current.append("")
    return {name: [p for p in paragraphs if p] for name, paragraphs in out.items()}


@lru_cache(maxsize=None)
def guidance(mode: str = "full") -> str:
    """The block appended to a coding worker's prompt."""
    if mode not in MODES:
        raise LupusError("LEAN_MODE_INVALID", f"{mode!r}; one of {', '.join(MODES)}")
    source = resources.files("lupus").joinpath("vendor").joinpath("ponytail").joinpath("SKILL.md").read_text(encoding="utf-8")
    sections = _sections(_filter_for_mode(source, mode))
    missing = [name for name in (*KEEP, "Intensity") if name not in sections]
    unused = [marker for marker in DROP if marker not in source]
    if missing or unused:
        # The pinned file is not the one this adaptation was written against: say so, do not guess.
        raise LupusError("LEAN_SOURCE_CHANGED", "; ".join(missing + unused))
    out = [HEADER]
    for name in KEEP:
        out.append(f"## {name}")
        for paragraph in sections[name]:
            kept = [line for line in paragraph.split("\n") if not line.startswith(DROP)]
            if kept and not paragraph.startswith(DROP):
                out.append("\n".join(kept))
    level = next((p for p in sections["Intensity"] for line in p.split("\n") if line.startswith(f"| **{mode}**")), None)
    if level is None:
        raise LupusError("LEAN_SOURCE_CHANGED", f"no intensity row for {mode}")
    row = next(line for line in level.split("\n") if line.startswith(f"| **{mode}**"))
    out.append("Level: " + " — ".join(cell.strip().strip("*") for cell in row.strip("|").split("|")))
    return "\n\n".join(out)


def applies(criteria: list[dict]) -> bool:
    """Code only: a task judged by running something. Documents, plans and reports are not built
    by the ladder."""
    return any(c["verifier"]["kind"] in ("command", "red_test") for c in criteria)
