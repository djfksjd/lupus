"""Offline failure evidence: collect <issues-workdir> <output>; check <ledger>."""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
from pathlib import Path

LABELS = {"undisclosed_intent", "missed_explicit_requirement", "regression", "frozen_test_conflict", "unknown"}
DOCS = Path(__file__).resolve().parents[1] / "docs"


def collect(work: Path, docs: Path = DOCS) -> dict:
    instances = json.loads((work / "instances.json").read_text())
    groups = {}
    for path in sorted(docs.glob("issues-*-2026-10-07.json")):
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            continue
        rows = data.get("rows", [])
        for index, row in enumerate(rows):
            if row.get("arm") == "plain" or not row.get("lupus_done") or row.get("hidden_tests_pass") is not False:
                continue
            matches = [i for i in instances if i["repo"] == row["repo"] and i["commit"].startswith(row["commit"])]
            if len(matches) != 1:
                raise ValueError(f"ambiguous or missing instance: {row['repo']} {row['commit']}")
            inst = matches[0]
            key = (inst["repo"], inst["commit"])
            if key not in groups:
                clone = work / "clones" / inst["repo"]
                def git(*args):
                    return subprocess.run(["git", "-C", str(clone), *args], check=True,
                                          capture_output=True, text=True).stdout
                paths = git("diff", "--name-only", inst["commit"] + "^", inst["commit"], "--", "tests").splitlines()
                groups[key] = {"repo": key[0], "commit": key[1], "request": inst["request"],
                               "test_diff": git("diff", inst["commit"] + "^", inst["commit"], "--", "tests"),
                               "parent_tests": {p: git("show", inst["commit"] + "^:" + p)
                                                for p in paths if git("ls-tree", inst["commit"] + "^", "--", p)},
                               "observations": [], "labels": []}
            plain = [r["hidden_tests_pass"] for r in rows if r.get("arm") == "plain"
                     and r["repo"] == row["repo"] and r["commit"] == row["commit"]]
            groups[key]["observations"].append({"file": path.name, "row": index, "driver": data["driver"],
                                               "arm": row["arm"], "hidden_detail": row.get("hidden_detail"),
                                               "approval": row.get("approval"), "plain_pass": plain})
    return {"date": "2026-10-08", "groups": list(groups.values())}


def material(group: dict) -> dict[str, str]:
    out = {"request": group["request"], "test_diff": group["test_diff"]}
    out.update({"parent_tests." + p: text for p, text in group["parent_tests"].items()})
    for i, obs in enumerate(group["observations"]):
        out[f"observations.{i}.hidden_detail"] = obs.get("hidden_detail") or ""
        if obs.get("approval"):
            out[f"observations.{i}.approval.test"] = obs["approval"].get("test", "")
            out[f"observations.{i}.approval.described"] = json.dumps(obs["approval"].get("described"), ensure_ascii=False)
    return out


def check(data: dict) -> dict:
    commits = collections.Counter(dict.fromkeys(sorted(LABELS), 0))
    observations = collections.Counter(dict.fromkeys(sorted(LABELS), 0))
    seen = set()
    for group in data["groups"]:
        key = (group["repo"], group["commit"])
        if key in seen:
            raise ValueError(f"duplicate group: {key}")
        seen.add(key)
        sources = material(group)
        if not group["labels"]:
            raise ValueError(f"missing labels: {key}")
        labels = set()
        for label in group["labels"]:
            name = label["class"]
            if name not in LABELS or not label.get("reason"):
                raise ValueError(f"invalid label: {key}")
            if name == "unknown":
                if not label.get("missing_evidence"):
                    raise ValueError(f"missing evidence explanation: {key}")
            else:
                quotes = label.get("quotes", [])
                if not quotes:
                    raise ValueError(f"missing quotes: {key}")
                for quote in quotes:
                    if not quote.get("quote") or quote["quote"] not in sources.get(quote["source"], ""):
                        raise ValueError(f"quote absent from gathered material: {key} {quote}")
                if not any(q["source"] == "request" for q in quotes) or not any(q["source"] != "request" for q in quotes):
                    raise ValueError(f"both sides need quotes: {key}")
            labels.add(name)
        commits.update(labels)
        observations.update({name: len(group["observations"]) for name in labels})
    return {"unique_commits": len(seen), "observations": sum(len(g["observations"]) for g in data["groups"]),
            "by_commit": dict(sorted(commits.items())), "by_observation": dict(sorted(observations.items()))}


def write(path: Path, data: dict) -> None:
    # Escape fixture paths too, so the artifact contains no literal home-directory paths.
    text = json.dumps(data, ensure_ascii=False, indent=2).replace("\u002fUsers\u002f", "\\u002fUsers\\u002f").replace("\u002fprivate\u002f", "\\u002fprivate\\u002f")
    path.write_text(text + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    c = commands.add_parser("collect")
    c.add_argument("work", type=Path)
    c.add_argument("output", type=Path)
    c = commands.add_parser("check")
    c.add_argument("ledger", type=Path)
    args = parser.parse_args()
    if args.command == "collect":
        write(args.output, collect(args.work))
    else:
        print(json.dumps(check(json.loads(args.ledger.read_text())), ensure_ascii=False, indent=2))
