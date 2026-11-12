"""Read-only host boundary for native asynchronous outreach acquisition.

Main describes a fixed recipe without any customer I/O. The existing native
acquisition job later supplies owner/target/current fence and retains complete
selection originals separately from the final per-item SourceManifest.
No profile, job, service, campaign or tool is registered by this module.
"""
import json
from pathlib import Path

from background_jobs.contracts import ContractError,canonical,digest,private_file,approved_path
from .pool_sources import CANDIDATE_LISTS,LISTS,TwentyPoolSource,related_originals
from .templates import TemplateSource,SOURCE_ID
from .protections import ServerProtectionSource,permits,exclusions
from .acquisition import AcribicAcquisition


def _save(root,ref,value):
    raw=canonical(value).encode()
    try:private_file(Path(root)/ref,raw)
    except FileExistsError:
        if approved_path(root,ref).read_bytes()!=raw:raise ContractError('outreach_resolution_original_changed')
    return digest(raw)


class HostDomainFactory:
    def __init__(self,*,pool_source=None,acquisition=None,protection_source=None,template_source=None,
                 ted=None,smtp=None,company_sources=None,native_budget=None):
        self.pool_source,self.acquisition=pool_source,acquisition
        self.protections,self.templates=protection_source,template_source
        self.ted,self.smtp,self.company_sources=ted,smtp,company_sources
        self.native_budget=native_budget

    @staticmethod
    def describe_pool(name):
        if name not in {'acribic:'+v for v in CANDIDATE_LISTS}:
            raise ContractError('native_named_pool_not_allowed')
        value={'contract':'hermes-native-outreach-pool-recipe-v1','name':name,'project':'acribic',
               'account':'operator@primary.example.invalid','version':1,'source':'twenty',
               'segments':list(LISTS),'template_source_id':SOURCE_ID,
               'acquisition_contract':'hermes-native-outreach-pool-resolution-v1'}
        return {**value,'recipe_sha256':digest(value)}

    @classmethod
    def from_existing(cls,store,policy,*,native_budget=None):
        from .clients import existing_twenty,existing_gmail
        from .sources import GmailSources,TwentySources
        from .ted import TEDOriginals
        from .company_sources import CompanyOriginals
        client=existing_twenty(policy)
        gmail=GmailSources(existing_gmail(policy,'acribic'),'acribic',store.root)
        protection=ServerProtectionSource.from_existing(store,policy)
        template_root=policy.get('outreach_template_root')
        if not isinstance(template_root,str)or not Path(template_root).is_absolute():
            raise ContractError('approved_template_host_source_not_configured')
        # A reviewed shared SMTP quota/probe route is not currently installed.
        # No live SMTP callback is silently inferred from this engineering call.
        return cls(pool_source=TwentyPoolSource(client,store.root,protection_source=protection),
                   acquisition=AcribicAcquisition(gmail,TwentySources(client,store.root)),
                   protection_source=protection,template_source=TemplateSource(template_root,store.root),
                   ted=TEDOriginals(store.root),company_sources=CompanyOriginals(store.root),native_budget=native_budget)

    def acquire_pool(self,recipe,store,policy,active,*,target,native_scope):
        expected=self.describe_pool(recipe.get('name')if isinstance(recipe,dict)else None)
        if recipe!=expected:raise ContractError('native_pool_recipe_changed')
        if not callable(active)or type(target)is not int or not 1<=target<=500:
            raise ContractError('explicit_native_acquisition_target_required')
        from .write_scope import NativeWriteScope
        from background_jobs.draft_intake import DraftIntake
        if (not isinstance(native_scope,NativeWriteScope)or native_scope.kind!='background'or native_scope.phase!='source'or
            native_scope.lifecycle.db_path!=store.lifecycle.db_path):
            raise ContractError('actual_native_acquisition_parent_scope_required')
        parent=json.loads(store.row(native_scope.identity['job_id'])['contract'])
        request=parent.get('intake_request_id')
        if not request:raise ContractError('native_acquisition_parent_intake_required')
        intake=DraftIntake(store,policy)
        original_active=active
        def parent_active():
            original_active();native_scope.check();current=intake.current(request)
            if current['recipe']!=recipe or current['main_decision']['args']['target']!=target:
                raise ContractError('native_acquisition_recipe_or_target_changed')
        parent_active();active=parent_active
        if (policy.get('enabled')is not True or policy.get('outreach_profile_enabled')is not True or
            policy.get('account')!=recipe['account']or recipe['project']not in policy.get('categories',[])):
            raise ContractError('outreach_staging_profile_not_activated')
        if self.pool_source is None:
            return self.from_existing(store,policy,native_budget=self.native_budget).acquire_pool(recipe,store,policy,active,target=target,native_scope=native_scope)
        if any(v is None for v in (self.acquisition,self.protections,self.templates)):
            raise ContractError('native_complete_acquisition_adapters_required')
        smtp=self.smtp
        if smtp is None and self.native_budget is not None:
            from .smtp import SMTPOriginals
            def smtp_scope(identity,candidate,check):
                parent_active()
                if identity['item']['account']!=native_scope.account or identity['item']['category']!=recipe['project']:
                    raise ContractError('native_acquisition_candidate_account_changed')
                return native_scope
            smtp=SMTPOriginals(store.root,native_budget=self.native_budget,scope_factory=smtp_scope)
        owner=policy['owner'];active()
        selection=self.pool_source.capture(recipe['name'],active)
        binding={'recipe':recipe,'owner':owner,'target':target,
                 'selection_manifest_sha256':selection['source_manifest_sha256'],
                 'selection_manifest':selection['source_manifest']}
        protection=self.protections.capture(binding,active)
        central=json.loads(protection[0]['content'])['original']
        if selection['source_manifest'].get('central_source_version')and selection['source_manifest']['central_source_version']!={
            k:central[k]for k in ('project','account','revision','entry_sha256')}:
            raise ContractError('central_selection_source_version_changed')
        template_originals,template=self.templates.capture(binding,active)
        original=approved_path(store.root,selection['source_manifest']['native_original_ref']).read_bytes()
        if digest(original)!=selection['source_manifest']['native_original_sha256']:
            raise ContractError('native_full_pool_original_changed')
        packet=json.loads(original);records=packet['original']['records'];allowed=[];excluded=[]
        binding['external_originals']=[{'ref':selection['source_manifest']['native_original_ref'],
                                      'sha256':selection['source_manifest']['native_original_sha256']},
                                     {'ref':protection[1],'sha256':protection[0]['sha256']}]
        if central.get('contract')=='outreach-central-sources-v1':
            for entry in central['changes']:
                ref={'ref':entry['ref'],'sha256':entry['sha256']}
                # One immutable raw original, never copied into every item.
                if digest(approved_path(store.root,ref['ref']).read_bytes())!=ref['sha256']:
                    raise ContractError('central_source_original_changed')
                if ref not in binding['external_originals']:binding['external_originals'].append(ref)
        blocked=exclusions(protection[0])
        for candidate in selection['candidates']:
            company=records['company'][candidate['company_id']]
            if permits(candidate,company['name'],protection[0],blocked=blocked):allowed.append(candidate)
            else:excluded.append({'item_id':candidate['item_id'],'reason':'authoritative_protection_collision'})
        # A small explicit replacement reserve, not all permitted firms deeply
        # probed for target=1. The full unacquired pool remains in the resolution.
        reserve=min(5,max(1,target));limit=min(500,target+reserve)
        chosen=[];items=[];examined=[]
        # Complete 36+ MB pool/history originals are preserved and byte-bound,
        # not needlessly sent again to each firm-specific quality judgement.
        from .sources import persist
        membership=persist(store.root,'crm_pool','crm-complete-selection',
            {'complete_original':binding['external_originals'][0],'source_counts':selection['source_manifest']['source_counts'],
             'objects':selection['source_manifest']['objects'],'eligible_item_ids':[c['item_id']for c in allowed]},
            binding,'twenty.full-pool.deterministic-membership')
        protection_check=persist(store.root,'protections','acribic-protection-membership',
            {'complete_original':binding['external_originals'][1],'excluded':excluded},binding,
            'authoritative-protections.deterministic-membership')
        common=[membership[0],protection_check[0],*[d for d,_ in template_originals]]
        source_root=Path(policy['sources_root'])
        catalog=policy.get('reviewed_templates')
        if not isinstance(catalog,dict)or not catalog:raise ContractError('known_sent_positive_control_required')
        for candidate in allowed:
            if len(chosen)>=limit:break
            family_original=persist(store.root,'crm_family','twenty-family:'+candidate['item_id'],
                {'complete_pool_original':binding['external_originals'][0],'records':related_originals(records,candidate)},
                {'candidate':candidate,**binding},'twenty.complete-relevant-family-originals')
            acquired=self.acquisition.capture_page([candidate],catalog,source_root,owner,active=active,
                common_documents=common+[family_original[0]],ted=self.ted,smtp=smtp,company_sources=self.company_sources,source_binding=binding)
            item=acquired['manifest']['items'][0]
            raw=approved_path(source_root,item['source_ref']).read_bytes();bundle=json.loads(raw)
            if digest(raw)!=item['source_hash']:raise ContractError('native_acquired_source_changed')
            missing=bundle['missing_native_sources']
            examined.append({'candidate':candidate,'item':item,'missing_native_sources':missing,
                             'factual_errors':bundle['candidate_factual_errors']})
            if not missing:chosen.append(candidate);items.append(item)
            # An unavailable common adapter is an actual access/engineering
            # blocker, not evidence that the whole permitted pool is exhausted.
            if smtp is None or self.ted is None or self.company_sources is None:break
        manifest={'contract':'background-sources-v1','items':items}
        named={'contract':'hermes-named-draft-pool-v1','name':recipe['name'],'project':recipe['project'],
               'account':recipe['account'],'candidates':chosen,'source_manifest_sha256':digest(manifest)}
        resolution={'contract':'hermes-native-outreach-pool-resolution-v1','recipe':recipe,'owner':owner,'target':target,
            'selection':selection,'protection_original_ref':protection[1],'protection_original_sha256':protection[0]['sha256'],
            'template_originals':[{'ref':r,'sha256':d['sha256']}for d,r in template_originals],
            'approved_template_sha256':template['sha256'],'eligible_after_protections':allowed,
            'excluded_by_current_protections':excluded,'acquired':examined,'replacement_reserve':reserve,
            'unacquired_item_ids':[c['item_id']for c in allowed if c['item_id']not in {e['candidate']['item_id']for e in examined}],
            'named_pool':named,'source_manifest':manifest,'originals_complete':len(chosen)>=target,
            'draft_inputs_ready':False,'shared_write_authority_ready':False,'external_business_writes':0}
        ref='outreach-pool-resolutions/'+digest(resolution)+'.json';sha=_save(store.root,ref,resolution)
        if len(chosen)<target:
            reason='outreach_native_sources_incomplete'if any(e['missing_native_sources']for e in examined)else 'outreach_permitted_pool_insufficient'
            raise ContractError(reason+':'+ref)
        manifest_ref=digest(manifest)+'.json';_save(Path(policy['manifests_root']),manifest_ref,manifest)
        active()
        return {'named_pool':named,'source_manifest':manifest,'resolution_ref':ref,'resolution_sha256':sha}

    def current_readiness(self,store,policy,active):
        from .readiness import AcribicReadiness
        from .smtp import SMTPOriginals
        if self.acquisition is None:raise ContractError('native_complete_acquisition_adapters_required')
        return AcribicReadiness(store,self.acquisition.gmail,self.acquisition.twenty,policy['reviewed_templates'],
            pool_source=self.pool_source,protection_source=self.protections,template_source=self.templates,
            smtp=self.smtp or SMTPOriginals(store.root,native_budget=self.native_budget),active=active)


def describe_pool(name):return HostDomainFactory.describe_pool(name)


def acquire_pool(recipe,store,policy,active,*,target,native_scope):
    return HostDomainFactory().acquire_pool(recipe,store,policy,active,target=target,native_scope=native_scope)


def native_sales_factory(store,policy,*,current_admission=None):
    from .binding import factory
    return factory(store,policy,current_admission=current_admission)
