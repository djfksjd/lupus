"""Supervisors that keep running after the terminal is closed.

`lupus run … --background` (or `alpha-run`) starts the same foreground supervisor in a detached
process whose output goes to a log file in the runtime. It is the ordinary supervisor: same
lock (so only one runs per runtime), same recovery if it is killed. Stopping a job asks the
supervisor to stop; it empties its worker's process group on the way out.
"""

from __future__ import annotations

import json
import os
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
    read_fd, write_fd = os.pipe()
    try:
        with open(log_path, "ab", buffering=0) as log:
            os.chmod(log_path, 0o600)
            proc = subprocess.Popen([sys.executable, "-m", "lupus", "--home", str(k.home), "job-run", job_id],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    env={**env, GATE_FD: str(read_fd)}, cwd=str(k.home), pass_fds=(read_fd,))
        _STARTED.append(proc)
        with k.tx():
            k.run("UPDATE job SET pid = ?, proc_start = ?, status = 'RUNNING' WHERE job_id = ?",
                  proc.pid, proc_start(proc.pid), job_id)
        os.write(write_fd, b"g")      # only now may it do anything; if we die first it reads EOF and exits
    finally:
        os.close(read_fd)
        os.close(write_fd)
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


def _alive(row) -> bool:
    for proc in _STARTED:
        proc.poll()      # a finished child of ours must not linger as a zombie that still "exists"
    return row["pid"] is not None and row["proc_start"] is not None and proc_start(row["pid"]) == row["proc_start"]


def listing(k: Kernel) -> list[dict]:
    """Jobs, newest first. A job whose process is gone without having reported is closed here
    (it was killed); the run it left behind is closed by the usual recovery."""
    out = []
    for row in k.q("SELECT * FROM job ORDER BY started_at DESC, rowid DESC LIMIT 50"):
        row = dict(row)
        if row["status"] != "EXITED" and not _alive(row) and (row["status"] == "RUNNING" or k.now() - row["started_at"] > 30_000):
            finished(k, row["job_id"], None)
            row.update(status="EXITED", exit_code=None)
        out.append({"job_id": row["job_id"], "status": row["status"], "exit_code": row["exit_code"],
                    "command": " ".join(json.loads(row["command"])), "pid": row["pid"], "log": row["log_path"]})
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
    row = k.one("SELECT * FROM job WHERE job_id = ?", job_id)
    if row is None:
        raise LupusError("JOB_NOT_FOUND", job_id)
    if row["status"] == "EXITED" or not _alive(row):
        finished(k, job_id, row["exit_code"])
        return {"job_id": job_id, "stopped": True, "was_running": False}
    os.kill(row["pid"], signal.SIGTERM)       # only ever the pid whose start time we recorded
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and _alive(row):
        time.sleep(0.2)
    if _alive(row):
        return {"job_id": job_id, "stopped": False, "was_running": True, "note": "still stopping its worker; check `lupus jobs`"}
    finished(k, job_id, None)
    return {"job_id": job_id, "stopped": True, "was_running": True}


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
