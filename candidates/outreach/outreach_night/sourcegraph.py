"""Complete selection/protection originals are code inputs, not LLM megablobs.

Each candidate retains its full relevant CRM, company, TED, SMTP and mail
originals. Selection itself is deterministic over the complete shared original;
the immutable bundle binds those complete files transitively and rechecks them.
"""
import json
from background_jobs.contracts import ContractError,approved_path,digest


def checked_dependencies(root,original):
    bundle=json.loads(original)
    entries=bundle.get('external_originals',[])
    if not isinstance(entries,list):raise ContractError('outreach_external_originals_invalid')
    seen=set()
    for row in entries:
        if (not isinstance(row,dict)or set(row)!={'ref','sha256'}or row['ref']in seen or
            not isinstance(row['sha256'],str)or len(row['sha256'])!=64):
            raise ContractError('outreach_external_originals_invalid')
        raw=approved_path(root,row['ref']).read_bytes()
        if digest(raw)!=row['sha256']:raise ContractError('outreach_external_original_changed')
        seen.add(row['ref'])
    return entries
