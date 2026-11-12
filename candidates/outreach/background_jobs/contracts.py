"""Immutable source and scope contracts. Inputs contain private content, never telemetry."""
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re

class ContractError(ValueError): pass
class TemporaryFailure(Exception): pass
class PermanentFailure(Exception): pass
class UncertainWrite(Exception): pass

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()
def token(value, limit=128):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:@.\-/]{1,'+str(limit)+'}', value):
        raise ContractError('invalid_identifier')
    return value

def private_dir(path):
    p = Path(path).absolute()
    for part in (p, *p.parents):
        if part.is_symlink(): raise ContractError('symlink_path')
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.stat().st_uid != os.getuid(): raise ContractError('foreign_path_owner')
    p.chmod(0o700)
    return p

def private_file(path, body):
    p = Path(path)
    private_dir(p.parent)
    if p.is_symlink(): raise ContractError('symlink_path')
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(body); f.flush(); os.fsync(f.fileno())
    return p

def approved_path(root, ref):
    token(ref, 256)
    relative = Path(ref)
    if relative.is_absolute() or '..' in relative.parts: raise ContractError('relative_source_required')
    p = Path(root) / relative
    for part in (p, *p.parents):
        if part.is_symlink(): raise ContractError('symlink_path')
    p = p.resolve()
    if not p.is_relative_to(Path(root).resolve()) or not p.is_file(): raise ContractError('unapproved_source')
    if p.stat().st_uid != os.getuid(): raise ContractError('foreign_source_owner')
    if p.name.startswith('.env') or p.name in {'auth.json', 'credentials.json'}: raise ContractError('credential_source')
    return p

@dataclass(frozen=True)
class Item:
    item_id: str
    source_ref: str
    source_version: int
    source_hash: str
    owner: str
    account: str
    category: str
    def checked(self, policy):
        token(self.item_id); token(self.source_ref, 256)
        if type(self.source_version) is not int or self.source_version < 1: raise ContractError('positive_source_version_required')
        if not re.fullmatch(r'[a-f0-9]{64}', self.source_hash): raise ContractError('source_sha_required')
        if self.owner != policy['owner'] or self.account != policy['account'] or self.category not in policy['categories']:
            raise ContractError('source_binding_mismatch')
        accounts={'acribic':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}
        if self.category in accounts and self.account!=accounts[self.category]:raise ContractError('project_account_mismatch')
        return self
    def fields(self): return asdict(self)

PROFILES = {'readonly-research': ('source', 'analysis', 'artifact'),
            'fixture-outreach': ('source', 'eligibility', 'draft', 'crm', 'readback'),
            # Phase vocabulary does not activate either staging profile. Native
            # policy below remains readonly; private process tests use fake SaaS.
            'outreach-acquire-staging-v1': ('source',),
            'outreach-drafts-staging-v1': ('source', 'analysis', 'draft', 'readback')}

