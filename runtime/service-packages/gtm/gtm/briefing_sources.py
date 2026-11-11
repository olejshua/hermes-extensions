"""Read-only briefing enrichment over the installed native mail/CRM readers.

No model, writes, search by guessed company, or alternate account is used here.
Each optional failure is represented as a gap alongside the remaining originals.
"""
from __future__ import annotations
import hashlib
import importlib
import importlib.util
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

LIMIT = 240_000


def native_routing():
    """The reviewed shared reader is an explicit dependency, not ambient sys.path."""
    path=Path('/home/hermes/.hermes/scripts/calendar_routing.py')
    if not path.is_file():raise ValueError('native_project_router_missing')
    spec=importlib.util.spec_from_file_location('_hermes_call_shared_routing',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def gap(context, source, exc):
    context.setdefault('source_gaps', []).append(dict(source=source, code=type(exc).__name__))


def document(source_id, kind, text, **metadata):
    return dict(source_id=source_id, kind=kind, text=text,
                sha256=hashlib.sha256(text.encode()).hexdigest(), **metadata)


def complete_messages(module, alias, raw, service):
    """Reuse verified RFC ancestors even when the latest message is our reply.

    The shared reader checks the exact reference, account, SENT, recipients,
    chronology and real thread ID. No new RFC resolver is implemented here.
    """
    messages = sorted(raw['messages'], key=lambda m: int(m.get('internalDate') or 0))
    result = {m['id']: m for m in messages}
    complete = getattr(module, 'complete_thread_sources', None)
    if not callable(complete):
        raise ValueError('native_reference_reader_missing')
    expected = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}[alias]
    from email.utils import parseaddr
    incoming = []
    for index, message in enumerate(messages):
        headers = {h['name'].lower(): h['value'] for h in message.get('payload', {}).get('headers', [])}
        if ('DRAFT' not in message.get('labelIds', []) and 'SENT' not in message.get('labelIds', [])
                and parseaddr(headers.get('from', ''))[1].casefold() != expected):
            incoming.append(index)
    # First and latest incoming messages cover the root and the current reply;
    # bounded intermediate references are included without mailbox scanning.
    indexes = list(dict.fromkeys(incoming[:1] + incoming[-7:]))
    for index in indexes:
        for message in complete(alias, messages[:index + 1], service):
            if message['id'] not in result:
                if message.get('hermes_reference_source') is not True:
                    raise ValueError('unverified_cross_thread_source')
                result[message['id']] = message
    return sorted(result.values(), key=lambda m: int(m.get('internalDate') or 0))


def compact(messages):
    return [dict(id=m['message_id'], thread_id=m['thread_id'], labels=m['labels'],
                 reference_source=m.get('reference_source', False), internalDate=m['internal_date'],
                 text=m['original'], truncated=False, **{
                     'from': m['headers'].get('from'), 'to': m['headers'].get('to'), 'cc': m['headers'].get('cc'),
                     'rfc_message_id': m['headers'].get('message-id'), 'in_reply_to': m['headers'].get('in-reply-to'),
                     'references': m['headers'].get('references')}) for m in messages if 'DRAFT' not in m['labels']]


def project_proof(context, contacts, module=None):
    """Bind project attribution using the reviewed shared native routing code."""
    router = module or native_routing()
    messages = compact(context['messages'])
    for incoming in reversed(messages):
        from email.utils import parseaddr
        if ('SENT' in incoming['labels'] or parseaddr(incoming.get('from') or '')[1].casefold() not in contacts):
            continue
        chain = [m for m in messages if (m['thread_id'] == incoming['thread_id'] or m.get('reference_source'))
                 and int(m['internalDate'] or 0) <= int(incoming['internalDate'] or 0)]
        try:
            if router.project_from_thread('code', chain) == 'acribic':
                return dict(category='acribic', source_thread_id=incoming['thread_id'], source_message_id=incoming['id'],
                            contact_emails=[parseaddr(incoming.get('from') or '')[1].casefold()], readback_verified=True)
        except ValueError:
            continue
    return None


