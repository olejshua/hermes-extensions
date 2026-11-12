"""Typed legacy F03/F06 award proof, never an arbitrary mentioned supplier.

This supports original pre-eForms notices for the repeated-award evidence. The
selected modern notice still requires its exact lot/winner/buyer graph.
"""
import re
import xml.etree.ElementTree as ET
from .central import Stop


def legal(value):return ' '.join(re.findall(r'[^\W_]+',value.casefold()))


def legacy_awards(raw,firm,publication):
    if len(raw)>8*1024*1024 or re.search(br'<!\s*(DOCTYPE|ENTITY)',raw,re.I):raise Stop('unsafe_legacy_xml')
    root=ET.fromstring(raw)
    if not root.tag.endswith('}TED_EXPORT')or not root.tag.startswith('{http://publications.europa.eu/resource/schema/ted/'):
        raise Stop('unsupported_legacy_schema')
    namespace=root.tag.split('}')[0]+'}';q=lambda name:namespace+name
    section=root.find(q('FORM_SECTION'))
    if section is None:raise Stop('legacy_original_form_missing')
    forms=[f for f in section if f.tag in {q('F03_2014'),q('F06_2014')}
           and f.get('CATEGORY')=='ORIGINAL']
    if len(forms)!=1:raise Stop('legacy_unique_original_award_required')
    form=forms[0]
    buyers=[n.text.strip()for n in form.findall(q('CONTRACTING_BODY')+'/'+q('ADDRESS_CONTRACTING_BODY')+'/'+q('OFFICIALNAME'))if n.text]
    if not buyers:raise Stop('legacy_buyer_missing')
    facts=[]
    for contract in form.findall(q('AWARD_CONTRACT')):
        awarded=contract.find(q('AWARDED_CONTRACT'))
        if awarded is None:continue
        date=awarded.findtext(q('DATE_CONCLUSION_CONTRACT'))
        if not date:raise Stop('legacy_awarded_contract_date_required')
        winners=[n.text.strip()for n in awarded.findall(q('CONTRACTORS')+'/'+q('CONTRACTOR')+'/'+q('ADDRESS_CONTRACTOR')+'/'+q('OFFICIALNAME'))if n.text]
        exact=[name for name in winners if legal(name)==legal(firm)]
        if len(exact)>1:raise Stop('legacy_winner_ambiguous')
        if not exact:continue
        lot=contract.findtext(q('LOT_NO'))or contract.get('ITEM')or'unknown'
        facts.append({'publication':publication,'winner':exact[0],'buyers':buyers,
            'award_date':date,'lot_id':lot,'contract_reference':contract.findtext(q('CONTRACT_NO')),
            'secondary_winner_proof':True,'source_schema':'TED_EXPORT/F03-or-F06/ORIGINAL/AWARDED_CONTRACT'})
    if not facts:raise Stop('legacy_no_exact_awarded_winner')
    return facts
