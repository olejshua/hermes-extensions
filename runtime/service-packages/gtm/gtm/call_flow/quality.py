"""One source-bound coverage review, shared by the archive and CRM note.

This verifies attribution and coverage structure, not the truth of arbitrary
model conclusions. Real-call acceptance remains a separate quality check.
"""
from __future__ import annotations
import copy

TOPICS = {
    'process': 'Ablauf und Zuständigkeiten',
    'needs': 'Konkreter Bedarf und Aufwand',
    'tools': 'Heutige Werkzeuge und Alternativen',
    'scale': 'Volumen und Größenordnung',
    'requirements': 'Anforderungen und Grenzen',
    'objections': 'Einwände und Gegenargumente',
    'commercial_signal': 'Produktinteresse und Einordnung',
    'next_steps': 'Zusagen und nächste Schritte',
}
ADDITIONAL_TOPICS = {
    'permissions': 'Rückfragen, Wiederkontakt und Werbeeinwilligung getrennt',
    'attribution': 'Sprecherzuordnung und verbleibende Unsicherheit',
}

ANALYSIS_METHOD = '''Read the complete unchanged source once, not just the summary or closing exchange.
Extract every distinct material point raised by either party: actual process, benefit and remaining need,
tools and alternatives, tradeoffs, constraints, requirements, objections, explicit rejections and open questions.
Do not collapse several distinct requirements into a generic sentence. Cover supported and opposing evidence
by the same standard, including what the counterparty would keep doing with existing alternatives.
Separate their current practice, our proposed/planned functionality and any actual agreement.
Each substantive finding has its own exact short original quote and verified speaker, or visible attribution uncertainty.
Check the full question/answer around invitations and every relevant closing passage. Capture person, topic,
channel and conditions; no marketing consent or product interest from our own offer. Preserve contradictions.
Record all actual commitments by both parties; assignee and conditions stay explicit, unknown deadline/effort null.
Stored CRM values are existing state to preserve, not a model answer or an independent transcript fact.
Review every applicable live field with set/keep and a concrete source-grounded reason. Do not fill every flag.
If a field cannot be supported, retain prior information and state the limit. No new contacts, mail, tasks or invites.'''


def contract(version=2):
    topics={**TOPICS,**ADDITIONAL_TOPICS} if version>=2 else TOPICS
    return dict(version=version,analysis_method=ANALYSIS_METHOD,topics=topics, topic_fields=['status', 'reason', 'findings'],
        analysis_fields=['summary','demo','followup_permission','statements','review_items','coaching','quality_review','crm'],
        outcome=dict(states=['yes','no','conditional','not_discussed','unclear'],fields=['state','evidence']),
        statement=dict(fields=['speaker','party','quote','evidence'],party=['ole','piet','counterparty','unknown'],
            quote='One unchanged contiguous original span within the exact speaker segment; separate speakers into separate evidence objects.'),
        review_item=dict(fields=['key','title','kind','assignee','assignee_name','evidence','due_date','deadline_basis','estimated_minutes'],
            kind=['action','open_question'],assignee=['ole','other','unclear'],unknown_deadline_or_effort=None,
            evidence='One statement object or a nonempty list of statement objects; preserve and verify every speaker/quote, including question/answer context.'),
        coaching=dict(type='array',max_items=3,fields=['kind','evidence','reason','suggestion','timestamp'],
            kind=['strength','improvement','missed_followup'],
            evidence='One statement object or a nonempty list of statement objects. Preserve every exact speaker-attributed quote; comparisons may need both parties.',
            reason='Concrete source-grounded reason, max 1200 characters.',
            suggestion='Concrete suggested formulation/question for Alex, max 1200 characters.',
            timestamp=None,privacy='Private feedback only; do not export coaching to CRM, archive or Monolith.'),
        crm=dict(fields=['company','people','opportunity'],disposition=['keep','set'],
            opportunity_decision=['none','review','create','update'],schema='Use every applicable field/options from the supplied current crm_context.schema except generated meetingReview/evidenceNotionUrl.'),
        status=['evidenced', 'not_discussed', 'unclear'],
        finding_fields=['text', 'kind', 'evidence', 'action_key'], kind=['fact', 'assessment'],
        evidence='List of exact {speaker,party,quote,evidence} statements from the actual source. No guessed figures or roles.',
        output_shape='quality_review is the complete topic-key map, each value {status,reason,findings}. Also accepts exactly {version:<matching contract version>,topics:<same complete topic-key map>}.',
        next_steps='Actual actions/open questions link action_key to an existing review_items key in whichever topic fits. Link evidence uses an identical original quote or exact verified quotes from the same original speaker turn. Every review_item must be linked. Context findings without an action need no action_key and must not become invented tasks. Include both parties, conditions and timing; missing date stays null.',
        distinction='Our product proposal is not their buying intent. Friendly feedback, expert help, demo interest and buying intent differ. Permission needs its original person/topic/channel/timing scope. Read every invitation with its preceding question and following answer; cover all relevant passages, including the closing exchange. Our own offer is no consent. mailErlaubt=NEIN is no rejection of contextual questions. Use unknown for genuinely disputed labels; no guessed reverse attribution. New steps include both parties, conditions and deadlines; absent deadline/effort is null. Review every applicable live CRM option, including additive tags and interestEvidence, without setting every flag.')


