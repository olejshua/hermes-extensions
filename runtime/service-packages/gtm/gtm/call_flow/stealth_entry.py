"""Stable native SDK entry for a model-free Stealth briefing/project lookup."""
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0,'/home/hermes/.hermes/hermes-agent')
import hermes_bootstrap
sys.path.insert(0,'/home/hermes/hermes-extensions-staging/runtime/payload')
sys.path.insert(0,'/home/hermes/hermes-extensions-staging/brain/package/src')
from gtm.call_flow.stealth import Reads,READS,business_mapping


if __name__=='__main__':
    try:
        spec=importlib.util.spec_from_file_location('ole_call_stealth_native',Path('/home/hermes/.hermes/plugins/ole-gtm/runtime.py'))
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        event=json.load(sys.stdin)
        result=business_mapping(event,Reads(lambda:runtime.NativeMCP('notion_2',READS)))
        print(json.dumps(result))
    except Exception:
        print(json.dumps({'error':'stealth_business_read_unavailable'}));sys.exit(1)
