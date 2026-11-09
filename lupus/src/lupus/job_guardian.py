"""One local lifecycle guardian for a new detached job; no model or task policy."""
import json
import stat
import os
import re
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

from . import jobs,job_context
from .kernel import Kernel
from .util import group_alive,proc_start

RESULT_FD='LUPUS_JOB_RESULT_FD'
CONTROL_FD='LUPUS_JOB_CONTROL_FD'
TERM_GRACE=2.0
KILL_GRACE=2.0
CANCEL_GRACE=20.0
MAX_RESULT=4096
MAX_EVENTS=8192
POLL=.05


def latest(k,job_id,type_):
    row=k.one('SELECT payload FROM event WHERE aggregate_id=? AND type=? ORDER BY seq DESC LIMIT 1',job_id,type_)
    return json.loads(row['payload']) if row else None


def matching(pid,start):
    if type(pid) is not int or pid<=0 or not isinstance(start,str) or not start:return False
    try:return proc_start(pid)==start and os.getpgid(pid)==pid and os.getsid(pid)==pid
    except (ProcessLookupError,PermissionError):return False


def _signal(pid,start,sig):
    if not matching(pid,start):return False
    try:os.killpg(pid,sig);return True
    except (ProcessLookupError,PermissionError):return False


def _registrations(k,job_id,nonce):
    rows=k.q("SELECT type,payload FROM event WHERE aggregate_id=? AND type IN ('job.process_registered','job.process_discharged') ORDER BY seq LIMIT ?",job_id,MAX_EVENTS+1)
    if len(rows)>MAX_EVENTS:raise RuntimeError('registration_limit')
    pending={}
    for row in rows:
        payload=json.loads(row['payload'])
        if payload.get('nonce')!=nonce:continue
        key=(payload['pid'],payload['start'],payload['kind'])
        if row['type']=='job.process_registered':pending[key]=payload
        else:pending.pop(key,None)
    return list(pending.values())


def _registered_state(reg):
    # A container is only discharged by the existing verified controller cleanup.
    if ':container:' in reg.get('purpose',''):
        from .container_lifetime import state,cleanup
        value=reg['purpose'].split(':container:',1)[1]
        observed=state(value)
        if observed=='live':observed='gone' if cleanup(value) else 'unknown'
        if observed!='gone':return 'container_unknown'
    if not group_alive(reg['pid']):return 'gone'
    return 'live' if matching(reg['pid'],reg['start']) else 'ownership_unknown'


def _clean_registered(k,job_id,nonce):
    pending=_registrations(k,job_id,nonce)
    for sig,grace in ((signal.SIGTERM,TERM_GRACE),(signal.SIGKILL,KILL_GRACE)):
        for reg in pending:
            if _registered_state(reg)=='live':_signal(reg['pid'],reg['start'],sig)
        deadline=time.monotonic()+grace
        while time.monotonic()<deadline:
            if all(_registered_state(r)!='live' for r in pending):break
            time.sleep(POLL)
    return [dict(pid=r['pid'],kind=r['kind'],state=_registered_state(r)) for r in pending if _registered_state(r)!='gone']


def _only_anchor(pid):
    try:
        result=subprocess.run(['/bin/ps','-A','-o','pid=,pgid=,stat='],capture_output=True,text=True,timeout=.5)
        if result.returncode or len(result.stdout)>1000000:return False
        members=[]
        for line in result.stdout.splitlines():
            parts=line.split()
            if len(parts)==3 and parts[1]==str(pid) and not parts[2].startswith('Z'):members.append(int(parts[0]))
        return members==[pid]
    except (OSError,ValueError,subprocess.TimeoutExpired):return False


def _clean_anchor(process,start,control,result_received=True):
    if process.poll() is not None:return not group_alive(process.pid)
    if not _signal(process.pid,start,signal.SIGTERM):return not group_alive(process.pid)
    deadline=time.monotonic()+TERM_GRACE
    while time.monotonic()<deadline:
        if process.poll() is not None:return not group_alive(process.pid)
        if result_received and _only_anchor(process.pid):
            try:os.write(control,b'x')
            except OSError:pass
            try:process.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:break
            return not group_alive(process.pid)
        time.sleep(POLL)
    # The post-result leader ignores TERM and remains the identity anchor for KILL.
    if not _signal(process.pid,start,signal.SIGKILL):return not group_alive(process.pid)
    try:process.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:return False
    return not group_alive(process.pid)


def _event(k,jid,type_,**payload):
    with k.tx():k.emit('supervisor',type_,'job',jid,**payload)


