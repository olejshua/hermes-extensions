"""Credential-free request client for the nightly agent's read-only broker."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,os,time,uuid

def frozen_website(folder,arguments):
    if set(arguments)!={'url'}:return None
    root=Path(folder).resolve().parent/'research/web'
    meta=root/(hashlib.sha256(arguments['url'].encode()).hexdigest()+'.json')
    if not meta.is_file()or meta.is_symlink():return None
    value=json.loads(meta.read_text());path=Path(value['path']).resolve()
    if value.get('url')!=arguments['url']or not path.is_relative_to(root.resolve()):raise RuntimeError('frozen_source_identity_changed')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=value['sha256']:raise RuntimeError('frozen_source_content_changed')
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(value['checked_at'])).total_seconds()
    if not 0<=age<12*3600:return None
    # Both judges read the exact captured original. Its original timestamp is
    # preserved; no fresh HTTP check is claimed and no evidence is overwritten.
    return {**value,'text':value['text'][:18000],'full_text_path':str(meta),'frozen_original_reused':True}

def request(command,arguments,*,folder=None,timeout=1200):
    if command=='website':
        frozen=frozen_website(folder or Path.cwd(),arguments)
        if frozen is not None:return frozen
    base=Path(folder or Path.cwd())/'.rpc';base.mkdir(mode=0o700,exist_ok=True)
    key=uuid.uuid4().hex;target=base/(key+'.request.json')
    temporary=base/(key+'.tmp')
    temporary.write_text(json.dumps({'command':command,'arguments':arguments}));temporary.chmod(0o600)
    os.replace(temporary,target)
    result=base/(key+'.response.json');deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if result.exists():
            value=json.loads(result.read_text())
            if value.get('ok')is not True:raise RuntimeError(value.get('error','read_failed'))
            return value['result']
        time.sleep(.5)
    raise TimeoutError('read_broker_timeout')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['pool','screen','ted','website','render','family'])
    p.add_argument('arguments',nargs='?',default='{}');a=p.parse_args()
    print(json.dumps(request(a.command,json.loads(a.arguments)),ensure_ascii=False))
