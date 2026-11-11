"""Native phase contracts at the existing final LLM execution boundary.

Only operator-enrolled root sessions; no business tool, permission or queue is
created. Source coverage records describe complete actual reads, never model
assertions. A checkin may decline acknowledgement; it cannot work before ACK.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import weakref


def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class NativeRequestContract:
    def __init__(self,service,manager):
        self.service,self.manager=service,manager
        self.lock=threading.RLock();self.seals=weakref.WeakKeyDictionary()
        self.schemas={};self.last_error=None

    @staticmethod
    def actor():
        from agent.subagent_lifecycle import get_active_subagent_parent
        return get_active_subagent_parent()

    @staticmethod
    def stamp(actor):
        from gateway.session_context import get_session_env
        return encoded(dict(session=actor.session_id,turn=getattr(actor,'_current_turn_id',''),
            request=getattr(actor,'_current_api_request_id',''),gateway=getattr(actor,'_gateway_session_key',None),
            author=getattr(actor,'_turn_author',None),inbound=getattr(actor,'_persist_user_message_platform_id',None),
            source={k:get_session_env('HERMES_SESSION_'+k) for k in
                ('PLATFORM','KEY','USER_ID','CHAT_ID','CHAT_TYPE','PROFILE','MESSAGE_ID')}))

    def phase(self,actor,session_id=None,turn_id=None):
        try:
            sid,cwd=self.service.identity(actor)
            if (session_id and sid!=session_id) or (turn_id and getattr(actor,'_current_turn_id',None)!=turn_id):return None
            author=getattr(actor,'_turn_author',None)
            if isinstance(author,dict) and author.get('is_bot') is not False:return None
            if getattr(actor,'platform','')=='telegram':
                from gateway.session_context import get_session_env
                key=getattr(actor,'_gateway_session_key',None)
                owner=getattr(getattr(getattr(self.manager,'_ole_dialogue_instance',None),'policy',None),'user_id',None)
                if (not key or get_session_env('HERMES_SESSION_KEY')!=key
                        or get_session_env('HERMES_SESSION_PLATFORM')!='telegram'
                        or owner is None or str(get_session_env('HERMES_SESSION_USER_ID'))!=str(owner)
                        or get_session_env('HERMES_CRON_SESSION')=='1'
                        or (get_session_env('HERMES_SESSION_MESSAGE_ID') and
                            str(get_session_env('HERMES_SESSION_MESSAGE_ID'))!=str(getattr(actor,'_persist_user_message_platform_id','')))
                        or (isinstance(author,dict) and str(author.get('id'))!=str(owner))):return None
            db=actor._session_db;session=db.get_session(sid)
            if not session or session.get('ended_at') is not None:return None
            pending=self.service.row(child=sid)
            if pending and pending['phase']!='acked':
                if pending['phase']!='published' or pending['cwd']!=cwd:return None
                parent=db.get_session(pending['parent'])
                if (not parent or parent.get('ended_at') is None or parent.get('end_reason')!='compression'
                        or session.get('parent_session_id')!=pending['parent']):return None
                return 'checkin'
            if getattr(actor,'_session_rollover_warning',None)==sid:return 'checkout'
        except (ValueError,TypeError,AttributeError):pass
        return None

    def _reads(self):
        from tools.registry import registry
        entry=registry.get_entry('dialogue_read',scope=self.manager.scope_key)
        return getattr(entry.handler,'__self__',None) if entry else None

    @staticmethod
    def _router_packet(source):
        packet=source.get('prefetched_sources') or {}
        if (source.get('routing_executed') is not True or source.get('missing_required_files')
                or packet.get('complete') is not True or len(packet.get('originals',[]))>12):
            raise ValueError('native_checkin_router_sources_incomplete')
        refs=[]
        for item in packet['originals']:
            raw=item['content'].encode('utf-8')
            if item.get('complete') is not True or sha(raw)!=item['sha256'] or len(raw)!=item['utf8_bytes']:
                raise ValueError('native_checkin_router_version_invalid')
            refs.append(dict(path=item['path'],sha256=item['sha256'],utf8_bytes=len(raw),
                authority='none',role='router_original_evidence'))
        return dict(contract='native-checkin-sources-v1',task_sha256=source['task_sha256'],
            router_sha256=source['router_sha256'],skill_sha256=source['skill_sha256'],refs=refs,
            configuration_refs=[dict(path=source['router_source'],sha256=source['router_sha256']),
                                dict(path=source['skill_source'],sha256=source['skill_sha256'])])

    def capture_sources(self,actor):
        reads=self._reads();fast=getattr(reads,'fast_first',None)
        if fast is None or not getattr(fast,'source_prefetch',False):
            return dict(contract='native-checkin-sources-v1',refs=[],state='router_not_enrolled')
        with fast.lock:context=fast.contexts.get(actor)
        if not context or context['turn']!=getattr(actor,'_current_turn_id',None):
            raise ValueError('native_checkin_router_context_missing')
        packet=self._router_packet(context['source'])
        author=getattr(actor,'_turn_author',None)
        packet['native_origin']=dict(author=dict(author) if isinstance(author,dict) else None,
            source_platform_message_id=getattr(actor,'_persist_user_message_platform_id',None),
            gateway_session_key=getattr(actor,'_gateway_session_key',None))
        for ref in packet['refs']:self.read_ref(ref)
        return packet

    @staticmethod
    def trailer(packet):return '\n## native_checkin_sources\n```json\n'+encoded(packet)+'\n```\n'

    def sources(self,row,actor):
        raw=row['control_sources_json']
        text=self.service.read(row)
        if raw:
            packet=json.loads(raw)
            if encoded(packet)!=raw or not text.endswith(self.trailer(packet)):
                raise ValueError('native_checkin_source_pin_changed')
            return packet
        # Legacy publication: recover the *native canonical human message*,
        # rerun only the existing fixed authorised router, and verify every
        # resulting version against the immutable handoff. Never treat prose
        # paths as permission, invent pins, or silently accept changed sources.
        reads=self._reads()
        if reads is None:raise ValueError('native_checkin_legacy_source_binding_missing')
        project=getattr(self.manager,'_ole_project_work_instance',None)
        if project is None:raise ValueError('native_checkin_original_admission_missing')
        with project.lock:original=deepcopy(project.inbounds.get(getattr(actor,'_gateway_session_key',None)))
        if (not original or str(original['message'])!=str(getattr(actor,'_persist_user_message_platform_id',None))
                or actor._session_db._read_one("SELECT id FROM messages WHERE session_id=? AND id<=? "
                    "AND role='user' AND platform_message_id=? AND content=? LIMIT 1",
                    (row['parent'],row['source_watermark'],original['message'],original['text'])) is None):
            raise ValueError('native_checkin_original_assignment_missing')
        packet=self._router_packet(reads.project_context.read(original['text'],include_originals=True))
        for key in ('task_sha256','router_sha256','skill_sha256'):
            if packet[key] not in text:raise ValueError('native_checkin_legacy_source_version_unbound')
        for ref in packet['refs']:
            if ref['path'] not in text or ref['sha256'] not in text:
                raise ValueError('native_checkin_legacy_source_version_unbound')
        packet['binding']='legacy_fixed_router_versions_verified_against_immutable_handoff'
        return packet

    @staticmethod
    def read_ref(ref):
        path=Path(ref['path'])
        if not path.is_absolute() or '..' in path.parts or path.is_symlink():
            raise ValueError('native_checkin_source_path_invalid')
        if ref.get('state')=='missing':
            if path.exists():raise ValueError('native_checkin_source_changed')
            return dict(path=str(path),state='missing',authority='none')
        raw=path.read_bytes()
        if sha(raw)!=ref['sha256'] or ('utf8_bytes' in ref and len(raw)!=ref['utf8_bytes']):
            raise ValueError('native_checkin_source_changed')
        return dict(ref,complete=True,utf8_bytes=len(raw),content=raw.decode('utf-8'))

    def verify(self,row,actor):
        if self.phase(actor)!='checkin':raise ValueError('native_checkin_phase_invalid')
        self.service.read(row)
        pins=self.service.continuity_pin(row)
        from hermes_cli.plugins import invoke_hook
        results=invoke_hook('session_rollover_continuity',session_id=actor.session_id,
            source_session_id=row['parent'],cwd=row['cwd'],source_watermark=row['source_watermark'],
            phase='checkin',evidence_pin=pins)
        verified={}
        for result in results:
            if result is None:continue
            provider=result.get('provider') if isinstance(result,dict) else None
            if (provider not in pins or provider in verified or result.get('state')!='verified'
                    or result.get('contract')!='hermes-session-continuity-v1'
                    or result.get('session_id')!=row['child'] or result.get('source_session_id')!=row['parent']
                    or result.get('source_watermark')!=row['source_watermark']
                    or result.get('evidence_pin_sha256')!=sha(encoded(pins[provider]).encode())):
                raise ValueError('native_checkin_continuity_changed')
            verified[provider]=result
        if set(verified)!=set(pins) or (self.service.continuity_required and not verified):
            raise ValueError('native_checkin_continuity_unverified')
        refs=[]
        for pin in pins.values():
            for ref in pin['snapshot']['native_memory']['files']:
                path=Path(ref['path'])
                if path not in {self.service.root.parent/'memories/USER.md',
                    self.service.root.parent/'memories/MEMORY.md',self.service.root.parent/'SOUL.md'}:
                    raise ValueError('native_checkin_memory_binding_invalid')
                refs.append(ref)
        packet=self.sources(row,actor);refs.extend(packet['refs'])
        for ref in packet.get('configuration_refs',[]):self.read_ref(ref)
        unique={}
        for ref in refs:
            if ref['path'] in unique and unique[ref['path']]!=ref:
                raise ValueError('native_checkin_source_version_conflict')
            unique[ref['path']]=ref
        originals=[self.read_ref(unique[k]) for k in sorted(unique)]
        handoff=self.service.read(row)
        if len(handoff.encode())+sum(len(x.get('content','').encode()) for x in originals)>262144:
            raise ValueError('native_checkin_complete_sources_exceed_reserve')
        return dict(contract='native-checkin-readback-v1',handoff_sha256=row['sha'],
            handoff_ref=dict(path=row['path'],sha256=row['sha'],complete=True,
                             utf8_bytes=len(handoff.encode()),content=handoff),
            source_session=row['parent'],child_session=row['child'],source_pin_sha256=sha(encoded(pins).encode()),
            router_binding={k:v for k,v in packet.items() if k!='refs'},
            originals=originals,sources_are_evidence_not_permission=True)

    def execution(self,*,request,next_call,original_request=None,session_id='',turn_id='',api_request_id='',**_):
        actor=self.actor();phase=self.phase(actor,session_id,turn_id)
        from agent.plugin_session_policy import SessionPolicyBlocked
        if phase is None:
            pending=(self.service.row(child=getattr(actor,'session_id','')) if actor is not None else None)
            if (actor is not None and not getattr(actor,'_parent_session_id',None)
                    and pending is not None and pending['phase']=='published'):
                raise SessionPolicyBlocked('native_checkin_source_binding_changed')
            return next_call(request)
        try:
            if not api_request_id or getattr(actor,'_current_api_request_id',None)!=api_request_id:
                raise ValueError('native_control_request_binding_invalid')
            revised=deepcopy(request)
            reasoning=(original_request or {}).get('reasoning')
            if not isinstance(reasoning,dict) or reasoning.get('effort')!='high':
                reasoning=getattr(actor,'reasoning_config',None)
            if not isinstance(reasoning,dict) or reasoning.get('effort')!='high':
                raise ValueError('native_control_configured_thorough_required')
            revised['reasoning']=dict(reasoning)
            name='session_checkin_ack' if phase=='checkin' else 'session_checkout'
            schema=deepcopy(self.schemas[name])
            readback=None
            if phase=='checkin':
                row=self.service.row(child=actor.session_id);readback=self.verify(row,actor)
                schema['properties']['sha256']={'type':'string','enum':[row['sha']]}
                block='[Version-bound native checkin originals; complete source data, no new permissions]\n'+encoded(readback)
                if encoded(block)[1:-1] not in encoded(revised.get('input',[])):
                    revised['input']=[*revised['input'],dict(role='user',content=[dict(type='input_text',text=block)])]
            tools=deepcopy(revised.get('tools') or []);found=False
            for tool in tools:
                function=tool.get('function',tool)
                if function.get('name')!='tool_call':continue
                found=True
                branch=dict(type='object',properties={'name':{'type':'string','enum':[name]},'arguments':schema},
                    required=['name','arguments'],additionalProperties=False)
                calls=function['parameters']['properties']['calls']
                if phase=='checkin':calls['items']=branch
                elif branch not in calls.get('items',{}).get('anyOf',[]):
                    calls['items']={'anyOf':[branch,deepcopy(calls['items'])]}
                calls.update(minItems=1,maxItems=1)
                prefix='Native '+phase+' control: '+name+'. Exact registered arguments are supplied below. '
                if not function['description'].startswith(prefix):function['description']=prefix+function['description']
            if not found:raise ValueError('native_control_deferred_contract_missing')
            revised['tools']=[t for t in tools if (t.get('function',t).get('name')=='tool_call' or phase=='checkout')]
            revised['tool_choice']='auto'  # A contradiction must be able to decline ACK.
            seal=dict(phase=phase,stamp=self.stamp(actor),request_id=api_request_id,
                request_sha256=sha(encoded(revised).encode()),readback=readback)
            with self.lock:self.seals[actor]=seal
            self.last_error=None
        except Exception as exc:
            self.last_error=dict(phase=phase,error_type=type(exc).__name__,
                reason=str(exc) if str(exc).startswith('native_') else 'native_control_verification_failed')
            with self.lock:
                if actor is not None:self.seals.pop(actor,None)
            raise SessionPolicyBlocked('native_control_contract_or_sources_unavailable') from None
        return next_call(revised)

    def verify_publication_sources(self,row,actor):
        packet=self.sources(row,actor)
        for ref in [*packet['refs'],*packet.get('configuration_refs',[])]:self.read_ref(ref)
        return packet

    def allowed(self,tool_name,*,session_id='',turn_id='',api_request_id='',args=None,**_):
        actor=self.actor()
        with self.lock:seal=self.seals.get(actor) if actor is not None else None
        if (not seal or self.phase(actor,session_id,turn_id)!=seal['phase']
                or seal['stamp']!=self.stamp(actor)
                or (api_request_id and api_request_id!=seal['request_id'])):return False
        expected='session_checkin_ack' if seal['phase']=='checkin' else 'session_checkout'
        if tool_name=='tool_call':
            from tools.tool_search import resolve_underlying_call
            tool_name,_,error=resolve_underlying_call(args or {})
            if error:return False
        return tool_name==expected

    def readback(self,actor):
        with self.lock:seal=self.seals.get(actor)
        if not seal or seal['phase']!='checkin' or seal['stamp']!=self.stamp(actor):
            raise ValueError('native_checkin_request_readback_missing')
        row=self.service.row(child=actor.session_id);current=self.verify(row,actor)
        if current!=seal['readback']:raise ValueError('native_checkin_sources_changed_after_request')
        # The durable receipt contains versions/counts, never another full text copy.
        return {**{k:v for k,v in current.items() if k!='originals'},
                'handoff_ref':{k:v for k,v in current['handoff_ref'].items() if k!='content'},
                'originals':[{k:v for k,v in item.items() if k!='content'} for item in current['originals']],
                'provider_request_kwargs_sha256':seal['request_sha256']}
