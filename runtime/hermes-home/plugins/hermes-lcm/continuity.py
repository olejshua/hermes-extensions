"""Synchronous evidence descriptor for the native session continuation hook.

This preserves references, not semantic judgements or new permissions. No store,
model, source refresh, service or business action is started here.
"""
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .engine_registry import resolve_active_lcm_engine


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _file_ref(path, authority):
    if path.is_symlink():
        raise ValueError('continuity_symlink_refused')
    if not path.exists():
        return {'path': str(path), 'state': 'missing', 'authority': 'none'}
    return {'path': str(path), 'state': 'present', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'authority': authority}


def _regular_path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('continuity_generated_path_invalid')
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError('continuity_generated_path_missing')
    return path


def _original_version(record):
    provenance = record.get('provenance') or json.loads(record.get('provenance_json') or '{}')
    version = {k: provenance.get(k) for k in ('source_uri', 'source_version', 'message_sha256',
                                               'session_source', 'evidence_status')}
    version.update(ref=record.get('ref'), role=record.get('role'))
    if (any(not isinstance(version.get(k), str) or not version[k]
            for k in ('ref', 'role', 'source_uri', 'source_version'))
            or not isinstance(version['message_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', version['message_sha256'])):
        raise ValueError('continuity_generated_original_unbound')
    return version


def _generated_dream_source(source, manifest):
    """Bind the existing generated publication type; never infer human approval.

    Publication provenance is read from the producer's own committed records.
    File names/content claims, sync acknowledgements and generic SourceSpec
    status defaults cannot turn this branch into a rule/consent authority.
    """
    config = manifest.get('dreaming') or {}
    # This is the exact existing manifest shape. Other unreceipted source types
    # (including altered filters/authority fields) retain the ordinary blocker.
    if (set(source) != {'id', 'root', 'namespace', 'include_extensions'}
            or source.get('id') != 'dream-conclusions' or source.get('namespace') != 'dreams'
            or source.get('include_extensions') != ['.md']):
        raise ValueError('continuity_source_receipt_unbound')
    root = _regular_path(source['root'], directory=True)
    if root != _regular_path(config['import_dir'], directory=True):
        raise ValueError('continuity_generated_root_mismatch')
    reports = _regular_path(config['reports_dir'], directory=True)
    state = _regular_path(config['dream_state_db'])
    inventory = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('continuity_generated_symlink')
        if path.is_file() and path.suffix == '.md':
            inventory.append(_file_ref(_regular_path(path), 'none'))
    conn = sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        publications = [dict(r) for r in conn.execute(
            'SELECT publication_id,run_id,fingerprint,final_import_path,final_report_path,'
            'content_hash,report_hash,status,indexable FROM publications WHERE indexable=1 '
            'ORDER BY publication_id')]
        if any(r['status'] not in ('pending', 'locally_published') for r in publications):
            raise ValueError('continuity_generated_publication_state_unknown')
        committed = [r for r in publications if r['status'] == 'locally_published']
        expected_paths = [r['final_import_path'] for r in committed]
        # Completeness is checked at first capture too. A missing committed
        # original must not become a new apparently valid empty snapshot.
        # Pending outbox records have not published a source yet; indexable=0
        # private reports deliberately never enter this watched source.
        if (len(expected_paths) != len(set(expected_paths))
                or set(expected_paths) != {r['path'] for r in inventory}):
            raise ValueError('continuity_generated_published_inventory_incomplete')
        bound = []
        for item in inventory:
            matches = [r for r in committed if r['final_import_path'] == item['path']]
            if len(matches) != 1:
                raise ValueError('continuity_generated_publication_unbound')
            pub = matches[0]
            run_id = pub['run_id']
            if (not re.fullmatch(r'run_[a-zA-Z0-9_]+', run_id)
                    or Path(item['path']) != root / (run_id + '.md')
                    or pub['status'] != 'locally_published' or pub['content_hash'] != item['sha256']):
                raise ValueError('continuity_generated_publication_mismatch')
            report = _regular_path(pub['final_report_path'])
            report_ref = _file_ref(report, 'none')
            if report != reports / (run_id + '.md') or report_ref['sha256'] != pub['report_hash']:
                raise ValueError('continuity_generated_report_mismatch')
            run = conn.execute('SELECT run_id,status,mode FROM runs WHERE run_id=?', (run_id,)).fetchone()
            report_rows = [dict(r) for r in conn.execute(
                'SELECT report_id,run_id,path,fingerprint FROM reports WHERE run_id=? AND path=?',
                (run_id, str(report)))]
            if (run is None or run['status'] != 'complete' or len(report_rows) != 1
                    or report_rows[0]['fingerprint'] != pub['fingerprint']):
                raise ValueError('continuity_generated_run_unbound')
            promotions = [dict(r) for r in conn.execute(
                'SELECT promotion_id,candidate_id,run_id,target,score,rationale,artifact_path '
                'FROM promotions WHERE run_id=? AND target=\'openviking_dream\' ORDER BY promotion_id',
                (run_id,))]
            originals = []
            for promotion in promotions:
                if promotion['artifact_path'] != item['path']:
                    raise ValueError('continuity_generated_promotion_mismatch')
                rows = list(conn.execute('SELECT ref,role,provenance_json FROM evidence '
                                         'WHERE candidate_id=? ORDER BY evidence_id', (promotion['candidate_id'],)))
                if not rows:
                    raise ValueError('continuity_generated_evidence_missing')
                originals.extend(_original_version(dict(r)) for r in rows)
            insights = [dict(r) for r in conn.execute(
                'SELECT insight_id,run_id,kind,status,deep_supported,evidence_json,candidate_ids_json,'
                'contradiction_json,reflection_json FROM insights WHERE run_id=? AND deep_supported=1 '
                'ORDER BY insight_id', (run_id,)) if
                (r['status'] == 'grounded' and r['kind'] not in ('contradiction', 'stale_conflict', 'hypothesis', 'proposal'))
                or (r['status'] == 'proposed' and r['kind'] == 'proposal')]
            for insight in insights:
                evidence = json.loads(insight['evidence_json'])
                if not evidence:
                    raise ValueError('continuity_generated_evidence_missing')
                originals.extend(_original_version(r) for r in evidence)
            if not originals:
                raise ValueError('continuity_generated_originals_missing')
            # One original identity cannot silently acquire conflicting roles or
            # versions. All stored refs remain bound, including assistant refs.
            identities = {}
            for original in originals:
                key = original['ref']
                if key in identities and identities[key] != original:
                    raise ValueError('continuity_generated_original_conflict')
                identities[key] = original
            original_map = [identities[k] for k in sorted(identities)]
            records = dict(publication=pub, run=dict(run), reports=report_rows,
                           promotions=promotions, insights=insights, originals=original_map)
            bound.append(dict(publication_id=pub['publication_id'], run_id=run_id, artifact=item,
                              report=report_ref, original_ref_count=len(original_map),
                              provenance_sha256=_digest(records), provenance_ref=str(state), authority='none'))
    finally:
        conn.close()
    registry = _regular_path(manifest['state_db'])
    conn = sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        rows = conn.execute('SELECT source_id,source_root_id,namespace,path,relative_path,sha256,status,'
                            'ov_resource_id,sync_receipt FROM resources WHERE source_root_id=? OR namespace=? '
                            'ORDER BY source_id', (source['id'], source['namespace'])).fetchall()
        for row in rows:
            if row[1:3] != (source['id'], source['namespace']) or not Path(row[3]).is_relative_to(root):
                raise ValueError('continuity_generated_registry_mismatch')
    finally:
        conn.close()
    return dict(source_id=source['id'], namespace=source['namespace'], root=str(root),
                state='bound_generated_unconfirmed', authority='none', approval_verified=False,
                evidence_status='unknown', source_definition_sha256=_digest(source),
                publication_store=str(state), publication_catalog_sha256=_digest(publications),
                artifacts=bound, artifact_count=len(bound),
                pending_publication_count=sum(r['status'] == 'pending' for r in publications),
                registry_ref=str(registry), registry_record_count=len(rows), registry_versions_sha256=_digest(rows))


def _brain_descriptor():
    supplied = os.environ.get('OLE_BRAIN_MANIFEST')
    path = Path(supplied or '/home/hermes/hermes-extensions-staging/brain/manifest.json')
    if not path.exists() and not supplied:
        return {'state': 'not_configured', 'authority': 'none'}
    manifest = json.loads(path.read_text())
    result = {'state': 'bound_evidence_refs', 'manifest': _file_ref(path, 'configuration_only'),
              'sources': []}
    grant_path = manifest.get('source_grants')
    if grant_path:
        grant_path = Path(grant_path)
        grants = json.loads(grant_path.read_text())
        result['grants'] = _file_ref(grant_path, 'explicit_source_grant_registry')
        result['grant_states'] = sorted([{'id': g['id'], 'active': g.get('active') is True}
                                         for g in grants['grants']], key=lambda g: g['id'])
    else:
        result['grants'] = {'state': 'not_bound', 'authority': 'none'}
    for source in manifest.get('sources', []):
        receipts = source.get('approval_receipts')
        if not receipts:
            result['sources'].append(_generated_dream_source(source, manifest))
            continue
        receipt_path = Path(receipts)
        if receipt_path.is_symlink():
            raise ValueError('continuity_source_receipt_symlink')
        packet = json.loads(receipt_path.read_text())
        # Exclude periodic generated_at/last_verified churn, retain actual
        # identity, version and authority. This digest references the full map.
        records = {k: {f: r.get(f) for f in ('original_path', 'sha256', 'export_sha256', 'status',
                                            'role', 'export_path', 'char_start', 'char_end')}
                   for k, r in packet['records'].items()}
        result['sources'].append({'source_id': source['id'], 'namespace': source['namespace'],
                                  'receipt_ref': str(receipt_path), 'record_count': len(records),
                                  'identity_version_authority_sha256': _digest(records),
                                  'accepted_statuses': source.get('accepted_statuses', ['confirmed']),
                                  'authority': 'source_evidence_only; current consent and freshness must be rechecked'})
    state_path = Path(manifest['state_db'])
    if state_path.is_symlink():
        raise ValueError('continuity_registry_symlink')
    conn = sqlite3.connect(state_path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        rows = conn.execute('SELECT source_id,sha256,status FROM resources ORDER BY source_id').fetchall()
    finally:
        conn.close()
    result['native_registry'] = {'path': str(state_path), 'record_count': len(rows),
                                 'versions_sha256': _digest(rows)}
    return result


def make_continuity_provider(hermes_home):
    home = Path(hermes_home).resolve()

    def continuity(*, session_id, cwd, source_watermark, phase, evidence_pin=None,
                   source_session_id=None, **unused):
        engine = resolve_active_lcm_engine(session_id=str(session_id or ''))
        if engine is None or not engine._hermes_home or Path(engine._hermes_home).resolve() != home:
            return None  # No guessed binding and no global rollover blocker.
        envelope = {'contract': 'hermes-session-continuity-v1', 'provider': 'hermes-lcm',
                    'session_id': session_id, 'source_watermark': source_watermark}
        try:
            if phase == 'checkin':
                # The active child verifies its own retained parent originals;
                # no rebinding of the engine to a guessed/foreign session.
                if (not isinstance(source_session_id, str) or not source_session_id
                        or source_session_id == session_id or type(source_watermark) is not int
                        or source_watermark < 0 or not isinstance(evidence_pin, Mapping)):
                    raise ValueError('continuity_checkin_binding_invalid')
                pinned = evidence_pin.get('hermes-lcm')
                expected = dict(envelope, session_id=source_session_id)
                if (not isinstance(pinned, Mapping) or pinned.get('state') != 'complete'
                        or any(pinned.get(k) != v for k, v in expected.items())):
                    raise ValueError('continuity_checkin_pin_invalid')
                snapshot = pinned['snapshot']
                refs = snapshot['work_scope']['evidence_refs']
                if (not isinstance(refs, list) or len(refs) != 1
                        or snapshot['work_scope']['cwd'] != str(Path(cwd).resolve())):
                    raise ValueError('continuity_checkin_scope_invalid')
                original = refs[0]
                if (not isinstance(original, Mapping)
                        or type(original.get('last_store_id')) is not int
                        or original['last_store_id'] <= 0
                        or engine._store.session_evidence_descriptor(source_session_id,
                            through_store_id=original['last_store_id']) != original):
                    raise ValueError('continuity_checkin_originals_changed')
                current_memory = {'state': 'file_refs', 'files': [
                    _file_ref(home / 'memories/USER.md', 'configured_user_memory'),
                    _file_ref(home / 'memories/MEMORY.md', 'configured_curated_memory'),
                    _file_ref(home / 'SOUL.md', 'configured_identity')]}
                if (_brain_descriptor() != snapshot['source_rights']
                        or current_memory != snapshot['native_memory']):
                    raise ValueError('continuity_checkin_sources_changed')
                return dict(envelope, state='verified', source_session_id=source_session_id,
                            evidence_pin_sha256=_digest(pinned))
            if type(source_watermark) is not int or source_watermark < 0 or phase not in ('checkout', 'publication'):
                raise ValueError('continuity_invalid_binding')
            pinned = None
            if phase == 'publication':
                if not isinstance(evidence_pin, Mapping):
                    raise ValueError('continuity_publication_pin_missing')
                pinned = evidence_pin.get('hermes-lcm')
                if not isinstance(pinned, Mapping) or any(pinned.get(k) != v for k, v in envelope.items()):
                    raise ValueError('continuity_publication_pin_binding_mismatch')
                if pinned.get('state') != 'complete' or type(pinned.get('source_watermark')) is not int:
                    raise ValueError('continuity_publication_pin_invalid')
                refs = pinned['snapshot']['work_scope']['evidence_refs']
                if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
                    raise ValueError('continuity_original_pin_invalid')
                original = refs[0]
                if (any(type(original.get(k)) is not int or original[k] <= 0
                        for k in ('first_store_id', 'last_store_id', 'message_count'))
                        or original['first_store_id'] > original['last_store_id']
                        or not isinstance(original.get('ordered_rows_sha256'), str)
                        or not re.fullmatch(r'[0-9a-f]{64}', original['ordered_rows_sha256'])):
                    raise ValueError('continuity_original_pin_boundary_invalid')
                archive = engine._store.session_evidence_descriptor(
                    session_id, through_store_id=original['last_store_id'])
                if archive != original:
                    raise ValueError('continuity_original_prefix_changed')
            else:
                if evidence_pin is not None:
                    raise ValueError('continuity_checkout_cannot_choose_a_prefix')
                archive = engine._store.session_evidence_descriptor(session_id)
            if not archive['message_count']:
                raise ValueError('continuity_originals_missing')
            unclassified = {'state': 'unclassified', 'semantic_owner': 'checkout', 'evidence_refs': [archive]}
            snapshot = {'coverage': 'evidence_only_semantic_completeness_not_asserted',
                        'work_scope': dict(unclassified, session_id=session_id, source_watermark=source_watermark,
                                           cwd=str(Path(cwd).resolve())),
                        'instructions': dict(unclassified), 'corrections': dict(unclassified),
                        'source_rights': _brain_descriptor(), 'open_decisions': dict(unclassified),
                        'verified_partials': dict(unclassified),
                        'native_memory': {'state': 'file_refs', 'files': [
                            _file_ref(home / 'memories/USER.md', 'configured_user_memory'),
                            _file_ref(home / 'memories/MEMORY.md', 'configured_curated_memory'),
                            _file_ref(home / 'SOUL.md', 'configured_identity')]}}
            # No source prose/Auth appears in the descriptor. Missing semantic
            # classifications remain explicit; a reference does not grant consent.
            if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 80000:
                raise ValueError('continuity_descriptor_too_large')
            result = dict(envelope, state='complete', snapshot=snapshot)
            # Recompute every live source/grant/version and native memory ref;
            # only append-only conversation growth may be outside the prefix.
            if pinned is not None and result != pinned:
                raise ValueError('continuity_source_or_native_memory_changed')
            return result
        except Exception:
            # Exceptions can contain private paths or DB values; only a fixed code leaves this boundary.
            return dict(envelope, state='blocked', reason='continuity_evidence_unavailable')

    return continuity
