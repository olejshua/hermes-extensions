"""Native envelope-only recipient probes; never DATA, send, auth, or downgrade.

The legacy VPS daily quota is shared, including attempts with unknown outcomes.
Every protocol response is retained in full, before any ready classification.
"""
import base64
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
from pathlib import Path
import re
import secrets
import smtplib
import socket
import ssl
import subprocess
from zoneinfo import ZoneInfo

from background_jobs.contracts import ContractError, PermanentFailure, digest, canonical
from .sources import persist
from .pool_sources import MAIL

POLICY=re.compile(r'5\.7\.\d|bad reputation|spamhaus|\bblocked\b|client host|listed|spam|\brdns\b|reverse[ -]dns|cannot find (?:your )?hostname|unable to relay|relay access denied|command rejected|protocol error|not permitted',re.I)
RECIPIENT=re.compile(r'5\.1\.[012]|unknown (?:user|recipient|mailbox)|not found|no such|unroutable|unrouteable|mailbox unavailable|address invalid|invalid recipient|no mailbox here by that name|does not exist|^5\.4\.1\s+recipient address rejected: access denied',re.I)


def outbound_ip():
    run=subprocess.run(['curl','-4','--fail','--silent','--show-error','--max-time','8','https://api.ipify.org'],
        capture_output=True,timeout=10,check=True)
    value=run.stdout.decode('ascii').strip()
    if not ipaddress.ip_address(value).is_global:raise ContractError('smtp_public_exit_required')
    return value


def mx_records(domain):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}',domain):raise ContractError('smtp_exact_domain_required')
    run=subprocess.run(['dig','+time=3','+tries=1','+short','MX',domain],capture_output=True,timeout=5,check=True)
    original=run.stdout.decode('ascii');records=[]
    for line in original.splitlines():
        parts=line.split()
        if len(parts)!=2 or not parts[0].isdigit()or not re.fullmatch(r'[A-Za-z0-9.-]+\.',parts[1])or parts[1]=='.':
            raise ContractError('smtp_mx_unknown_or_null')
        records.append((int(parts[0]),parts[1].rstrip('.').lower()))
    if not records:raise ContractError('smtp_mx_unknown_or_null')
    return {'domain':domain,'raw':original,'records':sorted(records)}


def reserve_quota(budget,scope,recipient,attempt_key):
    """The sole effective counter is the existing native SQL ledger.

    Legacy root/now arguments cannot create a parallel file counter.
    """
    from .smtp_budget import NativeSMTPBudget
    if not isinstance(budget,NativeSMTPBudget):raise ContractError('smtp_shared_native_budget_required')
    return budget.reserve(scope,recipient,attempt_key)


def recipient_session(host,recipient,account,active,*,response_sink=None):
    """One connection, one recipient, strict verified STARTTLS when offered."""
    session=None;responses=[]
    def record(command,value):
        code,body=value
        if not isinstance(body,bytes):raise ContractError('smtp_native_response_required')
        responses.append({'command':command,'code':code,'response_base64':base64.b64encode(body).decode(),
                          'text':body.decode('utf-8',errors='replace')})
        if response_sink is not None:response_sink(responses[-1])
        return code
    result={'host':host,'recipient':recipient,'sender':account,'responses':responses,'tls':False,'rcpt_completed':False}
    try:
        active();addresses=socket.getaddrinfo(host,25,type=socket.SOCK_STREAM);active()
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ContractError('smtp_mx_not_public')
        result['resolved_ips']=sorted({a[4][0]for a in addresses})
        session=smtplib.SMTP(timeout=20,local_hostname='primary.example.invalid')
        active();record('CONNECT',session.connect(host,25));active()
        peer=session.sock.getpeername()[0];result['peer_ip']=peer
        if peer not in result['resolved_ips']or not ipaddress.ip_address(peer).is_global:raise ContractError('smtp_peer_changed')
        active();code=record('EHLO',session.ehlo());active()
        if code!=250:return result
        if not session.has_extn('starttls'):
            result['error_type']='VerifiedTLSRequired'
            return result
        active();code=record('STARTTLS',session.starttls(context=ssl.create_default_context()));active()
        if code!=220:return result
        result['tls']=True
        active();code=record('EHLO_TLS',session.ehlo());active()
        if code!=250:return result
        active();code=record('MAIL_FROM',session.mail(account));active()
        if code!=250:return result
        active();record('RCPT_TO',session.rcpt(recipient));active();result['rcpt_completed']=True
        return result
    except (ContractError,InterruptedError):raise
    except Exception as exc:
        result['error_type']=type(exc).__name__
        return result
    finally:
        # Closing a probe needs no authority for a new business operation.
        if session is not None:
            try:record('QUIT',session.quit())
            except Exception:session.close()


