"""Last-wire refinement of the actual BG SourceDelivery/Codex capture hooks.

No callback parameters or self-reported model flags can commit a quality result.
The inherited capture requires actual serialization, 2xx and native post-request.
"""
from background_jobs.source_delivery import SourceDelivery
from background_jobs.contracts import ContractError, digest, canonical
from background_jobs.outreach_candidate import native_plan
import json


class LastWireDelivery(SourceDelivery):
    def __init__(self,identity,text):
        super().__init__(identity,text)
        self.last_success=None;self.last_ranges=[];self.body_hashes={}
        self.latest_request=None;self.last_native_receipt=None;self.terminal_failed=False
    def reset(self):
        super().reset();self.last_success=None;self.last_ranges=[];self.body_hashes.clear()
        self.latest_request=None;self.last_native_receipt=None;self.terminal_failed=False
    def pre_request(self,**event):
        with self.lock:
            super().pre_request(**event)
            key=event.get('api_request_id')
            if key in self.requests and event.get('session_id')==self.session_id:self.latest_request=key
    def provider_response(self,**event):
        key=(event.get('api_request_id'),event.get('wire_request_id'))
        with self.lock:
            already=event.get('wire_request_id') in self.confirmed
            super().provider_response(**event)
            if not already and event.get('wire_request_id') in self.confirmed:
                self.body_hashes[key]=event.get('body_sha256')
    def _commit(self,request,wire):
        key=(request,wire)
        if key in self.successful and key in self.pending:
            self.last_success=key;self.last_ranges=self._merged(self.pending[key])
            # Sealed only after inherited actual serialization/2xx AND native
            # post_api_request have joined. No later actor counter is guessed.
            self.last_native_receipt={'actor_id':self.agent_id,'session_id':self.session_id,'scope_hash':self.scope,
                'source_hash':self.identity['item']['source_hash'],'api_request_id':request,'wire_request_id':wire}
        super()._commit(request,wire)
    def missing(self):
        with self.lock:
            cursor=0;missing=[]
            for start,end in self.last_ranges:
                if start>cursor:missing.append((cursor,start))
                cursor=max(cursor,end)
            if cursor<len(self.text):missing.append((cursor,len(self.text)))
            return missing
    def complete(self):
        with self.lock:
            return (not self.terminal_failed and bool(self.last_success and self.last_ranges and self.last_native_receipt) and
                self.last_success in self.successful and self.last_success[1] in self.confirmed and
                self.latest_request==self.last_success[0] and digest(self.identity)==self.scope and
                self.actor is not None and id(self.actor)==self.agent_id and getattr(self.actor,'session_id',None)==self.session_id and
                getattr(self.actor,'_last_provider_wire_request_id',None)==self.last_success[1] and
                digest(self.text.encode())==self.identity['item']['source_hash'] and not self.missing())
    def status(self):
        with self.lock:
            value=super().status();value['confirmed_source_characters']=sum(b-a for a,b in self.last_ranges)
            value['last_successful_api_request_id']=self.last_success[0] if self.last_success else None
            value['last_successful_wire_request_id']=self.last_success[1] if self.last_success else None
            value['last_wire_body_sha256']=self.body_hashes.get(self.last_success)
            value['last_wire_complete']=self.complete()
            return value
    def quality_binding(self,actor):
        value=self.status()
        if (actor is not self.actor or not self.complete() or not value['last_wire_body_sha256'] or
            getattr(actor,'_current_api_request_id',None)!=value['last_successful_api_request_id'] or
            getattr(actor,'_last_provider_wire_request_id',None)!=value['last_successful_wire_request_id']):
            raise ContractError('quality_requires_last_complete_native_wire')
        return value

    def terminal_binding(self,actor,identity,original):
        with self.lock:
            value=self.status();native=self.last_native_receipt
            if (actor is not self.actor or id(actor)!=self.agent_id or getattr(actor,'session_id',None)!=self.session_id or
                identity!=self.identity or digest(identity)!=self.scope or not isinstance(original,bytes) or
                digest(original)!=identity['item']['source_hash'] or original!=self.text.encode() or
                not self.complete() or not value['last_wire_body_sha256'] or
                getattr(actor,'_last_provider_wire_request_id',None)!=value['last_successful_wire_request_id'] or
                native!={'actor_id':id(actor),'session_id':self.session_id,'scope_hash':self.scope,
                    'source_hash':identity['item']['source_hash'],'api_request_id':value['last_successful_api_request_id'],
                    'wire_request_id':value['last_successful_wire_request_id']}):
                self.terminal_failed=True
                raise ContractError('native_terminal_last_full_original_required')
            # The authoritative API ID was checked by inherited post_request
            # while that request was current. It is retained in this receipt;
            # terminal export never infers an ID from a reset actor counter.
            return {'contract':'outreach-terminal-wire-v1','identity':identity,**native,
                    'body_sha256':value['last_wire_body_sha256'],'source_delivery':value}