def validate(value, raw, binding, transcript, statement_validator, *, required=False):
    if value is None:
        if required: raise ValueError('call_quality_review_required')
        return None
    topics={**TOPICS,**ADDITIONAL_TOPICS} if binding.get('analysis_contract_version',1)>=2 else TOPICS
    if isinstance(value,dict) and set(value)=={'version','topics'}:
        if type(value['version']) is not int or value['version']!=binding.get('analysis_contract_version',1):
            raise ValueError('call_quality_contract_version_mismatch')
        value=value['topics']
    if not isinstance(value, dict) or set(value) != set(topics):
        raise ValueError('call_quality_topic_coverage_incomplete')
    actions = {item['key']: item for item in raw.get('review_items', [])}
    action_quotes={}
    for key,item in actions.items():
        quotes=item.get('evidence')
        if isinstance(quotes,dict):quotes=[quotes]
        if not isinstance(quotes,list) or not quotes:
            raise ValueError('call_quality_action_evidence_missing')
        verified=[]
        for quote in quotes:
            if not isinstance(quote,dict):raise ValueError('call_quality_action_evidence_missing')
            verified.append((quote,statement_validator(quote,binding,transcript)))
        action_quotes[key]=verified
    linked=set()
    for topic, review in value.items():
        if (not isinstance(review, dict) or set(review) != {'status', 'reason', 'findings'} or
                review['status'] not in ('evidenced', 'not_discussed', 'unclear') or
                not isinstance(review['reason'], str) or not 1 <= len(review['reason'].strip()) <= 600 or
                not isinstance(review['findings'], list)):
            raise ValueError('call_quality_topic_invalid')
        if review['status'] == 'evidenced' and not review['findings']:
            raise ValueError('call_quality_evidence_missing')
        if review['status'] == 'not_discussed' and review['findings']:
            raise ValueError('call_quality_not_discussed_with_claims')
        for finding in review['findings']:
            if (not isinstance(finding, dict) or set(finding) - {'text', 'kind', 'evidence', 'action_key'} or
                    not isinstance(finding.get('text'), str) or not 1 <= len(finding['text'].strip()) <= 1200 or
                    finding.get('kind') not in ('fact', 'assessment') or
                    not isinstance(finding.get('evidence'), list) or not 1 <= len(finding['evidence']) <= 4):
                raise ValueError('call_quality_finding_invalid')
            verified=[]
            for quote in finding['evidence']:
                if not isinstance(quote, dict): raise ValueError('call_quality_evidence_missing')
                verified.append((quote,statement_validator(quote, binding, transcript)))
            key = finding.get('action_key')
            if key is not None:
                if key not in actions: raise ValueError('call_quality_unknown_action_key')
                def same_source(q,proof,anchor,anchor_proof):
                    if q['speaker'].strip().casefold()!=anchor['speaker'].strip().casefold() or q['party']!=anchor['party']:
                        return False
                    if q['quote']==anchor['quote']:return True
                    if not isinstance(proof,dict) or not isinstance(anchor_proof,dict):
                        return False
                    left=set(proof.get('speaker_turns',()))
                    right=set(anchor_proof.get('speaker_turns',()))
                    return (proof.get('speaker')==anchor_proof.get('speaker')
                            and len(left)==len(right)==1 and left==right)
                if not any(same_source(q,proof,anchor,anchor_proof)
                           for q,proof in verified for anchor,anchor_proof in action_quotes[key]):
                    raise ValueError('call_quality_action_source_mismatch')
                linked.add(key)
    if set(actions) != linked:
        raise ValueError('call_quality_actions_omitted')
    return copy.deepcopy(value)


def render(value):
    if value is None: return ''
    lines = ['Gesprächserkenntnisse:']
    for topic, label in {**TOPICS,**ADDITIONAL_TOPICS}.items():
        if topic not in value:continue
        review = value[topic]
        if review['status'] == 'not_discussed':
            lines.append(f'- {label}: nicht besprochen. {review["reason"]}')
            continue
        if review['status'] == 'unclear':
            lines.append(f'- {label}: unklar. {review["reason"]}')
        for finding in review['findings']:
            prefix = 'Einschätzung: ' if finding['kind'] == 'assessment' and not finding['text'].casefold().startswith('einschätzung:') else ''
            lines.append(f'- {label}: {prefix}{finding["text"]}')
            for q in finding['evidence']:
                lines.append(f'  {q["speaker"]}: „{q["quote"]}“')
    return '\n'.join(lines)
