import copy
import json
from pathlib import Path

from evaluations import ledger
from .helpers import Env


class LedgerTests(Env):
    def test_published_ledger_quotes_and_counts(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "failure-ledger-2026-10-08.json"
        data = json.loads(path.read_text())
        counts = ledger.check(data)
        self.assertEqual((counts["unique_commits"], counts["observations"]), (8, 23))
        self.assertEqual(counts["by_commit"]["undisclosed_intent"], 6)
        self.assertEqual(counts["by_observation"]["unknown"], 4)
        broken = copy.deepcopy(data)
        group = next(g for g in broken["groups"] if g["labels"][0]["class"] != "unknown")
        group["labels"][0]["quotes"][0]["quote"] = "not a gathered quotation"
        with self.assertRaisesRegex(ValueError, "quote absent"):
            ledger.check(broken)
        data["groups"][0]["labels"] = []
        with self.assertRaisesRegex(ValueError, "missing labels"):
            ledger.check(data)

    def test_collection_groups_repeats_and_matches_plain_in_same_file(self):
        docs, work = self.tmp / "docs", self.tmp / "work"
        docs.mkdir()
        work.mkdir()
        (work / "instances.json").write_text(json.dumps([{"repo": "demo", "commit": "abcdef123", "request": "do it"}]))
        rows = [{"repo": "demo", "commit": "abcdef12", "arm": "plain", "hidden_tests_pass": True},
                {"repo": "demo", "commit": "abcdef12", "arm": "do", "lupus_done": True,
                 "hidden_tests_pass": False, "hidden_detail": "failure", "approval": {"test": "assert x"}},
                {"repo": "demo", "commit": "abcdef12", "arm": "lean", "lupus_done": True, "hidden_tests_pass": False},
                {"repo": "demo", "commit": "abcdef12", "arm": "do", "lupus_done": False, "hidden_tests_pass": False}]
        (docs / "issues-demo-2026-10-07.json").write_text(json.dumps({"driver": "fake", "rows": rows}))
        from unittest import mock
        with mock.patch.object(ledger.subprocess, "run", return_value=mock.Mock(stdout="")):
            data = ledger.collect(work, docs)
        self.assertEqual(len(data["groups"]), 1)
        obs = data["groups"][0]["observations"]
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0]["approval"], {"test": "assert x"})
        self.assertEqual([o["plain_pass"] for o in obs], [[True], [True]])
