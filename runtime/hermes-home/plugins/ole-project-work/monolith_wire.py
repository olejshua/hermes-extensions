"""Bounded REST transport using only the existing native Monolith connection."""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .monolith_context import native_client

LIMIT=18*1024*1024
ID=r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
PATH=re.compile(r'^/agent/assignments(?:/(?:propose|claim|ready)|/requests/'+ID+r'|/'+ID+r'(?:/(?:claims|reports)/'+ID+r'|/report|/results/'+ID+r'/files/'+ID+r')?)$')


class Unavailable(RuntimeError):
    def __init__(self,status=None):
        super().__init__('monolith_http_unavailable' if status is None else 'monolith_http_'+str(status))
        self.status=status


class Wire:
    def __init__(self,endpoint,headers,*,allow_loopback=False):
        u=urlsplit(endpoint)
        if u.username or u.password or u.query or u.fragment or not u.hostname:
            raise ValueError('monolith_endpoint_invalid')
        if u.scheme!='https' and not (allow_loopback and u.scheme=='http' and u.hostname in ('127.0.0.1','localhost')):
            raise ValueError('monolith_endpoint_invalid')
        self.endpoint=endpoint.rstrip('/');self.headers=dict(headers)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(NoRedirect())
    @classmethod
    def native(cls,ctx):
        existing=native_client(ctx)
        if not existing.endpoint.endswith('/mcp'):raise ValueError('monolith_endpoint_invalid')
        return cls(existing.endpoint[:-4],existing.headers)
    def call(self,path,body=None):
        if not PATH.fullmatch(path):raise ValueError('monolith_link_route_invalid')
        if path=='/agent/assignments/ready' and body is not None:raise ValueError('monolith_read_only_route_required')
        data=None if body is None else json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if data is not None and len(data)>LIMIT:raise ValueError('monolith_report_transport_too_large')
        request=urllib.request.Request(self.endpoint+path,data=data,method='GET' if data is None else 'POST',
            headers={**self.headers,'Content-Type':'application/json','Accept':'application/json'})
        try:
            with self.opener.open(request,timeout=20) as response:raw=response.read(LIMIT+1)
            if len(raw)>LIMIT:raise Unavailable()
            value=json.loads(raw)
        except urllib.error.HTTPError as e:
            code=e.code;e.close();raise Unavailable(code) from None
        except (OSError,ValueError):raise Unavailable() from None
        if not isinstance(value,dict) or value.get('success') is not True or 'data' not in value:raise Unavailable()
        return value['data']
