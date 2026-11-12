"""Fresh legacy capture and canonical server source consumer.

FileProtectionSource is the fresh cutover inventory reader. After the switch,
ServerProtectionSource reads current canonical state and native attempts without
a Mac connection; the historical raw files remain immutable originals.
"""
import base64
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import socket

from background_jobs.contracts import ContractError, canonical, digest
from .pool_sources import MAIL, domain, family, company_key
from .sources import persist


def _read(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ContractError('authoritative_protection_file_missing')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if before.st_size>32*1024*1024:raise ContractError('protection_original_too_large')
        raw=b''
        while True:
            part=os.read(fd,1024*1024)
            if not part:break
            raw+=part
            if len(raw)>32*1024*1024:raise ContractError('protection_original_too_large')
        after=os.fstat(fd);live=path.stat()
        fields=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if fields(before)!=fields(after) or fields(after)!=fields(live) or len(raw)!=after.st_size:
            raise ContractError('protection_original_changed_during_read')
        return raw,{'device':after.st_dev,'inode':after.st_ino,'bytes':after.st_size,'mtime_ns':after.st_mtime_ns}
    finally:os.close(fd)


class FileProtectionSource:
    """Host-wired paths only, never paths supplied in Main tool arguments."""
    def __init__(self,root,*,source_host,suppression,addendum,state_roots):
        self.root=Path(root);self.source_host=source_host
        self.suppression,self.addendum=Path(suppression),Path(addendum)
        self.state_roots=tuple(Path(p)for p in state_roots)

    def _inventory(self):
        if not self.source_host or socket.gethostname()!=self.source_host:
            raise ContractError('authoritative_protection_host_unavailable')
        if len(self.state_roots)!=2 or len(set(self.state_roots))!=2:
            raise ContractError('both_legacy_and_twenty_journals_required')
        paths={self.suppression:'suppression',self.addendum:'addendum'}
        for root in self.state_roots:
            if root.is_symlink()or not root.is_dir():raise ContractError('legacy_protection_inventory_missing')
            entries=sorted(root.rglob('*.jsonl'))
            if not entries:raise ContractError('legacy_protection_inventory_missing')
            for path in entries:
                if any(p.is_symlink()for p in (path,*path.parents)if p!=root.parent):
                    raise ContractError('protection_inventory_symlink')
                paths[path]='journal'
        return paths

    def capture(self,binding,active):
        active();paths=self._inventory();files=[]
        for path,kind in sorted(paths.items(),key=lambda p:str(p[0])):
            active();raw,stat=_read(path);active()
            try:text=raw.decode('utf-8')
            except UnicodeError:raise ContractError('protection_original_encoding_unknown')from None
            # Validate syntax now. Empty JSONL is permitted only inside the
            # nonempty complete inventory; required suppression/CSV never are.
            if kind!='journal'and not text.strip():raise ContractError('required_protection_original_empty')
            if kind=='journal':
                for line in text.splitlines():
                    try:row=json.loads(line)
                    except ValueError:raise ContractError('legacy_journal_incomplete')from None
                    if not isinstance(row,dict):raise ContractError('legacy_journal_incomplete')
            if kind=='addendum':
                try:
                    rows=list(csv.reader(io.StringIO(text),delimiter=';',strict=True))
                except csv.Error:raise ContractError('addendum_csv_incomplete')from None
                if len(rows)<2:raise ContractError('authoritative_addendum_history_missing')
            files.append({'path':str(path),'kind':kind,**stat,'sha256':digest(raw),
                          'raw_base64':base64.b64encode(raw).decode(),'text':text})
        active()
        if paths!=self._inventory():raise ContractError('protection_inventory_changed')
        for entry in files:
            raw,stat=_read(entry['path'])
            if stat!={k:entry[k]for k in stat}or digest(raw)!=entry['sha256']:
                raise ContractError('protection_original_changed_during_read')
        packet={'contract':'outreach-authoritative-protections-v1','source_host':self.source_host,
                'captured_at':datetime.now(timezone.utc).isoformat(),'files':files}
        return persist(self.root,'protections','acribic-authoritative-protections',packet,binding,'authoritative-host.raw-protection-snapshot')


def legacy_evidence(packet):
    """Typed projection with original file/line identity; raw bytes stay intact."""
    from .central_sources import checked_packet, evidence
    checked_packet(packet)
    rows = []; freed = set(); deferred = []
    for entry in packet['files']:
        text = entry['text']; prefix = entry['sha256']
        if entry['kind'] == 'suppression':
            free_match = re.search(r'^## 8\. [Ff]reigegeben.*?(?=^## |\Z)', text, re.S | re.M)
            if free_match:
                freed |= {family(domain(v)) for v in re.findall(r'`([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})`', free_match.group())}
                text = text[:free_match.start()] + text[free_match.end():]
            reservation=re.search(r'^## 7\. .*?(?=^## |\Z)',text,re.S|re.M)
            reserved=reservation.group()if reservation else ''
            outside=text[:reservation.start()]+text[reservation.end():]if reservation else text
            for d in sorted({domain(v) for v in re.findall(r'(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}', text)}):
                if family(d) not in freed:
                    state='icp_reserved'if d in reserved.casefold()and d not in outside.casefold()else'suppress'
                    rows.append(evidence(prefix + ':domain:' + d, state, {'domain': d}, original=entry['path']))
    for entry in packet['files']:
        prefix = entry['sha256']; text = entry['text']
        if entry['kind'] == 'addendum':
            for n, row in enumerate(csv.reader(io.StringIO(text), delimiter=';', strict=True), 1):
                if not row or not MAIL.fullmatch(row[0].strip()): continue
                stage = row[1].strip() if len(row) > 1 else ''
                state = ('bounce' if 'bounce' in ';'.join(row).casefold() else 'sent_verified' if stage == 'E-Mail raus'
                         else 'suppress' if stage in {'Kein Interesse', 'Gesperrt (ICP)'} else 'address_review' if stage == 'Adresse pruefen'
                         else 'attempt_unknown')
                if state == 'address_review' and family(domain(row[0])) in freed: continue
                rows.append(evidence(prefix + ':csv:' + str(n), state, {'email': row[0].strip()}, original={'path': entry['path'], 'line': n}))
        elif entry['kind'] == 'journal':
            for n, line in enumerate(text.splitlines(), 1):
                row = json.loads(line); event = str(row.get('event') or '')
                states = {'attempt':'attempt_unknown', 'draft_attempt':'attempt_unknown', 'draft_update_attempt':'attempt_unknown',
                    'send_ack':'send_ack', 'sent_readback':'sent_verified', 'history_attempt':'attempt_unknown',
                    'history_readback':'sent_verified', 'draft_ack':'draft_ack', 'draft_readback':'draft_verified',
                    'own_draft_verified':'draft_verified', 'draft_update_ack':'draft_ack', 'draft_update_readback':'draft_verified',
                    'scheduled_readback':'scheduled', 'planned_crm_readback':'scheduled',
                    'draft_crm_attempt':'attempt_unknown', 'draft_crm_readback':'draft_verified',
                    'draft_block_update_attempt':'attempt_unknown', 'draft_block_update_ack':'draft_ack',
                    'draft_block_update_readback':'draft_verified', 'blocked_draft_complete':'draft_verified',
                    'delivery_issue_attempt':'bounce', 'delivery_issue_readback':'bounce',
                    'crm_committed_verified':'sent_verified', 'crm_committed_readback':'sent_verified'}
                if event not in states: continue
                # A preparation, query, CRM/note/calendar attempt is not a mail
                # attempt. Do not mine addresses from arbitrary nested content.
                mail = row.get('email') or row.get('to') or row.get('mail')
                if isinstance(mail, dict): mail = mail.get('to') or mail.get('email')
                values = mail if isinstance(mail, list) else [mail]
                for address in values:
                    if not isinstance(address, str) or not MAIL.fullmatch(address.strip()): continue
                    target = {'email': address.strip()}
                    for source, dest in (('firma','company'), ('company_id','company_id')):
                        if isinstance(row.get(source), str) and row[source].strip(): target[dest] = row[source]
                    operation = str(row.get('attempt_id') or row.get('draft_id') or row.get('message_id') or
                                    canonical([entry['path'], row.get('run_id') or row.get('batch_id'), address]))
                    deferred.append(evidence(prefix + ':jsonl:' + str(n) + ':' + address, states[event], target,
                        operation_id=operation, original={'path':entry['path'], 'line':n, 'row_sha256':digest(line.encode()), 'event':event}))
    # All event originals remain. An ACK never means delivery; unresolved
    # attempts remain explicit even alongside a later verified occurrence.
    return rows + deferred


def stealth_legacy_evidence(packet):
    """Only named Stealth legacy sources; never a complete pool assertion."""
    from .central_sources import ACCOUNTS,evidence
    if (not isinstance(packet,dict)or packet.get('contract')!='outreach-stealth-named-legacy-v1'
        or not isinstance(packet.get('files'),list)or not packet['files']or not packet.get('source_host')):
        raise ContractError('stealth_named_legacy_original_required')
    result=[];paths=set()
    for entry in packet['files']:
        try:
            raw=base64.b64decode(entry['raw_base64'],validate=True);text=raw.decode('utf-8')
        except (KeyError,ValueError,UnicodeError):raise ContractError('stealth_named_legacy_raw_invalid')from None
        if (entry['path']in paths or not Path(entry['path']).is_absolute()or digest(raw)!=entry['sha256']
            or raw.decode()!=entry['text']or len(raw)!=entry['bytes']):
            raise ContractError('stealth_named_legacy_original_changed')
        paths.add(entry['path']);prefix=entry['sha256']
        if entry['kind']=='delivery_suppression':
            records=list(csv.DictReader(io.StringIO(text)))
            if any(not MAIL.fullmatch(r.get('email',''))for r in records):raise ContractError('stealth_delivery_suppression_target_invalid')
            for n,row in enumerate(records):result.append(evidence(prefix+':csv:'+str(n),'bounce',{'email':row['email']},original={'path':entry['path'],'row':n}))
        elif entry['kind']=='stealth_history':
            value=json.loads(text);records=value.get('rows')if isinstance(value,dict)else value
            if not isinstance(records,list):raise ContractError('stealth_legacy_history_schema_unknown')
            for n,row in enumerate(records):
                email=row.get('email')
                if not MAIL.fullmatch(str(email or '')):raise ContractError('stealth_legacy_history_target_missing')
                sender=(row.get('smtp')or {}).get('sender')
                if sender and sender!=ACCOUNTS['stealth']:raise ContractError('stealth_legacy_history_account_mismatch')
                pending=any(row.get(k)is True for k in ('draft_pending','create_pending','schedule_pending','send_pending'))
                state=('sent_verified'if row.get('sent_verified')is True else'sent_recorded'if row.get('sent_message_id')
                       else'scheduled'if row.get('scheduled')is True else'attempt_unknown'if pending
                       else'draft_verified'if row.get('draft_verified')is True else'draft_recorded'if row.get('draft_id')else'review_only')
                target={'email':email}
                if row.get('firm'):target['company']=row['firm']
                # Parent-group text is not automatically an identity/family ID.
                # It remains in the original for later full-pool interpretation.
                result.append(evidence(prefix+':row:'+str(n),state,target,
                    operation_id=str(row.get('draft_id')or row.get('sent_message_id')or prefix+':'+str(n)),
                    original={'path':entry['path'],'row':n,'row_sha256':digest(row)}))
        else:raise ContractError('stealth_named_legacy_kind_unknown')
    return result


def first_contact_exclusions(value):
    """Historical first-contact family protection, separate from reply scope."""
    return {**{k:set(v)for k,v in value.items()},
            'domains':{family(v)for v in value['domains']}|{family(domain(v))for v in value['mailboxes']}}


def exclusions(document):
    """Current projection; complete legacy/native/change originals retained."""
    packet=json.loads(document['content'])['original']
    from .central_sources import project_evidence, ACCOUNTS
    if packet.get('contract') == 'outreach-central-sources-v1':
        if ACCOUNTS.get(packet.get('project')) != packet.get('account'):
            raise ContractError('central_source_project_account_mismatch')
        if not packet.get('legacy_complete'):
            raise ContractError('central_project_legacy_import_incomplete')
        if not packet.get('native_history_complete'):
            raise ContractError('central_native_attempt_coverage_incomplete')
        originals = packet['legacy_projection'] + packet['events'] + packet['native_history']
        value = project_evidence(originals)
        return first_contact_exclusions(value)
    value = project_evidence(legacy_evidence(packet))
    return first_contact_exclusions(value)


def permits(candidate,company_name,document,*,blocked=None):
    blocked=exclusions(document)if blocked is None else blocked
    return (candidate['to'].strip().casefold()not in blocked['mailboxes']and
            not any(family(v)in blocked['domains']for v in candidate['family_domains'])and
            company_key(company_name)not in blocked['names']and
            candidate.get('company_id')not in blocked.get('company_ids',set())and
            candidate.get('family_id')not in blocked.get('family_ids',set()))


class ServerProtectionSource:
    """Drop-in existing consumer, current native database on the server only."""
    def __init__(self, root, authority, *, project, account):
        from .central_sources import scope_key
        scope_key(project,account)
        self.root=Path(root);self.authority=authority;self.project=project;self.account=account

    @classmethod
    def from_existing(cls,store,policy,*,project='acribic'):
        from .central_sources import CanonicalSources,ACCOUNTS
        from hermes_second_brain.lifecycle import LifecycleStore
        cfg=policy.get('outreach_central_sources')
        if not isinstance(cfg,dict)or set(cfg)!={'native_db','root','host_id','account_lock_root','native_ledgers'}:
            raise ContractError('central_source_host_configuration_required')
        db=Path(cfg['native_db'])
        if not db.is_absolute()or not db.is_file()or db.is_symlink():
            raise ContractError('central_source_native_database_missing')
        authority=CanonicalSources(LifecycleStore(db),cfg['root'],host_id=cfg['host_id'],
            account_lock_root=cfg['account_lock_root'],native_ledgers=cfg['native_ledgers'])
        return cls(store.root,authority,project=project,account=ACCOUNTS[project])

    def capture(self,binding,active):
        active()
        packet=self.authority.snapshot(self.project,self.account,own_identity=binding.get('identity'))
        # Different native workflows retain their own source roots. Materialize
        # each immutable shared original once there, with exact hash binding;
        # later compact captures reference it rather than recopying the bytes.
        from background_jobs.contracts import private_file,approved_path
        for entry in packet['changes']:
            destination=self.root/entry['ref']
            if not destination.exists():
                raw=approved_path(self.authority.root,entry['ref']).read_bytes()
                if digest(raw)!=entry['sha256']:raise ContractError('central_source_original_changed')
                try:private_file(destination,raw)
                except FileExistsError:
                    if approved_path(self.root,entry['ref']).read_bytes()!=raw:raise ContractError('central_source_original_changed')
        active()
        return persist(self.root,'protections',self.project+'-central-protections',packet,binding,
                       'native-canonical-source.current-version-and-existing-attempt-originals')
