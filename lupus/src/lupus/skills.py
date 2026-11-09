"""Local, on-demand SKILL.md discovery. Listing never executes a skill or reads its assets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import os
import stat
import hashlib
import json
import time
from . import definitions, util

from .util import LupusError

MAX_SKILLS = 512
MAX_BUILTIN_SKILLS = 5
MAX_DEPTH = 6
MAX_BODY_BYTES = 65_536
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


def plain(value: str) -> str:
    """Keep metadata from injecting terminal control sequences."""
    return " ".join("".join(c for c in value if c.isprintable()).split())


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    path: Path
    resolved_path: Path
    source: str
    shadowed: tuple[str, ...] = ()
    directory_identity: tuple[int, int] | None = None
    file_signature: tuple | None = None


def _metadata(prefix: str) -> dict[str, str]:
    lines = prefix.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    active = ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if match:
            active, value = match.groups()
            out[active] = "" if value in ("|", ">", "|-", ">-") else value.strip("\"'")
        elif active and line[:1].isspace():
            out[active] = (out[active] + " " + line.strip()).strip()
        elif line and not line[:1].isspace():
            active = ""
    return {key: plain(value)[:400] for key, value in out.items()}


def _allowed_root(root: Path, home: Path, user: Path, excluded=()) -> bool:
    if root == user or user.is_relative_to(root):
        return False
    if any(part.lower() in (".ssh", ".aws", ".gnupg") for part in root.parts):
        return False
    for item in {str(home), *util.PROTECTED_STATE, *excluded}:
        protected = Path(item).resolve()
        if root.is_relative_to(protected):
            if protected == home and root.is_relative_to(home / "skills"):
                continue
            return False
        if protected.is_relative_to(root):
            return False
    return True


def _read_regular(root: Path, parts: tuple[str, ...], limit: int, *, identity=None,
                  signature=None, prefix=False) -> tuple[bytes, tuple[int, int], tuple]:
    try:
        with definitions._file(root, parts) as (fd, opened_root):
            before = os.fstat(fd)
            fingerprint = definitions._signature(before)
            if identity is not None and opened_root != tuple(identity):
                raise LupusError("SKILL_CHANGED", "skill directory changed")
            if signature is not None and fingerprint != tuple(signature):
                raise LupusError("SKILL_CHANGED", "skill file changed")
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise LupusError("SKILL_UNSAFE", "skill content must be a regular nonlinked file")
            if not prefix and before.st_size > limit:
                raise LupusError("SKILL_TOO_LARGE", "skill content exceeds the byte limit")
            remaining = min(before.st_size, limit)
            chunks=[]
            while remaining:
                block=os.read(fd,remaining)
                if not block:break
                chunks.append(block);remaining-=len(block)
            raw=b''.join(chunks)
            if remaining or definitions._signature(os.fstat(fd)) != fingerprint:
                raise LupusError("SKILL_CHANGED", "skill content changed during read")
            # Reopen through the same no-follow path; a replaced parent/leaf is not trusted.
            with definitions._file(root, parts) as (again, current_root):
                if current_root != opened_root or definitions._signature(os.fstat(again)) != fingerprint:
                    raise LupusError("SKILL_CHANGED", "skill content path changed")
        return raw, opened_root, fingerprint
    except (OSError, RuntimeError) as exc:
        raise LupusError("SKILL_UNREADABLE", "skill path is inaccessible or linked") from exc


def _files(root: Path, diagnostics=None, allowed=None, deadline=None):
    """Bound traversal and entries before sorting, including linked skill directories."""
    diagnostics = diagnostics if diagnostics is not None else {}
    stack=[(root,0)];seen=set();entries=0
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            diagnostics['deadline_limited']=True;return
        directory,depth=stack.pop()
        try:
            resolved=directory.resolve()
            if resolved in seen or not directory.is_dir():continue
            if allowed is not None and not allowed(resolved):
                diagnostics['rejected_roots']=diagnostics.get('rejected_roots',0)+1;continue
            seen.add(resolved)
            if len(seen)>4096:
                diagnostics['traversal_limited']=True;return
            manifest=directory/'SKILL.md'
            try:info=manifest.lstat()
            except FileNotFoundError:info=None
            if info is not None:
                if stat.S_ISREG(info.st_mode):yield manifest
                else:diagnostics['rejected_entries']=diagnostics.get('rejected_entries',0)+1
                continue
            if depth>=MAX_DEPTH:
                diagnostics['depth_limited']=True;continue
            children=[]
            with os.scandir(directory) as scan:
                for entry in scan:
                    entries+=1
                    if entries>4096:
                        diagnostics['entry_limited']=True;return
                    if deadline is not None and time.monotonic()>=deadline:
                        diagnostics['deadline_limited']=True;return
                    if not entry.name.startswith('.') and entry.name not in ('node_modules','__pycache__') and entry.is_dir():
                        children.append(Path(entry.path))
            stack.extend((child,depth+1) for child in sorted(children,reverse=True))
        except FileNotFoundError:continue
        except (OSError,RuntimeError):diagnostics['unreadable_sources']=diagnostics.get('unreadable_sources',0)+1


def _entry(path: Path, source: Path, label: str, diagnostics: dict, allowed=None) -> Skill | None:
    sid=path.parent.relative_to(source).as_posix()
    sid=source.name if sid=='.' else sid
    if not _ID.fullmatch(sid) or any(part in ('.','..') for part in sid.split('/')):return None
    try:
        resolved=path.resolve(strict=True)
        if allowed is not None and not allowed(resolved.parent):raise LupusError('SKILL_UNSAFE','skill root is outside authority')
        if path.is_symlink():raise LupusError('SKILL_UNSAFE','leaf manifest symlink')
        raw,identity,signature=_read_regular(resolved.parent,(resolved.name,),4096,prefix=True)
        if path.resolve(strict=True)!=resolved:raise LupusError('SKILL_CHANGED','skill link retargeted')
        meta=_metadata(raw.decode('utf-8',errors='replace'))
        return Skill(sid,meta.get('name') or sid,meta.get('description') or '',path,resolved,label,(),identity,signature)
    except (OSError,RuntimeError,LupusError):
        diagnostics['rejected_entries']=diagnostics.get('rejected_entries',0)+1
        return None


def discover(cwd: Path, home: Path, project_root: Path | None = None,
             user_home: Path | None = None, *, diagnostics: dict | None = None,
             excluded=(), deadline=None) -> dict[str, Skill]:
    """User sources first, then project sources from root to cwd; later sources win.

    IDs come from relative directory paths, never executable frontmatter. Built-in slash
    commands retain their names; `/skill ID` can select a colliding skill explicitly.
    """
    user = (user_home or Path.home()).resolve()
    home = home.resolve()
    diagnostics = diagnostics if diagnostics is not None else {}
    allowed = lambda path: _allowed_root(path,home,user,excluded)
    sources = [(user / ".codex/skills", "user · codex"),
               (user / ".claude/skills", "user · claude"),
               (user / ".agents/skills", "user · agents"), (home / "skills", "user · lupus")]
    current = cwd.resolve()
    root = (project_root or current).resolve()
    ancestry = [current]
    if current.is_relative_to(root):
        while ancestry[-1] != root:
            ancestry.append(ancestry[-1].parent)
    for directory in reversed(ancestry):
        sources.extend((directory / f".{kind}/skills", f"project · {kind}")
                       for kind in ("codex", "claude", "agents", "lupus"))
    found: dict[str, Skill] = {}
    inspected = 0
    # Select highest-precedence sources first so the global size cap cannot hide a
    # project's override behind a large user library.
    for source, label in reversed(sources):
        for path in _files(source,diagnostics,allowed,deadline):
            if inspected >= MAX_SKILLS:
                break
            inspected += 1
            skill = _entry(path,source,label,diagnostics,allowed)
            if skill is None:continue
            sid=skill.id
            old = found.get(sid)
            if old:
                if old.resolved_path != skill.resolved_path:
                    found[sid] = replace(old, shadowed=(*old.shadowed, str(skill.path)))
                continue
            found[sid] = skill
        if inspected >= MAX_SKILLS:
            diagnostics['catalogue_limited']=True
            break
    # A separate small package budget reserves the five basics even with a full local
    # library. Local/project IDs already selected above retain precedence.
    builtin = Path(__file__).with_name("builtin_skills")
    for index, path in enumerate(_files(builtin,diagnostics,allowed,deadline)):
        if index >= MAX_BUILTIN_SKILLS:
            diagnostics['builtin_limited']=True;break
        skill=_entry(path,builtin,"builtin · lupus",diagnostics,allowed)
        if skill is None:continue
        sid=skill.id
        if sid in found:
            found[sid] = replace(found[sid], shadowed=(*found[sid].shadowed, str(path)))
        else:
            found[sid] = skill
    return dict(sorted(found.items()))


def load(skill: Skill) -> str:
    try:
        if skill.path.resolve(strict=True) != skill.resolved_path:
            raise LupusError("SKILL_CHANGED", "run /skills again before selecting this skill")
        raw,_,_=_read_regular(skill.resolved_path.parent,(skill.resolved_path.name,),MAX_BODY_BYTES,
                              identity=skill.directory_identity,signature=skill.file_signature)
        if skill.path.resolve(strict=True)!=skill.resolved_path:
            raise LupusError('SKILL_CHANGED','skill link retargeted')
        return raw.decode('utf-8')
    except (OSError,UnicodeError,RuntimeError) as exc:
        raise LupusError('SKILL_UNREADABLE','skill cannot be read as bounded UTF-8') from exc


def expand(skill: Skill, request: str) -> str:
    """Instructions only: bundled scripts are never started by slash expansion."""
    body = load(skill)
    return (f"Use the selected skill {skill.id}.\n"
            f"Skill source: {skill.path}\n"
            f"Resolve this skill's relative references against: {skill.resolved_path.parent}\n"
            "Supporting resources remain subject to this worker's file/tool permissions.\n\n"
            f"{body}\n\nUser request:\n{request or 'Apply this skill to the current workspace.'}")


def binding(skill: Skill, digest: str) -> dict:
    """Controller-to-worker binding for an explicitly preloaded body, never a body copy."""
    if skill.directory_identity is None or skill.file_signature is None:
        raise LupusError('SKILL_CHANGED','selected skill has no pinned content identity')
    return {'id':skill.id,'name':skill.name,'description':skill.description,'source':skill.source,
            'path':str(skill.path),'resolved_path':str(skill.resolved_path),
            'directory_identity':list(skill.directory_identity),'file_signature':list(skill.file_signature),
            'sha256':digest}


def project_root(cwd: Path) -> Path:
    """Metadata-only root lookup, performed only after a real API worker is needed."""
    root=cwd.resolve()
    for item in (root,*list(root.parents)[:32]):
        if (item/'.git').exists():return item
    return root


class SkillAccess:
    """Worker-local lazy skill authority. No global cache, execution, imports or networking."""
    def __init__(self, cwd: Path, home: Path, *, mode='auto', user_home=None,
                 project=None, explicit=None, excluded=(), deadline=None):
        if mode not in ('auto','off'):raise LupusError('SKILL_POLICY_INVALID','choose auto or off')
        self.cwd=cwd.resolve();self.home=home.resolve();self.user=(user_home or Path.home()).resolve()
        self.project=project.resolve() if project is not None else None;self.mode=mode
        self.excluded=tuple(excluded);self.deadline=deadline;self.catalogue=None;self.diagnostics={}
        self.selected={};self.preloaded={};self.served=0;self.resource_attempts=0
        for data in explicit or []:
            if not isinstance(data,dict) or not _ID.fullmatch(str(data.get('id',''))) or not re.fullmatch(r'[0-9a-f]{64}',str(data.get('sha256',''))):
                raise LupusError('SKILL_CHANGED','invalid selected skill binding')
            skill=Skill(data['id'],data.get('name',data['id']),data.get('description',''),
                        Path(data['path']),Path(data['resolved_path']),data['source'],(),
                        tuple(data['directory_identity']),tuple(data['file_signature']))
            self.selected[skill.id]=(skill,data['sha256'])
            self.preloaded[skill.id]=True
        if len(self.selected)>8:raise LupusError('SKILL_LIMIT','at most eight selected skills')
    def _check_deadline(self):
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise LupusError('SKILL_LIMIT','skill operation deadline reached')
    def _check(self, skill: Skill):
        self._check_deadline()
        root=skill.resolved_path.parent
        if not _allowed_root(root,self.home,self.user,self.excluded):
            raise LupusError('SKILL_UNSAFE','skill root is outside declared authority')
        try:
            if skill.path.is_symlink() or skill.path.resolve(strict=True)!=skill.resolved_path:
                raise LupusError('SKILL_CHANGED','skill link changed')
        except (OSError,RuntimeError) as exc:
            raise LupusError('SKILL_CHANGED','skill path changed') from exc
    def _text(self, raw: bytes):
        if b'\0' in raw or util.find_secret_bytes(raw):
            raise LupusError('SKILL_UNSAFE','binary or credential content is excluded')
        try:text=raw.decode('utf-8')
        except UnicodeError as exc:raise LupusError('SKILL_UNREADABLE','skill content must be UTF-8') from exc
        if any(ord(c)<32 and c not in '\r\n\t' for c in text):
            raise LupusError('SKILL_UNSAFE','binary/control content is excluded')
        return text
    def _body(self, skill: Skill, expected=None):
        self._check(skill)
        raw,_,_=_read_regular(skill.resolved_path.parent,(skill.resolved_path.name,),MAX_BODY_BYTES,
                              identity=skill.directory_identity,signature=skill.file_signature)
        self._check(skill)
        digest=hashlib.sha256(raw).hexdigest()
        if expected is not None and digest!=expected:raise LupusError('SKILL_CHANGED','selected body digest changed')
        return raw,self._text(raw),digest
    def _catalogue(self):
        if self.catalogue is None:
            self._check_deadline()
            self.catalogue=discover(self.cwd,self.home,self.project or project_root(self.cwd),self.user,diagnostics=self.diagnostics,
                                    excluded=self.excluded,deadline=self.deadline)
        return self.catalogue
    def _entry(self, sid):
        if not isinstance(sid,str) or not _ID.fullmatch(sid) or any(part in ('.','..') for part in sid.split('/')):
            raise LupusError('SKILL_UNKNOWN','invalid skill ID')
        if sid in self.selected:return self.selected[sid][0]
        if self.mode!='auto':raise LupusError('SKILL_POLICY_OFF','automatic skill access is off; only explicitly selected resources are allowed')
        found=self._catalogue().get(sid)
        if found is None:raise LupusError('SKILL_UNKNOWN','skill ID not in the bounded catalogue; listing limits may apply')
        return found
    def _spend(self,size):
        if self.served+size>256*1024:raise LupusError('SKILL_LIMIT','skill response byte budget reached')
        self.served+=size
    def list(self,query='',offset=0,limit=16):
        if self.mode!='auto':raise LupusError('SKILL_POLICY_OFF','automatic discovery is off')
        if not isinstance(query,str) or len(query)>128 or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=16:
            raise LupusError('SKILL_ARGUMENTS','use a bounded query and page of at most sixteen skills')
        rows=[]
        for item in self._catalogue().values():
            if query.casefold() not in (item.id+' '+item.name+' '+item.description).casefold():continue
            name=plain(item.name)[:80];description=plain(item.description)[:160]
            if util.find_secret(name+' '+description):
                name=item.id;description='[metadata withheld]'
            version=hashlib.sha256(repr((item.directory_identity,item.file_signature)).encode()).hexdigest()
            rows.append({'id':item.id,'name':name,'description':description,'source':item.source,
                         'version':version,'body_bytes':item.file_signature[4],
                         'preloaded':item.id in self.preloaded})
        page=rows[offset:offset+limit]
        result={'skills':page,'matched':len(rows),'next_offset':offset+len(page) if offset+len(page)<len(rows) else None,
                'catalogue_complete':not bool(self.diagnostics),'diagnostics':dict(self.diagnostics),
                'scope':'declared skill roots within fixed depth/entry/count bounds; no body or script preload'}
        encoded=json.dumps(result,ensure_ascii=False).encode()
        if len(encoded)>16*1024:raise LupusError('SKILL_LIMIT','metadata page exceeds response bound')
        self._spend(len(encoded));return result
    def read(self,sid):
        skill=self._entry(sid)
        if sid not in self.selected and len(self.selected)>=8:raise LupusError('SKILL_LIMIT','at most eight selected skills')
        expected=self.selected[sid][1] if sid in self.selected else None
        raw,text,digest=self._body(skill,expected)
        self._spend(len(raw));self.selected[sid]=(skill,digest)
        return {'id':sid,'content':text,'sha256':digest,'bytes':len(raw),'source':skill.source,
                'resource_scope':'relative files inside this pinned skill directory; read does not authorize execution'}
    def resource(self,sid,path):
        self._check_deadline()
        if self.resource_attempts>=16:raise LupusError('SKILL_LIMIT','resource read limit reached')
        self.resource_attempts+=1
        if sid not in self.selected:raise LupusError('SKILL_NOT_SELECTED','read the skill body before its resources')
        skill,digest=self.selected[sid]
        # Also verifies a parent-preloaded body before any resource access, without
        # another model request or returning that body a second time.
        self._body(skill,digest)
        if not isinstance(path,str) or not path or len(path.encode())>1024 or '\\' in path or any(not c.isprintable() for c in path):
            raise LupusError('SKILL_RESOURCE_PATH','use a plain relative resource path')
        parts=tuple(path.split('/'))
        if len(parts)>12 or any(part in ('','.','..','.git','.ssh','.aws','.gnupg') or part.lower() in ('auth.json','credentials.json','.credentials.json','id_rsa','id_ed25519') or part.startswith('.env') for part in parts):
            raise LupusError('SKILL_RESOURCE_PATH','resource path is outside skill authority')
        target=skill.resolved_path.parent.joinpath(*parts)
        if not _allowed_root(target,self.home,self.user,self.excluded):raise LupusError('SKILL_UNSAFE','protected state is excluded')
        raw,_,_=_read_regular(skill.resolved_path.parent,parts,MAX_BODY_BYTES,identity=skill.directory_identity)
        self._check(skill);self._body(skill,digest)
        text=self._text(raw);self._spend(len(raw))
        return {'id':sid,'path':path,'content':text,'sha256':hashlib.sha256(raw).hexdigest(),
                'bytes':len(raw),'source':skill.source,'execution':'text only; no resource is executed by this tool'}
