"""Native pre-create failures enter repair; uncertain writes never replay."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), '/home/hermes/hermes-extensions-staging/brain/package/src']
from self_repair import sources, recover
from self_repair.policy import check_source, PolicyError
from self_repair.store import Incidents
from self_repair.publish import observer_binding
from self_repair.policy import digest
import configparser
import pytest

def failed_case():
    return dict(account='code',message_id='incoming',thread_id='thread',source_hash='source',
        contract='mail-workflow-20261022-business-sources-1',phase='review_required',
        prepared_body='prepared but not written',prepared_slots=['2026-11-02T12:00:00+01:00'],
        outcome=dict(status='workflow_blocked',review_reason='reservation_unavailable'),
        original_thread=[dict(id='incoming',labels=[],text='Bitte neue Termine nächste Woche.')])

def test_legacy_case_is_captured_once_even_without_error_code(tmp_path):
    p=tmp_path/'email_watcher/cases';p.mkdir(parents=True)
    (p/'case.json').write_text(json.dumps(failed_case()))
    store=Incidents(tmp_path/'repair')
    assert sources.capture(store,home=tmp_path,targets={}) == 1
    assert sources.capture(store,home=tmp_path,targets={}) == 0
    incident=store.pending()[0]
    assert incident['code']=='scheduling_prewrite_failed'
    snapshot=json.loads(Path(incident['sources'][0]['snapshot_path']).read_text())
    assert snapshot['case']['prepared_body']=='prepared but not written'
    assert len(store.pending())==1

@pytest.mark.parametrize('status,phase,proof', [
    ('needs_manual_review_draft_state','review_required',False),
    ('workflow_blocked','draft_write_pending',False),
    ('workflow_blocked','review_required',True)])
def test_unknown_write_is_not_captured_or_replayed(tmp_path,status,phase,proof):
    case=failed_case();case.update(phase=phase,error_code='mail_text_validation')
    case['outcome']['status']=status
    case['draft_failure']=dict(contract='mail-prewrite-failure-v1',gmail_create_started=proof,code='mail_text_validation')
    assert sources.prewrite_failure(case) is None
    runtime=SimpleNamespace(case_path=lambda _:tmp_path/'case.json',read_case=lambda _:case,_draft_one=Mock())
    with patch.dict(sys.modules,{'email_realtime':runtime}):
        assert recover.mail(dict(item={},case=copy.deepcopy(case)),'repair')['state']=='needs_write_readback'
    runtime._draft_one.assert_not_called()

def test_safe_prewrite_recovery_verifies_latest_then_reuses_success(tmp_path):
    import mail_outcomes as standard
    import mail_result_io as result_io
    import mail_policy as policy
    (tmp_path/'scripts').mkdir();(tmp_path/'scripts/mail_policy.py').write_text('reviewed fix')
    case=failed_case()
    case['original_thread'][0]['from']='Sarah <sarah@example.invalid>'
    snapshot=dict(item=dict(account='code',id='incoming',threadId='thread'),case=copy.deepcopy(case))
    slot=case['prepared_slots'][0]
    body=policy.label(slot)+' passt mir gut.\n\nBeste Grüße\nOle Beispiel'
    receipt=standard.assess({'thread':case['original_thread'],'draft':body},lambda *_:{
        'reply_required':True,'scheduling_required':True,'result':'ready',
        'source_quote':case['original_thread'][-1]['text'],'issues':[]})
    def draft(_):
        assert case['prepared_body'] is None
        case.update(phase='completed',standard_verified=True,
            standard_attempts=[{'candidate':{'body':body,'proposed_slots':[slot]},'receipt':receipt}],
            outcome=dict(status='created',draft_id='draft-one',notify=True,appointment=True,
                notification_summary='bittet um neue Termine'))
        return case['outcome']
    def read(*_,**kw):
        return dict(state='current',thread=case['original_thread'],draft=body,
            owned_draft={'draft_id':'draft-one'},hard_facts={
                'saved_in_gmail':True,'source_bound':True,'single_draft':True,
                'calendar_required':True,'calendar_verified':True,'hard_errors':[]})
    runtime=SimpleNamespace(case_path=lambda _:tmp_path/'case.json',read_case=lambda _:dict(case),
        remember_case=lambda _,**kw:case.update(kw),get_thread=Mock(return_value=(case['original_thread'],False)),
        _draft_one=Mock(side_effect=draft),reply_signature=lambda _,text:text,
        notice_party=lambda *_:'Sarah',notify=Mock(return_value=dict(message_id='one')))
    from mail_fixtures import runtime as mail_runtime
    runtime.queue_draft_notification=mail_runtime(tmp_path).queue_draft_notification
    with patch.object(recover,'LIVE',tmp_path),patch.dict(sys.modules,{'email_realtime':runtime}),\
            patch.object(result_io,'current',side_effect=read):
        assert recover.mail(snapshot,'repair')['state']=='restored'
        assert recover.mail(snapshot,'repair')['reused'] is True
    assert runtime.get_thread.call_count==runtime._draft_one.call_count==runtime.notify.call_count==1
    assert runtime.notify.call_args.args==('Sarah bittet um neue Termine. Der Entwurf ist bereit.',)

def test_other_prepared_state_is_never_proof_of_safe_replay(tmp_path):
    case=failed_case();case['outcome']['review_reason']='other'
    assert sources.prewrite_failure(case) is None

def test_format_scope_never_allows_calendar_or_claim_changes():
    rel='gtm/calendar/runtime/mail_policy.py'
    before='def format_body(text):\n    return text.strip()\ndef scheduling():\n    return 1\n'
    assert check_source(rel,before,before.replace('text.strip()', 'text.strip()+"."'))==['format_body']
    with pytest.raises(PolicyError):check_source(rel,before,before.replace('return 1','return 2'))

@pytest.mark.parametrize('foreign_drift',[False,True])
def test_publisher_updates_only_exact_reviewed_dependency_without_changing_business_grants(tmp_path,foreign_drift):
    home=tmp_path/'home'; config=home/'ole-reply-observer/reply-observer.ini';config.parent.mkdir(parents=True)
    source=tmp_path/'mail_policy.py';source.write_text('old reviewed code')
    other=tmp_path/'mail_workflow.py';other.write_text('unrelated reviewed code')
    deps=[dict(path=str(p),sha256=digest(p)) for p in (source,other)]
    review=dict(contract='ole-acribic-reply-observer-review-v1',revision='R2',runtime_files=[],
                active_dependency_variants=dict(baseline=deps))
    manifest=tmp_path/'review.json';manifest.write_text(json.dumps(review))
    cfg=configparser.ConfigParser(interpolation=None)
    cfg['reply_observer']=dict(review_manifest=str(manifest),review_manifest_sha256=digest(manifest),
        package_root=str(tmp_path),active_dependencies=json.dumps(deps),crm_enabled='false',invitation_enabled='false')
    with config.open('w') as f:cfg.write(f)
    before=config.read_bytes();transaction=tmp_path/'transaction';transaction.mkdir()
    if foreign_drift:other.write_text('unreviewed foreign change')
    planned=[dict(target=str(source),before=digest(source),after='a'*64)]
    if foreign_drift:
        with pytest.raises(PolicyError,match='observer_dependency_drift'):
            observer_binding(home,transaction,planned)
    else:
        change=observer_binding(home,transaction,planned)
        rewritten=configparser.ConfigParser(interpolation=None);rewritten.read(change['candidate'])
        section=rewritten['reply_observer']
        assert not section.getboolean('crm_enabled') and not section.getboolean('invitation_enabled')
        installed=json.loads(section['active_dependencies'])
        assert installed==[{**deps[0],'sha256':'a'*64},deps[1]]
        assert digest(section['review_manifest'])==section['review_manifest_sha256']
    assert config.read_bytes()==before
