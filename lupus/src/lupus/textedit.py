"""A bounded, literal document replacement with a fixed byte-for-byte acceptance result.

This is supervisor work, serialized with other supervisors. It never asks a model to
decide request completion. A supplied check uses the existing gated verifier contract.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import goals, projects, protect, quick, runs, supervisor, verify
from .kernel import Kernel
from .util import LupusError, atomic_write, find_secret_bytes, sha256_bytes

MAX_BYTES = 512 * 1024
_QUOTED = r'''(?:"([^"\r\n]*)"|'([^'\r\n]*)')'''
_PATH = r'(?P<tick>`?)([A-Za-z0-9_가-힣][A-Za-z0-9_가-힣./-]*?)(?P=tick)'
_EN = re.compile(r'\s*(?:please\s+)?replace\s+' + _QUOTED + r'\s+with\s+' + _QUOTED + r'\s+in\s+' + _PATH + r'\s*[.!]?\s*', re.I)
_KO = re.compile(r'\s*' + _PATH + r'에서\s*' + _QUOTED + r'(?:을|를)\s*' + _QUOTED + r'(?:으로|로)\s*교체해\s*줘\s*[.!]?\s*')


def eligible(rel: str, source: bool = False) -> bool:
    path = Path(rel)
    if path.is_absolute() or any(p in ('.', '..', 'node_modules', 'venv', 'env', '__pycache__') or p.startswith('.') for p in path.parts):
        return False
    # Dependency manifests and agent instructions are control inputs even when plain text.
    if path.stem.lower() in ('agents', 'skill', 'claude', 'gemini', 'cmakelists') or path.stem.lower().startswith(('requirements', 'constraints')):
        return False
    if source:
        return path.suffix == '.py'
    if path.suffix.lower() not in ('.md', '.rst', '.txt'):
        return False
    return (len(path.parts) == 1 and path.stem.lower() in ('readme', 'changelog', 'contributing')) or (
        len(path.parts) > 1 and path.parts[0] == 'docs')


def parse(request: str, source: bool = False) -> dict | None:
    match = _EN.fullmatch(request)
    if match:
        a, b, c, d, _tick, rel = match.groups()
    else:
        match = _KO.fullmatch(request)
        if not match:
            return None
        _tick, rel, a, b, c, d = match.groups()
    old, new = a if a is not None else b, c if c is not None else d
    if not old or not eligible(rel, source):
        return None
    return {'path': Path(rel).as_posix(), 'old': old, 'new': new}


def _target(root: Path, rel: str) -> Path:
    target = root / rel
    if any(root.joinpath(*Path(rel).parts[:i]).is_symlink() for i in range(1, len(Path(rel).parts) + 1)):
        raise LupusError('TEXT_EDIT_INVALID', 'document path contains a symlink')
    if root not in target.resolve().parents or not target.is_file():
        raise LupusError('TEXT_EDIT_INVALID', 'document must be an existing regular file inside the project')
    return target


def _idle(k: Kernel, project_id: str) -> None:
    if k.one("SELECT 1 FROM run WHERE project_id = ? AND status <> 'STOPPED'", project_id) or runs.aux_alive(k, project_id):
        raise LupusError('WRITER_NOT_STOPPED', project_id)


def apply(k: Kernel, project: dict, request: str, actor: str, check: str | None = None,
          protect_paths: list[str] | None = None, caps: dict | None = None) -> dict:
    if actor != 'user':
        raise LupusError('USER_AUTHORITY_REQUIRED', 'document edits are submitted by the user')
    spec = parse(request, source=check is not None)
    if spec is None:
        raise LupusError('TEXT_EDIT_INVALID', 'expected one literal replacement in a supported document')
    if spec['old'] == spec['new']:
        raise LupusError('TEXT_EDIT_INVALID', 'old and new text are identical; no edit requested')
    with k.supervisor_lock():
        project = projects.check_root(k, project['project_id'])
        root, rel = Path(project['canonical_root']), spec['path']
        _idle(k, project['project_id'])
        if k.one("SELECT 1 FROM goal WHERE project_id = ? AND status NOT IN ('DONE','CANCELLED')", project['project_id']):
            raise LupusError('TEXT_EDIT_PROTECTED', 'another unfinished goal may own frozen project inputs')
        target = _target(root, rel)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or info.st_nlink != 1:
            raise LupusError('TEXT_EDIT_INVALID', 'document must be a small regular file without hard links')
        with target.open('rb') as stream:
            before = stream.read(MAX_BYTES + 1)
        if len(before) > MAX_BYTES:
            raise LupusError('TEXT_EDIT_INVALID', 'document exceeds size limit')
        try:
            text = before.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise LupusError('TEXT_EDIT_INVALID', 'document must be UTF-8 text') from exc
        if b'\x00' in before or find_secret_bytes(before):
            raise LupusError('TEXT_EDIT_INVALID', 'binary data or credentials in document')
        first = text.find(spec['old'])
        if first < 0 or text.find(spec['old'], first + 1) >= 0:
            raise LupusError('TEXT_EDIT_INVALID', 'old text must occur exactly once')
        expected = text.replace(spec['old'], spec['new'], 1).encode('utf-8')
        if len(expected) > MAX_BYTES or find_secret_bytes(expected):
            raise LupusError('TEXT_EDIT_INVALID', 'result exceeds size limit or contains credentials')
        verifier = {'kind': 'file_sha256', 'path': rel, 'sha256': sha256_bytes(expected)}
        criteria = [{'id': 'c0', 'text': 'Exact requested replacement; all other document bytes preserved', 'verifier': verifier}]
        if check is not None:
            _, command = quick.checked_verifier(root, check, protect_paths)
            protected = [*command.get('protect', []), *command.get('frozen_trees', [])]
            if any(Path(rel) == Path(item) or Path(item) in Path(rel).parents for item in protected):
                raise LupusError('TEXT_EDIT_PROTECTED', 'replacement overlaps a protected input')
            criteria = [{'id': 'c0', 'text': f'User command passes: {check}', 'verifier': command},
                        {'id': 'c1', 'text': 'Exact expected bytes remain after the command', 'verifier': verifier}]
        with k.tx():
            goal = goals.submit(k, project['project_id'], request, criteria,
                                (caps or quick.DEFAULT_CAPS) if check is not None else
                                {'calls': 1, 'attempts': 1, 'active_ms': 60_000}, actor='user')
            if check is not None:
                protect.freeze(k, goal['goal_id'], root)
            backup = k.runtime / 'text-edits' / goal['goal_id'] / 'original'
            backup.parent.mkdir(mode=0o700, parents=True)
            atomic_write(backup, before)
            k.emit('supervisor', 'text_edit.prepared', 'goal', goal['goal_id'], path=rel,
                   original_sha256=sha256_bytes(before), expected_sha256=verifier['sha256'], backup=str(backup),
                   original_mode=stat.S_IMODE(info.st_mode))
        # Hold the DB writer transaction as well as the supervisor lock across mutation:
        # another task cannot claim the project in the gap between idle check and writing.
        with k.tx():
            _idle(k, project['project_id'])
            if k.one("SELECT 1 FROM goal WHERE project_id = ? AND goal_id <> ? AND status NOT IN ('DONE','CANCELLED')",
                     project['project_id'], goal['goal_id']):
                raise LupusError('TEXT_EDIT_PROTECTED', 'another unfinished goal appeared before the document edit')
            current = _target(root, rel).stat()
            if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (
                    info.st_dev, info.st_ino, info.st_mode, info.st_nlink) or current.st_size != len(before) or target.read_bytes() != before:
                raise LupusError('TEXT_EDIT_CHANGED', 'document changed after its original state was captured')
            if check is not None and protect.drift(k, goal['goal_id'], root):
                raise LupusError('TEXT_EDIT_CHANGED', 'protected inputs changed outside the command')
            atomic_write(target, expected, mode=stat.S_IMODE(info.st_mode))
            # atomic_write's new inode is created under the process umask. Restore the
            # exact permission bits before accepting the result.
            os.chmod(target, stat.S_IMODE(info.st_mode), follow_symlinks=False)
            if check is None:
                verdict, digest, detail = verify.run(verifier, root)
                goals.record_evidence(k, goal['goal_id'], 'c0', verify.VERSION, digest, verdict, detail,
                                      acceptance_revision=goal['acceptance_revision'], verifier=verifier)
            k.emit('supervisor', 'text_edit.applied', 'goal', goal['goal_id'], path=rel, edit_scope=[rel])
            if check is None:
                goals.complete(k, goal['goal_id'])
        if check is not None:
            if protect.drift(k, goal['goal_id'], root):
                raise LupusError('TEXT_EDIT_CHANGED', 'protected inputs changed before command verification')
            restored = {'restored': [], 'unrestorable': [], 'kept': []}
            try:
                report = supervisor.finish(k, goal['goal_id'])
            finally:
                # A mutated frozen input invalidates the finish verdict. Restore it without
                # silently repeating a command whose side effects the user did not request.
                if not runs.aux_alive(k, project['project_id']):
                    restored = protect.restore(k, goal['goal_id'], root,
                                               keep_dir=k.runtime / 'text-edits' / goal['goal_id'] / 'after-check')
            return {**report, 'route': 'checked', 'execution': 'literal-edit', 'check': check,
                    'calls': 0, 'path': rel, 'expected_sha256': verifier['sha256'],
                    'restored': restored, 'budget': goals.view(k, goal['goal_id'])['budget'],
                    'usage': {'calls': 0, 'tokens_in': 0, 'tokens_out': 0}}
        return {'route': 'text-edit', 'goal_id': goal['goal_id'], 'done': True, 'path': rel,
                'calls': 0, 'test_runs': 0, 'expected_sha256': verifier['sha256'], 'edit_scope': [rel],
                'usage': {'calls': 0, 'tokens_in': 0, 'tokens_out': 0}}