def checked_policy(policy, *, fixture=False, verify_runtime=True):
    if policy.get('contract') != 'ole-background-v1': raise ContractError('reviewed_policy_required')
    if policy.get('enabled') is not True: raise ContractError('background_not_activated')
    token(policy['owner']); token(policy['account'])
    if not isinstance(policy.get('categories'), list) or not policy['categories']: raise ContractError('categories_required')
    if not policy.get('sources_root') or not Path(policy['sources_root']).is_absolute(): raise ContractError('absolute_sources_root_required')
    if not policy.get('manifests_root') or not Path(policy['manifests_root']).is_absolute(): raise ContractError('absolute_manifests_root_required')
    if policy.get('backend') not in ({'fake'} if fixture else {'native'}): raise ContractError('explicit_backend_required')
    if not 1 <= policy.get('max_workers', 0) <= 2: raise ContractError('maximum_two_workers')
    if not 1 <= policy.get('per_item_attempts', 0) <= 20: raise ContractError('per_item_attempt_budget_required')
    if not 1 <= policy.get('per_item_iterations', 0) <= 300: raise ContractError('per_item_iteration_budget_required')
    allowed = {'readonly-research', 'fixture-outreach'} if fixture else {'readonly-research'}
    if fixture and policy.get('private_outreach_runtime') is True:
        allowed |= {'outreach-acquire-staging-v1', 'outreach-drafts-staging-v1'}
    native_outreach={'outreach-acquire-staging-v1','outreach-drafts-staging-v1'}
    if not fixture and native_outreach.intersection(policy.get('profiles',[])):
        # Existing reviewed profile gate; this grants no mail write capability.
        # Control/readback construction survives drift. Fresh submit/dispatch
        # rechecks the actual host release and all existing runtime pins below.
        if verify_runtime:
            import sys
            package=Path(policy.get('outreach_package',''))
            if not package.is_absolute()or package.is_symlink()or not package.is_dir():
                raise ContractError('native_outreach_package_required')
            if str(package)not in sys.path:sys.path.insert(0,str(package))
            from outreach_night.release import checked_release
            from outreach_night import host_runtime
            from outreach_night.host_runtime import registered_paths,background_paths
            if Path(host_runtime.__file__).resolve().parents[1]!=package:
                raise ContractError('native_outreach_package_changed')
            checked_release(policy)
            release,_,_,_=registered_paths();database,_=background_paths(release)
            if ('outreach'not in release['writers']['supported_routes']or
                policy.get('hermes_home')!=release['home']or
                database!=Path(release['home'])/'ole-background/state/state.sqlite3'):
                raise ContractError('native_outreach_host_release_required')
        allowed |= native_outreach
    if not set(policy.get('profiles', [])) <= allowed or not policy.get('profiles'): raise ContractError('unreviewed_profile')
    if not fixture:
        if policy.get('driver')!='systemd':raise ContractError('supervised_native_service_required')
        slot_root=policy.get('slot_root')
        if not slot_root or not Path(slot_root).is_absolute() or os.environ.get('HERMES_CHILD_SLOT_ROOT')!=slot_root:
            raise ContractError('shared_native_slot_environment_required')
        core=Path(policy.get('core','/home/hermes/.hermes/hermes-agent'))
        home=Path(policy.get('hermes_home','/home/hermes/.hermes'))
        paths={'native_limiter':core/'tools/child_slot_limiter.py','native_delegate':core/'tools/delegate_tool.py',
               'native_child':core/'tools/delegate_tool_child_run.py','coding_worker':home/'plugins/ole-coding/worker.py',
               'coding_native':home/'plugins/ole-coding/native_worker.py'}
        delivery_paths={'capture':core/'agent/served_model.py','registry':core/'hermes_cli/plugins.py','dispatch':core/'hermes_cli/plugins_dispatch.py'}
        for key,components,error in [('global_slot_review',paths,'global_native_coding_slot_review_required'),
                                     ('source_delivery_review',delivery_paths,'native_source_delivery_review_required')]:
            reviewed=policy.get(key,{})
            if not isinstance(reviewed,dict) or set(reviewed)!=set(components):raise ContractError(error)
            if any(not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value) for value in reviewed.values()):raise ContractError(error)
            if verify_runtime:
                for name,path in components.items():
                    if not path.is_file() or path.is_symlink() or digest(path.read_bytes())!=reviewed[name]:
                        raise ContractError('global_slot_implementation_changed' if key=='global_slot_review' else 'source_delivery_implementation_changed')
    return policy

def file_digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(262144),b''):h.update(chunk)
    return h.hexdigest()

def acquire_original(store,policy,identity,active):
    """Full original acquisition is a fenced background item phase, never Main intake."""
    import codecs
    import uuid
    item=identity['item'];target=store.snapshot_path(identity['job_id'],item['item_id'])
    private_dir(target.parent)
    if target.exists():
        if target.is_symlink() or file_digest(target)!=item['source_hash']:raise PermanentFailure('snapshot_corrupt')
        return target
    source=approved_path(policy['sources_root'],item['source_ref'])
    limit=policy.get('max_source_bytes',16_000_000)
    if source.stat().st_size>limit:raise PermanentFailure('source_too_large')
    temporary=target.with_name(target.name+'.tmp-'+uuid.uuid4().hex)
    # Own prior incomplete acquisition files can never masquerade as originals.
    for old in target.parent.glob(target.name+'.tmp-*'):
        if not old.is_symlink():old.unlink(missing_ok=True)
    input_fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
    output_fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    h=hashlib.sha256();decoder=codecs.getincrementaldecoder('utf-8')();size=0
    try:
        with os.fdopen(input_fd,'rb') as reader,os.fdopen(output_fd,'wb') as writer:
            while True:
                active();chunk=reader.read(262144)
                if not chunk:break
                size+=len(chunk)
                if size>limit:raise PermanentFailure('source_too_large')
                decoder.decode(chunk);h.update(chunk);writer.write(chunk)
            decoder.decode(b'',final=True)
            if h.hexdigest()!=item['source_hash']:raise PermanentFailure('source_changed')
            writer.flush();os.fsync(writer.fileno())
        active()
        try:os.link(temporary,target,follow_symlinks=False)
        except FileExistsError:
            if target.is_symlink() or file_digest(target)!=item['source_hash']:raise ContractError('snapshot_conflict')
        directory=os.open(target.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return target
    finally:temporary.unlink(missing_ok=True)
