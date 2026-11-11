"""Scoped original pages: issued callbacks are not provider delivery receipts."""
import json
import threading
import uuid
from .contracts import ContractError,digest

class SourceDelivery:
    def __init__(self,identity,text):
        self.identity=identity;self.text=text;self.scope=digest(identity);self.lock=threading.RLock()
        self.issued={};self.delivered=[];self.requests={};self.confirmed=set()
        self.pending={};self.successful=set();self.actor=None
        self.session_id=None;self.agent_id=None;self.target=('chatgpt.com','/backend-api/codex/responses')
    def bind(self,session_id,agent_id,*,target=None,actor=None):
        with self.lock:
            self.session_id=session_id;self.agent_id=agent_id;self.actor=actor
            if target is not None:self.target=target # Only fixture callers change this; the native worker uses fixed subscription origin.
    def reset(self):
        with self.lock:self.issued.clear();self.delivered.clear();self.requests.clear();self.confirmed.clear();self.pending.clear();self.successful.clear()
    def issue(self,start,end):
        with self.lock:
            if len(self.issued)>=16384:raise ContractError('source_page_receipt_budget_exhausted')
            receipt=uuid.uuid4().hex;self.issued[receipt]=(start,end)
            return {'scope_hash':self.scope,'delivery_id':receipt,'proof':'pending_serialized_provider_delivery'}
    def pre_request(self,**event):
        # This hook only binds an attempt ID, never content/coverage.
        with self.lock:
            key=event.get('api_request_id')
            if self.session_id and event.get('session_id')==self.session_id and isinstance(key,str) and key:
                self.requests[key]=event.get('started_at')
    def provider_response(self,**event):
        try:
            with self.lock:
                if (not self.session_id or event.get('session_id')!=self.session_id or event.get('agent_id')!=self.agent_id
                    or event.get('api_request_id') not in self.requests or event.get('provider')!='openai-codex'
                    or event.get('api_mode')!='codex_responses' or event.get('model')!='gpt-6.1-sol'
                    or event.get('method')!='POST' or (event.get('request_host'),event.get('request_path'))!=self.target
                    or type(event.get('status_code')) is not int or not 200<=event['status_code']<300):return
                wire=event.get('wire_request_id');body=event.get('serialized_body')
                if not isinstance(wire,str) or not wire or wire in self.confirmed or not isinstance(body,bytes) or digest(body)!=event.get('body_sha256'):return
                if len(body)>64_000_000:return # Unknown delivery fails closed; never read/serialize a stream.
                packet=json.loads(body);inputs=packet.get('input')
                if packet.get('model')!='gpt-6.1-sol' or not isinstance(inputs,list):return
                intervals=[]
                for message in inputs:
                    # Codex's actual serialized input format, after middleware/sanitize/prune.
                    if not isinstance(message,dict) or message.get('type')!='function_call_output' or not isinstance(message.get('output'),str):continue
                    try:page=json.loads(message['output'])
                    except (ValueError,TypeError):continue
                    proof=page.get('source_delivery') if isinstance(page,dict) else None
                    if not isinstance(proof,dict) or proof.get('scope_hash')!=self.scope:continue
                    issued=self.issued.get(proof.get('delivery_id'))
                    if issued is None:continue
                    start,end=issued
                    if (page.get('item_id')!=self.identity['item']['item_id'] or page.get('identity')!=self.identity['item']
                        or page.get('offset')!=start or page.get('total_characters')!=len(self.text)
                        or page.get('offset_unit')!='unicode_codepoints' or page.get('truncated') is not False
                        or page.get('next_offset')!=(end if end<len(self.text) else None) or page.get('text')!=self.text[start:end]):continue
                    intervals.append((start,end))
                self.confirmed.add(wire);self.pending[(event['api_request_id'],wire)]=intervals
                self._commit(event['api_request_id'],wire)
        except Exception:return # Callback errors can never turn incomplete delivery into a proof.
    def post_request(self,**event):
        with self.lock:
            request=event.get('api_request_id')
            if (not self.actor or event.get('session_id')!=self.session_id or request not in self.requests
                or self.requests[request]!=event.get('started_at') or getattr(self.actor,'_current_api_request_id',request)!=request
                or event.get('provider')!='openai-codex' or event.get('model')!='gpt-6.1-sol' or event.get('api_mode')!='codex_responses'):return
            wire=getattr(self.actor,'_last_provider_wire_request_id',None)
            if not wire:return
            self.successful.add((request,wire));self._commit(request,wire)
    def _commit(self,request,wire):
        key=(request,wire)
        if key in self.successful and key in self.pending:
            self.delivered.extend(self.pending.pop(key));self.delivered=self._merged(self.delivered)
    @staticmethod
    def _merged(intervals):
        merged=[]
        for start,end in sorted(intervals):
            if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:merged.append((start,end))
        return merged
    def missing(self):
        with self.lock:
            cursor=0;missing=[]
            for start,end in self.delivered:
                if start>cursor:missing.append((cursor,start))
                cursor=max(cursor,end)
            if cursor<len(self.text):missing.append((cursor,len(self.text)))
            return missing
    def complete(self):
        with self.lock:return bool(self.delivered) and not self.missing()
    def status(self):
        with self.lock:
            missing=self.missing()
            return {'item_id':self.identity['item']['item_id'],'source_hash':self.identity['item']['source_hash'],
                'scope_hash':self.scope,'delivery_complete':self.complete(),'proof':'successful_serialized_provider_request_and_native_response',
                'total_characters':len(self.text),'confirmed_source_characters':sum(b-a for a,b in self.delivered),
                'missing_ranges':[{'offset':a,'end_offset':b} for a,b in missing[:64]],'more_missing_ranges':len(missing)>64,
                'recommended_page_characters':4096,'recommended_parallel_pages':4,'ambient_recovery_allowed':False,
                'empty_original_requires_confirmed_read':not self.text and not self.delivered}

def require_complete_delivery(reader):
    if not reader.complete():raise ContractError('source_delivery_incomplete')
