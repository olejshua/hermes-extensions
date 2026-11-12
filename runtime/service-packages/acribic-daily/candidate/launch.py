#!/usr/bin/env python3
"""One dated native entry. No model, no installation, no schedule creation."""
import argparse
from contextlib import redirect_stdout
from pathlib import Path
import json
import os
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from acribic_pilot.central import Central,Stop,atomic,fingerprint


def guard(root):
    central=Central(root/'central')
    with central.lock():
        activation=json.loads((root/'activation.json').read_text())
        if activation.get('enabled')is not False:raise Stop('pilot_still_active')
        central.assert_clear()
        if not(root/'runs/acribic-20261102-N10-0701-0719/result.json').exists():raise Stop('reviewed_end_state_missing')
        result=json.loads((root/'runs/acribic-20261102-N10-0701-0719/result.json').read_text())
        if result['send_uncertain']or result['crm_open']:raise Stop('pilot_end_unresolved')
        return {'contract':'acribic-mac-send-permission/v1','run_id':'acribic-20261102-N10-0701-0719','mac_send_allowed':True}


def create(root):
    from acribic_pilot.pilot import Pilot,validate_selection,N
    mails=json.loads((root/'selected.json').read_text());validate_selection(mails,N)
    plan=json.loads((root/'run-plan.json').read_text())
    if plan['central_root']!='/home/hermes/.hermes/ole-acribic-daily/central':raise Stop('canonical_server_account_required')
    central_root=Path(plan['central_root'])
    state_root=central_root.parent
    import hashlib
    review=json.loads((root/'semantic-review.json').read_text())
    if review.get('approved')is not True or any(hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected
            for name,expected in review['source_hashes'].items()):raise Stop('independent_content_review_required')
    if not review.get('evidence_files')or any(hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected
            for name,expected in review['evidence_files'].items()):raise Stop('reviewed_primary_evidence_required')
    os.environ['ACRIBIC_CRM_BACKEND']='twenty'
    os.environ['ACRIBIC_VERSAND_STATE']=str(root/'state')
    os.environ['TZ']='Europe/Berlin'
    import time
    time.tzset()
    vendor=root/'vendor'
    sys.path[:0]=['/home/hermes/acribic-smtp',str(vendor),'/home/hermes/.hermes/scripts',str(root)]
    # Legacy sender modules prepend their own directory. Load the existing
    # native SMTP helper first so those imports cannot select the Mac copy.
    import smtp_pruefen
    if Path(smtp_pruefen.__file__).resolve()!=Path('/home/hermes/acribic-smtp/smtp_pruefen.py').resolve():
        raise Stop('existing_native_SMTP_helper_required')
    original=sys.argv;sys.argv=[original[0],'--liste',mails[0]['liste']]
    try:
        import versand as v
    finally:sys.argv=original
    import versand_senden_sicher as sender,versand_abschluss_sicher as closure
    import email_watcher as watcher,ted_original_reader
    from acribic_pilot.gmail import Gmail
    from acribic_pilot.evidence import Evidence
    from acribic_pilot.delivery import check
    state=root/'state';state.mkdir(mode=0o700,parents=True,exist_ok=True)
    v.SPERR=str(state_root/'protections/Nicht-kontaktieren.md');v.NACHTRAG=str(state_root/'protections/Nachtrag-seit-Export.csv')
    if not Path(v.SPERR).is_file()or not Path(v.NACHTRAG).is_file():raise Stop('authoritative_local_protection_files_missing')
    v.INDEX=str(state/f'CRM-Index-{v.LISTE}.csv');v.SENDER_LOCK=str(state/'completion.lock')
    v.ATTEMPT_REGISTRY=str(central_root/'actions.jsonl')
    gmail=Gmail(lambda:watcher.service('code'),allow_write=False)
    evidence=Evidence(root,ted_original_reader,v,smtp_pruefen)
    evidence.validate_all(mails)
    sources={f'vendor/{path.name}':path for path in vendor.glob('*.py')}
    sources.update({f'candidate/{path.relative_to(HERE)}':path for path in HERE.rglob('*.py')})
    sources.update({'native-gmail-reader':Path(watcher.__file__),'native-smtp':Path(smtp_pruefen.__file__),
                    'ted-original-reader':Path(ted_original_reader.__file__),
                    'firm-evidence':root/'firm-evidence.json','positive-control':root/'positive-control.json',
                    'legacy-protection-manifest':root/'legacy-protection-manifest.json'})
    sources.update({'daily-run-plan':root/'run-plan.json','semantic-review':root/'semantic-review.json'})
    sources.update({'reviewed-evidence/'+name:root/name for name in review['evidence_files']})
    for path in HERE.parent.glob('*.py'):sources['daily/'+path.name]=path
    import requests.sessions,requests.adapters,urllib3.connectionpool,urllib3.util.retry
    import googleapiclient.http,google.auth.transport.requests
    for label,module in [('requests-session',requests.sessions),('requests-adapter',requests.adapters),
            ('urllib3-pool',urllib3.connectionpool),('urllib3-retry',urllib3.util.retry),
            ('google-SDK-http',googleapiclient.http),('google-auth-http',google.auth.transport.requests)]:
        sources['transport-library/'+label]=Path(module.__file__)
    def family_gate(mail,snapshot):
        row=evidence.family(mail,snapshot)
        for event in Central(central_root).events():
            if event['kind']=='legacy_snapshot':
                if set(row['family_domains'])&set(event['payload']['org_domains']):raise Stop('family_legacy_reservation')
            elif event.get('org_domain')in row['family_domains']:
                raise Stop('family_prior_central_attempt')
        queries=[f'in:anywhere (to:{d} OR from:{d})'for d in row['family_domains']if d!=mail['org_domain']]
        queries+=['in:anywhere "'+name+'"'for name in row['family_aliases']if name!=mail['firma']]
        for response in sender.fetch_queries(gmail,queries):
            if response.get('messages')or response.get('nextPageToken')or response.get('resultSizeEstimate',0):raise Stop('family_Gmail_history')
    def permission():
        if (state_root/'paused.json').exists():raise Stop('campaign_explicitly_paused')
        c=json.loads((state_root/'campaign.json').read_text())
        if (c.get('enabled')is not True or c.get('id')!=plan['campaign_id'] or c.get('expected')!=10
                or c.get('mode')!='send' or c.get('sole_send_host')!='vps' or c.get('first_time')!='07:03'
                or c.get('interval_seconds')!=120 or c.get('timezone')!='Europe/Berlin'):
            raise Stop('daily_campaign_paused_or_changed')
        if plan['date']<c['start_date']:raise Stop('before_campaign_start')
    pilot=Pilot(root,v,sender,closure,gmail,ted_gate=evidence.ted,smtp_probe=evidence.probe,
                family_gate=family_gate,source_files=sources,bounce_check=lambda events:check(gmail,events),
                date=plan['date'],run_id=plan['run_id'],central_root=central_root,
                preflight_lead=40,max_lateness=30,permission_check=permission)
    return pilot


