"""Entrypoint: python -m gtm.prep_daemon_entry [--activate --permissions FILE] [--once]

Dry-run by default: renders and prints, sends nothing, records nothing.
Activation needs a private, owner-reviewed permission file naming the private
destination, the allowed evidence sources and any owner-set policy values.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import time
from datetime import time as clock_time
from pathlib import Path
from gtm.briefing_daemon import CODE, DESTINATION, ReviewRequired, private_dir, verify_private_destination
from gtm.prep import PrepConfig
from gtm.prep_adapters import CalendarReads, MailEvidence, PrivateSender, evidence_drafter
from gtm.prep_daemon import PrepStore, tick

CONTRACT = 'reviewed-prep-readonly-v1'
SETTABLE = {'lead_hours': int, 'outside_attendees_qualify': bool, 'important_terms': tuple, 'important_event_ids': tuple}


def config_from(permission):
    values = permission.get('policy', {})
    unknown = set(values) - set(SETTABLE) - {'quiet_start', 'quiet_end', 'evening_slot'}
    if unknown:
        raise ReviewRequired('unknown_prep_policy_field')
    args = {}
    for name, value in values.items():
        args[name] = clock_time.fromisoformat(value) if name in ('quiet_start', 'quiet_end', 'evening_slot') else \
            (tuple(value) if SETTABLE[name] is tuple else value)
    return PrepConfig(**args, owner_set=frozenset(values))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--permissions')
    parser.add_argument('--state', default='/home/hermes/.hermes/gtm/prep')
    args = parser.parse_args()
    path = Path(args.permissions) if args.permissions else None
    if path and (path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ReviewRequired('permission_file_not_private_owned')
    permission = json.loads(path.read_text()) if path else {}
    if args.activate and (permission.get('contract') != CONTRACT or permission.get('owner') != CODE or
                          permission.get('target') != DESTINATION):
        raise ReviewRequired('reviewed_activation_permission_required')
    if args.activate:
        verify_private_destination()
    config = config_from(permission)
    allowed = tuple(permission.get('allowed_sources', ()))
    store = PrepStore(private_dir(args.state) / 'prep.sqlite3')
    reads, evidence, sender = CalendarReads(), MailEvidence(), PrivateSender(args.state)
    while True:
        try:
            report = tick(reads, store, sender, evidence, evidence_drafter, now=dt.datetime.now(dt.timezone.utc),
                          config=config, allowed_sources=allowed, own_addresses=(CODE,), activate=args.activate)
            print(json.dumps({k: v for k, v in report.items() if k != 'notes'}), flush=True)
        except Exception:
            print(json.dumps({'abstain': True, 'reason': 'prep_pass_unavailable'}), flush=True)
        if args.once:
            return
        time.sleep(300)


if __name__ == '__main__':
    main()
