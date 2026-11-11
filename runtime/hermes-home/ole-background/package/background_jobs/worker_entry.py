"""Absolute entry usable under native -I re-exec or explicit private fixtures."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,os,signal
root,policy_path,job_id,item_id=sys.argv[1:5]
fixture='--fixture' in sys.argv[5:]
policy=json.loads(Path(policy_path).read_bytes())
if not fixture:
    sys.path.insert(0,policy.get('core','/home/hermes/.hermes/hermes-agent'))
    import hermes_bootstrap
sys.path.insert(0,policy.get('brain_package',str(Path(__file__).resolve().parents[1]/'second-brain/src')))
from background_jobs.contracts import checked_policy,private_file,canonical,digest
checked_policy(policy,fixture=fixture)
from background_jobs.store import Store
from background_jobs.engine import PhaseEngine
from background_jobs.adapters import factory
# Exact worker is session/group leader. Parent-death must clean descendants too.
def terminate(*_):
    if os.getpgid(0)==os.getpid():os.killpg(os.getpid(),signal.SIGKILL)
    raise SystemExit(77)
signal.signal(signal.SIGTERM,terminate)
store=Store(root)
slot_fd=int(next(v.split('=',1)[1] for v in sys.argv[5:] if v.startswith('--slot-fd=')))
slot_index=int(next(v.split('=',1)[1] for v in sys.argv[5:] if v.startswith('--slot-index=')))
owned=os.fstat(slot_fd);expected=(Path(policy['slot_root'])/('slot-'+str(slot_index)+'.lock')).stat()
if (owned.st_dev,owned.st_ino)!=(expected.st_dev,expected.st_ino):raise RuntimeError('owned_shared_slot_fd_missing_after_bootstrap')
proof={'isolated':sys.flags.isolated==1,'fd_inherited':True,'slot_index':slot_index,'native_bootstrap':'hermes_bootstrap' in sys.modules,'python_version':sys.version.split()[0]}
try:private_file(store.root/'bootstrap-proofs'/(digest([job_id,item_id])+'.json'),canonical(proof).encode())
except FileExistsError:pass
engine=PhaseEngine(store,factory(store,policy),lease_seconds=policy.get('lease_seconds',30))
engine.run_item(job_id,item_id)
