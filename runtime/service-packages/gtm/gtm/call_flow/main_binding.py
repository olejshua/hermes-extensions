"""Private actual Gateway admission; model/tool arguments cannot supply this proof."""
class MainBinding:
    def __init__(self,user_id='5644072286'):self.user_id=user_id;self.routes={}
    def bound(self,source):
        return not (source is None or getattr(getattr(source,'platform',None),'value',None)!='telegram'
                or getattr(source,'chat_type',None)not in ('dm','private')
                or str(getattr(source,'user_id',''))!=self.user_id or str(getattr(source,'chat_id',''))!=self.user_id
                or getattr(source,'profile',None)not in (None,'') or getattr(source,'is_bot',False)
                or getattr(source,'profile_route_rejected',False))
    def admitted(self,event,gateway,session_key,**_):
        source=getattr(event,'source',None)
        if getattr(event,'internal',False) or not self.bound(source):return
        try:
            if not gateway._is_user_authorized_for_source(source) or gateway._session_key_for_source(source)!=session_key:return
        except Exception:return
        self.routes[session_key]=source
    def verified(self,session_id,actor):
        if not actor or getattr(actor,'session_id',None)!=session_id:return False
        try:
            from gateway.session_context import get_session_env
            key=get_session_env('HERMES_SESSION_KEY','')
            return (key in self.routes and get_session_env('HERMES_SESSION_PLATFORM','')=='telegram'
                    and get_session_env('HERMES_SESSION_CHAT_TYPE','')in ('dm','private')
                    and str(get_session_env('HERMES_SESSION_USER_ID',''))==self.user_id
                    and (get_session_env('HERMES_SESSION_PROFILE','')or None)is None)
        except Exception:return False
