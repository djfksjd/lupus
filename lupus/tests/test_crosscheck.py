import json
import importlib.util
import unittest
import os
from unittest.mock import patch

from lupus import adapters, budget, goals, crosscheck, probe, quick, runs, service, supervisor, verify
from .helpers import CAPS, Env, evidence

REQUEST = 'Return the difference for sub(a, b).'
BASE = 'def add(a, b):\n    return a + b\ndef sub(a, b):\n    return 0\n'
FINISHED = BASE.replace('return 0', 'return a - b')
TESTS = '''import unittest
from calc import add, sub
class Checks(unittest.TestCase):
    def test_both(self): self.assertEqual(add(1, 2), 3)
    def test_agrees(self): self.assertEqual(sub(3, 1), 2)
    def test_dispute(self): self.assertEqual(sub(3, 1), 9, 'bad\\x1b[2J\\rhidden\\u202e')
    def test_invalid(self): self.assertEqual(unknown(1), 2)
'''


def declarations():
    return [{'test': name, 'input': 'sub(3, 1)', 'expected': '9', 'rule': name,
             'source': 'explicit_request', 'quote': 'Return the difference'}
            for name in ('test_both', 'test_agrees', 'test_dispute', 'test_invalid')]


class CrosscheckTests(Env):
    def setUp(self):
        super().setUp()
        self.addCleanup(service.OVERRIDE.clear)
        (self.root / 'calc.py').write_text(BASE)
        # The real draft entry point captures before any worker; no existing tests need a verifier.
        self.draft = quick.draft_check(self.k, self.project, REQUEST, 'user', cross_check=True)
        self.test_path = self.draft['test_path']
        (self.root / self.test_path).write_text('WORKER TEST MUST NOT BE SHOWN')
        (self.root / 'calc.py').write_text(FINISHED)
        self.build = goals.submit(self.k, self.project['project_id'], REQUEST,
                                  [{'id': 'c0', 'text': 'fixture', 'verifier': {'kind': 'file_contains',
                                    'path': 'calc.py', 'text': 'sub'}}], {**CAPS, 'tokens': 10000})
        self.gid = self.build['goal_id']
        task = goals.add_task(self.k, self.gid, 'fixture', 'fixture', ['c0'])
        evidence(self.k, self.gid, 'c0', 'PASS')
        with self.k.tx():
            self.k.run("UPDATE goal SET status = 'DONE' WHERE goal_id = ?", self.gid)
            self.k.run("UPDATE task SET status = 'DONE' WHERE task_id = ?", task['task_id'])
            self.k.emit('user', 'check.approved', 'goal', self.gid, draft_goal=self.draft['goal_id'], test=self.test_path)
        self.real_run = verify._run_gated
        # The outer sandbox cannot inspect process start times; children still use the exec gate.
        def process_start(pid):
            try:
                os.kill(pid, 0)
                return 'fixture-start'
            except ProcessLookupError:
                return None
        self.process_clock = patch.object(runs, 'proc_start', side_effect=process_start)
        self.process_clock.start()
        self.addCleanup(self.process_clock.stop)

    def adapter(self, tests=TESTS, reply=None, extra='', error=None):
        script = ('import pathlib, sys\n'
                  f'assert pathlib.Path("calc.py").read_text() == {BASE!r}\n'
                  f'assert not pathlib.Path({self.test_path!r}).exists()\n'
                  'assert "WORKER TEST" not in sys.argv[1]\n'
                  f'pathlib.Path("{crosscheck.CROSSCHECK_DIR}/test_checks.py").write_text({tests!r})\n'
                  + extra + '\nprint(' + repr(json.dumps(declarations() if reply is None else reply)) + ')\n')
        if error:
            script += 'raise SystemExit(3)\n'
        a = adapters.FakeAdapter(script, usage={'tokens_in': 7, 'tokens_cached': 2, 'tokens_out': 5}, error_class=error)
        a.driver = 'fake_alt'
        service.OVERRIDE['fake_alt'] = a

    def state(self):
        return ({p.relative_to(self.root).as_posix():
                 ('link', os.readlink(p)) if p.is_symlink() else
                 ('file', p.read_bytes(), p.stat().st_mode) if p.is_file() else ('dir', p.stat().st_mode)
                 for p in self.root.rglob('*')},
                [tuple(r) for r in self.k.q('SELECT * FROM goal WHERE goal_id = ?', self.gid)],
                [tuple(r) for r in self.k.q('SELECT * FROM evidence WHERE goal_id = ?', self.gid)],
                goals.wait_reasons(self.k, self.gid), goals.tasks(self.k, self.gid),
                goals.completion_blockers(self.k, self.gid))

    def run_probe(self, timeout_s=10):
        # Exercise real structured runner processes without nesting the unavailable OS sandbox.
        def local(verifier, *args):
            self.assertNotIn('sandbox', verifier)
            return self.real_run({**verifier, 'sandbox': False}, *args)
        with patch.object(verify, '_run_gated', side_effect=local):
            return crosscheck.shadow(self.k, self.gid, 'fake_alt', timeout_s=timeout_s)

    def test_isolation_classification_shadow_budget_and_inert_output(self):
        self.adapter(extra='pathlib.Path("outside.py").write_text("outside")\n'
                     f'pathlib.Path("{crosscheck.CROSSCHECK_DIR}/conftest.py").write_text("raise RuntimeError()")\n'
                     'pathlib.Path("calc.py").write_text("broken implementation")')
        before = self.state()
        verdict = supervisor.finish(self.k, self.gid)
        out = self.run_probe()
        self.assertEqual(supervisor.finish(self.k, self.gid), verdict)
        self.assertNotIn('error', out, out)
        self.assertEqual(out['counts'], dict.fromkeys(crosscheck.CLASSIFICATIONS, 1))
        self.assertEqual([d['test'] for d in out['disputes']], ['test_dispute'])
        self.assertEqual(before, self.state())
        self.assertEqual(out['tokens'], 14)
        used = budget.snapshot(self.k, self.build['budget_id'])
        self.assertEqual((used['tokens']['used'], used['calls']['used']), (14, 1))
        self.assertGreater(used['active_ms']['used'], 0)
        shown = crosscheck.render(out)
        for char in ('\x1b', '\r', '\u202e'):
            self.assertNotIn(char, shown)
        self.assertIn('<U+001B>', shown)
        self.assertIn('the request could also be read as 9', shown)
        event = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'crosscheck.recorded' AND aggregate_id = ?",
                                    self.gid)['payload'])
        self.assertEqual(event, out)
        self.assertTrue(out['disputes'][0]['quote_in_request'])

    def test_model_failure_is_shadow_and_charged(self):
        self.adapter(error='timeout')
        before = self.state()
        out = self.run_probe()
        self.assertEqual(before, self.state())
        self.assertEqual(out['error'], 'CROSSCHECK_MODEL_FAILED')
        self.assertEqual(out['disputes'], [])
        self.assertEqual(out['tokens'], 14)
        self.assertEqual(budget.snapshot(self.k, self.build['budget_id'])['calls']['used'], 1)

    def test_syntax_import_wrong_name_and_runtime_error_are_invalid(self):
        for source, count in (('this is broken python !', 4), ('import nonexistent_probe_module', 4),
                              (TESTS.replace('test_invalid', 'test_other'), 2)):
            with self.subTest(source=source):
                self.adapter(tests=source)
                before = self.state()
                out = self.run_probe()
                self.assertEqual(before, self.state())
                self.assertEqual(out['counts']['INVALID'], count, out)
                self.assertNotIn('test_invalid', [d['test'] for d in out['disputes']])

    def test_nothing_produced_budget_and_runner_failures_are_shadow(self):
        self.adapter(reply=[])
        before = self.state()
        self.assertEqual(self.run_probe()['error'], 'CROSSCHECK_NOTHING_PRODUCED')
        self.assertEqual(before, self.state())
        self.adapter()
        with patch.object(verify, '_run_gated', side_effect=RuntimeError('sandbox')):
            self.assertEqual(crosscheck.shadow(self.k, self.gid, 'fake_alt')['error'], 'RuntimeError')
        self.assertEqual(before, self.state())
        with patch.object(budget, 'reserve', side_effect=RuntimeError('budget')):
            self.assertEqual(self.run_probe()['error'], 'RuntimeError')
        self.assertEqual(before, self.state())

    def test_extraction_limits_secrets_configuration_and_links(self):
        work = self.tmp / 'author'
        folder = work / crosscheck.CROSSCHECK_DIR
        folder.mkdir(parents=True)
        (work / 'test_outside.py').write_text(TESTS)
        for name in ('conftest.py', 'sitecustomize.py', 'pyproject.toml'):
            (folder / name).write_text('config')
        (folder / 'test_secret.py').write_text("key = 'AKIAABCDEFGHIJKLMNOP'")
        (folder / 'test_link.py').symlink_to(work / 'test_outside.py')
        (folder / 'test_good.py').write_text(TESTS)
        self.assertEqual(crosscheck.take_tests(work), {'test_good.py': TESTS.encode()})
        (folder / 'test_large.py').write_bytes(b'x' * (crosscheck.MAX_CROSSCHECK_BYTES + 1))
        self.assertRefused('CROSSCHECK_TOO_LARGE', crosscheck.take_tests, work)
        (folder / 'test_large.py').unlink()
        for i in range(crosscheck.MAX_CROSSCHECK_FILES):
            (folder / f'test_{i}.py').write_text('pass')
        self.assertRefused('CROSSCHECK_TOO_LARGE', crosscheck.take_tests, work)

    def test_provenance_quote_checks_and_grouping(self):
        items = declarations()
        items[2]['quote'] = 'words not in the request'
        descriptions = crosscheck._descriptions(json.dumps(items), REQUEST, {'test_x.py': TESTS.encode()})
        self.assertFalse(descriptions[2]['quote_in_request'])
        entry = {**descriptions[2], 'observed': '\x1b[2J\rtext'}
        out = crosscheck.render({'disputes': [entry] * 10})
        self.assertEqual(out.count('on sub(3, 1)'), 1)
        self.assertIn('quote check: False', out)
        self.assertNotIn('\x1b', out)
        self.assertTrue(crosscheck._inert('stack context ' * 1000 + 'observed actual').endswith('observed actual'))
        self.assertEqual(crosscheck.classify('INVALID', 'ASSERTION'), 'INVALID')

    def test_harness_summary_counts_without_judgement(self):
        from evaluations.issues import probe_summary
        rows = [{'lupus_done': True, 'hidden_tests_pass': passed, 'disputes': disputes,
                 'probe_counts': {'INVALID': 2}, 'probe_tokens': 14, 'probe_seconds': 3}
                for passed, disputes in ((False, [{}]), (False, []), (True, [{}]), (True, []))]
        out = probe_summary(rows)
        self.assertEqual((out['done_hidden_fail'], out['done_hidden_fail_with_dispute']), (2, 1))
        self.assertEqual((out['done_hidden_pass'], out['done_hidden_pass_with_dispute']), (2, 1))
        self.assertEqual(out['invalid_probes'], 8)
        self.assertEqual((out['probe_tokens_mean'], out['probe_seconds_mean']), (14, 3))

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is an optional project runner")
    def test_pytest_structured_outcomes(self):
        tests = """from calc import add, sub
import pytest
def test_both(): assert add(1, 2) == 3
def test_agrees(): assert sub(3, 1) == 2
def test_dispute(): assert sub(3, 1) == 9
def test_invalid(): unknown(1)
"""
        criteria = goals.criteria(self.k, self.draft['goal_id'])
        criteria[0]['verifier']['require_tests'] = 'pytest'
        original = goals.criteria
        def fixture(k, gid, revision=None):
            return criteria if gid == self.draft['goal_id'] else original(k, gid, revision)
        self.adapter(tests=tests)
        with patch.object(goals, 'criteria', side_effect=fixture):
            out = self.run_probe()
        self.assertNotIn('error', out, out)
        self.assertEqual(out['counts'], dict.fromkeys(crosscheck.CLASSIFICATIONS, 1))

    def test_cli_goal_probe_prints_only_rendering_and_keeps_capability_command(self):
        import contextlib
        import io
        from lupus import cli
        for same_vendor in (True, False):
            record = {"disputes": [], "same_vendor": same_vendor, "worker_drivers": ["native_codex"]}
            with patch.object(crosscheck, 'shadow', return_value=record) as shadow:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.main(['--home', str(self.home), 'cross-check', self.gid, '--driver', 'codex'])
                self.assertEqual(code, 0)
                vendor_line = ("Same vendor: true (author and worker; not cross-vendor results)" if same_vendor
                               else "Same vendor: false (author and worker)")
                self.assertEqual(output.getvalue(), 'Author confinement: unverified\n' + vendor_line + '\nNo disputes observed.\n')
                self.assertEqual(shadow.call_args.args[1:], (self.gid, 'native_codex'))
        with patch.object(probe, 'run', return_value={'capabilities': []}) as capability:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--home', str(self.home), 'probe']), 0)
            self.assertFalse(capability.call_args.kwargs['live'])

    def test_author_profiles_exclude_finished_project_and_shared_temp(self):
        from lupus import util
        profile = adapters.codex_filesystem_profile(shared_tmp=False)
        self.assertNotIn(':tmpdir', profile)
        self.assertEqual(profile[':project_roots']['.'], 'write')
        author = self.tmp / 'author-profile'
        author.mkdir()
        policy = util.sandbox_profile_worker(author, 'claude', (str(self.root),))
        self.assertIn('(deny file-read* file-write* (subpath ' + json.dumps(str(self.root), ensure_ascii=False) + '))', policy)

    def test_timeout_and_execute_exception_charge_calls_and_preserve_shadow_state(self):
        self.adapter(extra='import time; time.sleep(1)')
        service.OVERRIDE['fake_alt'].usage = None
        before = self.state()
        out = self.run_probe(timeout_s=0.01)
        self.assertEqual(out['error_detail'], 'timeout')
        self.assertFalse(out['usage_observed'])
        self.assertEqual(out['tokens'], 0)
        self.assertEqual(before, self.state())
        calls = budget.snapshot(self.k, self.build['budget_id'])['calls']['used']
        self.assertEqual(calls, 1)
        with patch.object(adapters, 'execute', side_effect=RuntimeError('model execution')):
            out = self.run_probe()
        self.assertEqual(out['error'], 'RuntimeError')
        self.assertEqual(before, self.state())
        self.assertEqual(budget.snapshot(self.k, self.build['budget_id'])['calls']['used'], 2)
        self.assertEqual(self.k.one('SELECT status FROM service_call ORDER BY rowid DESC LIMIT 1')['status'], 'FAILED')

    def test_snapshot_preserves_binary_fixtures_and_does_not_copy_private_files_or_links(self):
        source = self.tmp / 'snapshot-source'
        source.mkdir()
        (source / 'fixture.bin').write_bytes(b'\x00\x01data')
        (source / '.env').write_text('private')
        (source / 'link').symlink_to(self.root)
        dest = self.tmp / 'snapshot-copy'
        crosscheck._copy_files(source, dest)
        self.assertEqual((dest / 'fixture.bin').read_bytes(), b'\x00\x01data')
        self.assertFalse((dest / '.env').exists())
        self.assertFalse((dest / 'link').exists())

    def test_service_author_workspace_cannot_be_the_project(self):
        self.adapter()
        self.assertRefused('CROSSCHECK_WORKSPACE_INVALID', service.call, self.k,
                           project_id=self.project['project_id'], goal_id=self.gid,
                           budget_id=self.build['budget_id'], purpose='judge', driver='fake_alt',
                           prompt='probe', workspace=self.root)

    def test_default_draft_neither_copies_nor_records_snapshot(self):
        from lupus import projects
        root = self.tmp / "default-project"
        root.mkdir()
        (root / "calc.py").write_text(BASE)
        project = projects.register(self.k, root, "default", ["local"])
        with patch.object(crosscheck, "capture_base") as capture:
            draft = quick.draft_check(self.k, project, REQUEST, "user")
        capture.assert_not_called()
        self.assertFalse((self.k.runtime / "base" / (draft["goal_id"] + "-crosscheck")).exists())
        self.assertIsNone(self.k.one("SELECT 1 FROM event WHERE type = 'do.crosscheck_base' AND aggregate_id = ?", draft["goal_id"]))
        timing = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'timing.operation' AND aggregate_id = ?", draft["goal_id"])[0])
        self.assertEqual(timing["stages_ns"]["crosscheck_snapshot"], 0)

    def test_snapshot_timing_and_prune_respects_active_owner(self):
        from lupus import jobs
        path = self.k.runtime / "base" / (self.draft["goal_id"] + "-crosscheck")
        timing = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'timing.operation' AND aggregate_id = ?", self.draft["goal_id"])[0])
        self.assertGreater(timing["stages_ns"]["crosscheck_snapshot"], 0)
        jobs.prune(self.k, 0)
        self.assertTrue(path.is_dir())
        with self.k.tx():
            self.k.run("UPDATE goal SET status = 'CANCELLED' WHERE goal_id = ?", self.draft["goal_id"])
        self.assertEqual(jobs.prune(self.k, 0)["snapshots"], 1)
        self.assertFalse(path.exists())

    def test_author_input_audit_and_distinct_service_purpose(self):
        import hashlib
        self.adapter()
        out = self.run_probe()
        audit = out["author_input"]
        self.assertTrue(audit["snapshot_verified"])
        self.assertTrue(audit["worker_test_absent"])
        self.assertNotIn(self.test_path, audit["workspace_manifest"])
        self.assertEqual(audit["workspace_manifest"]["calc.py"], hashlib.sha256(BASE.encode()).hexdigest())
        self.assertNotEqual(audit["workspace_manifest"]["calc.py"], hashlib.sha256(FINISHED.encode()).hexdigest())
        self.assertEqual(self.k.one("SELECT purpose FROM service_call WHERE goal_id = ?", self.gid)[0], "crosscheck")
        base = self.k.runtime / "base" / (self.draft["goal_id"] + "-crosscheck")
        (base / "calc.py").write_text(FINISHED)
        with patch.object(service, "call") as call:
            self.assertEqual(self.run_probe()["error"], "CROSSCHECK_BASE_CHANGED")
        call.assert_not_called()

    def test_regression_and_fails_both_render_and_summary(self):
        from evaluations.issues import probe_summary
        regressed = FINISHED.replace("return a + b", "return 0")
        (self.root / "calc.py").write_text(regressed)
        self.adapter()
        out = self.run_probe()
        self.assertEqual((out["regressions"], out["fails_both"]), (1, 1))
        self.assertEqual(out["counts"]["DISPUTE"], 2)
        text = crosscheck.render(out)
        self.assertIn("regression (passed pristine)", text)
        self.assertIn("fails both pristine and implementation", text)
        summary = probe_summary([{"lupus_done": True, "hidden_tests_pass": True, "crosscheck": out}])
        self.assertEqual((summary["regressions"], summary["fails_both"]), (1, 1))

    def test_live_canary_reports_leaks_write_failure_and_cleanup_offline(self):
        from evaluations import crosscheck_canary
        from pathlib import Path
        def execute(adapter, prompt, work, spawned, timeout):
            paths = [Path(line.split(": ", 1)[1]) for line in prompt.splitlines()
                     if line.startswith(("finished_project:", "runtime:", "sibling_temp:", "snapshot_parent:", "workspace_temp_neighbor:"))]
            self.assertEqual(len(paths), 5)
            self.assertEqual(paths[4].parent.parent, work.parent)
            self.assertNotEqual(paths[4].parent, work)
            self.assertTrue(all(p.is_file() for p in paths))
            (work / "author-write.txt").write_text("CROSSCHECK-WRITE-OK")
            return adapters.AdapterResult(0, text=paths[2].read_text())
        with patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")) as factory, patch.object(adapters, "execute", side_effect=execute):
            out = crosscheck_canary.run(self.k, self.gid, "native_claude")
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[2], (str(self.root), str(self.k.home), str(self.k.runtime / "base")))
        self.assertFalse(out["passed"])
        self.assertTrue(out["contents_returned"]["sibling_temp"])
        self.assertTrue(out["workspace_write"])
        self.assertFalse(list(self.root.glob("crosscheck-canary-*")))
        self.assertFalse(list(self.k.runtime.rglob("crosscheck-canary-*")))
        def blocked(adapter, prompt, work, spawned, timeout):
            (work / "author-write.txt").write_text("CROSSCHECK-WRITE-OK")
            return adapters.AdapterResult(0, text="BLOCKED")
        with patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")), patch.object(adapters, "execute", side_effect=blocked):
            self.assertTrue(crosscheck_canary.run(self.k, self.gid, "native_codex")["passed"])
        with patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")), patch.object(adapters, "execute", return_value=adapters.AdapterResult(0, text="BLOCKED")):
            self.assertFalse(crosscheck_canary.run(self.k, self.gid, "native_codex")["passed"])

    def test_claude_author_has_private_temp_and_shared_temp_denials(self):
        from lupus import util
        work = self.tmp / "claude-author"
        work.mkdir()
        with patch.object(service, "sandbox_available", return_value=True):
            author = service.author_adapter("native_claude", work, (str(self.root), str(self.k.runtime / "base")))
        self.assertFalse(author.shared_tmp)
        self.assertEqual(author.private_tmp, work / ".lupus-author-tmp")
        policy = util.sandbox_profile_worker(work, "claude", author.read_exclude, author.shared_tmp)
        self.assertIn('(deny file-read* (subpath ', policy)
        read_deny = next(line for line in policy.splitlines() if line.startswith("(deny file-read* ") and "/private/var/folders" in line)
        self.assertIn('"/private/tmp"', read_deny)
        write_allow = next(line for line in policy.splitlines() if line.startswith("(allow file-write*"))
        self.assertNotIn('(subpath "/private/tmp")', write_allow)
        self.assertNotIn('(subpath "/private/var/folders")', write_allow)

    def test_v5_upgrade_preserves_service_rows_and_accepts_crosscheck_purpose(self):
        from lupus.kernel import migrations
        with self.k.tx():
            self.k.run("INSERT INTO service_call VALUES (?,?,?,?,?,?,?,?,?,?,?)", "old-judge", self.project["project_id"], self.gid,
                       self.build["budget_id"], "judge", "fake_alt", "DONE", None, None, self.k.now(), self.k.now())
        row = tuple(self.k.one("SELECT * FROM service_call WHERE call_id = 'old-judge'"))
        sql = next(sql for version, name, sql, checksum in migrations() if version == 5)
        table = sql[sql.index("CREATE TABLE service_call"):sql.index("-- Alpha:")]
        self.k.conn.executescript("DROP TABLE service_call;" + table)
        self.k.conn.execute("INSERT INTO service_call VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        self.k.conn.execute("DELETE FROM schema_migration WHERE version = 6")
        self.reopen()
        self.assertEqual(tuple(self.k.one("SELECT * FROM service_call WHERE call_id = 'old-judge'")), row)
        self.adapter()
        self.assertNotIn("error", self.run_probe())
        self.assertEqual(self.k.one("SELECT COUNT(*) FROM service_call WHERE purpose = 'crosscheck'")[0], 1)

    def test_author_fails_closed_if_os_sandbox_is_unavailable_at_execute(self):
        from pathlib import Path
        work = self.tmp / "fail-closed-author"
        work.mkdir()
        with patch.object(service, "sandbox_available", return_value=True):
            author = service.author_adapter("native_claude", work, (str(self.root),))
        with patch.object(adapters, "sandbox_available", return_value=False), patch.object(adapters.subprocess, "Popen") as spawn:
            self.assertRefused("CROSSCHECK_SANDBOX_UNAVAILABLE", adapters.execute, author, "test", work, lambda pid: None, 10)
        spawn.assert_not_called()

    def test_codex_author_denies_protected_trees_even_under_installation_grants(self):
        from pathlib import Path
        work = self.tmp / "codex-author"
        work.mkdir()
        protected = ("/private/tmp/finished", "/private/tmp/state", "/private/tmp/state/base",
                     "/arbitrary/project", "/private/var/folders/example/T/snapshot")
        cli = "/private/tmp/installation/bin/codex"
        real = "/private/tmp/installation/packages/codex/bin/codex"
        with patch.object(adapters.shutil, "which", return_value=cli):
            with patch.object(adapters.os.path, "realpath", side_effect=lambda p, **kwargs: real if str(p) == cli else str(p)):
                author = service.author_adapter("native_codex", work, protected)
                profile = adapters.codex_filesystem_profile(shared_tmp=False, read_exclude=author.read_exclude)
                argv = author.argv("test", work)
        for path in protected:
            self.assertEqual(profile[path], "deny")
            self.assertIn(adapters._toml({path: "deny"})[1:-1], " ".join(argv))
        self.assertEqual(profile["/private/tmp/installation/bin"], "read")
        self.assertEqual(profile["/private/tmp/installation/packages"], "read")
        for path in (":tmpdir", ":slash_tmp", "/tmp", "/private/tmp", "/private/var/folders"):
            self.assertNotIn(path, profile)
        nested = "/private/tmp/installation/packages/finished"
        self.assertEqual(adapters.codex_filesystem_profile(shared_tmp=False, read_exclude=(nested,))[nested], "deny")

    def test_author_confinement_requires_current_driver_canary_and_revokes_failure(self):
        from evaluations import crosscheck_canary
        self.adapter()
        self.assertEqual(self.run_probe()["author_confinement"], "unverified")
        # Ordinary probe facts, including read_confinement, never establish this claim.
        with self.k.tx():
            self.k.run("INSERT OR REPLACE INTO capability VALUES (?,?,?,?,?,?,?,?)", "native_codex",
                       "read_confinement", "default_auth_stripped", "verified", "identity", "home only", "probe", self.k.now())
        self.assertEqual(crosscheck.author_confinement(self.k, "native_codex"), "unverified")
        def blocked(adapter, prompt, work, spawned, timeout):
            (work / "author-write.txt").write_text("CROSSCHECK-WRITE-OK")
            return adapters.AdapterResult(0, text="BLOCKED")
        with patch.object(crosscheck, "author_cli_identity", return_value="sha256:fixture"), patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")), patch.object(adapters, "execute", side_effect=blocked):
            report = crosscheck_canary.run(self.k, self.gid, "native_codex")
            self.assertEqual(report["author_confinement"], "verified by canary")
            self.assertEqual(crosscheck.author_confinement(self.k, "native_codex"), "verified by canary")
            self.assertEqual(crosscheck.author_confinement(self.k, "native_claude"), "unverified")
            with patch.object(crosscheck, "author_cli_identity", return_value="sha256:upgrade"):
                self.assertEqual(crosscheck.author_confinement(self.k, "native_codex"), "unverified")
            with patch.object(adapters, "execute", return_value=adapters.AdapterResult(1, error_class="crash")):
                self.assertFalse(crosscheck_canary.run(self.k, self.gid, "native_codex")["passed"])
            self.assertEqual(crosscheck.author_confinement(self.k, "native_codex"), "unverified")
        self.assertEqual(self.k.one("SELECT status FROM capability WHERE name = ?", crosscheck.AUTHOR_CONFINEMENT_NAME)[0], "unsupported")

    def test_crosscheck_persists_canary_flag_in_event_and_rendering(self):
        self.adapter()
        with patch.object(crosscheck, "author_cli_identity", return_value="sha256:fixture"):
            with self.k.tx():
                self.k.run("INSERT INTO capability VALUES (?,?,?,?,?,?,?,?)", "fake_alt",
                           crosscheck.AUTHOR_CONFINEMENT_NAME, crosscheck.AUTHOR_CONFINEMENT_MODE,
                           "verified", "sha256:fixture", "fixture canary", "fixture", self.k.now())
            out = self.run_probe()
        self.assertEqual(out["author_confinement"], "verified by canary")
        event = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'crosscheck.recorded' AND aggregate_id = ?", self.gid)[0])
        self.assertEqual(event["author_confinement"], "verified by canary")
        self.assertIn("Author confinement: verified by canary", crosscheck.render(out))

    def test_harness_splits_counts_by_recorded_confinement(self):
        from evaluations.issues import probe_summary
        rows = [{"lupus_done": True, "hidden_tests_pass": False, "disputes": [{}],
                 "crosscheck": {"author_confinement": flag}, "probe_counts": {"INVALID": count}}
                for flag, count in (("verified by canary", 2), ("unverified", 3))]
        rows.append({"lupus_done": True, "hidden_tests_pass": True})
        split = probe_summary(rows)["by_author_confinement"]
        self.assertEqual(split["verified by canary"]["n"], 1)
        self.assertEqual(split["verified by canary"]["invalid_probes"], 2)
        self.assertEqual(split["verified by canary"]["counts"]["INVALID"], 2)
        self.assertEqual(split["unverified"]["n"], 2)
        self.assertEqual(split["unverified"]["invalid_probes"], 3)
        self.assertEqual(split["unverified"]["counts"]["INVALID"], 3)
        self.assertEqual(split["unverified"]["done_hidden_pass"], 1)

    def record_worker(self, driver, goal_id=None):
        goal_id = goal_id or self.gid
        task = goals.tasks(self.k, goal_id)[0]
        with self.k.tx():
            self.k.run("INSERT INTO run(run_id, project_id, goal_id, task_id, execution_driver, auth_mode, "
                       "fencing_token, status, lease_expires_at, revocation_epoch, recovery_epoch, policy_version, "
                       "stop_evidence, started_at) VALUES (?,?,?,?,?,?,?, 'STOPPED', ?,?,?,?, 'fixture', ?)",
                       "fixture-" + driver, self.project["project_id"], goal_id, task["task_id"], driver,
                       "fixture", self.k.next_counter("next_fencing_token"), self.k.now(), 0, 0, 1, self.k.now())

    def test_same_vendor_record_rendering_and_isolation(self):
        self.adapter()
        self.record_worker("fake_alt")
        before = self.state()
        out = self.run_probe()
        self.assertTrue(out["same_vendor"])
        self.assertEqual(out["worker_drivers"], ["fake_alt"])
        self.assertNotIn("error", out)
        self.assertEqual(before, self.state())
        self.assertIn("Same vendor: true", crosscheck.render(out))
        self.assertIn("not cross-vendor results", crosscheck.render(out))
        event = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'crosscheck.recorded' "
                                     "AND aggregate_id = ?", self.gid)[0])
        self.assertTrue(event["same_vendor"])

    def test_cross_vendor_record_and_unknown_worker_rendering(self):
        self.adapter()
        self.assertIn("cross-vendor comparison not established", crosscheck.render(self.run_probe()))
        self.record_worker("fake_other")
        out = self.run_probe()
        self.assertFalse(out["same_vendor"])
        self.assertEqual(out["worker_drivers"], ["fake_other"])
        self.assertIn("Same vendor: false", crosscheck.render(out))
        self.assertNotIn("unavailable", crosscheck.render(out))

    def test_same_vendor_survives_author_failure_and_mixed_workers(self):
        self.record_worker("native_codex", self.draft["goal_id"])
        self.record_worker("native_claude")
        with patch.object(service, "call", return_value=adapters.AdapterResult(1, error_class="timeout")):
            out = crosscheck.shadow(self.k, self.gid, "native_codex", timeout_s=10)
        self.assertTrue(out["same_vendor"])
        self.assertEqual(out["worker_drivers"], ["native_claude", "native_codex"])
        self.assertEqual(out["error"], "CROSSCHECK_MODEL_FAILED")
        self.assertIn("Same vendor: true", crosscheck.render(out))

    def test_harness_same_vendor_opt_in_validation_and_cli(self):
        from evaluations import issues
        from pathlib import Path
        for driver in ("native_codex", "native_claude"):
            with self.assertRaisesRegex(ValueError, "--allow-same-vendor"):
                issues.run(self.tmp, "missing.json", "unused.json", driver, 20, arms=("probe",), prober=driver)
        with self.assertRaisesRegex(ValueError, "requires a prober driver"):
            issues.run(self.tmp, "missing.json", "unused.json", "native_codex", 20,
                       arms=("probe",), allow_same_vendor=True)
        # Accepted configurations reach the input read, before any CLI call or setup.
        for driver, prober, opt_in in (("native_codex", "native_codex", True),
                                      ("native_claude", "native_claude", True),
                                      ("native_codex", "native_claude", False)):
            with self.assertRaises(FileNotFoundError):
                issues.run(self.tmp, str(self.tmp / "missing.json"), "unused.json", driver, 20,
                           arms=("probe",), prober=prober, allow_same_vendor=opt_in)
        argv = ["run", str(self.tmp), "instances.json", "out.json", "native_codex", "20", "", "probe", "", "native_codex"]
        with patch.object(issues, "run") as run:
            issues.main(argv)
            self.assertFalse(run.call_args.kwargs["allow_same_vendor"])
            issues.main(argv + ["--allow-same-vendor"])
            run.assert_called_with(self.tmp.resolve(), "instances.json", "out.json", "native_codex", 20,
                                   None, ("probe",), ("",), "native_codex", allow_same_vendor=True)

    def test_harness_splits_all_metrics_by_same_vendor(self):
        from evaluations.issues import probe_summary
        rows = [{"same_vendor": flag, "lupus_done": True, "hidden_tests_pass": flag,
                 "disputes": [{}], "probe_counts": {"INVALID": count}, "probe_error": "timeout",
                 "probe_tokens": count * 10, "probe_seconds": count,
                 "crosscheck": {"regressions": count, "fails_both": 1}}
                for flag, count in ((True, 2), (False, 3))]
        rows.append({"same_vendor": True, "lupus_done": False, "hidden_tests_pass": False})
        rows.append({"lupus_done": True, "hidden_tests_pass": True, "crosscheck": {"same_vendor": False}})
        rows.append({"lupus_done": False, "hidden_tests_pass": False})
        split = probe_summary(rows)["by_same_vendor"]
        self.assertEqual((split["true"]["n"], split["false"]["n"], split["unknown"]["n"]), (2, 2, 1))
        for key, count in (("true", 2), ("false", 3)):
            self.assertEqual(split[key]["counts"]["INVALID"], count)
            self.assertEqual(split[key]["invalid_probes"], count)
            self.assertEqual(split[key]["regressions"], count)
            self.assertEqual(split[key]["fails_both"], 1)
            self.assertEqual(split[key]["probe_failures"], 1)
            self.assertEqual(split[key]["probe_tokens_mean"], count * 5)
            self.assertEqual(split[key]["probe_seconds_mean"], count / 2)
        self.assertEqual(split["true"]["done_hidden_pass_with_dispute"], 1)
        self.assertEqual(split["false"]["done_hidden_fail_with_dispute"], 1)

    def test_harness_records_same_vendor_on_rows_that_never_reach_done(self):
        from evaluations import issues
        instances = self.tmp / "instances.json"
        instances.write_text(json.dumps([{"repo": "fixture", "commit": "abcdef123", "request": REQUEST}]))
        output = self.tmp / "out.json"
        with patch.object(issues.Kernel, "init", return_value=self.k), patch.object(self.k, "close"), \
             patch.object(issues.probe, "run") as capability_probe, patch.object(issues, "checkout"), \
             patch.object(issues.adapters, "native", return_value=adapters.FakeAdapter("")), \
             patch.object(issues.projects, "register", return_value=self.project), \
             patch.object(issues.quick, "draft_check", side_effect=issues.LupusError("FIXTURE", "refused")), \
             patch.object(issues, "added_lines", return_value=0), patch.object(issues, "score", return_value=(False, "fixture")):
            for prober, allowed in (("native_codex", True), ("native_claude", False)):
                issues.run(self.tmp, str(instances), str(output), "native_codex", 20,
                           arms=("probe",), prober=prober, allow_same_vendor=allowed)
                row = json.loads(output.read_text())["rows"][0]
                self.assertIs(row["same_vendor"], allowed)
                self.assertFalse(row["lupus_done"])
                self.assertNotIn("crosscheck", row)
                capability_probe.assert_called_with(self.k, live=True,
                    drivers=("native_codex",) if allowed else ("native_codex", "native_claude"))

    def test_capability_probe_only_invokes_selected_vendor(self):
        with patch.object(probe.shutil, "which", return_value="fixture"), \
             patch.object(probe, "_run", return_value=(0, "ChatGPT", "")) as call, \
             patch.object(probe, "_live") as live:
            report = probe.run(live=True, drivers=("native_codex",))
        self.assertEqual(set(report["adapters"]), {"native_codex"})
        self.assertTrue(all(c.args[0][0] == "codex" for c in call.call_args_list))
        self.assertEqual(live.call_count, 1)
        self.assertEqual(live.call_args.args[2], "native_codex")

    def test_private_temp_environment_reaches_author_process(self):
        work = self.tmp / "temp-env-author"
        work.mkdir()
        private = work / ".lupus-author-tmp"
        private.mkdir()
        author = adapters.FakeAdapter("import os, json; print(json.dumps([os.environ[k] for k in ('TMPDIR', 'TMP', 'TEMP')]))")
        author.private_tmp = private
        result = adapters.execute(author, "", work, lambda pid: None, 10)
        self.assertEqual(json.loads(result.text), [str(private)] * 3)

    def test_cli_do_crosscheck_option_is_explicit_and_defaults_off(self):
        from lupus import cli
        with patch.object(cli, "_dispatch", return_value=0) as dispatch:
            self.assertEqual(cli.main(["do", REQUEST, "--driver", "codex"]), 0)
            self.assertIsNone(dispatch.call_args.args[0].cross_check)
            self.assertEqual(cli.main(["do", REQUEST, "--driver", "codex", "--cross-check", "claude"]), 0)
            self.assertEqual(dispatch.call_args.args[0].cross_check, "claude")

    def test_claude_temp_exceptions_follow_denial_and_protected_denies_are_last(self):
        from lupus import util
        from pathlib import Path
        work = Path("/private/var/folders/example/T/author/tree")
        protected = "/private/tmp/protected-tree"
        with patch.object(util.os, "getuid", return_value=123):
            policy = util.sandbox_profile_worker(work, "claude", (protected,), shared_tmp=False)
        lines = policy.splitlines()
        deny = next(i for i, line in enumerate(lines) if line.startswith("(deny file-read* ") and "/private/var/folders" in line)
        read = next(i for i, line in enumerate(lines) if line.startswith("(allow file-read* "))
        write = next(i for i, line in enumerate(lines) if line.startswith("(allow file-write* "))
        self.assertLess(deny, read)
        self.assertLess(deny, write)
        for i in (read, write):
            self.assertIn(str(work), lines[i])
            self.assertIn('"/tmp/claude-123"', lines[i])
            self.assertIn('"/private/tmp/claude-123"', lines[i])
            self.assertNotIn('(subpath "/private/tmp")', lines[i])
            self.assertNotIn('(subpath "/private/var/folders")', lines[i])
        self.assertGreater(next(i for i, line in enumerate(lines) if protected in line), read)

    def test_author_temp_roots_allow_metadata_without_data_access(self):
        from lupus import util
        metadata = '(allow file-read-metadata (literal "/tmp") (literal "/private/tmp") (literal "/private"))'
        policy = util.sandbox_profile_worker(self.root, "claude", shared_tmp=False)
        self.assertIn(metadata, policy.splitlines())
        for line in policy.splitlines():
            if line.startswith(("(allow file-read* ", "(allow file-read-data ")):
                for root in ("/tmp", "/private/tmp", "/private"):
                    self.assertNotIn(f'(literal "{root}")', line)
                    self.assertNotIn(f'(subpath "{root}")', line)
        self.assertNotIn(metadata, util.sandbox_profile_worker(self.root, "claude").splitlines())

    def test_codex_author_keeps_worker_installation_grants_without_shared_temp(self):
        cli = "/example-home/.local/bin/codex"
        real = "/example-home/packages/codex/bin/codex"
        with patch.object(adapters.shutil, "which", return_value=cli), patch.object(adapters.os.path, "realpath", return_value=real):
            worker = adapters.codex_filesystem_profile()
            author = adapters.codex_filesystem_profile(shared_tmp=False)
        self.assertEqual(author, {k: v for k, v in worker.items() if k != ":tmpdir"})
        self.assertEqual(author["/example-home/.local/bin"], "read")
        self.assertEqual(author["/example-home/packages"], "read")

    def test_failed_author_stderr_is_inert_bounded_and_recorded_without_state_changes(self):
        self.adapter(extra='import sys; sys.stderr.write("x" * 5000 + "EEXIST\\x1b[2J\\rhidden\\u202e"); sys.exit(1)')
        before = self.state()
        out = self.run_probe()
        self.assertEqual(out["error"], "CROSSCHECK_MODEL_FAILED")
        self.assertEqual(out["error_detail"], "crash")
        self.assertEqual(out["author_exit_code"], 1)
        tail = out["author_stderr_tail"]
        self.assertLessEqual(len(tail), 2000)
        self.assertIn("EEXIST<U+001B>[2J<U+000D>hidden<U+202E>", tail)
        self.assertNotIn("\x1b", tail)
        self.assertEqual(before, self.state())
        event = json.loads(self.k.one("SELECT payload FROM event WHERE type = 'crosscheck.recorded' AND aggregate_id = ?", self.gid)[0])
        self.assertEqual(event["author_stderr_tail"], tail)

    def test_diagnostic_screen_precedes_truncation_and_execution_captures_real_tail(self):
        from lupus.util import diagnostic_tail
        secret = "sk-ant-" + "A" * 40
        self.assertIn("withheld", diagnostic_tail(secret + "z" * 5000))
        self.assertIn("withheld", diagnostic_tail("Authorization: Bearer example"))
        work = self.tmp / "stderr-author"
        work.mkdir()
        author = adapters.FakeAdapter('import sys; sys.stderr.write("x" * 2000 + "last startup failure"); sys.exit(1)')
        with patch.object(adapters, "MAX_OUTPUT_BYTES", 100):
            result = adapters.execute(author, "", work, lambda pid: None, 10)
        self.assertTrue(result.stderr_tail.endswith("last startup failure"))
        self.assertEqual(len(result.stderr_tail), 100)
        author = adapters.FakeAdapter(f'import sys; sys.stderr.write({secret!r}); sys.exit(1)')
        result = adapters.execute(author, "", work, lambda pid: None, 10)
        self.assertIn("withheld", result.stderr_tail)
        author = adapters.FakeAdapter('import sys; sys.stderr.write("startup warning")')
        result = adapters.execute(author, "", work, lambda pid: None, 10)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stderr_tail, "startup warning")

    def test_canary_start_only_uses_production_factory_and_reports_screened_failure(self):
        from evaluations import crosscheck_canary
        secret = "sk-ant-" + "A" * 40
        def execute(adapter, prompt, work, spawned, timeout):
            self.assertTrue(work.is_dir())
            self.assertIn("START-OK", prompt)
            return adapters.AdapterResult(1, error_class="crash", stderr_tail=secret)
        with patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")) as factory, patch.object(adapters, "execute", side_effect=execute), patch.object(crosscheck_canary, "Kernel") as kernel:
            out = crosscheck_canary.start_only("native_claude")
        kernel.assert_not_called()
        factory.assert_called_once()
        self.assertFalse(out["passed"])
        self.assertEqual(out["exit_code"], 1)
        self.assertIn("withheld", out["stderr_tail"])
        self.assertFalse(factory.call_args.args[1].exists())
        with patch.object(service, "author_adapter", return_value=adapters.FakeAdapter("")), patch.object(adapters, "execute", return_value=adapters.AdapterResult(1, error_class="crash", stderr_tail="sandbox-exec: execvp failed")):
            full = crosscheck_canary.run(self.k, self.gid, "native_codex")
        self.assertFalse(full["passed"])
        self.assertEqual(full["stderr_tail"], "sandbox-exec: execvp failed")
