"""Complete native Twenty acquisition for fixed, named Acribic source lists.

Read contracts and protections follow the existing twenty_backend.py and
versand.py. This module has no writer, model, job, scheduler, or activation.
Every baseline/delta response and policy original is retained before projection.
"""
from collections import Counter
from datetime import datetime, timezone
import csv
import io
import json
from pathlib import Path
import re
import uuid

from background_jobs.contracts import ContractError, canonical, digest, private_file, approved_path
from background_jobs.outreach_candidate import ACCOUNTS
from .sources import persist

LISTS=('it','ingenieur','medizin','beratung','sicherheit','reinigung','icp')
CANDIDATE_LISTS=LISTS[:4]
POLICY_ID=str(uuid.uuid5(uuid.UUID('00000000-0000-4000-8000-000000000008'),'outreach-policy:acribic-twenty-v1'))
FIELDS={
 'company':'id updatedAt name domainName { primaryLinkUrl primaryLinkLabel } address { addressCity } kontaktiert kontaktiertAm kennungVergabedaten kontaktQuelle notionQuellen notionSeitenIds notionListen notionStages notionEmails notionVersandbereit outreachHistorie',
 'review':'id updatedAt name notionPageId notionListe notionRohdaten notionStage notionEmail notionLastEdited kandidaten versandbereit outreachHistorie importStatus',
 'person':'id updatedAt name { firstName lastName } emails { primaryEmail additionalEmails } companyId mailErlaubt'}
OBJECTS={'company':('companies','Company'),'review':('notionImportCases','NotionImportCase'),'person':('people','Person')}
FREEMAIL={'gmail.com','googlemail.com','web.de','gmx.de','gmx.net','t-online.de','outlook.com','hotmail.com','yahoo.com','yahoo.de','freenet.de','aol.com','online.de','icloud.com'}
TECH=re.compile(r'^(karriere|career|careers|jobs?|bewerbung\w*|recruiting|hr|personal|postmaster|hostmaster|webmaster|abuse|noreply|no-reply|donotreply|mailer-daemon|support|helpdesk|hotline|servicedesk|datenschutz|privacy|dsgvo|rechnung\w*|invoice\w*|buchhaltung|accounting|billing|newsletter|presse|press|marketing|social|events?)([.\-_]|$)',re.I)
FAMILY=re.compile(r'\b(nexus|allgeier|dedalus|mesalvo|conet|rohde\s*&\s*schwarz|allegion|interflex|primeline|asknet|cerner|oracle)\b',re.I)
STOP=re.compile(r'catch-all|policy|(?<!zufallsadresse )abgelehnt|bounce|existiert nicht|gesperrt|keine\s+(?:weitere\s+)?(?:e-?mail|nachricht)\s+(?:senden|schicken)|nicht\s+(?:erneut\s+|mehr\s+)?(?:anschreiben|kontaktieren)|opt[ -]?out',re.I)
MAIL=re.compile(r'[^@\s<>;,]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}',re.I)


def domain(value):
    value=str(value or '').strip().casefold()
    if '@'in value:return value.rsplit('@',1)[1].strip(' >;,')
    value=re.sub(r'^https?://','',value).split('/')[0]
    return value.removeprefix('www.')


def family(value):
    parts=domain(value).split('.')
    return '.'.join(parts[-3:])if len(parts)>2 and parts[-2]in ('co','com')else '.'.join(parts[-2:])


def company_key(value):
    value=str(value or '').casefold()
    for a,b in (('ä','ae'),('ö','oe'),('ü','ue'),('ß','ss')):value=value.replace(a,b)
    forms={'gmbh','mbh','ag','se','kg','co','ohg','ug','ev','e','v','inc','ltd','llc','deutschland','germany'}
    return ' '.join(w for w in re.findall(r'[a-z0-9]+',value)if w not in forms)


