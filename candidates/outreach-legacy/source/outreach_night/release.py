"""No automatic publication/activation. Separate exact release review gate."""
import json
from pathlib import Path
from background_jobs.contracts import ContractError,digest

BACKGROUND_INIT_SHA='91858f74e01fb93c4f6b93bd763954800e2c8e352871df784e5e03bf69a477ca'
BACKGROUND_INIT_MANIFEST_SHA='5599d017b67713ca26888d1c11d0a6a80961869208b04b70c9f73bca5973a61d'


def require_background_init():
    import background_jobs
    ref=getattr(background_jobs,'__file__',None)
    if not ref or Path(ref).is_symlink() or digest(Path(ref).read_bytes())!=BACKGROUND_INIT_SHA:
        raise ContractError('reviewed_background_lazy_init_required')


def checked_release(policy):
    ref=Path(policy.get('outreach_release_manifest',''))
    if not ref.is_absolute() or not ref.is_file() or ref.is_symlink():raise ContractError('outreach_release_review_required')
    raw=ref.read_bytes()
    if digest(raw)!=policy.get('outreach_release_sha256'):raise ContractError('outreach_release_changed')
    release=json.loads(raw)
    if (release.get('contract')!='outreach-native-staging-release-v1' or release.get('native_profile_review')!='approved' or
        release.get('fresh_source_gate_review')!='approved' or release.get('pool_dispatch_review')!='approved' or
        release.get('required_background_lazy_init_release_sha256')!=BACKGROUND_INIT_SHA or
        release.get('required_background_lazy_init_manifest_sha256')!=BACKGROUND_INIT_MANIFEST_SHA or
        release.get('production_enabled') is not True):
        raise ContractError('outreach_native_source_profile_review_required')
    require_background_init()
    return release
