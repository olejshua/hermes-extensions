"""Official eForms award originals, with an actual result/tender/party chain.

Schema: https://docs.ted.europa.eu/eforms/latest/schema/competition-results.html
and /parties.html. Legacy TEDXML is explicitly unsupported, never guessed.
"""
import base64
from datetime import datetime, timezone
import json
import re
import unicodedata
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import xml.etree.ElementTree as ET

class ContractError(ValueError): pass
class PermanentFailure(ValueError): pass
def canonical(v): return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))


NS={'c':'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'a':'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'e':'http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1',
    'b':'http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1'}
ROOT='{urn:oasis:names:specification:ubl:schema:xsd:ContractAwardNotice-2}ContractAwardNotice'
PUBLICATION=re.compile(r'[1-9][0-9]{0,8}-20[0-9]{2}')


def text(node,path):
    rows=node.findall(path,NS)
    if len(rows)!=1 or not (rows[0].text or '').strip():raise ContractError('ted_award_chain_ambiguous')
    return rows[0].text.strip()


def legal_name(value):
    # Punctuation/spacing normalization only, no removal of legal entity forms.
    value=unicodedata.normalize('NFKC',value).casefold()
    return ' '.join(re.findall(r'[^\W_]+',value,flags=re.UNICODE))


def party_id(node):
    # Both documented eForms serializations exist: Parties uses cbc:ID,
    # Competition Results shows cac:PartyIdentification/cbc:ID. Preserve a
    # repeated party list, but never guess conflicting identities in one party.
    ids=[n.text.strip()for path in ('c:ID','a:PartyIdentification/c:ID')for n in node.findall(path,NS)if n.text and n.text.strip()]
    if not ids or len(set(ids))!=1:raise ContractError('ted_party_reference_ambiguous')
    return ids[0]


