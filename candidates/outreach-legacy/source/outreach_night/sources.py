"""Actual existing Gmail SDK/REST read verbs; complete private source bundles.

Constructors accept already authenticated native handles. They never read keys,
request scopes, import Gmail into Brain/Vault, send, schedule or mutate CRM.
"""
import base64
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import getaddresses
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from background_jobs.contracts import ContractError, canonical, digest, private_file
from background_jobs.outreach_candidate import ACCOUNTS, gmail_id


def persist(root, kind, source_id, original, binding, operation):
    """Original SDK result + exact acquisition binding, never a summary."""
    packet={'contract':'outreach-sdk-original-v1','binding':binding,'source_id':source_id,
            'operation':operation,'original':original}
    raw=canonical(packet).encode();ref='native-originals/'+digest(raw)+'.json'
    try:private_file(Path(root)/ref,raw)
    except FileExistsError:
        if (Path(root)/ref).read_bytes()!=raw:raise ContractError('native_original_changed')
    return {'source_id':source_id,'kind':kind,'content':raw.decode(),'sha256':digest(raw)},ref


def decoded_mail(result, account):
    if (not isinstance(result,dict) or not gmail_id(result.get('id')) or not gmail_id(result.get('threadId')) or
        not isinstance(result.get('raw'),str) or not result['raw']):
        raise ContractError('gmail_complete_original_required')
    try:raw=base64.b64decode(result['raw']+'='*(-len(result['raw'])%4),altchars=b'-_',validate=True)
    except (ValueError,TypeError):raise ContractError('gmail_complete_original_required') from None
    msg=BytesParser(policy=email_policy.default).parsebytes(raw)
    if msg.defects:raise ContractError('gmail_complete_original_required')
    headers=[(key,str(value)) for key,value in msg.items()]
    for name in ('From','To','Cc','Bcc','Sender','Reply-To','Message-ID','Subject'):
        if len(msg.get_all(name,[]))>1:raise ContractError('gmail_authority_ambiguous')
    bodies=[];attachments=[]
    for part in msg.walk():
        if part.defects:raise ContractError('gmail_complete_original_required')
        if part.is_multipart():continue
        data=part.get_payload(decode=True)
        # email may append transfer-encoding defects only DURING decoding.
        if part.defects or data is None:raise ContractError('gmail_complete_original_required')
        record={'mime_type':part.get_content_type(),'filename':part.get_filename(),
                'bytes_base64':base64.b64encode(data).decode(),'sha256':digest(data)}
        if part.get_content_type().startswith('text/'):
            try:record['text']=data.decode(part.get_content_charset() or 'utf-8',errors='strict')
            except (UnicodeError,LookupError):raise ContractError('gmail_complete_original_required') from None
        (attachments if part.get_filename() or part.get_content_disposition()=='attachment' else bodies).append(record)
    return {'account':account,'sdk_result':result,'raw_sha256':digest(raw),'headers':headers,
            'body_parts':bodies,'attachments':attachments}


