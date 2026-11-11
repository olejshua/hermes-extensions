"""Explicit operator pause/resume of existing deep lane; no budget reset or provider call."""
import argparse,fcntl,json
from pathlib import Path
from .ole_policy import atomic

def control(root,pause):
 path=root/'control.json'
 with(root/'budget.lock').open('a')as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);value=json.loads(path.read_text())
  measured=json.loads((root/'measured.json').read_text())if(root/'measured.json').exists()else{}
  if not pause:
   if measured.get('unsettled_reservations'):raise RuntimeError('Unsettled physical dispatches need measured recovery before resume')
   if value.get('recovery_required'):
    raise RuntimeError('Recover the bound job with source/version/L2/recall proof before resume')
   if value.get('active_job_id'):raise RuntimeError('Active native job still bound; recovery required before resume')
   if not value.get('authorized'):raise RuntimeError('Existing budget is not authorized')
   if any(measured.get(field,0)>=value[cap]for field,cap in [('physical_calls','max_physical_calls'),('known_prompt_tokens','max_prompt_tokens'),('known_output_tokens','max_output_tokens')]):raise RuntimeError('Measured budget exhausted; resume does not extend it')
  value['paused']=pause;
  if not pause:value['quota_pause']=False
  value['pause_reason']='operator request'if pause else None;atomic(path,json.dumps(value).encode());return {'paused':pause,'budget_extended':False,'canonical_sources_changed':False}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('/home/hermes/hermes-extensions-staging/brain/state/deep-budget'));group=p.add_mutually_exclusive_group(required=True);group.add_argument('--pause',action='store_true');group.add_argument('--resume',action='store_true');a=p.parse_args();print(json.dumps(control(a.root,a.pause)))
