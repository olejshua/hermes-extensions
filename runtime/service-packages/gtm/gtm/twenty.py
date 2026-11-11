"""Source-bound Twenty notes; no deletion, contact permission, or implicit migration.

Writes require the coordinator's durable execute-once marker. Notes and links
are immutable targets. An uncertain operation is recovered by readback only.
"""
from __future__ import annotations
import copy
import hashlib
import importlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from gtm.coordinator import Identity, Readback

NAMESPACE=uuid.UUID('00000000-0000-4000-8000-00000000000f')
FIVE_STATES={'yes','no','conditional','not_discussed','unclear'}
OUTCOME_LABELS={'yes':'ja','no':'nein','conditional':'unter Bedingung','not_discussed':'nicht besprochen','unclear':'unklar'}
# Exact established triage.py labels; a completed Call uses Gesprochen, not an invented Stage.
EXISTING_REPLY_STAGES={'Antwort: Interesse','Antwort: Rueckfrage','Kein Interesse','Adresse pruefen'}

def validate_read_query(document):
    """Reject write documents before the retry-capable read transport.

    A skipped mutation is still a mutation: Twenty's native engine may ignore
    directives. Never use one as a validation/read-only probe.
    """
    if not isinstance(document,str):raise ValueError('twenty_read_query_required')
    # Adapter queries use variables for data and never need block strings.
    # Reject this unsupported form rather than partially parse its escapes.
    if '"""'in document:raise ValueError('twenty_read_query_required')
    code=re.sub(r'#[^\r\n]*|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"',' ',document)
    tokens=re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[{}]',code)
    if not tokens or tokens[0]not in ('query','{') or any(t in ('mutation','subscription')for t in tokens):
        raise ValueError('twenty_read_query_required')
    if any(re.match(r'^(?:create|update|delete|destroy|restore|upsert|merge|duplicate)[A-Z_]',t)for t in tokens):
        raise ValueError('twenty_read_query_required')

class StageReviewRequired(ValueError):
    """Keep the source Note; conflicting Stage changes need explicit review."""

@dataclass(frozen=True)
class Statement:
    speaker: str
    quote: str
    evidence: str
    party: str='unknown'  # explicit actor association, never inferred from proposal text

@dataclass(frozen=True)
class Outcome:
    state: str
    evidence: tuple[Statement,...]=()

