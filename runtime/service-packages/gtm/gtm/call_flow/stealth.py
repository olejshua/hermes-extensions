"""Stealth stays in its existing Notion contact records; no Twenty migration."""
import hashlib
import html
import json
import re

SOURCES=('00000000-0000-4000-8000-00000000000c','00000000-0000-4000-8000-00000000000d')
ROOT='e000000000000000000000000000000e'
READS={'notion-fetch','notion-query-data-sources'}
TOOLS=READS|{'notion-update-page','notion-get-async-task'}
PROTECTED={'Name','E-Mail','Verantwortlich','Outreach Alex','Outreach Alex Notiz'}


def tag(value,name):
    text=value.get('text','')
    match=re.search('<'+re.escape(name)+r'(?:\s[^>]*)?>(.*?)</'+re.escape(name)+'>',text,re.S)
    if not match:raise ValueError('stealth_notion_native_shape_changed')
    body=match.group(1).strip()
    while body.startswith('\\n'):body=body[2:]
    while body.endswith('\\n'):body=body[:-2]
    return body


def page_id(url):
    match=re.fullmatch(r'https://(?:app\.)?notion\.(?:com|so)/(?:p/)?([a-f0-9]{32})(?:\?[^\s]*)?',url or '')
    if not match:raise ValueError('stealth_existing_contact_url_required')
    return match[1]


def verify_page(value,source,email):
    ancestry=tag(value,'ancestor-path')
    if 'collection://'+source not in ancestry or ROOT not in ancestry.replace('-',''):
        raise ValueError('stealth_contact_outside_approved_workspace')
    properties=json.loads(tag(value,'properties'))
    if properties.get('E-Mail','').casefold()!=email.casefold() or properties.get('Hinweis')=='Dublette':
        raise ValueError('stealth_exact_existing_contact_required')
    return properties


class Reads:
    def __init__(self,client_factory):self.factory=client_factory
    def binding(self,emails):
        client=self.factory();matches=[]
        try:
            for source in SOURCES:
                ds='collection://'+source
                schema_value=client.call('notion-fetch',{'id':ds})
                schema=json.loads(tag(schema_value,'data-source-state'))['schema']
                placeholders=','.join('?'for _ in emails)
                value=client.call('notion-query-data-sources',{'data':{'mode':'sql','data_source_urls':[ds],
                    'query':'SELECT url, "E-Mail", "Hinweis" FROM ['+ds+'] WHERE lower("E-Mail") IN ('+placeholders+') LIMIT 10',
                    'params':[e.casefold()for e in emails]}})
                if value.get('has_more') or not isinstance(value.get('results'),list) or len(value['results'])>=10:
                    raise ValueError('stealth_contact_lookup_incomplete')
                for row in value['results']:
                    email=row.get('E-Mail','').casefold()
                    if email not in emails or row.get('Hinweis')=='Dublette':continue
                    current=client.call('notion-fetch',{'id':row['url']})
                    properties=verify_page(current,source,email)
                    matches.append(dict(page_id=page_id(current['url']),url=current['url'],source_id=source,email=email,
                                        properties=properties,schema=schema,title=current.get('title')))
        finally:client.close()
        # Never pick one of two legitimate contacts or a cross-list duplicate by name/domain.
        unique={m['page_id']:m for m in matches}
        if len(unique)!=1:return dict(stealth_crm_verified=False,stealth_crm_review='no_unique_existing_contact')
        return dict(stealth_crm_verified=True,stealth_contact=next(iter(unique.values())))


def business_mapping(event,reader):
    """A real Stealth record plus matching business correspondence, never a domain guess."""
    from gtm.call_flow.followup import context
    from gtm.briefing_daemon import external_attendees,CODE,STEALTH
    if event.get('status')=='cancelled' or not event.get('start',{}).get('dateTime'):return None
    attendees={a.get('email','').casefold()for a in event.get('attendees',[])if a.get('responseStatus')!='declined' and not a.get('resource')}
    if not attendees.intersection({CODE,STEALTH}) and event.get('organizer',{}).get('email','').casefold()not in {CODE,STEALTH}:return None
    emails=sorted(set(external_attendees(event))-{CODE,STEALTH})
    if not emails:return None
    links=[p.get('uri','')for p in event.get('conferenceData',{}).get('entryPoints',[])if p.get('entryPointType')=='video']
    if event.get('hangoutLink'):links.append(event['hangoutLink'])
    title_match='stealth' in event.get('summary','').casefold() and event.get('organizer',{}).get('email','').casefold()in {CODE,STEALTH}
    if not title_match and not links:return None
    binding=reader.binding(emails)
    if not binding.get('stealth_crm_verified'):return None
    mail=context(event,'stealth')
    matching=[m for m in mail['messages']if 'SENT'in m.get('labels',[]) and 'DRAFT'not in m.get('labels',[])
              and m.get('author')==STEALTH and binding['stealth_contact']['email']in m.get('recipients',[])
              and 'stealth' in m.get('original','').casefold()
              and (title_match or any(link and link in m.get('original','')for link in links))]
    if not matching:return None
    return dict(readback_verified=True,calendar_event_id=event['id'],category='stealth',account=STEALTH,
                lead_id=binding['stealth_contact']['page_id'],contact_emails=[binding['stealth_contact']['email']],
                binding=binding,mail_context=mail)


