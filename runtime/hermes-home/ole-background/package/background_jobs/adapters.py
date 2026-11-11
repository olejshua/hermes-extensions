"""Reviewed read-only local artifact adapters and genuine private fake destinations."""
import json
import os
from pathlib import Path
import sqlite3
import time
from .contracts import (canonical,digest,private_dir,private_file,TemporaryFailure,PermanentFailure,ContractError,UncertainWrite,acquire_original,file_digest,approved_path)

class LocalReadonly:
    def __init__(self, store, policy, identity, contract, cancelled):
        self.store,self.policy,self.identity,self.contract,self.cancelled=store,policy,identity,contract,cancelled
        self.target=private_dir(store.root/'results'/identity['job_id']/digest(identity['item']['item_id']))/(identity['phase']+'.json')
    def readback(self, identity):
        if identity['phase']=='source':
            original=self.store.snapshot_path(identity['job_id'],identity['item']['item_id'])
            if not original.exists():return {'state':'absent','identity':identity}
            if original.is_symlink() or file_digest(original)!=identity['item']['source_hash']:return {'state':'conflict','identity':identity}
            return {'state':'verified','identity':identity,'destination_id':str(original.relative_to(self.store.root)),'proof_sha256':identity['item']['source_hash']}
        if not self.target.exists(): return {'state':'absent','identity':identity}
        packet=json.loads(self.target.read_bytes())
        if packet.get('identity') != identity or digest(packet.get('result')) != packet.get('result_hash'):
            return {'state':'conflict','identity':identity}
        return {'state':'verified','identity':identity,'destination_id':str(self.target.relative_to(self.store.root)),
                'proof_sha256':digest(self.target.read_bytes())}
    def preflight(self, identity):
        if identity['phase']=='source':
            approved_path(self.policy['sources_root'],identity['item']['source_ref']);return
        if digest(self.store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes()) != identity['item']['source_hash']:
            raise PermanentFailure('source_corrupt')
        if self.cancelled(): raise InterruptedError()
    def execute_once(self, identity, active):
        phase=identity['phase']
        if phase=='source':
            # Every original page is consumed; content stays only in immutable original.
            path=acquire_original(self.store,self.policy,identity,active)
            body=path.read_bytes()
            result={'source_hash':digest(body),'source_bytes':len(body),'full_source_available':True}
        elif phase=='analysis':
            from .native import run_native
            result=run_native(self.store,self.policy,identity,self.contract,self.cancelled,active)
        elif phase=='artifact':
            analysis=self.target.with_name('analysis.json')
            packet=json.loads(analysis.read_bytes())
            if packet['identity']['scope_hash']!=identity['scope_hash']: raise ContractError('analysis_scope_changed')
            result={'analysis_ref':str(analysis.relative_to(self.store.root)),'analysis_hash':digest(analysis.read_bytes()),
                    'review_required':True,'external_write':False}
        else: raise ContractError('unreviewed_phase')
        active()
        private_file(self.target,canonical({'identity':identity,'result':result,'result_hash':digest(result)}).encode())

class FakeAdapter:
    """Actual isolated SQLite draft/CRM destinations + source pagination, never a call counter stub."""
    def __init__(self, store, policy, identity, contract, cancelled):
        self.store,self.policy,self.identity,self.contract,self.cancelled=store,policy,identity,contract,cancelled
        self.db=store.root/'fixture-destinations.sqlite3'
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS results(key TEXT PRIMARY KEY,identity TEXT,result TEXT,result_hash TEXT,writes INTEGER)')
            c.execute('CREATE TABLE IF NOT EXISTS faults(key TEXT PRIMARY KEY,attempts INTEGER NOT NULL)')
        self.key=digest(identity)
        self.fault=policy.get('faults',{}).get(identity['item']['item_id']+':'+identity['phase'])
    def connect(self):
        c=sqlite3.connect(self.db,timeout=10);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');self.db.chmod(0o600);return c
    def readback(self,identity):
        if self.fault=='readback_unknown': return {'state':'unknown','identity':identity}
        with self.connect() as c: r=c.execute('SELECT * FROM results WHERE key=?',(self.key,)).fetchone()
        if r is None:return {'state':'absent','identity':identity}
        if json.loads(r['identity'])!=identity or digest(json.loads(r['result']))!=r['result_hash']:
            return {'state':'conflict','identity':identity}
        return {'state':'verified','identity':identity,'destination_id':'fixture:'+self.key,'proof_sha256':digest([r['identity'],r['result'],r['result_hash']])}
    def preflight(self,identity):
        if self.fault=='permanent': raise PermanentFailure()
        if self.fault in {'429','503'}:
            with self.connect() as c:
                c.execute('INSERT OR IGNORE INTO faults VALUES(?,0)',(self.key,))
                c.execute('UPDATE faults SET attempts=attempts+1 WHERE key=?',(self.key,))
                n=c.execute('SELECT attempts FROM faults WHERE key=?',(self.key,)).fetchone()[0]
            if n<=2: raise TemporaryFailure()
        if self.fault=='crash_before_attempt': os._exit(73)
    def execute_once(self,identity,active):
        if self.fault=='crash_before_write': os._exit(74)
        phase=identity['phase']
        if phase=='source':acquire_original(self.store,self.policy,identity,active)
        source=self.store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes().decode('utf-8')
        if self.policy.get('fake_delay_seconds') and identity['phase']==self.policy.get('fake_delay_phase','source'):
            until=time.monotonic()+self.policy['fake_delay_seconds']
            while time.monotonic()<until:
                active();time.sleep(.05)
        if phase=='source':
            cursor=0;pages=[];chunks=[]
            while cursor<len(source):
                active();end=min(cursor+self.policy.get('fake_page_size',31),len(source));pages.append([cursor,end]);chunks.append(source[cursor:end]);cursor=end
            if ''.join(chunks)!=source: raise ContractError('pagination_incomplete')
            result={'original_hash':digest(source.encode()),'pages':pages,'full_source_consumed':True}
        elif phase=='eligibility': result={'eligible':True,'source_hash':identity['item']['source_hash'],'account':identity['item']['account']}
        elif phase=='draft':
            # Fixture body is immutable evidence from the entire private fake original.
            result={'kind':'draft','body':'Fixture draft: '+source,'account':identity['item']['account'],'send_enabled':False}
        elif phase=='crm': result={'kind':'crm_note','source_hash':identity['item']['source_hash'],'owner':identity['item']['owner'],'note':source}
        elif phase=='readback':
            required=[]
            for prior in ('source','eligibility','draft','crm'):
                binding={**identity,'phase':prior}
                proof=FakeAdapter(self.store,self.policy,binding,self.contract,self.cancelled).readback(binding)
                if proof['state']!='verified': raise ContractError('final_destination_missing')
                required.append(proof)
            result={'ready':True,'destination_proofs':required,'gmail_crm_live_fixture_checked':True}
        else: raise ContractError('fixture_phase_unknown')
        active()
        with self.connect() as c:
            c.execute('INSERT INTO results VALUES(?,?,?,?,1)',(self.key,canonical(identity),canonical(result),digest(result)))
        if self.fault=='lost_response': raise UncertainWrite()
        if self.fault=='crash_after_write': os._exit(75)

def factory(store,policy):
    cls=FakeAdapter if policy['backend']=='fake' else LocalReadonly
    return lambda identity,contract,cancelled:cls(store,policy,identity,contract,cancelled)
