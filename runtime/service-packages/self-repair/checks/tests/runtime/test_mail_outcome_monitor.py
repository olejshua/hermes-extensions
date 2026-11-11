"""Coverage, semantic outcomes, durable work and no identical model loops."""
import copy
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'second-brain/src'),str(ROOT/'gtm/calendar/runtime')]
from self_repair.outcomes import Monitor
from self_repair.store import Incidents
from self_repair.mail_probe import original_failure
import mail_outcomes as standard

NOW=1793271600
ITEM={'account':'code','id':'incoming','threadId':'thread'}
TEXT='Könnten Sie bitte neue Termine vorschlagen?'

def receipt(result='missing',scheduling=True):
    return {'contract':standard.CONTRACT,'review':dict(reply_required=result!='not_required',
        scheduling_required=scheduling,result=result,source_quote=TEXT,
        issues=[]if result in{'ready','not_required'}else[dict(criterion='missing_draft',
            source_quote=TEXT,draft_quote=None,explanation='Kein fertiger Antwortentwurf.')])}

def setup(tmp_path,result='missing'):
    db=Incidents(tmp_path/'repair')
    facts={'state':'current','thread':[dict(id='incoming',text=TEXT,thread_id='thread',internalDate=str(NOW*1000))],
           'case':{},'draft':None,'hard_facts':{},'owned_draft':None}
    reads=SimpleNamespace(standard=standard,facts=Mock(side_effect=lambda _:copy.deepcopy(facts)),
        review=Mock(return_value=receipt(result)),discover=Mock(return_value={}))
    monitor=Monitor(db,reads=reads,home=tmp_path,targets={},clock=lambda:NOW)
    monitor.store.observe(ITEM,NOW-600)
    return monitor,db,facts,reads

def check(monitor,now=NOW):
    rows=monitor.store.pending(now)
    assert rows
    monitor.check(rows[0],now)

