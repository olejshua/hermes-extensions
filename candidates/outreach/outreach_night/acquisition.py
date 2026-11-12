"""Background-only acquisition: intake keeps metadata, originals stay private."""
from pathlib import Path
import json
from background_jobs.contracts import ContractError,PermanentFailure,canonical,digest,private_file


class AcribicAcquisition:
    def __init__(self,gmail_sources,twenty_sources):
        self.gmail,self.twenty=gmail_sources,twenty_sources
        if gmail_sources.category!='acribic':raise ContractError('project_account_mismatch')
    @classmethod
    def from_existing(cls,policy,state_root):
        # Concrete native routes; disabled-profile checks precede existing auth.
        from .clients import existing_gmail,existing_twenty
        from .sources import GmailSources,TwentySources
        return cls(GmailSources(existing_gmail(policy,'acribic'),'acribic',state_root),
                   TwentySources(existing_twenty(policy),state_root))
    def capture_page(self,candidates,templates,source_root,owner,*,active=lambda:None,
                     common_documents=(),ted=None,smtp=None,company_sources=None,source_binding=None):
        if not isinstance(candidates,list) or not 1<=len(candidates)<=500:raise ContractError('bounded_source_page_required')
        active();crm_by_item=self.twenty.capture_many(candidates);active()
        contexts={}
        for c in candidates:
            company=json.loads(crm_by_item[c['item_id']][0][0]['content'])['original']
            sources=[s for s in company['notionQuellen']if str(s['page_id']).replace('-','')==c['source_id'].replace('-','')]
            if len(sources)!=1:raise ContractError('existing_source_binding_missing')
            values={**sources[0]['properties'],**sources[0].get('outreach',{})}
            contexts[c['item_id']]={'company_names':list(dict.fromkeys(v for v in (company.get('name'),values.get('Firma'))if v)),
                                     'properties':values}
        mail=self.gmail.capture_many(candidates,templates,contexts=contexts,active=active);items=[];all_missing=set()
        for candidate in candidates:
            active()
            crm=crm_by_item[candidate['item_id']]
            documents=[d for d,_ in crm]+mail[candidate['item_id']]['documents']+list(common_documents)
            from .templates import document
            documents.append(document())
            kinds={d['kind']for d in documents}
            missing=[]
            for kind,label in (('crm_pool','complete_live_crm_pool_original'),('protections','current_canonical_protections_and_preserved_legacy'),
                               ('template_authority','current_explicit_template_authority')):
                if kind not in kinds:missing.append(label)
            identity={'acquisition':source_binding or {'owner':owner},'item':{'item_id':candidate['item_id'],
                      'category':'acribic','account':self.gmail.account}}
            values=contexts[candidate['item_id']]['properties']
            factual_errors={}
            if mail[candidate['item_id']]['history_ids']:
                missing.append('gmail_family_history_collision')
            for label,reader,args in (
                ('current_official_ted_award_original',ted,(identity,candidate,values,active)),
                ('current_individual_smtp_recipient_original',smtp,(identity,candidate,active)),
                ('company_bidder_role_and_tender_pressure_original',company_sources,(identity,candidate,values,active))):
                if reader is None or mail[candidate['item_id']]['history_ids']:missing.append(label);continue
                try:
                    captured=reader.capture(*args)
                    if label=='current_official_ted_award_original':documents.extend(d for d,_ in captured[0])
                    elif label=='company_bidder_role_and_tender_pressure_original':documents.extend(d for d,_ in captured)
                    else:documents.append(captured[0])
                except PermanentFailure as exc:
                    # Unsuitable candidate, retained for same-worker judgement;
                    # transport/authority failures still stop acquisition.
                    missing.append(label);factual_errors[label]=str(exc)
            bundle={'contract':'outreach-originals-v1','candidate':candidate,'documents':documents,
                    'source_binding':{'account':self.gmail.account,'category':'acribic','owner':owner,'source_version':1,
                                      'acquisition':source_binding},
                    'missing_native_sources':missing,'candidate_factual_errors':factual_errors}
            if source_binding and source_binding.get('external_originals'):
                bundle['external_originals']=source_binding['external_originals']
            raw=canonical(bundle).encode();ref=digest(raw)+'.original'
            try:private_file(Path(source_root)/ref,raw)
            except FileExistsError:
                if (Path(source_root)/ref).read_bytes()!=raw:raise ContractError('outreach_original_changed')
            items.append({'item_id':candidate['item_id'],'source_ref':ref,'source_hash':digest(raw),'source_version':1,
                          'owner':owner,'account':self.gmail.account,'category':'acribic'})
            all_missing.update(missing)
        active()
        return {'manifest':{'contract':'background-sources-v1','items':items},'draft_inputs_ready':False,
                'originals_complete':not all_missing,'missing_native_sources':sorted(all_missing)}
