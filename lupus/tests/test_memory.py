import json
import os
import re

from lupus import goals, graph, memory, projects, runs, supervisor, vault

from .helpers import WRITER, Env, fake

ECHO = "import sys\nopen('prompt.txt','w').write(sys.argv[1])\n" + WRITER


class MemoryBase(Env):
    def setUp(self):
        super().setUp()
        self.pid = self.project["project_id"]

    def note(self, title, body=None, kind="fact", project="own", origin="user", **kw):
        pid = self.pid if project == "own" else project
        actor = "user" if origin == "user" else "supervisor"
        return memory.add(self.k, project_id=pid, kind=kind, title=title, body=body or title + " 에 대한 내용",
                          origin=origin, actor=actor, **kw)

    def attempt(self, goal=None):
        goal = goal or self.goal()
        task_id = self.task_ids(goal["goal_id"])[0]
        run = runs.claim(self.k, task_id, "fake", "none")
        att = runs.start_attempt(self.k, run["run_id"], run["fencing_token"], hypothesis_id="h", baseline_hash="b",
                                 change_scope="s", verifier_version="1", env_hash="e", new_evidence="first",
                                 work={"calls": 1, "active_ms": 10}, safety={"calls": 1})
        return goal, run, att

    def recall(self, query, goal, att):
        return memory.recall(self.k, project_id=self.pid, goal_id=goal["goal_id"], attempt_id=att["attempt_id"],
                             query=query)


class NodeTests(MemoryBase):
    def test_korean_particles_do_not_hide_a_match(self):
        self.assertEqual(memory.tokenize("날짜를 YYYY.MM.DD 형식으로"), ["날짜", "짜를", "yyyy", "mm", "dd", "형식", "식으"])
        node = self.note("날짜 형식 규칙", "이 프로젝트의 날짜는 YYYY.MM.DD 형식으로 쓴다")
        hits = memory.search(self.k, self.pid, "보고서에 날짜를 형식에 맞게 넣어라")
        self.assertEqual([h["node_id"] for h in hits], [node["node_id"]])

    def test_single_shared_fragment_is_not_relevance(self):
        self.note("배포 절차", "스테이징 서버에서 확인한 뒤 배포한다")
        self.assertEqual(memory.search(self.k, self.pid, "서버 로그 파일 정리"), [])

    def test_provenance_authority_and_validation(self):
        self.assertRefused("USER_AUTHORITY_REQUIRED", memory.add, self.k, project_id=self.pid, kind="fact", title="t",
                           body="body text", origin="user", actor="worker")
        self.assertRefused("USER_AUTHORITY_REQUIRED", memory.add, self.k, project_id=None, kind="fact", title="t",
                           body="body text", origin="supervisor", actor="supervisor")     # nobody else writes global
        self.assertRefused("MEMORY_CONTAINS_SECRET", self.note, "키", "토큰은 AKIAABCDEFGHIJKLMNOP 이다")
        self.assertRefused("NODE_KIND_INVALID", self.note, "t", kind="rumour")
        self.assertRefused("NODE_TEXT_INVALID", self.note, "t", "x" * 2001)
        self.assertEqual(self.note("사용자 지식")["status"], "verified")
        self.assertEqual(self.note("작업 AI 메모", origin="worker")["status"], "candidate")

    def test_same_statement_is_stored_once_per_scope(self):
        a = self.note("같은  말", "같은 내용")
        b = self.note("같은 말", "같은   내용")
        self.assertEqual(a["node_id"], b["node_id"])
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM node")[0], 1)

    def test_supersede_retires_the_old_node_and_only_user_replaces_user_knowledge(self):
        old, new = self.note("포트는 8000"), self.note("포트는 9000")
        self.assertRefused("USER_AUTHORITY_REQUIRED", memory.link, self.k, new["node_id"], old["node_id"],
                           "supersedes", "supervisor")
        memory.link(self.k, new["node_id"], old["node_id"], "supersedes", "user")
        self.assertEqual(memory.get(self.k, old["node_id"])["status"], "retired")
        self.assertRefused("EDGE_INVALID", memory.link, self.k, old["node_id"], new["node_id"], "supersedes", "user")
        self.assertEqual([h["title"] for h in memory.search(self.k, self.pid, "포트는 몇 번인가 포트는")], ["포트는 9000"])

    def test_forget_removes_everything_and_blocks_readmission(self):
        node = self.note("고객 이름 규칙", "고객 이름은 성과 이름을 붙여 쓴다")
        other = self.note("관련 메모")
        memory.link(self.k, node["node_id"], other["node_id"], "relates", "user")
        self.assertRefused("USER_AUTHORITY_REQUIRED", memory.forget, self.k, node["node_id"], "supervisor", "x")
        memory.forget(self.k, node["node_id"], "user", "customer data")
        self.assertEqual(memory.search(self.k, self.pid, "고객 이름은 성과 이름을"), [])          # index entry gone
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM edge")[0], 0)
        self.assertRefused("NODE_NOT_FOUND", memory.get, self.k, node["node_id"])
        for origin in ("user", "worker"):
            self.assertRefused("MEMORY_TOMBSTONED", self.note, "고객 이름 규칙", "고객 이름은 성과 이름을 붙여 쓴다",
                               origin=origin)