def test_missing_mail_without_error_record_is_durable_repair(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    check(m)
    incident=db.pending()[0]
    assert incident['code']=='reply_result_missing'
    source=json.loads(Path(incident['sources'][0]['snapshot_path']).read_bytes())
    assert source['case']=={}and source['original_thread']==facts['thread']
    check(m,NOW+900)
    assert len(db.pending())==1 and reads.review.call_count==1
    assert original_failure(source,standard)['result']=='missing'

def test_false_completed_no_draft_does_not_hide_missing_result(tmp_path):
    m,db,facts,_=setup(tmp_path)
    facts['case']={'phase':'completed','outcome':{'status':'no_draft_needed'}}
    check(m)
    assert db.pending()[0]['code']=='reply_result_missing'

def test_already_answered_source_closes_without_model_or_repair(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    facts['state']='superseded';check(m)
    reads.review.assert_not_called();assert not db.pending()

def test_closed_threads_and_luma_do_not_spend_model_work(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    reads.runtime=SimpleNamespace(CLOSED_THREADS={'code':{'thread'}})
    check(m);reads.facts.assert_not_called();reads.review.assert_not_called()

def test_owner_lock_is_respected(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    path=tmp_path/'email_watcher/cases/code-incoming.lock';path.parent.mkdir(parents=True)
    with path.open('a')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);check(m)
    reads.facts.assert_not_called();reads.review.assert_not_called();assert not db.pending()

def test_one_good_saved_result_closes_and_never_rechecks_unchanged(tmp_path):
    m,db,facts,reads=setup(tmp_path,'ready')
    facts.update(draft='Gerne am geprüften Termin.',hard_facts={'calendar_verified':True,'hard_errors':[]})
    check(m)
    assert not m.store.pending(NOW+86400)and not db.pending()

def test_calendar_claim_without_real_evidence_cannot_pass(tmp_path):
    m,db,facts,reads=setup(tmp_path,'ready')
    facts.update(draft='Der Termin passt.',owned_draft={'draft_id':'draft'},hard_facts={'calendar_verified':False,'hard_errors':[]})
    check(m)
    assert db.pending()[0]['code']=='reply_standard_failed'

def test_other_writers_draft_is_evidence_but_never_auto_overwrite(tmp_path):
    m,db,facts,reads=setup(tmp_path,'revise')
    facts['draft']='Vielen Dank.';check(m)
    assert db.pending()[0]['code']=='reply_existing_draft_needs_review'

def test_approval_for_invitation_does_not_suppress_permitted_reply_check(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    facts['case']={'pending_decision':{'action':'create_invitation','authorized':False}}
    check(m);assert db.pending()[0]['code']=='reply_result_missing'

def test_uncertain_model_dispatch_is_diagnosed_without_another_model_call(tmp_path):
    m,db,facts,reads=setup(tmp_path)
    reads.review.side_effect=TimeoutError('private credentials must not leak')
    check(m);check(m,NOW+1800)
    assert reads.review.call_count==1
    assert len(db.pending())==1
    snapshot=Path(db.pending()[0]['sources'][0]['snapshot_path']).read_text()
    assert 'private credentials'not in snapshot

def test_gmail_coverage_preserves_partial_cursor_and_healthy_account(tmp_path):
    m,db,_,reads=setup(tmp_path)
    def discover(account,since,until,token):
        if account=='stealth':raise RuntimeError('invalid_grant')
        return {'messages':[{'id':'unseen','threadId':'other'}],'nextPageToken':'next'}
    reads.discover.side_effect=discover;m.coverage(NOW)
    with db.lifecycle.connect()as c:
        code=dict(c.execute("select * from mail_outcome_coverage where account='code'").fetchone())
        stealth=dict(c.execute("select * from mail_outcome_coverage where account='stealth'").fetchone())
        assert c.execute("select 1 from mail_outcome_sources where message_id='unseen'").fetchone()
    assert code['watermark']==NOW-86400 and code['upper_bound']==NOW and code['page_token']=='next'
    assert stealth['watermark']==NOW-86400 and stealth['page_token']==''

def test_finished_incident_gets_new_source_in_new_queue_batch(tmp_path):
    db=Incidents(tmp_path)
    args=dict(component='mail',code='gap',versions={},snapshot={})
    first=db.record(source_key='first',**args)
    job=db.lifecycle.start_job(first['job_id'],owner='owner',lease_seconds=120)
    fence=dict(owner='owner',lease_generation=job['lease_generation'])
    db.lifecycle.begin_verification(first['job_id'],**fence)
    db.lifecycle.complete_job(first['job_id'],verification_evidence='actual result verified',**fence)
    second=db.record(source_key='second',**args)
    third=db.record(source_key='third',**args)
    assert first['signature']!=second['signature']==third['signature']
    assert len(db.get(first['signature'])['sources'])==1
    assert len(db.pending())==1 and len(db.pending()[0]['sources'])==2
    assert db.record(source_key='first',**args)['signature']==first['signature']

def test_started_bundle_is_sealed_and_same_cause_guard_still_applies(tmp_path):
    db=Incidents(tmp_path)
    args=dict(component='mail',code='gap',versions={},snapshot={})
    first=db.record(source_key='first',**args)
    db.dispatch(first['job_id'],'build:1',seconds=60)
    db.lifecycle.start_job(first['job_id'],owner='owner',lease_seconds=120)
    second=db.record(source_key='second',**args)
    assert first['signature']!=second['signature']
    assert db.dispatch(second['job_id'],'build:1',seconds=60)['code']=='same_cause_paused'

def test_native_semantic_proof_rejects_fabricated_baseline():
    value=receipt();value['review']['source_quote']='Erfundene Frage'
    snapshot={'original_thread':[{'text':TEXT}],'expectation':{'receipt':value,'checked_draft':None}}
    with pytest.raises(standard.OutcomeReviewError):original_failure(snapshot,standard)

def test_measured_time_budget_defers_work_without_losing_source(tmp_path):
    m,db,_,reads=setup(tmp_path)
    with db.lifecycle.connect()as c:
        c.execute('INSERT INTO mail_outcome_model_runs(input_key,started,finished,seconds)VALUES(?,?,?,?)',
                  ('prior-work',NOW-60,NOW-1,1200))
    check(m);reads.review.assert_not_called()
    rows=m.store.pending(NOW+900)
    assert rows[0]['state']=='review_budget_wait'and not db.pending()

def test_actual_time_releases_unused_reservation_instead_of_fixed_attempt_count(tmp_path):
    m,_,_,_=setup(tmp_path)
    for i in range(12):
        assert m.store.reserve_review('different-'+str(i),NOW)=='reserved'
        m.store.finish_review('different-'+str(i),NOW+1,1)

def test_unknown_saved_write_is_reconciled_instead_of_silently_marked_done(tmp_path):
    m,db,facts,reads=setup(tmp_path,'ready')
    facts.update(draft='Geprüfte Antwort.',hard_facts={'calendar_verified':True,'hard_errors':[]},
                 case={'phase':'draft_write_pending','prepared_body':'Geprüfte Antwort.'})
    check(m)
    assert db.pending()[0]['code']=='reply_standard_failed'

def test_blocked_conversation_does_not_freeze_first_recovery_of_another(tmp_path):
    db=Incidents(tmp_path);args=dict(component='mail',code='reply_standard_failed',versions={},snapshot={})
    first=db.record(source_key='first',**args)
    db.dispatch(first['job_id'],'recovery:1',seconds=60)
    db.guard.finish(first['job_id'],'recovery:1','source_recovery_needs_review')
    db.lifecycle.start_job(first['job_id'],owner='owner',lease_seconds=120)
    second=db.record(source_key='second',**args)
    assert db.dispatch(second['job_id'],'recovery:1',seconds=60)['allowed']

def test_resolved_sources_are_preserved_but_not_given_to_next_builder(tmp_path):
    from self_repair.service import Engine
    db=Incidents(tmp_path);args=dict(component='mail',code='gap',versions={},snapshot={})
    first=db.record(source_key='resolved',**args);second=db.record(source_key='unresolved',**args)
    with db.lifecycle.connect()as c:c.execute("UPDATE repair_sources SET outcome='restored'WHERE source_key='resolved'")
    engine=Engine.__new__(Engine);engine.root=tmp_path;engine.store=db;engine._work=Mock(return_value={})
    engine.work(second)
    assert [s['source_key']for s in engine._work.call_args.args[0]['sources']]==['unresolved']
    assert len(db.get(first['signature'])['sources'])==2
