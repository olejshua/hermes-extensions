"""Stable native service entry. No -m/PYTHONPATH bootstrap dependency."""
import sys
from pathlib import Path
import json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
policy=json.loads(Path(sys.argv[2]).read_bytes())
fixture='--fixture' in sys.argv[3:]
if not fixture:
    sys.path.insert(0,policy.get('core','/home/hermes/.hermes/hermes-agent'))
    import hermes_bootstrap
sys.path.insert(0,policy.get('brain_package',str(Path(__file__).resolve().parents[1]/'second-brain/src')))
from background_jobs.supervisor import Supervisor
if __name__=='__main__':Supervisor(sys.argv[1],policy,fixture=fixture).run()