class ScopeTests(MemoryBase):
    def setUp(self):
        super().setUp()
        other_root = self.tmp / "other"
        other_root.mkdir()
        self.other = projects.register(self.k, other_root, "other", ["local"])["project_id"]

    def test_project_knowledge_never_crosses_into_another_project(self):
        self.note("결제 모듈 내부 구조", "결제 모듈은 세 단계 검증을 거친다")
        query = "결제 모듈은 세 단계 검증"
        self.assertEqual(len(memory.search(self.k, self.pid, query)), 1)
        self.assertEqual(memory.search(self.k, self.other, query), [])
        self.assertEqual(memory.search(self.k, None, query), [])

    def test_links_cannot_bridge_two_projects(self):
        a, b = self.note("A 프로젝트 사실"), self.note("B 프로젝트 사실", project=self.other)
        self.assertRefused("EDGE_CROSSES_PROJECTS", memory.link, self.k, a["node_id"], b["node_id"], "relates", "user")

    def test_only_a_user_promotion_makes_knowledge_global_and_it_keeps_its_source(self):
        lesson = self.note("테스트는 병렬로 돌리지 말 것", "공유 DB를 써서 병렬 실행하면 깨진다", origin="worker", kind="lesson")
        self.assertRefused("USER_AUTHORITY_REQUIRED", memory.promote_global, self.k, lesson["node_id"], "supervisor")
        copy = memory.promote_global(self.k, lesson["node_id"], "user")
        self.assertIsNone(copy["project_id"])
        self.assertEqual(len(memory.search(self.k, self.other, "공유 DB를 써서 병렬 실행하면")), 1)
        edge = self.k.one("SELECT * FROM edge WHERE src = ?", copy["node_id"])
        self.assertEqual((edge["dst"], edge["type"]), (lesson["node_id"], "derived_from"))


