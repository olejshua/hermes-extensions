from pathlib import Path
import hashlib,json,os

def check(root):
 root=Path(root)
 pins=root/'runtime-pins.json'
 if pins.is_symlink() or pins.stat().st_uid!=os.getuid() or pins.stat().st_mode&0o077:raise SystemExit(1)
 manifest=json.loads(pins.read_bytes())
 if manifest.get('contract')!='project-work-worker-deps/v1':raise SystemExit(1)
 for name,expected in manifest['files'].items():
  p=Path(name)
  if any(x.is_symlink() for x in (p,*p.parents)) or not p.is_file():raise SystemExit(1)
  digest=hashlib.sha256()
  with p.open('rb') as f:
   for chunk in iter(lambda:f.read(1048576),b''):digest.update(chunk)
  if digest.hexdigest()!=expected:raise SystemExit(1)
 codex=manifest['codex']
 if str(Path(codex['entry']).resolve(strict=True))!=codex['resolved']:raise SystemExit(1)

if __name__=='__main__':check('/home/hermes/.hermes/ole-project-work')
