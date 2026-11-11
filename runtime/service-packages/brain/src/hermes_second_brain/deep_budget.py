"""Durable physical reservations; token thresholds settle after one bounded in-flight response."""
import fcntl,json,os,time,uuid
from pathlib import Path
from .ole_policy import atomic

def location():return Path(os.getenv('OLE_BRAIN_BUDGET_DIR','/home/hermes/hermes-extensions-staging/brain/state/deep-budget'))
def pause(reason):
 root=location()
 with(root/'budget.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);path=root/'control.json';grant=json.loads(path.read_text());grant.update(paused=True,pause_reason=reason);atomic(path,json.dumps(grant).encode())
def update(event=None,reserve=False,quota_error=False,reservation_id=None):
 root=location();control=root/'control.json'
 if not control.exists():return
 root.mkdir(parents=True,exist_ok=True,mode=0o700)
 with(root/'budget.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);grant=json.loads(control.read_text())
  if not grant.get('enforce'):return
  path=root/'measured.json';stats=json.loads(path.read_text())if path.exists()else{'physical_calls':0,'known_prompt_tokens':0,'known_output_tokens':0,'usage_unknown_calls':0,'subscription_quota_available':False}
  unsettled=stats.setdefault('unsettled_reservations',{})
  settling=bool(event and event.get('api_request_id')in unsettled)
  if reserve and unsettled:
   grant.update(paused=True,pause_reason='unsettled physical reservation; crash recovery requires review');atomic(control,json.dumps(grant).encode());raise RuntimeError(grant['pause_reason'])
  if not grant.get('active_job_id')and not settling:
   if reserve and(grant.get('quota_pause')or grant.get('recovery_required')):raise RuntimeError('Provider recovery required')
   return
  if reserve:
   if unsettled:
    grant.update(paused=True,pause_reason='unsettled physical reservation; crash recovery requires review');atomic(control,json.dumps(grant).encode());raise RuntimeError(grant['pause_reason'])
   if not grant.get('authorized')or grant.get('paused')or stats['physical_calls']>=grant['max_physical_calls']or stats['known_prompt_tokens']>=grant['max_prompt_tokens']or stats['known_output_tokens']>=grant['max_output_tokens']:raise RuntimeError('Native deep physical budget paused')
   reservation_id=reservation_id or str(uuid.uuid4());unsettled[reservation_id]={'job_id':grant['active_job_id'],'reserved_at':time.time()};stats['physical_calls']+=1
  if event:
   limits=event.get('subscription_rate_limits',{})
   if limits:
    stats['subscription_rate_limits']=limits;stats['subscription_quota_available']=any(key.endswith('used-percent')for key in limits)
    if any(value>=100-grant.get('account_margin_percent',10)for key,value in limits.items()if key.endswith('used-percent')):grant.update(paused=True,pause_reason='observed subscription account margin reached')
   identity=event.get('api_request_id');unsettled.pop(identity,None);usage=event.get('usage')
   if usage and usage.get('prompt_tokens_available')and usage.get('output_available'):
    stats['known_prompt_tokens']+=usage['prompt_tokens'];stats['known_output_tokens']+=usage['output_tokens']
   else:stats['usage_unknown_calls']+=1;grant.update(paused=True,pause_reason='physical usage unavailable; review budget')
  if stats['physical_calls']>=grant['max_physical_calls']or stats['known_prompt_tokens']>=grant['max_prompt_tokens']or stats['known_output_tokens']>=grant['max_output_tokens']:grant.update(paused=True,pause_reason='measured deep threshold reached')
  if quota_error:grant.update(paused=True,quota_pause=True,pause_reason='provider rate/quota rejection')
  stats['updated_at']=time.time();atomic(path,json.dumps(stats).encode());atomic(control,json.dumps(grant).encode());return reservation_id

def set_active(job_id):
 root=location();control=root/'control.json'
 if not control.exists():raise RuntimeError('Deep physical budget control unavailable')
 with(root/'budget.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);grant=json.loads(control.read_text());grant['active_job_id']=job_id;atomic(control,json.dumps(grant).encode())

def finish(event,quota_error,emit):
 # Accounting survives full spool and telemetry I/O failure. Never retry a provider unmetered.
 try:update(event=event,quota_error=quota_error)
 finally:
  try:emit(event)
  except Exception:
   if(location()/'control.json').exists():pause('metadata spool unavailable; review accounting')
   raise

def require_recovery(reason):
 root=location()
 with(root/'budget.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);path=root/'control.json';grant=json.loads(path.read_text());grant.update(paused=True,recovery_required=True,pause_reason=reason);atomic(path,json.dumps(grant).encode())
