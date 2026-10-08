"""Independent behaviour cross-checks; observations never change completion verdicts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import budget, contract, goals, release, review, runners, runs, service, timing, verify
from .kernel import Kernel
from .util import LupusError, diagnostic_tail, find_secret, find_secret_bytes


def manifest(root: Path) -> dict[str, str]:
    """Relative file names and SHA-256 content hashes, recorded before the author starts."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


CROSSCHECK_DIR = "_lupus_crosscheck_tests"
MAX_CROSSCHECK_FILES, MAX_CROSSCHECK_BYTES, MAX_CROSSCHECK_TOTAL = 10, 64 * 1024, 256 * 1024
MAX_FAILURE = 2000
CLASSIFICATIONS = ("BOTH_PASS", "AGREES", "DISPUTE", "INVALID")


@timing.measured("crosscheck_snapshot")
def capture_base(k: Kernel, draft_id: str, root: Path) -> None:
    """Before the drafting worker exists, retain the screened repository, without its new test."""
    try:
        saved = k.runtime / "base" / (draft_id + "-crosscheck")
        _copy_files(root, saved)
        saved_manifest = manifest(saved)
        error = None
    except Exception as exc:
        error = type(exc).__name__
    with k.tx():
        k.emit("supervisor", "do.crosscheck_base", "goal", draft_id, unavailable=error, manifest=saved_manifest if error is None else None)


def _copy_files(source: Path, dest: Path, omit: str | None = None) -> None:
    dest.mkdir(mode=0o700, parents=True)
    files, _, _ = review._every_file(source)
    total = 0
    for rel in files:
        parts = Path(rel).parts
        if parts[0] == ".git" or rel == omit or any(p in review.PRIVATE_DIRS for p in parts) or review.PRIVATE_NAME.match(parts[-1]):
            continue
        original = source / rel
        total += original.stat().st_size
        if total > review.MAX_BACKUP_BYTES:
            raise LupusError("CROSSCHECK_TOO_LARGE", "repository copy exceeds size limit")
        data = original.read_bytes()
        if find_secret_bytes(data):
            continue
        target = dest / rel
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(original.stat().st_mode & 0o777)


def take_tests(work: Path) -> dict[str, bytes]:
    """Take only new, regular Python tests by content; never follow an authored link."""
    folder, kept, total = work / CROSSCHECK_DIR, {}, 0
    if folder.is_symlink() or not folder.is_dir():
        return kept
    for current, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if (path.is_symlink() or not path.is_file() or name in runners.CONFIG["python"]
                    or not name.startswith("test_") or not name.endswith(".py")):
                continue
            if path.stat().st_size > MAX_CROSSCHECK_BYTES:
                raise LupusError("CROSSCHECK_TOO_LARGE", "test file exceeds size limit")
            data = path.read_bytes()
            if find_secret_bytes(data):
                continue
            total += len(data)
            if len(kept) >= MAX_CROSSCHECK_FILES or total > MAX_CROSSCHECK_TOTAL:
                raise LupusError("CROSSCHECK_TOO_LARGE", "test count or total size exceeds limit")
            kept[path.relative_to(folder).as_posix()] = data
    return kept


def _descriptions(reply: str, request: str, tests: dict[str, bytes]) -> list[dict]:
    found = None
    for match in re.finditer("\\[", reply):
        try:
            value, end = json.JSONDecoder().raw_decode(reply[match.start():])
        except ValueError:
            continue
        if isinstance(value, list) and not reply[match.start() + end:].strip():
            found = value
            break
    if found is None:
        return []
    # The same cleaner, secret check and verbatim request-quote check as the approval screen.
    packet = contract.parse(json.dumps({"assertions": found}), request,
                            "\n".join(data.decode("utf-8", "replace") for data in tests.values()))
    if packet is None:
        return []
    entries = {}
    for entry in packet["assertions"]:
        if entry["test"] in entries:
            entries[entry["test"]]["test_in_file"] = False      # contradictory declarations cannot identify one reading
        else:
            entries[entry["test"]] = entry
    return list(entries.values())