class GmailSources:
    def __init__(self,gmail,category,root):
        if category not in ACCOUNTS:raise ContractError('project_account_mismatch')
        self.gmail,self.category,self.root=gmail,category,Path(root)
        self.account=ACCOUNTS[category][1]

    def profile(self):
        result=self.gmail.users().getProfile(userId='me').execute()
        if result.get('emailAddress')!=self.account:raise ContractError('outreach_account_changed')
        return result

    def list_ids(self,domains):
        return self.list_many([{'item_id':'single','family_domains':domains}])['single']

    def read_many(self,ids,*,active=lambda:None):
        """Actual native SDK batch: independent complete originals, no Main loop."""
        if len(ids)!=len(set(ids)) or any(not gmail_id(mid) for mid in ids):raise ContractError('gmail_ids_invalid')
        results={};errors=[]
        for offset in range(0,len(ids),50):
            batch=self.gmail.new_batch_http_request()
            def callback(key,result,error):
                if error is not None:errors.append(key)
                else:results[key]=result
            for mid in ids[offset:offset+50]:
                batch.add(self.gmail.users().messages().get(userId='me',id=mid,format='raw'),request_id=mid,callback=callback)
            active();batch.execute();active()
        if errors or set(results)!=set(ids):raise ContractError('gmail_complete_read_unknown')
        if any(result.get('id')!=mid for mid,result in results.items()):raise ContractError('gmail_original_identity_changed')
        return {mid:decoded_mail(result,self.account) for mid,result in results.items()}

    def list_many(self,candidates,*,contexts=None,active=lambda:None):
        """Independent family searches share real SDK batches, including cursors."""
        if contexts is not None:
            # Separate exact mailbox/name/Draft/Scheduled queries are retained,
            # not inferred from a clean organizational-domain search alone.
            expanded=[];keys={}
            for c in candidates:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',c.get('to','')):
                    raise ContractError('exact_recipient_search_scope_required')
                ctx=contexts.get(c['item_id'])
                if not isinstance(ctx,dict)or not isinstance(ctx.get('company_names'),list)or not ctx['company_names']:
                    raise ContractError('complete_gmail_company_scope_required')
                queries=[None,'in:anywhere {'+' '.join(f'{f}:{c["to"]}'for f in ('from','to','cc','bcc'))+'}',
                         'in:drafts to:'+c['to'],'in:scheduled to:'+c['to']]
                for name in ctx['company_names']:
                    if not isinstance(name,str)or not name.strip()or any(t in name for t in ('"','\r','\n','\\')):
                        raise ContractError('complete_gmail_company_scope_required')
                    queries.append('in:anywhere "'+name+'"')
                keys[c['item_id']]=[]
                for n,q in enumerate(queries):
                    key=c['item_id']+':query:'+str(n);keys[c['item_id']].append(key)
                    expanded.append({**c,'item_id':key,'_query':q})
            output=self.list_many(expanded,active=active)
            return {key:{'queries':[output[k]for k in parts],
                'message_ids':sorted({mid for k in parts for mid in output[k]['message_ids']}),
                'complete_scope':{'mailbox':True,'family_domains':True,'company_names':True,'drafts':True,'scheduled':True}}
                for key,parts in keys.items()}
        state={}
        for c in candidates:
            domains=c['family_domains']
            if (not isinstance(domains,list) or not domains or
                any(type(d) is not str or not d or not all(ch.isascii() and (ch.isalnum() or ch in '.-') for ch in d) for d in domains)):
                raise ContractError('exact_family_scope_required')
            terms=' '.join(f'{field}:{domain}' for domain in sorted(set(domains)) for field in ('from','to','cc','bcc'))
            if c['item_id'] in state:raise ContractError('duplicate_pool_item')
            state[c['item_id']]={'query':c.get('_query')or 'in:anywhere {'+terms+'}','message_ids':[],'pages':[],
                                 'cursor':None,'tokens':set(),'done':False}
        while any(not row['done'] for row in state.values()):
            pending=[key for key,row in state.items() if not row['done']];results={};errors=[]
            for offset in range(0,len(pending),50):
                batch=self.gmail.new_batch_http_request()
                def callback(key,result,error):
                    if error is not None:errors.append(key)
                    else:results[key]=result
                for key in pending[offset:offset+50]:
                    row=state[key]
                    batch.add(self.gmail.users().messages().list(userId='me',q=row['query'],includeSpamTrash=True,
                        maxResults=500,pageToken=row['cursor']),request_id=key,callback=callback)
                active();batch.execute();active()
            if errors or set(results)!=set(pending):raise ContractError('gmail_pagination_unknown')
            for key in pending:
                row=state[key];page=results[key]
                if (not isinstance(page,dict) or ('messages' not in page and
                    (type(page.get('resultSizeEstimate')) is not int or page['resultSizeEstimate']!=0))):
                    raise ContractError('gmail_pagination_unknown')
                row['pages'].append({'request_page_token':row['cursor'],'response':page})
                messages=page.get('messages',[])
                if not isinstance(messages,list):raise ContractError('gmail_pagination_unknown')
                for message in messages:
                    mid=message.get('id') if isinstance(message,dict) else None
                    if not gmail_id(mid) or mid in row['message_ids']:raise ContractError('gmail_pagination_unknown')
                    row['message_ids'].append(mid)
                cursor=page.get('nextPageToken')
                if cursor is None:row['done']=True;continue
                if type(cursor) is not str or not cursor or cursor in row['tokens'] or len(row['tokens'])>=1000:
                    raise ContractError('gmail_pagination_unknown')
                row['tokens'].add(cursor);row['cursor']=cursor
        return {key:{name:row[name] for name in ('query','message_ids','pages')} for key,row in state.items()}

    def capture_many(self,candidates,templates,*,contexts=None,active=lambda:None):
        """Templates are explicit reviewed ID/SHA allowlists, not regex selection.

        A Stealth catalog must contain only known correct Stealth Sent messages;
        its review is a required private scope input, never inferred from keywords.
        """
        active();profile=self.profile();active()
        if not isinstance(templates,dict)or not templates:raise ContractError('known_sent_positive_control_required')
        by_item=self.list_many(candidates,contexts=contexts,active=active);all_ids=set(templates)
        for candidate in candidates:
            all_ids.update(by_item[candidate['item_id']]['message_ids'])
        active();originals=self.read_many(sorted(all_ids),active=active);active()
        for mid,expected in templates.items():
            source=originals[mid];headers=dict((k.casefold(),v) for k,v in source['headers'])
            sender=[a.casefold() for _,a in getaddresses([headers.get('from','')])]
            if (set(expected)!={'category','account','raw_sha256'} or expected['category']!=self.category or
                expected['account']!=self.account or expected['raw_sha256']!=source['raw_sha256'] or
                'SENT' not in source['sdk_result'].get('labelIds',[]) or sender!=[self.account]):
                raise ContractError('reviewed_correct_sent_template_required')
        bundles={}
        for candidate in candidates:
            binding={'candidate':candidate,'category':self.category,'account':self.account,'source_version':1}
            documents=[];refs={}
            for mid in sorted(set(by_item[candidate['item_id']]['message_ids'])|set(templates)):
                kind='sent_template' if mid in templates else 'gmail_history'
                doc,ref=persist(self.root,kind,'gmail:'+mid,originals[mid],binding,'gmail.users.messages.get.raw')
                documents.append(doc);refs[doc['source_id']]=ref
            doc,ref=persist(self.root,'account','gmail-profile',profile,binding,'gmail.users.getProfile')
            documents.append(doc);refs[doc['source_id']]=ref
            doc,ref=persist(self.root,'gmail_search','gmail-family-search',by_item[candidate['item_id']],binding,
                            'gmail.users.messages.list.complete')
            documents.append(doc);refs[doc['source_id']]=ref
            bundles[candidate['item_id']]={'documents':documents,'refs':refs,
                'history_ids':by_item[candidate['item_id']]['message_ids']}
        return bundles


