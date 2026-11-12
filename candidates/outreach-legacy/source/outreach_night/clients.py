"""Pinned imports of the existing native auth/client routes. Disabled by default.

Only these known public helper names may be imported; outreach_final is never
imported because its actual public copy executes operational top-level code.
"""
import hashlib
import importlib
from pathlib import Path
import sys
from background_jobs.contracts import ContractError


def reviewed_module(path,pins):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or pins.get(str(path))!=hashlib.sha256(path.read_bytes()).hexdigest():
        raise ContractError('native_module_not_reviewed')
    return path


def load_existing(directory,name,pins):
    allowed={'email_watcher','google_auth_store','mail_policy','google_api','notion_twenty_migration'}
    if name not in allowed:raise ContractError('native_module_not_allowed')
    directory=Path(directory);path=reviewed_module(directory/(name+'.py'),pins)
    # Import dependencies only from existing reviewed directories. The caller's
    # manifest must pin every transitively importable protected helper there.
    sys.path.insert(0,str(directory))
    try:
        module=importlib.import_module(name)
        if Path(module.__file__).resolve()!=path.resolve():raise ContractError('native_module_origin_changed')
        return module
    finally:sys.path.pop(0)


def existing_gmail(policy,category):
    if policy.get('enabled') is not True or policy.get('outreach_profile_enabled') is not True:raise ContractError('outreach_staging_profile_not_activated')
    return _gmail_route(policy,category)


def _gmail_route(policy,category):
    from background_jobs.outreach_candidate import ACCOUNTS
    if category not in ACCOUNTS:raise ContractError('project_account_mismatch')
    directory=Path(policy['existing_scripts_dir']);pins=policy['active_dependencies']
    # Both routes are the actual reviewed host helpers, no tokens are copied.
    load_existing(directory,'google_auth_store',pins)
    watcher=load_existing(directory,'email_watcher',pins)
    return watcher.service(ACCOUNTS[category][0])


def existing_owned_gmail(policy,store,identity):
    """Existing auth only for a previously attempted exact owned draft phase."""
    from .recovery import phase
    native=phase(store,identity)
    if not native or not store.attempted(native):raise ContractError('owned_draft_native_attempt_required')
    import json
    batch=json.loads(store.row(identity['job_id'])['contract'])
    if batch['profile']=='fixture-outreach'and policy.get('backend')!='fake':
        raise ContractError('fixture_profile_not_native_sales')
    return _gmail_route(policy,identity['item']['category'])


def existing_code_calendar(policy):
    if policy.get('enabled') is not True or policy.get('outreach_profile_enabled') is not True:raise ContractError('outreach_staging_profile_not_activated')
    # google_api imports these actual helper routes. Pin before any import/auth.
    reviewed_module(Path(policy['existing_scripts_dir'])/'google_auth_store.py',policy['active_dependencies'])
    reviewed_module(Path(policy['existing_google_dir'])/'_hermes_home.py',policy['active_dependencies'])
    module=load_existing(policy['existing_google_dir'],'google_api',policy['active_dependencies'])
    expected_home=Path(policy['hermes_home']).resolve()
    if Path(module.TOKEN_PATH).resolve() not in {expected_home/'google_token_code.json',expected_home/'google_token.json'}:
        raise ContractError('calendar_code_auth_route_required')
    # Actual google_api uses the existing account-bound CODE token path.
    return module.build_service('calendar','v3')


def existing_twenty(policy):
    if policy.get('enabled') is not True or policy.get('outreach_profile_enabled') is not True:raise ContractError('outreach_staging_profile_not_activated')
    helper=load_existing(policy['existing_helpers_dir'],'notion_twenty_migration',policy['active_dependencies'])
    reviewed_module(Path(__file__).resolve().parents[1]/'gtm'/'twenty.py',policy['active_dependencies'])
    from gtm import twenty
    reviewed_module(twenty.__file__,policy['active_dependencies'])
    client=twenty.ExistingHelpersClient(Path(policy['existing_helpers_dir']),Path(policy['existing_twenty_key']))
    if client.helper is not helper:raise ContractError('native_helper_origin_changed')
    return client
