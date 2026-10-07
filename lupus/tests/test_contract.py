"""The description of a drafted test: what it asserts and what the worker chose on its own,
checked where that is possible, bound to the exact test, shown before approval."""

import json

from lupus import contract, goals, quick, supervisor
from lupus.util import sha256_bytes

from .helpers import Env, fake
from .test_quick import CALC, GOOD_TEST, IMPL, STAGED, TEST, WRITE

REQUEST = "calc 에 sub(a, b) 빼기 함수 추가"
PACKET = {
    "assertions": [
        {"test": "test_sub", "input": "sub(5, 3)", "expected": "2", "rule": "빼기", "source": "explicit_request", "quote": "sub(a, b) 빼기 함수"},
        {"test": "test_sub", "input": "sub('a', 1)", "expected": "TypeError", "rule": "숫자가 아니면 TypeError", "source": "worker_choice",
         "choice": "TypeError 를 낸다", "alternative": "ValueError 를 낸다", "differs_on": "sub('a', 1)"},
        {"test": "test_missing", "input": "sub(1)", "expected": "오류", "rule": "인자 둘", "source": "explicit_request", "quote": "인자는 반드시 둘"},
    ],
    "clauses": [{"clause": "빼기 함수 추가", "covered_by": ["test_sub"]}, {"clause": "calc 에", "covered_by": []},
                {"clause": "요청에 없는 구절", "covered_by": []}],
    "untested": [{"tested": "양의 정수", "not_tested": "음수와 실수"}],
}
SAY = "\nprint(%r)\n" % ("done\n" + json.dumps(PACKET, ensure_ascii=False))


class Parse(Env):
    def test_claims_are_checked_where_they_can_be_and_a_failed_check_is_kept_visible(self):
        got = contract.parse("앞말 {\"x\": 1} " + json.dumps(PACKET, ensure_ascii=False), REQUEST, GOOD_TEST)
        first, chosen, false_quote = got["assertions"]
        self.assertTrue(first["quote_in_request"] and first["test_in_file"])
        self.assertEqual(chosen["source"], "worker_choice")
        self.assertFalse(false_quote["quote_in_request"])              # "from the request", but the words are not in it
        self.assertFalse(false_quote["test_in_file"])                  # and the test it names is not in the file
        self.assertEqual([c["clause"] for c in got["clauses"]], ["빼기 함수 추가", "calc 에"])
        self.assertEqual(got["clauses_not_in_request"], 1)
        shown = contract.render(got)
        self.assertIn("정한 것: TypeError 를 낸다 / 다른 선택지: ValueError 를 낸다 / 갈리는 입력: sub('a', 1)", shown)
        self.assertIn("worker는 요청에 있다고 했으나 그 인용이 요청문에 없습니다", shown)
        self.assertIn('요청 문구 중 이 검사가 확인하지 않는 것\n  - "calc 에"', shown)
        self.assertIn("검사 안 함: 음수와 실수", shown)
        self.assertIn("이 이름을 테스트 원문에서 찾지 못함", shown)

    def test_a_quotation_must_be_the_requests_own_letters(self):
        packet = {"assertions": [{"test": "test_sub", "rule": "r", "expected": "e", "source": "explicit_request", "quote": "return FAIL"}]}
        got = contract.parse(json.dumps(packet), "on error  return fail", GOOD_TEST)["assertions"][0]
        self.assertFalse(got["quote_in_request"])                        # `FAIL` is not `fail`
        packet["assertions"][0]["quote"] = "return   fail"               # (spacing is not meaning)
        self.assertTrue(contract.parse(json.dumps(packet), "on error  return fail", GOOD_TEST)["assertions"][0]["quote_in_request"])

    def test_nothing_readable_is_shown_as_not_disclosed_never_as_nothing_assumed(self):
        for reply in ("done", "{not json", json.dumps({"assertions": []}), json.dumps({"assertions": "x"}),
                      json.dumps({"assertions": [{"test": "t", "rule": "key AKIAABCDEFGHIJKLMNOP", "expected": "x"}]})):
            self.assertIsNone(contract.parse(reply, REQUEST, GOOD_TEST), reply)
        self.assertIn("정한 것이 없다는 뜻이 아닙니다", contract.render(None))
        none_chosen = contract.parse(json.dumps({"assertions": [PACKET["assertions"][0]]}, ensure_ascii=False), REQUEST, GOOD_TEST)
        self.assertIn("worker는 없다고 했습니다(확인된 것은 아닙니다)", contract.render(none_chosen))

    def test_what_is_printed_cannot_move_the_cursor_or_hide_text_and_an_unstated_origin_is_the_workers(self):
        hostile = {"assertions": [{"test": "test_sub", "input": "a\x1b[2J\x07b‮c", "expected": "ok\nnext line", "rule": "r" * 900}]}
        got = contract.parse(json.dumps(hostile), REQUEST, GOOD_TEST)["assertions"][0]
        self.assertEqual((got["input"], got["expected"], len(got["rule"])), ("a [2J bc", "ok next line", contract.MAX_TEXT))
        self.assertEqual(got["source"], "worker_choice")