def classify(real,control):
    def rcpt(value):
        if value.get('tls')is not True or not value.get('rcpt_completed')or value.get('error_type'):return None
        rows=[r for r in value['responses']if r['command']=='RCPT_TO']
        return rows[0]if len(rows)==1 else None
    a,b=rcpt(real),rcpt(control)
    if not a or not b:return 'UNKNOWN'
    if POLICY.search(a['text'])or POLICY.search(b['text']):return 'POLICY'
    if 400<=a['code']<500 or 400<=b['code']<500:return 'GREY'
    if a['code']==250 and b['code']==250:return 'CATCH_ALL'
    if a['code']==250 and b['code']in (550,551,553)and RECIPIENT.search(b['text']):return 'OK'
    return 'UNKNOWN'


class SMTPOriginals:
    def __init__(self,root,*,native_budget=None,scope_factory=None,ip_reader=outbound_ip,mx_reader=mx_records,session=recipient_session):
        self.root=Path(root);self.budget,self.scope_factory=native_budget,scope_factory
        self.ip_reader,self.mx_reader,self.session=ip_reader,mx_reader,session
    def capture(self,identity,candidate,active):
        if identity['item']['category']!='acribic'or identity['item']['account']!='operator@primary.example.invalid'or not MAIL.fullmatch(candidate['to']):
            raise ContractError('smtp_exact_acribic_recipient_required')
        if self.budget is None or not callable(self.scope_factory):raise ContractError('smtp_shared_native_budget_and_fence_required')
        scope=self.scope_factory(identity,candidate,active);scope.check()
        if scope.account!=identity['item']['account']:raise ContractError('smtp_native_account_changed')
        proof=self.budget._verified()
        quota={'contract':'smtp-shared-native-sql-quota-v2','daily_limit':200,'fingerprint':proof['fingerprint'],
               'database':str(self.budget.lifecycle.db_path),'native_evidence_ref':proof['native_evidence_ref'],
               'native_evidence_sha256':proof['native_evidence_sha256']}
        active();ip=self.ip_reader();active()
        if ip!='45.89.127.191':raise PermanentFailure('smtp_current_exit_not_individually_reviewed')
        started=datetime.now(timezone.utc);target=candidate['to'];domain=target.rsplit('@',1)[1]
        permit=reserve_quota(self.budget,scope,target,'smtp:'+digest([str(scope.lifecycle.db_path),scope.native_id,target]))
        quota['attempt_key']=permit.key
        check=lambda:(active(),permit.check())
        control='hermes-unknown-'+secrets.token_hex(16)+'@'+domain
        trace_ref='native-smtp-traces/'+digest(permit.key.encode())+'.jsonl';trace=self.root/trace_ref
        trace.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        import os
        trace_fd=os.open(trace,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        def sink(side):
            def append(response):
                line=(canonical({'side':side,'response':response})+'\n').encode()
                view=memoryview(line)
                while view:view=view[os.write(trace_fd,view):]
                os.fsync(trace_fd)
            return append
        real=random=None;mx=None;after=None;error=None
        try:
            active();mx=self.mx_reader(domain);active();host=mx['records'][0][1]
            real=self.session(host,target,identity['item']['account'],check,response_sink=sink('real'))
            random=self.session(host,control,identity['item']['account'],check,response_sink=sink('control'))
            active();after=self.ip_reader();active()
        except BaseException as exc:error=exc
        finally:
            os.close(trace_fd)
            status=classify(real,random)if real is not None and random is not None and after==ip else'UNKNOWN'
            packet={'contract':'outreach-smtp-recipient-original-v1','identity':identity,'candidate':candidate,
                'started_at':started.isoformat(),'finished_at':datetime.now(timezone.utc).isoformat(),'public_ip':ip,'public_ip_after':after,
                'mx':mx,'real':real,'control':random,'status':status,'data_command_issued':False,'quota_authority':quota,
                'trace_ref':trace_ref,'trace_sha256':digest(trace.read_bytes()),'error_type':type(error).__name__ if error else None}
            result=persist(self.root,'smtp','smtp:'+target,packet,{'identity':identity,'candidate':candidate},'smtp.ehlo-mail-rcpt-quit')
            self.budget.finish(permit,result[1],result[0]['sha256'])
        if error is not None:raise error
        if status!='OK':raise PermanentFailure('smtp_recipient_not_verified:'+status)
        return result

    def reuse(self,document,identity,candidate,active,*,max_age_seconds=3600):
        """Reuse exact recent recipient evidence, with fresh egress/MX reads.

        No second RCPT pair and no second quota charge. Old, other-recipient,
        other-account, edited or unbound source evidence is never promoted.
        """
        if document.get('kind')!='smtp'or digest(document['content'].encode())!=document.get('sha256'):
            raise ContractError('smtp_original_changed')
        source=json.loads(document['content'])
        packet=source.get('original',{})
        try:finished=datetime.fromisoformat(packet['finished_at'])
        except (ValueError,KeyError,TypeError):raise ContractError('smtp_current_original_required')from None
        now=datetime.now(timezone.utc)
        quota=packet.get('quota_authority',{})
        if (finished.tzinfo is None or not 0<=(now-finished).total_seconds()<=max_age_seconds or
            now.astimezone(ZoneInfo('Europe/Berlin')).date()!=finished.astimezone(ZoneInfo('Europe/Berlin')).date()or
            packet.get('contract')!='outreach-smtp-recipient-original-v1'or packet.get('candidate')!=candidate or
            packet.get('public_ip')!='45.89.127.191'or packet.get('public_ip_after')!=packet['public_ip']or
            packet.get('data_command_issued')is not False or packet.get('status')!='OK'or
            classify(packet.get('real',{}),packet.get('control',{}))!='OK'or
            packet['real'].get('recipient')!=candidate['to']or packet['control'].get('recipient')==candidate['to']or
            packet['control'].get('recipient','').rsplit('@',1)[-1]!=candidate['to'].rsplit('@',1)[-1]or
            not re.fullmatch('hermes-unknown-[0-9a-f]{32}@'+re.escape(candidate['to'].rsplit('@',1)[-1]),packet['control'].get('recipient',''))or
            any(packet[k].get('sender')!=identity['item']['account']for k in ('real','control'))or
            quota.get('contract')!='smtp-shared-native-sql-quota-v2'or quota.get('daily_limit')!=200):
            raise PermanentFailure('smtp_current_original_required')
        if self.budget is None:raise ContractError('smtp_shared_native_budget_required')
        proof=self.budget._verified()
        self.budget.verify_original(quota.get('attempt_key'),document['sha256'],candidate['to'])
        if quota.get('fingerprint')!=proof['fingerprint']or quota.get('database')!=str(self.budget.lifecycle.db_path):
            raise PermanentFailure('smtp_enforcement_changed')
        active();ip=self.ip_reader();active();mx=self.mx_reader(candidate['to'].rsplit('@',1)[-1]);active()
        if ip!=packet['public_ip']or canonical(mx['records'])!=canonical(packet['mx']['records']):
            raise PermanentFailure('smtp_egress_or_mx_changed')
        return persist(self.root,'smtp','smtp-current:'+candidate['to'],
            {'source_document':source,'source_sha256':document['sha256'],'checked_at':now.isoformat(),'public_ip':ip,'mx':mx},
            {'identity':identity,'candidate':candidate},'smtp.exact-current-evidence-reuse')