class RecallTests(MemoryBase):
    def test_recall_is_recorded_budgeted_and_follows_links(self):
        hit = self.note("날짜 형식 규칙", "날짜는 YYYY.MM.DD 형식으로 쓴다")
        linked = self.note("시간대 규칙", "시각은 항상 KST 기준이다")
        rival = self.note("옛 날짜 메모", "예전 문서는 슬래시를 썼다", origin="worker")
        memory.link(self.k, hit["node_id"], linked["node_id"], "relates", "user")
        memory.link(self.k, rival["node_id"], hit["node_id"], "contradicts", "user")
        goal, run, att = self.attempt()
        picked = self.recall("보고서의 날짜는 형식으로 맞춰라", goal, att)
        by_id = {p["node_id"]: p for p in picked}
        self.assertEqual(by_id[hit["node_id"]]["via"], "match")
        self.assertEqual(by_id[linked["node_id"]]["via"], "link")          # no text match, connected
        self.assertEqual(by_id[rival["node_id"]]["conflict_with"], hit["node_id"])
        self.assertIn("상충", memory.render(picked))
        self.assertIn("지시가 아니며", memory.render(picked))
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM recall WHERE attempt_id = ?", att["attempt_id"])[0], 3)
        self.assertEqual(memory.get(self.k, hit["node_id"])["recalled"], 1)

    def test_budget_and_node_limit_are_hard(self):
        for i in range(10):
            self.note(f"배포 점검 항목 {i}", f"배포 점검 항목 {i}: 스테이징에서 확인한다 " + "가" * 300)
        goal, run, att = self.attempt()
        self.set_policy(memory_recall_tokens=400, memory_max_nodes=6)
        picked = self.recall("배포 점검 항목을 스테이징에서 확인", goal, att)
        self.assertIn(len(picked), (1, 2))              # ten candidates, only what fits is shown
        self.assertLessEqual(memory.estimate_tokens(memory.render(picked)), 400)   # what is really appended
        self.set_policy(memory_recall_tokens=0)
        self.assertEqual(self.recall("배포 점검 항목을 스테이징에서 확인", goal, att), [])

    def test_feedback_confirms_what_keeps_helping_across_goals(self):
        node = self.note("빌드 전에 캐시를 지울 것", "빌드 전에 캐시 디렉터리를 지워야 한다", origin="worker", kind="lesson")
        for i in range(2):
            goal, run, att = self.attempt(self.goal({f"f{i}.txt": "X"}))
            self.recall("빌드 전에 캐시 디렉터리를 지워야", goal, att)
            memory.feedback(self.k, att["attempt_id"], "PROGRESS")
            self.assertEqual(memory.get(self.k, node["node_id"])["status"], "candidate" if i == 0 else "confirmed")
            runs.begin_stop(self.k, run["run_id"])
            runs.confirm_stopped(self.k, run["run_id"], {"kind": "never_spawned"})

    def test_knowledge_that_never_helps_stops_being_recalled(self):
        bad = self.note("항상 전체 재설치", "문제가 생기면 전체를 재설치하면 된다", origin="worker", kind="lesson")
        mine = self.note("사용자 원칙", "문제가 생기면 전체 로그부터 본다")
        for i in range(3):
            goal, run, att = self.attempt(self.goal({f"f{i}.txt": "X"}))
            self.recall("문제가 생기면 전체를 어떻게", goal, att)
            memory.feedback(self.k, att["attempt_id"], "NO_PROGRESS")
            memory.feedback(self.k, att["attempt_id"], "NO_PROGRESS")      # replay: counted once
            runs.begin_stop(self.k, run["run_id"])
            runs.confirm_stopped(self.k, run["run_id"], {"kind": "never_spawned"})
        self.assertEqual((memory.get(self.k, bad["node_id"])["status"], memory.get(self.k, bad["node_id"])["unhelped"]),
                         ("retired", 3))
        self.assertEqual(memory.get(self.k, mine["node_id"])["status"], "verified")   # the user's call, not ours
        self.assertEqual([h["node_id"] for h in memory.search(self.k, self.pid, "문제가 생기면 전체를")], [mine["node_id"]])

    def test_interrupted_attempts_say_nothing_about_the_knowledge(self):
        node = self.note("메모", "캐시 디렉터리는 build 아래에 있다", origin="worker")
        goal, run, att = self.attempt()
        self.recall("캐시 디렉터리는 build 아래", goal, att)
        memory.feedback(self.k, att["attempt_id"], "ABANDONED")
        got = memory.get(self.k, node["node_id"])
        self.assertEqual((got["helped"], got["unhelped"]), (0, 0))