def _uuid(value):
    try:
        if str(uuid.UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise ContractError('twenty_original_identity_invalid')from None
    return value


def _resolved(review,companies):
    status=review.get('importStatus')or ''
    if not status.startswith('GELOEST:'):return False
    target=_uuid(status.removeprefix('GELOEST:'));company=companies.get(target)
    if not company or review.get('versandbereit')is not False:raise ContractError('twenty_resolved_review_invalid')
    original=review['notionRohdaten']
    matches=[s for s in company.get('notionQuellen')or []if s['page_id'].replace('-','').lower()==original['page_id'].replace('-','').lower()]
    if len(matches)!=1 or {k:v for k,v in matches[0].items()if k!='outreach'}!={k:v for k,v in original.items()if k!='outreach'}:
        raise ContractError('twenty_resolved_original_not_preserved')
    if any(event not in (company.get('outreachHistorie')or [])for event in review.get('outreachHistorie')or []):
        raise ContractError('twenty_resolved_history_not_preserved')
    return True


class TwentyPoolSource:
    def __init__(self,client,root,*,protection_source=None):
        self.client,self.root=client,Path(root);self.protection_source=protection_source

    def _central(self,binding,active):
        if self.protection_source is None:return None
        doc,ref=self.protection_source.capture(binding,active)
        value=json.loads(doc['content'])['original']
        if value.get('contract')!='outreach-central-sources-v1':raise ContractError('canonical_source_consumer_required')
        binding['central_source_version']={k:value[k]for k in ('project','account','revision','entry_sha256')}
        binding['central_source_original']={'ref':ref,'sha256':doc['sha256']}
        return doc

    def _pages(self,kind,since,active,binding):
        plural,title=OBJECTS[kind]
        query=f'query($first:Int!,$after:String,$filter:{title}FilterInput) {{ {plural}(first:$first,after:$after,filter:$filter) {{ totalCount pageInfo {{ endCursor hasNextPage }} edges {{ node {{ {FIELDS[kind]} }} }} }} }}'
        cursor=None;seen=set();ids=set();rows=[];pages=[];expected=None
        while True:
            variables={'first':1000,'after':cursor}
            if since:variables['filter']={'updatedAt':{'gte':since}}
            active();result=self.client.query(query,variables);active()
            doc,ref=persist(self.root,'crm_pool_page',f'twenty:{kind}:{since or "baseline"}:{cursor or "first"}',
                {'query':query,'variables':variables,'response':result},binding,'twenty.graphql.complete-page')
            pages.append({'ref':ref,'sha256':doc['sha256']})
            data=result.get(plural)if isinstance(result,dict)else None
            if not isinstance(data,dict)or type(data.get('totalCount'))is not int or data['totalCount']<0:
                raise ContractError('twenty_complete_pagination_required')
            if expected is None:expected=data['totalCount']
            info=data.get('pageInfo');edges=data.get('edges')
            if data['totalCount']!=expected or not isinstance(info,dict)or type(info.get('hasNextPage'))is not bool or not isinstance(edges,list):
                raise ContractError('twenty_complete_pagination_required')
            for edge in edges:
                row=edge.get('node')if isinstance(edge,dict)else None
                if not isinstance(row,dict):raise ContractError('twenty_complete_original_required')
                rid=_uuid(row.get('id'))
                if rid in ids:raise ContractError('twenty_repeated_original')
                ids.add(rid);rows.append(row)
            if len(rows)>expected:raise ContractError('twenty_count_changed')
            if not info['hasNextPage']:break
            cursor=info.get('endCursor')
            if not isinstance(cursor,str)or not cursor or cursor in seen or not edges or len(seen)>=10000:
                raise ContractError('twenty_complete_pagination_required')
            seen.add(cursor)
        if len(rows)!=expected:raise ContractError('twenty_complete_pagination_required')
        return {r['id']:r for r in rows},pages

    def capture(self,name,active):
        if name not in {'acribic:'+v for v in CANDIDATE_LISTS}:raise ContractError('native_named_pool_not_allowed')
        if not callable(active):raise ContractError('native_current_source_gate_required')
        anchor=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        binding={'pool_name':name,'category':'acribic','account':ACCOUNTS['acribic'][1],'read_started_at':anchor}
        records={};pages={}
        for kind in OBJECTS:records[kind],pages[kind]=self._pages(kind,None,active,binding)
        # Same native ordering as TwentyBackend: resolution review first, then
        # its company evidence. The floor-second anchor includes baseline edits.
        for kind in ('review','company','person'):
            delta,refs=self._pages(kind,anchor,active,binding);records[kind].update(delta);pages[kind]+=refs
        active();note=self.client.get('note',POLICY_ID);active()
        text=(note.get('bodyV2')or {}).get('markdown')if isinstance(note,dict)else None
        if not isinstance(note,dict)or note.get('id')!=POLICY_ID or not isinstance(text,str)or not text.startswith('ACRIBIC_TWENTY_POLICY_V1\n'):
            raise ContractError('twenty_outreach_policy_required')
        policy=json.loads(text.split('\n',1)[1])
        if (policy.get('version')!=1 or set(policy.get('list_counts',{}))!=set(LISTS)or
            any(type(v)is not int or v<0 for v in policy['list_counts'].values())or
            not isinstance(policy.get('suppression_text'),str)or not isinstance(policy.get('sent_history_csv'),str)or
            not isinstance(policy.get('attempted_mailboxes'),list)or any(not isinstance(v,str)for v in policy['attempted_mailboxes'])):
            raise ContractError('twenty_seven_list_protection_required')
        protection=self._central(binding,active)
        doc,ref=persist(self.root,'crm_pool','twenty-complete-pool',{'records':records,'policy_note':note,'page_originals':pages},binding,'twenty.full-baseline-and-delta')
        candidates,excluded,counts=project(records,policy,name.split(':',1)[1],protection=protection)
        manifest={'contract':'outreach-named-pool-sources-v1',**binding,'native_original_ref':ref,'native_original_sha256':doc['sha256'],
            'source_counts':counts,'objects':{k:len(v)for k,v in records.items()},'excluded':excluded,'complete':True}
        result={'contract':'hermes-native-named-outreach-pool-v1','pool_name':name,'project':'acribic','account':ACCOUNTS['acribic'][1],
            'candidates':candidates,'pool_sha256':digest(candidates),'source_manifest':manifest,'source_manifest_sha256':digest(manifest)}
        raw=canonical(result).encode();target=self.root/'native-pools'/(digest(raw)+'.json')
        try:private_file(target,raw)
        except FileExistsError:
            if target.read_bytes()!=raw:raise ContractError('native_pool_original_changed')
        active()
        return result

    def refresh_from(self,source_manifest,name,active,*,own_identity=None):
        """Existing baseline + all object deltas before each final check.

        Complete raw deltas and note are embedded in the new receipt. The full
        baseline stays immutable and hash-bound, rather than fetching 47k firms
        again and again for every mail. Previous protections remain conservative.
        """
        if name not in {'acribic:'+v for v in CANDIDATE_LISTS}:raise ContractError('native_named_pool_not_allowed')
        raw=approved_path(self.root,source_manifest['native_original_ref']).read_bytes()
        if digest(raw)!=source_manifest['native_original_sha256']:raise ContractError('native_full_pool_original_changed')
        packet=json.loads(raw)
        if packet.get('binding',{}).get('pool_name')!=name:raise ContractError('native_named_pool_scope_changed')
        records=packet['original']['records'];anchor=source_manifest['read_started_at'];pages={}
        old_policy=json.loads(packet['original']['policy_note']['bodyV2']['markdown'].split('\n',1)[1])
        binding={'pool_name':name,'category':'acribic','account':ACCOUNTS['acribic'][1],
                 'baseline_ref':source_manifest['native_original_ref'],'baseline_sha256':digest(raw)}
        if own_identity is not None:binding['identity']=own_identity
        protection=self._central(binding,active)
        baseline_candidates=project(records,old_policy,name.split(':',1)[1],protection=protection)[0]
        for kind in ('review','company','person'):
            delta,refs=self._pages(kind,anchor,active,binding)
            # A later edit cannot erase an already contacted/suppressed row.
            for rid,row in delta.items():
                old=records[kind].get(rid,{})
                if old.get('outreachHistorie')and not row.get('outreachHistorie'):
                    row={**row,'outreachHistorie':old['outreachHistorie']}
                if old.get('kontaktiert')is True:row={**row,'kontaktiert':True}
                if old.get('mailErlaubt')is False:row={**row,'mailErlaubt':False}
                records[kind][rid]=row
            pages[kind]=[json.loads(approved_path(self.root,r['ref']).read_bytes())for r in refs]
        active();note=self.client.get('note',POLICY_ID);active()
        text=(note.get('bodyV2')or {}).get('markdown')if isinstance(note,dict)else None
        if not isinstance(note,dict)or note.get('id')!=POLICY_ID or not isinstance(text,str)or not text.startswith('ACRIBIC_TWENTY_POLICY_V1\n'):
            raise ContractError('twenty_outreach_policy_required')
        try:policy=json.loads(text.split('\n',1)[1])
        except ValueError:raise ContractError('twenty_outreach_policy_required')from None
        if (policy.get('version')!=1 or set(policy.get('list_counts',{}))!=set(LISTS)or
            any(type(v)is not int or v<0 for v in policy['list_counts'].values())or
            not isinstance(policy.get('suppression_text'),str)or not isinstance(policy.get('sent_history_csv'),str)or
            not isinstance(policy.get('attempted_mailboxes'),list)):
            raise ContractError('twenty_seven_list_protection_required')
        # Preserve policy suppressions observed during the original acquisition.
        old=json.loads(packet['original']['policy_note']['bodyV2']['markdown'].split('\n',1)[1])
        for key in ('suppression_text','sent_history_csv'):policy[key]+='\n'+old[key]
        policy['attempted_mailboxes']=list(set(policy['attempted_mailboxes'])|set(old['attempted_mailboxes']))
        candidates,excluded,counts=project(records,policy,name.split(':',1)[1],protection=protection)
        candidates=[c for c in candidates if c in baseline_candidates]
        doc,ref=persist(self.root,'crm_pool','twenty-current-complete-pool',
            {'baseline':binding,'delta_originals':pages,'policy_note':note,'source_counts':counts,
             'candidates':candidates,'excluded':excluded},binding,'twenty.full-baseline-and-current-deltas')
        return candidates,(doc,ref)


def project(records,policy,selected,*,protection=None):
    """Conservative native exclusions. The quality worker still judges each lead."""
    if selected not in CANDIDATE_LISTS:raise ContractError('native_named_pool_not_allowed')
    companies=records['company'];resolved={rid for rid,r in records['review'].items()if _resolved(r,companies)}
    holds={c['id']for rid,r in records['review'].items()if rid not in resolved for c in r.get('kandidaten')or []}
    rows=[];seen=set();counts=Counter()
    for kind in ('company','review'):
        for target in records[kind].values():
            if kind=='review'and target['id']in resolved:continue
            sources=(target.get('notionQuellen')or [])if kind=='company'else [target['notionRohdaten']]
            for source in sources:
                sid=str(uuid.UUID(source['page_id']));segment=source.get('liste')
                if sid in seen or segment not in LISTS or not isinstance(source.get('properties'),dict):
                    raise ContractError('twenty_duplicate_or_unknown_source')
                seen.add(sid);counts[segment]+=1
                values={**source['properties'],**source.get('outreach',{})}
                blocked=(kind!='company'or target.get('notionVersandbereit')is not True or
                    target.get('kontaktiert')is not False or bool(target.get('kontaktiertAm'))or bool(target.get('outreachHistorie'))or target['id']in holds or
                    values.get('Stage')!='Nicht kontaktiert'or values.get('Gesprochen')in (True,'True')or bool(STOP.search(str(values.get('Notizen')or '').split(' | ')[0])))
                rows.append({'source_id':sid,'segment':segment,'kind':kind,'target':target,'values':values,'blocked':blocked})
    if any(counts[k]<policy['list_counts'][k]for k in LISTS):raise ContractError('twenty_seven_list_coverage_incomplete')
    # The Twenty policy note remains a complete immutable original. After the
    # source cutover its copied MD/CSV/attempt fields are historical exports;
    # they cannot override an explicit newer canonical source release.
    if protection is not None:
        policy={**policy,'suppression_text':'','sent_history_csv':'','attempted_mailboxes':[]}
    text=policy['suppression_text'].casefold()
    freed=re.search(r'^## 8\. freigegeben.*?(?=^## |\Z)',text,re.S|re.M)
    free=set(re.findall(r'`([a-z0-9.\-]+\.[a-z]{2,})`',freed.group()if freed else ''))
    if freed:text=text[:freed.start()]+text[freed.end():]
    blocked_domains={domain(v)for v in re.findall(r'(?:[a-z0-9-]+\.)+[a-z]{2,}',text)}-free
    for fields in csv.reader(io.StringIO(policy['sent_history_csv']),delimiter=';'):
        if fields and '@'in fields[0]:
            pure_review=len(fields)>1 and fields[1]=='Adresse pruefen'and 'bounce'not in ';'.join(fields).casefold()
            if not (pure_review and domain(fields[0])in free):blocked_domains.add(domain(fields[0]))
    blocked_domains|={domain(v)for v in policy['attempted_mailboxes']}
    blocked_names=set();mail_counts=Counter()
    for row in rows:
        values=row['values'];mail=str(values.get('E-Mail')or '').strip().casefold()
        if row['segment']==selected:mail_counts[mail]+=1
        if row['blocked']or row['segment']!=selected:
            blocked_domains|={domain(values.get(k))for k in ('E-Mail','Website','Domain')}
            blocked_names.add(company_key(values.get('Firma')))
    blocked_companies=set()
    for cid,company in companies.items():
        if (not company.get('notionQuellen')or company.get('kontaktiert')is not False or company.get('kontaktiertAm')or
            company.get('outreachHistorie')or company.get('notionVersandbereit')is not True or cid in holds):
            blocked_companies.add(cid)
            blocked_domains.add(domain((company.get('domainName')or {}).get('primaryLinkUrl')))
            blocked_names.add(company_key(company.get('name')))
    # Every native person contributes opt-out/unreviewed mailbox protection.
    people={}
    company_people={}
    for person in records['person'].values():
        company_people.setdefault(person.get('companyId'),[]).append(person)
        emails=person.get('emails')or {}
        for value in [emails.get('primaryEmail'),*(emails.get('additionalEmails')or [])]:
            if not value:continue
            key=(person.get('companyId'),str(value).strip().casefold());people.setdefault(key,[]).append(person)
            if person.get('mailErlaubt')is False:blocked_domains.add(domain(value))
            if person.get('companyId')not in companies or person.get('companyId')in blocked_companies:blocked_domains.add(domain(value))
    blocked_domains.discard('');blocked_names.discard('');bases={family(d)for d in blocked_domains}
    candidates=[];excluded=[];chosen_families=set();chosen_names=set()
    for row in sorted(rows,key=lambda r:(-int(r['values'].get('Zuschläge')or 0),r['source_id'])):
        if row['segment']!=selected:continue
        values=row['values'];mail=str(values.get('E-Mail')or '').strip().casefold();d=domain(mail)
        name=str(values.get('Firma')or row['target'].get('name')or '');key=company_key(name)
        domains=sorted({domain(values.get('Website')),d,domain((row['target'].get('domainName')or {}).get('primaryLinkUrl'))}-{''})
        persons=people.get((row['target']['id'],mail),[])
        reason=None
        if row['blocked']:reason='source_or_company_blocked'
        elif not MAIL.fullmatch(mail)or d in FREEMAIL or TECH.match(mail.split('@')[0])or re.search(r'tedremoved|emailaddress\.given|^x+@|dummy|beispiel@|example\.',mail,re.I):reason='mailbox_not_permitted'
        elif len(persons)>1:reason='no_unique_existing_person'
        elif not persons and company_people.get(row['target']['id']):reason='source_contact_native_person_conflict'
        elif mail_counts[mail]!=1:reason='duplicate_mailbox'
        elif any(family(v)in bases for v in domains)or key in blocked_names:reason='family_history_or_other_segment'
        elif FAMILY.search(name)and not (name.casefold().startswith('nexus institut für kooperationsmanagement')and family(d)=='nexusinstitut.de'):reason='known_company_family_hold'
        elif family(d)in chosen_families or key in chosen_names:reason='pool_family_duplicate'
        if reason:excluded.append({'source_id':row['source_id'],'reason':reason});continue
        # Existing imported source contacts are genuine CRM contacts too. Do not
        # fabricate a native person or pretend this discriminator is a UUID.
        person_id=persons[0]['id']if persons else 'source-contact:'+row['source_id']
        candidate={'item_id':'source-'+row['source_id'],'company_id':row['target']['id'],'person_id':person_id,
            'source_id':row['source_id'],'family_id':family(d),'family_domains':domains,'to':mail}
        if protection is not None:
            from .protections import permits
            if not permits(candidate,name,protection):
                excluded.append({'source_id':row['source_id'],'reason':'current_canonical_protection'});continue
        candidates.append(candidate);chosen_families.add(family(d));chosen_names.add(key)
    return candidates,excluded,{k:counts[k]for k in LISTS}


def related_originals(records,candidate):
    """Unshortened relevant family/company/contact/review originals."""
    bases={family(v)for v in candidate['family_domains']}
    selected=records['company'][candidate['company_id']]
    name=company_key(selected.get('name'))
    def matches(value):
        if not isinstance(value,dict):return False
        values=[value.get(k)for k in ('E-Mail','Website','Domain')]
        return (any(family(v)in bases for v in values if v)or
                bool(name and company_key(value.get('Firma')or value.get('name'))==name))
    companies={}
    for cid,company in records['company'].items():
        relevant=(cid==candidate['company_id']or matches(company)or
                  family((company.get('domainName')or {}).get('primaryLinkUrl'))in bases or
                  any(matches({**s.get('properties',{}),**s.get('outreach',{})})for s in company.get('notionQuellen')or []))
        if relevant:companies[cid]=company
    people={pid:p for pid,p in records['person'].items()if p.get('companyId')in companies or
        any(family(v)in bases for v in [(p.get('emails')or {}).get('primaryEmail'),*((p.get('emails')or {}).get('additionalEmails')or [])]if v)}
    reviews={rid:r for rid,r in records['review'].items()if
        any(c.get('id')in companies for c in r.get('kandidaten')or [])or matches((r.get('notionRohdaten')or {}).get('properties'))}
    return {'company':companies,'person':people,'review':reviews}


def named_pool_resolver(policy,root,check_current):
    """Host-wired current native Main source gate; no model-supplied path inputs."""
    def resolve_named_pool(name):
        if name not in {'acribic:'+v for v in CANDIDATE_LISTS}:raise ContractError('native_named_pool_not_allowed')
        if not callable(check_current):raise ContractError('native_current_source_gate_required')
        check_current()
        from .clients import existing_twenty
        from .protections import ServerProtectionSource
        from background_jobs.store import Store
        source=ServerProtectionSource.from_existing(Store(root),policy)
        return TwentyPoolSource(existing_twenty(policy),root,protection_source=source).capture(name,check_current)
    return resolve_named_pool
