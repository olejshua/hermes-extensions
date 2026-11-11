"""Journaled reviewed-code publisher; native business stores are never restored."""
from __future__ import annotations
import json
import ast
import configparser
import io
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from self_repair.policy import digest,changed_files,check_source,PolicyError
from self_repair.sources import LIVE,TARGETS,PAYLOAD
from self_repair.store import private_json

class Busy(RuntimeError):pass

def ctl(*args):
    r=subprocess.run(['systemctl','--user',*args],capture_output=True,text=True,timeout=60)
    if r.returncode and args[0]!='is-active':raise RuntimeError('service_control_failed')
    return r.stdout.strip()

def stop_confirmed(control,units):
    """Never replace files while any old or newly started process may run."""
    try:control('stop',*units)
    except Exception:pass  # A timed-out stop is decided by process readback.
    for unit in units:
        try:
            text=control('show',unit,'--property=LoadState,ActiveState,MainPID,ControlPID')
            fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        except Exception:raise PolicyError('service_quiescence_unproven') from None
        if (fields.get('LoadState')!='loaded' or fields.get('ActiveState') not in {'inactive','failed'}
                or fields.get('MainPID')!='0' or fields.get('ControlPID')!='0'):
            raise PolicyError('service_quiescence_unproven')

def running_processes(control,units):
    result={}
    for unit in units:
        text=control('show',unit,'--property=ActiveState,MainPID,ExecMainStartTimestampMonotonic')
        fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if (fields.get('ActiveState')!='active' or not fields.get('MainPID','').isdigit()
                or int(fields['MainPID'])<=0 or not fields.get('ExecMainStartTimestampMonotonic','').isdigit()
                or int(fields['ExecMainStartTimestampMonotonic'])<=0):
            raise PolicyError('service_start_unconfirmed')
        result[unit]=fields
    return result

def active_record(journal,*,control=ctl):
    r=json.loads(Path(journal).read_text())
    if r['phase']!='active':raise PolicyError('publication_not_active')
    if any(Path(x['target']).is_symlink() or digest(x['target'])!=x['after'] for x in r['files']):
        raise PolicyError('published_source_changed')
    running_processes(control,r['units'])
    return r

def rollback(record,journal,control):
    # Start may have succeeded before its acknowledgement was lost. Explicitly
    # stop and prove no process remains BEFORE touching even our own code.
    try:stop_confirmed(control,record['units'])
    except Exception:
        record['phase']='runtime_state_unknown';private_json(journal,record)
        raise
    for row in record['files']:
        target,backup=Path(row['target']),Path(row['backup'])
        if target.is_symlink() or backup.is_symlink() or digest(backup)!=row['before']:
            record['phase']='foreign_write_requires_review';private_json(journal,record)
            raise PolicyError('foreign_write_requires_review')
        if digest(target) not in {row['before'],row['after']}:
            record['phase']='foreign_write_requires_review';private_json(journal,record)
            # Preserve newer source bytes; restore normal service availability.
            control('start',*record['units'])
            raise PolicyError('foreign_write_requires_review')
    record['phase']='rolling_back';private_json(journal,record)
    for row in reversed(record['files']):
        target=Path(row['target'])
        if digest(target)==row['after']:private_replace(target,Path(row['backup']).read_bytes())
    record['phase']='rolled_back';private_json(journal,record)
    control('start',*record['units'])
    if any(control('is-active',u)!='active' for u in record['units']):
        raise PolicyError('rollback_service_not_active')
    return record

def idle(home=LIVE):
    state=json.loads((home/'email_watcher/realtime.json').read_text())
    if state.get('active')or state.get('queue') or state.get('notifications'):return False
    with sqlite3.connect('file:'+str(home/'gtm/briefings/briefings.sqlite3')+'?mode=ro',uri=True) as c:
        return not c.execute("SELECT 1 FROM jobs WHERE state IN ('claimed','model_attempted','send_attempted') AND updated>? LIMIT 1",(time.time()-240,)).fetchone()

