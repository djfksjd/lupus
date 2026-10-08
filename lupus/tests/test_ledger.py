import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evaluations import ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="lupus-ledger-test-")
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)

    def test_check_rejects_email_anywhere_including_escaped_json(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "failure-ledger-2026-10-08.json"
        original = json.loads(path.read_text())
        for field in ("request", "test_diff", "parent_tests", "observations", "labels", "extra"):
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data["groups"][0][field] = "Person+tag@Example.ORG"
                with self.assertRaisesRegex(ValueError, "email address"):
                    ledger.check(data)
        data = copy.deepcopy(original)
        data["extra"] = {"person@example.org": "key"}
        with self.assertRaisesRegex(ValueError, "email address"):
            ledger.check(data)
        data["extra"] = "person@example.org"
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(data).replace("@", "\\u0040"))
        result = subprocess.run([sys.executable, ledger.__file__, "check", str(bad)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("email address in ledger", result.stderr)

    def test_collection_removes_trailers_and_redacts_all_material(self):
        docs, work = self.tmp / "docs", self.tmp / "work"
        docs.mkdir()
        work.mkdir()
        request = ("Fix contact person@example.org\n\n"
                   "Co-authored-by: Person <person@example.org>\n"
                   "Assisted-by: Helper <helper@example.org>\r\n"
                   "Signed-off-by: Signer <signer@example.org>")
        (work / "instances.json").write_text(json.dumps([
            {"repo": "demo", "commit": "abcdef123", "request": request}]))
        row = {"repo": "demo", "commit": "abcdef12", "arm": "do", "lupus_done": True,
               "hidden_tests_pass": False, "hidden_detail": "contact person@example.org",
               "approval": {"test": "person@example.org", "described": {"contact": ["helper@example.org"]},
                            "red": "signer@example.org"}}
        (docs / "issues-demo-2026-10-07.json").write_text(json.dumps({"driver": "fake", "rows": [row]}))
        from unittest import mock
        outputs = ["tests/test_demo.py\n", "+contact person@example.org\n+@pytest.mark.parametrize('x', [])\n",
                   "tests/test_demo.py\n", "contact helper@example.org\n@pytest.mark.parametrize('x', [])\n"]
        with mock.patch.object(ledger.subprocess, "run", side_effect=[mock.Mock(stdout=s) for s in outputs]):
            data = ledger.collect(work, docs)
        group = data["groups"][0]
        self.assertEqual(group["request"], "Fix contact <address removed>\n\n")
        self.assertEqual(group["test_diff"], "+contact <address removed>\n+@pytest.mark.parametrize('x', [])\n")
        self.assertEqual(group["parent_tests"]["tests/test_demo.py"],
                         "contact <address removed>\n@pytest.mark.parametrize('x', [])\n")
        self.assertEqual(group["observations"][0]["hidden_detail"], "contact <address removed>")
        self.assertEqual(group["observations"][0]["approval"],
                         {"test": "<address removed>", "described": {"contact": ["<address removed>"]},
                          "red": "<address removed>"})
        self.assertFalse(any(ledger.EMAIL.search(s) for s in ledger.strings(data)))

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
