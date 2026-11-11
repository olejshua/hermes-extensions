"""Private Mac export/transfer; no source writes, unchanged hashes skip remote model ingestion."""
import json,os,subprocess,time,fcntl
from pathlib import Path
PROJECT=Path('/path/to/operator/Dev/hermes-extensions')
AUDIT=Path('/path/to/operator/Codex Generell/hermes-audit')
VAULT=Path('/path/to/vault')
APPROVED=AUDIT/'private-brain-export';CONTEXT=AUDIT/'private-vault-context-export'
REMOTE='/home/hermes/hermes-extensions-staging/brain'
def run():
 state=AUDIT/'private-brain-refresh';state.mkdir(mode=0o700,exist_ok=True);state.chmod(0o700)
 with (state/'lock').open('w') as lock:
  os.chmod(lock.name,0o600)
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  result={'started_at':time.time(),'status':'failed'}
  try:
   subprocess.run([str(PROJECT/'second-brain/.venv/bin/python'),str(PROJECT/'second-brain/refresh_sources.py'),'--export-only'],check=True,capture_output=True,timeout=600)
   approved=json.loads((APPROVED/'receipts.json').read_text());context=json.loads((CONTEXT/'receipts.json').read_text())
   for local,name in ((APPROVED,'export'),(CONTEXT,'context-export')):
    # --delete affects only this dedicated derivative export directory.
    subprocess.run(['/usr/bin/rsync','-a','--delete','--chmod=D700,F600',str(local/'documents')+'/',f'hermes-host:{REMOTE}/{name}/documents/'],check=True,capture_output=True,timeout=600)
   for local,name in ((APPROVED,'export'),(CONTEXT,'context-export')):
    subprocess.run(['/usr/bin/rsync','-a','--chmod=F600',str(local/'receipts.json'),f'hermes-host:{REMOTE}/{name}/receipts.json'],check=True,capture_output=True,timeout=60)
   subprocess.run(['/usr/bin/ssh','hermes-host',f'{REMOTE}/venv/bin/hermes-second-brain sync --manifest {REMOTE}/manifest.json'],check=True,capture_output=True,timeout=3600)
   result.update(status='completed',approved=len(approved['records']),context_chunks=len(context['records']))
  except Exception as e:result['error_type']=type(e).__name__
  finally:
   result['ended_at']=time.time()
   from hermes_second_brain.ole_policy import atomic
   atomic(state/'status.json',json.dumps(result).encode())
if __name__=='__main__':run()