def _parse_result(buffer,job_id,nonce):
    if len(buffer)>MAX_RESULT or buffer.count(b'\n')!=1 or not buffer.endswith(b'\n'):raise RuntimeError('result_framing')
    candidate=json.loads(buffer)
    if (not isinstance(candidate,dict) or set(candidate)!={'job_id','nonce','version','exit_code','error'}
            or candidate['job_id']!=job_id or candidate['nonce']!=nonce or type(candidate['version']) is not int or candidate['version']!=1
            or type(candidate['exit_code']) not in (int,type(None))
            or candidate['error'] is not None and (not isinstance(candidate['error'],str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}',candidate['error']) is None)):
        raise RuntimeError('result_identity')
    return candidate


def run(home,job_id):
    if not jobs.released():return 111
    k=Kernel(home);process=None;control=None;read_fd=None;start=None;nonce=None
    stop=[False];actual=None;reason=None;complete=False;unknown=[];buffer=b''
    previous={sig:signal.signal(sig,lambda *_:stop.__setitem__(0,True)) for sig in (signal.SIGTERM,signal.SIGINT)}
    try:
        binding=latest(k,job_id,'job.guardian_started')
        if not binding or binding['pid']!=os.getpid() or binding['start']!=proc_start(os.getpid()):raise RuntimeError('guardian_identity')
        nonce=binding['nonce']
        lifetime_fd=int(os.environ.pop('LUPUS_JOB_LIFETIME_FD'))
        os.set_inheritable(lifetime_fd,False)
        info=os.fstat(lifetime_fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(binding['lifetime_dev'],binding['lifetime_ino']):raise RuntimeError('guardian_lifetime_identity')
        # Raw FD intentionally remains open until OS process exit, never inherited by children.
        command=jobs.command_of(k,job_id)
        if not command or command[0] not in jobs.ALLOWED:raise RuntimeError('command_not_allowed')
        read_fd,result_write=os.pipe();control_read,control=os.pipe();gate_read,gate_write=os.pipe()
        try:
            process=subprocess.Popen([sys.executable,'-m','lupus','--home',str(home),'job-run',job_id],
                stdin=subprocess.DEVNULL,start_new_session=True,
                env={**os.environ,jobs.GATE_FD:str(gate_read),RESULT_FD:str(result_write),CONTROL_FD:str(control_read)},
                pass_fds=(gate_read,result_write,control_read),close_fds=True,cwd=str(home))
        finally:
            os.close(result_write);os.close(control_read);os.close(gate_read)
        start=proc_start(process.pid)
        if start is None:raise RuntimeError('inner_identity')
        try:
            _event(k,job_id,'job.inner_started',pid=process.pid,start=start,nonce=nonce)
            os.write(gate_write,b'g')
        finally:os.close(gate_write)
        buffer=b'';cancel_at=None
        while actual is None:
            if stop[0] and cancel_at is None:
                cancel_at=time.monotonic()
                if matching(process.pid,start):os.kill(process.pid,signal.SIGTERM)
            if cancel_at is not None and time.monotonic()-cancel_at>=CANCEL_GRACE:
                reason='cancel_grace_exhausted';break
            ready,_,_=select.select([read_fd],[],[],POLL)
            if ready:
                chunk=os.read(read_fd,MAX_RESULT+1-len(buffer));buffer+=chunk
                if len(buffer)>MAX_RESULT:raise RuntimeError('result_limit')
                if b'\n' in buffer:
                    actual=_parse_result(buffer,job_id,nonce)
                    _event(k,job_id,'job.command_result',**actual);break
                if not chunk:reason='result_missing';break
            if process.poll() is not None and not ready:reason='inner_exited_without_result';break
    except BaseException as exc:
        reason=reason or type(exc).__name__
    finally:
        try:
            if process is not None:
                try:unknown=_clean_registered(k,job_id,nonce)
                except Exception:unknown=[{'state':'registration_unavailable'}]
                anchor_gone=_clean_anchor(process,start,control,actual is not None) if start is not None and control is not None else False
                # Inner controller cannot register further gated groups once its anchor is gone.
                try:unknown=_clean_registered(k,job_id,nonce)
                except Exception:unknown=[{'state':'registration_unavailable'}]
                complete=anchor_gone and not unknown
                if actual is None and read_fd is not None:
                    # Cleanup/cancellation may finish main and publish while the guardian
                    # is stopping groups. Preserve that late valid result, never a prefix.
                    try:
                        while select.select([read_fd],[],[],0)[0]:
                            chunk=os.read(read_fd,MAX_RESULT+1-len(buffer));buffer+=chunk
                            if not chunk or len(buffer)>MAX_RESULT:break
                        if buffer:
                            actual=_parse_result(buffer,job_id,nonce)
                            _event(k,job_id,'job.command_result',**actual)
                    except Exception:pass
            else:complete=True
            code=actual.get('exit_code') if actual else None
            payload={'nonce':nonce,'complete':complete,'reason':reason,'unknown':unknown[:32],
                     'command_exit_code':code,'inner_exit_code':process.returncode if process else None}
            # Actual result and cleanup outcome are separate, even if cleanup failed.
            if actual is None:_event(k,job_id,'job.command_result',version=1,job_id=job_id,nonce=nonce,exit_code=None,error=reason or 'missing')
            with k.tx():
                k.emit('supervisor','job.cleanup','job',job_id,**payload)
                if complete:jobs.finished(k,job_id,code if type(code) is int else 1)
        except BaseException:
            print(json.dumps({'job_id':job_id,'command_result':actual,'cleanup_complete':False,'error':'cleanup_journal_unavailable'}),file=sys.stderr,flush=True)
        finally:
            for fd in (read_fd,control):
                if fd is not None:
                    try:os.close(fd)
                    except OSError:pass
            for sig,handler in previous.items():signal.signal(sig,handler)
            k.close()
    return 0 if complete else 1


def inner(home,job_id,main):
    if not jobs.released():return 111
    result_fd=os.environ.pop(RESULT_FD,None);control_fd=os.environ.pop(CONTROL_FD,None)
    if result_fd is None or control_fd is None:return 111
    result_fd,control_fd=int(result_fd),int(control_fd)
    os.set_inheritable(result_fd,False);os.set_inheritable(control_fd,False)
    k=Kernel(home)
    try:
        binding=latest(k,job_id,'job.inner_started')
        if not binding or binding['pid']!=os.getpid() or binding['start']!=proc_start(os.getpid()):return 111
        command=jobs.command_of(k,job_id)
    finally:k.close()
    token=job_context.CURRENT.set({'home':str(Path(home).resolve()),'job_id':job_id,'nonce':binding['nonce']})
    published=threading.Event();lost=threading.Event()
    def terminate(*_):raise SystemExit(143)
    old=signal.signal(signal.SIGTERM,terminate)
    def own_group_kill():
        if os.getpgrp()==os.getpid() and os.getsid(0)==os.getpid():os.killpg(os.getpid(),signal.SIGKILL)
    def cleanup_registered_after_loss():
        connection=None
        try:
            connection=Kernel(home)
            unknown=_clean_registered(connection,job_id,binding['nonce'])
            _event(connection,job_id,'job.guardian_lost',nonce=binding['nonce'],unknown=unknown[:32],cleanup_complete=False)
        except Exception:pass
        finally:
            if connection is not None:connection.close()
    def watchdog():
        try:
            while True:
                byte=os.read(control_fd,1)
                if byte==b'x' and published.is_set():os._exit(0)
                if byte==b'':
                    lost.set()
                    if not published.is_set():
                        os.kill(os.getpid(),signal.SIGTERM)
                        # Main-thread cleanup owns budgets/fences. Never publish success here.
                        published.wait(CANCEL_GRACE)
                    cleanup_registered_after_loss()
                    own_group_kill();return
        except OSError:
            lost.set();os.kill(os.getpid(),signal.SIGTERM)
    threading.Thread(target=watchdog,daemon=True).start()
    code=None;error=None
    try:code=main(['--home',str(home),*command])
    except SystemExit as exc:code=exc.code if type(exc.code) is int else 1;error='interrupted'
    except KeyboardInterrupt:code=130;error='interrupted'
    except BaseException as exc:code=1;error=type(exc).__name__
    finally:
        signal.signal(signal.SIGTERM,lambda *_:None)
        published.set();sys.stdout.flush();sys.stderr.flush()
        payload=json.dumps({'version':1,'job_id':job_id,'nonce':binding['nonce'],'exit_code':code,'error':error}).encode()+b'\n'
        try:
            if len(payload)>MAX_RESULT:raise ValueError('result_limit')
            os.write(result_fd,payload)
            os.close(result_fd)
        except (OSError,ValueError):
            lost.set()
            cleanup_registered_after_loss()
            own_group_kill()
        if lost.is_set():
            cleanup_registered_after_loss()
            own_group_kill()
        # Keep this live group leader as authority until the external guardian cleans it.
        while True:signal.pause()
        signal.signal(signal.SIGTERM,old);job_context.CURRENT.reset(token)
