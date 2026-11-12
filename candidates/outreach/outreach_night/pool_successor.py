"""Acquire only retained, previously permitted and still unacquired members.

The caller supplies a real source-phase capability and the original native
resolution. This is not a campaign grant, scheduler, quota or source catalogue.
"""
import json
from pathlib import Path

from background_jobs.contracts import ContractError, approved_path, digest
from background_jobs.draft_intake import load, save
from .protections import exclusions, permits
from .pool_sources import related_originals
from .sources import persist
from .write_scope import NativeWriteScope


def acquire_successor(host, store, policy, active, *, intent, native_scope, result_ref):
    if (not isinstance(native_scope, NativeWriteScope) or native_scope.kind != 'background'
            or native_scope.phase != 'source' or native_scope.lifecycle.db_path != store.lifecycle.db_path):
        raise ContractError('actual_native_acquisition_parent_scope_required')
    # The native caller revalidates origin, old K, revisions and unknown writes
    # at every boundary; JSON allow flags cannot provide this capability.
    def checked():
        active()
        native_scope.check()
    checked()
    if (store.root / result_ref).exists():
        value = load(store.root, result_ref)
        if value.get('intent_sha256') != digest(intent):
            raise ContractError('pool_successor_original_changed')
        return value
    if not hasattr(host, 'acquisition') or host.pool_source is None:
        from .domain_factory import HostDomainFactory
        host = HostDomainFactory.from_existing(store, policy, native_budget=getattr(host, 'native_budget', None))
    if any(getattr(host, key, None) is None for key in ('acquisition', 'protections', 'templates')):
        raise ContractError('native_complete_acquisition_adapters_required')
    first = load(store.root, intent['first_resolution']['ref'])
    if digest(first) != intent['first_resolution']['sha256']:
        raise ContractError('pool_successor_first_resolution_changed')
    recipe = first['recipe']
    if host.describe_pool(recipe['name']) != recipe:
        raise ContractError('native_pool_recipe_changed')
    selection = first['selection']['source_manifest']
    raw = approved_path(store.root, selection['native_original_ref']).read_bytes()
    if digest(raw) != selection['native_original_sha256']:
        raise ContractError('native_full_pool_original_changed')
    records = json.loads(raw)['original']['records']
    allowed = first['eligible_after_protections']
    by_id = {candidate['item_id']: candidate for candidate in allowed}
    remaining = intent['unacquired_item_ids']
    if len(by_id) != len(allowed) or any(key not in by_id for key in remaining):
        raise ContractError('pool_successor_membership_changed')
    binding = dict(recipe=recipe, owner=intent['owner'], target=intent['original_target'],
        selection_manifest=selection, selection_manifest_sha256=digest(selection),
        continuation_intent_sha256=digest(intent), first_resolution=intent['first_resolution'])
    protection, protection_ref = host.protections.capture(binding, checked)
    template_originals, template = host.templates.capture(binding, checked)
    binding['external_originals'] = [
        dict(ref=selection['native_original_ref'], sha256=selection['native_original_sha256']),
        dict(ref=protection_ref, sha256=protection['sha256'])]
    central=json.loads(protection['content'])['original']
    if central.get('contract')=='outreach-central-sources-v1':
        for entry in central['changes']:
            ref={'ref':entry['ref'],'sha256':entry['sha256']}
            if digest(approved_path(store.root,ref['ref']).read_bytes())!=ref['sha256']:
                raise ContractError('central_source_original_changed')
            if ref not in binding['external_originals']:binding['external_originals'].append(ref)
    membership = persist(store.root, 'crm_pool', 'crm-complete-selection',
        dict(complete_original=binding['external_originals'][0], source_counts=selection['source_counts'],
             objects=selection['objects'], eligible_item_ids=list(by_id)), binding,
        'twenty.full-pool.deterministic-membership')
    blocked = exclusions(protection)
    excluded, examined, chosen, items = [], [], [], []
    check_doc = persist(store.root, 'protections', 'acribic-protection-membership',
        dict(complete_original=binding['external_originals'][1], excluded=first['excluded_by_current_protections']),
        binding, 'authoritative-protections.deterministic-membership')
    common = [membership[0], check_doc[0], *[doc for doc, _ in template_originals]]
    from .smtp import SMTPOriginals
    def smtp_scope(identity, candidate, check):
        checked()
        if (identity['item']['account'] != native_scope.account
                or identity['item']['category'] != recipe['project']):
            raise ContractError('native_acquisition_candidate_account_changed')
        return native_scope
    smtp = host.smtp
    if isinstance(smtp, SMTPOriginals):
        # Same real counter and transports, capability of this source Attempt.
        smtp = SMTPOriginals(smtp.root, native_budget=smtp.budget, scope_factory=smtp_scope,
            ip_reader=smtp.ip_reader, mx_reader=smtp.mx_reader, session=smtp.session)
    elif smtp is None and host.native_budget is not None:
        smtp = SMTPOriginals(store.root, native_budget=host.native_budget, scope_factory=smtp_scope)
    limit = min(500, intent['remaining_target'] + min(5, max(1, intent['remaining_target'])))
    retained = intent['retained_receipts']
    families = {entry['candidate']['family_id'] for entry in retained}
    mailboxes = {entry['candidate']['to'].casefold() for entry in retained}
    domains = {entry['candidate']['to'].split('@')[1].casefold() for entry in retained}
    for entry in intent.get('carried', []):
        checked()
        candidate, item = entry['candidate'], entry['item']
        if (candidate not in allowed or digest(approved_path(policy['sources_root'], item['source_ref']).read_bytes()) != item['source_hash']):
            raise ContractError('pool_successor_carried_source_changed')
        chosen.append(candidate)
        items.append(item)
    for key in remaining:
        checked()
        candidate = by_id[key]
        if len(chosen) >= limit:
            break
        if (candidate['family_id'] in families or candidate['to'].casefold() in mailboxes
                or candidate['to'].split('@')[1].casefold() in domains
                or not permits(candidate, records['company'][candidate['company_id']]['name'], protection, blocked=blocked)):
            excluded.append(dict(item_id=key, reason='current_protection_or_retained_family_collision'))
            continue
        family = persist(store.root, 'crm_family', 'twenty-family:' + key,
            dict(complete_pool_original=binding['external_originals'][0], records=related_originals(records, candidate)),
            dict(candidate=candidate, **binding), 'twenty.complete-relevant-family-originals')
        acquired = host.acquisition.capture_page([candidate], policy['reviewed_templates'], Path(policy['sources_root']),
            intent['owner'], active=checked, common_documents=common + [family[0]], ted=host.ted,
            smtp=smtp, company_sources=host.company_sources, source_binding=binding)
        item = acquired['manifest']['items'][0]
        raw = approved_path(policy['sources_root'], item['source_ref']).read_bytes()
        if digest(raw) != item['source_hash']:
            raise ContractError('native_acquired_source_changed')
        bundle = json.loads(raw)
        examined.append(dict(candidate=candidate, item=item, missing_native_sources=bundle['missing_native_sources'],
            factual_errors=bundle['candidate_factual_errors']))
        if not bundle['missing_native_sources']:
            chosen.append(candidate)
            items.append(item)
        if any(value in {'smtp_daily_quota_exhausted', 'smtp_current_day_prior_usage_unknown',
                         'smtp_existing_attempt_requires_original_readback'}
               for value in bundle['candidate_factual_errors'].values()):
            break
        if smtp is None or host.ted is None or host.company_sources is None:
            break  # Missing engineering/authority is never pool exhaustion.
    consumed = {entry['candidate']['item_id'] for entry in examined} | {entry['item_id'] for entry in excluded}
    manifest = dict(contract='background-sources-v1', items=items)
    named = dict(contract='hermes-named-draft-pool-v1', name=recipe['name'], project=recipe['project'],
        account=recipe['account'], candidates=chosen, source_manifest_sha256=digest(manifest))
    missing = sorted({reason for entry in examined for reason in entry['missing_native_sources']})
    rest = [key for key in remaining if key not in consumed]
    blocker = None if len(chosen) >= intent['remaining_target'] else (
        'outreach_native_sources_incomplete' if missing else 'outreach_permitted_pool_insufficient')
    if blocker:
        blocker = next((value for entry in examined for value in entry['factual_errors'].values()
            if value in {'smtp_daily_quota_exhausted', 'smtp_current_day_prior_usage_unknown',
                         'smtp_existing_attempt_requires_original_readback'}), blocker)
    value = dict(contract='hermes-native-outreach-pool-successor-v1', intent_sha256=digest(intent),
        first_resolution=intent['first_resolution'], recipe=recipe, source_fence=native_scope.binding,
        protection_original_ref=protection_ref, protection_original_sha256=protection['sha256'],
        template_originals=[dict(ref=ref, sha256=doc['sha256']) for doc, ref in template_originals],
        approved_template_sha256=template['sha256'], acquired=examined, excluded=excluded,
        unacquired_item_ids=rest, source_manifest=manifest, named_pool=named, blocker=blocker,
        missing_native_sources=missing, original_target=intent['original_target'])
    checked()
    save(store.root, result_ref, value)  # fsynced before any new scope publication.
    return value
