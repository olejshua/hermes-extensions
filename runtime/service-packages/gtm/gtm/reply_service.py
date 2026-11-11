"""Review-gated concrete zero-model CRM consumer; no Gmail/calendar writes."""
from __future__ import annotations
import argparse,configparser,json,os,stat,sys,time
from datetime import datetime
from pathlib import Path
from gtm.coordinator import Coordinator
from gtm.reply import ExistingReplyReadClient,ReplyEnvelopeHook,ReplyQueueConsumer


def read_config(path):
    path=Path(path);info=path.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
        raise ValueError('Reply service config must be privately owned mode 600')
    parser=configparser.ConfigParser(interpolation=None);parser.read(path)
    cfg=parser['reply'];cutoff=datetime.fromisoformat(cfg['activation_cutoff'].replace('Z','+00:00'))
    if cutoff.tzinfo is None:raise ValueError('Explicit activation cutoff required')
    db=Path(cfg['database']).expanduser()
    if not db.is_absolute() or not db.parent.is_dir() or stat.S_IMODE(db.parent.stat().st_mode)!=0o700:
        raise ValueError('Existing private mode 700 GTM state directory required')
    return cfg,cutoff,db


def envelope_hook_from_config(path):
    cfg,cutoff,db=read_config(path)
    coordinator=Coordinator(db);os.chmod(db,0o600)
    return ReplyEnvelopeHook(coordinator,cutoff=cutoff) # only explicit Acribic


def build_consumer(path):
    cfg,_,_=read_config(path)
    envelopes=envelope_hook_from_config(path)
    if not cfg.getboolean('writes_enabled',fallback=False) or cfg.get('parent_review')!='approved':
        return ReplyQueueConsumer(envelopes,None,None,None,writes_authorized=False)
    # Existing helpers and account service load privately only after activation.
    sys.path.insert(0,cfg['existing_scripts_dir'])
    try:import email_watcher as watcher
    finally:sys.path.pop(0)
    client=ExistingReplyReadClient(Path(cfg['existing_helpers_dir']),Path(cfg['existing_twenty_key']))
    from urllib.request import urlopen
    with urlopen('https://crm.example.invalid/client-config',timeout=20) as response:
        version=json.load(response)['appVersion']
    if version!='v2.42.6':raise ValueError('Reviewed Twenty deployment version changed')
    return ReplyQueueConsumer(envelopes,watcher,watcher.service,client,writes_authorized=True,server_version=version)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args();worker=build_consumer(args.config)
    while True:
        result=worker.run_one()
        # Fixed states only: no source IDs, firms, bodies, provider errors/auth.
        print(json.dumps({'state':result['state'],'review_required':result.get('review_required',False)}),flush=True)
        if args.once or result['state']=='disabled':return
        time.sleep(60)

if __name__=='__main__':main()