class Flow(Env):
    def setUp(self):
        super().setUp()
        (self.root / "calc.py").write_text(CALC)
        (self.root / "test_calc.py").write_text(TEST)

    def draft(self, tail=SAY):
        d = quick.draft_check(self.k, self.project, REQUEST, "user")
        script = WRITE + (STAGED % (GOOD_TEST, IMPL)).replace("{T}", d["test_path"]) + tail
        self.assertTrue(supervisor.run_goal(self.k, d["goal_id"], fake(script))["done"])
        return d, sha256_bytes((self.root / d["test_path"]).read_bytes())

    def test_the_worker_is_asked_for_it_in_place_of_the_one_word_reply(self):
        d = quick.draft_check(self.k, self.project, REQUEST, "user")
        prompt = supervisor.build_prompt(self.k, goals.tasks(self.k, d["goal_id"])[0], root=self.root)
        self.assertTrue(prompt.endswith(contract.REQUEST))
        self.assertNotIn("'done' 한 단어로 답하라", prompt)              # not two closing instructions that contradict
        build_like = goals.submit(self.k, self.project["project_id"], "x", [
            {"id": "c0", "text": "t", "verifier": {"kind": "command", "argv": ["/usr/bin/true"], "paths": ["calc.py"]}}], {"calls": 9, "attempts": 2, "active_ms": 9})
        other = goals.add_task(self.k, build_like["goal_id"], "t", "p", ["c0"])
        self.assertIn("'done' 한 단어로 답하라", supervisor.build_prompt(self.k, other, root=self.root))

    def test_it_is_stored_for_exactly_the_test_it_describes_and_approval_is_bound_to_what_was_shown(self):
        d, sha = self.draft()
        shown = contract.latest(self.k, d["goal_id"], sha)
        self.assertEqual(len(shown["assertions"]), 3)
        self.assertRefused("CHECK_CHANGED", quick.approve_check, self.k, self.project, d["goal_id"], d["request"], sha, "user",
                           contract_sha256="")                          # the screen showed "not disclosed", but there is one
        self.assertRefused("CHECK_CHANGED", quick.approve_check, self.k, self.project, d["goal_id"], d["request"], sha, "user",
                           contract_sha256="0" * 64)
        build = quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user",
                                    contract_sha256=contract.digest(shown))
        approved = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'check.approved'")[0])
        self.assertEqual((approved["described"], approved["description_shown"]), (contract.digest(shown), True))
        self.assertTrue(supervisor.run_goal(self.k, build["goal_id"], fake("raise SystemExit('never called')"))["done"])

    def test_an_edited_test_has_no_description_any_more(self):
        d, sha = self.draft()
        path = self.root / d["test_path"]
        path.write_text(GOOD_TEST + "    def test_more(self): self.assertEqual(calc.sub(0, 1), -1)\n")
        edited = sha256_bytes(path.read_bytes())
        self.assertIsNone(contract.latest(self.k, d["goal_id"], edited))
        self.assertRefused("CHECK_CHANGED", quick.approve_check, self.k, self.project, d["goal_id"], d["request"], edited, "user",
                           contract_sha256=contract.digest(contract.latest(self.k, d["goal_id"], sha)))      # the old one was shown
        quick.approve_check(self.k, self.project, d["goal_id"], d["request"], edited, "user", contract_sha256="")

    def test_a_worker_that_says_nothing_does_not_block_and_a_caller_that_shows_nothing_is_not_asked(self):
        d, sha = self.draft(tail="\nprint('done')\n")
        self.assertIsNone(contract.latest(self.k, d["goal_id"], sha))
        quick.approve_check(self.k, self.project, d["goal_id"], d["request"], sha, "user")
        approved = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'check.approved'")[0])
        self.assertEqual((approved["described"], approved["description_shown"]), ("", False))
