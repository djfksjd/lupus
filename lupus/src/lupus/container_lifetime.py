"""Owned local Docker verifier lifetime; no name-only destructive authority.

The verifier workload remains gated. Two independently owned helpers observe the
controller and each other; daemon failures retain an unknown recovery fence.
No claim is made for simultaneous loss of both helpers or an unreachable daemon.
"""
from __future__ import annotations
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time

COMMAND_TIMEOUT=8.0
FINISH_TIMEOUT=40.0
MAX_CONTROL=65536
LABEL='lupus.owner'
SAFE_ENV={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'C','LC_ALL':'C'}


def _life():
    # Standalone -I -S helper: only the installed sibling trusted module.
    if __package__:
        from . import process_lifetime
        return process_lifetime
    import importlib.util
    spec=importlib.util.spec_from_file_location('lupus_container_process_lifetime',Path(__file__).with_name('process_lifetime.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_CONTROL:raise ValueError('unsafe container state')
        raw=stream.read(MAX_CONTROL+1)
        after=os.fstat(stream.fileno())
        if (before.st_ino,before.st_dev,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_dev,after.st_size,after.st_mtime_ns):raise ValueError('container state changed')
    if len(raw)>MAX_CONTROL:raise ValueError('container state overflow')
    return json.loads(raw)


def _write(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def _same(actual,expected,daemon=True,cid=True):
    fields=('version','docker','endpoint','name','nonce','config','state')+(('daemon',) if daemon else ())
    return all(actual.get(key)==expected.get(key) for key in fields) and (not cid or not expected.get('cid') or actual.get('cid')==expected['cid'])


def _command(desc,args):
    life=_life()
    with tempfile.TemporaryFile() as output:
        proc=life.spawn([desc['docker'],'--host',desc['endpoint'],'--config',desc['config'],*args],
            env=SAFE_ENV,cwd='/',stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
        code=None
        try:
            proc.stdin.write(b'g');proc.stdin.flush()
            deadline=time.monotonic()+COMMAND_TIMEOUT
            while time.monotonic()<deadline:
                if os.fstat(output.fileno()).st_size>MAX_CONTROL:raise RuntimeError('docker control output overflow')
                code=life.poll(proc)
                if code is not None:break
                time.sleep(.025)
            if code is None:raise TimeoutError('docker control deadline')
        finally:
            original=sys.exception()
            try:
                cleanup=life.finish(proc,cancel=code is None)
                if not cleanup['complete']:raise RuntimeError('docker control cleanup unknown')
            except BaseException as exc:
                if original is not None and not isinstance(original,Exception):
                    original.add_note('Docker control cleanup failed: '+type(exc).__name__);raise original
                raise
            finally:proc.stdin.close()
        output.seek(0);raw=output.read(MAX_CONTROL+1)
        if len(raw)>MAX_CONTROL:raise RuntimeError('docker control output overflow')
        return code,raw.decode('utf-8',errors='strict').strip()


_ACTOR='controller'
DIAGNOSTIC_LIMIT=512
_PHASES=frozenset(('daemon','lookup','inspect','rm','post_absence','state_write'))
_REASONS=frozenset(('exception','nonzero','invalid_response','identity_changed','ownership_mismatch','state_unproven','absence_unproven','still_present'))
_ACTORS=('guardian','backup','controller')
_EXCEPTION_NAMES=frozenset(('RuntimeError','OSError','TimeoutError','PermissionError','FileNotFoundError','ValueError','JSONDecodeError','UnicodeDecodeError','KeyboardInterrupt','SystemExit','TimeoutExpired','BrokenPipeError'))


def _mark(observation,phase,reason='exception',code=None):
    if observation is not None:observation.update(phase=phase,reason=reason,returncode=code)


def _record_failure(desc,operation,observation,original=None):
    """First bounded diagnostic only; never consulted by ownership decisions."""
    try:
        actor=_ACTOR
        if actor not in _ACTORS or operation not in ('state','cleanup'):return
        phase=observation.get('phase');reason=observation.get('reason')
        if phase not in _PHASES or reason not in _REASONS:return
        name=type(original).__name__ if original is not None else None
        name=name if name is None or name in _EXCEPTION_NAMES else 'OtherException'
        code=observation.get('returncode');code=code if type(code) is int else None
        value={'version':1,'operation':operation,'actor':actor,'phase':phase,'reason':reason,'exception_class':name,'returncode':code}
        raw=json.dumps(value,separators=(',',':')).encode()
        if len(raw)>DIAGNOSTIC_LIMIT:return
        path=Path(desc['state']).parent/f'diagnostic-{actor}-{operation}.json'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            if os.write(fd,raw)!=len(raw):raise OSError('incomplete diagnostic write')
        finally:
            pending=sys.exception()
            try:os.close(fd)
            except BaseException:
                if pending is not None and not isinstance(pending,Exception):raise pending
                raise
    except Exception:pass  # Observability cannot change a verdict or trigger a retry.
    except BaseException:
        if original is not None and not isinstance(original,Exception):raise original
        raise


def diagnostics(value):
    """Read first observations only, not current status or authority. No queries."""
    desc=decode(value)
    if desc is None:return []
    result=[]
    for actor in _ACTORS:
        for operation in ('state','cleanup'):
            path=Path(desc['state']).parent/f'diagnostic-{actor}-{operation}.json'
            try:
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as stream:
                    info=os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>DIAGNOSTIC_LIMIT:continue
                    raw=stream.read(DIAGNOSTIC_LIMIT+1)
                if len(raw)>DIAGNOSTIC_LIMIT:continue
                item=json.loads(raw)
                if (not isinstance(item,dict) or set(item)!={'version','operation','actor','phase','reason','exception_class','returncode'}
                        or type(item['version']) is not int or item['version']!=1 or item['actor']!=actor or item['operation']!=operation
                        or item['phase'] not in _PHASES or item['reason'] not in _REASONS
                        or item['exception_class'] not in _EXCEPTION_NAMES|{'OtherException',None}
                        or item['returncode'] is not None and type(item['returncode']) is not int):continue
                result.append(item)
            except (OSError,ValueError,TypeError):continue
    return result


def _daemon(desc,observation=None,phase='daemon'):
    _mark(observation,phase)
    code,text=_command(desc,['info','--format','{{.ID}}'])
    if code or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}',text):
        _mark(observation,phase,'nonzero' if code else 'invalid_response',code)
        raise RuntimeError('daemon identity unavailable')
    return text


def encode(desc):
    value={k:desc[k] for k in ('version','docker','endpoint','daemon','name','nonce','config','state')}
    if desc.get('cid'):value['cid']=desc['cid']
    raw=json.dumps(value,separators=(',',':')).encode()
    if len(raw)>4096:raise ValueError('container ownership too large')
    return 'v2.'+base64.urlsafe_b64encode(raw).decode()


def decode(value):
    if not isinstance(value,str) or not value.startswith('v2.') or len(value)>6000:return None
    try:
        d=json.loads(base64.b64decode(value[3:],altchars=b'-_',validate=True))
        if d['version']!=2 or not re.fullmatch(r'[0-9a-f]{48}',d['nonce']) or not re.fullmatch(r'lupus-[0-9a-f]{32}',d['name']):return None
        if not os.path.isabs(d['docker']) or not d['endpoint'].startswith('unix:///') or any(c in d['endpoint'] for c in '\n\r\x00'):return None
        if not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}',d['daemon']):return None
        if d.get('cid') and not re.fullmatch(r'[0-9a-f]{64}',d['cid']):return None
        control=Path.home()/'.lupus-container-lifetimes'
        folder=Path(d['state']).parent
        if folder.parent!=control or Path(d['config'])!=folder/'docker-config' or Path(d['state']).name!='state.json':return None
        return d
    except (ValueError,KeyError,TypeError):return None


def _objects(desc,observation=None,phase=None):
    daemon_phase=phase or 'daemon'
    if _daemon(desc,observation,daemon_phase)!=desc['daemon']:
        _mark(observation,daemon_phase,'identity_changed');raise RuntimeError('daemon identity changed')
    selector='id='+desc['cid'] if desc.get('cid') else 'label='+LABEL+'='+desc['nonce']
    _mark(observation,phase or 'lookup')
    code,text=_command(desc,['ps','-aq','--no-trunc','--filter',selector])
    ids=text.splitlines()
    if code or len(ids)>1 or any(not re.fullmatch(r'[0-9a-f]{64}',cid) for cid in ids):
        _mark(observation,phase or 'lookup','nonzero' if code else 'invalid_response',code);raise RuntimeError('container lookup unknown')
    if not ids:return []
    if desc.get('cid') and ids!=[desc['cid']]:
        _mark(observation,phase or 'lookup','ownership_mismatch');raise RuntimeError('container CID filter mismatch')
    _mark(observation,phase or 'inspect')
    code,text=_command(desc,['inspect',ids[0]])
    if code:
        _mark(observation,phase or 'inspect','nonzero',code);raise RuntimeError('container inspect unknown')
    _mark(observation,phase or 'inspect','invalid_response',code)
    rows=json.loads(text)
    if not isinstance(rows,list) or len(rows)!=1:raise RuntimeError('container inspect shape')
    row=rows[0]
    if row.get('Id')!=ids[0] or row.get('Name','').lstrip('/')!=desc['name'] or row.get('Config',{}).get('Labels',{}).get(LABEL)!=desc['nonce']:
        _mark(observation,phase or 'inspect','ownership_mismatch');raise RuntimeError('container ownership mismatch')
    return ids


def state(value):
    desc=decode(value)
    if desc is None:return 'unknown'
    observation={};failure=None
    try:
        ids=_objects(desc,observation)
        if ids:return 'live'
        # No CID yet can mean a lost, still-in-flight create, not proven absence.
        if not desc.get('cid'):
            _mark(observation,'post_absence')
            saved=_read(desc['state'])
            if not _same(saved,desc) or saved.get('phase') not in ('not_created','created','cleanup_complete'):
                _mark(observation,'post_absence','state_unproven')
            else:return 'gone'
        else:return 'gone'
    except BaseException as exc:failure=exc
    # Outside the observation try: a fresh cancellation during diagnostics is
    # propagated once, not recaptured as another cleanup/diagnostic attempt.
    _record_failure(desc,'state',observation,failure)
    if failure is not None and not isinstance(failure,Exception):raise failure
    return 'unknown'


def cleanup(value):
    desc=decode(value)
    if desc is None:return False
    observation={};failure=None
    def attempt():
        ids=_objects(desc,observation)
        if not ids:
            _mark(observation,'post_absence')
            result=state(value)=='gone'
            if not result:_mark(observation,'post_absence','absence_unproven')
            return result
        _mark(observation,'rm')
        code,_=_command(desc,['rm','-f',ids[0]])
        if code:
            _mark(observation,'rm','nonzero',code);return False
        if _daemon(desc,observation,'post_absence')!=desc['daemon']:
            _mark(observation,'post_absence','identity_changed');return False
        if _objects(desc,observation,'post_absence'):
            _mark(observation,'post_absence','still_present');return False
        _mark(observation,'state_write')
        _write(desc['state'],{**desc,'cid':ids[0],'phase':'cleanup_complete'})
        return True
    try:
        if attempt():return True
    except BaseException as exc:failure=exc
    _record_failure(desc,'cleanup',observation,failure)
    if failure is not None and not isinstance(failure,Exception):raise failure
    return False


def _eof(fd):
    return bool(select.select([fd],[],[],0)[0]) and os.read(fd,1)==b''


def _stop_suspended_peer(folder,nonce,peer_fd):
    """Only a matching live helper registered before create may be signalled."""
    try:
        if _eof(peer_fd):return  # Closed peer-only heartbeat is no live-identity authority.
        registered=_read(folder/'helpers.json')
        if registered.get('nonce')!=nonce:return
        for peer in registered['helpers']:
            pid=peer['pid']
            if pid==os.getpid():continue
            if _life()._identity(pid)!=peer['start'] or os.getpgid(pid)!=pid or os.getsid(pid)!=pid:continue
            status=subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],env=SAFE_ENV,cwd='/',capture_output=True,text=True,timeout=.5)
            if status.returncode==0 and len(status.stdout)<128 and status.stdout.strip().startswith('T'):
                if not _eof(peer_fd) and _life()._identity(pid)==peer['start'] and os.getpgid(pid)==pid:os.killpg(pid,signal.SIGKILL)
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):pass


