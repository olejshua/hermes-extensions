"""Validate retained review audit records against their active company source."""
import uuid


def resolved_target(review):
    status = review.get('importStatus') or ''
    if not status.startswith('GELOEST:'):
        return None
    target = status.removeprefix('GELOEST:')
    try:
        if str(uuid.UUID(target)) != target:
            raise ValueError()
    except ValueError as exc:
        raise RuntimeError('Malformed resolved review target') from exc
    return target


def validate_resolved(review, companies):
    target = resolved_target(review)
    if target is None:
        return False
    company = companies.get(target)
    if not company or review.get('versandbereit') is not False:
        raise RuntimeError('Resolved review target missing or review is sendable')
    original = review['notionRohdaten']
    matches = [s for s in company.get('notionQuellen') or []
               if s['page_id'].replace('-', '').lower() == original['page_id'].replace('-', '').lower()]
    if len(matches) != 1 or {k:v for k,v in matches[0].items() if k != 'outreach'} != {
            k:v for k,v in original.items() if k != 'outreach'}:
        raise RuntimeError('Resolved review original source is not preserved exactly once')
    # Later outreach overrides may evolve on the company; historical mail events
    # on the retained review must still protect its active target.
    history = company.get('outreachHistorie') or []
    for event in review.get('outreachHistorie') or []:
        if event not in history:
            raise RuntimeError('Resolved review mail history missing from company')
    return True


def merge_mail_evidence(*lists):
    """Keep attribution variants; fail on conflicting actual message metadata."""
    actual, result = {}, []
    for events in lists:
        for event in events or []:
            identity = (event['event'], event['message_id'], event['mailbox'])
            message = {k:v for k,v in event.items() if k != 'match'}
            if identity in actual and actual[identity] != message:
                raise RuntimeError('Conflicting mail evidence; reconcile before resolving')
            actual[identity] = message
            if event not in result:
                result.append(event)
    return sorted(result, key=lambda e:(str(e.get('at')), e['message_id'], e['mailbox'], str(e.get('match'))))
