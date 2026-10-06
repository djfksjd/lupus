"""Test runners Lupus can read: which command runs a project's tests, which files decide what
gets tested (and are therefore frozen), and how to read the runner's output.

Everything here is decided from files and from the runner's own summary lines. No model is
asked, and nothing is guessed: a project whose runner is not recognised is refused, and the user
can name the command themselves (`--check`), in which case only its exit status is judged.

  python   unittest, pytest
  node     node:test (built in), jest, vitest
  go       go test
  rust     cargo test
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .util import LupusError, safe_path, scrubbed_env

SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "target", "dist",
        "build", "vendor", ".next"}
MAX_SCAN = 4000

# Files that decide which tests run or how. Frozen when present; for a `do` request they may not
# be created either (an added config could deselect the approved test).
CONFIG = {
    "python": ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "sitecustomize.py",
               "usercustomize.py"),
    "node": ("package.json", "jest.config.js", "jest.config.cjs", "jest.config.mjs", "jest.config.ts", "jest.config.json",
             "vitest.config.js", "vitest.config.mjs", "vitest.config.ts", "vite.config.js", "vite.config.mjs",
             "vite.config.ts", ".mocharc.json", ".mocharc.js", ".mocharc.cjs", ".mocharc.yml", "tsconfig.json", ".npmrc"),
    "go": ("go.mod", "go.sum", "go.work"),
    "rust": ("Cargo.toml", "Cargo.lock", "build.rs", "rust-toolchain", "rust-toolchain.toml"),
}
LANGUAGE = {"unittest": "python", "pytest": "python", "node": "node", "jest": "node", "vitest": "node", "go": "go",
            "cargo": "rust"}
SOURCE_EXT = {"python": (".py",), "node": (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"), "go": (".go",), "rust": (".rs",)}

_RUST_TEST_ATTR = re.compile(r"#\[\s*(?:\w+::)*test\b[^\]]*\]")
_NODE_TEST = re.compile(r"(?:\.(?:test|spec)\.[cm]?[jt]sx?|[-_]test\.[cm]?[jt]s)$")


def is_test_file(language: str, rel: str) -> bool:
    name, parts = os.path.basename(rel), Path(rel).parts[:-1]
    if language == "python":
        return name.endswith(".py") and (name.startswith("test") or name.endswith("_test.py") or name == "conftest.py"
                                         or any(p in ("tests", "test") for p in parts))
    if language == "node":
        return bool(_NODE_TEST.search(name)) or (name.endswith(SOURCE_EXT["node"]) and any(
            p in ("test", "tests", "__tests__") for p in parts))
    if language == "go":
        return name.endswith("_test.go")
    if language == "rust":
        return name.endswith(".rs") and bool(parts) and parts[0] == "tests"
    return False


def _walk(root: Path, exts: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(exts):
                out.append(os.path.relpath(os.path.join(current, name), root))
                if len(out) >= MAX_SCAN:
                    return out
    return out


def _tool(name: str, root: Path, host: bool = True) -> str:
    """Absolute path of an interpreter/toolchain binary on the sanitized PATH, never one inside
    the project (a file called `node` or `go` in the project must not become the test runner).
    When the tests will run in a container (`host=False`) the program's plain name is returned:
    what is installed on this machine does not matter then."""
    if not host:
        return name
    real_root = os.path.realpath(root)
    found = shutil.which(name, path=safe_path())
    if found is None or os.path.realpath(found).startswith(real_root + os.sep):
        raise LupusError("TEST_RUNNER_UNAVAILABLE", f"`{name}` was not found on PATH")
    return found


def _language(root: Path) -> str | None:
    """Which language's tests live here. A marker file is not enough on its own (a Python project
    can have a package.json for tooling): the language with test files wins, Python first."""
    counts = {lang: sum(is_test_file(lang, rel) for rel in _walk(root, SOURCE_EXT[lang])) for lang in SOURCE_EXT}
    marked = {"node": (root / "package.json").is_file(), "go": (root / "go.mod").is_file(),
              "rust": (root / "Cargo.toml").is_file(), "python": True}
    for lang in ("python", "node", "go", "rust"):
        if counts[lang] and marked[lang]:
            return lang
    for lang in ("node", "go", "rust"):     # no tests yet: the project's own marker decides
        if marked[lang]:
            return lang
    return "python" if _walk(root, (".py",)) else None


_PYTEST_STYLE = re.compile(r"^(?:def test_\w*\(|import pytest\b|from pytest\b|@pytest\.)", re.M)


def project_python(root: Path) -> tuple[str, str] | None:
    """(interpreter, environment folder) of the project's own virtual environment, if it has one.
    The project's tests are meant to run with the project's dependencies, not with whatever
    Python happens to run Lupus. The folder is fingerprinted like a dependency tree, so a worker
    cannot swap the interpreter or a package for something that prints a pass."""
    for name in (".venv", "venv", "env"):
        python = root / name / "bin" / "python"
        if (root / name / "pyvenv.cfg").is_file() and python.exists() and os.access(python, os.X_OK):
            return str(python), name
    return None


def _python(root: Path, host: bool = True, python_from: Path | None = None) -> dict:
    test_dirs = [d for d in ("tests", "test") if (root / d).is_dir()]
    loose = sorted(p.name for p in root.glob("test_*.py")) + sorted(p.name for p in root.glob("*_test.py"))
    candidates = loose + [str(p.relative_to(root)) for d in test_dirs for p in sorted((root / d).rglob("*.py"))][:200]
    uses_pytest = any((root / f).is_file() for f in ("pytest.ini", "conftest.py")) or (
        (root / "pyproject.toml").is_file() and "[tool.pytest" in (root / "pyproject.toml").read_text(errors="ignore")) or any(
        # no configuration, but the tests are written the pytest way (plain functions, fixtures):
        # unittest would import such a file and run nothing
        _PYTEST_STYLE.search((root / rel).read_text(errors="ignore")) for rel in candidates)
    own = project_python(root) if host else None
    # An isolated checkout has no environment of its own: it uses the one of the repository it was
    # made from (outside the checkout, so nothing a worker there can write to).
    borrowed = project_python(python_from) if host and own is None and python_from is not None else None
    python = own[0] if own else (borrowed[0] if borrowed else sys.executable)
    if uses_pytest and not host:
        argv, runner = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"], "pytest"
    elif uses_pytest:
        # Is pytest there? Asked only of Lupus's own interpreter, outside the project and with the
        # scrubbed environment. An interpreter that belongs to the project is never run here: it
        # runs only as a sandboxed check, where a missing pytest shows up as that check failing.
        probe = None if own or borrowed else subprocess.run(
            [python, "-m", "pytest", "--version"], capture_output=True, cwd=tempfile.gettempdir(), env=scrubbed_env(),
            stdin=subprocess.DEVNULL)
        if probe is not None and probe.returncode != 0:
            raise LupusError("TEST_RUNNER_UNAVAILABLE", "the project is configured for pytest but pytest is not installed")
        argv, runner = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "pytest"
    else:
        argv, runner = [python if host else "python3", "-m", "unittest", "discover", "-q"], "unittest"
    tests = loose + [str(p.relative_to(root)) for d in test_dirs for p in sorted((root / d).rglob("test*.py"))]
    # "src layout": the package lives in src/ and is not importable from the project root unless
    # it is installed. The tests are run against the source in front of us, not an installed copy.
    src = (root / "src").is_dir() and any(p.suffix == ".py" or (p / "__init__.py").is_file() for p in (root / "src").iterdir())
    bases = ["src", ""] if src else [""]
    related: list[str] = []
    for rel in tests[:8]:
        for module in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", (root / rel).read_text(errors="ignore"), re.M):
            for base in bases:
                single, package = os.path.join(base, f"{module}.py"), root / base / module
                found = [single] if (root / single).is_file() else (
                    sorted(str(p.relative_to(root)) for p in package.glob("*.py"))[:6] if (package / "__init__.py").is_file() else [])
                related += [f for f in found if f not in related and not is_test_file("python", f)]
    return {"runner": runner, "argv": argv, "protect": test_dirs + loose, "tests": tests, "related": related,
            "test_dir": test_dirs[0] if test_dirs else "", **({"env": {"PYTHONPATH": "src"}} if src else {}),
            **({"frozen_trees": [own[1]]} if own else {})}


_SHELL = re.compile(r"[|&;<>$`(){}*?~]|^\w+=")


def _node_argv(node: str, script: str) -> tuple[str, str, list[str]]:
    """(runner, entry file inside node_modules or "", argv) for a package.json test script. The
    script's own options are kept (a custom --config decides what the tests are); a script that is
    more than one plain command is not something to guess at."""
    try:
        words = shlex.split(script)
    except ValueError:
        words = ["?"]
    if not script or "no test specified" in script:
        return "node", "", [node, "--test", "--test-reporter=tap"]
    if words[0] in ("npx", "pnpm", "yarn") and len(words) > 1 and words[1] in ("jest", "vitest"):
        words = words[1:]
    if any(_SHELL.search(w) for w in words):
        words = ["?"]
    if words[0] == "vitest":
        rest = [w for w in words[1:] if w not in ("run", "watch", "dev", "--watch", "-w")]
        return "vitest", "node_modules/vitest/vitest.mjs", [node, "node_modules/vitest/vitest.mjs", "run", *rest]
    if words[0] == "jest":
        rest = [w for w in words[1:] if w not in ("--watch", "--watchAll", "--ci")]
        return "jest", "node_modules/jest/bin/jest.js", [node, "node_modules/jest/bin/jest.js", "--ci", *rest]
    if words[0] == "node" and "--test" in words:
        rest = [w for w in words[1:] if w != "--test" and not w.startswith("--test-reporter") and w != "--watch"]
        return "node", "", [node, "--test", "--test-reporter=tap", *rest]      # options before any file argument
    raise LupusError("TEST_RUNNER_UNKNOWN", f"package.json runs tests with {script[:80]!r}, which Lupus will not guess at; "
                                            "name the command yourself: --check \"npm test\"")


def _node(root: Path, host: bool = True) -> dict:
    node = _tool("node", root, host)
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    script = str((package.get("scripts") or {}).get("test", "")) if isinstance(package, dict) else ""
    tests = [rel for rel in _walk(root, SOURCE_EXT["node"]) if is_test_file("node", rel)]
    runner, entry, argv = _node_argv(node, script)
    if entry and not (root / entry).is_file():
        raise LupusError("TEST_RUNNER_UNAVAILABLE", f"{runner} is not installed in this project ({entry})")
    # Whatever the script's options point at (a --config file, a setup file, a directory) decides
    # which tests run just as much as the well-known config names do: it is frozen with them.
    referenced: list[str] = []
    real_root = os.path.realpath(root)
    for word in argv[1:]:
        for candidate in {word, word.split("=", 1)[-1]}:
            target = os.path.realpath(os.path.join(root, candidate))
            if not candidate.startswith("-") and target.startswith(real_root + os.sep) and os.path.exists(target) \
                    and "node_modules" not in Path(candidate).parts:
                referenced.append(os.path.relpath(target, real_root))
    dirs = [d for d in ("test", "tests", "__tests__") if (root / d).is_dir()]
    loose = [t for t in tests if not any(t == d or t.startswith(d + os.sep) for d in dirs)]
    related: list[str] = []
    for rel in tests[:8]:
        for spec in re.findall(r"""(?:from\s+|require\(\s*|import\(\s*)['"](\.{1,2}/[^'"]+)['"]""",
                               (root / rel).read_text(errors="ignore")):
            target = os.path.normpath(os.path.join(os.path.dirname(rel), spec))
            for cand in (target, *[target + e for e in SOURCE_EXT["node"]]):
                if (root / cand).is_file() and not is_test_file("node", cand) and cand not in related:
                    related.append(cand)
                    break
    return {"runner": runner, "argv": argv, "protect": dirs + loose + sorted(set(referenced) - set(dirs)), "tests": tests,
            "related": related, "test_dir": dirs[0] if dirs else "",
            # jest/vitest are programs inside the project's node_modules: a worker that could rewrite
            # them could print any result. The tree is fingerprinted and must not change.
            **({"frozen_trees": ["node_modules"]} if entry else {})}


def _go(root: Path, host: bool = True) -> dict:
    tests = [rel for rel in _walk(root, (".go",)) if is_test_file("go", rel)]
    related = [t[: -len("_test.go")] + ".go" for t in tests[:8] if (root / (t[: -len("_test.go")] + ".go")).is_file()]
    return {"runner": "go", "argv": [_tool("go", root, host), "test", "-v", "-count=1", "./..."], "protect": tests,
            "tests": tests, "related": related, "test_dir": "",
            # the default build cache is in the home directory, which a verifier may not write
            "env": {"GOCACHE": os.path.join(os.path.realpath(tempfile.gettempdir()) if host else "/tmp", "lupus-gocache"),
                    "GOFLAGS": "-mod=mod", "GOPROXY": "off"}}


def _rust(root: Path, host: bool = True) -> dict:
    tests = [rel for rel in _walk(root, (".rs",)) if is_test_file("rust", rel)]
    # --no-fail-fast: every test binary runs, so one run yields both the verdict and the full list of names
    return {"runner": "cargo", "argv": [_tool("cargo", root, host), "test", "--offline", "--no-fail-fast"],
            "protect": ["tests"] if (root / "tests").is_dir() else [], "tests": tests,
            "related": [f for f in ("src/lib.rs", "src/main.rs") if (root / f).is_file()], "test_dir": "tests"}


def detect(root: Path, need_tests: bool = True, host: bool = True, python_from: Path | None = None) -> dict:
    """Pick the test command and what to freeze, from files only (no model, no guessing).
    `host=False`: the tests will run in a container, so programs are named, not looked up here."""
    root = Path(root)
    language = _language(root)
    if language is None:
        raise LupusError("NO_TESTS_FOUND", "no tests and no recognised project file here")
    found = (_python(root, host, python_from) if language == "python"
             else {"node": _node, "go": _go, "rust": _rust}[language](root, host))
    # Rust and Go keep unit tests inside source files; those cannot be frozen as files, so only
    # the test files proper count as "has tests" for freezing purposes.
    has_tests = bool(found["tests"]) or (language == "rust" and (root / "src").is_dir() and any(
        _RUST_TEST_ATTR.search((root / "src" / rel).read_text(errors="ignore")) for rel in _walk(root / "src", (".rs",))[:200]))
    if need_tests and not has_tests:
        raise LupusError("NO_TESTS_FOUND", f"no {language} test files in this project")
    config = [f for f in CONFIG[language] if (root / f).is_file()]
    return {"runner": found["runner"], "language": language, "argv": found["argv"], "env": found.get("env", {}),
            "frozen_trees": found.get("frozen_trees", []),
            "protect": found["protect"] + config, "inputs": found["tests"][:8] + found["related"][:8],
            "has_tests": has_tests, "test_dir": found["test_dir"]}


# ---------------------------------------------------------------- reading a runner's output

# The runner's own closing summary. Anything the code under test prints (Node turns
# console.log("pass 20") into "# pass 20") must not be mistaken for it: for Node only the complete
# closing block at the very end counts. This stops accidents and cheap tricks; code that runs
# inside the test process and sets out to forge the whole summary is not something output parsing
# can stop (see CONTRACT, 범위 밖).
_NODE_SUMMARY = re.compile(r"^# tests (\d+)\n# suites \d+\n# pass (\d+)\n# fail (\d+)\n# cancelled \d+\n# skipped \d+\n"
                           r"# todo \d+\n# duration_ms [\d.]+\s*\Z", re.M)
_UNITTEST_SKIPPED = re.compile(r"^OK \((?:[^)]*\b)?skipped=(\d+)", re.M)
_PASSED = {
    "unittest": re.compile(r"^Ran (\d+) tests?", re.M),
    "pytest": re.compile(r"(\d+) passed"),
    "jest": re.compile(r"^\s*Tests:\s.*?(\d+) passed", re.M),
    "vitest": re.compile(r"^\s*Tests\s.*?(\d+) passed", re.M),
    "cargo": re.compile(r"^test result: \w+\. (\d+) passed", re.M),
}
_FAILED = {
    "unittest": re.compile(r"^Ran ([1-9]\d*) tests?", re.M),
    "pytest": re.compile(r"([1-9]\d*) (?:failed|errors?)"),
    "jest": re.compile(r"^\s*Tests:\s+([1-9]\d*) failed", re.M),
    "vitest": re.compile(r"^\s*Tests\s+([1-9]\d*) failed", re.M),
    "go": re.compile(r"^\s*--- FAIL: "),
    "cargo": re.compile(r"^test result: FAILED\. \d+ passed; ([1-9]\d*) failed", re.M),
}
_GO_PASS = re.compile(r"^\s*--- PASS: ", re.M)
_GO_FAIL = re.compile(r"^\s*--- FAIL: ", re.M)
# The test file could not even be loaded, so no test body ran.
_NOT_LOADED = {
    "unittest": re.compile(r"_FailedTest|Failed to import test module"),
    "pytest": re.compile(r"ERROR collecting|errors? during collection"),
    "jest": re.compile(r"Test suite failed to run"),
    "vitest": re.compile(r"Failed Suites|Failed to load|Error: Cannot find module"),
}
_NODE_MISSING = re.compile(r"does not provide an export named|ERR_MODULE_NOT_FOUND|Cannot find module")
_BROKEN = {
    "python": re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b"),
    "node": re.compile(r"\bSyntaxError\b"),
    "go": re.compile(r"syntax error|expected '[^']+', found|\.go:\d+:\d+: (?:missing|unexpected)"),
    "rust": re.compile(r"error: (?:expected|unexpected|unknown start of token|mismatched closing|this file contains an unclosed)"),
}
# Compiled languages: a test that calls something not written yet cannot be built at all. That is
# an honest "red" only when EVERY compiler error is a missing name reported in the new test file.
_BUILD_FAILED = {"go": re.compile(r"\[build failed\]|^# [\w./-]+ \[", re.M), "rust": re.compile(r"^error(?:\[E\d+\])?: ", re.M)}
_GO_ERROR = re.compile(r"^(\S+\.go):\d+:\d+: (.*)$", re.M)
_GO_MISSING = re.compile(r"undefined: \w|[\w.]+ undefined \(type .* has no field or method")
_RUST_ERROR = re.compile(r"^error(\[E\d+\])?: (.*)\n\s*--> (\S+?):\d+:\d+", re.M)
_RUST_MISSING = ("[E0425]", "[E0432]", "[E0433]", "[E0412]", "[E0599]", "[E0405]", "[E0422]", "[E0423]", "[E0609]")


def _only_missing_names(language: str, test_path: str, output: str) -> bool:
    name = os.path.basename(test_path)
    if language == "go":
        errors = _GO_ERROR.findall(output)
        return bool(errors) and all(os.path.basename(path) == name and _GO_MISSING.match(message) for path, message in errors)
    errors = _RUST_ERROR.findall(output)
    plain = [line for line in re.findall(r"^error(?:\[E\d+\])?: .*$", output, re.M)
             if not re.match(r"error: (?:could not compile|aborting due to)", line)]
    return (bool(errors) and len(errors) == len(plain)
            and all(code in _RUST_MISSING and os.path.basename(path) == name for code, _, path in errors))


def passed(runner: str, output: str) -> int | None:
    """Number of tests the runner reports as passed; None for a runner without a readable count."""
    if runner == "go":
        return len(_GO_PASS.findall(output))
    if runner == "node":
        summary = _NODE_SUMMARY.search(output.rstrip() + "\n")
        return int(summary.group(2)) if summary else 0
    pattern = _PASSED.get(runner)
    if pattern is None:
        return None
    counts = [int(n) for n in pattern.findall(output)]
    if not counts:
        return 0
    if runner == "cargo":
        return sum(counts)
    # A runner prints its summary once. The code under test can add look-alike lines (and output
    # buffering decides where they land) but cannot remove the real one, so the smallest count wins.
    if runner == "unittest":      # "Ran 3 tests … OK (skipped=3)" ran nothing
        skipped = [int(n) for n in _UNITTEST_SKIPPED.findall(output)]
        return max(0, min(counts) - (max(skipped) if skipped else 0))
    return min(counts)


_FAILED_ID = {
    "pytest": re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M),                       # needs -rfE
    "unittest": re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]*)\)", re.M),
}


def failed_ids(runner: str, output: str) -> set[str] | None:
    """Names of the tests (or test files that could not be loaded) a run reports as failed; None
    for a runner whose output does not name them."""
    pattern = _FAILED_ID.get(runner)
    if pattern is None:
        return None
    if runner == "unittest":
        return {(where if "_FailedTest" not in where else name) or name for name, where in pattern.findall(output)}
    return {name.split(" - ")[0] for name in pattern.findall(output)}


def with_failure_names(runner: str, argv: list[str]) -> list[str]:
    return [*argv, "-rfE"] if runner == "pytest" and "-rfE" not in argv else list(argv)


def nothing_ran(runner: str, detail: str) -> bool:
    return (detail == "no tests ran" or "NO TESTS RAN" in detail or "Ran 0 tests" in detail
            or (runner == "pytest" and detail.startswith("exit 5")) or "No tests found" in detail
            or "No test files found" in detail or "[no test files]" in detail and "---" not in detail)


def judge_red(runner: str, test_path: str, output: str) -> str | None:
    """Why this failing run does NOT count as a useful red test; None when it does: at least one
    test of the new file really ran and failed (or, for a compiled language, the build failed only
    because the thing under test does not exist yet)."""
    language = LANGUAGE.get(runner, "")
    load_hint = ("the test file failed to load, so no test body ran. Import what does not exist yet inside the "
                 "test functions, not at the top of the file")
    if language == "node" and _NODE_MISSING.search(output):
        return load_hint        # Node reports a missing export as a SyntaxError; it is a load failure
    broken = _BROKEN.get(language)
    if broken is not None and broken.search(output):
        # Python names the error class in any traceback. When the file loaded and its tests ran and
        # failed, a SyntaxError in the output was raised INSIDE a test (one that compiles generated
        # code, say), which is an ordinary red.
        ran = language == "python" and _FAILED[runner].search(output) and not _NOT_LOADED[runner].search(output)
        if not ran:
            return "the test file does not parse"
    build = _BUILD_FAILED.get(language)
    if build is not None and build.search(output):
        if _only_missing_names(language, test_path, output):
            return None
        return "the test does not build, and not only because the requested code is missing"
    not_loaded = _NOT_LOADED.get(runner)
    if (not_loaded is not None and not_loaded.search(output)) or (
            runner == "node" and re.search(r"^# Subtest: .*" + re.escape(os.path.basename(test_path)) + r"\s*$", output, re.M)):
        return load_hint
    if runner == "node":
        summary = _NODE_SUMMARY.search(output.rstrip() + "\n")
        return None if summary and int(summary.group(3)) > 0 else "no test from the new file ran"
    failed = _FAILED.get(runner)
    if failed is not None and not (_GO_FAIL.search(output) if runner == "go" else failed.search(output)):
        return "no test from the new file ran"
    return None


_CARGO_SECTION = re.compile(r"^\s*Running (unittests )?(\S+)", re.M)
_CARGO_TEST = re.compile(r"^test (\S+) \.\.\. (ok|FAILED)$", re.M)


def cargo_tests(output: str) -> tuple[list[str], list[str]]:
    """(names of tests that passed, names of FAILED tests that live inside source files). Rust unit
    tests sit in the files a worker has to edit, so they cannot be frozen like test files."""
    passed, failed_in_source, in_source = [], [], False
    for line in output.splitlines():
        section = _CARGO_SECTION.match(line)
        if section:
            in_source = bool(section.group(1))
            continue
        test = _CARGO_TEST.match(line)
        if test and test.group(2) == "ok":
            passed.append(test.group(1))
        elif test and in_source:
            failed_in_source.append(test.group(1))
    return passed, failed_in_source


# ---------------------------------------------------------------- `lupus do`: the new test file

def one_file_argv(found: dict, test_path: str) -> list[str]:
    runner, argv = found["runner"], found["argv"]
    if runner == "pytest":
        return [*argv, test_path]
    if runner == "unittest":
        return [argv[0], "-m", "unittest", "-q", test_path]
    if runner in ("node", "jest", "vitest"):
        return [*argv, test_path]
    if runner == "go":
        package = "./" + os.path.dirname(test_path) if os.path.dirname(test_path) else "."
        return [*argv[:-1], "-run", "Lupus", package]
    if runner == "cargo":
        return [*[a for a in argv if a != "--no-fail-fast"], "--test", Path(test_path).stem]
    raise LupusError("TEST_RUNNER_UNKNOWN", runner)


def new_test_path(root: Path, found: dict, digest: str) -> str:
    runner, test_dir = found["runner"], found["test_dir"]
    if LANGUAGE[runner] == "python":
        name = f"test_lupus_{digest}.py"
    elif LANGUAGE[runner] == "node":
        existing = [t for t in found["inputs"] if is_test_file("node", t)]
        ext = os.path.splitext(existing[0])[1] if existing else (".mjs" if runner == "node" else ".js")
        name = f"lupus_{digest}.test{ext}"
        test_dir = test_dir or ("" if existing else "test")
    elif runner == "go":
        return f"lupus_{digest}_test.go"
    else:
        return f"tests/lupus_{digest}.rs"
    return f"{test_dir}/{name}" if test_dir else name


def count_tests(runner: str, source: str) -> int:
    """Tests declared in a test file, counted from the source (a failing import hides the
    individual tests from the runner's summary)."""
    language = LANGUAGE[runner]
    if language == "python":
        tree = ast.parse(source)     # SyntaxError is the caller's to report
        n = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
                for node in ast.walk(tree))
    elif language == "node":
        n = len(re.findall(r"^\s*(?:test|it)(?:\.only)?\s*\(", source, re.M))
    elif language == "go":
        n = len(re.findall(r"^func Test\w*\(", source, re.M))
    else:
        n = len(_RUST_TEST_ATTR.findall(source))
    return max(1, n)


DRAFT_HINT = {
    "python": "파일 자체는 지금도 import 되어야 한다: 아직 없는 함수·클래스는 파일 맨 위가 아니라 각 테스트 함수 안에서 import 하거나 "
              "모듈을 import 한 뒤 속성으로 호출하라.",
    "node": "파일 자체는 지금도 로드되어야 한다: 아직 없는 모듈·export는 파일 맨 위의 import가 아니라 각 테스트 안에서 "
            "`await import()`(또는 require)로 불러와 사용하라.",
    "go": "테스트 함수 이름은 반드시 TestLupus로 시작하라. 아직 없는 함수를 호출해 빌드가 실패하는 것은 허용된다(그 외의 빌드 오류는 안 된다).",
    "rust": "통합 테스트로 작성하라(crate의 공개 API만 사용). 아직 없는 함수를 호출해 빌드가 실패하는 것은 허용된다(그 외의 빌드 오류는 안 된다).",
}
FORMAT = {"unittest": "unittest", "pytest": "pytest", "node": "node:test (import test from 'node:test')", "jest": "jest",
          "vitest": "vitest", "go": "Go testing", "cargo": "Rust #[test]"}


def sources(root: Path, language: str, limit: int = 40, about: str = "") -> list[str]:
    """Source files, the ones most likely to matter for `about` first: files whose name or content
    mentions the identifiers in the request. A plain alphabetical slice says nothing in a
    repository of real size."""
    files = [rel for rel in _walk(root, SOURCE_EXT[language]) if not is_test_file(language, rel)]
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", about)} - _COMMON
    if not words or len(files) <= limit // 4:
        return files[:limit]
    scored = []
    for position, rel in enumerate(files[:1500]):
        try:
            text = (root / rel).read_bytes()[:200_000].decode("utf-8", errors="ignore").lower()
        except OSError:
            continue
        name = rel.lower()
        score = sum(8 for w in words if w in name) + sum(min(text.count(w), 5) for w in words)
        scored.append((-score, position, rel))
    return [rel for _, _, rel in sorted(scored)[:limit]]


_COMMON = frozenset("the and for with that this from into when should must add fix use not are was has have will can".split())
