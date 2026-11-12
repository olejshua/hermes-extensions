#!/usr/bin/env python3
"""Private import/source-edit/export entry. No Google/CRM client is imported."""
import base64
import csv
import io
import json
import os
from pathlib import Path
import sys

BUNDLE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BUNDLE/'source'),str(BUNDLE/'deps'),str(BUNDLE/'deps/second-brain/src')]
from background_jobs.contracts import ContractError,canonical,digest,private_file,approved_path
from hermes_second_brain.lifecycle import LifecycleStore
from outreach_night.central_sources import CanonicalSources,ACCOUNTS,project_evidence


def authority(config):
    value=json.loads(Path(config).read_bytes())
    if set(value)!={'native_db','root','host_id','account_lock_root','native_ledgers'}:
        raise ContractError('central_source_host_configuration_required')
    db=Path(value.pop('native_db'))
    if not db.is_absolute()or db.is_symlink():raise ContractError('central_source_native_database_invalid')
    return CanonicalSources(LifecycleStore(db),**value)


def source_view(a,project,account,*,icp=False,own_identity=None):
    from outreach_night.protections import legacy_evidence
    snap=a.snapshot(project,account,own_identity=own_identity)
    rows=snap['legacy_projection']+snap['events']+snap['native_history']
    if icp:rows=[r for r in rows if r['state']!='icp_reserved']
    from outreach_night.protections import first_contact_exclusions
    blocked={k:sorted(v)for k,v in first_contact_exclusions(project_evidence(rows)).items()}
    # Compact exported view for old deterministic readers. Raw originals are
    # separate and immutable. Neither the view nor a CSV row grants a Send.
    suppression='## Kanonischer Quellenexport\n'+''.join('- `'+d+'`\n'for d in blocked['domains'])
    addendum='E-Mail;Stage;Datum;Notiz\n'
    if snap['legacy_original']:addendum=snap['legacy_addendum']
    output=io.StringIO();output.write(addendum.rstrip('\n')+'\n');writer=csv.writer(output,delimiter=';',lineterminator='\n')
    # A broad historical Sent observation already protects first contact, but
    # it is not the explicit CSV row written only after a CRM completion.
    completed=io.StringIO();completed.write(addendum.rstrip('\n')+'\n')
    completion_writer=csv.writer(completed,delimiter=';',lineterminator='\n')
    for row in snap['events']:
        state=row['state']
        if state not in {'sent_verified','bounce','suppress','address_review'}:continue
        if row.get('legacy_csv_row'):
            writer.writerow(row['legacy_csv_row']);completion_writer.writerow(row['legacy_csv_row']);continue
        for email in row['mailboxes']:
            writer.writerow([email,{'sent_verified':'E-Mail raus','bounce':'Adresse pruefen','address_review':'Adresse pruefen','suppress':'Kein Interesse'}[state],
                             '',state+';Beleg '+row['operation_id']])
    return {'version':{k:snap[k]for k in ('project','account','revision','entry_sha256','legacy_complete')},
            'blocked':blocked,'suppression_text':suppression,'addendum_csv':output.getvalue(),
            'completion_addendum_csv':completed.getvalue(),
            'explicit_blocked':project_evidence([row for row in rows if row['state']in {'suppress','address_review','bounce','release'}]),
            'history_text':snap['legacy_history_text']+'\n'+'\n'.join(r['history_text']for r in snap['events']if 'history_text'in r)}


def execute(a,request):
    op=request['operation'];project=request.get('project','acribic');account=ACCOUNTS[project]
    if request.get('account',account)!=account:raise ContractError('central_source_project_account_mismatch')
    if op=='import':
        a.initialize();packet=json.loads(base64.b64decode(request['raw_base64'],validate=True))
        return a.import_legacy(packet,operation_id=request['operation_id'],expected_revision=request['expected_revision'])
    if op=='namespace':
        a.initialize();return a.initialize_empty_project(project,account,operation_id=request['operation_id'],original=canonical(request).encode())
    if op=='import-stealth':
        if project!='stealth':raise ContractError('central_source_project_account_mismatch')
        a.initialize();packet=json.loads(base64.b64decode(request['raw_base64'],validate=True))
        return a.import_named_stealth(packet,operation_id=request['operation_id'],expected_revision=request['expected_revision'])
    if op=='current':return a.current(project,account)
    if op=='document-read':
        from legacy_source_documents import read
        return read(a,project,account,request['document'])
    if op=='document-write':
        from legacy_source_documents import write
        return write(a,project,account,request)
    if op=='document-read-operation':
        from legacy_source_documents import operation
        return operation(a,project,account,request)
    if op in {'view','icp-view'}:return source_view(a,project,account,icp=op=='icp-view')
    if op=='read-operation':return a.read_operation(project,account,request['operation_id'])
    if op=='change':
        return a.apply(project,account,request['change'],writer_id=request['writer_id'],operation_id=request['operation_id'],
            expected_revision=request['expected_revision'],original=base64.b64decode(request['raw_base64'],validate=True))
    if op=='export':
        target=Path(request['target'])
        # New export directory only. Never overwrite historical Mac originals.
        target.mkdir(parents=True,exist_ok=False);os.chmod(target,0o700)
        snap=a.snapshot(project,account,verify_originals=True);view=source_view(a,project,account)
        private_file(target/'CURRENT-EXPORT.md',('# Serverexport - keine lokale Hauptquelle\n\n'
            +'Version '+str(snap['revision'])+' / '+snap['entry_sha256']+'\n\n'+view['suppression_text']).encode())
        private_file(target/'Nachtrag-EXPORT.csv',view['addendum_csv'].encode())
        inventory=[]
        if snap['legacy_original']:
            packet=json.loads(approved_path(a.root,snap['legacy_original']['ref']).read_bytes())
            for entry in packet['files']:
                raw=base64.b64decode(entry['raw_base64'],validate=True)
                ref='historical-originals/'+entry['sha256']+'.raw'
                path=target/ref
                try:private_file(path,raw)
                except FileExistsError:
                    if path.read_bytes()!=raw:raise ContractError('central_export_original_collision')
                inventory.append({'source_path':entry['path'],'ref':ref,'sha256':entry['sha256'],'bytes':len(raw)})
        private_file(target/'provenance.json',canonical({'source_version':view['version'],'originals':inventory,
            'notice':'Export only. Source changes use the authenticated server CLI.'}).encode())
        return {'target':str(target),'source_version':view['version'],'originals':len(inventory)}
    raise ContractError('central_source_operation_not_supported')


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--config':raise SystemExit('Use --config <host-pinned-config> and one JSON request on stdin')
    try:
        a=authority(sys.argv[2]);request=json.load(sys.stdin);result=execute(a,request)
        print(canonical({'state':'verified','result':result}))
    except Exception as exc:
        # Source material may contain customer text. Error reports contain only
        # a stable code; the operation ID supports a separate owner readback.
        code=str(exc)if isinstance(exc,ContractError)else type(exc).__name__
        print(canonical({'state':'blocked','error':code}));raise SystemExit(2)


if __name__=='__main__':main()
