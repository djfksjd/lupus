"""`--lean`: implementation-economy guidance adapted from Ponytail, fixed per task by the
supervisor, for code only, and never a reason to build less than was asked."""

import json
from importlib import resources

from lupus import economy, goals, supervisor
from lupus.util import sha256_bytes

from .helpers import CAPS, Env, contains


def command(cid: str) -> dict:
    return {"id": cid, "text": "the tests pass", "verifier": {"kind": "command", "argv": ["/usr/bin/true"], "paths": ["a.py"]}}


class Guidance(Env):
    def test_the_vendored_text_is_the_pinned_unmodified_upstream_file(self):
        vendor = resources.files("lupus").joinpath("vendor")
        lock = json.loads(vendor.joinpath("upstream-lock.json").read_text())["ponytail"]
        self.assertFalse(lock["modified"])
        for name, entry in lock["files"].items():
            self.assertEqual(sha256_bytes(vendor.joinpath(*name.split("/")).read_bytes()), entry["sha256"], name)

    def test_a_worker_gets_the_ladder_and_the_limits_but_not_what_only_fits_a_chat(self):
        text = economy.guidance()
        for kept in ("Already in this codebase?", "Stdlib does it?", "No unrequested abstractions",
                     "Never simplify away: input validation at trust boundaries", "anything\nexplicitly requested",
                     "Bug fix = root cause, not symptom"):
            self.assertIn(kept, text)
        # persona, persistence and mode switching are for an interactive session
        for dropped in ("lazy senior developer", "ACTIVE EVERY RESPONSE", "/ponytail", "stop ponytail", "Caveman"):
            self.assertNotIn(dropped, text)
        # a goal's criteria are not the worker's to renegotiate, and the checks are the supervisor's
        for dropped in ("Ship the lazy version and question it", "leaves ONE runnable check behind", "Code first."):
            self.assertNotIn(dropped, text)
        self.assertIn("Level: full", text)
        self.assertNotIn("**lite**", text)       # the other levels' rows and examples are filtered out
        self.assertNotIn("YAGNI extremist", text)
        self.assertTrue(text.startswith("구현 방식 지침"))
        self.assertIn("요청된 동작을 줄이거나 빼는 근거로 쓰지 마라", text)

    def test_the_mode_filter_keeps_a_rule_that_merely_starts_with_a_mode_word(self):
        body = '---\nname: x\n---\n| **lite** | a |\n| **full** | b |\n- lite: "one"\n- full: "two"\n- Full: not an example\n- other: "kept"'
        self.assertEqual(economy._filter_for_mode(body, "full").split("\n"),
                         ["| **full** | b |", '- full: "two"', "- Full: not an example", '- other: "kept"'])

    def test_an_unknown_level_is_refused(self):
        self.assertRefused("LEAN_MODE_INVALID", economy.guidance, "ultra")


class InThePrompt(Env):
    def _task(self, criterion: dict, lean: bool) -> dict:
        goal = goals.submit(self.k, self.project["project_id"], "demo", [criterion], CAPS)
        return goals.add_task(self.k, goal["goal_id"], "t", "do it", [criterion["id"]], lean=lean)

    def test_only_a_code_task_that_asked_for_it_gets_the_guidance_and_once(self):
        asked = supervisor.build_prompt(self.k, self._task(command("c0"), True), root=self.root)
        self.assertEqual(asked.count("## The ladder"), 1)
        self.assertLess(asked.index("완료 조건"), asked.index("구현 방식 지침"))      # below what it must not override
        self.assertNotIn("## The ladder", supervisor.build_prompt(self.k, self._task(command("c0"), False), root=self.root))
        # a task judged by reading a file, not by running code (a document): never
        self.assertNotIn("## The ladder", supervisor.build_prompt(self.k, self._task(contains("c0", "a.txt", "A"), True), root=self.root))

    def test_a_task_without_it_is_stored_exactly_as_before(self):
        self.assertNotIn("lean", self._task(command("c0"), False)["spec"])
        self.assertIs(self._task(command("c0"), True)["spec"]["lean"], True)