class TwentySources:
    """Same existing helper/client read route as Twenty call/reply consumers."""
    def __init__(self,existing_client,root):self.client,self.root=existing_client,Path(root)
    def capture_many(self,candidates):
        # Existing request_json opens each independent read separately; it has
        # no shared httplib transport. Four bounded I/O threads save source-read
        # latency; this does not add native child/model slots or a Main loop.
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix='outreach-source') as pool:
            originals=list(pool.map(self.capture,candidates))
        return {c['item_id']:v for c,v in zip(candidates,originals)}
    def capture(self,candidate):
        source_contact=candidate.get('person_id')=='source-contact:'+candidate.get('source_id','')
        try:
            uuid.UUID(candidate['company_id']);uuid.UUID(candidate['source_id'])
            if not source_contact:uuid.UUID(candidate['person_id'])
        except (ValueError,TypeError,KeyError):raise ContractError('existing_crm_ids_required') from None
        company=self.client.get('company',candidate['company_id'])
        if not company or company.get('id')!=candidate['company_id']:raise ContractError('existing_company_binding_missing')
        pages=company.get('notionQuellen')
        matches=[r for r in pages or []if r.get('page_id','').replace('-','')==candidate['source_id'].replace('-','')]
        if not isinstance(pages,list) or len(matches)!=1:
            raise ContractError('existing_source_binding_missing')
        binding={'candidate':candidate,'category':'acribic','account':ACCOUNTS['acribic'][1],'source_version':1}
        if source_contact:
            source=matches[0];values={**source.get('properties',{}),**source.get('outreach',{})}
            if str(values.get('E-Mail')or '').strip().casefold()!=candidate['to']:
                raise ContractError('existing_mailbox_binding_missing')
            if (company.get('kontaktiert')is not False or company.get('outreachHistorie')or
                values.get('Stage')!='Nicht kontaktiert'or values.get('Gesprochen')in (True,'True')):
                raise ContractError('existing_source_contact_not_open')
            # All native people are checked in the complete pool gate. This
            # branch never sends the discriminator to any native CRUD route.
            contact={'contract':'twenty-preserved-source-contact-v1','company_id':company['id'],
                     'source_id':candidate['source_id'],'source_contact_id':candidate['person_id'],'source':source}
            return [persist(self.root,'crm','twenty-company:'+candidate['company_id'],company,binding,'twenty.rest.companies.get'),
                    persist(self.root,'crm','twenty-source-contact:'+candidate['source_id'],contact,binding,'twenty.existing-company.preserved-source-contact')]
        # The actual live ExistingHelpersClient supports company/note/target.
        # Use its exact already-authenticated helper read route for people,
        # without extending that shared class or introducing a second key.
        result=self.client.helper.request_json('GET',self.client.helper.TWENTY_URL+'/rest/people/'+candidate['person_id'],self.client._key)
        person=result.get('data',{}).get('person')
        if not person or person.get('id')!=candidate['person_id']:raise ContractError('existing_person_binding_missing')
        company_id=person.get('companyId') or (person.get('company') or {}).get('id')
        emails=person.get('emails') or {}
        values=[emails.get('primaryEmail'),*(emails.get('additionalEmails') or [])]
        if company_id!=candidate['company_id'] or candidate['to'] not in values:raise ContractError('existing_mailbox_binding_missing')
        return [persist(self.root,'crm','twenty-company:'+candidate['company_id'],company,binding,'twenty.rest.companies.get'),
                persist(self.root,'crm','twenty-person:'+candidate['person_id'],person,binding,'twenty.rest.people.get')]


def publish_bundle(store,identity,candidate,documents):
    raw=canonical({'contract':'outreach-originals-v1','candidate':candidate,'documents':documents}).encode()
    if digest(raw)!=identity['item']['source_hash']:raise ContractError('outreach_acquisition_hash_changed')
    target=store.snapshot_path(identity['job_id'],identity['item']['item_id'])
    try:private_file(target,raw)
    except FileExistsError:
        if target.read_bytes()!=raw:raise ContractError('outreach_acquisition_hash_changed')
    return target
