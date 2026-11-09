"""Optional controller learning, never a worker tool or a completion authority.

Only original CLI input can be a correction. File observations have UNKNOWN actor/intent.
No model classifies/summarises, no watcher scans a tree, and no full file is retained.
"""
from __future__ import annotations
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import definitions, memory, projects
from .kernel import Kernel, migrations
from .util import LupusError, PROTECTED_STATE, find_secret, find_secret_bytes, new_id, sha256_json

_CURRENT = contextvars.ContextVar('lupus_controller_learning',default=None)
_PATH = re.compile(r'(?<![\w/.:\\])(?:[A-Za-z0-9_가-힣-]+/)*[A-Za-z0-9_가-힣-]+\.(?:py|js|ts|tsx|md|txt|rst|json|yaml|yml|toml)(?![\w./:\\])')
_CORRECTION = re.compile(r'(?:정정\s*:\s*\S|correction\s*:\s*\S|아니\s*[,，]?\s*.+?\s*말고\s*\S|no\s*[,，]\s*.+?\binstead\s+of\s+\S)',re.I)



_TOPIC_STOP = frozenset("correction no do that this it instead of use please the a an to with not rather than one thing change make fix is be should way me my your our 정정 아니 말고 대신 이거 그거 저거 이것 그것 저것 이걸 그걸 저걸 해 해줘 해주세요 줘 하자 고쳐 고쳐줘 바꿔 바꿔줘 수정해 수정해주세요 그렇게 이렇게 저렇게 내용 방식 방법 이전 새 새로운".split())


def _has_topic(text):
    text=re.sub(r'(?:이런거|그런거|저런거|그거|이거|저거|이것|그것|저것|이걸|그걸|저걸)(?:으로|로|을|를|은|는|만|도)?',' ',text)
    words=re.findall(r'[a-z][a-z0-9_-]*|[가-힣]+',text.lower())
    words=[re.sub(r'(?:으로|로|은|는|을|를|이|가|만|도|에)$','',word) if re.search('[가-힣]',word) else word for word in words]
    return any(len(word)>=2 and word not in _TOPIC_STOP for word in words)


def _named_paths(request):
    """Only unambiguous bare paths. Quoted spans are unsupported, never suffix-guessed."""
    plain=[];quote=None;escaped=False;text=request[:32000]
    for index,char in enumerate(text):
        if quote is not None:
            plain.append('\0')
            if char==quote and not escaped:quote=None
            escaped=char=='\\' and not escaped
        elif char in "\"'`" and not (char=="'" and index and index+1<len(text) and text[index-1].isalnum() and text[index+1].isalnum()):
            quote=char;escaped=False;plain.append('\0')
        else:plain.append(char)
    found=[]
    for token in re.finditer(r'\S+',''.join(plain)):
        path=token.group(0)
        if len(path)>512 or _PATH.fullmatch(path) is None:continue
        if path not in found:found.append(path)
        if len(found)==8:break
    return found


def correction(request):
    if not isinstance(request,str) or not 8<=len(request)<=400 or any(c in request for c in '\r\n`'):
        return None
    text=request.strip()
    if not _CORRECTION.match(text) or not _has_topic(text) or text[0] in '\"\'' or find_secret(text) or memory._REFERENCE_OVERRIDE.search(memory.visible(text)):
        return None
    if any(ord(c)<32 or c!=memory.visible(c) for c in text):return None
    # Literal evidence only; no inferred topic equivalence or replacement of user rules.
    return text


def suppress_node(k,node_id):
    """Called inside the same user retirement/deletion transaction, before deletion."""
    if k.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_learning_source'"):
        k.run('UPDATE auto_learning_source SET suppressed=1 WHERE node_id=?',node_id)
        if k.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_learning_entry'"):
            k.run('UPDATE auto_learning_source SET suppressed=1 WHERE (project_id,kind,source_key) IN (SELECT project_id,kind,source_key FROM auto_learning_entry WHERE node_id=?)',node_id)


def _activity(k,pid):
    return sha256_json([
        k.one('SELECT coalesce(max(rowid),0) FROM run WHERE project_id=?',pid)[0],
        k.one('SELECT coalesce(max(rowid),0) FROM service_call WHERE project_id=?',pid)[0],
        k.one("SELECT coalesce(max(seq),0) FROM event WHERE aggregate_id=? AND type IN ('agent.started','agent.finished')",pid)[0]])


