"""Bounded company-owned originals for the one quality worker's role judgement.

The presence of these pages proves no tender pressure. The same native worker
must cite the actual relevant source and judge bidder role and need; TED counts
are not substituted. Only existing CRM website links and links on those pages.
"""
from datetime import datetime,timezone
from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.parse import urlsplit,urljoin
from urllib.request import build_opener,HTTPRedirectHandler,Request

from background_jobs.contracts import ContractError,PermanentFailure
from .pool_sources import family
from .sources import persist


class Page(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.text=[];self.links=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag=='a':
            for name,value in attrs:
                if name=='href'and value:self.links.append(value)
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,value):
        if not self.hidden and value.strip():self.text.append(value.strip())


def safe_url(url,domains):
    parts=urlsplit(url)
    if (parts.scheme!='https'or not parts.hostname or parts.username or parts.password or
        parts.port not in (None,443)or parts.fragment or family(parts.hostname)not in {family(d)for d in domains}):
        raise ContractError('company_existing_official_origin_required')
    addresses=socket.getaddrinfo(parts.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ContractError('company_official_origin_not_public')
    return parts


def read_page(url,domains):
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            safe_url(newurl,domains)
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    safe_url(url,domains)
    with build_opener(Redirect()).open(Request(url,headers={'User-Agent':'Hermes-company-source-reader'}),timeout=20)as response:
        safe_url(response.url,domains);raw=response.read(2*1024*1024+1)
        if response.status!=200 or len(raw)>2*1024*1024:raise PermanentFailure('company_complete_page_unavailable')
        media=response.headers.get_content_type()
        if media not in ('text/html','text/plain'):raise PermanentFailure('company_supported_original_required')
        try:text=raw.decode(response.headers.get_content_charset()or 'utf-8',errors='strict')
        except (LookupError,UnicodeError):raise PermanentFailure('company_complete_page_unavailable')from None
        return text,{'requested_url':url,'response_url':response.url,'status':response.status,
                     'headers':list(response.headers.items())}


class CompanyOriginals:
    def __init__(self,root,reader=read_page):self.root,self.reader=root,reader
    def capture(self,identity,candidate,properties,active):
        website=properties.get('Website')
        if not isinstance(website,str)or not website.startswith('https://'):
            raise PermanentFailure('company_existing_https_website_required')
        pending=[website];seen=set();documents=[]
        while pending and len(seen)<4:
            url=pending.pop(0)
            if url in seen:continue
            seen.add(url);active();html,http=self.reader(url,candidate['family_domains']);active()
            parts=urlsplit(http.get('response_url',''))
            if http.get('requested_url')!=url or http.get('status')!=200 or family(parts.hostname or '')not in {family(d)for d in candidate['family_domains']}:
                raise ContractError('company_original_origin_changed')
            page=Page();page.feed(html)
            documents.append(persist(self.root,'company','company:'+url,
                {'fetched_at':datetime.now(timezone.utc).isoformat(),'http':http,'html':html,'visible_text':'\n'.join(page.text)},
                {'identity':identity,'candidate':candidate},'company.existing-official-link.read'))
            for href in page.links:
                linked=urljoin(http['response_url'],href).split('#',1)[0]
                p=urlsplit(linked)
                if (re.search(r'ausschreibung|tender|\bbid\b|public-sector|karriere|careers|jobs',linked,re.I)and
                    p.scheme=='https'and not p.username and not p.password and p.port in (None,443)and
                    family(p.hostname or '')in {family(d)for d in candidate['family_domains']}and linked not in seen):
                    pending.append(linked)
        if not documents:raise PermanentFailure('company_bidder_and_pressure_original_missing')
        return documents
