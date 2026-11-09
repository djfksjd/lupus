"""Supervisors that keep running after the terminal is closed.

`lupus run … --background` (or `alpha-run`) starts the same foreground supervisor in a detached
process whose output goes to a log file in the runtime. It is the ordinary supervisor: same
lock (so only one runs per runtime), same recovery if it is killed. Stopping a job asks the
supervisor to stop; it empties its worker's process group on the way out.
"""

from __future__ import annotations

import contextlib
import fcntl
import stat
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .kernel import Kernel
from .util import LupusError, new_id, proc_start

GATE_FD = "LUPUS_JOB_GATE_FD"
ALLOWED = ("run", "alpha-run")      # commands that never need a person at the terminal while they run
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
_STARTED: list[subprocess.Popen] = []      # jobs started by this process, so their exit can be collected


def start(k: Kernel, command: list[str]) -> dict:
    """Record the job, then start it. The row exists before the process can do anything."""
    if not command or command[0] not in ALLOWED:
        raise LupusError("JOB_COMMAND_NOT_ALLOWED", command[0] if command else "")
    job_id = new_id("job")
    logs = k.runtime / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    log_path = logs / f"{job_id}.log"
    with k.tx():
        k.run("INSERT INTO job(job_id, command, log_path, status, started_at) VALUES (?,?,?, 'STARTING', ?)",
              job_id, json.dumps(command), str(log_path), k.now())
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [PACKAGE_PARENT, os.environ.get("PYTHONPATH", "")]))}
    read_fd=write_fd=lifetime_fd=None
    try:
        read_fd, write_fd = os.pipe()
        lifetime_dir=k.runtime/'job-lifetimes';lifetime_dir.mkdir(mode=0o700,exist_ok=True)
        lifetime_path=lifetime_dir/(job_id+'.lock')
        lifetime_fd=os.open(lifetime_path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(lifetime_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        lifetime_info=os.fstat(lifetime_fd)
        with open(log_path, "ab", buffering=0) as log:
            os.chmod(log_path, 0o600)
            proc = subprocess.Popen([sys.executable, "-m", "lupus", "--home", str(k.home), "job-run", job_id, "--guardian"],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    env={**env, GATE_FD: str(read_fd),'LUPUS_JOB_LIFETIME_FD':str(lifetime_fd)}, cwd=str(k.home), pass_fds=(read_fd,lifetime_fd))
        _STARTED.append(proc)
        with k.tx():
            k.run("UPDATE job SET pid = ?, proc_start = ?, status = 'RUNNING' WHERE job_id = ?",
                  proc.pid, proc_start(proc.pid), job_id)
            k.emit('supervisor','job.guardian_started','job',job_id,protocol=1,pid=proc.pid,start=proc_start(proc.pid),nonce=new_id('owner'),lifetime_dev=lifetime_info.st_dev,lifetime_ino=lifetime_info.st_ino)
        os.write(write_fd, b"g")      # only now may it do anything; if we die first it reads EOF and exits
    finally:
        for fd in (read_fd,write_fd,lifetime_fd):
            if fd is not None:
                try:os.close(fd)
                except OSError:pass
    return {"job_id": job_id, "pid": proc.pid, "log": str(log_path)}


def released() -> bool:
    """In the job process: wait until the starter has recorded who we are."""
    fd = os.environ.pop(GATE_FD, None)
    if fd is None:
        return False
    try:
        return os.read(int(fd), 1) == b"g"
    finally:
        os.close(int(fd))


def command_of(k: Kernel, job_id: str) -> list[str]:
    row = k.one("SELECT command FROM job WHERE job_id = ?", job_id)
    if row is None:
        raise LupusError("JOB_NOT_FOUND", job_id)
    return json.loads(row["command"])


def finished(k: Kernel, job_id: str, exit_code: int | None) -> None:
    with k.tx():
        k.run("UPDATE job SET status = 'EXITED', exit_code = ?, ended_at = ? WHERE job_id = ? AND status <> 'EXITED'",
              exit_code, k.now(), job_id)


def _reap_owned() -> None:
    # A child can publish EXITED before interpreter teardown. Reap it independently
    # of that publication, without waiting on or signalling unrelated processes.
    _STARTED[:] = [proc for proc in _STARTED if proc.poll() is None]


def _alive(row) -> bool:
    _reap_owned()
    return row["pid"] is not None and row["proc_start"] is not None and proc_start(row["pid"]) == row["proc_start"]


def _group_present(pid):
    if pid is None:return False
    try:os.killpg(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


def _lifetime_gone(k,job_id,binding):
    """Nonblocking birth-identity certificate; no ps, waits or reused-PGID adoption."""
    fd=None
    try:
        path=k.runtime/'job-lifetimes'/(job_id+'.lock')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or (info.st_dev,info.st_ino)!=(binding.get('lifetime_dev'),binding.get('lifetime_ino')):return None
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        return True
    except OSError:return None
    finally:
        if fd is not None:os.close(fd)


def _view(k,row):
    from .job_guardian import latest
    row=dict(row);binding=latest(k,row['job_id'],'job.guardian_started')
    cleanup=latest(k,row['job_id'],'job.cleanup') if binding else None
    result=latest(k,row['job_id'],'job.command_result') if binding else None
    legacy_identity=type(row['pid']) is int and row['pid']>0 and isinstance(row['proc_start'],str) and bool(row['proc_start'])
    present=_group_present(row['pid']) if not binding and legacy_identity else None
    gone=_lifetime_gone(k,row['job_id'],binding) if binding else (not present if legacy_identity else None)
    complete=bool(cleanup and cleanup.get('complete') and cleanup.get('nonce')==binding.get('nonce')) if binding else bool(legacy_identity and not present)
    pending=not(complete and gone is True)
    if binding:
        state=('complete' if not pending else 'finishing' if complete and gone is False else
               'cleanup_failed' if cleanup else 'guardian_lost' if gone is True else
               'ownership_unknown' if gone is None else 'cleanup_pending' if result else 'running')
    else:state='legacy_group_gone' if legacy_identity and not present else 'ownership_unknown'
    return {**row,'status':('STARTING' if row['status']=='STARTING' else 'RUNNING') if pending else 'EXITED','cleanup_pending':pending,
            'cleanup_complete':complete and gone is True,'cleanup_state':state,
            **({'command_exit_code':result.get('exit_code')} if result else {})}


def listing(k: Kernel) -> list[dict]:
    """Nonblocking lifecycle projection. Never perform cleanup or signal a process here."""
    _reap_owned();out=[]
    for row in k.q("SELECT * FROM job ORDER BY started_at DESC,rowid DESC LIMIT 50"):
        view=_view(k,row)
        out.append({key:view[key] for key in ('job_id','status','exit_code','pid','cleanup_pending','cleanup_complete','cleanup_state')} |
                   {'command':' '.join(json.loads(row['command'])),'log':row['log_path']} |
                   ({'command_exit_code':view['command_exit_code']} if 'command_exit_code' in view else {}))
    return out


def log_tail(k: Kernel, job_id: str, lines: int = 40) -> str:
    row = k.one("SELECT log_path FROM job WHERE job_id = ?", job_id)
    if row is None:
        raise LupusError("JOB_NOT_FOUND", job_id)
    try:
        data = Path(row["log_path"]).read_bytes()[-200_000:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])


def stop(k: Kernel, job_id: str, wait_s: float = 20.0) -> dict:
    from .job_guardian import latest,matching
    row=k.one('SELECT * FROM job WHERE job_id=?',job_id)
    if row is None:raise LupusError('JOB_NOT_FOUND',job_id)
    _reap_owned();view=_view(k,row)
    if view['cleanup_complete']:
        if row['status']!='EXITED':finished(k,job_id,row['exit_code'])
        return {'job_id':job_id,'stopped':True,'was_running':False,'cleanup_complete':True}
    # No PID reuse or dead-leader group can authorize signals.
    if not matching(row['pid'],row['proc_start']):
        return {'job_id':job_id,'stopped':False,'was_running':False,'cleanup_complete':False,'reason':'ownership_unknown'}
    os.kill(row['pid'],signal.SIGTERM)
    deadline=time.monotonic()+wait_s
    while time.monotonic()<deadline:
        _reap_owned();view=_view(k,k.one('SELECT * FROM job WHERE job_id=?',job_id))
        if view['cleanup_complete']:return {'job_id':job_id,'stopped':True,'was_running':True,'cleanup_complete':True}
        if latest(k,job_id,'job.guardian_started') is None:
            # Legacy jobs have no lifetime certificate. After an authenticated
            # stop, an unreaped zombie is no writable process, not a live worker.
            from .util import group_alive
            if not group_alive(row['pid']):
                finished(k,job_id,row['exit_code'])
                return {'job_id':job_id,'stopped':True,'was_running':True,'cleanup_complete':True}
        time.sleep(.05)
    return {'job_id':job_id,'stopped':False,'was_running':True,'cleanup_complete':False,'reason':'cleanup_pending'}


def prune(k: Kernel, days: float, kept: bool = False) -> dict:
    """Delete what only accumulates: files set aside when a frozen file was put back, logs and
    records of finished background jobs, session scratch files — older than `days`. Goals, evidence
    and the audit trail are history and are not touched.

    A copy of a project that a review left behind (`…-verified`) is there because the way back to
    the verified state was incomplete or was cut short: it may be the only good copy. Age does
    not make it disposable. It is listed, and deleted only when the user says so (`kept`)."""
    cutoff = time.time() - days * 86_400
    removed = {"displaced": 0, "logs": 0, "session_files": 0, "jobs": 0, "snapshots": 0}
    live = {row["goal_id"] for row in k.q("SELECT goal_id FROM goal WHERE status NOT IN ('DONE','CANCELLED')")}
    with contextlib.ExitStack() as stack:
        # Set-aside files and review backups are what a running supervisor returns to. They are
        # pruned only while no supervisor runs; otherwise they wait for the next prune.
        try:
            stack.enter_context(k.supervisor_lock())
            idle = True
        except LupusError:
            idle = False
            removed["skipped_while_a_supervisor_runs"] = ["displaced", "snapshots"]
        for name, key in (("displaced", "displaced"), ("sessions", "session_files"), ("base", "snapshots"),
                          ("released", "snapshots")):      # (a proposal for released files of a goal that has since ended)
            base = k.runtime / name
            for entry in (sorted(base.iterdir()) if base.is_dir() and (idle or name == "sessions") else []):
                owner = entry.name.removesuffix(".json").removesuffix("-verified").removesuffix("-crosscheck").removesuffix("-probe")
                for_the_user = name == "base" and entry.name.endswith("-verified") and not entry.is_symlink()
                if entry.is_symlink() or owner in live or any(owner == r["run_id"] for r in k.q(
                        "SELECT run_id FROM run WHERE status <> 'STOPPED'")) or (
                        entry.stat().st_mtime > cutoff and not (for_the_user and kept)):
                    if for_the_user:
                        removed.setdefault("kept_for_you", []).append(str(entry))
                    continue
                if for_the_user and not kept:
                    removed.setdefault("kept_for_you", []).append(str(entry))
                    continue
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
                removed[key] += 1
    listing(k)      # reap direct owned handles; cleanup projection below retains unknown rows
    with k.tx():
        for row in k.q("SELECT job_id, log_path FROM job WHERE coalesce(ended_at, started_at) < ?",
                       int(cutoff * 1000)):
            full=k.one('SELECT * FROM job WHERE job_id=?',row['job_id'])
            if not _view(k,full)['cleanup_complete']:
                continue
            (k.runtime/'job-lifetimes'/(row['job_id']+'.lock')).unlink(missing_ok=True)
            Path(row["log_path"]).unlink(missing_ok=True)
            k.run("DELETE FROM job WHERE job_id = ?", row["job_id"])
            removed["jobs"] += 1
            removed["logs"] += 1
    return removed


def notify(title: str, text: str) -> None:
    """Best effort desktop notification (macOS). Never fails a job."""
    if sys.platform != "darwin" or os.environ.get("LUPUS_NO_NOTIFY"):
        return
    try:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, text[:200]],
                       capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
