"""Join reviewed Dream proposals to independently observed CURRENT incidents.

No proposal text is executable. No model dispatch, retry, activation, business
write or second queue. Unreproduced ideas remain in the existing Dream store.
The caller supplies a trusted read-only committed-reflection loader and an
explicit source binding; neither is derived by parsing a suggestion's prose.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from self_repair.policy import digest
from self_repair.store import canonical, hash_value, private_json


def admit_reflection(record, *, load_reflection, monitor, item, expected_source_key=None, relationship=None):
    """Return a provenance link only; the existing controller owns all action."""
    item_id = record.get('item_id', '')
    item_hash = record.get('item_sha256', '')
    if not re.fullmatch(r'ins_[A-Za-z0-9_-]{1,120}', item_id) or not re.fullmatch(r'[a-f0-9]{64}', item_hash):
        return {'state': 'proposal_only', 'reason': 'reflection_identity_invalid'}
    frozen = load_reflection(item_id)
    if not frozen or canonical(frozen) != canonical(record):
        return {'state': 'proposal_only', 'reason': 'reflection_changed_or_uncommitted'}
    if (record.get('schema_version') != 'dream-reflection/v1'
            or record.get('deterministic_status') != 'supported_proposal'
            or record.get('classification') != 'proposal' or record.get('scope') != 'hermes'
            or record.get('deep_supported') is not True or not record.get('evidence')
            or record.get('requires_approval') is not True
            or record.get('action_authorized') is not False or record.get('executed') is not False):
        return {'state': 'proposal_only', 'reason': 'reflection_not_supported_proposal'}
    # No cartesian matching of generic suggestions and open mail failures.
    # This structured relationship is supplied by the integration reviewer,
    # never inferred from prose or issued by the Dream record itself.
    if (not isinstance(relationship, dict)
            or relationship.get('contract') != 'reviewed-technical-source/v1'
            or relationship.get('reviewed_by') != 'root'
            or not re.fullmatch(r'[a-f0-9]{64}', str(expected_source_key or ''))
            or relationship.get('evidence_ref') not in {e.get('ref') for e in record['evidence']
                                                        if e.get('role') in {'user','canonical'}}):
        return {'state': 'proposal_only', 'reason': 'reviewed_structured_relationship_required'}
    # Only a source already covered by the independent outcome monitor may bind.
    with monitor.incidents.lifecycle.connect() as db:
        row = db.execute('''SELECT * FROM mail_outcome_sources
            WHERE account=? AND message_id=? AND thread_id=?''',
            (item.get('account'), item.get('id'), item.get('threadId'))).fetchone()
    if not row or row['state'] != 'repair_recorded':
        return {'state': 'proposal_only', 'reason': 'no_current_observed_violation'}
    row = dict(row)
    facts = monitor.reads.facts(item)
    if facts.get('state') != 'current':
        return {'state': 'proposal_only', 'reason': 'source_superseded_or_unverified'}
    case = facts.get('case') or {}
    runtime = getattr(monitor.reads, 'runtime', None)
    if ((runtime and item['threadId'] in runtime.CLOSED_THREADS.get(item['account'], set()))
            or (case.get('outcome') or {}).get('status') == 'deliberately_closed'):
        return {'state': 'proposal_only', 'reason': 'source_deliberately_closed'}
    if (case.get('phase') in {'draft_write_pending', 'draft_update_pending'}
            or (case.get('outcome') or {}).get('status') == 'needs_manual_review_draft_state'):
        return {'state': 'proposal_only', 'reason': 'unknown_write_requires_readback'}
    standard = monitor.reads.standard
    thread = facts['thread']
    stable = [{k: v for k, v in m.items() if k not in {'labels', 'truncated'}} for m in thread]
    worker = monitor.home / 'scripts/mail_model_worker.py'
    key = hash_value([standard.CONTRACT, standard.REVIEW, digest(worker) if worker.exists() else None,
                      stable, facts.get('draft'), facts.get('hard_facts')])
    receipt_path = monitor.store.root / (key + '.json')
    if (row['input_key'] != key or row['receipt_path'] != str(receipt_path)
            or receipt_path.is_symlink() or not receipt_path.is_file()):
        return {'state': 'proposal_only', 'reason': 'observation_changed'}
    receipt = json.loads(receipt_path.read_bytes())
    if receipt.get('contract') != standard.CONTRACT:
        return {'state': 'proposal_only', 'reason': 'standard_changed'}
    verdict = standard.validate_review(receipt['review'], standard.evidence_text(thread[-1]), facts.get('draft'))
    hard = facts.get('hard_facts') or {}
    if verdict['result'] not in {'missing', 'revise'} and not hard.get('hard_errors'):
        return {'state': 'proposal_only', 'reason': 'no_current_standard_violation'}
    code = ('reply_existing_draft_needs_review' if facts.get('draft') and not facts.get('owned_draft')
            else 'reply_result_missing' if verdict['result'] == 'missing' else 'reply_standard_failed')
    source_key = hash_value(['outcome', item['account'], item['id'], key, code])
    if expected_source_key is not None and source_key != expected_source_key:
        return {'state': 'proposal_only', 'reason': 'reviewed_source_binding_changed'}
    with monitor.incidents.lifecycle.connect() as db:
        source = db.execute('SELECT * FROM repair_sources WHERE source_key=?', (source_key,)).fetchone()
    if not source:
        return {'state': 'proposal_only', 'reason': 'independent_incident_missing'}
    incident = monitor.incidents.get(source['signature'])
    versions = {rel: digest(path) for rel, path in monitor.targets.items()}
    if (relationship.get('source_snapshot_sha256') != digest(source['snapshot_path'])
            or relationship.get('versions_sha256') != hash_value(versions)):
        return {'state': 'proposal_only', 'reason': 'relationship_source_version_changed'}
    if incident['component'] != 'mail' or incident['code'] != code or incident['versions'] != versions:
        return {'state': 'proposal_only', 'reason': 'component_version_changed'}
    # Finished/closed work is never reopened by a reflection.
    if (source['outcome'] not in {'pending', 'workflow_blocked', 'case_changed_or_retry_used'}
            or incident['job']['state'] not in {'queued', 'failed', 'stalled'}):
        return {'state': 'proposal_only', 'reason': 'incident_already_settled_or_running'}
    snapshot = json.loads(Path(source['snapshot_path']).read_bytes())
    expected = snapshot.get('expectation') or {}
    original = [{k: v for k, v in m.items() if k not in {'labels', 'truncated'}}
                for m in snapshot.get('original_thread', [])]
    if (expected.get('input_key') != key or snapshot.get('item') != item
            or original != stable or expected.get('checked_draft') != facts.get('draft')
            or expected.get('receipt') != receipt):
        return {'state': 'proposal_only', 'reason': 'original_evidence_changed'}
    # Recheck exact versions and observation after all native reads, before linking.
    if versions != {rel: digest(path) for rel, path in monitor.targets.items()}:
        return {'state': 'proposal_only', 'reason': 'component_version_changed'}
    link = {'contract': 'dream-mail-repair-link/v1', 'item_id': item_id, 'item_sha256': item_hash,
            'source_key': source_key, 'signature': incident['signature'], 'job_id': incident['job_id'],
            'versions': versions, 'source_snapshot_sha256': digest(source['snapshot_path']),
            'relationship': relationship,
            'advisory': {key: str(record.get(key) or '')[:1800] for key in
                         ('claim','detail','proposed_result','expected_check','uncertainty')},
            'action_authorized': False, 'executed': False}
    destination = monitor.incidents.root / 'proposal-links' / (item_id + '-' + item_hash + '.json')
    if destination.exists():
        if destination.is_symlink() or json.loads(destination.read_bytes()) != link:
            return {'state': 'proposal_only', 'reason': 'proposal_binding_changed'}
    else:
        private_json(destination, link)
    return {'state': 'linked_existing_incident', **link}
