"""Current complete domain checks against originals and the exact final MIME.

The N receipt is historical preparation. Each call checks current complete-pool
changes, authoritative history, explicit template, Gmail, TED and exact SMTP.
"""
import base64
from datetime import datetime,timezone
import json
from background_jobs.contracts import ContractError,canonical,digest,private_file,PermanentFailure,approved_path
from background_jobs.outreach_candidate import content,draft_raw,gmail_id
from .sources import persist
from .templates import checked,document
from .protections import permits
from .ted import award_facts
from .sourcegraph import checked_dependencies


class AcribicReadiness:
    def __init__(self,store,gmail_sources,twenty_sources,catalog,*,pool_source=None,
                 protection_source=None,template_source=None,smtp=None,active=lambda:None):
        self.store,self.gmail,self.twenty,self.catalog=store,gmail_sources,twenty_sources,catalog
        self.pool,self.protections,self.templates,self.smtp=pool_source,protection_source,template_source,smtp
        self.active=active
        if gmail_sources.category!='acribic':raise ContractError('project_account_mismatch')

    def _original(self,identity):
        raw=self.store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes()
        if digest(raw)!=identity['item']['source_hash']:raise ContractError('outreach_original_changed')
        checked_dependencies(self.store.root,raw)
        value=json.loads(raw)
        if value.get('missing_native_sources'):raise PermanentFailure('native_quality_required_original_sources_missing')
        return value

    def _facts(self,identity,plan,bundle,values,current_crm):
        variables=checked(plan,document())
        if variables['firm']!=values.get('Firma'):raise ContractError('approved_template_company_binding_changed')
        documents=bundle['documents'];facts=[];ted=[];company=[];smtp=[]
        for d in documents:
            if digest(d['content'].encode())!=d.get('sha256'):raise ContractError('outreach_original_changed')
            if d['kind']=='ted':
                packet=json.loads(d['content'])['original']
                raw=base64.b64decode(packet['xml_base64'],validate=True)
                actual=award_facts(raw,variables['firm'])
                if actual!=packet['awards']:raise ContractError('ted_award_original_changed')
                facts.extend({**f,'publication':packet['publication']}for f in actual);ted.append(d)
            if d['kind']=='company':company.append(d)
            if d['kind']=='smtp':smtp.append(d)
        if (len({(f['procedure_id'],f['contract_reference']or f['contract_id'])for f in facts})<2 or
            variables['buyer']not in {f['buyer']for f in facts}):
            raise PermanentFailure('ted_template_buyer_or_repeated_awards_unproved')
        # Presence is no semantic proof: the same native quality worker must
        # judge own bidder role/pressure and cite its actual corporate source.
        evidence=plan.get('evidence',[])
        if not company or not any(e.get('source_id')==d['source_id']and isinstance(e.get('quote'),str)and
            e['quote'].strip()and e['quote']in d['content']for e in evidence for d in company):
            raise PermanentFailure('company_role_and_pressure_source_citation_required')
        if variables['first_name']is not None:
            contacts=[json.loads(d['content'])['original']for d,_ in current_crm if d['source_id'].startswith('twenty-person:')]
            if len(contacts)!=1 or contacts[0].get('name',{}).get('firstName')!=variables['first_name']:
                raise PermanentFailure('verified_contact_first_name_required')
        if len(smtp)!=1:raise PermanentFailure('current_individual_smtp_recipient_original_required')
        return {'ted':ted,'company':company,'smtp':smtp[0]}

    def capture_available(self,identity,plan,*,existing_draft_id=None):
        if any(v is None for v in (self.pool,self.protections,self.templates,self.smtp)):
            raise PermanentFailure('native_ted_smtp_original_adapters_missing_or_full_source_gate_unbound')
        active=self.active;active();candidate=plan['candidate']
        if identity['item']['account']!=self.gmail.account or identity['item']['category']!='acribic':
            raise ContractError('project_account_mismatch')
        bundle=self._original(identity)
        if bundle.get('candidate')!=candidate:raise ContractError('outreach_candidate_binding_changed')
        binding=bundle.get('source_binding',{}).get('acquisition')or {}
        selection=binding.get('selection_manifest');name=(binding.get('recipe')or {}).get('name')
        if not isinstance(selection,dict)or not isinstance(name,str):raise ContractError('native_full_pool_original_required')
        current,pool_original=self.pool.refresh_from(selection,name,active,own_identity=identity)
        if candidate not in current:raise PermanentFailure('current_complete_crm_pool_blocks_candidate')
        protection=self.protections.capture({'identity':identity,'candidate':candidate},active)
        active();crm=self.twenty.capture(candidate);active()
        company=json.loads(crm[0][0]['content'])['original']
        source=[s for s in company['notionQuellen']if str(s['page_id']).replace('-','')==candidate['source_id'].replace('-','')]
        if len(source)!=1:raise ContractError('existing_source_binding_missing')
        values={**source[0]['properties'],**source[0].get('outreach',{})}
        if not permits(candidate,values.get('Firma',''),protection[0]):raise PermanentFailure('current_authoritative_protections_block_candidate')
        template_originals,template=self.templates.capture({'identity':identity,'candidate':candidate},active)
        checked(plan,template)
        contexts={candidate['item_id']:{'company_names':list(dict.fromkeys(n for n in (company.get('name'),values.get('Firma'))if n))}}
        mail=self.gmail.capture_many([candidate],self.catalog,contexts=contexts,active=active)[candidate['item_id']]
        expected=content(draft_raw(identity,plan));content_hash=digest(expected);own_message=None
        if existing_draft_id is not None:
            active();draft=self.gmail.gmail.users().drafts().get(userId='me',id=existing_draft_id,format='raw').execute();active()
            message=draft.get('message',{});own_message=message.get('id')
            if (not gmail_id(existing_draft_id)or draft.get('id')!=existing_draft_id or not gmail_id(own_message)or
                not gmail_id(message.get('threadId'))or 'DRAFT'not in message.get('labelIds',[])or 'SENT'in message.get('labelIds',[])):
                raise ContractError('own_draft_binding_changed')
            encoded=message.get('raw')
            if not isinstance(encoded,str):raise ContractError('own_draft_binding_changed')
            raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
            if content(raw)!=expected:raise ContractError('own_draft_content_changed')
            persist(self.store.root,'draft_current_original','gmail-draft:'+existing_draft_id,draft,
                    {'identity':identity,'candidate':candidate,'content_sha256':content_hash},'gmail.users.drafts.get.raw')
        if any(mid!=own_message for mid in mail['history_ids']):raise PermanentFailure('gmail_family_history_collision')
        facts=self._facts(identity,plan,bundle,values,crm)
        smtp=self.smtp.reuse(facts['smtp'],identity,candidate,active)
        chosen={'crm_pool':pool_original,'address':crm[1],
                'account':next((d,mail['refs'][d['source_id']])for d in mail['documents']if d['kind']=='account'),
                'gmail_family_history':next((d,mail['refs'][d['source_id']])for d in mail['documents']if d['kind']=='gmail_search'),
                # Legacy gate name; source is explicit approval, not old Sent.
                'sent_template':template_originals[-1],'smtp':smtp,'protections':protection}
        if getattr(self,'native_history',None)is not None:chosen['native_attempt_history']=self.native_history
        chosen['ted']=persist(self.store.root,'ted','ted-current-template-facts',
            {'original_documents':facts['ted'],'same_worker_company_evidence':facts['company'],'evidence':plan['evidence']},
            {'identity':identity,'candidate':candidate,'content_sha256':content_hash},'ted.actual-award-chain-and-same-template-buyer')
        checks={}
        for key,(doc,ref)in chosen.items():
            gate,gate_ref=persist(self.store.root,'current_gate_original','readiness:'+key,
                {'source_document':json.loads(doc['content']),'source_original_ref':ref,'source_original_sha256':doc['sha256']},
                {'identity':identity,'candidate':candidate,'content_sha256':content_hash,'existing_draft_id':existing_draft_id},
                'current-native-source-check:'+key)
            checks[key]={'state':'verified','native_original_ref':gate_ref,'native_original_sha256':gate['sha256']}
        active()
        return {'checks':checks,'content_sha256':content_hash,'missing':[]}

    def __call__(self,identity,plan,*,existing_draft_id=None):
        available=self.capture_available(identity,plan,existing_draft_id=existing_draft_id)
        value={'contract':'outreach-native-readiness-v1','identity':identity,'candidate':plan['candidate'],
               'content_sha256':available['content_sha256'],'existing_draft_id':existing_draft_id,
               'checks':available['checks'],'checked_at':datetime.now(timezone.utc).isoformat()}
        raw=canonical(value).encode();ref='outreach-readiness/'+digest(raw)+'.json'
        try:private_file(self.store.root/ref,raw)
        except FileExistsError:
            if approved_path(self.store.root,ref).read_bytes()!=raw:raise ContractError('native_outreach_live_receipt_changed')
        return {'identity':identity,'candidate':plan['candidate'],'receipt_ref':ref,'receipt_sha256':digest(raw)}

    def pre_reservation(self,identity,plan):return self(identity,plan)
