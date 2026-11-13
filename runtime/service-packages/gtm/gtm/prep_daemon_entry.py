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
from gtm.prep_adapters import (CalendarReads, CombinedEvidence, MailEvidence, PrivateSender, TwentyEvidence,
                               Unconfigured, evidence_drafter)
from gtm.prep_daemon import PrepStore, tick
from gtm.synthesis import MODES, ModelPrepDrafter
from gtm.commitments import CommitmentStore, commitment_tick
from zoneinfo import ZoneInfo

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
    return PrepConfig(**args, configured=frozenset(values))


def build_evidence(permission, allowed, client_factory=None, mail=None):
    """Collectors for the allowed sources. Building one is not permission to read: every
    collector still returns nothing unless its source is in `allowed`, and an allowed source
    without private configuration becomes a named gap in the note."""
    collectors = [mail or MailEvidence()]
    allowed_names = {a.casefold() for a in allowed}
    if 'twenty' in allowed_names or permission.get('twenty'):
        paths = permission.get('twenty') or {}
        if 'twenty' in allowed_names and paths.get('helper_dir') and paths.get('key_path'):
            factory = client_factory
            if factory is None:
                from gtm.twenty import ExistingHelpersClient
                factory = ExistingHelpersClient
            try:
                collectors.append(TwentyEvidence(factory(Path(paths['helper_dir']), Path(paths['key_path'])), own=(CODE,)))
            except Exception:
                collectors.append(Unconfigured('twenty'))
        else:
            collectors.append(Unconfigured('twenty'))
    return CombinedEvidence(*collectors), collectors[0]


def build_drafter(permission, complete_factory=None, ledger=None):
    """Echo drafter unless the permission file names synthesis mode dry_run/on AND a completion
    factory is injected. This module ships no factory, holds no key and chooses no model, so
    today every path returns the deterministic echo. Naming a mode without a factory is a stop,
    not a silent downgrade."""
    mode = (permission.get('synthesis') or {}).get('mode', 'off')
    if mode not in MODES:
        raise ReviewRequired('unknown_synthesis_mode')
    if mode == 'off':
        return evidence_drafter
    if complete_factory is None:
        raise ReviewRequired('synthesis_completion_not_configured')
    # The owner's approval of provider/account is a separate, explicit field; configuration alone is not approval.
    approved = (permission.get('synthesis') or {}).get('transmission_approved') is True
    if not approved:
        raise ReviewRequired('synthesis_transmission_not_approved')
    if ledger is None:
        raise ReviewRequired('synthesis_attempt_ledger_required')
    cfg = permission.get('synthesis') or {}
    scope = '|'.join((str(permission.get('owner')), 'openai-codex', str(cfg.get('model')), 'prep'))
    return ModelPrepDrafter(complete_factory(permission), mode, transmission_approved=True, ledger=ledger, scope=scope)


def outside_quiet_hours(config, local_now):
    """Quiet window from the prep config (design default 21:30-08:00 unless the owner's file set it); wraps midnight."""
    t, start, end = local_now.time(), config.quiet_start, config.quiet_end
    quiet = (t >= start or t < end) if start > end else (start <= t < end)
    return not quiet


def proposal_section(permission, allowed, proposals, commitments, *, activate, service_factory=None, complete_factory=None,
                     ledger=None):
    """Mail-commitment proposals for the owner-private note. Returns (text, report).

    Reads and model use happen only when 'mail_commitments' is allowed AND synthesis is configured and
    approved (propose_from_thread stays disabled otherwise). reconcile() only runs when activated.
    Rendering never sends or commits anything."""
    from gtm.commitment_proposals import propose_from_thread, reconcile, render_pending
    from gtm.gmail_mail_source import recent_sent_thread_ids
    report = {'proposed': 0, 'reconciled': None, 'status': 'render_only'}
    names = {a.casefold() for a in allowed}
    cfg = permission.get('synthesis') or {}
    if 'mail_commitments' in names and service_factory and complete_factory and cfg.get('mode') in ('dry_run', 'on'):
        if cfg.get('transmission_approved') is not True or ledger is None:
            raise ReviewRequired('synthesis_not_approved_or_no_ledger')
        if cfg['mode'] == 'on' and not activate:
            raise ReviewRequired('model_mode_on_requires_activation')
        service = service_factory()
        owner = CODE
        scope = '|'.join((str(permission.get('owner')), 'openai-codex', str(cfg.get('model')), 'mail'))
        complete = complete_factory(permission)
        for thread_id in recent_sent_thread_ids(service, owner):
            res = propose_from_thread(service, owner, thread_id, proposals, complete=complete, mode=cfg['mode'],
                                      transmission_approved=True, ledger=ledger, scope=scope)
            report['proposed'] += res.get('proposed', 0)
        report['status'] = 'collected'
    if activate:
        report['reconciled'] = reconcile(proposals, commitments)
    return render_pending(proposals), report


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
    commitments = CommitmentStore(private_dir(args.state) / 'commitments.sqlite3')
    reads, sender = CalendarReads(), PrivateSender(args.state)
    evidence, _ = build_evidence(permission, allowed)
    from gtm.synthesis import ModelLedger
    drafter = build_drafter(permission, ledger=ModelLedger(private_dir(args.state) / 'model-attempts.sqlite3'))
    while True:
        try:
            report = tick(reads, store, sender, evidence, drafter, now=dt.datetime.now(dt.timezone.utc),
                          config=config, allowed_sources=allowed, own_addresses=(CODE,), activate=args.activate)
            print(json.dumps({k: v for k, v in report.items() if k != 'notes'}), flush=True)
            if 'commitments' in allowed:
                from gtm.commitment_proposals import ProposalStore
                proposals_store = ProposalStore(private_dir(args.state) / 'proposals.sqlite3')
                note, prep = proposal_section(permission, allowed, proposals_store, commitments, activate=args.activate)
                from gtm.commitment_proposals import deliver_notice
                notice = deliver_notice(proposals_store, sender, activate=args.activate, allowed_now=outside_quiet_hours(config, dt.datetime.now(ZoneInfo('Europe/Berlin'))))
                print(json.dumps({'proposals': prep, 'pending': bool(note), 'notice': notice}), flush=True)
                done = commitment_tick(commitments, sender, dt.datetime.now(dt.timezone.utc), ZoneInfo('Europe/Berlin'), activate=args.activate)
                print(json.dumps({k: v for k, v in done.items() if k != 'message'}), flush=True)
        except Exception:
            print(json.dumps({'abstain': True, 'reason': 'prep_pass_unavailable'}), flush=True)
        if args.once:
            return
        time.sleep(300)


if __name__ == '__main__':
    main()
