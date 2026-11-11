"""Main-call adapter to reviewed R3 serialized-wire delivery, without Core edits."""
import json
import hashlib
import os
from pathlib import Path


def same_native_lineage(expected,current):
    if expected==current:return True
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
        path=Path(get_hermes_home())/'state.db'
        if not path.is_file():return False
        db=SessionDB(path,read_only=True)
        try:return db.resolve_resume_session_id(expected)==current
        finally:db.close()
    except Exception:return False


class ModelInputProof:
    def __init__(self,runtime):self.runtime=runtime;self.trackers={}
    def issue(self,page,session_id,actor,*,native_context_verified=False):
        # A model cannot provide this binding in tool arguments. Native dispatcher
        # supplies the actual Main agent/session; background worker receipts differ.
        if (not native_context_verified or not actor or not session_id or getattr(actor,'session_id',None)!=session_id
                or session_id.startswith('olebackground-') or getattr(actor,'is_subagent',False)):
            return dict(page,model_source_delivery='blocked_native_main_binding_missing')
        try:from background_jobs.source_delivery import SourceDelivery
        except ImportError:return dict(page,model_source_delivery='blocked_reviewed_r3_capture_dependency')
        identity,binding,text,directory=self.runtime.identity(page['workflow_id'])
        if binding.get('analysis_session_id') and not same_native_lineage(binding['analysis_session_id'],session_id):
            return dict(page,model_source_delivery='blocked_main_session_mismatch')
        key=(page['workflow_id'],session_id)
        tracker=self.trackers.get(key)
        if tracker is None:
            item=dict(item_id=identity.key,source_hash=identity.source_hash,source_id=identity.source_id,version=identity.version)
            tracker=SourceDelivery({'item':item},text);tracker.bind(session_id,id(actor),actor=actor)
            self.trackers[key]=tracker
        if tracker.actor is not actor:return dict(page,model_source_delivery='blocked_main_actor_mismatch')
        start=page['offset'];end=start+len(page['text'])
        return dict(page,item_id=identity.key,identity=tracker.identity['item'],total_characters=len(text),
                    offset_unit='unicode_codepoints',truncated=False,source_delivery=tracker.issue(start,end))
    def status(self,key):
        statuses=[dict(tracker.status(),session_id=session)for (workflow,session),tracker in self.trackers.items()if workflow==key]
        return dict(sessions=statuses,complete=any(s['delivery_complete']for s in statuses),
                    recommended_page_characters=4096,provider_capture_dependency='reviewed R3 post_provider_response',
                    tool_read_is_not_model_delivery=True)
    def before(self,**event):
        for tracker in list(self.trackers.values()):tracker.pre_request(**event)
    def provider(self,**event):
        for tracker in list(self.trackers.values()):tracker.provider_response(**event)
        self._save()
    def after(self,**event):
        for tracker in list(self.trackers.values()):tracker.post_request(**event)
        self._save()
    def error(self,**event):
        # An unsuccessful API attempt creates no native ACK and no receipt.
        pass
    def _save(self):
        for (key,session),tracker in list(self.trackers.items()):
            if not tracker.complete():continue
            identity,binding,text,directory=self.runtime.identity(key)
            receipt=dict(source_hash=identity.source_hash,version=identity.version,session_id=session,
                         main_agent_id=tracker.agent_id,source_delivery=tracker.status(),full_source=True)
            target=directory/('model-input-proof-'+hashlib.sha256(session.encode()).hexdigest()+'.json')
            # Preserve the first completed source-delivery proof; never reinterpret
            # unrelated worker capture as this call's Main receipt.
            if target.exists():continue
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w')as out:json.dump(receipt,out);out.flush();os.fsync(out.fileno())


def verified(directory,identity,session_id=None):
    paths=([Path(directory)/('model-input-proof-'+hashlib.sha256(session_id.encode()).hexdigest()+'.json'),Path(directory)/'model-input-proof.json']
           if session_id else list(Path(directory).glob('model-input-proof*.json')))
    return any(_verified(p,identity,session_id) for p in paths)


def _verified(p,identity,session_id):
    if not p.is_file() or p.is_symlink():return False
    try:value=json.loads(p.read_text())
    except (ValueError,OSError):return False
    proof=value.get('source_delivery',{})
    return (value.get('full_source')is True and value.get('source_hash')==identity.source_hash
            and (session_id is None or value.get('session_id')==session_id)
            and value.get('version')==identity.version and proof.get('delivery_complete')is True
            and proof.get('source_hash')==identity.source_hash
            and proof.get('item_id')==identity.key
            and proof.get('total_characters')==proof.get('confirmed_source_characters')
            and proof.get('proof')=='successful_serialized_provider_request_and_native_response')
