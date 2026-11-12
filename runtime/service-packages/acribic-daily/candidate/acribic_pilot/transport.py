"""Exactly one business POST, bypassing httplib2's stale-connection replay.

The installed httplib2 0.32.0 retries BadStatusLine for POST even when the
Google HttpRequest has num_retries=0. Use the native credentials and SDK
request serialization, with a dedicated zero-retry HTTP session for send.
"""
from urllib.parse import urlsplit
from .central import Stop


class OneSendHttp:
    def __init__(self,service,*,before_post=None,session_factory=None,request_factory=None):
        import requests
        from requests.adapters import HTTPAdapter
        from google.auth.transport.requests import Request
        self.credentials=service._http.credentials;self.used=False
        self.before_post=before_post
        self.session=(session_factory or requests.Session)()
        self.session.mount('https://',HTTPAdapter(max_retries=0))
        self.auth_request=(request_factory or Request)(session=self.session)

    def request(self,uri,method='GET',body=None,headers=None,**kwargs):
        import httplib2
        target=urlsplit(uri)
        if (self.used or method!='POST'or target.scheme!='https'or target.hostname!='gmail.googleapis.com'
                or target.path!='/gmail/v1/users/me/messages/send'or target.username or target.password):
            raise Stop('one_native_Gmail_send_POST_only')
        self.used=True
        headers=dict(headers or {})
        try:
            if not callable(self.before_post):raise Stop('bound_slot_gate_required')
            self.credentials.before_request(self.auth_request,method,uri,headers)
            self.before_post()
            response=self.session.request(method,uri,data=body,headers=headers,
                allow_redirects=False,timeout=(10,40))
            return httplib2.Response({**dict(response.headers),'status':str(response.status_code)}),response.content
        finally:self.session.close()
