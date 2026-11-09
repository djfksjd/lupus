"""Trusted local group lifetime. Executed with -I -S; never imports project code.

The controller owns the lifetime writer; an external monitor owns its reader.
A gate/anchor remains the recorded group leader until cleanup. This covers the
anchored process group only, not setsid escapes, remote jobs or containers.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

TERM_GRACE = 2.0
KILL_GRACE = 2.0
POLL = .025
MAX_STATUS = 8192
MONITOR_ENV = {"PATH":"/usr/bin:/bin:/usr/sbin:/sbin", "LANG":"C", "LC_ALL":"C"}


def _payload_pid(value):
    """Optional diagnostic only: never process ownership or signal authority."""
    return value if type(value) is int and 0 < value <= 2_147_483_647 else None


def _send(fd, message):
    data = json.dumps(message,separators=(',',':')).encode()+b'\n'
    if len(data)>1024:raise ValueError('lifetime message too large')
    try:os.write(fd,data)
    except BrokenPipeError:pass


def _identity(pid):
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=.3,env=MONITOR_ENV,cwd='/')
    value=result.stdout.strip()
    return value if result.returncode==0 and 0<len(value)<128 else None


def _signal_anchor(pid, expected, sig):
    if not expected or _identity(pid)!=expected or os.getpgid(pid)!=pid:
        raise RuntimeError('anchor identity not current')
    os.killpg(pid,sig)


def owns_current_group():
    """Legacy leader or direct payload of the live, exact trusted isolated anchor.

    No environment flag grants this authority. Only a bounded OS command header is
    inspected; request/config arguments are never returned, logged or retained.
    """
    pid,group,parent=os.getpid(),os.getpgrp(),os.getppid()
    if group==pid:return True
    try:
        session=os.getsid(0)
        if parent!=group or parent<=1 or os.getpgid(parent)!=group or os.getsid(parent)!=session:return False
        identity=_identity(parent)
        if identity is None:return False
        executable=subprocess.run(['/bin/ps','-p',str(pid),'-o','comm='],capture_output=True,text=True,timeout=.3,env=MONITOR_ENV,cwd='/').stdout.strip()
        parent_executable=subprocess.run(['/bin/ps','-p',str(parent),'-o','comm='],capture_output=True,text=True,timeout=.3,env=MONITOR_ENV,cwd='/').stdout.strip()
        if not executable.startswith('/') or len(executable)>4096 or parent_executable!=executable:return False
        # macOS Python may exec Python.app, so sys.executable's venv spelling is not the OS argv0.
        prefix=(executable+' -I -S '+str(Path(__file__).resolve())+' anchor ').encode()
        process=subprocess.Popen(['/bin/ps','-ww','-p',str(parent),'-o','command='],stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env={**MONITOR_ENV,'LC_ALL':'en_US.UTF-8','LANG':'en_US.UTF-8'},cwd='/')
        try:
            if not select.select([process.stdout],[],[],.3)[0]:return False
            header=os.read(process.stdout.fileno(),len(prefix)+64)
        finally:
            process.stdout.close()
            try:process.wait(timeout=.3)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        import re
        match=re.match(re.escape(prefix)+rb'([0-9]{1,10}) ([0-9]{1,10}) ',header)
        if match is None or min(map(int,match.groups()))<3 or match.group(1)==match.group(2):return False
        return (os.getppid()==parent and os.getpgrp()==group and os.getsid(0)==session
                and os.getsid(parent)==session and _identity(parent)==identity)
    except (OSError,ValueError,subprocess.TimeoutExpired):return False


def _members(pgid):
    result=subprocess.run(['/bin/ps','-A','-o','pid=,pgid=,stat='],capture_output=True,text=True,timeout=.3,env=MONITOR_ENV,cwd='/')
    if result.returncode or len(result.stdout)>2000000:raise RuntimeError('group observation unavailable')
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts:=line.split())==3 and parts[1]==str(pgid) and not parts[2].startswith('Z')]


def anchor(control, events, argv):
    # These private FDs must never survive exec into a model/tool or grandchild.
    for fd in (control,events):os.set_inheritable(fd,False)
    if os.getpgrp()!=os.getpid():return 111
    result=[None];ready=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:None)
    signal.signal(signal.SIGINT,lambda *_:None)
    signal.signal(signal.SIGHUP,lambda *_:None)
    def watch():
        try:
            while True:
                byte=os.read(control,1)
                if byte==b'x' and ready.is_set():
                    code=result[0]
                    os._exit(code if code>=0 else 128-code)
                if byte==b'':break
        except OSError:pass
        # Monitor loss: this still-live anchor can authorize its own group.
        try:os.killpg(os.getpid(),signal.SIGTERM)
        except OSError:pass
        time.sleep(.5)
        os.killpg(os.getpid(),signal.SIGKILL)
    threading.Thread(target=watch,daemon=True).start()
    gate_fd=os.environ.pop('LUPUS_GATE_FD',None)
    fd=int(gate_fd) if gate_fd is not None else 0
    go=os.read(fd,1)
    if gate_fd is not None:os.close(fd)
    payload_pid=None
    if go!=b'g':code=111
    else:
        try:
            process=subprocess.Popen(argv,stdin=None if gate_fd is not None else subprocess.DEVNULL,
                                     close_fds=True)
            payload_pid=process.pid
            code=process.wait()
        except OSError:code=127
    result[0]=code;ready.set()
    _send(events,{'type':'result','exit_code':code,'payload_pid':payload_pid})
    while True:signal.pause()


def monitor(lifetime, control, events, status, pid, expected):
    for fd in (lifetime,control,events,status):os.set_inheritable(fd,False)
    pending=b'';actual=None;payload_pid=None;parent_lost=False;anchor_lost=False;leftover=False
    def receive(timeout):
        nonlocal pending,actual,payload_pid,parent_lost,anchor_lost
        readable,_,_=select.select(([lifetime] if not parent_lost else [])+[events],[],[],timeout)
        # Observe result before EOF when both become readable.
        if events in readable:
            chunk=os.read(events,2048)
            if not chunk:anchor_lost=True
            else:
                pending+=chunk
                if len(pending)>2048:raise RuntimeError('anchor event overflow')
                if b'\n' in pending:
                    line,pending=pending.split(b'\n',1);value=json.loads(line)
                    if actual is not None or value.get('type')!='result' or type(value.get('exit_code')) is not int:
                        raise RuntimeError('invalid anchor result')
                    actual=value['exit_code'];payload_pid=_payload_pid(value.get('payload_pid'));_send(status,{'type':'result','exit_code':actual,'payload_pid':payload_pid})
        if lifetime in readable and os.read(lifetime,1)==b'':parent_lost=True
    try:
        while actual is None and not parent_lost and not anchor_lost:receive(POLL)
        if anchor_lost:
            # No live authority remains; never signal a potentially reused PGID.
            complete=not _members(pid)
            _send(status,{'type':'cleanup','complete':complete,'exit_code':actual,'payload_pid':payload_pid,
                          'leftover':False,'reason':'anchor_lost'})
            return 0 if complete else 1
        members=_members(pid)
        if pid not in members:raise RuntimeError('anchor identity unavailable')
        leftover=actual is not None and any(member!=pid for member in members)
        # The anchor catches TERM, while exec resets the payload's caught handlers.
        _signal_anchor(pid,expected,signal.SIGTERM)
        deadline=time.monotonic()+TERM_GRACE
        while time.monotonic()<deadline:
            receive(POLL)
            if anchor_lost:break
            members=_members(pid)
            if members==[pid] and actual is not None:
                os.write(control,b'x')
                deadline=time.monotonic()+KILL_GRACE
                while not anchor_lost and time.monotonic()<deadline:receive(POLL)
                break
        if not anchor_lost:
            # Anchor cannot voluntarily exit before our x; signal the live anchor group once.
            if pid not in _members(pid):raise RuntimeError('anchor identity lost before kill')
            _signal_anchor(pid,expected,signal.SIGKILL)
            deadline=time.monotonic()+KILL_GRACE
            while not anchor_lost and time.monotonic()<deadline:receive(POLL)
        complete=anchor_lost and not _members(pid)
        _send(status,{'type':'cleanup','complete':complete,'exit_code':actual,'payload_pid':payload_pid,
                      'leftover':leftover,'reason':'controller_lost' if parent_lost else 'result'})
        return 0 if complete else 1
    except BaseException as exc:
        _send(status,{'type':'cleanup','complete':False,'exit_code':actual,'payload_pid':payload_pid,
                      'leftover':leftover,'reason':type(exc).__name__})
        return 1
    finally:
        # Closing this writer also activates the anchor's independent fallback.
        os.close(control)


class Lifetime:
    def __init__(self,process,monitor_process,writer,status):
        self.process=process;self.monitor=monitor_process;self.writer=writer;self.status=status
        self.buffer=b'';self.actual=None;self.payload_pid=None;self.result_seen=False;self.cleanup=None;self.closed=False
        self._finish_error=None
    def drain(self,timeout=0):
        if self.status is None:return
        if not select.select([self.status],[],[],timeout)[0]:return
        chunk=os.read(self.status,4096)
        if not chunk:return
        self.buffer+=chunk
        if len(self.buffer)>MAX_STATUS:raise RuntimeError('lifetime status overflow')
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1);event=json.loads(line)
            if event.get('type')=='result' and type(event.get('exit_code')) is int:
                self.actual=event['exit_code'];self.payload_pid=_payload_pid(event.get('payload_pid'));self.result_seen=True
            elif event.get('type')=='cleanup':self.cleanup=event
            else:raise RuntimeError('invalid lifetime status')
    def wait(self,timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        while True:
            self.drain(0)
            if self.result_seen:return self.actual
            if self.process.poll() is not None:return self.process.returncode
            remaining=None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining<=0:raise subprocess.TimeoutExpired(self.process.args,timeout)
            self.drain(min(POLL,remaining) if remaining is not None else POLL)
    def finish(self,cancel=False):
        if self.closed:
            if self._finish_error is not None:raise self._finish_error
            return self.cleanup
        pending = None
        cleanup_error = None
        notes = []
        deadline = time.monotonic()+TERM_GRACE+KILL_GRACE+2
        # Keep the original six-second normal deadline and the existing bounded
        # one-second monitor/two-second anchor fallback. Interruptions reset neither.
        fallback_deadline = deadline+3
        def remember(exc):
            nonlocal pending, cleanup_error
            if not isinstance(exc,Exception) and pending is None:pending=exc
            if cleanup_error is None:cleanup_error=exc
            elif exc is not cleanup_error and len(notes)<8:notes.append(type(exc).__name__[:64])
        def close_owned(name):
            fd=getattr(self,name)
            if fd is None:return True
            # close may have completed before raising: never retry this FD number.
            setattr(self,name,None)
            try:os.close(fd);return True
            except BaseException as exc:remember(exc);return False
        def wait_owned(child,until):
            while True:
                try:
                    child.wait(timeout=max(0,until-time.monotonic()))
                    return
                except BaseException as exc:
                    remember(exc)
                    if isinstance(exc,Exception) or time.monotonic()>=until:raise
                    if not close_owned('writer'):raise cleanup_error
        try:
            if cancel and not close_owned('writer'):raise cleanup_error
            wait_owned(self.process,deadline)
            wait_owned(self.monitor,deadline)
            self.drain(0)
            if not self.cleanup or self.cleanup.get('complete') is not True:
                raise RuntimeError('worker group cleanup not confirmed')
            if not cancel and pending is None and self.cleanup.get('exit_code') is None:
                raise RuntimeError('target result not observed')
            if cleanup_error is not None:raise cleanup_error
        except BaseException as exc:
            remember(exc)
            close_owned('writer')
            try:
                if self.monitor.poll() is None:self.monitor.kill()
            except BaseException as fallback_error:remember(fallback_error)
            try:wait_owned(self.monitor,min(fallback_deadline,time.monotonic()+1))
            except BaseException as fallback_error:remember(fallback_error)
            # Monitor OS exit closes its sole control writer; anchor fallback owns its group.
            try:wait_owned(self.process,min(fallback_deadline,time.monotonic()+2))
            except BaseException as fallback_error:remember(fallback_error)
            try:self.fallback_group_gone = not _members(self.process.pid)
            except BaseException as fallback_error:remember(fallback_error)
        finally:
            close_owned('writer')
            close_owned('status')
            self.closed=True
            self._finish_error=pending if pending is not None else cleanup_error
        if self._finish_error is not None:
            for name in notes:
                try:self._finish_error.add_note('Lifetime cleanup: '+name)
                except BaseException as note_error:
                    if not isinstance(note_error,Exception) and pending is None:
                        pending=note_error;self._finish_error=note_error
            raise self._finish_error
        return self.cleanup


def spawn(argv, **kwargs):
    """Construct anchor+monitor; caller still records anchor PID before go."""
    fds=[];process=None;watcher=None
    def pipe():
        pair=os.pipe();fds.extend(pair);return pair
    try:
        life_r,life_w=pipe();control_r,control_w=pipe();event_r,event_w=pipe();status_r,status_w=pipe()
        extra=tuple(kwargs.pop('pass_fds',()))
        script=str(Path(__file__).resolve())
        process=subprocess.Popen([sys.executable,'-I','-S',script,'anchor',str(control_r),str(event_w),*argv],
                                 pass_fds=(*extra,control_r,event_w),**kwargs)
        identity=_identity(process.pid)
        if identity is None:raise RuntimeError('anchor identity unavailable')
        watcher=subprocess.Popen([sys.executable,'-I','-S',script,'monitor',str(life_r),str(control_w),str(event_r),str(status_w),str(process.pid),identity],
             pass_fds=(life_r,control_w,event_r,status_w),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
             stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True,env=MONITOR_ENV,cwd='/')
        handle=Lifetime(process,watcher,life_w,status_r)
        process._lupus_lifetime=handle
        for fd in (life_w,status_r):fds.remove(fd)
        return process
    except BaseException as original:
        try:
            if process is not None:
                # No caller has received the process, hence no go byte can have been sent.
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
                process.wait()
                if process.stdin is not None:process.stdin.close()
            if watcher is not None:
                if watcher.poll() is None:watcher.terminate()
                watcher.wait(timeout=3)
        except BaseException as cleanup_error:
            original.add_note('Lifetime constructor cleanup failed: '+type(cleanup_error).__name__)
        raise
    finally:
        for fd in fds:os.close(fd)


def wait(process,timeout=None):
    handle=getattr(process,'_lupus_lifetime',None)
    return handle.wait(timeout) if handle is not None else process.wait(timeout=timeout)


def poll(process):
    handle=getattr(process,'_lupus_lifetime',None)
    if handle is None:return process.poll()
    handle.drain(0)
    return handle.actual if handle.result_seen else process.poll()


def finish(process,cancel=False):
    handle=getattr(process,'_lupus_lifetime',None)
    if handle is None:raise RuntimeError('missing worker lifetime ownership')
    return handle.finish(cancel)


if __name__=='__main__':
    mode,*args=sys.argv[1:]
    if mode=='anchor':raise SystemExit(anchor(int(args[0]),int(args[1]),args[2:]))
    if mode=='monitor':raise SystemExit(monitor(*map(int,args[:5]),args[5]))
    raise SystemExit(111)
