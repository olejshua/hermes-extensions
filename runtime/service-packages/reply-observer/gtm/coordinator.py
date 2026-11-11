"""Durable phase execution; an uncertain external write is never replayed.

The existing LifecycleStore owns leases/state. This module adds immutable source
identity and phase dependencies, not a second generic job lifecycle. An adapter's
readback must verify the destination against the exact planned source version.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Protocol

from hermes_second_brain.lifecycle import LifecycleStore, coerce_utc_clock

PLANS = {
    'hold_cleanup': {'source': (), 'invitation': ('source',), 'hold_release': ('invitation',)},
    'call': {'source': (), 'analysis': ('source',), 'notion': ('analysis',),
             'twenty_note': ('analysis',), 'twenty_link': ('twenty_note',),
             'twenty_status': ('twenty_link',), 'monolith': ('analysis', 'notion')},
    'reply': {'source': (), 'classification': ('source',), 'twenty_note': ('classification',),
              'twenty_link': ('twenty_note',), 'twenty_status': ('twenty_link',),
              'draft': ('classification',), 'invitation': ('classification',),
              'hold_release': ('invitation',)},
    'briefing': {'source': (), 'briefing': ('source',), 'delivery': ('briefing',)},
    'outreach': {'sources': (), 'eligibility': ('sources',), 'draft': ('eligibility',),
                 'readback': ('draft',)},
}
ACCOUNTS = {'acribic': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}


@dataclass(frozen=True)
class Identity:
    category: str
    account: str
    kind: str
    source_id: str
    version: int
    source_hash: str

    def checked(self):
        if self.category not in ACCOUNTS or self.account != ACCOUNTS[self.category]:
            raise ValueError('Project/account identity mismatch')
        if self.kind not in PLANS or type(self.version) is not int or self.version < 1:
            raise ValueError('Explicit kind and positive source version required')
        if not isinstance(self.source_id, str) or not re.fullmatch(r'[A-Za-z0-9_:@.\-/]{1,256}', self.source_id):
            raise ValueError('Stable source ID required; no URLs, commands or secrets')
        if not re.fullmatch(r'[a-f0-9]{64}', self.source_hash):
            raise ValueError('Exact source SHA256 required')
        return self

    def fields(self):
        return dict(category=self.category, account=self.account, kind=self.kind,
                    source_id=self.source_id, version=self.version, source_hash=self.source_hash)

    @property
    def key(self):
        fields = self.fields(); fields.pop('source_hash')
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Readback:
    state: str  # absent, verified, unknown, or conflict
    identity: Identity
    destination_id: str | None = None


class Adapter(Protocol):
    def readback(self, identity: Identity) -> Readback: ...
    def preflight(self, identity: Identity) -> None: ...
    def execute_once(self, identity: Identity) -> None: ...


class Coordinator:
    def __init__(self, db_path: Path | str):
        self.store = LifecycleStore(db_path)
        with self.store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS gtm_sources(
                workflow_id TEXT PRIMARY KEY, identity TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS gtm_reply_plans(
                workflow_id TEXT PRIMARY KEY, phases TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS gtm_attempts(
                phase_key TEXT PRIMARY KEY, verification_only INTEGER NOT NULL CHECK(verification_only=1))''')

    def plan(self, identity: Identity, *, reply_phases=None):
        identity.checked(); canonical = json.dumps(identity.fields(), sort_keys=True)
        if reply_phases is not None:
            if identity.kind!='reply' or tuple(reply_phases)!=('source','classification','invitation'):
                raise ValueError('Only the source-bound invitation-only reply subset is supported')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?', (identity.key,)).fetchone()
            if old and old['identity'] != canonical:
                raise ValueError('Source changed under same ID/version; retain prior work and review')
            if identity.kind=='reply':
                saved=conn.execute('SELECT phases FROM gtm_reply_plans WHERE workflow_id=?',(identity.key,)).fetchone()
                if reply_phases is not None:
                    encoded=json.dumps(list(reply_phases))
                    if (saved and saved['phases']!=encoded) or (old and not saved):
                        raise ValueError('Existing reply plan cannot be reinterpreted')
                else:encoded=saved['phases'] if saved else json.dumps(list(PLANS['reply']))
                # Scope and immutable source commit together before any enqueue.
                if not old or saved:
                    conn.execute('INSERT OR IGNORE INTO gtm_reply_plans VALUES(?,?)',(identity.key,encoded))
            conn.execute('INSERT OR IGNORE INTO gtm_sources VALUES(?,?)', (identity.key, canonical))
        # Idempotent enqueue makes a crash during planning repairable.
        for phase in self._phases(identity):
            # Retries are recovery/readback attempts. The durable attempt marker
            # independently prevents another external write across leases.
            self.store.enqueue_job(idempotency_key=self._phase_key(identity, phase), job_kind='gtm_' + phase, max_attempts=3)
        return self.status(identity)

    def _phases(self,identity):
        phases=dict(PLANS[identity.kind])
        if identity.kind=='reply':
            with self.store.connect() as conn:
                saved=conn.execute('SELECT phases FROM gtm_reply_plans WHERE workflow_id=?',(identity.key,)).fetchone()
            if saved:
                selected=json.loads(saved['phases'])
                if selected not in (list(PLANS['reply']),['source','classification','invitation']):
                    raise ValueError('Invalid immutable reply plan')
                phases={key:phases[key] for key in selected}
        return phases

    def _phase_key(self, identity, phase):
        return 'gtm:' + identity.key + ':' + phase

    def _job_id(self, identity, phase):
        if phase not in self._phases(identity):
            raise ValueError('Phase unsupported; no automatic email send phase exists')
        return 'job_' + hashlib.sha256(self._phase_key(identity, phase).encode()).hexdigest()[:32]

    def status(self, identity):
        identity.checked()
        with self.store.connect() as conn:
            row = conn.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?', (identity.key,)).fetchone()
        if row is None or json.loads(row['identity']) != identity.fields():
            raise ValueError('Workflow identity is not planned')
        return {'workflow_id': identity.key, 'kind': identity.kind, 'category': identity.category,
                'version': identity.version, 'source_hash': identity.source_hash,
                'phases': {phase: self.store.get_job(self._job_id(identity, phase))['state']
                           for phase in self._phases(identity)}, 'email_send_enabled': False}

    def run_phase(self, identity: Identity, phase: str, adapter: Adapter, *, now=None):
        self.status(identity)
        job_id = self._job_id(identity, phase)
        for requirement in self._phases(identity)[phase]:
            if self.store.get_job(self._job_id(identity, requirement))['state'] != 'completed':
                return {'state': 'waiting_dependency', 'dependency': requirement}
        job = self.store.get_job(job_id)
        if job['state'] == 'completed':
            return {'state': 'completed', 'reused': True}
        if job['state'] != 'queued':
            # A previous process may still be active. Only verify its target; do
            # not acquire its lease or issue another external write.
            proof = adapter.readback(identity)
            self._check_readback(proof, identity)
            if proof.state == 'absent':
                # A live owner stays protected. Recovery may write only after
                # the expired native lease and the unattempted marker are proven.
                recovered = (self.recover_hold_release(identity, phase, adapter, now=now)
                             if phase == 'hold_release' else
                             self.recover_unattempted(identity, phase, adapter, now=now))
                if recovered['state'] != 'waiting_owner': return recovered
            return {'state': 'verified_result_recovery_required' if proof.state == 'verified' else 'waiting_readback',
                    'write_replayed': False}
        owner = 'gtm_' + uuid.uuid4().hex
        try:
            claimed = self.store.start_job(job_id, owner=owner, lease_seconds=300, now=now)
        except ValueError:
            return {'state': 'waiting_owner', 'write_replayed': False}
        return self._run_owned(identity, phase, adapter, job_id, owner, claimed['lease_generation'], now=now)

    def _run_owned(self, identity, phase, adapter, job_id, owner, generation, *, now=None):
        try:return self._execute_owned(identity, phase, adapter, job_id, owner, generation, now=now)
        except Exception:
            with self.store.connect() as conn:
                attempted=conn.execute('SELECT 1 FROM gtm_attempts WHERE phase_key=?',(self._phase_key(identity,phase),)).fetchone()
            if not attempted:
                # A caught pre-write failure releases its own native lease.
                # A process crash cannot do this and needs expired-lease recovery.
                # Do not waste intake retries while a known-safe failure appears
                # to retain a live downstream owner for another five minutes.
                try:self.store.fail_job(job_id,owner=owner,lease_generation=generation,error_type='gtm_unattempted_error',now=now)
                except Exception:pass # expired/lost fence remains recovery-only
            raise

    def _execute_owned(self, identity, phase, adapter, job_id, owner, generation, *, now=None):
        proof = adapter.readback(identity)
        self._check_readback(proof, identity)
        with self.store.connect() as conn:
            attempted = conn.execute('SELECT 1 FROM gtm_attempts WHERE phase_key=?', (self._phase_key(identity, phase),)).fetchone()
        partial = self._is_hold_release(phase, adapter, identity)
        if proof.state == 'absent' and (not attempted or partial):
            adapter.preflight(identity)
            # Durable marker precedes the potentially ambiguous network write.
            self.store.checkpoint_job(job_id, owner=owner, lease_generation=generation, checkpoint='execute_once_attempted', now=now)
            if not attempted and not self._mark_attempt(identity, phase, job_id, owner, generation, now=now):
                return {'state': 'waiting_readback', 'write_replayed': False}
            try:
                if partial:
                    adapter.bind_native_fence(lambda: self.store.heartbeat(job_id, owner=owner, lease_generation=generation, lease_seconds=300, now=now))
                if attempted and partial:adapter.resume_pending(identity)
                else:adapter.execute_once(identity)
            except Exception:
                # Readback is still allowed after an uncertain action; it is
                # never followed by a second write.
                pass
            proof = adapter.readback(identity)
            self._check_readback(proof, identity)
        if proof.state != 'verified':
            return {'state': 'waiting_readback', 'write_replayed': False}
        return self._complete(identity, job_id, owner, generation, proof, now=now)

    def _mark_attempt(self, identity, phase, job_id, owner, generation, *, now=None):
        # Checkpoint and marker are separate transactions. Fence again inside
        # the marker transaction so a delayed old owner cannot race its successor.
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            clock = coerce_utc_clock(now).timestamp()
            row = conn.execute('SELECT owner,lease_generation,lease_until,state FROM lifecycle_jobs WHERE job_id=?', (job_id,)).fetchone()
            if (not row or row['owner'] != owner or row['lease_generation'] != generation or
                row['lease_until'] <= clock or row['state'] != 'checkpointed'):
                raise ValueError('Native phase ownership expired before attempt')
            key = self._phase_key(identity, phase)
            if conn.execute('SELECT 1 FROM gtm_attempts WHERE phase_key=?', (key,)).fetchone():
                return False
            conn.execute('INSERT INTO gtm_attempts VALUES(?,1)', (key,))
        return True

    @staticmethod
    def _is_hold_release(phase, adapter, identity=None):
        if phase != 'hold_release' or (identity is not None and identity.kind not in ('reply','hold_cleanup')):return False
        from gtm.calendar.invitation import HoldReleaseAdapter
        return isinstance(adapter, HoldReleaseAdapter)

    def recover_hold_release(self, identity, phase, adapter, *, now=None):
        if not self._is_hold_release(phase, adapter, identity):raise ValueError('Only journaled HoldReleaseAdapter supports partial recovery')
        return self.recover_unattempted(identity, phase, adapter, now=now, _partial=True)

    def recover_unattempted(self, identity: Identity, phase: str, adapter: Adapter, *, now=None, _partial=False):
        """Reclaim an expired pre-write failure, never an uncertain write.

        Authoritative absence, no durable attempt, native fencing, then fresh
        readback/preflight are all required. Unknown never means absent.
        """
        self.status(identity); job_id = self._job_id(identity, phase)
        for dependency in self._phases(identity)[phase]:
            if self.store.get_job(self._job_id(identity, dependency))['state'] != 'completed':
                return {'state': 'waiting_dependency', 'dependency': dependency}
        with self.store.connect() as conn:
            attempted = conn.execute('SELECT 1 FROM gtm_attempts WHERE phase_key=?', (self._phase_key(identity, phase),)).fetchone()
        if _partial and not self._is_hold_release(phase, adapter, identity):raise ValueError('Unsupported compound recovery')
        if attempted and not _partial:return {'state': 'waiting_readback', 'write_replayed': False}
        self.store.detect_stale_jobs(now=now)
        job = self.store.get_job(job_id)
        if job['state'] == 'completed':return {'state': 'completed', 'reused': True}
        if job['state'] not in ('stalled', 'failed', 'queued'):
            return {'state': 'waiting_owner', 'write_replayed': False}
        proof = adapter.readback(identity); self._check_readback(proof, identity)
        if proof.state != 'absent':return {'state': 'waiting_readback', 'write_replayed': False}
        if job['state'] != 'queued':
            with self.store.connect() as conn:
                prior = conn.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?', (job_id,)).fetchone()
            try:self.store.retry_job(job_id, owner=prior['owner'], lease_generation=prior['lease_generation'], now=now)
            except ValueError:return {'state': 'waiting_owner', 'write_replayed': False}
        owner = 'gtm_' + uuid.uuid4().hex
        try:claimed = self.store.start_job(job_id, owner=owner, lease_seconds=300, now=now)
        except ValueError:return {'state': 'waiting_owner', 'write_replayed': False}
        return self._run_owned(identity, phase, adapter, job_id, owner, claimed['lease_generation'], now=now)

    def recover_verified(self, identity: Identity, phase: str, adapter: Adapter, *, now=None):
        """Finish an abandoned phase only from a verified existing destination.

        A live lease cannot be stolen. After expiry, use the existing lifecycle's
        fencing/retry path. Crash at any point permanently retains readback-only
        mode; never repeat the external operation.
        """
        self.status(identity); job_id = self._job_id(identity, phase)
        proof = adapter.readback(identity); self._check_readback(proof, identity)
        if proof.state != 'verified':
            return {'state': 'waiting_readback', 'write_replayed': False}
        job = self.store.get_job(job_id)
        if job['state'] == 'completed':
            return {'state': 'completed', 'reused': True}
        self.store.detect_stale_jobs(now=now)
        job = self.store.get_job(job_id)
        if job['state'] not in ('stalled', 'failed', 'queued'):
            return {'state': 'waiting_owner', 'write_replayed': False}
        with self.store.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO gtm_attempts VALUES(?,1)', (self._phase_key(identity, phase),))
        if job['state'] != 'queued':
            # The native public status intentionally omits owner identity.
            with self.store.connect() as conn:
                prior = conn.execute('SELECT owner, lease_generation FROM lifecycle_jobs WHERE job_id=?', (job_id,)).fetchone()
            self.store.retry_job(job_id, owner=prior['owner'], lease_generation=prior['lease_generation'], now=now)
        owner = 'gtm_' + uuid.uuid4().hex
        try:
            claimed = self.store.start_job(job_id, owner=owner, lease_seconds=300, now=now)
        except ValueError:
            return {'state': 'waiting_owner', 'write_replayed': False}
        # Recheck after acquiring ownership; never finalize an old observation.
        proof = adapter.readback(identity); self._check_readback(proof, identity)
        if proof.state != 'verified':
            return {'state': 'waiting_readback', 'write_replayed': False}
        return self._complete(identity, job_id, owner, claimed['lease_generation'], proof, now=now)

    def _complete(self, identity, job_id, owner, generation, proof, *, now=None):
        self.store.begin_verification(job_id, owner=owner, lease_generation=generation, now=now)
        receipt = json.dumps({'identity_hash': identity.key, 'source_hash': identity.source_hash,
                              'destination_hash': hashlib.sha256(proof.destination_id.encode()).hexdigest()}, sort_keys=True)
        self.store.complete_job(job_id, owner=owner, lease_generation=generation, verification_evidence=receipt, now=now)
        return {'state': 'completed', 'reused': False, 'write_replayed': False}

    @staticmethod
    def _check_readback(proof, identity):
        if not isinstance(proof, Readback) or proof.identity != identity:
            raise ValueError('Adapter readback is not bound to planned account/source/version/hash')
        if proof.state not in ('absent', 'verified', 'unknown', 'conflict'):
            raise ValueError('Unknown readback state')
        if proof.state == 'verified' and (not isinstance(proof.destination_id, str) or not proof.destination_id):
            raise ValueError('Verified destination ID required')
