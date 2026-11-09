"""Bounded Python declarations in one named file or explicit directory, without imports."""
from __future__ import annotations

import ast
import contextlib
import os
import re
import stat
from pathlib import Path

from .util import find_secret

MAX_BYTES, MAX_NODES, MAX_MATCHES, MAX_DEPTH = 512 * 1024, 50_000, 100, 32
MAX_FILES, MAX_ENTRIES, MAX_DIRS = 64, 512, 64
MAX_TOTAL_BYTES, MAX_TOTAL_NODES = 2 * 1024 * 1024, 250_000
_NAME = r'(?P<name_quote>`?)(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?P=name_quote)'
_PATH = r'(?P<path>`[^`\r\n]+\.py`|[^\s`?]+\.py)'
_EN = re.compile(r'\s*Where\s+is\s+(?:the\s+)?Python\s+(?P<kind>function|class)\s+' + _NAME
                 + r'\s+defined\s+in\s+' + _PATH + r'\s*\??\s*', re.I)
_KO = re.compile(r'\s*' + _PATH + r'에서\s*파이썬\s+(?P<kind>함수|클래스)\s+' + _NAME
                 + r'\s+정의\s+위치(?:를)?\s*알려\s*(?:줘|주세요)\s*[.!?]?\s*')

_DIRECTORY_PATH = r'(?P<path>`[^`\r\n]+`|[^\s`?]+)'
_EN_DIRECTORY = re.compile(r'\s*Where\s+is\s+(?:the\s+)?Python\s+(?P<kind>function|class)\s+' + _NAME
                           + r'\s+defined\s+under\s+' + _DIRECTORY_PATH + r'\s*\??\s*', re.I)
_KO_DIRECTORY = re.compile(r'\s*' + _DIRECTORY_PATH + r'\s+아래에서\s*파이썬\s+(?P<kind>함수|클래스)\s+' + _NAME
                           + r'\s+정의\s+위치(?:를)?\s*알려\s*(?:줘|주세요)\s*[.!?]?\s*')


