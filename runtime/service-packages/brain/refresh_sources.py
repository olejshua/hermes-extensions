#!/usr/bin/env python3
"""Both authorized source sets; deterministic private export, no original modifications."""
import argparse,json,subprocess
from pathlib import Path
from hermes_second_brain.ole_policy import export
from hermes_second_brain.vault_context_export import export_context
VAULT=Path('/path/to/vault')
AUDIT=Path('/path/to/operator/Codex Generell/hermes-audit')
TOKENIZER=AUDIT/'brain-local-model/tokenizer.json'
REMOTE='/home/hermes/hermes-extensions-staging/brain'
def build_exports():
 c=VAULT/'Codex';explicit=[c/n for n in ('AGENTS.md','PROFIL.md','SYSTEME.md')]+[c/'wissen/vault'/n for n in ('vault-nur-auf-ansage-lesen.md','entwuerfe-schickt-ole-selbst.md','versand-absender-code-berlin.md','terminmails-regeln.md','nur-stealth-campus-workspace.md','composio-mehrere-gmail-konten.md','acribic-twenty-operativer-wechsel.md')]
 approved=export(VAULT,AUDIT/'private-brain-export',explicit)
 context=export_context(VAULT,AUDIT/'private-vault-context-export',AUDIT/'private-brain-export/receipts.json',TOKENIZER)
 return {'approved_documents':len(approved['records']),'context_documents':context['source_documents'],'context_chunks':len(context['records']),'excluded_documents':len(context['excluded'])}
def transfer():
 for local,remote in [('private-brain-export','export'),('private-vault-context-export','context-export')]:
  path=AUDIT/local;dest='hermes-host:'+REMOTE+'/'+remote+'/'
  subprocess.run(['rsync','-az','--delete',str(path/'documents')+'/',dest+'documents/'],check=True)
  subprocess.run(['scp',str(path/'receipts.json'),dest+'receipts.next.json'],check=True)
  subprocess.run(['ssh','hermes-host','chmod 600 '+REMOTE+'/'+remote+'/receipts.next.json && mv '+REMOTE+'/'+remote+'/receipts.next.json '+REMOTE+'/'+remote+'/receipts.json'],check=True)
 subprocess.run(['ssh','hermes-host',REMOTE+'/venv/bin/hermes-second-brain sync --manifest '+REMOTE+'/manifest.json'],check=True)
 subprocess.run(['ssh','hermes-host',REMOTE+'/venv/bin/python -m hermes_second_brain.ole_search refresh --manifest '+REMOTE+'/manifest.json --receipts '+REMOTE+'/export/receipts.json --refresh-lexical'],check=True,stdout=subprocess.DEVNULL)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--export-only',action='store_true');a=p.parse_args();result=build_exports()
 if not a.export_only:transfer()
 print(json.dumps(result))