def existing_crm(context, contacts, client=None):
    """Exact participant identity first, then the already reviewed RFC handoff."""
    from gtm.reply import ExistingReplyReadClient, ExistingLeadNotFound, make_crm_binding_resolver
    underlying = client or ExistingReplyReadClient(Path('/home/hermes/.hermes/gtm/helpers'), Path('/home/hermes/.config/acribic/twenty-api-key'))
    class ReadCache:
        def __init__(self):self.lookups={};self.records={}
        def lookup_existing_sender(self,email):
            if email not in self.lookups:
                try:self.lookups[email]=underlying.lookup_existing_sender(email)
                except ValueError as error:self.lookups[email]=error
            value=self.lookups[email]
            if isinstance(value,Exception):raise value
            return value
        def get(self,kind,rid):
            key=(kind,rid)
            if key not in self.records:self.records[key]=underlying.get(kind,rid)
            return self.records[key]
        def query(self,*args,**kwargs):return underlying.query(*args,**kwargs)
    client=ReadCache()
    lookup_emails=contacts;identity_proof=None
    business = dict(category='acribic', account='operator@primary.example.invalid',
                    occurred_at=context['calendar']['start']['dateTime'], mail_context=context)
    mapped = make_crm_binding_resolver(client)(contacts, business)
    if not mapped.get('crm_binding_verified'):
        # Missing exact participant records may be a genuine role-mailbox handoff.
        # Ambiguity or a read error must not be downgraded to a domain guess.
        for email in contacts:
            try:
                client.lookup_existing_sender(email)
            except ExistingLeadNotFound:
                continue
            else:
                return dict(available=False, reason='participant_mapping_not_unique')
        from gtm.call_flow.identity import original_recipients
        original = original_recipients(business, contacts)
        if original:
            mapped = make_crm_binding_resolver(client)(original['addresses'], business)
            lookup_emails=original['addresses'];identity_proof=original['proof']
    if not mapped.get('crm_binding_verified'):
        return dict(available=False, reason='existing_contact_binding_unresolved')
    company = client.get('company', mapped['company_id'])
    person_ids={client.lookup_existing_sender(email).person_id for email in lookup_emails}
    people=[]
    for pid in person_ids:
        if not pid:continue
        try:people.append(client.get('person',pid))
        except Exception as exc:gap(context,'twenty:person:'+pid,exc)
    # Notes and relationships are evidence, not current commitments by default.
    try:
        notes = client.query('''query($filter:NoteTargetFilterInput!){noteTargets(first:21,filter:$filter){pageInfo{hasNextPage} edges{node{id noteId}}}}''',
                             {'filter': {'targetCompanyId': {'eq': company['id']}}})['noteTargets']
    except Exception as exc:
        gap(context,'twenty:company_notes',exc)
        notes=dict(pageInfo=dict(hasNextPage=False),edges=[])
    note_docs = []
    if notes.get('pageInfo', {}).get('hasNextPage') or len(notes['edges']) > 20:
        context.setdefault('source_gaps', []).append(dict(source='crm_notes', code='bounded_notes_scope'))
    else:
        for row in notes['edges']:
            try:note = client.get('note', row['node']['noteId'])
            except Exception as exc:
                gap(context,'twenty:note:'+row['node']['noteId'],exc)
                continue
            if note:
                text = (note.get('title') or '') + '\n' + ((note.get('bodyV2') or {}).get('markdown') or '')
                note_docs.append(document('twenty:note:' + note['id'], 'crm_note', text, company_id=company['id'],updated_at=note.get('updatedAt')))
    facts = {k: company.get(k) for k in ('id', 'updatedAt', 'name', 'domainName', 'schwerpunkt', 'branche', 'art',
              'industryEvidence', 'interestEvidence', 'tags', 'notionQuellen', 'prioritaet')}
    facts['effective_sources']=[dict(page_id=s.get('page_id'),**{k:v for k,v in {**s.get('properties',{}),**s.get('outreach',{})}.items() if k!='page_id'}) for s in company.get('notionQuellen') or []]
    docs = [document('twenty:company:' + company['id'], 'crm_company', json.dumps(facts, ensure_ascii=False),company_id=company['id'],updated_at=company.get('updatedAt'))]
    for person in people:
        if person:
            docs.append(document('twenty:person:' + person['id'], 'crm_person', json.dumps({k: person.get(k)
                for k in ('id', 'name', 'jobTitle', 'rolle', 'companyId')}, ensure_ascii=False),company_id=company['id'],updated_at=person.get('updatedAt')))
    docs.extend(note_docs)
    return dict(available=True, company_id=company['id'], company=facts, people=people, documents=docs,
                matched_existing_emails=lookup_emails,identity_proof=identity_proof)


