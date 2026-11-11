"""Uses the gateway's existing per-profile Monolith credential resolution. Never logs bodies."""
import asyncio
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class TransportError(RuntimeError):
    def __init__(self, status=None):
        super().__init__('monolith_transport_unavailable' if status is None else 'monolith_request_rejected')
        self.status = status


class Backend:
    def __init__(self, endpoint, headers, *, allow_loopback=False):
        url = urlsplit(endpoint)
        local = allow_loopback and url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
        if not (local or url.scheme == 'https') or url.username or url.password or url.query or url.fragment:
            raise ValueError('monolith_endpoint_invalid')
        self.endpoint = endpoint.rstrip('/')
        self.headers = dict(headers)

    @classmethod
    def native(cls):
        from tools.mcp_tool_config import _load_mcp_config
        server = _load_mcp_config().get('monolith') or {}
        endpoint = str(server.get('url') or '')
        if server.get('enabled') is False or not endpoint.endswith('/mcp'):
            raise ValueError('monolith_native_configuration_missing')
        configured_headers = server.get('headers') or {}
        if not isinstance(configured_headers, dict):
            raise ValueError('monolith_native_credential_unresolved')
        headers = {str(k).lower(): str(v) for k, v in configured_headers.items()}
        if not headers.get('x-monolith-agent-token') or any('${' in v for v in headers.values()):
            raise ValueError('monolith_native_credential_unresolved')
        return cls(endpoint[:-4], headers)

    def _request(self, path, body=None):
        headers = {**self.headers, 'Content-Type': 'application/json', 'Accept': 'application/json'}
        request = urllib.request.Request(self.endpoint + path, headers=headers,
                                         data=None if body is None else json.dumps(body).encode(),
                                         method='GET' if body is None else 'POST')
        # Refuse redirects: credentials belong to this exact existing origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise TransportError(error.code) from None
        except (OSError, ValueError):
            raise TransportError() from None
        if not isinstance(result, dict) or result.get('success') is not True:
            raise TransportError()
        return result.get('data')

    async def call(self, path, body=None):
        return await asyncio.to_thread(self._request, path, body)