def validate_review(raw,binding,validate_evidence):
    value=raw.get('stealth_crm')
    contact=binding.get('stealth_contact')
    if not contact:return None
    if not isinstance(value,dict) or set(value)!=set(contact['schema']):raise ValueError('stealth_all_live_fields_must_be_reviewed')
    updates={}
    for name,schema in contact['schema'].items():
        disposition=value[name]
        if (not isinstance(disposition,dict) or disposition.get('decision')not in ('keep','set')
                or not isinstance(disposition.get('reason'),str) or not disposition['reason'].strip()):
            raise ValueError('stealth_crm_field_disposition_required')
        if disposition['decision']=='keep':
            if 'value'in disposition:raise ValueError('stealth_keep_cannot_set_value')
            continue
        if name in PROTECTED or schema.get('readOnly') or schema.get('type')not in ('select','multi_select','text','number','checkbox','date'):
            raise ValueError('stealth_crm_protected_or_unsupported_field')
        evidence=disposition.get('evidence')
        if not isinstance(evidence,list) or not evidence:raise ValueError('stealth_crm_evidence_required')
        for item in evidence:validate_evidence(item)
        new=disposition.get('value');kind=schema['type']
        if kind=='select' and new not in [o['name']for o in schema.get('options',[])]:raise ValueError('stealth_unknown_live_option')
        if kind=='multi_select' and (not isinstance(new,list) or any(x not in [o['name']for o in schema.get('options',[])]for x in new)):raise ValueError('stealth_unknown_live_option')
        if kind=='text' and (not isinstance(new,str) or len(new)>5000):raise ValueError('stealth_field_value_type')
        if kind=='number' and (type(new)not in (int,float) or not disposition.get('basis')):raise ValueError('stealth_number_basis_required')
        if kind=='checkbox' and new not in ('__YES__','__NO__'):raise ValueError('stealth_checkbox_value')
        if kind=='date':
            if not isinstance(new,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',new) or not disposition.get('basis'):raise ValueError('stealth_date_basis_required')
            updates['date:'+name+':start']=new;updates['date:'+name+':is_datetime']=0
        else:updates[name]=new
    return updates


class ContactArchive:
    def __init__(self,client_factory,analysis,binding):
        self.factory=client_factory;self.analysis=analysis;self.contact=binding['stealth_contact']
        self.marker='Hermes call '+analysis.identity.key+' source '+analysis.identity.source_hash
        self.end='End Hermes call '+analysis.identity.key
        archive=analysis.archive_markdown()
        if not archive.endswith(analysis.transcript):raise ValueError('full_archive_source_tail_required')
        fence='`'*max(3,max((len(m.group())+1 for m in re.finditer(r'`+',analysis.transcript)),default=3))
        self.section=('\n## Gespräch '+analysis.occurred_at+'\n\n'+self.marker+'\n\n'
                      +archive[:-len(analysis.transcript)]+fence+'text\n'+analysis.transcript+'\n'+fence+'\n\n'+self.end+'\n')
    def current(self):
        client=self.factory()
        try:
            value=client.call('notion-fetch',{'id':self.contact['url']})
            verify_page(value,self.contact['source_id'],self.contact['email'])
            return value
        finally:client.close()
    def readback(self,identity):
        from gtm.coordinator import Readback
        value=self.current();text=value.get('text','')
        if self.marker not in text:return Readback('absent',identity)
        # Native fetch renders Markdown and may escape literal markup. Verify all
        # original lines as well as both boundary markers, never only a hash label.
        text=html.unescape(text.replace('\\n','\n'))
        text=re.sub(r'\\([\\`*_{}\[\]()#+.!|>~-])',r'\1',text)
        original=self.analysis.transcript.replace('\r\n','\n').replace('\r','\n')
        complete=self.end in text and original in text.replace('\r\n','\n').replace('\r','\n')
        return Readback('verified'if complete else'unknown',identity,self.contact['url'])
    def preflight(self,identity):
        if self.readback(identity).state!='absent':raise ValueError('stealth_archive_not_absent')
    def execute_once(self,identity):
        client=self.factory()
        try:client.call('notion-update-page',{'page_id':self.contact['page_id'],'command':'insert_content',
                    'content':self.section,'position':{'type':'end'},'allow_async':False})
        finally:client.close()
    def url(self):return self.contact['url']


class ContactFields:
    def __init__(self,archive,updates):self.archive=archive;self.updates=updates
    def readback(self,identity):
        from gtm.coordinator import Readback
        if self.updates is None:return Readback('unknown',identity)
        current=self.archive.current();properties=json.loads(tag(current,'properties'))
        if all(properties.get(k)==v for k,v in self.updates.items()):return Readback('verified',identity,self.archive.url())
        # Preserve concurrent edits to any field this plan would change.
        if any(properties.get(k)!=self.archive.contact['properties'].get(k)for k in self.updates):return Readback('conflict',identity)
        return Readback('absent',identity)
    def preflight(self,identity):
        if self.readback(identity).state!='absent':raise ValueError('stealth_field_snapshot_changed')
    def execute_once(self,identity):
        client=self.archive.factory()
        try:client.call('notion-update-page',{'page_id':self.archive.contact['page_id'],'command':'update_properties','properties':self.updates,'allow_async':False})
        finally:client.close()


class NotApplicable:
    def readback(self,identity):
        from gtm.coordinator import Readback
        return Readback('verified',identity,'not_applicable_stealth_uses_notion')
    def preflight(self,identity):raise ValueError('not_applicable')
    def execute_once(self,identity):raise ValueError('not_applicable')
