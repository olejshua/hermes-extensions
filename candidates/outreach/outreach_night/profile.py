"""Explicit staging scope/profile. BG's global <=500 rule is not widened."""
import re
from background_jobs.contracts import ContractError, digest, canonical, private_file, token
from background_jobs.outreach_candidate import ACCOUNTS, gmail_id

PROFILE='outreach-drafts-staging-v1'


def scope(category,owner,session_id,request_id,target,candidates,reviewed_templates):
    if category not in ACCOUNTS or type(target) is not int or not 1<=target<=500:
        raise ContractError('explicit_outreach_target_required')
    if not isinstance(candidates,list) or not target<len(candidates)<=10000:
        raise ContractError('explicit_replacement_pool_required')
    required={'item_id','company_id','person_id','source_id','family_id','family_domains','to'}
    if any(not isinstance(v,dict) or set(v)!=required for v in candidates):raise ContractError('permitted_candidate_binding_required')
    for candidate in candidates:
        for field in required-{'family_domains','to'}:token(candidate[field],256)
        domains=candidate['family_domains']
        if (not isinstance(domains,list) or not domains or
            any(type(d) is not str or not re.fullmatch(r'[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*\.[A-Za-z]{2,}',d) for d in domains) or
            len(domains)!=len(set(domains))):
            raise ContractError('exact_family_scope_required')
        if type(candidate['to']) is not str or not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+',candidate['to']):raise ContractError('exact_recipient_required')
    if len({v['item_id'] for v in candidates})!=len(candidates):raise ContractError('duplicate_pool_item')
    if not isinstance(reviewed_templates,dict) or not reviewed_templates:raise ContractError('reviewed_correct_sent_template_required')
    for mid,record in reviewed_templates.items():
        if (not gmail_id(mid) or not isinstance(record,dict) or set(record)!={'category','account','raw_sha256'} or
            record['category']!=category or record['account']!=ACCOUNTS[category][1] or
            type(record['raw_sha256']) is not str or not re.fullmatch(r'[a-f0-9]{64}',record['raw_sha256'])):
            raise ContractError('reviewed_correct_sent_template_required')
    for value in (owner,session_id,request_id):token(value)
    # This is a domain scope/admission proposal, not a production BG submission.
    # Every future BG page stays <=500; cross-page K/N admission needs its own
    # explicitly reviewed parent pool/aggregate contract, not a global cap change.
    value={'contract':'outreach-night-scope-staging-v1','profile':PROFILE,'category':category,
           'account':ACCOUNTS[category][1],'owner':owner,'session_id':session_id,'request_id':request_id,
           'target':target,'permitted_candidates':candidates,'reviewed_templates':reviewed_templates,
           'pages':[{'offset':offset,'end':min(offset+500,len(candidates))} for offset in range(0,len(candidates),500)],
           'native_child_slots':2,'activated':False,'send_enabled':False,'schedule_enabled':False,
           'cross_page_native_admission_available':False}
    value['source_authority']=({'server':'notion_2','datasource_id':'00000000-0000-4000-8000-00000000000e'} if category=='stealth' else {'crm':'twenty','project':'acribic'})
    return value


def admit_local(root,value):
    ref='scope-'+digest(value)+'.json';private_file(root/ref,canonical(value).encode())
    return {'accepted_local':True,'scope_ref':ref,'scope_hash':digest(value),'target':value['target'],
            'pool_size':len(value['permitted_candidates']),'activated':False,'worker_started':False,
            'missing': ['native_profile_registration','fresh_project_gate_bindings']+
                (['cross_page_native_admission'] if len(value['pages'])>1 else [])}