def award_facts(raw,firm):
    if len(raw)>8*1024*1024 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)',raw,re.I):raise ContractError('ted_complete_safe_xml_required')
    try:root=ET.fromstring(raw)
    except ET.ParseError:raise ContractError('ted_complete_safe_xml_required')from None
    if root.tag!=ROOT:raise ContractError('ted_supported_award_notice_required')
    kind=root.find('c:NoticeTypeCode',NS)
    if kind is None or not (kind.text or '').startswith('can-'):raise ContractError('ted_actual_award_notice_required')
    procedure=text(root,'c:ContractFolderID')
    organizations={}
    for org in root.findall('.//e:Organizations/e:Organization/e:Company',NS):
        oid=text(org,'a:PartyIdentification/c:ID')
        if oid in organizations:raise ContractError('ted_duplicate_organization')
        names=[n.text.strip()for n in org.findall('a:PartyName/c:Name',NS)if n.text and n.text.strip()]
        if not names:raise ContractError('ted_organization_name_missing')
        organizations[oid]={'names':names,'legal_ids':[n.text for n in org.findall('a:PartyLegalEntity/c:CompanyID',NS)],
            'website':org.findtext('c:WebsiteURI',default='',namespaces=NS)}
    buyers={party_id(p)for n in root.findall('a:ContractingParty',NS)for p in n.findall('a:Party',NS)}
    sections=root.findall('.//e:NoticeResult',NS)
    if len(sections)!=1 or not buyers or not buyers<=set(organizations):raise ContractError('ted_unique_result_and_buyers_required')
    section=sections[0]
    def indexed(tag):
        rows=section.findall('e:'+tag,NS);result={}
        for row in rows:
            key=text(row,'c:ID')
            if key in result:raise ContractError('ted_duplicate_result_identity')
            result[key]=row
        return result
    tenders=indexed('LotTender');parties=indexed('TenderingParty');contracts=indexed('SettledContract');facts=[]
    for result in section.findall('e:LotResult',NS):
        if result.findtext('c:TenderResultCode',default='',namespaces=NS)!='selec-w':continue
        lot=text(result,'e:TenderLot/c:ID');result_id=text(result,'c:ID')
        for tender_ref in result.findall('e:LotTender',NS):
            tid=text(tender_ref,'c:ID');tender=tenders.get(tid)
            if tender is None or text(tender,'e:TenderLot/c:ID')!=lot:raise ContractError('ted_result_tender_lot_mismatch')
            # A mentioned/non-winning tender has no awarded contract edge.
            matching=[(cid,c)for cid,c in contracts.items()if tid in [n.text for n in c.findall('e:LotTender/c:ID',NS)]
                and cid in [n.text for n in result.findall('e:SettledContract/c:ID',NS)]]
            if not matching:continue
            party=parties.get(text(tender,'e:TenderingParty/c:ID'))
            if party is None:raise ContractError('ted_tender_party_missing')
            winner_ids=[text(w,'c:ID')for w in party.findall('e:Tenderer',NS)]
            if not winner_ids or any(w not in organizations for w in winner_ids):raise ContractError('ted_winner_organization_missing')
            ours=[wid for wid in winner_ids if any(legal_name(n)==legal_name(firm)for n in organizations[wid]['names'])]
            if len(ours)>1:raise ContractError('ted_legal_winner_ambiguous')
            if not ours:continue
            for cid,contract in matching:
                signatories=contract.findall('a:SignatoryParty',NS)
                signing={party_id(p)for p in signatories}
                binding='explicit_contract_signatories'
                if not signing and len(buyers)==1:
                    # Signatory is optional. Exactly one actual Buyer for the
                    # complete notice also unambiguously owns this result/lot.
                    signing=set(buyers);binding='sole_notice_buyer'
                if not signing or not signing<=buyers:raise ContractError('ted_lot_buyer_ambiguous')
                for buyer_id in sorted(signing):
                    buyer=organizations[buyer_id]['names']
                    facts.append({'result_id':result_id,'lot_id':lot,'tender_id':tid,'contract_id':cid,
                        'procedure_id':procedure,'contract_reference':contract.findtext('e:ContractReference/c:ID',default='',namespaces=NS),
                        'winner_id':ours[0],'winner':next(n for n in organizations[ours[0]]['names']if legal_name(n)==legal_name(firm)),
                        'buyer_id':buyer_id,'buyer':buyer[0],'buyer_names':buyer,
                        'buyer_ids':sorted(signing),'buyer_binding':binding})
    if not facts:raise PermanentFailure('ted_no_matching_awarded_legal_entity')
    return facts


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        url=urlsplit(newurl)
        if url.scheme!='https'or url.hostname!='ted.europa.eu'or url.username or url.password or url.port not in (None,443):
            raise ContractError('ted_official_origin_changed')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(publication):
    if not PUBLICATION.fullmatch(publication):raise ContractError('ted_exact_publication_required')
    url='https://ted.europa.eu/en/notice/'+publication+'/xml'
    with build_opener(OfficialRedirects()).open(Request(url,headers={'User-Agent':'Hermes-official-notice-reader','Accept':'application/xml'}),timeout=30)as response:
        raw=response.read(8*1024*1024+1)
        if response.status!=200 or len(raw)>8*1024*1024:raise ContractError('ted_complete_official_xml_required')
        return raw,{'requested_url':url,'response_url':response.url,'status':response.status,'headers':list(response.headers.items())}


def search_winner(firm):
    """Official anonymous read API; bounded discovery, never a pool expansion."""
    if not isinstance(firm,str)or not firm.strip()or any(c in firm for c in '\r\n"\\'):
        raise ContractError('ted_exact_existing_company_name_required')
    url='https://api.ted.europa.eu/v3/notices/search'
    body={'query':'winner-name = "'+firm+'"','fields':['publication-number','winner-name','publication-date'],
          'page':1,'limit':6,'scope':'ALL','paginationMode':'PAGE_NUMBER','onlyLatestVersions':True}
    class NoRedirects(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ContractError('ted_official_search_origin_changed')
    with build_opener(NoRedirects()).open(Request(url,data=canonical(body).encode(),
        headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'Hermes-official-notice-reader'},method='POST'),timeout=30)as response:
        raw=response.read(2*1024*1024+1)
        if response.status!=200 or len(raw)>2*1024*1024:raise ContractError('ted_complete_search_response_required')
        result=json.loads(raw)
        if (not isinstance(result,dict)or result.get('timedOut')is True or
            type(result.get('totalNoticeCount'))is not int or not isinstance(result.get('notices'),list)):
            raise ContractError('ted_complete_search_response_required')
        return {'http':{'requested_url':url,'response_url':response.url,'status':response.status,'headers':list(response.headers.items())},
                'request':body,'response':result,'response_raw_base64':base64.b64encode(raw).decode()}


