import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def runtime(home):
    def atomic(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    watcher = types.SimpleNamespace(HOME=home, _atomic_json=atomic,
        SchedulingDraftReviewRequired=type('SchedulingDraftReviewRequired', (ValueError,), {}),
        DraftStateUnverified=type('DraftStateUnverified', (RuntimeError,), {}),
        SourceChangedBeforeDraft=type('SourceChangedBeforeDraft', (RuntimeError,), {}),
        make_draft=Mock(return_value={'status':'created','draftId':'private-draft','to':'person@example.invalid'}))
    with patch.dict(sys.modules, {'email_watcher': watcher}):
        spec = importlib.util.spec_from_file_location('private_mail_pipeline', ROOT/'gtm/calendar/runtime/email_realtime.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026,10,20,12,tzinfo=ZoneInfo('Europe/Berlin')).astimezone(tz)
    module.datetime=FixedClock
    module._reply_context = Mock(return_value=None)
    module.check_incoming_availability = Mock(return_value=True)
    module.live_calendar_options = module.calendar_options
    module.calendar_options = Mock(return_value=[])
    return module


def interpretation(*, offers=None, mode='none', reply=True, notify=True, kind='personal_reply', invite=False):
    return {'kind':kind,'reply_needed':reply,'notify':notify,'summary':'fragt nach einem Gespräch',
            'company':None,'scheduling':mode,'offers':offers or [],'invitation_requested':invite}


def message(text='Hätten Sie Zeit für ein Gespräch?', alias='code', mid='incoming', tid='thread'):
    row = {'account':alias,'id':mid,'threadId':tid,'from':'Eine Person <person@example.invalid>',
           'internalDate':'1792485614000'}
    thread = [{'id':mid,'from':row['from'],'to':'operator@primary.example.invalid' if alias=='code' else 'operator@secondary.example.invalid',
               'cc':'','text':text,'labels':[],'truncated':False,'internalDate':row['internalDate']}]
    return row, thread


def models(module, understand, answer=None):
    answer = answer or {'body':'Vielen Dank. {{SLOT}} passt mir gut.','question':None,
                         'proposed_slot':'2026-10-23T10:00:00+02:00','referral_recipients':[],
                         'notification_summary':'möchte mit dir sprechen'}
    def call(phase, context):
        if phase == 'review':
            draft=context.get('draft');question=context.get('user_question')
            required=bool(draft or question)
            return {'reply_required':required,'scheduling_required':understand['scheduling']!='none'and required,
                    'result':'ready'if draft else'needs_user'if question else'not_required',
                    'source_quote':context['thread'][-1]['text'],
                    'new_time_offer':bool((context.get('hard_facts')or{}).get('proposed_slots')) if draft else False,
                    'proposed_starts':(context.get('hard_facts')or{}).get('proposed_slots',[]) if draft else [],
                    'issues':([{'criterion':'missing_answer','source_quote':context['thread'][-1]['text'],
                    'new_time_offer':bool((context.get('hard_facts')or{}).get('proposed_slots')) if draft else False,
                    'proposed_starts':(context.get('hard_facts')or{}).get('proposed_slots',[]) if draft else [],
                               'draft_quote':None,'explanation':question}]if question else[])}
        return copy.deepcopy(understand if phase=='understand' else answer)
    module.model_json = Mock(side_effect=call)
    return module.model_json
