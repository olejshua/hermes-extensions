"""Source consent and exact original verification; source prose never grants authority."""
from __future__ import annotations
import hashlib, json, os, tempfile, time, platform
from pathlib import Path
import yaml

APPROVED = 'status/bestätigt'
EXPLICIT = ('AGENTS.md', 'PROFIL.md', 'SYSTEME.md')

def status_of(path: Path) -> str:
    # Read frontmatter only. Do not read rejected document bodies during inventory.
    with path.open(encoding='utf-8') as f:
        if f.readline().strip() != '---': return 'unknown'
        lines=[]
        for _ in range(512):
            line=f.readline()
            if not line: return 'unknown'
            if line.strip() == '---': break
            lines.append(line)
        else: return 'unknown'
    try: data=yaml.safe_load(''.join(lines)) or {}
    except yaml.YAMLError: return 'unknown'
    tags=data.get('tags',[]) if isinstance(data,dict) else []
    if isinstance(tags,str): tags=[tags]
    if not isinstance(tags,list): return 'unknown'
    statuses=[t for t in tags if isinstance(t,str) and t.startswith('status/')]
    return APPROVED if statuses == [APPROVED] else 'unknown'

def digest(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp,0o600); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def export(vault: Path, output: Path, explicit: list[Path]) -> dict:
    if not vault.is_dir(): raise FileNotFoundError('Vault unavailable; previous export not changed')
    selected=[]; rejected=0
    # inventory first: all file headers checked before any selected full body read
    for p in sorted(vault.rglob('*.md')):
        if p.is_symlink() or 'Codex' in p.relative_to(vault).parts: continue
        if status_of(p)==APPROVED: selected.append((p,'confirmed_note'))
        else: rejected+=1
    for p in explicit:
        p=p.resolve()
        if not p.is_file(): raise FileNotFoundError(f'explicit authority unavailable: {p}')
        selected.append((p,'explicit_policy' if p.name in ('AGENTS.md','CLAUDE.md') or '/wissen/' in str(p) else 'explicit_reference'))
    records={}; now=time.time()
    for p,role in selected:
        raw=p.read_bytes(); sha=digest(raw); key=digest(str(p).encode())[:32]
        from .vault_context_export import status
        if role=='confirmed_note' and status(p,raw)!='confirmed': raise RuntimeError('source status changed during export')
        if p.read_bytes()!=raw: raise RuntimeError('source changed during export; no receipt published')
        # Secrets in canonical source are blocked, not silently rewritten into a different canonical version.
        from .vault_context_export import SECRET
        if SECRET.search(raw.decode('utf-8')): raise ValueError(f'secret-like material blocked: {p.name}')
        name=key+'.md'; atomic(output/'documents'/name,raw)
        records[key]={'original_path':str(p),'export_path':name,'sha256':sha,'status':'confirmed',
            'role':role,'authority':'source_evidence','mtime_ns':p.stat().st_mtime_ns,'approved_at':now}
    # This is derivative cleanup only. Never touches original files.
    for stale in (output/'documents').glob('*.md'):
        if stale.stem not in records: stale.unlink()
    receipt={'version':1,'source_host':platform.node(),'generated_at':now,'records':records,'rejected_headers':rejected,
       'live_authorities':{'calendar':'Google CODE + Fuxam live','crm':'Twenty live'},'dreaming_enabled':False}
    atomic(output/'receipts.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode())
    return receipt

def verified_record(receipts: Path, key: str, max_age=1800, allow_stale=False, accepted_statuses=("confirmed",), packet=None):
    packet=packet if packet is not None else json.loads(receipts.read_text()); record=packet['records'].get(key)
    age=time.time()-packet['generated_at']
    if packet.get('version')!=1 or (age>max_age and not allow_stale) or age < -60 or not record or record['status'] not in accepted_statuses: return None
    p=receipts.parent/'documents'/record['export_path']
    if not p.is_file() or digest(p.read_bytes())!=record.get('export_sha256',record['sha256']): return None
    # When running on the original host, verify original live too.
    original=Path(record['original_path'])
    if packet.get('source_host')==platform.node() and not original.is_file(): return None
    if original.exists():
        if digest(original.read_bytes())!=record['sha256']: return None
        if record['role']=='confirmed_note' and status_of(original)!=APPROVED: return None
    return {**record, 'last_verified':packet['generated_at'],'age_seconds':max(0,age),'stale':age>max_age}

if __name__=='__main__':
    import argparse
    a=argparse.ArgumentParser(); a.add_argument('--vault',type=Path,required=True);a.add_argument('--output',type=Path,required=True); a.add_argument('--explicit',type=Path,action='append',default=[])
    args=a.parse_args();r=export(args.vault,args.output,args.explicit);print(json.dumps({'approved':len(r['records']),'rejected_headers':r['rejected_headers']}))