def observer_binding(home, transaction, planned):
    """Advance only reviewed dependencies; unrelated drift blocks publication."""
    path = Path(home)/'ole-reply-observer/reply-observer.ini'
    if not path.exists(): return None
    cfg = configparser.ConfigParser(interpolation=None); cfg.read(path)
    section = cfg['reply_observer']
    dependencies = json.loads(section['active_dependencies'])
    changes = {r['target']: r for r in planned}
    affected = [r for r in dependencies if r['path'] in changes]
    if not affected: return None
    manifest_path = Path(section['review_manifest'])
    if manifest_path.is_symlink() or digest(manifest_path) != section['review_manifest_sha256']:
        raise PolicyError('observer_review_manifest_changed')
    review = json.loads(manifest_path.read_bytes())
    if review.get('contract') != 'ole-acribic-reply-observer-review-v1' or review.get('revision') != 'R2':
        raise PolicyError('observer_review_contract_changed')
    variant = 'model_reuse' if section.getboolean('model_reuse_enabled', fallback=False) else 'baseline'
    if dependencies != review['active_dependency_variants'][variant]:
        raise PolicyError('observer_dependency_binding_changed')
    for dependency in dependencies:
        dependency_path = Path(dependency['path'])
        if dependency_path.is_symlink() or digest(dependency_path) != dependency['sha256']:
            raise PolicyError('observer_dependency_drift')
        change = changes.get(dependency['path'])
        if change and change['before'] != dependency['sha256']:
            raise PolicyError('observer_reviewed_source_mismatch')
    package = Path(section['package_root'])
    for row in review['runtime_files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise PolicyError('observer_runtime_path_changed')
        source = package/relative
        if source.is_symlink() or digest(source) != row['sha256']:
            raise PolicyError('observer_runtime_source_changed')
        change=changes.get(str(source))
        if change:
            if change['before']!=row['sha256']:raise PolicyError('observer_reviewed_source_mismatch')
            row['sha256']=change['after']
    dependencies = [{**r, 'sha256': changes[r['path']]['after']} if r['path'] in changes else r
                    for r in dependencies]
    review['active_dependency_variants'][variant] = dependencies
    replacement = Path(transaction)/'observer-review.json'
    private_json(replacement, review)
    section['review_manifest'] = str(replacement)
    section['review_manifest_sha256'] = digest(replacement)
    section['active_dependencies'] = json.dumps(dependencies, separators=(',', ':'))
    rendered = io.StringIO(); cfg.write(rendered)
    candidate = Path(transaction)/'observer-config.ini'
    candidate.write_text(rendered.getvalue()); candidate.chmod(0o600)
    backup = Path(transaction)/'observer-config.before'
    backup.write_bytes(path.read_bytes()); backup.chmod(0o600)
    return {'relative': None, 'target': str(path), 'backup': str(backup),
            'before': digest(path), 'after': digest(candidate), 'candidate': str(candidate)}

PROJECT_SOURCE='gtm/calendar/runtime/calendar_routing.py'

def project_source_mirrors(work,before,transaction,*,home=LIVE,mirrors=None):
    """Copy only the reviewed pure interpreter; preserve every caller's guards."""
    def function(text):
        nodes=[n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef)and n.name=='owned_business_source']
        if len(nodes)!=1:raise PolicyError('business_source_interpreter_missing')
        return nodes[0]
    previous=(Path(before)/PROJECT_SOURCE).read_text();updated=(Path(work)/PROJECT_SOURCE).read_text()
    oldfn,newfn=function(previous),function(updated)
    replacement=''.join(updated.splitlines(True)[newfn.lineno-1:newfn.end_lineno])
    if mirrors is None:
        cfg=configparser.ConfigParser(interpolation=None);cfg.read(Path(home)/'ole-reply-observer/reply-observer.ini')
        package=Path(cfg['reply_observer']['package_root'])
        if not package.is_relative_to(Path(home)/'ole-reply-observer/releases'):
            raise PolicyError('business_source_package_scope_changed')
        mirrors=[(PAYLOAD/'gtm/calendar/routing.py',Path(before)/'gtm/calendar/routing.py'),
                 (package/'gtm/calendar/routing.py',None),
                 (Path(home)/'gtm/cleanup-runtime/gtm/calendar/routing.py',None)]
    rows=[]
    for index,(path,baseline)in enumerate(mirrors):
        path=Path(path)
        if path.is_symlink()or not path.is_file():raise PolicyError('business_source_mirror_missing')
        if baseline is not None and digest(path)!=digest(baseline):raise PolicyError('business_source_mirror_changed')
        text=path.read_text();node=function(text)
        if ast.dump(node,include_attributes=False)!=ast.dump(oldfn,include_attributes=False):
            raise PolicyError('business_source_mirror_diverged')
        lines=text.splitlines(True);lines[node.lineno-1:node.end_lineno]=[replacement]
        candidate_text=''.join(lines)
        check_source(PROJECT_SOURCE,text,candidate_text)
        backup=Path(transaction)/('project-source-'+str(index)+'.before')
        candidate=Path(transaction)/('project-source-'+str(index)+'.py')
        backup.write_text(text);backup.chmod(0o600);candidate.write_text(candidate_text);candidate.chmod(0o600)
        rows.append(dict(relative=None,target=str(path),backup=str(backup),candidate=str(candidate),
                         before=digest(backup),after=digest(candidate)))
    return rows

