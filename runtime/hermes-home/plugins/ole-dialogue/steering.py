"""Bound native follow-up barrier; the gateway retains ownership of message steering.

No message content is stored here. This veto ends at the pre-tool hook boundary;
it cannot retract a tool that has already passed that boundary.
"""
import threading
from contextlib import nullcontext


class SteeringBarrier:
    def __init__(self, policy):
        self.policy = policy
        self.lock = threading.RLock()
        self.routes = {}

    def observe(self, event, gateway, session_key, *, busy):
        if (getattr(event, 'internal', False) or not self.policy.bound(event.source)
                or not gateway._is_user_authorized_for_source(event.source)):
            return
        peek = getattr(gateway, '_peek_session_state', None)
        state = peek(session_key) if callable(peek) else None
        actor = getattr(getattr(state, 'turn', None), 'agent', None)
        with self.lock:
            self.routes[session_key] = dict(gateway=gateway, source=event.source,
                message_id=str(getattr(event,'message_id','') or ''),
                actor=actor if busy else None,
                request=getattr(actor, '_current_api_request_id', None) if busy else None,
                turn=getattr(actor, '_current_turn_id', None) if busy else None)

    def current_inbound(self,session_key,actor,native_session_id):
        with self.lock:route=self.routes.get(session_key)
        if route is None or actor is None or getattr(actor,'session_id',None)!=native_session_id:
            raise ValueError('native_private_session_required')
        gateway,source=route['gateway'],route['source']
        state=gateway._peek_session_state(session_key)
        stale=(route['actor'] is actor and route['turn']==getattr(actor,'_current_turn_id',None)
               and route['request']==getattr(actor,'_current_api_request_id',None))
        if (getattr(getattr(state,'turn',None),'agent',None) is not actor or not self.policy.bound(source)
                or not gateway._is_user_authorized_for_source(source) or gateway._session_key_for_source(source)!=session_key
                or not route['message_id'] or stale):raise ValueError('native_private_session_required')
        return route['message_id']

    def before_tool(self, *, session_id='', turn_id='', api_request_id='', **_):
        if not session_id or not turn_id or not api_request_id:
            return None
        with self.lock:
            routes = list(self.routes.items())
        for key, route in routes:
            gateway, source = route['gateway'], route['source']
            if not self.policy.bound(source) or not gateway._is_user_authorized_for_source(source):
                continue
            state = gateway._peek_session_state(key)
            actor = getattr(getattr(state, 'turn', None), 'agent', None)
            if (actor is None or getattr(actor, 'session_id', None) != session_id
                    or getattr(actor, '_current_turn_id', None) != turn_id
                    or getattr(actor, '_current_api_request_id', None) != api_request_id):
                continue
            stale = (route['actor'] is actor and route['turn'] == turn_id
                     and route['request'] == api_request_id)
            with getattr(actor, '_pending_steer_lock', None) or nullcontext():
                pending_steer = bool(getattr(actor, '_pending_steer', None))
            adapter = gateway._delivery_adapter_for(source)
            pending = getattr(adapter, '_pending_messages', {}).get(key)
            queued_human = (pending is not None and not getattr(pending, 'internal', False)
                            and self.policy.bound(pending.source))
            if queued_human:
                # Native queue fallback (e.g. correction during agent construction).
                # Stop the stale turn; the gateway alone drains the accepted next turn.
                actor.interrupt()
            if stale or pending_steer or queued_human:
                return {'action': 'block', 'message':
                    'Eine neue Nutzernachricht ist bereits eingegangen. Diesen noch nicht '
                    'gestarteten Werkzeugaufruf überspringen und zuerst die neue Nachricht '
                    'im bestehenden Auftrag berücksichtigen.'}
        return None
