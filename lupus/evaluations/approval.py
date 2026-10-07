"""Does the description shown at approval expose what a drafted test got wrong? (a proxy)

Input: the JSON written by `issues.py run … do` (each row keeps what a user would have been
shown at approval: the drafted test, how it failed on the original code, and the worker's
description of what the test asserts and what it chose on its own).

For every row a judge model is given the upstream commit's test diff (the reference
expectation, which the worker never saw) and is asked for discrepancies between that and what
the drafted test accepts, twice:

    bare        request + drafted test + red output
    described   the same + the worker's description

Each discrepancy must cite a line of the test or an item of the description. Counted per row
and condition: discrepancies, how many cite the description, and (for rows whose hidden tests
failed) whether any discrepancy was found at all.

What this is not: the judge is told the reference expectation, so it measures whether the
description makes a mismatch easier to point at for someone who already knows the answer. It
does not measure what a user without the reference would notice. That needs a user.

Usage: PYTHONPATH=src python3 evaluations/approval.py <workdir> instances.json <issues-out.json> out.json <judge driver>
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from lupus import adapters, contract

from issues import REPOS, git

PROMPT = """You are checking a test that someone is about to approve as the definition of "done" for a change request.
You are given the request, the drafted test, how it failed before the change{extra}, and separately the REFERENCE: the test changes of the real upstream commit for this request (the person approving does not have it).
List every discrepancy between what the drafted test would accept and what the reference requires: a behaviour, value, message or boundary the reference pins that the drafted test does not check or checks differently. Only list a discrepancy if it would change the test.
Every discrepancy must cite what in the material shows it: either a quoted line of the drafted test (cite "test: <line>") or{cite} nothing else. Do not use any tool; answer with one JSON object only:
{{"discrepancies": [{{"what": "...", "cites": "test: ... | description: item N"}}]}}

REQUEST:
{request}

DRAFTED TEST:
{test}

HOW IT FAILED ON THE ORIGINAL CODE:
{red}
{described}
REFERENCE (upstream test changes):
{reference}
"""


def ask(driver: str, prompt: str) -> list[dict] | None:
    with tempfile.TemporaryDirectory(prefix="lupus-approval-") as tmp:
        result = adapters.execute(adapters.native(driver), prompt, Path(tmp).resolve(), lambda pid: None, timeout_s=300)
    for match in re.finditer(r"\{", result.text or ""):
        try:
            found, _ = json.JSONDecoder().raw_decode(result.text[match.start():])
        except ValueError:
            continue
        if isinstance(found, dict) and isinstance(found.get("discrepancies"), list):
            return [d for d in found["discrepancies"] if isinstance(d, dict)]
    return None


def main(work: Path, instances_path: str, rows_path: str, out_path: str, driver: str) -> None:
    by_commit = {i["commit"][:8]: i for i in json.loads(Path(instances_path).read_text())}
    out = []
    for row in json.loads(Path(rows_path).read_text())["rows"]:
        if row["arm"] == "plain" or "approval" not in row:
            continue
        inst = by_commit[row["commit"]]
        reference = git(work / "clones" / inst["repo"], "show", "--format=", inst["commit"], "--", REPOS[inst["repo"]][2])[:7000]
        shown = row["approval"]
        record = {"repo": row["repo"], "commit": row["commit"], "hidden_tests_pass": row["hidden_tests_pass"],
                  "lupus_done": row.get("lupus_done"), "has_description": shown["described"] is not None,
                  "chosen": [a for a in (shown["described"] or {}).get("assertions", []) if a["source"] == "worker_choice" or not a["quote_in_request"]]}
        for condition in ("bare", "described"):
            text = contract.render(shown["described"]) if condition == "described" else ""
            found = ask(driver, PROMPT.format(
                request=inst["request"], test=shown["test"], red=shown["red"], reference=reference,
                extra=", the author's own description of what the test asserts and what they chose themselves" if text else "",
                cite=' an item of the description (cite "description: item N" or the quoted line), or' if text else "",
                described=f"\nTHE AUTHOR'S DESCRIPTION (may be wrong or incomplete):\n{text}\n" if text else ""))
            record[condition] = None if found is None else {
                "n": len(found), "cite_description": sum("description" in str(d.get("cites", "")).lower() for d in found),
                "items": [{"what": str(d.get("what", ""))[:200], "cites": str(d.get("cites", ""))[:120]} for d in found[:8]]}
        out.append(record)
        print(json.dumps({k: v for k, v in record.items() if k != "chosen"}, ensure_ascii=False)[:400], flush=True)
        wrong = [r for r in out if not r["hidden_tests_pass"]]
        summary = {
            "rows": len(out), "with_description": sum(r["has_description"] for r in out),
            "rows_where_the_worker_named_a_choice": sum(bool(r["chosen"]) for r in out),
            "hidden_fail_rows": len(wrong), "hidden_fail_rows_where_the_worker_named_a_choice": sum(bool(r["chosen"]) for r in wrong),
            **{f"{c}_discrepancies_on_hidden_fail": sum((r[c] or {}).get("n", 0) for r in wrong) for c in ("bare", "described")},
            **{f"{c}_hidden_fail_rows_with_any": sum(bool((r[c] or {}).get("n")) for r in wrong) for c in ("bare", "described")},
            **{f"{c}_discrepancies_on_hidden_pass": sum((r[c] or {}).get("n", 0) for r in out if r["hidden_tests_pass"]) for c in ("bare", "described")},
            "described_citing_the_description": sum((r["described"] or {}).get("cite_description", 0) for r in out)}
        Path(out_path).write_text(json.dumps({"judge": driver, "summary": summary, "rows": out}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