def install(work,before,transaction,*,targets=TARGETS,home=LIVE,control=ctl,is_idle=idle):
    work,before,transaction=map(Path,(work,before,transaction))
    changed=changed_files(before,work)
    transaction.mkdir(parents=True,exist_ok=True,mode=0o700)
    journal=transaction/'publication.json'
    if journal.exists():
        old=json.loads(journal.read_text())
        if old['phase']=='active':return active_record(journal,control=control)
        raise PolicyError('publication_needs_reconciliation')
    for rel in changed:
        if digest(targets[rel])!=digest(before/rel):raise PolicyError('installed_source_changed')
    if not is_idle(home):raise Busy('business_work_in_progress')
    units=[]
    if any('/calendar/runtime/' in r for r in changed):units.append('hermes-email-realtime.service')
    if any('/calendar/runtime/' not in r for r in changed):units.append('ole-gtm-briefings.service')
    if any(control('is-active',u)!='active' for u in units):raise Busy('service_not_in_active_state')
    rows=[]
    for rel in changed:
        backup=transaction/(Path(rel).name+'.before')
        backup.write_bytes(targets[rel].read_bytes());backup.chmod(0o600)
        rows.append({'relative':rel,'target':str(targets[rel]),'backup':str(backup),
                     'before':digest(backup),'after':digest(work/rel)})
    if PROJECT_SOURCE in changed:
        rows.extend(project_source_mirrors(work,before,transaction,home=home))
        if 'ole-gtm-briefings.service'not in units:units.append('ole-gtm-briefings.service')
        if control('is-active','ole-gtm-briefings.service')!='active':raise Busy('briefing_not_in_active_state')
    binding = observer_binding(home, transaction, rows)
    if binding:
        units.append('hermes-acribic-reply-observer.service')
        if control('is-active', units[-1]) != 'active': raise Busy('observer_not_in_active_state')
        rows.append(binding)
    record={'phase':'prepared','files':rows,'units':units,'installed':[]}
    private_json(journal,record)
    try:
        stop_confirmed(control,units)
        if not is_idle(home):raise Busy('work_arrived_during_drain')
        for row in rows:
            target=Path(row['target'])
            if target.is_symlink() or digest(target)!=row['before']:raise PolicyError('installed_source_changed')
            candidate = Path(row['candidate']) if row.get('candidate') else work/row['relative']
            private_replace(target,candidate.read_bytes())
            record['installed'].append(row['relative']);record['phase']='applying';private_json(journal,record)
        if any(digest(Path(r['target']))!=r['after'] for r in rows):raise PolicyError('publication_readback_mismatch')
        record['phase']='starting';private_json(journal,record)
        control('start',*units)
        record['processes']=running_processes(control,units)
        record['phase']='active';private_json(journal,record)
        return record
    except BaseException as error:
        if record['phase']=='starting':
            try:
                processes=running_processes(control,units)
                if any(digest(r['target'])!=r['after'] for r in rows):raise PolicyError('publication_readback_mismatch')
            except Exception:pass
            else:
                # Positive readback wins over a lost start acknowledgement.
                record.update(phase='active',processes=processes,start_ack_reconciled=True)
                private_json(journal,record)
                return record
        # Only restore our exact replaced code. A foreign writer never loses its
        # newer bytes, and mail/calendar/CRM/state databases are never copied back.
        rollback(record,journal,control)
        raise

def private_replace(path,data):
    path=Path(path);temp=path.with_name('.'+path.name+'.repair-'+str(os.getpid()))
    with temp.open('xb') as f:
        os.fchmod(f.fileno(),path.stat().st_mode & 0o777)
        f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def reconcile(journal,*,control=ctl):
    """After interruption, roll back a partial publication before another build."""
    journal=Path(journal);r=json.loads(journal.read_text())
    if r['phase']=='active':return active_record(journal,control=control)
    if r['phase']=='rolled_back':return r
    if r['phase']=='starting' and all(digest(x['target'])==x['after'] for x in r['files']):
        try:processes=running_processes(control,r['units'])
        except Exception:pass
        else:
            r.update(phase='active',processes=processes,start_ack_reconciled=True);private_json(journal,r)
            return r
    return rollback(r,journal,control)