class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg'): self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden and data.strip(): self.parts.append(data.strip())


def public_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or '.' not in parsed.hostname):
        raise ValueError('public_company_url_required')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('nonpublic_company_url')
    return parsed


def website(url):
    original = public_url(url)
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            parsed = public_url(newurl)
            if parsed.hostname.removeprefix('www.') != original.hostname.removeprefix('www.'):
                raise ValueError('company_redirect_needs_review')
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    request = Request(url, headers={'User-Agent': 'Hermes-Call-Briefing/1.0', 'Accept': 'text/html'})
    with build_opener(Redirect()).open(request, timeout=5) as response:
        if response.headers.get_content_type() != 'text/html': raise ValueError('company_page_not_html')
        raw = response.read(180_001)
        parsed = Text(); parsed.feed(raw[:180_000].decode(response.headers.get_content_charset() or 'utf-8', errors='replace'))
        text = '\n'.join(parsed.parts)
        # This is explicitly a homepage excerpt, never a complete website or mail.
        return document('website:' + url, 'company_website_excerpt', text[:12_000], url=response.geturl(),
                        complete=False, excerpt=True, bytes_read=min(len(raw), 180_000))


def enrich(context, category, contacts, *, crm=None, fetch_website=website, research=None):
    context.setdefault('documents', [])
    context.setdefault('source_gaps', [])
    context['documents'].append(document('calendar:'+context['calendar']['id'], 'calendar',
                                         json.dumps(context['calendar'], ensure_ascii=False)))
    if category == 'acribic':
        try:
            context['crm'] = (crm or existing_crm)(context, contacts)
            context['documents'].extend(context['crm'].pop('documents', []))
        except Exception as exc:
            context['crm'] = dict(available=False, reason=type(exc).__name__)
            gap(context, 'crm', exc)
        if research:
            try:
                value = research(compact(context['messages']))
                for source in (value or {}).get('sources', []):
                    context['documents'].append(document('project:' + source['id'], 'project_research_focus', source['text'],
                        observed_at=value.get('observed_sent_at'), scope=value['scope'], precedence=value['precedence']))
            except Exception as exc:
                gap(context, 'project_research_focus', exc)
    urls = []
    domain = context.get('crm', {}).get('company', {}).get('domainName') or {}
    if domain.get('primaryLinkUrl'):
        urls.append(domain['primaryLinkUrl'])
    for source in context.get('crm',{}).get('company',{}).get('effective_sources',[]):
        if isinstance(source.get('Website'),str) and source['Website'].strip():urls.append(source['Website'].strip())
    # Only explicit business sender URLs on the actual contact's corporate domain.
    # No guessed www/domain URL, tracking URL, private host, or shared-mail provider.
    domains = {e.rsplit('@', 1)[-1].casefold() for e in contacts} - {'gmail.com', 'hotmail.de', 'hotmail.com', 'outlook.com', 'primary.example.invalid', 'gmx.de', 'web.de'}
    for message in context['messages']:
        if message.get('author') not in contacts or 'DRAFT' in message.get('labels', []) or message.get('calendar_notification'): continue
        for url in re.findall(r'https?://[^\s<>"\)]+', message['original']):
            parsed = urlsplit(url.rstrip('.,;'))
            if parsed.hostname and parsed.hostname.removeprefix('www.').casefold() in domains:
                urls.append('https://' + parsed.netloc + '/')
    for url in list(dict.fromkeys(urls))[:1]:
        try:
            if url.startswith('http://'):url='https://'+url[len('http://'):]
            elif '://'not in url:url='https://'+url
            context['documents'].append(fetch_website(url))
        except Exception as exc:
            gap(context, 'company_website', exc)
    if len(json.dumps(context, ensure_ascii=False).encode()) > LIMIT:
        # Optional sources are whole documents; complete mail originals are kept.
        while context['documents'] and len(json.dumps(context, ensure_ascii=False).encode()) > LIMIT:
            omitted = context['documents'].pop()
            context['source_gaps'].append(dict(source=omitted['source_id'], code='document_did_not_fit'))
    return context