def parse(request: str) -> dict | None:
    match = _EN.fullmatch(request) or _KO.fullmatch(request)
    directory = False
    if match is None:
        match = _EN_DIRECTORY.fullmatch(request) or _KO_DIRECTORY.fullmatch(request)
        directory = True
    if match is None:
        return None
    found = match.groupdict()
    spec = {'name': found['name'], 'kind': 'function' if found['kind'].lower() in ('function', '함수') else 'class',
            'path': found['path'].strip('`')}
    return {**spec, 'directory': True} if directory else spec


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextlib.contextmanager
def _file(root: Path, parts: tuple[str, ...]):
    """Open this exact file through no-follow directory descriptors; never walk a tree."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        current = os.open(root, directory_flags)
        stack.callback(os.close, current)
        root_info = os.fstat(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            stack.callback(os.close, current)
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        stack.callback(os.close, target)
        yield target, (root_info.st_dev, root_info.st_ino)


def locate(request: str, root: Path, max_reply: int = 32_000) -> dict | None:
    """All matching syntactic declarations, or None for the existing read-only reader.

    No matches/errors/limits are not evidence of repository absence. Never return a partial
    list when a complete traversal of the named file could not be established.
    """
    spec = parse(request)
    if spec is None:
        return None
    if spec.get('directory'):
        return _locate_directory(spec, root, max_reply)
    path = Path(spec['path'])
    parts = path.parts
    if path.is_absolute() or not parts or len(parts) > MAX_DEPTH or '..' in parts or '\x00' in str(path):
        return None
    try:
        with _file(root.resolve(), parts) as (fd, root_id):
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
                return None
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or _signature(before) != _signature(os.fstat(fd)):
                return None
        parsed = _declarations(data, str(path), spec, MAX_NODES, MAX_MATCHES)
        if parsed is None:
            return None
        matches, _ = parsed
        if not matches:
            return None
        with _file(root.resolve(), parts) as (fd, current_root_id):
            if root_id != current_root_id or _signature(before) != _signature(os.fstat(fd)):
                return None
        matches.sort(key=lambda item: (item['line'], item['qualified_name']))
        scope_label = 'Python syntactic definitions in ' + path.as_posix()
        result = scope_label + '\n' + '\n'.join(
            f"{item['path']}:{item['line']} — {item['qualified_name']} ({item['kind']})" for item in matches)
        if len(result) > max_reply or find_secret(result):
            return None
        return {'result': result, 'definition_scope': scope_label, 'definitions': matches}
    except (OSError, ValueError, UnicodeError, SyntaxError, RecursionError, MemoryError, AttributeError):
        return None


@contextlib.contextmanager
def _directory(root: Path, parts: tuple[str, ...]):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        current = os.open(root, flags)
        stack.callback(os.close, current)
        root_info = os.fstat(current)
        for part in parts:
            current = os.open(part, flags, dir_fd=current)
            stack.callback(os.close, current)
        yield current, (root_info.st_dev, root_info.st_ino)


def _inventory(root: Path, parts: tuple[str, ...]):
    """All directories and Python files in the explicit tree, or no local result.

    No subtree is silently pruned. Non-Python regular files are never opened.
    Symlinks cannot establish a complete recursive scope without following them.
    """
    pending, directories, files = [parts], [], []
    entries = total_bytes = 0
    identity = None
    while pending:
        current = pending.pop()
        if len(current) > MAX_DEPTH or len(directories) >= MAX_DIRS:
            return None
        with _directory(root, current) as (fd, root_id):
            before = os.fstat(fd)
            if identity is not None and identity != root_id:
                return None
            identity = root_id
            with os.scandir(fd) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        return None
                    info = child.stat(follow_symlinks=False)
                    child_parts = (*current, child.name)
                    if stat.S_ISLNK(info.st_mode):
                        return None
                    if stat.S_ISDIR(info.st_mode):
                        if not child.name.isprintable():
                            return None
                        pending.append(child_parts)
                    elif child.name.endswith('.py'):
                        if (not child.name.isprintable() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
                                or len(child_parts) > MAX_DEPTH):
                            return None
                        total_bytes += info.st_size
                        files.append((child_parts, _signature(info)))
                        if len(files) > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
                            return None
            if _signature(before) != _signature(os.fstat(fd)):
                return None
            directories.append((current, _signature(before)))
    return identity, directories, sorted(files)


def _declarations(data: bytes, path: str, spec: dict, remaining_nodes: int, remaining_matches: int):
    tree = ast.parse(data.decode('utf-8-sig'), filename=path)
    pending, matches, visited = [(tree, ())], [], 0
    while pending:
        node, scope = pending.pop()
        visited += 1
        if visited > min(MAX_NODES, remaining_nodes):
            return None
        nested = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = '.'.join((*scope, node.name))
            kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
            wanted = qualified if '.' in spec['name'] else node.name
            if kind == spec['kind'] and wanted == spec['name']:
                matches.append({'path': path, 'line': node.lineno, 'qualified_name': qualified, 'kind': kind})
                if len(matches) > remaining_matches:
                    return None
            nested = (*scope, node.name)
        pending.extend((child, nested) for child in ast.iter_child_nodes(node))
    return matches, visited


def _locate_directory(spec: dict, root: Path, max_reply: int):
    path = Path(spec['path'])
    parts = path.parts
    if path.is_absolute() or not parts or '..' in parts or not str(path).isprintable() or len(parts) > MAX_DEPTH:
        return None
    try:
        root = root.resolve()
        inventory = _inventory(root, parts)
        if inventory is None:
            return None
        root_id, directories, files = inventory
        matches, total_nodes = [], 0
        for file_parts, signature in files:
            with _file(root, file_parts) as (fd, identity):
                if identity != root_id or _signature(os.fstat(fd)) != signature:
                    return None
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES or _signature(os.fstat(fd)) != signature:
                    return None
            result = _declarations(data, Path(*file_parts).as_posix(), spec,
                                             MAX_TOTAL_NODES - total_nodes, MAX_MATCHES - len(matches))
            if result is None:
                return None
            found, visited = result
            matches.extend(found)
            total_nodes += visited
        # Reopen all paths without following links. This is observed snapshot
        # validation, not an OS-atomic snapshot against arbitrary outside edits.
        for dir_parts, signature in directories:
            with _directory(root, dir_parts) as (fd, identity):
                if identity != root_id or _signature(os.fstat(fd)) != signature:
                    return None
        for file_parts, signature in files:
            with _file(root, file_parts) as (fd, identity):
                if identity != root_id or _signature(os.fstat(fd)) != signature:
                    return None
        if not matches:
            return None
        matches.sort(key=lambda item: (item['path'], item['line'], item['qualified_name']))
        scope = 'Python syntactic definitions in .py files recursively under ' + path.as_posix()
        result = scope + '\n' + '\n'.join(
            f"{item['path']}:{item['line']} — {item['qualified_name']} ({item['kind']})" for item in matches)
        if len(result) > max_reply or find_secret(result):
            return None
        return {'result': result, 'definition_scope': scope, 'definitions': matches}
    except (OSError, ValueError, UnicodeError, SyntaxError, RecursionError, MemoryError, AttributeError):
        return None
