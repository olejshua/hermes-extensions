"""Pure source fixes reach all callers without weakening their identity guards."""
import ast
import configparser
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'second-brain/src')]
from self_repair.publish import project_source_mirrors,PROJECT_SOURCE,observer_binding,idle
from self_repair.policy import digest,PolicyError

SOURCE='''CODE="protected account"
def owned_business_source(authored):
    return authored.startswith("research")
def project_from_thread(message):
    return message["verified_sender"] and owned_business_source(message["text"])
'''
FIXED=SOURCE.replace('authored.startswith("research")','authored.startswith("research") or authored.startswith("our demo")')

def test_active_mail_phase_blocks_publication_even_with_empty_queue(tmp_path):
    path=tmp_path/'email_watcher/realtime.json';path.parent.mkdir()
    path.write_text(json.dumps(dict(active={'message_id':'currently-working'},queue=[],notifications=[])))
    assert idle(tmp_path)is False

def test_publication_updates_only_the_shared_interpreter_in_different_callers(tmp_path):
    before=tmp_path/'before';work=tmp_path/'work';tx=tmp_path/'transaction';tx.mkdir()
    for directory,text in((before,SOURCE),(work,FIXED)):
        p=directory/PROJECT_SOURCE;p.parent.mkdir(parents=True);p.write_text(text)
    first=tmp_path/'first.py';first.write_text(SOURCE)
    second=tmp_path/'second.py';text=SOURCE.replace('message["verified_sender"]','message["verified_sender"] and message["verified_rfc"]');second.write_text(text)
    rows=project_source_mirrors(work,before,tx,mirrors=[(first,None),(second,None)])
    assert len(rows)==2 and first.read_text()==SOURCE and second.read_text()==text
    def protected(source):return[ast.dump(n,include_attributes=False)for n in ast.parse(source).body if getattr(n,'name',None)!='owned_business_source']
    for row,original in zip(rows,[SOURCE,text]):
        candidate=Path(row['candidate']).read_text()
        assert 'our demo'in candidate and protected(candidate)==protected(original)
        assert row['before']==digest(row['target'])

def test_foreign_helper_change_blocks_mirroring_without_overwrite(tmp_path):
    before=tmp_path/'before';work=tmp_path/'work';tx=tmp_path/'transaction';tx.mkdir()
    for directory,text in((before,SOURCE),(work,FIXED)):
        p=directory/PROJECT_SOURCE;p.parent.mkdir(parents=True);p.write_text(text)
    mirror=tmp_path/'mirror.py';mirror.write_text(SOURCE.replace('research','foreign rule'))
    with pytest.raises(PolicyError,match='mirror_diverged'):
        project_source_mirrors(work,before,tx,mirrors=[(mirror,None)])
    assert 'foreign rule'in mirror.read_text()

def test_review_binding_covers_changed_internal_source_and_preserves_permissions(tmp_path):
    home=tmp_path/'home';cfgpath=home/'ole-reply-observer/reply-observer.ini';cfgpath.parent.mkdir(parents=True)
    external=tmp_path/'mail-policy.py';external.write_text('old external')
    package=tmp_path/'package';internal=package/'gtm/calendar/routing.py';internal.parent.mkdir(parents=True);internal.write_text(SOURCE)
    dependencies=[dict(path=str(external),sha256=digest(external))]
    review=dict(contract='ole-acribic-reply-observer-review-v1',revision='R2',
        active_dependency_variants=dict(baseline=dependencies),runtime_files=[dict(path='gtm/calendar/routing.py',sha256=digest(internal))])
    manifest=tmp_path/'review.json';manifest.write_text(json.dumps(review))
    cfg=configparser.ConfigParser(interpolation=None);cfg['reply_observer']=dict(package_root=str(package),
        review_manifest=str(manifest),review_manifest_sha256=digest(manifest),active_dependencies=json.dumps(dependencies),
        crm_enabled='false',invitation_enabled='false')
    with cfgpath.open('w')as f:cfg.write(f)
    tx=tmp_path/'transaction';tx.mkdir()
    changes=[dict(target=str(p),before=digest(p),after=('a'if p==external else'b')*64)for p in(external,internal)]
    result=observer_binding(home,tx,changes)
    updated=configparser.ConfigParser(interpolation=None);updated.read(result['candidate']);s=updated['reply_observer']
    new=json.loads(Path(s['review_manifest']).read_bytes())
    assert new['runtime_files'][0]['sha256']=='b'*64
    assert not s.getboolean('crm_enabled')and not s.getboolean('invitation_enabled')
    assert internal.read_text()==SOURCE