def _quiet(k,pid):
    return not k.one("SELECT 1 FROM run WHERE project_id=? AND status<>'STOPPED'",pid) and not k.one('SELECT 1 FROM aux_process WHERE project_id=? LIMIT 1',pid) and not k.one("SELECT 1 FROM service_call WHERE project_id=? AND status='RUNNING'",pid)


def _read(root,rel,home,known_hash=None):
    parts=tuple(rel.split('/'))
    if not parts or len(parts)>12 or len(rel)>512 or any(not p or p in ('.','..') or p.startswith('.') for p in parts):return None
    if any(c in rel for c in '\\:*?[]') or not rel.isprintable():return None
    if re.search(r'secret|credential|password|token|api[-_]?key|private[-_]?key',rel,re.I):return None
    path=root.joinpath(*parts)
    if any(path==Path(p).resolve() or Path(p).resolve() in path.parents for p in {*PROTECTED_STATE,str(home)}):return None
    try:
        with definitions._file(root,parts) as (fd,rid):
            before=os.fstat(fd);signature=definitions._signature(before)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>16384:return None
            raw=os.read(fd,16385)
            if len(raw)!=before.st_size or definitions._signature(os.fstat(fd))!=signature:return None
            with definitions._file(root,parts) as (again,rid2):
                if rid2!=rid or definitions._signature(os.fstat(again))!=signature:return None
        if b'\0' in raw or find_secret_bytes(raw):return None
        text=raw.decode('utf-8')
        if any(ord(c)<32 and c not in '\r\n\t' for c in text):return None
        if text.count('\n')+1>256:return None
        digest=hashlib.sha256(raw).hexdigest()
        # A hint only saves line work after every existing validation has passed.
        if type(known_hash) is str and digest==known_hash:return {'hash':digest}
        lines=text.split('\n')
        return {'hash':digest,'lines':[hashlib.sha256(s.encode()).hexdigest() for s in lines],'text_lines':lines}
    except (OSError,UnicodeError,RuntimeError):return None


def _safe_read(k,project,rel,home,known_hash=None):
    pid=project['project_id']
    if k.one('SELECT 1 FROM protected_file f JOIN goal g ON g.goal_id=f.goal_id AND g.acceptance_revision=f.acceptance_revision WHERE g.project_id=? AND f.path=? LIMIT 1',pid,rel):return None
    if k.one("SELECT 1 FROM protected_dir d JOIN goal g ON g.goal_id=d.goal_id AND g.acceptance_revision=d.acceptance_revision WHERE g.project_id=? AND (d.path IN ('','.') OR d.path=? OR substr(?,1,length(d.path)+1)=d.path||'/') LIMIT 1",pid,rel,rel):return None
    if known_hash is None:return _read(Path(project['canonical_root']),rel,home)
    return _read(Path(project['canonical_root']),rel,home,known_hash)


def _existing(home):
    """Inspect only existing/current schema before a controller may open a writing Kernel."""
    db=home/'runtime/lupus.db'
    if not db.is_file():return None,'missing'
    conn=None
    try:
        conn=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
        conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON');conn.execute('PRAGMA trusted_schema=OFF')
        expected={v:h for v,_,_,h in migrations()}
        if {r['version']:r['checksum'] for r in conn.execute('SELECT version,checksum FROM schema_migration')}!=expected:
            conn.close();return None,'schema_unavailable'
        return memory._ReadContext(conn),'ready'
    except (OSError,sqlite3.Error,ValueError,TypeError):
        if conn is not None:conn.close()
        return None,'unavailable'


def _would_add(k,pid,kind,key,body):
    from . import learn
    old=k.one('SELECT * FROM auto_learning_source WHERE project_id=? AND kind=? AND source_key=?',pid,kind,key)
    if old and (old['suppressed'] or old['fingerprint']==sha256_json(body)):return False
    if old is None and k.one('SELECT count(*) FROM auto_learning_source WHERE project_id=?',pid)[0]>=128:return False
    if old and old['node_id']:
        previous=memory.get(k,old['node_id'])
        if previous['origin']=='user' or previous['status'] in ('verified','confirmed') or learn._snapshot(previous)!=old['snapshot']:return False
    return True