def helper(mode,folder,life_fd,peer_fd,heartbeat_fd,gate_fd):
    global _ACTOR
    _ACTOR=mode
    for fd in (life_fd,peer_fd,heartbeat_fd,gate_fd):os.set_inheritable(fd,False)
    folder=Path(folder);config=_read(folder/'intent.json');desc=config['descriptor']
    receipt=folder/(mode+'-cleanup.json')
    try:
        if mode=='guardian':
            desc['daemon']=_daemon(desc)
            _write(desc['state'],{**desc,'phase':'not_created'})
            _write(folder/'bound.json',desc)
            # Callback registration must commit before the create gate opens.
            if os.read(gate_fd,1)!=b'g':return
            with (folder/'operation.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                _write(desc['state'],{**desc,'phase':'create_pending'})
                code,text=_command(desc,['create','--name',desc['name'],'--label','lupus=verifier','--label',LABEL+'='+desc['nonce'],*config['create_args']])
                if code or not re.fullmatch(r'[0-9a-f]{64}',text):raise RuntimeError('container create result unknown')
                desc['cid']=text
                if _objects(desc)!=[text]:raise RuntimeError('created container identity unknown')
                _write(desc['state'],{**desc,'phase':'created'})
                _write(folder/'ready.json',desc)
        while not _eof(life_fd) and not _eof(peer_fd):time.sleep(.025)
    except BaseException as exc:
        _write(folder/(mode+'-error.json'),{'type':type(exc).__name__})
    finally:
        _stop_suspended_peer(folder,desc['nonce'],peer_fd)
        try:
            bound=_read(folder/'bound.json')
            if not _same(bound,desc,daemon=desc['daemon']!='pending',cid=False):raise RuntimeError('container bound identity changed')
            # bound is immutable pre-create intent, while this guardian may
            # already hold a validated create result. Do not regress that CID.
            if desc.get('cid'):bound['cid']=desc['cid']
            with (folder/'operation.lock').open('a') as lock:
                deadline=time.monotonic()+20
                while True:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=deadline:raise TimeoutError('container operation still in flight')
                        time.sleep(.025)
                try:
                    saved=_read(bound['state'])
                    if not _same(saved,bound):raise RuntimeError('container state identity changed')
                    if saved.get('cid'):bound['cid']=saved['cid']
                except FileNotFoundError:pass
                complete=cleanup(encode(bound))
                if complete:_write(bound['state'],{**bound,'phase':'cleanup_complete'})
                _write(receipt,{'complete':complete,'nonce':bound['nonce']})
        except BaseException as exc:
            _write(receipt,{'complete':False,'error':type(exc).__name__,'nonce':desc['nonce']})
        for fd in (life_fd,peer_fd,heartbeat_fd,gate_fd):
            try:os.close(fd)
            except OSError:pass


class Lease:
    def __init__(self,docker,create_args):
        import secrets
        if os.environ.get('DOCKER_CONTEXT'):
            raise RuntimeError('container verifier does not load host Docker contexts; select an explicit local DOCKER_HOST and unset DOCKER_CONTEXT')
        endpoint=os.environ.get('DOCKER_HOST','unix:///var/run/docker.sock')
        if not endpoint.startswith('unix:///') or any(c in endpoint for c in '\n\r\x00'):
            raise RuntimeError('container verifier requires an explicit local Unix Docker endpoint')
        control=Path.home()/'.lupus-container-lifetimes'
        if control.is_symlink():raise RuntimeError('container control root is a symlink')
        control.mkdir(mode=0o700,exist_ok=True)
        info=control.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise RuntimeError('container control root permissions unsafe')
        self.folder=Path(tempfile.mkdtemp(prefix='lease-',dir=control))
        (self.folder/'docker-config').mkdir(mode=0o700)
        self.desc={'version':2,'docker':os.path.realpath(docker),'endpoint':endpoint,'daemon':'pending','name':'lupus-'+secrets.token_hex(16),'nonce':secrets.token_hex(24),'config':str(self.folder/'docker-config'),'state':str(self.folder/'state.json')}
        _write(self.folder/'intent.json',{'descriptor':self.desc,'create_args':create_args})
        self.children=[];self.writer=None;self.gate=None;self.registered=False;self.registered_pid=None;self.registered_pids=[]
        self.fd_identities={}
        fds=[]
        try:
            pairs=[]
            for _ in range(4):
                pair=os.pipe();pairs.append(pair);fds.extend(pair)
                for descriptor in pair:
                    info=os.fstat(descriptor);self.fd_identities[descriptor]=(info.st_dev,info.st_ino,info.st_mode)
            life_r,life_w=pairs[0];a_r,a_w=pairs[1];b_r,b_w=pairs[2];gate_r,gate_w=pairs[3]
            script=str(Path(__file__).resolve())
            for mode,peer,heart,gate in [('guardian',b_r,a_w,gate_r),('backup',a_r,b_w,gate_r)]:
                child=subprocess.Popen([sys.executable,'-I','-S',script,mode,str(self.folder),str(life_r),str(peer),str(heart),str(gate)],pass_fds=(life_r,peer,heart,gate),env=SAFE_ENV,cwd='/',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
                self.children.append(child)
            self.writer=life_w;self.gate=gate_w
            fds.remove(life_w);fds.remove(gate_w)
        except BaseException as original:
            for fd in fds:os.close(fd)
            fds=[]
            for child in self.children:
                try:child.wait(timeout=FINISH_TIMEOUT)
                except subprocess.TimeoutExpired:child.kill();child.wait()
                except Exception as exc:original.add_note('Container helper cleanup failed: '+type(exc).__name__)
            raise
        finally:
            for fd in fds:os.close(fd)

    def _wait_file(self,name,timeout=30):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if (self.folder/name).exists():return _read(self.folder/name)
            if any(c.poll() is not None for c in self.children):raise RuntimeError('container guardian lost')
            if (self.folder/'guardian-error.json').exists():raise RuntimeError('container preparation failed')
            time.sleep(.025)
        raise TimeoutError('container preparation deadline')

    def prepare(self,on_spawn):
        bound=self._wait_file('bound.json')
        if not _same(bound,self.desc,daemon=False) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}',bound.get('daemon','')):raise RuntimeError('container bound identity changed')
        self.desc=bound
        _write(self.folder/'helpers.json',{'nonce':self.desc['nonce'],'helpers':[{'pid':child.pid,'start':_life()._identity(child.pid)} for child in self.children]})
        for child in self.children:
            if on_spawn:on_spawn(child.pid,':container:'+encode(self.desc))
            self.registered_pids.append(child.pid)
        self.registered=True;self.registered_pid=self.children[0].pid
        os.write(self.gate,b'g');os.close(self.gate);self.gate=None
        ready=self._wait_file('ready.json')
        if not _same(ready,self.desc) or not re.fullmatch(r'[0-9a-f]{64}',ready.get('cid','')):raise RuntimeError('container ready identity changed')
        self.desc=ready
        return [self.desc['docker'],'--host',self.desc['endpoint'],'--config',self.desc['config'],'start','--attach',self.desc['cid']]

    def healthy(self):
        return all(c.poll() is None for c in self.children)

    def finish(self):
        deadline=time.monotonic()+FINISH_TIMEOUT
        pending=None;ordinary=None
        def remember(exc):
            nonlocal pending,ordinary
            if isinstance(exc,Exception):ordinary=ordinary or exc
            else:pending=pending or exc
        if self.desc['daemon']=='pending':
            try:
                bound=_read(self.folder/'bound.json')
                if _same(bound,self.desc,daemon=False) and decode(encode(bound)) is not None:self.desc=bound
            except (OSError,ValueError,KeyError):pass
            except BaseException as exc:remember(exc)
        for attribute in ('gate','writer'):
            fd=getattr(self,attribute)
            while fd is not None and time.monotonic()<deadline:
                try:
                    info=os.fstat(fd)
                    if (info.st_dev,info.st_ino,info.st_mode)!=self.fd_identities[fd]:
                        # A prior close completed before cancellation and another
                        # owner reused this number. Never close that resource.
                        setattr(self,attribute,None);break
                    os.close(fd);setattr(self,attribute,None);break
                except OSError as exc:
                    if exc.errno==9:setattr(self,attribute,None)
                    else:remember(exc)
                    break
                except BaseException as exc:remember(exc)
        for child in self.children:
            while child.returncode is None and time.monotonic()<deadline:
                try:child.wait(timeout=min(.1,max(.01,deadline-time.monotonic())))
                except subprocess.TimeoutExpired:continue
                except BaseException as exc:
                    remember(exc)
                    if isinstance(exc,Exception):break
            if child.returncode is None:
                try:child.kill()
                except BaseException as exc:remember(exc)
                kill_deadline=time.monotonic()+2
                while child.returncode is None and time.monotonic()<kill_deadline:
                    try:child.wait(timeout=.1)
                    except subprocess.TimeoutExpired:continue
                    except BaseException as exc:
                        remember(exc)
                        if isinstance(exc,Exception):break
            if child.returncode is None:ordinary=ordinary or RuntimeError('container helper cleanup unconfirmed')
        if pending is not None:raise pending
        if ordinary is not None:raise ordinary
        certificates=[]
        for mode in ('guardian','backup'):
            try:certificates.append(_read(self.folder/(mode+'-cleanup.json')))
            except (OSError,ValueError):pass
        # A surviving helper's exact-ownership absence proof is sufficient; the
        # workload caller separately treats helper loss as failed execution.
        return any(c.get('complete') is True and c.get('nonce')==self.desc['nonce'] for c in certificates) and state(encode(self.desc))=='gone'


if __name__=='__main__':
    mode,folder,*fds=sys.argv[1:]
    helper(mode,folder,*(int(fd) for fd in fds))