@dataclass(frozen=True)
class NotePlan:
    identity: Identity
    company_id: str
    source_text: str
    summary: str
    statements: tuple[Statement,...]=()
    demo: Outcome | None=None
    followup_permission: Outcome | None=None
    occurred_at: str | None=None
    archive_url: str | None=None
    crm_review: dict | None=None
    crm_schema: dict | None=None
    action_review: str | None=None
    existing_note: dict | None=None
    existing_targets: dict | None=None

    def checked(self):
        self.identity.checked();uuid.UUID(self.company_id)
        if self.identity.kind not in ('reply','call'):
            raise ValueError('Only a reply or call can create a CRM result note')
        if hashlib.sha256(self.source_text.encode()).hexdigest()!=self.identity.source_hash:
            raise ValueError('Source text does not match immutable source hash')
        if not self.summary.strip():raise ValueError('Explicit source-grounded summary required')
        if self.existing_note:
            uuid.UUID(self.existing_note['id'])
            if self.identity.source_id not in json.dumps(self.existing_note,ensure_ascii=False):raise ValueError('Existing Note must carry exact source identity')
        if self.occurred_at is not None:
            occurred=datetime.fromisoformat(self.occurred_at.replace('Z','+00:00'))
            if occurred.tzinfo is None:raise ValueError('Source occurrence timestamp must include timezone')
        for statement in self.statements:
            self._check_statement(statement)
        if self.identity.kind=='call' and (self.demo is None or self.followup_permission is None):
            raise ValueError('Call demo and followup permission each require one of five states')
        for outcome in (self.demo,self.followup_permission):
            if outcome is None:continue
            if outcome.state not in FIVE_STATES:raise ValueError('Unknown outcome state')
            for statement in outcome.evidence:self._check_statement(statement)
            if outcome.state in ('yes','no','conditional'):
                if not outcome.evidence or any(s.party!='counterparty' or s.speaker.casefold() in ('alex','alex beispiel','jordan','jordan beispiel') for s in outcome.evidence):
                    raise ValueError('Explicit counterparty evidence required; our proposal is not their agreement')
        return self
    def _check_statement(self,s):
        if s.party not in ('counterparty','ole','piet','other','unknown') or not s.speaker.strip() or not s.evidence.strip() or not s.quote.strip() or s.quote not in self.source_text:
            raise ValueError('Original speaker, exact source quote, and evidence reference required')
    def id_for(self,kind):
        if kind=='note' and self.existing_note:return self.existing_note['id']
        if self.existing_targets and kind in self.existing_targets:return self.existing_targets[kind]
        key=json.dumps([self.identity.category,self.identity.account,self.identity.kind,self.identity.source_id,self.identity.version],separators=(',',':'))
        return str(uuid.uuid5(NAMESPACE,kind+':'+key))
    def note(self):
        if self.existing_note:return copy.deepcopy(self.existing_note)
        rows=[self.summary.strip(),'','Aussagen und Belege:']
        for s in self.statements:rows.append(f'- {s.speaker}: „{s.quote}“ ({s.evidence})')
        for title,outcome in (('Demo',self.demo),('Erlaubnis für Rückfragen',self.followup_permission)):
            if outcome is not None:
                rows.extend(['',f'{title}: {OUTCOME_LABELS[outcome.state]}'])
                rows.extend(f'- {s.speaker}: „{s.quote}“ ({s.evidence})' for s in outcome.evidence)
        from gtm.call_flow.crm import review_markdown
        if self.archive_url:rows.extend(['',f'Notion-Archiv: {self.archive_url}'])
        if self.crm_review:rows.append(review_markdown(self.crm_review,self.crm_schema))
        if self.action_review:rows.extend(['',self.action_review])
        rows.extend(['',f'Quelle: {self.identity.kind} {self.identity.source_id}, Version {self.identity.version}.'])
        # Machine provenance is separate from human narrative; hashes are not
        # presented as findings or evidence quotations.
        rows.extend(['','<!-- gtm-provenance '+json.dumps({'identity':self.identity.fields(),'occurred_at':self.occurred_at},sort_keys=True,separators=(',',':'))+' -->'])
        title='Gespräch' if self.identity.kind=='call' else 'Antwort auf Outreach'
        if self.occurred_at:
            title+=' vom '+datetime.fromisoformat(self.occurred_at.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin')).strftime('%d.%m.%Y')
        return {'id':self.id_for('note'),'title':title,
                'bodyV2':{'markdown':'\n'.join(rows)}}
    def target(self):return {'id':self.id_for('target'),'noteId':self.id_for('note'),'targetCompanyId':self.company_id}

class ExistingHelpersClient:
    """Private credential loading through the existing Acribic helper. No retries on writes."""
    def __init__(self,helper_dir:Path,key_path:Path):
        if not key_path.is_file():raise RuntimeError('Existing Twenty key file absent')
        sys.path.insert(0,str(helper_dir))
        try:self.helper=importlib.import_module('notion_twenty_migration')
        finally:sys.path.pop(0)
        self._key=key_path.read_text().strip()
        if not self._key:raise RuntimeError('Existing Twenty key file empty')
    def get(self,kind,rid):
        plural,singular={'company':('companies','company'),'note':('notes','note'),'target':('noteTargets','noteTarget'),'person':('people','person'),'opportunity':('opportunities','opportunity')}[kind]
        try:
            result=self.helper.request_json('GET',f'{self.helper.TWENTY_URL}/rest/{plural}/{rid}',self._key)
            record=result['data'][singular]
            if not isinstance(record,dict):raise RuntimeError('Malformed Twenty record')
            return record
        except Exception as exc:
            # Only a real HTTP404 in the exception cause chain means absent.
            cause=exc
            while cause is not None:
                if getattr(cause,'code',None)==404:return None
                cause=cause.__cause__
            raise RuntimeError('Twenty record read unavailable; no write allowed') from None
    def query(self,query,variables=None):
        validate_read_query(query)
        result=self.helper.request_json('POST',self.helper.TWENTY_URL+'/graphql',self._key,body={'query':query,'variables':variables or {}})
        if result.get('errors'):raise RuntimeError('Twenty query unavailable')
        return result['data']
    def metadata(self):
        result=self.helper.request_json('GET',self.helper.TWENTY_URL+'/rest/metadata/objects',self._key)
        if result.get('pageInfo',{}).get('hasNextPage'):raise RuntimeError('Twenty metadata requires full pagination')
        return result['data']
    def write_once(self,query,variables):
        try:return self.helper.write_once('POST',self.helper.TWENTY_URL+'/graphql',self._key,{'query':query,'variables':variables})
        except Exception:raise RuntimeError('Twenty write output uncertain; use exact readback, no retry') from None

def note_matches(record,expected):
    """Exact planned identity/title/Markdown; BlockNote is server rendering.

    The REST API generates bodyV2.blocknote even for Markdown-only creates.
    Never treat an unknown/malformed record as absence or ignore content drift.
    """
    if not isinstance(record,dict) or not isinstance(expected,dict):return False
    if set(expected)!={'id','title','bodyV2'}:return False
    body=record.get('bodyV2');planned=expected.get('bodyV2')
    if not isinstance(body,dict) or not isinstance(planned,dict):return False
    if set(body)-{'markdown','blocknote'} or set(planned)-{'markdown','blocknote'}:return False
    if not isinstance(body.get('markdown'),str) or not isinstance(planned.get('markdown'),str):return False
    if 'blocknote' in body and body['blocknote'] is not None and not isinstance(body['blocknote'],str):return False
    return (record.get('id')==expected['id'] and record.get('title')==expected['title']
            and body['markdown']==planned['markdown'])


class TwentyNoteAdapter:
    def __init__(self,client,plan:NotePlan,*,declared_twenty_projects=frozenset({'acribic'})):
        self.client,self.plan=client,plan.checked()
        self.declared=frozenset(declared_twenty_projects)
    def _identity(self,identity):
        if identity!=self.plan.identity:raise ValueError('Wrong source/version/account identity')
        if identity.category not in self.declared:raise ValueError('No explicit project-to-Twenty mapping; no migration allowed')
    def preflight(self,identity):
        self._identity(identity)
        company=self.client.get('company',self.plan.company_id)
        if not company or company.get('id')!=self.plan.company_id:raise ValueError('Existing exact company target required')
        if self.readback(identity).state!='absent':raise ValueError('Create requires confirmed absent note and target')
    def readback(self,identity):
        self._identity(identity)
        try:n,t=self.client.get('note',self.plan.id_for('note')),self.client.get('target',self.plan.id_for('target'))
        except Exception:return Readback('unknown',identity)
        if n is None and t is None:return Readback('absent',identity)
        if n is None or t is None:return Readback('unknown',identity)
        expected=self.plan.note();target=self.plan.target()
        if (not note_matches(n,expected) or any(t.get(k)!=v for k,v in target.items())):
            return Readback('conflict',identity)
        return Readback('verified',identity,expected['id'])
    def execute_once(self,identity):
        self.preflight(identity)
        # GraphQL root fields are sequential, not claimed transactional. A partial
        # result remains unknown and must be reconciled, never upserted/replayed.
        self.client.write_once('''mutation($note:NoteCreateInput!,$target:NoteTargetCreateInput!) {
          createNote(data:$note,upsert:false){id}
          createNoteTarget(data:$target,upsert:false){id}
        }''',{'note':self.plan.note(),'target':self.plan.target()})


def merge_source_overlay(company,source_page_id,updates):
    """Build a lossless narrow overlay; no email permission or Company overwrite."""
    allowed={'Stage','GTM Ergebnisse','Gesprochen'}
    if not updates or set(updates)-allowed:raise ValueError('Unsupported update or contact permission field')
    if source_page_id is None:
        if 'Stage' in updates:raise StageReviewRequired('No invented native Company Stage')
        return {}
    sources=copy.deepcopy(company.get('notionQuellen'))
    if not isinstance(sources,list):raise ValueError('Existing source overlay required')
    selected=[s for s in sources if s.get('page_id')==source_page_id]
    if len(selected)!=1:raise ValueError('Exact unique source page required')
    overlay=selected[0].setdefault('outreach',{})
    current_stage=str({**selected[0]['properties'],**overlay}.get('Stage') or '')
    if 'Stage' in updates and updates['Stage']!=current_stage and updates['Stage'] not in EXISTING_REPLY_STAGES:
        raise StageReviewRequired('Stage not in established reply vocabulary; preserve Note and review')
    blocked=('gesperrt','kein interesse','nicht kontaktieren','keine anfragen','absage','opt-out','optout')
    if 'Stage' in updates and updates['Stage']!=current_stage and any(word in current_stage.casefold() for word in blocked):
        raise StageReviewRequired('Blocked/opt-out source stage cannot be reopened by a result update')
    if 'Gesprochen' in updates and updates['Gesprochen'] is not True:
        raise ValueError('Call occurrence only supports confirmed true, never reset')
    merged=copy.deepcopy(updates)
    if 'GTM Ergebnisse' in merged:
        prior=overlay.get('GTM Ergebnisse',{})
        if not isinstance(prior,dict) or not isinstance(merged['GTM Ergebnisse'],dict):
            raise ValueError('GTM outcomes must be keyed by stable source identity')
        for key,value in merged['GTM Ergebnisse'].items():
            if key in prior and prior[key]!=value:raise ValueError('Source result already exists with conflicting content')
        merged['GTM Ergebnisse']={**prior,**merged['GTM Ergebnisse']}
    overlay.update(merged)
    stages=sorted({str({**s['properties'],**s.get('outreach',{})}.get('Stage')) for s in sources if {**s['properties'],**s.get('outreach',{})}.get('Stage')})
    return {'notionQuellen':sources,'notionStages':'; '.join(stages),**({'notionVersandbereit':False} if updates.get('Stage') and updates['Stage']!='Nicht kontaktiert' else {})}

REVIEWED_SERVER_VERSION='v2.42.6'

class ConditionalStatusWrite:
    """Separate CAS operation bound to the reviewed official server version.

    Schema evidence alone is insufficient. Caller must prove the server evaluates
    id+updatedAt in one UPDATE predicate, not a read followed by update-by-ID.
    """
    def __init__(self,client,*,server_version=None):
        self.client,self.server_version=client,server_version
    def execute_once(self,company,source_page_id,updates,*,call_occurred=False,occurred_at=None):
        if self.server_version!=REVIEWED_SERVER_VERSION:raise ValueError('Running server version not matched to reviewed atomic CAS code; linked note only')
        if not company.get('id') or not company.get('updatedAt'):raise ValueError('Exact live company revision required')
        data=merge_source_overlay(company,source_page_id,updates)
        if call_occurred:
            data.update(kontaktiert=True,notionVersandbereit=False)
            if not company.get('kontaktiertAm') and occurred_at is not None:
                data['kontaktiertAm']=datetime.fromisoformat(occurred_at.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()
        result=self.client.write_once('''mutation($data:CompanyUpdateInput!,$filter:CompanyFilterInput!){
            updateCompanies(data:$data,filter:$filter){id updatedAt}
        }''',{'data':data,'filter':{'and':[{'id':{'eq':company['id']}},{'updatedAt':{'eq':company['updatedAt']}}]}})
        rows=(result.get('data') or {}).get('updateCompanies')
        if (result.get('errors') or not isinstance(rows,list) or len(rows)!=1 or
            rows[0].get('id')!=company['id'] or not rows[0].get('updatedAt') or
            rows[0]['updatedAt']==company['updatedAt']):
            raise RuntimeError('CAS did not return exactly one changed company; reconcile, no retry')
        live=self.client.get('company',company['id'])
        if not live or any(live.get(k)!=v for k,v in data.items()):
            raise RuntimeError('Conditional status write conflicted or uncertain; reconcile, never retry')
        return live

class TwentyStatusAdapter:
    """Coordinator-compatible independent phase for one preplanned CAS overlay.

    Capture `company_snapshot` from a targeted live read. Run only after its Note
    phase. A newer differing revision is unknown, never silently re-merged/retried.
    """
    def __init__(self,client,note_plan,company_snapshot,source_page_id,updates,*,server_version=None,
                 declared_twenty_projects=frozenset({'acribic'})):
        self.client,self.plan=client,note_plan.checked()
        self.company=copy.deepcopy(company_snapshot)
        if self.company.get('id')!=self.plan.company_id:raise ValueError('Snapshot company differs from exact plan target')
        if set(updates)-{'Stage'}:raise ValueError('Status adapter builds its own source-bound outcomes')
        result={'source':self.plan.identity.fields(),'summary':self.plan.summary,
                'statements':[asdict(s) for s in self.plan.statements],
                'demo':asdict(self.plan.demo) if self.plan.demo else None,
                'followup_permission':asdict(self.plan.followup_permission) if self.plan.followup_permission else None}
        result['occurred_at']=self.plan.occurred_at
        if self.plan.crm_review is not None:result['crm_review']=copy.deepcopy(self.plan.crm_review)
        if self.plan.archive_url:result['archive_url']=self.plan.archive_url
        bound_updates={**copy.deepcopy(updates),'GTM Ergebnisse':{self.plan.id_for('note'):result}}
        if self.plan.identity.kind=='call':bound_updates['Gesprochen']=True
        # Freeze the exact JSON wire representation. dataclasses.asdict keeps
        # Outcome.evidence tuples; Twenty correctly reads JSON arrays as lists.
        # This changes no payload content and keeps strict source readback.
        bound_updates=json.loads(json.dumps(bound_updates,ensure_ascii=False,allow_nan=False))
        self.source_page_id,self.updates=source_page_id,bound_updates
        self.data=self._data(bound_updates)
        self.writer=ConditionalStatusWrite(client,server_version=server_version)
        self.notes=TwentyNoteAdapter(client,note_plan,declared_twenty_projects=declared_twenty_projects)
    def _data(self,updates):
        data=merge_source_overlay(self.company,self.source_page_id,updates)
        if self.plan.identity.kind=='call':
            data.update(kontaktiert=True,notionVersandbereit=False)
            if not self.company.get('kontaktiertAm') and self.plan.occurred_at:
                data['kontaktiertAm']=datetime.fromisoformat(self.plan.occurred_at.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()
        return data
    def readback(self,identity):
        self.notes._identity(identity)
        try:live=self.client.get('company',self.plan.company_id)
        except Exception:return Readback('unknown',identity)
        if not live or live.get('id')!=self.plan.company_id:return Readback('conflict',identity)
        if all(live.get(k)==v for k,v in self.data.items()):
            return Readback('verified',identity,self.plan.company_id)
        if live.get('updatedAt')==self.company.get('updatedAt'):
            return Readback('absent',identity)
        return Readback('unknown',identity)
    def preflight(self,identity):
        self.notes._identity(identity)
        if self.writer.server_version!=REVIEWED_SERVER_VERSION:
            raise ValueError('Running release must match reviewed atomic CAS code')
        if self.notes.readback(identity).state!='verified':raise ValueError('Source-bound Note/link must already be verified')
        if self.readback(identity).state!='absent':raise ValueError('Exact unchanged source revision required')
    def execute_once(self,identity):
        self.preflight(identity)
        self.writer.execute_once(self.company,self.source_page_id,self.updates,call_occurred=self.plan.identity.kind=='call',occurred_at=self.plan.occurred_at)

class TwentyRecordAdapter(TwentyNoteAdapter):
    """Production component phase: one Note OR one NoteTarget per durable marker.

    A crash between phases leaves the Note verified. Recovery creates only the
    confirmed-absent target, never replays a combined pair or edits an old note.
    """
    def __init__(self,client,plan,*,component,declared_twenty_projects=frozenset({'acribic'})):
        super().__init__(client,plan,declared_twenty_projects=declared_twenty_projects)
        if component not in ('note','target'):raise ValueError('Explicit note or target component required')
        self.component=component
    def _note_verified(self):
        note=self.client.get('note',self.plan.id_for('note'))
        expected=self.plan.note()
        return note_matches(note,expected)
    def readback(self,identity):
        self._identity(identity)
        kind='note' if self.component=='note' else 'target'
        expected=self.plan.note() if kind=='note' else self.plan.target()
        try:
            record=self.client.get(kind,expected['id'])
            if kind=='target' and record is not None:
                if not self._note_verified():return Readback('conflict',identity)
                company=self.client.get('company',self.plan.company_id)
                if not company or company.get('id')!=self.plan.company_id:return Readback('conflict',identity)
        except Exception:return Readback('unknown',identity)
        if record is None:return Readback('absent',identity)
        if (not note_matches(record,expected) if kind=='note' else any(record.get(k)!=v for k,v in expected.items())):
            return Readback('conflict',identity)
        return Readback('verified',identity,expected['id'])
    def preflight(self,identity):
        self._identity(identity)
        company=self.client.get('company',self.plan.company_id)
        if not company or company.get('id')!=self.plan.company_id:raise ValueError('Existing exact company target required')
        if self.component=='target' and not self._note_verified():
            raise ValueError('Exact source/version Note must be verified before target creation')
        if self.readback(identity).state!='absent':raise ValueError('Component create requires confirmed absent record')
    def execute_once(self,identity):
        self.preflight(identity)
        if self.component=='note':
            query='mutation($data:NoteCreateInput!){createNote(data:$data,upsert:false){id}}'
            data=self.plan.note()
        else:
            query='mutation($data:NoteTargetCreateInput!){createNoteTarget(data:$data,upsert:false){id}}'
            data=self.plan.target()
        self.client.write_once(query,{'data':data})