def _add(k,pid,kind,key,body,provenance):
    from . import learn
    old=k.one('SELECT * FROM auto_learning_source WHERE project_id=? AND kind=? AND source_key=?',pid,kind,key)
    fingerprint=sha256_json(body)
    if old and (old['suppressed'] or old['fingerprint']==fingerprint):return None
    if old is None and k.one('SELECT count(*) FROM auto_learning_source WHERE project_id=?',pid)[0]>=128:return None
    previous=memory.get(k,old['node_id']) if old and old['node_id'] else None
    if previous and (previous['origin']=='user' or previous['status'] in ('verified','confirmed') or learn._snapshot(previous)!=old['snapshot']):return None
    node=memory.add(k,project_id=pid,kind='lesson' if kind=='correction' else 'fact',
                    title=('User correction candidate' if kind=='correction' else 'External change candidate')+': '+(body[:75]),
                    body=body,origin='supervisor',actor='supervisor',near_duplicate=False,literal_identity=True)
    restored=False
    if node.get('already_stored'):
        history=k.one('SELECT * FROM auto_learning_entry WHERE node_id=? AND project_id=? AND kind=? AND source_key=?',node['node_id'],pid,kind,key)
        if (node['status']=='retired' and node['origin']=='supervisor' and history is not None
                and history['snapshot']==learn._snapshot(node) and (not old or not old['suppressed'])):
            # A prior exact automatic observation is current again. A user's retirement,
            # deletion or undo suppresses the source before this point; edited/verified
            # records never qualify. Do not introduce a supersedes graph cycle.
            k.run("UPDATE node SET status='candidate',retired_reason=NULL,updated_at=? WHERE node_id=?",k.now(),node['node_id'])
            node=memory.get(k,node['node_id']);restored=True
            k.emit('supervisor','memory.auto_reactivated','node',node['node_id'],source_key_hash=sha256_json(key))
        else:
            k.run('INSERT OR IGNORE INTO auto_learning_source(project_id,kind,source_key,fingerprint,provenance) VALUES(?,?,?,?,?)',pid,kind,key,fingerprint,json.dumps(provenance))
            return None
    if previous and previous['node_id']!=node['node_id'] and previous['status'] in ('candidate','retired') and not restored:
        memory.link(k,node['node_id'],previous['node_id'],'supersedes','supervisor')
    k.run('INSERT OR IGNORE INTO auto_learning_entry(node_id,project_id,kind,source_key,snapshot) VALUES(?,?,?,?,?)',node['node_id'],pid,kind,key,learn._snapshot(node))
    k.run('INSERT INTO auto_learning_source(project_id,kind,source_key,node_id,snapshot,fingerprint,suppressed,provenance) VALUES(?,?,?,?,?,?,0,?) ON CONFLICT(project_id,kind,source_key) DO UPDATE SET node_id=excluded.node_id,snapshot=excluded.snapshot,fingerprint=excluded.fingerprint,provenance=excluded.provenance',
          pid,kind,key,node['node_id'],learn._snapshot(node),fingerprint,json.dumps(provenance))
    return node


def _retire_stale(k,pid,path,current_hash):
    from . import learn
    source=k.one("SELECT * FROM auto_learning_source WHERE project_id=? AND kind='file' AND source_key=?",pid,path)
    if source is None or not source['node_id']:return
    previous=memory.get(k,source['node_id'])
    if previous['status']!='candidate' or previous['origin']=='user' or learn._snapshot(previous)!=source['snapshot']:return
    if json.loads(source['provenance']).get('after_hash')!=current_hash:
        memory._retire(k,previous['node_id'],'automatic file observation is no longer current','supervisor')
        return previous['node_id']


def _pass(k,pid,nodes):
    from . import learn
    if not nodes:return
    k.emit('supervisor','learn.pass','project',pid,**{'pass':new_id('learn'),'automatic':True,'cases':[],
        'added':[{'node_id':n['node_id'],'snapshot':learn._snapshot(n)} for n in nodes]})


