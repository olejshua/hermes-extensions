#!/usr/bin/env python3
"""One bounded interpretation/composition on the existing native subscription.

No mail/calendar tools, shell, file tools, global memories or production session.
The private home uses existing committed dependencies; credentials stay in RAM.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

CORE = Path('/home/hermes/.hermes/hermes-agent')
sys.path.insert(0, str(CORE))
import hermes_bootstrap  # Select the reviewed committed native dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mail_workflow import UNDERSTAND, COMPOSE, effort_for_phase
from mail_outcomes import REVIEW
from mail_model_status import safe_failure

WORK_STAGE = 'input'


def main():
    global WORK_STAGE
    packet = json.loads(sys.stdin.read(700000))
    if not isinstance(packet, dict) or packet.get('phase') not in {'understand', 'compose', 'review'}:
        raise ValueError('mail_model_phase_invalid')
    from hermes_constants import get_hermes_home
    from hermes_cli.auth import resolve_codex_runtime_credentials
    WORK_STAGE = 'credentials'
    original_home = Path(get_hermes_home())
    runtime = resolve_codex_runtime_credentials(refresh_skew_seconds=150)
    if (runtime.get('provider') != 'openai-codex' or runtime.get('auth_mode') != 'chatgpt'
            or runtime.get('base_url', '').rstrip('/') != 'https://chatgpt.com/backend-api/codex'):
        raise ValueError('existing_subscription_route_required')
    dependency_key = hashlib.sha256(str(CORE.resolve()).encode()).hexdigest()[:16]
    installed = original_home / 'installs' / dependency_key
    if not (installed / 'facts.json').is_file():
        raise ValueError('committed_native_dependencies_required')
    with tempfile.TemporaryDirectory(prefix='hermes-mail-model-') as directory:
        WORK_STAGE = 'setup'
        home = Path(directory)
        os.chmod(home, 0o700)
        dep = home / 'installs' / dependency_key
        dep.mkdir(parents=True, mode=0o700)
        (dep / 'facts.json').write_bytes((installed / 'facts.json').read_bytes())
        (dep / 'environments').symlink_to((installed / 'environments').resolve(), target_is_directory=True)
        (home / 'config.yaml').write_text(
            'plugins:\n  enabled: []\nagent:\n  api_max_retries: 1\n'
            'auxiliary:\n  title_generation:\n    enabled: false\ncompression:\n  enabled: false\n')
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        os.environ.pop('HERMES_REQUIRED_SESSION_POLICY', None)
        from hermes_cli.plugins import discover_plugins
        discover_plugins(force=True)
        from run_agent import AIAgent
        from agent.aux_provider_policy import allowed_auxiliary_providers
        system = {'understand': UNDERSTAND, 'compose': COMPOSE, 'review': REVIEW}[packet['phase']]
        # Optional reviewed CRM instruction is trusted host code, not model output.
        if packet['phase'] == 'compose' and packet.get('crm_instruction'):
            system += '\n' + packet['crm_instruction']
        with allowed_auxiliary_providers({'openai-codex'}), contextlib.redirect_stdout(sys.stderr):
            agent = AIAgent(model='gpt-6.1-sol', provider='openai-codex', api_mode='codex_responses',
                            api_key=runtime['api_key'], base_url=runtime['base_url'],
                            reasoning_config={'effort': effort_for_phase(packet['phase'], packet.get('effort'))}, max_tokens=6000,
                            enabled_toolsets=[], max_iterations=2, quiet_mode=True,
                            save_trajectories=False, skip_memory=True, skip_context_files=True,
                            load_soul_identity=False, skip_background_review=True, side_agent=True,
                            fallback_model=None, credential_pool=None, run_budget_seconds=150,
                            ephemeral_system_prompt=system)
            if agent.tools:
                raise ValueError('empty_native_tool_surface_required')
            agent.valid_tool_names = set()
            agent._persist_disabled = True
            WORK_STAGE = 'inference'
            try:
                result = agent.run_conversation(json.dumps(packet['context'], ensure_ascii=False))
            finally:
                if hasattr(agent, 'close'):
                    agent.close()
            if isinstance(result, dict) and result.get('error'):
                raise ValueError('native_mail_model_failed')
            output = (result.get('final_response') or result.get('response')) if isinstance(result, dict) else result
        if not isinstance(output, str) or len(output) > 25000:
            raise ValueError('mail_model_output_invalid')
        WORK_STAGE = 'output'
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError('mail_model_output_invalid')
        print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    original_home = os.environ.get('HERMES_HOME')
    try:
        main()
    except Exception as error:
        # The isolated model home has already closed. Status must use the same
        # original account, never mistake its temporary home for lost login.
        if original_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = original_home
        print(json.dumps(safe_failure(error, stage=WORK_STAGE)), file=sys.stderr)
        raise SystemExit(1)
