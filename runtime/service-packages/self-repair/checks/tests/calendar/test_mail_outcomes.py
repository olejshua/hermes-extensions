"""Actual preparation boundary, including semantic false-success detection."""
import copy
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest
from mail_fixtures import runtime,message,interpretation

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gtm/calendar/runtime'))
from mail_outcomes import assess,validate_review,OutcomeReviewError

def verdict(text,result='ready',*,scheduling=False,draft=None):
    issue={'criterion':'missing_draft'if result=='missing'else'missing_answer',
           'source_quote':text,'draft_quote':draft,'explanation':'Die konkrete Frage bleibt unbeantwortet.'}
    return dict(reply_required=result!='not_required',scheduling_required=scheduling,
        result=result,source_quote=text,issues=[issue]if result in{'missing','revise','needs_user'}else[])

def test_judge_must_use_exact_original_and_actual_draft_evidence():
    text='Bitte schlagen Sie neue Termine vor.'
    for change in [dict(source_quote='made up'),dict(issues=[dict(criterion='missing_answer',
                   source_quote=text,draft_quote='not in draft',explanation='gap')]),dict(result='ready')]:
        value={**verdict(text,'missing'),**change}
        with pytest.raises(OutcomeReviewError):validate_review(value,text,None)

def test_identical_review_can_reuse_but_changed_source_cannot():
    context={'thread':[{'text':'Was ist der nächste Schritt?'}],'draft':'Ich recherchiere die Anbieter.'}
    model=Mock(return_value=verdict(context['thread'][0]['text']))
    receipt=assess(context,model)
    assert assess(context,model,cached=receipt)==receipt
    assert model.call_count==1
    assess({**context,'draft':'Ich recherchiere die passenden Anbieter.'},model,cached=receipt)
    assert model.call_count==2

def test_inconsistent_judge_answer_gets_one_specific_schema_correction():
    context={'thread':[{'text':'Was ist der nächste Schritt?'}],'draft':'Ich recherchiere die Anbieter.'}
    invalid={**verdict(context['thread'][0]['text']),'reply_required':False}
    model=Mock(side_effect=[invalid,verdict(context['thread'][0]['text'])])
    receipt=assess(context,model)
    assert receipt['review']['result']=='ready'and len(receipt['corrected_validation'])==1
    assert model.call_args_list[1].args[1]['review_validation_feedback']['value']==invalid

def test_repeated_contradiction_preserves_original_outputs_without_loop():
    context={'thread':[{'text':'Was ist der nächste Schritt?'}],'draft':'Ich recherchiere die Anbieter.'}
    invalid={**verdict(context['thread'][0]['text']),'reply_required':False}
    model=Mock(return_value=invalid)
    with pytest.raises(OutcomeReviewError)as error:assess(context,model)
    assert model.call_count==2 and len(error.value.rejected)==2

@pytest.mark.parametrize('text',['','\r\n'])
def test_empty_automatic_notice_can_use_actual_subject_without_false_repair(text):
    subject='Angenommen: Austausch zu öffentlichen Ausschreibungen'
    context={'thread':[{'text':text,'subject':subject}],'draft':None}
    model=Mock(return_value=verdict(subject,'not_required'))
    assert assess(context,model)['review']['result']=='not_required'
    assert model.call_count==1

def test_empty_mail_cannot_gain_fabricated_content_or_reply_order():
    context={'thread':[{'text':'','subject':''}],'draft':None}
    model=Mock(return_value=verdict('','not_required'))
    assert assess(context,model)['review']['result']=='not_required'
    model=Mock(return_value=verdict('Invented question','missing'))
    with pytest.raises(OutcomeReviewError):assess(context,model)

def test_silent_no_reply_is_corrected_once_before_gmail_write(tmp_path):
    r=runtime(tmp_path);row,thread=message('Welche Schritte wollen Sie automatisieren?','stealth')
    r.get_thread=Mock(return_value=(thread,False));phases=[]
    def model(phase,context):
        phases.append(phase)
        second=phases.count('understand')>1
        if phase=='understand':
            if second:assert context['outcome_feedback']['review']['result']=='missing'
            return interpretation(reply=second,notify=second)
        if phase=='compose':return dict(body='Ich möchte die Recherche zu passenden Ausschreibungen vereinfachen.',
                                        question=None,proposed_slots=[],referral_recipients=[])
        return verdict(thread[-1]['text'],'ready'if second else'missing')
    r.model_json=Mock(side_effect=model)
    assert r.draft_one(row)['status']=='created'
    assert phases==['understand','review','understand','compose','review']
    assert r.w.make_draft.call_count==1
    assert len(r.read_case(row)['standard_attempts'])==2

def test_partial_reply_gets_concrete_feedback_instead_of_rewrite_loop(tmp_path):
    r=runtime(tmp_path);row,thread=message('Was automatisieren Sie, und wie nutzen Sie die Erkenntnisse?','stealth')
    r.get_thread=Mock(return_value=(thread,False));count=0
    def model(phase,context):
        nonlocal count
        if phase=='understand':return interpretation()
        if phase=='compose':
            count+=1
            return dict(body='Ich vereinfache die Recherche.'+(' Die Erkenntnisse bestimmen meine nächsten Prototypentests.'if count==2 else''),
                        question=None,proposed_slots=[],referral_recipients=[])
        return verdict(thread[-1]['text'],'revise'if count==1 else'ready',draft='Ich vereinfache die Recherche.'if count==1 else None)
    r.model_json=Mock(side_effect=model)
    assert r.draft_one(row)['status']=='created'
    assert count==2 and 'Erkenntnisse' in r.w.make_draft.call_args.args[2]

def test_persistent_gap_stops_without_writing_and_preserves_both_judgments(tmp_path):
    r=runtime(tmp_path);row,thread=message('Wie verwenden Sie die Erkenntnisse?','stealth')
    r.get_thread=Mock(return_value=(thread,False))
    def model(phase,context):
        if phase=='understand':return interpretation()
        if phase=='compose':return dict(body='Vielen Dank für die Rückmeldung.',question=None,proposed_slots=[],referral_recipients=[])
        return verdict(thread[-1]['text'],'revise',draft='Vielen Dank für die Rückmeldung.')
    r.model_json=Mock(side_effect=model)
    with pytest.raises(r.MailWorkflowError,match='reply_standard_failed'):r.draft_one(row)
    r.w.make_draft.assert_not_called()
    assert len(r.read_case(row)['standard_attempts'])==2
    assert not r.read_case(row)['standard_verified']

def test_real_missing_decision_remains_targeted_question_without_draft(tmp_path):
    r=runtime(tmp_path);row,thread=message('Zu welchem Preis bieten Sie das an?','stealth')
    r.get_thread=Mock(return_value=(thread,False))
    def model(phase,context):
        if phase=='understand':return interpretation()
        if phase=='compose':return dict(body=None,question='Welchen Preis möchtest du nennen?',proposed_slots=[],referral_recipients=[])
        return verdict(thread[-1]['text'],'needs_user')
    r.model_json=Mock(side_effect=model)
    assert r.draft_one(row)['status']=='needs_user_input'
    r.w.make_draft.assert_not_called()

