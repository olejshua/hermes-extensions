"""Build from installed sources and pinned external tests, preserving every original."""
from __future__ import annotations
import json
import ast
import shutil
from pathlib import Path
from self_repair.policy import EDITABLE,digest,PolicyError
from self_repair.sources import LIVE,PAYLOAD,TARGETS
from self_repair.store import private_json,hash_value

PYTHON=Path('/home/hermes/hermes-telegram-durable-20261023/activation/canonical-ci/requirements-build/testenv/bin/python')
RUNTIME=Path('/home/hermes/.hermes/tools')
TESTS=['tests/calendar/test_mail_routing.py','tests/calendar/test_mail_notifications.py',
       'tests/calendar/mail_fixtures.py','tests/runtime/test_briefing_daemon.py','tests/runtime/test_gtm_briefing.py',
       'tests/calendar/test_scheduling_prewrite.py','tests/calendar/test_mail_outcomes.py',
       'tests/calendar/test_business_project_sources.py','tests/calendar/test_mail_availability_bounds.py']
PYTEST_ARGS=['-q',*TESTS[:2],*TESTS[3:],'-k','not actual_native_empty_tools and not fresh_isolated_subprocess',
             '--basetemp=.tmp/pytest','-o','cache_dir=.tmp/pytest-cache']

def build_bundle(root,incident,checks,*,targets=TARGETS):
    root,checks=Path(root),Path(checks);work=root/'work';before=root/'before'
    if before.exists() and work.exists():return work,before
    if work.exists():raise PolicyError('partial_bundle_requires_review')
    for rel,path in targets.items():
        if digest(path)!=incident['versions'][rel]:raise PolicyError('installed_source_changed')
    work.mkdir(parents=True,mode=0o700)
    def copy_py(source,target):
        for path in sorted(Path(source).rglob('*.py')):
            if '__pycache__' in path.parts:continue
            if path.is_symlink():raise PolicyError('installed_source_symlink')
            dest=target/path.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,dest)
    copy_py(PAYLOAD,work)
    scripts=script_dependencies(LIVE/'scripts',{'email_realtime','email_watcher','mail_workflow','mail_policy','calendar_lookup',
                                             'mail_model_worker','mail_result_io','mail_outcomes'})
    for path in scripts:
        # Auth stores themselves never enter the job. Preserve the actual local
        # Python dependency closure, rather than a guessed list of helper names.
        dest=work/'gtm/calendar/runtime'/path.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    for rel,path in targets.items():
        dest=work/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    for rel in TESTS:
        dest=work/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(checks/rel,dest)
    originals=[]
    for row in incident['sources']:
        p=Path(row['snapshot_path']);value=json.loads(p.read_text())
        dest=Path('originals')/(hash_value(row['source_key'])+'.json')
        private_json(work/dest,value)
        originals.append({'path':str(dest),'kind':value['kind']})
    first=json.loads((work/originals[0]['path']).read_bytes())if originals else{}
    attempts=(first.get('case')or{}).get('standard_attempts')or[]
    receipt=(first.get('expectation')or{}).get('receipt')or(attempts[-1].get('receipt')if attempts else{})or{}
    private_json(work/'incident.json',{'component':incident['component'],'code':incident['code'],'originals':originals,
        'native_semantic_proof':incident['component']=='mail'and receipt.get('review',{}).get('result')in{'missing','revise'},
        'reviewed_reflections':bound_reflections(root.parent.parent,incident,targets=targets)})
    shutil.copy2(checks/'AGENTS.md',work/'AGENTS.md')
    private_json(root/'bundle-manifest.json',{'files':{str(p.relative_to(work)):digest(p) for p in work.rglob('*') if p.is_file()},
                                           'originals_hash':hash_value(originals)})
    shutil.copytree(work,before)
    return work,before

def bound_reflections(controller_root,incident,*,targets=TARGETS):
    """Advisory provenance joins this frozen bundle, never executable control."""
    versions={rel:digest(path)for rel,path in targets.items()}
    if versions!=incident['versions']:return []
    sources={s['source_key']:s for s in incident['sources']}
    found=[]
    for path in sorted((Path(controller_root)/'proposal-links').glob('*.json')):
        if path.is_symlink()or path.stat().st_size>32768:continue
        link=json.loads(path.read_bytes())
        source=sources.get(link.get('source_key'))
        if (link.get('contract')!='dream-mail-repair-link/v1'or link.get('signature')!=incident['signature']
                or link.get('versions')!=versions or not source
                or link.get('source_snapshot_sha256')!=digest(source['snapshot_path'])
                or link.get('action_authorized')is not False or link.get('executed')is not False):continue
        found.append({key:link[key]for key in ('item_id','item_sha256','source_key','versions',
            'source_snapshot_sha256','relationship','advisory','action_authorized','executed')})
        if len(found)==3:break
    return found


