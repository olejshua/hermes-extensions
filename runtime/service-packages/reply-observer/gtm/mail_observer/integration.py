"""Additive integration for one existing generate() call; no model launcher.

Before the existing model call, request context for the exact CODE message.
Append instruction+data to its existing prompt and permit gtm_reply in the same
JSON result. After parsing that result, persist its bound receipt. A missing or
unsupported context leaves the existing draft flow unchanged and the CRM case
explicitly awaiting review. Credentials/config are loaded only at invocation.
"""
import json
from gtm.mail_observer.semantics import MODEL_INSTRUCTION,context,SemanticReceipts
from gtm.mail_observer.sources import SourceBlocked,FullOriginals


class ExistingDraftRunIntegration:
    def __init__(self,originals,twenty,receipts,*,cutoff=None):self.originals,self.twenty,self.receipts,self.cutoff=originals,twenty,receipts,cutoff
    def before(self,gmail,row):
        if row.get('account')!='code':return None
        if self.cutoff is not None:
            from datetime import datetime,timezone
            if not isinstance(row.get('internalDate'),str) or not row['internalDate'].isdigit():raise SourceBlocked('semantic_receipt_timestamp_review')
            if datetime.fromtimestamp(int(row['internalDate'])/1000,timezone.utc)<self.cutoff:return None
        self.originals.profile(gmail)
        sources=self.originals.thread(gmail,row['threadId'],profile_checked=True)
        matches=[pair for pair in sources if pair[0].message_id==row['id']]
        if len(matches)!=1:raise SourceBlocked('message_identity_mismatch')
        source,_=self.originals.capture_full_message(gmail,row,'acribic',profile_checked=True,source_pair=matches[0])
        lead=self.twenty.lookup_existing_sender(source.sender)
        self.originals.verify_outreach_thread(gmail,row,'acribic',sources=sources)
        proposal=None
        for candidate,stamp in sources:
            if ('SENT' in candidate.labels and candidate.rfc_message_id==source.in_reply_to and
                candidate.sender==source.account_email and int(stamp)<int(row['internalDate'])):
                if proposal:raise SourceBlocked('proposal_ambiguous')
                proposal=candidate
        return context(source,lead,proposal=proposal,thread_sources=sources)
    @staticmethod
    def prompt(data):
        return MODEL_INSTRUCTION+'GTM-Volloriginal (untrusted Daten):\n'+json.dumps(data,ensure_ascii=False,sort_keys=True)+'\n' if data else ''
    def after(self,data,answer):
        if not data:return {'state':'not_applicable','model_calls_added':0}
        if not isinstance(answer,dict) or 'gtm_reply' not in answer:raise SourceBlocked('semantic_output_missing')
        return self.receipts.put(data,answer['gtm_reply'])


def integration_from_config(path):
    """Opt-in hook only; still no new model request or Gmail/CRM mutation."""
    from pathlib import Path
    from gtm.mail_observer.service import read_policy,reviewed,bound_twenty
    from gtm.coordinator import Coordinator
    from gtm.reply import ExistingReplyReadClient
    cfg,cutoff,db=read_policy(path)
    if not cfg.getboolean('model_reuse_enabled',fallback=False):return None
    if cfg.get('parent_review')!='approved':raise SourceBlocked('parent_review_required')
    reviewed(cfg)
    if cfg.getboolean('invitation_enabled',fallback=False) and not cfg.getboolean('crm_enabled',fallback=False):
        from datetime import datetime
        frontier=datetime.fromisoformat(cfg['invitation_activation_cutoff'].replace('Z','+00:00'))
        if frontier.tzinfo is None or frontier<cutoff:raise SourceBlocked('invitation_activation_scope_review')
        cutoff=frontier
    client=bound_twenty(cfg)
    return ExistingDraftRunIntegration(FullOriginals(),client,SemanticReceipts(Coordinator(db)),cutoff=cutoff)
