"""Source-bound Acribic research context, separate from product capabilities.

The complete real sent answer is fetched through the existing Gmail reader.
Its source pin is evidence of Alex's stated research focus on 19 October, not a
claim that these features exist. No credentials, model, write or notice here.
"""
import hashlib
from email.utils import parseaddr

SOURCE_ID = 'e000000000000005'
THREAD_ID = 'e000000000000006'
SOURCE_SHA256 = 'afe3c15e5c3b66a3afbbaacd04c91a7749e0073dba4ea19080b001652ead818a'
ACCOUNT = 'operator@primary.example.invalid'


def research_context(fetch, thread=()):
    matches = [m for m in thread if m.get('id') == SOURCE_ID]
    source = matches[0] if len(matches) == 1 else fetch()
    if (not isinstance(source, dict) or source.get('id') != SOURCE_ID or
            source.get('thread_id') != THREAD_ID or source.get('truncated') is not False or
            'SENT' not in source.get('labels', []) or 'DRAFT' in source.get('labels', []) or
            parseaddr(source.get('from') or '')[1].casefold() != ACCOUNT or
            not isinstance(source.get('text'), str) or
            hashlib.sha256(source['text'].encode()).hexdigest() != SOURCE_SHA256):
        raise ValueError('acribic_research_original_changed_or_incomplete')
    return {'project': 'acribic', 'scope': 'stated_research_focus_only_not_product_capabilities',
            'observed_sent_at': '2026-10-19T18:31:26+02:00',
            'precedence': 'newer_concrete_own_statements_in_current_thread_override_this_general_source',
            'newer_own_thread_sources': [{'message_id':m['id'],'internalDate':m['internalDate'],
                'body_sha256':hashlib.sha256(m['text'].encode()).hexdigest()}for m in thread
                if 'SENT'in m.get('labels',[]) and 'DRAFT'not in m.get('labels',[]) and
                parseaddr(m.get('from')or'')[1].casefold()==ACCOUNT and
                str(m.get('internalDate','')).isdigit() and int(m['internalDate'])>1792427486000],
            'sources': [{**source, 'account': ACCOUNT, 'body_sha256': SOURCE_SHA256}]}