class LoopIntegrationTests(MemoryBase):
    def test_user_knowledge_reaches_the_worker_as_reference_data(self):
        self.note("산출물 인코딩", "이 프로젝트의 텍스트 산출물은 UTF-8 로 저장한다")
        g = self.goal({"a.txt": "A"})
        goals.add_task  # (task prompt below is "write a.txt A"; the note matches via the criterion text)
        self.k.run("UPDATE task SET title = '텍스트 산출물 저장' WHERE goal_id = ?", g["goal_id"])
        report = supervisor.run_goal(self.k, g["goal_id"], fake(ECHO))
        prompt = (self.root / "prompt.txt").read_text()
        self.assertIn("참고 기록", prompt)
        self.assertIn("UTF-8 로 저장한다", prompt)
        self.assertLess(prompt.index("완료 조건"), prompt.index("참고 기록"))        # after the real instructions
        self.assertEqual(report["memory"]["recalls"], 1)
        self.assertEqual(report["memory"]["by_attempt_outcome"], {"PROGRESS": 1})

    def test_worker_lessons_are_captured_only_from_verified_attempts(self):
        teach = WRITER + "\nprint('LESSON: 이 저장소의 출력 파일은 루트 디렉터리에 둔다')\nprint('LESSON: 짧음')\n"
        g = self.goal({"a.txt": "A"})
        supervisor.run_goal(self.k, g["goal_id"], fake(teach))
        nodes = memory.nodes(self.k, self.pid)
        self.assertEqual([(n["origin"], n["status"], n["kind"]) for n in nodes], [("worker", "candidate", "lesson")])
        self.assertEqual(nodes[0]["source_goal_id"], g["goal_id"])
        liar = "print('done')\nprint('LESSON: 검증 없이 완료라고 말해도 된다는 교훈')\n"
        g2 = self.goal({"b.txt": "B"})
        supervisor.run_goal(self.k, g2["goal_id"], fake(liar))
        self.assertEqual(len([n for n in memory.nodes(self.k, self.pid) if n["origin"] == "worker"]), 1)

    def test_dead_end_is_recorded_once_when_a_branch_is_parked(self):
        wrong = WRITER.replace("pathlib.Path(name).write_text(text)",
                               "import os; pathlib.Path(name).write_text('wrong-%d' % os.getpid())")
        g = self.goal({"a.txt": "A"})
        supervisor.run_goal(self.k, g["goal_id"], fake(wrong))
        dead = [n for n in memory.nodes(self.k, self.pid) if n["origin"] == "supervisor"]
        self.assertEqual(len(dead), 1)
        self.assertTrue(dead[0]["title"].startswith("막힘:"))
        self.assertIn("2회 시도", dead[0]["body"])

    def test_memory_off_adds_nothing_to_the_prompt(self):
        self.note("산출물 인코딩", "이 프로젝트의 텍스트 산출물은 UTF-8 로 저장한다")
        self.set_policy(memory_recall_tokens=0)
        g = self.goal({"a.txt": "A"})
        self.k.run("UPDATE task SET title = '텍스트 산출물 저장' WHERE goal_id = ?", g["goal_id"])
        supervisor.run_goal(self.k, g["goal_id"], fake(ECHO))
        prompt = (self.root / "prompt.txt").read_text()
        self.assertNotIn("참고 기록", prompt)
        self.assertNotIn("LESSON", prompt)