class QualityCommit:
    """Native tool result of the same strong worker, never an outer callback."""
    def __init__(self,reader,candidate):self.reader,self.candidate,self.accepted=reader,candidate,None
    def __call__(self,args,**_):
        wire=self.reader.delivery.quality_binding(self.reader.delivery.actor)
        if json.loads(self.reader.text).get('missing_native_sources') and args.get('decision')=='ready':
            raise ContractError('native_quality_required_original_sources_missing')
        identity=self.reader.identity
        result={'identity':identity,'analysis':canonical(args),'complete_original_read':True,'budget_exhausted':False,
                'source_delivery':wire,'model':'gpt-6.1-sol','effort':'medium','provider':'openai-codex'}
        draft_identity={**identity,'phase':'draft'}
        native_plan(result,draft_identity,self.candidate,self.reader.text.encode())
        packet={**result,'quality_wire':{'analysis_sha256':digest(result['analysis'].encode()),
                'source_hash':identity['item']['source_hash'],'wire_request_id':wire['last_successful_wire_request_id'],
                'api_request_id':wire['last_successful_api_request_id'],'body_sha256':wire['last_wire_body_sha256'],
                'actor_id':self.reader.delivery.agent_id,'session_id':self.reader.delivery.session_id,'scope_hash':digest(identity)}}
        if self.accepted is not None and self.accepted!=packet:raise ContractError('native_quality_commit_changed')
        self.accepted=packet
        return canonical({'state':'quality_prepared','analysis_sha256':packet['quality_wire']['analysis_sha256'],
                          'external_write':False,'independent_review_required':True})

    def finish(self,actor,original):
        if self.accepted is None:raise ContractError('native_outreach_quality_commit_missing')
        terminal=self.reader.delivery.terminal_binding(actor,self.reader.identity,original)
        terminal['decision_receipt_sha256']=digest(self.accepted)
        return {**self.accepted,'terminal_wire':terminal}


def checked_quality_receipts(result,identity,original):
    """Validate both native child receipts without merging their wire IDs."""
    decision={key:result.get(key) for key in ('identity','analysis','complete_original_read','budget_exhausted',
        'source_delivery','model','effort','provider','quality_wire')}
    quality=result.get('quality_wire',{});delivery=result.get('source_delivery',{});terminal=result.get('terminal_wire',{})
    final=terminal.get('source_delivery',{})
    characters=len(original.decode('utf-8'))
    if (decision['identity']!=identity or digest(original)!=identity['item']['source_hash'] or
        quality.get('analysis_sha256')!=digest(result['analysis'].encode()) or quality.get('source_hash')!=identity['item']['source_hash'] or
        quality.get('scope_hash')!=digest(identity) or type(quality.get('actor_id')) is not int or quality['actor_id']<=0 or
        quality.get('session_id')!='olebackground-'+digest(identity) or
        quality.get('wire_request_id')!=delivery.get('last_successful_wire_request_id') or
        quality.get('api_request_id')!=delivery.get('last_successful_api_request_id') or
        not quality.get('body_sha256') or quality.get('body_sha256')!=delivery.get('last_wire_body_sha256') or
        terminal.get('contract')!='outreach-terminal-wire-v1' or terminal.get('identity')!=identity or
        terminal.get('source_hash')!=identity['item']['source_hash'] or terminal.get('scope_hash')!=digest(identity) or
        terminal.get('actor_id')!=quality['actor_id'] or terminal.get('session_id')!=quality['session_id'] or
        terminal.get('decision_receipt_sha256')!=digest(decision)):
        raise ContractError('native_quality_terminal_receipt_binding_missing')
    for proof in (delivery,final):
        if (proof.get('delivery_complete') is not True or proof.get('last_wire_complete') is not True or
            proof.get('proof')!='successful_serialized_provider_request_and_native_response' or
            proof.get('item_id')!=identity['item']['item_id'] or
            proof.get('source_hash')!=identity['item']['source_hash'] or proof.get('scope_hash')!=digest(identity) or
            type(proof.get('total_characters')) is not int or type(proof.get('confirmed_source_characters')) is not int or
            proof['total_characters']!=characters or proof['confirmed_source_characters']!=characters):
            raise ContractError('native_quality_terminal_original_incomplete')
    if (not terminal.get('api_request_id') or not terminal.get('wire_request_id') or not terminal.get('body_sha256') or
        terminal['api_request_id']!=final.get('last_successful_api_request_id') or
        terminal['wire_request_id']!=final.get('last_successful_wire_request_id') or
        terminal['body_sha256']!=final.get('last_wire_body_sha256')):
        raise ContractError('native_quality_terminal_receipt_binding_missing')
