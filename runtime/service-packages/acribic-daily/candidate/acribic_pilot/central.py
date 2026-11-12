"""One server account lock and append-only pre-API reservations.

Disconnect releases a process lock, never its durable unknown write. All peers
must stop before another send while a live attempt/CRM completion is unresolved.
Historical imported reservations remain conservative mailbox/domain/source blocks.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os


class Stop(ValueError):pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def atomic(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink():raise Stop('state_symlink')
    temporary=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    with temporary.open('x')as out:
        os.fchmod(out.fileno(),0o600);out.write(json.dumps(value,ensure_ascii=False,sort_keys=True));out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Central:
    def __init__(self,root):
        self.root=Path(root)
        if self.root.is_symlink():raise Stop('central_root_symlink')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.root/'actions.jsonl'
        self.held=False

    @contextmanager
    def lock(self):
        path=self.root/'code-account.lock'
        if path.is_symlink():raise Stop('central_lock_symlink')
        with path.open('a')as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Stop('central_sender_busy')from None
            self.held=True
            try:yield self
            finally:self.held=False

    def events(self):
        if not self.path.exists():return []
        if self.path.is_symlink():raise Stop('central_journal_symlink')
        result=[]
        for line in self.path.read_text().splitlines():
            try:value=json.loads(line)
            except ValueError:raise Stop('central_journal_corrupt')from None
            if value.get('central_schema')!=1:raise Stop('central_journal_schema')
            result.append(value)
        return result

    def append(self,kind,*,run_id,mailbox='',org_domain='',source_id='',payload=None):
        if not self.held:raise Stop('central_lock_required')
        value={'central_schema':1,'at':datetime.now(timezone.utc).isoformat(),'kind':kind,
               'run_id':run_id,'mailbox':mailbox.strip().casefold(),'org_domain':org_domain,
               'source_id':source_id,'payload':payload or {}}
        fd=os.open(self.path,os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
        with os.fdopen(fd,'a')as out:
            out.write(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\n');out.flush();os.fsync(out.fileno())
        directory=os.open(self.root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return fingerprint(value)

    def states(self):
        states={}
        for event in self.events():
            key=(event['run_id'],event['mailbox']);kind=event['kind']
            if kind=='legacy_snapshot':continue
            prior=states.get(key)
            if kind=='attempt':
                if prior:raise Stop('central_duplicate_attempt')
            elif kind=='send_ack':
                if not prior or prior['kind']!='attempt':raise Stop('central_ack_without_attempt')
            elif kind=='sent_readback':
                if not prior or prior['kind']not in {'send_ack','attempt'}:raise Stop('central_readback_without_attempt')
            elif kind=='crm_attempt':
                if not prior or prior['kind']not in {'sent_readback','crm_readback'}:raise Stop('central_crm_without_sent')
            elif kind=='crm_readback':
                if not prior or prior['kind']!='crm_attempt':raise Stop('central_crm_readback_without_attempt')
            elif kind=='crm_committed':
                if not prior or prior['kind']not in {'crm_readback','sent_readback'}:raise Stop('central_completion_without_sent')
            elif kind in {'run_halted','missed_slot','bounce'}:pass
            else:raise Stop('central_unknown_event')
            states[key]=event
        return states

    def assert_clear(self,*,except_key=None):
        for key,event in self.states().items():
            if key!=except_key and event['kind']!='crm_committed':raise Stop('central_unresolved_write_or_halt')

    def reserve(self,*,run_id,mailbox,org_domain,source_id,payload):
        if not self.held:raise Stop('central_lock_required')
        self.assert_clear()
        for event in self.events():
            if event['kind']=='legacy_snapshot':
                old=event['payload']
                if mailbox.casefold()in old['mailboxes']or org_domain in old['org_domains']or source_id in old['source_ids']:
                    raise Stop('legacy_reservation_blocks_contact')
            elif (event['mailbox']==mailbox.casefold()or event['org_domain']==org_domain
                  or event['source_id']==source_id):raise Stop('central_contact_already_attempted')
        # This acknowledgement is the only permission to reach a provider call.
        return self.append('attempt',run_id=run_id,mailbox=mailbox,org_domain=org_domain,
                           source_id=source_id,payload=payload)
