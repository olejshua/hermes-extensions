"""Canonical host source state, in the existing native lifecycle database.

This is a source adapter, not a scheduler or a mail permission. Raw legacy
originals and source edits are immutable. Native mail attempts remain in their
existing SQL/file ledgers and are read directly, never copied into a counter.
The CLI is an authenticated OS/SSH source-edit path; it cannot call a SaaS API.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import threading

from background_jobs.contracts import ContractError, canonical, digest, private_dir, private_file, approved_path
from .pool_sources import MAIL, domain, family, company_key

ACCOUNTS = {'acribic': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}
ACTIONS = {'suppress', 'release', 'bounce', 'sent_verified', 'draft_verified', 'attempt_unknown', 'address_review', 'review_only'}
WRITERS = {'source-console', 'triage', 'versand-completion', 'versand-bounce', 'icp-completion'}


def scope_key(project, account):
    if ACCOUNTS.get(project) != account:
        raise ContractError('central_source_project_account_mismatch')
    return digest([project, account])


def checked_packet(packet):
    if (not isinstance(packet, dict) or packet.get('contract') != 'outreach-authoritative-protections-v1'
            or not isinstance(packet.get('source_host'), str) or not packet.get('source_host')
            or not isinstance(packet.get('files'), list) or not packet['files']):
        raise ContractError('central_legacy_original_required')
    paths = set()
    kinds = []
    for entry in packet['files']:
        if entry.get('path') in paths or not Path(entry.get('path', '')).is_absolute():
            raise ContractError('central_legacy_path_identity_invalid')
        paths.add(entry['path']); kinds.append(entry.get('kind'))
        try:
            raw = base64.b64decode(entry['raw_base64'], validate=True)
            text = raw.decode('utf-8')
        except (ValueError, UnicodeError, KeyError, TypeError):
            raise ContractError('central_legacy_raw_invalid') from None
        if digest(raw) != entry.get('sha256') or text != entry.get('text') or len(raw) != entry.get('bytes'):
            raise ContractError('central_legacy_original_changed')
        if entry['kind'] == 'journal':
            for line in text.splitlines():
                try: row = json.loads(line)
                except ValueError: raise ContractError('central_legacy_journal_invalid') from None
                if not isinstance(row, dict): raise ContractError('central_legacy_journal_invalid')
                sender = row.get('sender')
                if sender and sender != ACCOUNTS['acribic']:
                    raise ContractError('central_legacy_sender_needs_explicit_partition')
        elif entry['kind'] not in {'suppression', 'addendum'}:
            raise ContractError('central_legacy_kind_unknown')
    if kinds.count('suppression') != 1 or kinds.count('addendum') != 1 or 'journal' not in kinds:
        raise ContractError('central_legacy_inventory_incomplete')
    return packet


def targets(value):
    """Only explicit target fields, never arbitrary emails in quoted JSON."""
    if not isinstance(value, dict): raise ContractError('central_explicit_target_required')
    allowed = {'email', 'domain', 'company', 'company_id', 'family_id'}
    if not value or not set(value) <= allowed or any(not isinstance(v, str) or not v.strip() for v in value.values()):
        raise ContractError('central_explicit_target_required')
    result = {'mailboxes': [], 'domains': [], 'names': [], 'company_ids': [], 'family_ids': []}
    if 'email' in value:
        email = value['email'].strip().casefold()
        if not MAIL.fullmatch(email): raise ContractError('central_target_mailbox_invalid')
        result['mailboxes'].append(email)
    if 'domain' in value:
        # Explicit event scope is a literal DNS name. The first-contact helper
        # strips www/folds families for a different purpose; never use it here.
        d = value['domain'].strip().casefold().rstrip('.')
        label=r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
        if not re.fullmatch(r'(?:'+label+r'\.)+'+label,d):raise ContractError('central_target_domain_invalid')
        result['domains'].append(d)
    if 'company' in value: result['names'].append(company_key(value['company']))
    for key in ('company_id', 'family_id'):
        if key in value: result[key + 's'].append(value[key])
    return {k: sorted(set(v)) for k, v in result.items()}


def evidence(identifier, state, target, *, operation_id=None, original=None):
    return {'evidence_id': identifier, 'state': state, **targets(target),
            'operation_id': operation_id, 'original': original}


def overlaps(left, right):
    return any(set(left[k]) & set(right[k]) for k in ('mailboxes', 'domains', 'names', 'company_ids', 'family_ids'))


def project_evidence(originals):
    """Release only a suppression/review. Sends, bounces and attempts survive."""
    effective = []
    axes=('mailboxes', 'domains', 'names', 'company_ids', 'family_ids')
    for row in originals:
        if row['state'] == 'release':
            retained=[]
            for previous in effective:
                if previous['state']not in {'suppress','address_review','review_only'}:
                    retained.append(previous);continue
                narrowed={**previous,**{k:sorted(set(previous[k])-set(row[k]))for k in axes}}
                if any(narrowed[k]for k in axes):retained.append(narrowed)
            effective=retained
        elif row['state'] != 'review_only':
            effective.append(row)
    return {k: sorted({v for row in effective for v in row[k]})
            for k in axes}


def operation_statuses(originals):
    """Summarize only the same original operation, retaining every observation.

    An earlier Attempt does not remain unresolved after its own readback. A
    readback for another operation or recipient cannot resolve that Attempt.
    Provider Sent/readback records are not proof of recipient delivery.
    """
    groups={}
    for row in originals:
        if not row.get('operation_id'):continue
        key=canonical([row['operation_id'],row['mailboxes'],row['domains']])
        value=groups.setdefault(key,{'operation_id':row['operation_id'],'mailboxes':row['mailboxes'],
                                    'states':set(),'evidence_ids':[]})
        value['states'].add(row['state']);value['evidence_ids'].append(row['evidence_id'])
    results=[]
    for row in groups.values():
        states=row.pop('states')
        status=next((name for state,name in (
            ('bounce','bounce_recorded'),('sent_verified','sent_readback_recorded'),('scheduled','scheduled_recorded'),
            ('draft_verified','draft_readback_recorded'),('send_ack','send_ack_outcome_unknown'),
            ('draft_ack','draft_ack_outcome_unknown'),('attempt_unknown','attempt_outcome_unknown'))if state in states),'source_change')
        results.append({**row,'status':status,'observed_states':sorted(states)})
    return results


class CanonicalSources:
    def __init__(self, lifecycle, root, *, host_id, account_lock_root, native_ledgers=()):
        self.lifecycle = lifecycle; self.root = Path(root)
        self.host_id = host_id; self.local = threading.local()
        if socket.gethostname() != host_id: raise ContractError('central_source_host_unavailable')
        if not self.root.is_absolute() or self.root.is_symlink(): raise ContractError('central_source_root_invalid')
        private_dir(self.root); self.locks = Path(account_lock_root)
        if not self.locks.is_absolute() or self.locks.is_symlink(): raise ContractError('central_account_lock_root_invalid')
        private_dir(self.locks)
        self.native_ledgers = tuple((Path(v['root']), Path(v['db'])) for v in native_ledgers)
        for root_path, db in self.native_ledgers:
            if (not root_path.is_absolute() or root_path.is_symlink() or not root_path.is_dir()
                    or not db.is_absolute() or db.is_symlink() or not db.is_file()):
                raise ContractError('central_native_ledger_unavailable')

    def initialize(self):
        with self.lifecycle.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS outreach_source_originals(
                sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS outreach_source_changes(
                scope TEXT NOT NULL, revision INTEGER NOT NULL, operation_id TEXT NOT NULL,
                payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, original_sha256 TEXT NOT NULL,
                prior_sha256 TEXT NOT NULL, entry_sha256 TEXT NOT NULL,
                PRIMARY KEY(scope, revision), UNIQUE(scope, operation_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS outreach_source_heads(
                scope TEXT PRIMARY KEY, project TEXT NOT NULL, account TEXT NOT NULL,
                revision INTEGER NOT NULL, entry_sha256 TEXT NOT NULL, import_sha256 TEXT)''')

    @contextmanager
    def hold(self, project, account, *, exclusive=False):
        key = scope_key(project, account)
        # Reuse the existing real account session, not an independent lock or
        # a caller boolean. Opening flock again would deadlock this process.
        from .write_boundary import SESSION
        record = SESSION.get()
        if record is not None:
            boundary = record['boundary']; boundary.check_session(record)
            if record['scope'].account != account or boundary.locks != self.locks:
                raise ContractError('central_account_session_changed')
            yield; return
        held = getattr(self.local, 'held', {})
        if key in held:
            if exclusive and not held[key][1]: raise ContractError('central_source_lock_upgrade_forbidden')
            yield; return
        path = self.locks / (digest(account.encode()) + '.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try: fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError: raise ContractError('central_source_version_busy') from None
            held[key] = (fd, exclusive); self.local.held = held
            initial = os.fstat(fd)
            yield
            current = path.stat()
            if (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino):
                raise ContractError('central_source_lock_replaced')
        finally:
            held.pop(key, None); fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def current(self, project, account):
        key = scope_key(project, account)
        with self.lifecycle.connect() as c:
            row = c.execute('SELECT * FROM outreach_source_heads WHERE scope=?', (key,)).fetchone()
        if not row or not row['import_sha256']: raise ContractError('central_source_not_imported')
        return dict(row)

    def _commit(self, project, account, operation_id, expected_revision, payload, raw, *, is_import=False):
        key = scope_key(project, account)
        if (not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256
                or type(expected_revision) is not int or expected_revision < 0 or not isinstance(raw, bytes)):
            raise ContractError('central_source_operation_identity_required')
        raw_sha = digest(raw); text = canonical(payload); payload_sha = digest(text.encode())
        # One immutable exportable original, referenced by all later captures.
        original_ref = 'central-source-originals/' + raw_sha + '.raw'
        try: private_file(self.root / original_ref, raw)
        except FileExistsError:
            if approved_path(self.root, original_ref).read_bytes() != raw:
                raise ContractError('central_source_original_changed')
        with self.hold(project, account, exclusive=True), self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM outreach_source_changes WHERE scope=? AND operation_id=?', (key, operation_id)).fetchone()
            if old:
                if old['payload_sha256'] != payload_sha or old['original_sha256'] != raw_sha:
                    raise ContractError('central_source_operation_changed')
                return {'scope': key, 'revision': old['revision'], 'entry_sha256': old['entry_sha256'],
                        'original_sha256': old['original_sha256'], 'payload_sha256': old['payload_sha256'], 'replayed': False}
            head = c.execute('SELECT * FROM outreach_source_heads WHERE scope=?', (key,)).fetchone()
            revision = head['revision'] if head else 0
            if expected_revision != revision: raise ContractError('central_source_version_changed')
            if is_import and head: raise ContractError('central_legacy_import_already_closed')
            if not is_import and not head: raise ContractError('central_source_not_imported')
            prior = head['entry_sha256'] if head else '0' * 64
            entry_sha = digest([key, revision + 1, operation_id, payload_sha, raw_sha, prior])
            c.execute('INSERT OR IGNORE INTO outreach_source_originals VALUES(?,?)', (raw_sha, raw))
            actual = c.execute('SELECT raw FROM outreach_source_originals WHERE sha256=?', (raw_sha,)).fetchone()[0]
            if bytes(actual) != raw: raise ContractError('central_source_original_changed')
            c.execute('INSERT INTO outreach_source_changes VALUES(?,?,?,?,?,?,?,?)',
                      (key, revision + 1, operation_id, text, payload_sha, raw_sha, prior, entry_sha))
            if head:
                c.execute('UPDATE outreach_source_heads SET revision=?,entry_sha256=? WHERE scope=?', (revision + 1, entry_sha, key))
            else:
                c.execute('INSERT INTO outreach_source_heads VALUES(?,?,?,?,?,?)', (key, project, account, 1, entry_sha, raw_sha))
        # Return a durable exact operation receipt, not a new mail permission.
        return self.read_operation(project, account, operation_id)

    def import_legacy(self, packet, *, operation_id, expected_revision=0):
        checked_packet(packet)
        raw = canonical(packet).encode()
        from .protections import legacy_evidence
        payload = {'kind': 'legacy-import', 'source_host': packet['source_host'], 'file_count': len(packet['files']),
                   'legacy_projection': legacy_evidence(packet),
                   'legacy_addendum':next(v['text']for v in packet['files']if v['kind']=='addendum'),
                   'legacy_suppression':next(v['text']for v in packet['files']if v['kind']=='suppression')}
        # Named source inventory is Acribic. Never infer a Stealth import from it.
        return self._commit('acribic', ACCOUNTS['acribic'], operation_id, expected_revision, payload, raw, is_import=True)

    def initialize_empty_project(self, project, account, *, operation_id, original):
        if project != 'stealth': raise ContractError('central_named_legacy_import_required')
        # This is an explicitly incomplete namespace, not a complete Stealth import.
        payload = {'kind': 'namespace', 'legacy_complete': False}
        return self._commit(project, account, operation_id, 0, payload, original, is_import=True)

    def import_named_stealth(self,packet,*,operation_id,expected_revision=0):
        from .protections import stealth_legacy_evidence
        projection=stealth_legacy_evidence(packet)
        payload={'kind':'legacy-import','source_host':packet['source_host'],'file_count':len(packet['files']),
                 'legacy_projection':projection,'legacy_addendum':'E-Mail;Stage;Datum;Notiz\n','legacy_suppression':'',
                 'legacy_complete':False,'coverage':'explicitly named suppression and historical send/draft originals only'}
        return self._commit('stealth',ACCOUNTS['stealth'],operation_id,expected_revision,payload,canonical(packet).encode(),is_import=True)

    def apply(self, project, account, change, *, writer_id, operation_id, expected_revision, original):
        if (writer_id not in WRITERS or not isinstance(change, dict)
                or set(change) != {'action', 'target', 'reason'} or change['action'] not in ACTIONS
                or not isinstance(change['reason'], str) or not change['reason'].strip()
                or not isinstance(original, bytes) or not original):
            raise ContractError('central_source_change_invalid')
        targets(change['target'])
        payload = {'kind': 'source-change', 'writer_id': writer_id, 'change': change}
        if change['reason']=='Abschlussnotiz':payload['history_text']=original.decode('utf-8')
        import csv,io
        try:
            row=next(csv.reader(io.StringIO(original.decode()),delimiter=';'))
            if len(row)>=4 and MAIL.fullmatch(row[0].strip()):payload['legacy_csv_row']=row
        except (ValueError,UnicodeError,StopIteration):pass
        return self._commit(project, account, operation_id, expected_revision, payload, original)

    def read_operation(self, project, account, operation_id):
        with self.lifecycle.connect() as c:
            row = c.execute('SELECT * FROM outreach_source_changes WHERE scope=? AND operation_id=?',
                            (scope_key(project, account), operation_id)).fetchone()
        if not row: raise ContractError('central_source_operation_unknown_readback_only')
        return {'scope': row['scope'], 'revision': row['revision'], 'entry_sha256': row['entry_sha256'],
                'original_sha256': row['original_sha256'], 'payload_sha256': row['payload_sha256'], 'replayed': False}

    def snapshot(self, project, account, *, own_identity=None, verify_originals=False):
        with self.hold(project, account), self.lifecycle.connect() as c:
            key = scope_key(project, account)
            head = c.execute('SELECT * FROM outreach_source_heads WHERE scope=?', (key,)).fetchone()
            if not head: raise ContractError('central_source_not_imported')
            rows = c.execute('SELECT * FROM outreach_source_changes WHERE scope=? ORDER BY revision', (key,)).fetchall()
            prior = '0' * 64; originals = []; events = []; projection = []; legacy_complete = False; legacy_ref = None; addendum=''; history=''
            for seq, row in enumerate(rows, 1):
                ref = 'central-source-originals/' + row['original_sha256'] + '.raw'
                if verify_originals:
                    raw_row = c.execute('SELECT raw FROM outreach_source_originals WHERE sha256=?', (row['original_sha256'],)).fetchone()
                    raw = approved_path(self.root, ref).read_bytes()
                    if not raw_row or bytes(raw_row[0]) != raw or digest(raw) != row['original_sha256']:
                        raise ContractError('central_source_original_changed')
                payload = json.loads(row['payload'])
                entry_sha = digest([key, seq, row['operation_id'], digest(row['payload'].encode()), row['original_sha256'], prior])
                if (row['revision'] != seq or row['prior_sha256'] != prior or row['entry_sha256'] != entry_sha
                        or row['payload_sha256'] != digest(row['payload'].encode())):
                    raise ContractError('central_source_version_chain_changed')
                prior = entry_sha
                originals.append({'revision':seq, 'entry_sha256':entry_sha, 'operation_id':row['operation_id'],
                                  'ref':ref, 'sha256':row['original_sha256'], 'payload_sha256':row['payload_sha256']})
                if payload['kind'] == 'legacy-import':
                    projection = payload['legacy_projection']; legacy_complete = payload.get('legacy_complete',True)
                    addendum=payload['legacy_addendum']
                    history=payload['legacy_suppression']
                    legacy_ref = {'ref':ref, 'sha256':row['original_sha256']}
                elif payload['kind'] == 'source-change':
                    change = payload['change']
                    event=evidence(row['entry_sha256'], change['action'], change['target'],
                                   operation_id=row['operation_id'], original=row['original_sha256'])
                    if 'legacy_csv_row'in payload:event['legacy_csv_row']=payload['legacy_csv_row']
                    if 'history_text'in payload:event['history_text']=payload['history_text']
                    events.append(event)
            if len(rows) != head['revision'] or prior != head['entry_sha256']:
                raise ContractError('central_source_head_changed')
            native = [];coverage=[]
            from .histories import native_source_history
            for root, db in self.native_ledgers:
                ledger=native_source_history(root, db, project, account, own_identity=own_identity,
                    allowed_databases=[str(d)for r,d in self.native_ledgers if r==root])
                native.extend(ledger['evidence']);coverage.append({k:v for k,v in ledger.items()if k!='evidence'})
            unique={}
            for row in native:
                key=row['evidence_id']
                if key in unique and unique[key]!=row:raise ContractError('central_native_evidence_conflict')
                unique[key]=row
            native=list(unique.values())
            return {'contract': 'outreach-central-sources-v1', 'project': project, 'account': account,
                    'source_host': self.host_id, 'revision': head['revision'], 'entry_sha256': prior,
                    'legacy_complete': legacy_complete, 'legacy_original': legacy_ref, 'legacy_projection':projection,
                    'legacy_addendum':addendum, 'legacy_history_text':history, 'changes': originals,
                    'events': events, 'native_history': native, 'native_ledger_coverage':coverage,
                    'operation_statuses':operation_statuses(projection+events+native),
                    'native_history_complete':{'background','gtm'}<={k for row in coverage for k in row['coverage']},
                    'captured_at': datetime.now(timezone.utc).isoformat()}

    @contextmanager
    def write_guard(self, scope, plan):
        """Use around existing source_gate AND durable native intent creation.

        The caller retains its existing account lock; source editors only take
        this source lock. Owner readback uses neither admission nor this guard.
        """
        from .write_scope import NativeWriteScope
        if not isinstance(scope, NativeWriteScope): raise ContractError('central_actual_native_scope_required')
        project = scope.identity.get('item', {}).get('category') if scope.kind == 'background' else scope.identity.get('category')
        scope_key(project, scope.account)
        if Path(scope.lifecycle.db_path) not in {db for _, db in self.native_ledgers}:
            raise ContractError('central_unregistered_native_database')
        scope.check()
        with self.hold(project, scope.account):
            self.current(project, scope.account); scope.check()
            yield