# This runner is supervisor-owned and installed only after the model has exited. Structured
# outcomes distinguish assertion failures from import/collection/fixture errors, not log wording.
_RUNNER = r"""
import json, sys, traceback, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
rows = []
def note(name, outcome, detail="", collection=False):
    rows.append({"test": name, "outcome": outcome, "detail": detail[-4000:], "collection": collection})
if sys.argv[1] == "unittest":
    class Result(unittest.TestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            note(test.id(), "PASS")
        def addFailure(self, test, err):
            super().addFailure(test, err)
            note(test.id(), "ASSERTION", "".join(traceback.format_exception(*err)))
        def addError(self, test, err):
            super().addError(test, err)
            note(test.id(), "INVALID", "".join(traceback.format_exception(*err)),
                 isinstance(test, unittest.loader._FailedTest))
        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            note(test.id(), "INVALID", "skipped: " + reason)
        def addExpectedFailure(self, test, err):
            note(test.id(), "INVALID", "expected failure")
        def addUnexpectedSuccess(self, test):
            note(test.id(), "INVALID", "unexpected success")
        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err:
                note(test.id(), "ASSERTION" if issubclass(err[0], test.failureException) else "INVALID",
                     "".join(traceback.format_exception(*err)))
    suite = unittest.TestSuite()
    for file in sorted(Path("_lupus_crosscheck_tests").rglob("test_*.py")):
        module = ".".join(file.with_suffix("").parts)
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
        except Exception:
            note(module, "INVALID", traceback.format_exc(), True)
    suite.run(Result())
else:
    import pytest
    class Plugin:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = (yield).get_result()
            if report.failed:
                assertion = (call.when == "call" and call.excinfo is not None
                             and call.excinfo.errisinstance((AssertionError, pytest.fail.Exception)))
                note(item.nodeid, "ASSERTION" if assertion else "INVALID", str(report.longrepr))
            elif report.skipped:
                note(item.nodeid, "INVALID", str(report.longrepr))
            elif call.when == "call":
                note(item.nodeid, "PASS")
        def pytest_collectreport(self, report):
            if report.failed:
                note(report.nodeid, "INVALID", str(report.longrepr), True)
    pytest.main(["-q", "-p", "no:cacheprovider", "_lupus_crosscheck_tests"], plugins=[Plugin()])
Path("_lupus_crosscheck_results.json").write_text(json.dumps(rows))
"""


def _observations(k: Kernel, goal: dict, source: Path, tests: dict[str, bytes], verifier: dict,
                  runner: str, omit: str | None) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="lupus-crosscheck-run-") as tmp:
        root = Path(os.path.realpath(tmp)) / "tree"
        _copy_files(source, root, omit)
        if (root / CROSSCHECK_DIR).exists() or (root / "_lupus_crosscheck_runner.py").exists():
            raise LupusError("CROSSCHECK_DIRECTORY_EXISTS", "reserved crosscheck directory or runner exists")
        for rel, data in tests.items():
            path = root / CROSSCHECK_DIR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / "_lupus_crosscheck_runner.py").write_text(_RUNNER)
        command = {"kind": "command", "argv": [verifier["argv"][0], "_lupus_crosscheck_runner.py", runner],
                   "timeout_s": 120, "env": verifier.get("env", {})}
        # No sandbox opt-out is inherited: model-authored tests always run as verifiers.
        code, output = verify._run_gated(command, root, runs.aux_recorder(k, goal["project_id"], "crosscheck-tests"),
                                         lambda pid: runs.clear_aux(k, pid))
        path = root / "_lupus_crosscheck_results.json"
        if code != 0 or path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise LupusError("CROSSCHECK_RUN_FAILED", "test runner did not produce bounded results")
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or len(rows) > 200:
            raise LupusError("CROSSCHECK_RUN_FAILED", "unreadable test results")
        return rows