class Session:
    def __init__(self):
        self.home=None;self.cwd=None;self.request=None;self.prepared=False;self.finished=False
        self.project=None;self.binding=None;self.kernel=None;self.mutating=False;self.paths=[];self.info={};self.literal=None
    def bind(self,home,request):
        if self.request is None:
            self.home=Path(home).resolve();self.cwd=Path.cwd().resolve();self.request=request
            self.literal=correction(request)
    def prepare(self,k=None,mutating=False):
        if self.prepared or self.request is None:return
        self.prepared=True;self.mutating=mutating;owned=None;reader=None
        if k is not None:self.kernel=k
        try:
            if k is None:
                reader,status=_existing(self.home)
                if reader is None:self.info={'status':status};return
                k=reader
            policy=k.policy
            if any(type(policy.get(key,1)) is not int for key in ('memory_recall_tokens','memory_max_nodes','automatic_learning')):
                self.info={'status':'unavailable'};return
            if policy['memory_recall_tokens']<=0 or policy['memory_max_nodes']<=0 or not policy.get('automatic_learning',1):self.info={'status':'off'};return
            project=projects.resolve(k,self.cwd)
            if project is None:self.info={'status':'unregistered'};return
            pid=project['project_id'];self.project=pid
            prior=k.q('SELECT * FROM auto_file_observation WHERE project_id=? ORDER BY path LIMIT 9',pid)
            new_paths=_named_paths(self.request) if mutating else []
            self.paths=list(dict.fromkeys(new_paths+[r['path'] for r in prior]))[:8]
            if not self.literal and not prior and not new_paths:
                self.project=None;return
            if reader is not None:
                reader.conn.close();reader=None;owned=Kernel(self.home);k=owned
            with k.supervisor_lock():
                project=projects.check_root(k,pid)
                if not _quiet(k,pid):self.info={'status':'writer_ambiguous'};self.project=None;return
                # The read-only eligibility check may precede another controller's update.
                # Reload watermarks only after acquiring the existing supervisor lock.
                prior=k.q('SELECT * FROM auto_file_observation WHERE project_id=? ORDER BY path LIMIT 9',pid)
                self.paths=list(dict.fromkeys(new_paths+[r['path'] for r in prior]))[:8]
                activity=_activity(k,pid);epoch=k.recovery_epoch
                self.binding=(project['root_identity'],project['revocation_epoch'],epoch)
                changes=[]
                for row in prior:
                    valid=(not row['pending'] and row['root_identity']==project['root_identity'] and row['revocation_epoch']==project['revocation_epoch'] and row['recovery_epoch']==epoch and row['activity']==activity)
                    current=_safe_read(k,project,row['path'],self.home,row['file_hash']) if valid else None
                    if current is None or current['hash']!=row['file_hash']:changes.append((row,current))
                # Kernel.tx advances the durable clock, even with no SQL mutations.
                # Ordinary unchanged questions must not enter it.
                if not changes and not (mutating and self.paths):return
                nodes=[];retired=[]
                with k.tx():
                    for row,current in changes:
                        stale=_retire_stale(k,pid,row['path'],current['hash'] if current else None)
                        if stale:retired.append(stale)
                        if current is None:
                            k.run('DELETE FROM auto_file_observation WHERE project_id=? AND path=?',pid,row['path']);continue
                        old_lines=json.loads(row['line_hashes']);delta=[]
                        for i in range(max(len(old_lines),len(current['lines']))):
                            if (old_lines[i] if i<len(old_lines) else None)!=(current['lines'][i] if i<len(current['lines']) else None):
                                delta.append({'line':i+1,'after':current['text_lines'][i][:120] if i<len(current['text_lines']) else '[line removed]'})
                                if len(delta)==2:break
                        body='Observed file change; actor and intent UNKNOWN. '+row['path']+': '+json.dumps(delta,ensure_ascii=False)
                        if not find_secret(body) and not memory._REFERENCE_OVERRIDE.search(memory.visible(body)):
                            node=_add(k,pid,'file',row['path'],body,{'path':row['path'],'before_hash':row['file_hash'],'after_hash':current['hash'],'actor':'unknown','intent':'unknown'})
                            if node:nodes.append(node)
                        k.run('UPDATE auto_file_observation SET file_hash=?,line_hashes=? WHERE project_id=? AND path=?',current['hash'],json.dumps(current['lines']),pid,row['path'])
                    _pass(k,pid,nodes)
                    if mutating and self.paths:
                        k.run('DELETE FROM auto_file_observation WHERE project_id=?',pid)
                        for path in self.paths:
                            observed=_safe_read(k,project,path,self.home)
                            if observed:self._save(k,project,path,observed,activity,pending=1)
                if nodes:self.info={'status':'captured','nodes':[{'id':n['node_id'],'hash':n['content_hash'],'kind':'file'} for n in nodes]}
                elif retired:self.info={'status':'observation_invalidated','retired':retired}
        except Exception:
            self.info={'status':'unavailable'}
        finally:
            for opened in (reader.conn if reader is not None else None, owned):
                if opened is not None:
                    try:opened.close()
                    except Exception:self.info={'status':'captured_cleanup_error' if self.info.get('nodes') else 'unavailable', 'nodes':self.info.get('nodes',[])}
    def _save(self,k,project,path,data,activity,pending=0):
        k.run('INSERT INTO auto_file_observation VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET root_identity=excluded.root_identity,revocation_epoch=excluded.revocation_epoch,recovery_epoch=excluded.recovery_epoch,activity=excluded.activity,file_hash=excluded.file_hash,line_hashes=excluded.line_hashes,pending=excluded.pending',
            project['project_id'],path,project['root_identity'],project['revocation_epoch'],k.recovery_epoch,activity,data['hash'],json.dumps(data['lines']),pending)
    def finish(self,success,*,correction_capture=False):
        if self.finished or not self.prepared:return
        self.finished=True
        if self.project is None or (not self.literal and not self.mutating):return
        k=None;owned=False
        try:
            k=self.kernel
            if k is None:k=Kernel(self.home);owned=True
            with k.supervisor_lock():
                project=projects.check_root(k,self.project)
                if not _quiet(k,self.project) or self.binding!=(project['root_identity'],project['revocation_epoch'],k.recovery_epoch):
                    success=False;correction_capture=False
                body='Explicit top-level user correction (candidate, not authority): '+self.literal if self.literal else None
                add_correction=(success or correction_capture is True) and body is not None and _would_add(k,self.project,'correction',sha256_json(self.literal),body)
                has_observations=k.one('SELECT 1 FROM auto_file_observation WHERE project_id=? LIMIT 1',self.project)
                if not add_correction and not (self.mutating and (self.paths or has_observations)):return
                with k.tx():
                    nodes=[]
                    if add_correction:
                        node=_add(k,self.project,'correction',sha256_json(self.literal),body,{'request_hash':sha256_json(self.request),'evidence':'anchored literal top-level correction'})
                        if node:nodes.append(node)
                    if self.mutating:
                        k.run('DELETE FROM auto_file_observation WHERE project_id=?',self.project)
                        if success:
                            activity=_activity(k,self.project)
                            for path in self.paths:
                                observed=_safe_read(k,project,path,self.home)
                                _retire_stale(k,self.project,path,observed['hash'] if observed else None)
                                if observed:self._save(k,project,path,observed,activity)
                    _pass(k,self.project,nodes)
                if self.mutating and success and not self.info:
                    self.info={'status':'observing','files':k.one('SELECT count(*) FROM auto_file_observation WHERE project_id=?',self.project)[0]}
                if nodes:self.info={'status':'captured','nodes':self.info.get('nodes',[])+[{'id':n['node_id'],'hash':n['content_hash'],'kind':'correction'} for n in nodes]}
        except Exception:self.info={'status':'write_failed','nodes':self.info.get('nodes',[])}
        finally:
            if k is not None and owned:
                try:k.close()
                except Exception:self.info={'status':'captured_cleanup_error' if self.info.get('nodes') else 'unavailable','nodes':self.info.get('nodes',[])}


def bind(home,request):
    current=_CURRENT.get()
    if current is not None:current.bind(home,request)


def prepare(k=None,mutating=False):
    current=_CURRENT.get()
    if current is not None:current.prepare(k,mutating)


def finish(report):
    current=_CURRENT.get()
    if current is None:return
    success=isinstance(report,dict) and not report.get('error') and report.get('exit_code',0)==0
    correction_capture=False
    if isinstance(report,dict) and type(report.get('exit_code')) is int and report['exit_code']!=0:
        lifetime=report.get('process_lifetime')
        if isinstance(lifetime,dict) and lifetime.get('cleanup_complete') is True:
            # A settled provider failure does not invalidate literal user input. This is
            # caller-owned observation only, never model text or process signal authority.
            from .supervisor import ENV_ERRORS
            correction_capture=report.get('error') in ENV_ERRORS
    current.finish(success,correction_capture=correction_capture)
    if isinstance(report,dict) and current.info:report['automatic_learning']=dict(current.info)
