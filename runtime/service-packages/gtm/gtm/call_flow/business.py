"""Externally organised/custom-title business calls require actual native record IDs."""
from gtm.coordinator import ACCOUNTS


def verified_business_mapping(event,read_record):
    if read_record is None:return None
    # This callable is the reviewed native reader, never a model-supplied mapping.
    proof=read_record(event)
    if not isinstance(proof,dict) or proof.get('readback_verified')is not True:return None
    if proof.get('calendar_event_id')!=event.get('id') or proof.get('category')not in ACCOUNTS:return None
    if proof.get('account')!=ACCOUNTS[proof['category']]:return None
    if not isinstance(proof.get('lead_id')or proof.get('task_id'),str) or not (proof.get('lead_id')or proof.get('task_id')):return None
    addresses={a.get('email','').casefold() for a in event.get('attendees',[]) if a.get('responseStatus')!='declined' and not a.get('resource')}
    known=proof.get('contact_emails')
    if not isinstance(known,list) or not known or not all(isinstance(a,str) and a.casefold()in addresses for a in known):return None
    return proof