def seed(root):
    manifest=json.loads((root/'legacy-protection-manifest.json').read_text())
    if manifest.get('schema')!='acribic-legacy-protection/v1'or manifest.get('complete')is not True:
        raise Stop('complete_legacy_inventory_required')
    import hashlib
    mailboxes=set();source_ids=set()
    for entry in manifest['journals']:
        path=Path(entry['copy_path']);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise Stop('legacy_journal_changed')
        for line in raw.decode().splitlines():
            if not line:raise Stop('empty_legacy_journal_record')
            row=json.loads(line)
            if entry.get('format','mac_registry_v1')=='mac_registry_v1':
                if row.get('event')not in {'attempt','draft_attempt'}:raise Stop('legacy_partition_required')
            elif entry['format']=='CODE_server_draft_manifest':
                if entry.get('account')!='operator@primary.example.invalid'or not row.get('draft_id'):
                    raise Stop('explicit_CODE_draft_partition_required')
            else:raise Stop('unknown_legacy_journal_format')
            address=row['email'].strip().casefold()
            if address.count('@')!=1 or any(c.isspace()for c in address):raise Stop('invalid_legacy_mailbox')
            mailboxes.add(address)
            for field in ('page_id','notion_id'):
                if row.get(field):source_ids.add(row[field].replace('-','').casefold())
    # Preserve a union of historical reservations, without labelling unknown
    # historical writes as SENT or CRM committed.
    def domain(value):
        parts=value.split('@')[1].split('.')
        return '.'.join(parts[-3:]if len(parts)>=3 and parts[-2]in {'co','com','org'}and len(parts[-1])==2 else parts[-2:])
    value={'mailboxes':sorted(mailboxes),'org_domains':sorted({domain(a)for a in mailboxes}),
           'source_ids':sorted(source_ids),'inventory_sha256':fingerprint(manifest)}
    central=Central(root/'central')
    with central.lock():
        prior=[e for e in central.events()if e['kind']=='legacy_snapshot']
        if prior:
            if prior[-1]['payload']!=value:raise Stop('legacy_inventory_changed_requires_review')
            return {'legacy_reservations':len(mailboxes),'already_seeded':True}
        if central.events():raise Stop('cannot_seed_after_pilot_attempts')
        central.append('legacy_snapshot',run_id='legacy-protection',payload=value)
    return {'legacy_reservations':len(mailboxes),'already_seeded':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('command',choices=['prepare','check','tick','status','result','guard','seed'])
    parser.add_argument('--slot',type=int)
    args=parser.parse_args();root=args.root.resolve()
    try:
        if args.command=='guard':answer=guard(root)
        elif args.command=='seed':answer=seed(root)
        else:
            pilot=create(root)
            with redirect_stdout(sys.stderr):
                if args.command=='prepare':answer=pilot.prepare()
                elif args.command=='check':answer=pilot.check_ready()
                elif args.command=='tick':
                    if args.slot is None:raise Stop('explicit_slot_required')
                    pilot.gmail.allow_write=True;answer=pilot.tick(args.slot)
                elif args.command=='result':
                    _,mails,batch=pilot.load_ready();_,events=pilot.statuses(mails,batch)
                    atomic(pilot.run/'latest-bounce-check.json',pilot.bounce_check(events));answer=pilot.result()
                else:answer=pilot.result()
            if args.command in {'status','result'}:
                print(answer['text']);return
        print(json.dumps(answer,ensure_ascii=False,sort_keys=True))
    except Exception as error:
        # No retry/reconciliation write occurs here. Every attempted provider
        # call already has a durable reservation in the shared server journal.
        print(json.dumps({'state':'stopped','reason':type(error).__name__+': '+str(error)[:220]},ensure_ascii=False))
        raise SystemExit(2)


if __name__=='__main__':main()
