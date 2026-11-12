"""Reviewed single write-host authority for account-wide outreach writes.

This gate is intentionally closed on today's Mac/VPS deployment: existing
writers have not all adopted one authority. An SSH flock on the VPS does not
enrol independent Mac SDK writers. No receipt here enables a foreign writer.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import socket
import threading

from background_jobs.contracts import ContractError, approved_path, canonical, digest


class WriteAuthority:
    def __init__(self,root,policy,*,enforcement_verifier=None):
        self.root,self.policy=Path(root),policy
        self.enforcement_verifier=enforcement_verifier;self.local=threading.local()

    def _current(self,account):
        cfg=self.policy.get('outreach_write_authority')
        if not isinstance(cfg,dict)or set(cfg)!={'ref','sha256'}:
            raise ContractError('shared_write_authority_not_reviewed')
        raw=approved_path(self.root,cfg['ref']).read_bytes()
        if digest(raw)!=cfg['sha256']:raise ContractError('shared_write_authority_changed')
        value=json.loads(raw)
        if (canonical(value).encode()!=raw or value.get('contract')!='outreach-single-write-host-authority-v1'or
            value.get('account')!=account or value.get('write_host')!=socket.gethostname()or
            value.get('foreign_credentials_disabled')is not True or value.get('uncoordinated_writers')!=[]or
            not isinstance(value.get('writers'),list)or not value['writers']):
            raise ContractError('shared_write_authority_incomplete')
        lock=Path(value.get('lock_path',''))
        if not lock.is_absolute()or lock.is_symlink()or not lock.parent.is_dir():
            raise ContractError('shared_write_authority_lock_invalid')
        names=set()
        for writer in value['writers']:
            if (not isinstance(writer,dict)or set(writer)!={'id','path','sha256','lock_path','protocol'}or
                writer['id']in names or writer['lock_path']!=str(lock)or
                writer['protocol']!='flock-held-through-external-write-and-readback-v1'):
                raise ContractError('shared_write_authority_writer_not_enrolled')
            names.add(writer['id']);path=Path(writer['path'])
            if not path.is_absolute()or path.is_symlink()or not path.is_file()or digest(path.read_bytes())!=writer['sha256']:
                raise ContractError('shared_write_authority_writer_changed')
        if 'hermes-outreach-domain'not in names:raise ContractError('shared_write_authority_writer_not_enrolled')
        # A reviewed JSON declaration/code-pin list alone cannot prove that
        # independent credential holders actually obey a shared lock. No native
        # capability/credential-isolation verifier is currently installed. The
        # host must supply that reviewed enforcement route, not a model flag.
        if not callable(self.enforcement_verifier):raise ContractError('shared_write_authority_enforcement_unavailable')
        evidence=self.enforcement_verifier(value)
        if (not isinstance(evidence,dict)or evidence.get('contract')!='outreach-enforced-write-capability-v1'or
            evidence.get('account')!=account or evidence.get('authority_sha256')!=digest(value)or
            evidence.get('unenforced_writers')!=[]or not evidence.get('native_enforcement_original_ref')):
            raise ContractError('shared_write_authority_enforcement_unproved')
        original=approved_path(self.root,evidence['native_enforcement_original_ref']).read_bytes()
        if digest(original)!=evidence.get('native_enforcement_original_sha256'):
            raise ContractError('shared_write_authority_enforcement_changed')
        return value,lock

    @contextmanager
    def hold(self,account,active):
        held=getattr(self.local,'held',None)
        if held is not None:
            if held[0]!=account:raise ContractError('shared_write_authority_account_changed')
            active();held[1]();yield held[1];return
        active();value,lock=self._current(account)
        fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ContractError('shared_write_authority_busy')from None
            def check():
                active();current,path=self._current(account)
                stat=os.fstat(fd);live=lock.stat()
                if current!=value or path!=lock or (stat.st_dev,stat.st_ino)!=(live.st_dev,live.st_ino):
                    raise ContractError('shared_write_authority_lost')
                return {'contract':'outreach-current-write-authority-v1','authority_sha256':digest(value),
                        'host':socket.gethostname(),'account':account}
            check();self.local.held=(account,check)
            try:yield check
            finally:self.local.held=None
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