def _outcome(rows: list[dict], name: str) -> tuple[str, str]:
    matching = [r for r in rows if isinstance(r, dict) and not r.get("collection") and (
        str(r.get("test", "")).split("::")[-1].split(".")[-1] == name)]
    if not matching:
        detail = next((str(r.get("detail", "")) for r in rows if isinstance(r, dict) and r.get("outcome") == "INVALID"),
                      "test was not collected under its declared name")
        return "INVALID", detail
    # Duplicate leaf names cannot identify a declared input unambiguously. Subtests may report
    # several outcomes for one id; an error wins over any assertion or success for that test.
    if len({r["test"] for r in matching}) != 1:
        return "INVALID", "ambiguous test name"
    for outcome in ("INVALID", "ASSERTION", "PASS"):
        for row in matching:
            if row.get("outcome") == outcome:
                return outcome, str(row.get("detail", ""))
    return "INVALID", "unknown test outcome"


def classify(pristine: str, finished: str) -> str:
    if "INVALID" in (pristine, finished):
        return "INVALID"
    if finished == "ASSERTION":
        return "DISPUTE"
    if pristine == "ASSERTION" and finished == "PASS":
        return "AGREES"
    if pristine == finished == "PASS":
        return "BOTH_PASS"
    return "INVALID"


def _inert(text: str) -> str:
    if find_secret(text):
        return "[failure text withheld: credential pattern]"
    return release._seen(text).replace("\n", " ").replace("\t", " ")[-MAX_FAILURE:]


# A distinct measured capability: the general probe's home-directory canary does
# not measure protection of finished implementations or snapshots in temp trees.
AUTHOR_CONFINEMENT_MODE = "crosscheck_author_v2"
AUTHOR_CONFINEMENT_NAME = "author_read_confinement"