def script_dependencies(root,seeds):
    root=Path(root);todo=list(seeds);seen=set();result=[]
    while todo:
        name=todo.pop()
        if name in seen:continue
        seen.add(name);path=root/(name+'.py')
        if not path.is_file():continue
        if path.is_symlink():raise PolicyError('installed_source_symlink')
        result.append(path)
        for node in ast.walk(ast.parse(path.read_text())):
            names=([x.name.split('.')[0] for x in node.names] if isinstance(node,ast.Import)
                   else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module and not node.level else [])
            todo.extend(x for x in names if (root/(x+'.py')).is_file())
    return result

def fingerprint(work):
    work=Path(work)
    return hash_value({r:digest(work/r) for r in EDITABLE})

def bundle_fingerprint(root):
    root=Path(root)
    return hash_value({str(p.relative_to(root)):digest(p) for p in root.rglob('*')
                       if p.is_file() and not any(x in {'.tmp','.pytest_cache','__pycache__'} for x in p.relative_to(root).parts)})

def immutable_bundle(directory,before,work):
    manifest=json.loads((Path(directory)/'bundle-manifest.json').read_text())['files']
    for root,candidate in ((Path(before),False),(Path(work),True)):
        actual={str(p.relative_to(root)):digest(p) for p in root.rglob('*')
                if p.is_file() and not any(x in {'.tmp','.pytest_cache','__pycache__'} for x in p.relative_to(root).parts)}
        expected={r:h for r,h in manifest.items() if not candidate or r not in EDITABLE}
        found={r:h for r,h in actual.items() if r!='repro.py' and (not candidate or r not in EDITABLE)}
        if found!=expected:raise PolicyError('immutable_original_bundle_changed')
        if (root/'.tmp').is_symlink():raise PolicyError('sandbox_temporary_symlink')

def regression(codex,work):
    program='import sys;sys.path.insert(0,".");import pytest;raise SystemExit(pytest.main('+repr(PYTEST_ARGS)+'))'
    expected=bundle_fingerprint(work)
    r=codex.command(work,[str(PYTHON),'-B','-c',program],readonly=True,timeout=180)
    if bundle_fingerprint(work)!=expected:raise PolicyError('immutable_bundle_changed_during_tests')
    if r.returncode:
        private_json(Path(work).parent/'gate-failure.json',{'code':'independent_regression_failed','details':(r.stdout+r.stderr)[-14000:]})
        raise PolicyError('independent_regression_failed')
    return {'returncode':r.returncode,'output_tail':r.stdout[-1200:],'candidate':fingerprint(work)}

def original_probe(codex,work,before):
    """A new reproduction must fail on the untouched baseline and pass on the patch."""
    work,before=Path(work),Path(before)
    p=work/'repro.py'
    if not p.is_file() or p.is_symlink():raise PolicyError('original_reproduction_missing')
    # Keep the candidate's exact proof program for both executions. Neither can
    # access production; the baseline is temporarily a private writable root.
    old=before/'repro.py';old.write_bytes(p.read_bytes());old.chmod(0o600)
    original_hash,candidate_hash=bundle_fingerprint(before),bundle_fingerprint(work)
    a=codex.command(before,['/usr/bin/python3','-B','repro.py'],readonly=True,timeout=90)
    b=codex.command(work,['/usr/bin/python3','-B','repro.py'],readonly=True,timeout=90)
    if (bundle_fingerprint(before),bundle_fingerprint(work))!=(original_hash,candidate_hash):
        raise PolicyError('immutable_bundle_changed_during_proof')
    if a.returncode==0 or b.returncode!=0:
        private_json(work.parent/'gate-failure.json',{'code':'original_not_proven_red_to_green','candidate':fingerprint(work),
            'details':{'check':'original_reproduction','baseline_exit':a.returncode,'candidate_exit':b.returncode,
                       'baseline_output':(a.stdout+a.stderr)[-3000:],'candidate_output':(b.stdout+b.stderr)[-3000:]}})
        raise PolicyError('original_not_proven_red_to_green')
    return {'baseline_failed':True,'candidate_passed':True,'proof_sha256':digest(p)}
