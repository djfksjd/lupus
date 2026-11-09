"""Trusted gated-controller ownership, never inferred from worker environment or timestamps."""
from contextvars import ContextVar
from pathlib import Path
from .util import LupusError

CURRENT = ContextVar('lupus_job_owner', default=None)


def owner_for(home):
    owner=CURRENT.get()
    return owner if owner is not None and owner['home']==str(Path(home).resolve()) else None


def record(k,pid,start,kind,**details):
    owner=getattr(k,'_job_owner',None)
    if owner is None:return
    if owner['home']!=str(k.home.resolve()):raise LupusError('JOB_OWNER_SCOPE','job owner runtime differs')
    k.emit('supervisor','job.process_registered','job',owner['job_id'],nonce=owner['nonce'],pid=pid,start=start,kind=kind,**details)


def discharge(k,pid,start,kind):
    owner=getattr(k,'_job_owner',None)
    if owner is not None and k.one("SELECT 1 FROM event WHERE aggregate_id=? AND type='job.process_registered' AND json_extract(payload,'$.nonce')=? AND json_extract(payload,'$.pid')=? AND json_extract(payload,'$.start')=? AND json_extract(payload,'$.kind')=? LIMIT 1",owner['job_id'],owner['nonce'],pid,start,kind):
        k.emit('supervisor','job.process_discharged','job',owner['job_id'],nonce=owner['nonce'],pid=pid,start=start,kind=kind)