def author_cli_identity(driver: str) -> str:
    """Bind measurements to the installed executable without invoking the CLI."""
    from . import adapters
    name = {"native_codex": "codex", "native_claude": "claude"}.get(driver)
    if name is None:
        return "unavailable"
    digest = hashlib.sha256()
    try:
        with open(adapters.resolve_cli(name), "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return "sha256:" + digest.hexdigest()


def author_confinement(k: Kernel, driver: str) -> str:
    cap = k.one("SELECT status, cli_version FROM capability WHERE adapter = ? AND name = ? AND mode = ?",
                driver, AUTHOR_CONFINEMENT_NAME, AUTHOR_CONFINEMENT_MODE)
    if cap and cap["status"] == "verified":
        identity = author_cli_identity(driver)
        if identity != "unavailable" and cap["cli_version"] == identity:
            return "verified by canary"
    return "unverified"


def shadow(k: Kernel, goal_id: str, driver: str, timeout_s: float = 300) -> dict:
    """One authoring call and two sandbox runs; only spend and an event are added."""
    with k.supervisor_lock():
        return _shadow(k, goal_id, driver, timeout_s)


def _shadow(k: Kernel, goal_id: str, driver: str, timeout_s: float) -> dict:
    goal = goals.get(k, goal_id)
    if goal["status"] != "DONE":
        raise LupusError("CROSSCHECK_NOT_READY", "only a DONE do goal can be cross-checked")
    row = k.one("SELECT payload FROM event WHERE type = 'check.approved' AND aggregate_id = ?", goal_id)
    if row is None:
        raise LupusError("CROSSCHECK_NOT_READY", "not an implementation goal made by do")
    approved = json.loads(row["payload"])
    draft_id = approved["draft_goal"]
    red = goals.criteria(k, draft_id)[0]["verifier"]
    worker_drivers = [r[0] for r in k.q(
        "SELECT DISTINCT execution_driver FROM run WHERE goal_id IN (?, ?) ORDER BY execution_driver",
        goal_id, draft_id)]
    record = {"driver": driver, "worker_drivers": worker_drivers, "same_vendor": driver in worker_drivers, "author_confinement": author_confinement(k, driver), "tests": [], "disputes": [], "counts": dict.fromkeys(CLASSIFICATIONS, 0),
              "tokens": 0, "seconds": 0.0, "model_seconds": 0.0, "usage_observed": False, "regressions": 0, "fails_both": 0}
    started, hold, result = time.monotonic(), None, None
    calls_before = k.one("SELECT COUNT(*) FROM service_call WHERE goal_id = ?", goal_id)[0]
    try:
        root = goals.project_root(k, goal_id)
        if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", goal["project_id"]) or runs.aux_alive(k, goal["project_id"]):
            raise LupusError("CROSSCHECK_WRITER_NOT_STOPPED", "project is busy")
        base_row = k.one("SELECT seq, payload FROM event WHERE type = 'do.crosscheck_base' AND aggregate_id = ?", draft_id)
        base = k.runtime / "base" / (draft_id + "-crosscheck")
        if base_row is None or json.loads(base_row["payload"]).get("unavailable") or not base.is_dir():
            raise LupusError("CROSSCHECK_BASE_UNAVAILABLE", "no pristine snapshot from before drafting")
        runner = red.get("require_tests")
        if runner not in ("unittest", "pytest"):
            raise LupusError("CROSSCHECK_RUNNER_UNSUPPORTED", "behaviour crosschecks currently require Python tests")
        hold = budget.reserve(k, goal["budget_id"], "work", "crosscheck",
                              {"calls": 1, "active_ms": int((timeout_s + 240) * 1000)})
        with tempfile.TemporaryDirectory(prefix="lupus-crosscheck-author-") as tmp:
            work = Path(os.path.realpath(tmp)) / "tree"
            _copy_files(base, work, approved["test"])
            if (work / CROSSCHECK_DIR).exists():
                raise LupusError("CROSSCHECK_DIRECTORY_EXISTS", "reserved crosscheck directory exists")
            (work / CROSSCHECK_DIR).mkdir()
            record["author_input"] = {
                "draft_goal": draft_id, "snapshot_event_seq": base_row["seq"],
                "snapshot_manifest": json.loads(base_row["payload"]).get("manifest"),
                "workspace_manifest": manifest(work), "omitted_worker_test": approved["test"],
                "worker_test_absent": not (work / approved["test"]).exists(),
                "source": "pre_draft_snapshot", "request_sha256": hashlib.sha256(red["contract_for"].encode()).hexdigest(),
            }
            expected_manifest = {name: digest for name, digest in record["author_input"]["snapshot_manifest"].items()
                                 if name != approved["test"]}
            record["author_input"]["snapshot_verified"] = expected_manifest == record["author_input"]["workspace_manifest"]
            if not record["author_input"]["snapshot_verified"]:
                raise LupusError("CROSSCHECK_BASE_CHANGED", "snapshot contents differ from the pre-draft record")
            prompt = (
                "Write behavioural tests from the request and this pristine repository only. "
                "Do not implement the request. Write new test_*.py files ONLY in " + CROSSCHECK_DIR + ". "
                f"Use {runner}; at most 20 tests and 10 files. Each declared name must be a unique test function name. "
                "Do not write runner configuration. End your reply with a JSON list, one object per test: "
                "{\"test\":\"test_name\", \"input\":\"concrete input\", \"expected\":\"expected result\", "
                "\"rule\":\"behaviour\", \"source\":\"explicit_request | existing_contract | worker_choice\", "
                "\"quote\":\"verbatim request quote\", \"where\":\"repository location\", "
                "\"choice\":\"your choice\", \"alternative\":\"one alternative\", \"differs_on\":\"input\"}. "
                "Use explicit_request only for words actually in the request; use existing_contract for a repository "
                "location; otherwise disclose your choice and one alternative.\nRequest: " + red["contract_for"])
            call_started = time.monotonic()
            try:
                result = service.call(k, project_id=goal["project_id"], goal_id=goal_id, budget_id=goal["budget_id"],
                                      purpose="crosscheck", driver=driver, prompt=prompt, timeout_s=timeout_s, workspace=work, author_exclude=(str(k.home), str(base.parent),))
            finally:
                record["model_seconds"] = time.monotonic() - call_started
            if result.usage:
                record["tokens"] = sum(result.usage.get(key, 0) for key in ("tokens_in", "tokens_cached", "tokens_out"))
            record["usage_observed"] = result.usage is not None
            if result.error_class:
                record["author_exit_code"] = result.exit_code
                record["author_stderr_tail"] = diagnostic_tail(result.stderr_tail)
                raise LupusError("CROSSCHECK_MODEL_FAILED", result.error_class)
            tests = take_tests(work)
            descriptions = _descriptions(result.text, red["contract_for"], tests)
        if not tests or not descriptions:
            raise LupusError("CROSSCHECK_NOTHING_PRODUCED", "no screened tests with readable declarations")
        try:
            count = sum(runners.count_tests(runner, data.decode("utf-8")) for data in tests.values())
        except (SyntaxError, UnicodeDecodeError):
            count = 0      # the runner records malformed files as INVALID
        if count > contract.MAX_ASSERTIONS:
            raise LupusError("CROSSCHECK_TOO_LARGE", "more than 20 tests")
        before = _observations(k, goal, base, tests, red, runner, approved["test"])
        after = _observations(k, goal, root, tests, red, runner, approved["test"])
        named = {entry["test"] for entry in descriptions}
        for row in before + after:
            if not isinstance(row, dict):
                continue
            name = str(row.get("test", "")).split("::")[-1].split(".")[-1]
            if name.startswith("test_") and not row.get("collection") and name not in named:
                named.add(name)
                descriptions.append({"test": contract._text(name), "input": "", "expected": "",
                                     "rule": "", "source": "worker_choice", "quote": "", "where": "",
                                     "choice": "", "alternative": "", "differs_on": "",
                                     "quote_in_request": False, "test_in_file": False})
        for entry in descriptions:
            old, _ = _outcome(before, entry["test"])
            new, detail = _outcome(after, entry["test"])
            described = (entry["test_in_file"] and entry["input"] and entry["expected"]
                         and (entry["quote"] if entry["source"] == "explicit_request" else
                              entry["where"] if entry["source"] == "existing_contract" else
                              entry["choice"] and entry["alternative"]))
            classification = classify(old, new) if described else "INVALID"
            item = {**entry, "classification": classification, "pristine": old, "finished": new,
                    "observed": _inert(detail)}
            record["tests"].append(item)
            record["counts"][classification] += 1
            if classification == "DISPUTE":
                record["regressions" if old == "PASS" else "fails_both"] += 1
                record["disputes"].append(item)
    except Exception as exc:
        record["error"] = _inert(exc.code if isinstance(exc, LupusError) else type(exc).__name__)
        record["error_detail"] = _inert(exc.detail if isinstance(exc, LupusError) else str(exc))
    finally:
        record["seconds"] = time.monotonic() - started
        if hold:
            calls = k.one("SELECT COUNT(*) FROM service_call WHERE goal_id = ?", goal_id)[0] - calls_before
            budget.settle(k, hold, {"calls": calls,
                                   "active_ms": int(record["seconds"] * 1000)}, "measured")
    with k.tx():
        k.emit("supervisor", "crosscheck.recorded", "goal", goal_id, **record)
    return record


def render(record: dict, limit: int = 5) -> str:
    """Several checks of one rule appear once; crosscheck errors are observations, never verdicts."""
    prefix = "Author confinement: " + record.get("author_confinement", "unverified") + "\n"
    if record.get("same_vendor"):
        prefix += "Same vendor: true (author and worker; not cross-vendor results)\n"
    elif record.get("worker_drivers"):
        prefix += "Same vendor: false (author and worker)\n"
    else:
        prefix += "Same vendor: false (worker vendor unavailable; cross-vendor comparison not established)\n"
    lines, seen = [], set()
    for entry in record.get("disputes", []):
        finding = "regression (passed pristine)" if entry.get("pristine") == "PASS" else "fails both pristine and implementation"
        key = (finding, entry.get("rule") or (entry.get("test"), entry.get("input"), entry.get("expected")))
        if key in seen:
            continue
        seen.add(key)
        if entry["source"] == "explicit_request":
            provenance = "request: \"" + entry["quote"] + "\"; quote check: " + str(entry["quote_in_request"])
        elif entry["source"] == "existing_contract":
            provenance = "repository: " + entry["where"]
        else:
            provenance = "author choice: " + entry["choice"] + "; alternative: " + entry["alternative"]
        lines.append(f"{finding}: on {contract._text(entry["input"])} the implementation gives {_inert(entry["observed"])}; "
                     f"the request could also be read as {contract._text(entry["expected"])} ({_inert(provenance)})")
        if len(lines) >= limit:
            break
    if lines:
        return prefix + "\n".join(lines)
    return prefix + ("Cross-check unavailable: " + _inert(record["error"]) if record.get("error") else "No disputes observed.")
