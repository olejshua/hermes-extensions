"""Typed native model failures and source-account-bound quota progress.

No model call, login, credit redemption or business action. Reuse Hermes's
existing account-usage reader with the explicit current subscription credential;
only its safe projection leaves this module. Never persist tokens or raw errors.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

CONTRACT = 'mail-model-status-20261104-v1'
MODELS = {'mail':'gpt-6.1-sol', 'repair':'gpt-6.1-sol'}
CODES = {'mail_model_quota', 'mail_model_auth', 'mail_model_unavailable',
         'mail_model_output_invalid', 'mail_model_runtime_failed'}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def unknown(route, code='availability_unverified'):
    return dict(contract=CONTRACT, route=route, state='unknown', account_fingerprint=None,
                model=MODELS[route], windows=[], code=code, credential_epoch=None,
                refreshable=False, observed_at=time.time())

def normalize(raw, *, account_fingerprint, route, observed_at=None, credential_epoch=None):
    result = unknown(route)
    result['observed_at'] = time.time() if observed_at is None else observed_at
    result['account_fingerprint'] = account_fingerprint
    result['credential_epoch'] = credential_epoch
    if not isinstance(raw, dict) or not account_fingerprint:
        return result
    limits = [('codex', raw.get('rate_limit'))]
    extras = raw.get('additional_rate_limits')
    if extras is not None and not isinstance(extras, list):
        return result
    for index, extra in enumerate(extras or []):
        if not isinstance(extra, dict):
            continue
        scope = extra.get('models') if isinstance(extra.get('models'),list) else []
        scope = [*scope, *(extra.get(key) for key in ('model','model_name','metered_feature','limit_name'))]
        if result['model'] not in scope:
            continue  # Another model's allowance cannot block this route.
        name = str(extra.get('limit_name') or extra.get('metered_feature') or index)
        limits.append((name, extra.get('rate_limit')))
    windows = []; blocked = False; incomplete = False
    for name, limit in limits:
        if not isinstance(limit, dict):
            incomplete = True; continue
        blocked = blocked or limit.get('limit_reached') is True or limit.get('allowed') is False
        for key in ('primary_window', 'secondary_window'):
            window = limit.get(key)
            if window is None:
                continue
            if not isinstance(window, dict):
                incomplete = True; continue
            used = window.get('used_percent')
            if not number(used) or not 0 <= used <= 100:
                incomplete = True; continue
            reset = window.get('reset_at'); duration = window.get('limit_window_seconds')
            windows.append(dict(bucket=name, window=key, used_percent=used,
                                reset_at=reset if number(reset) else None,
                                duration=duration if number(duration) else None))
            blocked = blocked or used >= 100
    result['windows'] = sorted(windows, key=lambda item: (item['bucket'], item['window']))
    if blocked:
        result.update(state='quota', code='mail_model_quota')
    elif windows and not incomplete:
        result.update(state='available', code=None)
    return result

def valid(value, *, route=None):
    if not isinstance(value, dict) or value.get('contract') != CONTRACT:
        return False
    if value.get('route') not in {'mail', 'repair'} or route and value['route'] != route:
        return False
    if value.get('state') not in {'available', 'quota', 'auth', 'unknown'}:
        return False
    if value.get('model') != MODELS[value['route']] or type(value.get('refreshable'))is not bool:
        return False
    account = value.get('account_fingerprint')
    if value.get('credential_epoch') is not None and not isinstance(value['credential_epoch'], (str, int)):
        return False
    if account is not None and (not isinstance(account, str) or len(account) != 64 or any(c not in 'e000000000000007' for c in account)):
        return False
    windows = value.get('windows')
    if not isinstance(windows, list) or len(windows) > 32:
        return False
    for window in windows:
        if not isinstance(window, dict) or set(window) != {'bucket', 'window', 'used_percent', 'reset_at', 'duration'}:
            return False
        if (not isinstance(window['bucket'], str) or len(window['bucket']) > 100
                or window['window'] not in {'primary_window', 'secondary_window'}
                or not number(window['used_percent']) or not 0 <= window['used_percent'] <= 100
                or any(window[key] is not None and not number(window[key]) for key in ['reset_at', 'duration'])):
            return False
    return value['state'] != 'available' or bool(account and windows)

def evidence_key(value):
    if not valid(value):
        return None
    return fingerprint({key: value.get(key) for key in ['contract', 'route', 'model', 'state', 'account_fingerprint', 'credential_epoch', 'windows']})

def advances(previous, current, *, code=None):
    """Time passing or other work spending more quota is not retry evidence."""
    if not valid(current) or current['state'] != 'available':
        return False
    if isinstance(previous,dict)and previous.get('account_fingerprint')and previous['account_fingerprint']!=current['account_fingerprint']:
        return False
    if not previous or not valid(previous):
        return True  # A known provider failure may get one positively verified attempt.
    if code in {'mail_model_auth', 'codex_auth'}:
        return (current.get('credential_epoch') is not None and current['credential_epoch'] != previous.get('credential_epoch'))
    before = {(w['bucket'], w['window']): w for w in previous['windows']}
    after = {(w['bucket'], w['window']): w for w in current['windows']}
    if previous['state'] != 'available' and not before:
        return True
    if set(before) != set(after):
        return False
    return any((old['reset_at'] is not None and after[key]['reset_at'] is not None
                and old['reset_at'] < after[key]['reset_at'])
               or after[key]['used_percent'] < old['used_percent']
               for key, old in before.items())

def refresh_cli_credentials():
    """Ask the installed CLI to refresh its own managed grant, without a turn.

    account/read refreshToken is the installed app-server's normal managed-auth
    flow. No native Hermes grant is passed to it, and no login/import is used.
    The caller must check the same account and actual token revision afterward.
    """
    import os,select,signal,subprocess
    binary=Path.home()/'.local/bin/codex'
    environment={key:os.environ[key]for key in('HOME','PATH','LANG','TZ')if key in os.environ}
    environment['CODEX_HOME']=str(Path.home()/'.codex')
    process=subprocess.Popen([str(binary),'app-server','--listen','stdio://'],env=environment,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        bufsize=0,start_new_session=True)
    deadline=time.monotonic()+30;seen=0;buffer=b''
    def send(value):process.stdin.write((json.dumps(value)+'\n').encode());process.stdin.flush()
    def response(identifier):
        nonlocal seen,buffer
        while time.monotonic()<deadline:
            while b'\n'in buffer:
                line,buffer=buffer.split(b'\n',1)
                value=json.loads(line)
                if value.get('id')==identifier:
                    if 'error'in value:raise ValueError('native_cli_refresh_rejected')
                    return value.get('result')
            if not select.select([process.stdout],[],[],max(0,deadline-time.monotonic()))[0]:break
            chunk=os.read(process.stdout.fileno(),65536);seen+=len(chunk)
            if not chunk or seen>131072:break
            buffer+=chunk
            if len(buffer)>65536:break
        raise TimeoutError('native_cli_refresh_unconfirmed')
    try:
        send({'id':1,'method':'initialize','params':{'clientInfo':{
            'name':'hermes-subscription-status','version':'1'},'capabilities':{'experimentalApi':False}}})
        response(1);send({'method':'initialized'})
        send({'id':2,'method':'account/read','params':{'refreshToken':True}})
        response(2)
    finally:
        if process.stdin:process.stdin.close()
        if process.poll()is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        if process.stdout:process.stdout.close()

def read_native(route='mail', *, expected_account=None, restore=False):
    """Read-only status by default; explicit restoration uses ONE native row.

    An expired access token does not imply a lost grant. The caller may reserve
    one restoration for its actual credential revision, then request restore.
    No CLI import, account selection, login or banked-reset redemption follows.
    """
    result = unknown(route)
    try:
        core = Path('/home/hermes/.hermes/hermes-agent')
        if str(core) not in sys.path:
            sys.path.insert(0, str(core))
        import hermes_bootstrap  # Existing committed dependencies, no installation.
        from hermes_cli.auth import resolve_codex_runtime_credentials, read_credential_pool, _codex_access_token_is_expiring
        from agent.account_usage import _get_json, _codex_headers, _codex_backend_urls
        from agent.codex_headers import codex_account_headers
        def account_of(token):
            return next((v for k,v in codex_account_headers(token).items()if k.casefold()=='chatgpt-account-id'),None)
        def account_key(token):
            account=account_of(token)
            return hashlib.sha256(account.encode()).hexdigest()if account else None
        rows=read_credential_pool('openai-codex')or[]
        try:runtime = resolve_codex_runtime_credentials(read_only=True)
        except Exception:
            # Read the same dormant native row even while the resolver excludes
            # it for an old quota cooldown. Ambiguous identities stay unknown.
            candidates=[row for row in rows if isinstance(row,dict)and row.get('access_token')
                and row.get('last_status')!='dead'
                and (row.get('base_url')or'https://chatgpt.com/backend-api/codex').rstrip('/')=='https://chatgpt.com/backend-api/codex'
                and (not expected_account or account_key(row['access_token'])==expected_account)]
            if len(candidates)!=1:return result
            runtime={'provider':'openai-codex','auth_mode':'chatgpt','base_url':'https://chatgpt.com/backend-api/codex',
                     'api_key':candidates[0]['access_token'],'source':'credential_pool'}
        if (runtime.get('provider') != 'openai-codex' or runtime.get('auth_mode') != 'chatgpt'
                or runtime.get('base_url', '').rstrip('/') != 'https://chatgpt.com/backend-api/codex'):
            return result
        account = account_of(runtime['api_key'])
        if not account:
            return result
        result['account_fingerprint'] = hashlib.sha256(account.encode()).hexdigest()
        if expected_account and expected_account!=result['account_fingerprint']:
            return unknown(route,'subscription_account_mismatch')
        token=runtime['api_key']
        # An irreversible identifier of actual credential bytes. File times,
        # refresh timestamps and unrelated auth-store writes are not evidence.
        credential_epoch = hashlib.sha256(token.encode()).hexdigest()
        matching=[row for row in rows if isinstance(row,dict)and row.get('access_token')==token
                  and row.get('refresh_token')and row.get('last_status')!='dead']
        result['credential_epoch']=credential_epoch
        if route == 'repair':
            cli = json.loads((Path.home() / '.codex/auth.json').read_bytes())
            tokens = cli.get('tokens') or {}
            credential_epoch = hashlib.sha256(tokens.get('access_token','').encode()).hexdigest()
            cli_headers = codex_account_headers(tokens.get('access_token', ''))
            cli_account = next((v for k, v in cli_headers.items() if k.casefold() == 'chatgpt-account-id'), None)
            if cli_account != account or tokens.get('account_id')not in{None,account}:
                result.update(state='auth', code='subscription_account_mismatch')
                return result
            token=tokens['access_token']
            matching=[]
            result['credential_epoch']=credential_epoch
            if _codex_access_token_is_expiring(token,0):
                result.update(state='auth',code='access_token_expired',refreshable=bool(tokens.get('refresh_token')))
                if not restore or not result['refreshable']:return result
                try:refresh_cli_credentials()
                except (OSError,ValueError,TimeoutError):return result
                refreshed=json.loads((Path.home()/'.codex/auth.json').read_bytes()).get('tokens')or{}
                token=refreshed.get('access_token','')
                if account_of(token)!=account or refreshed.get('account_id')not in{None,account}:
                    return unknown(route,'subscription_account_mismatch')
                if _codex_access_token_is_expiring(token,0):return result
                credential_epoch=hashlib.sha256(token.encode()).hexdigest()
                if credential_epoch==result['credential_epoch']:return result
        result['credential_epoch']=credential_epoch
        if route=='mail'and _codex_access_token_is_expiring(token,0):
            result.update(state='auth',code='access_token_expired',refreshable=len(matching)==1)
            if not restore or not result['refreshable']:return result
            from agent.credential_pool import load_pool
            refreshed=load_pool('openai-codex').try_refresh_matching(api_key_hint=token)
            if refreshed is None:return result
            token=refreshed.runtime_api_key
            if account_of(token)!=account:return unknown(route,'subscription_account_mismatch')
            if route=='mail':credential_epoch=hashlib.sha256(token.encode()).hexdigest()
        # Existing native HTTP reader, with explicit credential and NO hidden
        # fetch_account_usage 401 refresh/fallback. Status alone stays read-only.
        raw=_get_json(_codex_backend_urls(runtime['base_url'])[0],_codex_headers(token,None),timeout=15.0)
        return normalize(raw, account_fingerprint=result['account_fingerprint'], route=route,
                         credential_epoch=credential_epoch)
    except Exception as error:
        code = getattr(error, 'code', None)
        if isinstance(code, str) and code.startswith('codex_auth_'):
            result.update(state='auth', code='mail_model_auth')
        elif getattr(getattr(error,'response',None),'status_code',None)==401:
            result.update(state='auth',code='mail_model_auth')
        else:result['code']='status_'+type(error).__name__
        return result

class MailModelFailure(RuntimeError):
    def __init__(self, code, *, availability=None, stage=None):
        self.code = code if code in CODES else 'mail_model_unavailable'
        self.availability = availability if valid(availability, route='mail') else None
        self.stage = stage
        super().__init__(self.code)

def failure_from_stderr(stderr):
    for line in reversed(stderr.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('error') == 'native_mail_model_failed' and value.get('code') in CODES:
            return MailModelFailure(value['code'], availability=value.get('availability'), stage=value.get('stage'))
    return MailModelFailure('mail_model_unavailable')

def safe_failure(error, *, stage):
    availability = read_native('mail')
    if availability['state'] == 'quota':
        code = 'mail_model_quota'
    elif availability['state'] == 'auth':
        code = 'mail_model_auth'
    elif isinstance(error, json.JSONDecodeError) or stage == 'output':
        code = 'mail_model_output_invalid'
    elif stage in {'input', 'credentials', 'setup'}:
        code = 'mail_model_runtime_failed'
    else:
        code = 'mail_model_unavailable'
    return dict(error='native_mail_model_failed', code=code, error_type=type(error).__name__,
                stage=stage, availability=availability)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--route', choices=['mail', 'repair'], default='mail')
    parser.add_argument('--account')
    parser.add_argument('--restore',action='store_true')
    args=parser.parse_args()
    print(json.dumps(read_native(args.route,expected_account=args.account,restore=args.restore), sort_keys=True))
