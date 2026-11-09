"""Exact complete contents of one bounded named text file, or the existing reader fallback."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .definitions import _file, _signature
from .util import find_secret_bytes, sha256_bytes

_PATH = r'''(?P<path>`[^`\r\n]+`|'[^'\r\n]+'|"[^"\r\n]+"|[A-Za-z0-9_가-힣][A-Za-z0-9_가-힣./-]*?)'''
_EN = re.compile(r'\s*(?:show|read)\s+(?:the\s+)?(?:complete\s+)?contents\s+of\s+' + _PATH + r'\s*[.!?]?\s*',re.I)
_KO = re.compile(r'\s*' + _PATH + r'\s+내용을\s+그대로\s+보여\s*(?:줘|주세요)\s*[.!?]?\s*')
_SHORT_EN = re.compile(r'\s*(?:read|show|cat)\s+' + _PATH + r'\s*[.!?]?\s*',re.I)
_SHORT_KO = re.compile(r'\s*' + _PATH + r'\s+(?:내용(?:을)?\s+)?보여\s*(?:줘|주세요)\s*[.!?]?\s*')
_CREDENTIAL = re.compile(r'secret|credential|password|token|api[-_]?key|private[-_]?key',re.I)


def parse(request: str) -> dict | None:
    match = _EN.fullmatch(request) or _KO.fullmatch(request) or _SHORT_EN.fullmatch(request) or _SHORT_KO.fullmatch(request)
    if match is None:
        return None
    rel = match['path']
    return {'path': rel[1:-1] if rel[0] in ('`',"'",'"') else rel}


def contents(request: str, root: Path, max_reply: int = 32_000) -> dict | None:
    spec = parse(request)
    if spec is None or max_reply < 0:
        return None
    rel = spec['path']
    parts = tuple(rel.split('/'))
    if (not rel.isprintable() or len(parts) > 32 or
            any(not p or p in ('.','..') or p.startswith('.') or _CREDENTIAL.search(p) for p in parts) or
            any(c in rel for c in '\\:*?[]') or Path(rel).suffix.lower() not in ('.txt','.md','.rst')):
        return None
    try:
        root = root.resolve()
        with _file(root, parts) as (fd, root_id):
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_reply:
                return None
            with os.fdopen(fd,'rb',closefd=False) as stream:
                data = stream.read(max_reply + 1)
            if len(data) > max_reply or _signature(before) != _signature(os.fstat(fd)):
                return None
        text = data.decode('utf-8')  # Preserve CRLF and an initial UTF-8 BOM verbatim.
        if find_secret_bytes(data) or any(not (c.isprintable() or c in '\r\n\t' or
                (i == 0 and c == '\ufeff')) for i,c in enumerate(text)):
            return None
        with _file(root,parts) as (fd,current_root_id):
            if root_id != current_root_id or _signature(before) != _signature(os.fstat(fd)):
                return None
        return {'result':text,'file_path':rel,'file_sha256':sha256_bytes(data),'file_bytes':len(data),
                'read_scope':'complete contents of one named UTF-8 file'}
    except (OSError,ValueError,UnicodeError):
        return None