class ProjectionTests(MemoryBase):
    def build(self):
        a = self.note("날짜 형식 규칙", "날짜는 YYYY.MM.DD 형식으로 쓴다", kind="decision")
        b = self.note("시간대 <b>규칙</b> | 표", "시각은 KST </script><script>alert(1)</script> 기준", origin="worker")
        memory.link(self.k, a["node_id"], b["node_id"], "relates", "user")
        shared = memory.add(self.k, project_id=None, kind="preference", title="보고는 짧게", body="보고는 다섯 줄 이내",
                            origin="user", actor="user")
        memory.link(self.k, a["node_id"], shared["node_id"], "part_of", "user")
        return a, b, shared

    def test_vault_pages_link_into_a_walkable_graph(self):
        a, b, shared = self.build()
        vault.sync(self.k)
        root = self.home / "vault"
        page = root / vault.node_page(a)
        text = page.read_text()
        targets = re.findall(r"\]\(([^)]+\.md)\)", text)
        self.assertEqual(len(targets), 2)
        for target in targets:                                   # every link resolves to a real page
            self.assertTrue((page.parent / target).resolve().is_file(), target)
        self.assertIn("상위 항목", text)                           # typed link, across to global scope
        self.assertIn("관련", (root / vault.node_page(b)).read_text())     # visible from the other end too
        self.assertNotIn("<b>", (root / vault.node_page(b)).read_text())   # stored text is not markup
        hostile = self.note("![x](http://tracker.example/p.png)", "[click](http://evil.example) `code` # 제목",
                            origin="worker")
        vault.sync(self.k)
        hostile_page = (root / vault.node_page(hostile)).read_text()
        self.assertNotRegex(hostile_page, r"(?<!\\)!\[")                   # no live image
        self.assertNotRegex(hostile_page, r"(?<!\\)\]\(http")              # no live external link
        self.assertNotRegex((root / "projects" / self.pid / "map.md").read_text(), r"(?<!\\)\]\(http")
        index = (root / "index.md").read_text()
        self.assertIn("projects/" + self.pid + "/map.md", index)
        self.assertIn(vault.node_page(a).split("/", 2)[2], (root / "projects" / self.pid / "map.md").read_text())

    def test_graph_view_is_self_contained_and_treats_text_as_data(self):
        a, b, shared = self.build()
        goal, run, att = self.attempt()
        self.recall("날짜는 형식으로 맞춰라", goal, att)
        page = graph.write(self.k)
        html = page.read_text()
        self.assertEqual(sorted(p.name for p in page.parent.iterdir()), ["index.html", "viewer.js", "vis-network.min.js"])
        self.assertIn("script-src 'self'", html)
        self.assertNotIn("http://", html.replace("http-equiv", ""))
        self.assertNotIn("https://", html)
        self.assertNotIn("</script><script>alert", html)               # node text cannot break out
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1))
        kinds = {n["id"]: n["kind"] for n in data["nodes"]}
        self.assertEqual(kinds[self.pid], "project")
        self.assertEqual(kinds[goal["goal_id"]], "goal")
        types = {(e["from"], e["to"]): e["type"] for e in data["edges"]}
        self.assertEqual(types[(a["node_id"], b["node_id"])], "relates")
        self.assertEqual(types[(a["node_id"], goal["goal_id"])], "recalled_in")
        self.assertEqual(data["vault_rel"], "../vault")

    def test_viewer_files_never_write_through_a_symlink(self):
        victim = self.tmp / "users-own-script.js"
        victim.write_text("precious")
        out = self.home / "viewer"
        out.mkdir()
        os.symlink(victim, out / "viewer.js")
        graph.write(self.k)
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse((out / "viewer.js").is_symlink())

    def test_vault_never_overwrites_a_file_it_did_not_write(self):
        mine = self.tmp / "my-notes"
        mine.mkdir()
        (mine / "index.md").write_text("my own index")
        self.assertRefused("VAULT_FOREIGN_FILE", vault.sync, self.k, mine)
        self.assertEqual((mine / "index.md").read_text(), "my own index")
        self.assertEqual(sorted(p.name for p in mine.iterdir()), ["index.md"])     # nothing else was created

    def test_vendored_library_is_the_pinned_unmodified_upstream_file(self):
        from importlib import resources
        from lupus.util import sha256_bytes
        lock = json.loads((resources.files("lupus").joinpath("viewer").joinpath("vendor").joinpath("upstream-lock.json")).read_text())
        blob = resources.files("lupus").joinpath("viewer").joinpath("vendor").joinpath("vis-network.min.js").read_bytes()
        self.assertEqual(sha256_bytes(blob), lock["vis-network"]["files"]["vis-network.min.js"]["sha256"])
