"""Reviewed calendar topology shared by mail, holds, briefings and call sources.

The IDs match ~/.config/acribic/calendar.toml (19 October 2026) and were
independently read from the CODE account on 22 October. They are public resource
identities, not credentials. Changing a title never selects a different calendar.
The standalone mail runtime installs this same source as calendar_routing.py.
"""
CODE = 'operator@primary.example.invalid'
FUXAM = 'example-calendar-1@import.calendar.google.com'
ACRIBIC = 'example-calendar-2@group.calendar.google.com'
AVAILABILITY = (CODE, FUXAM, ACRIBIC)
MANAGED = (CODE, ACRIBIC)
INTERNAL_RECIPIENTS = frozenset((CODE, 'contact-3@primary.example.invalid'))


class ProjectRoutingReview(ValueError):
    code = 'mail_business_project_review'

    def __init__(self):super().__init__(self.code)


def calendar_id_for(account, *, project=None):
    """The mailbox is not a project. Generic CODE operations stay on CODE."""
    if account not in ('code', 'stealth'):
        raise ValueError('verified_business_account_required')
    if project not in (None, 'acribic', 'stealth'):
        raise ValueError('verified_business_project_required')
    if (project == 'acribic' and account != 'code' or
            project == 'stealth' and account != 'stealth'):
        raise ValueError('business_project_account_mismatch')
    return ACRIBIC if project == 'acribic' else CODE


def owned_business_source(authored):
    """Interpret text only, after frozen sender/SENT/quote checks by the caller."""
    import re
    own=' '.join(authored.casefold().split())
    research=all(marker in own for marker in ('code university','ted','ausschreib'))
    own_demo=(re.search(r'\b(?:wir|ich)\b[^.!?]{0,60}\b(?:stellen|stelle|zeigen|zeige|vorstellen)\b[^.!?]{0,120}\bacribic\b',own)
              and re.search(r'\b(?:demo|pilotphase|pilotprojekt)\b',own))
    return bool(research or own_demo)


def project_from_thread(account, messages):
    """Bind Acribic to our actual outreach original and its reply chain.

    Uses the same CODE/TED/public-tender source markers as FullOriginals'
    existing outreach gate, never a model label, subject or incoming claim.
    The native caller must verify its Gmail profile before supplying originals.
    RFC References preserve the original through internal forwards which never
    reached our mailbox. This is project attribution, not a CRM/company merge.
    Missing/ambiguous linkage cannot turn a generic CODE mail into Acribic.
    """
    from email.utils import getaddresses, parseaddr
    import re
    if account not in ('code', 'stealth'):
        raise ValueError('verified_business_account_required')
    if account == 'stealth':
        return 'stealth'
    if not messages:
        return None
    latest = messages[-1]
    sender = parseaddr(latest.get('from') or '')[1].casefold()
    if not sender or sender == CODE or 'SENT' in latest.get('labels', []):
        return None
    recipients = lambda m: {a.casefold() for _, a in getaddresses(
        [(m.get('to') or ''), (m.get('cc') or '')]) if a}
    def owned_outreach(m):
        if ('SENT'not in m.get('labels',[])or'DRAFT'in m.get('labels',[])or
                parseaddr(m.get('from')or'')[1].casefold()!=CODE):return False
        own=re.split(r'(?im)^\s*(?:>|Von:|From:|Am .+schrieb.+:|On .+wrote:|-----)',
                     m.get('text')or'',maxsplit=1)[0]
        return owned_business_source(own)
    # A missing owned business source is an ordinary CODE mail. Once one is
    # present, incomplete/foreign linkage requires review, not CODE fallback.
    if not any(owned_outreach(m)for m in messages):return None
    if any(m.get('truncated')for m in messages)or CODE not in recipients(latest):
        raise ProjectRoutingReview()
    thread_id = latest.get('thread_id')
    if not thread_id or any(m.get('thread_id') != thread_id for m in messages):
        raise ProjectRoutingReview()
    indexed, outreach = {}, set()
    # A company mailbox can hand off to a named employee in its real reply
    # chain (the original Marina case). A shared mail provider is not a firm.
    shared_domains = {'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com',
                      'yahoo.com', 'icloud.com', 'gmx.de', 'gmx.net', 'web.de', 'primary.example.invalid'}
    sender_domain = sender.rsplit('@', 1)[-1]
    def same_actor_or_firm(address):
        return address==sender or (sender_domain not in shared_domains and
                                   address.rsplit('@',1)[-1]==sender_domain)
    if any(not same_actor_or_firm(a)for a in recipients(latest)-INTERNAL_RECIPIENTS):
        raise ProjectRoutingReview()
    for m in messages:
        rfc = m.get('rfc_message_id')
        if not rfc or rfc in indexed:
            raise ProjectRoutingReview()
        indexed[rfc] = m
        targets = recipients(m)-INTERNAL_RECIPIENTS
        # An actual RFC-linked reply may come from a successor company (ASQA
        # -> TestSolutions). Do not pretend both domains identify one company.
        # Shared-provider mailboxes still require an exact actor match.
        corporate_handoff = (sender_domain not in shared_domains and targets and
            all(a.rsplit('@', 1)[-1] not in shared_domains for a in targets))
        if owned_outreach(m) and targets and (all(same_actor_or_firm(a) for a in targets) or corporate_handoff):
            outreach.add(rfc)
    current, parent, visited = latest, latest.get('in_reply_to'), set()
    try:frontier=int(latest['internalDate'])
    except (KeyError,TypeError,ValueError):raise ProjectRoutingReview()from None
    while parent:
        if parent in visited:
            raise ProjectRoutingReview()
        if parent not in indexed:
            # Outlook often points at a colleague's internal forward. Its
            # References header still names the exact owned source, unlike a
            # guessed subject, company/domain match or quoted body text.
            references = re.findall(r'<[^<>\s]+>', current.get('references') or '')
            known = [r for r in references if r in indexed and r not in visited]
            if not known:
                raise ProjectRoutingReview()
            parent = known[-1]
        visited.add(parent)
        original = indexed[parent]
        try:
            stamp=int(original['internalDate'])
            if stamp>=frontier:raise ProjectRoutingReview()
        except (KeyError, TypeError, ValueError):
            raise ProjectRoutingReview()from None
        frontier=stamp
        actor=parseaddr(original.get('from')or'')[1].casefold()
        if actor not in INTERNAL_RECIPIENTS and not same_actor_or_firm(actor):
            raise ProjectRoutingReview()
        if parent in outreach:
            return 'acribic'
        current, parent = original, original.get('in_reply_to')
    raise ProjectRoutingReview()


def claim_calendar(claim):
    # Pre-19-October claims belong to CODE. Never move or recreate them.
    value = claim.get('calendar_id', CODE)
    if value not in MANAGED:
        raise ValueError('unverified_claim_calendar')
    return value


def event_calendar(event):
    value = event.get('_hermes_calendar_id', CODE)
    if value not in MANAGED:
        raise ValueError('unverified_event_calendar')
    return value


def verified_calendars(calendar):
    items, seen, token = [], set(), None
    for _ in range(20):
        result = calendar.calendarList().list(**({'pageToken': token} if token else {})).execute()
        if not isinstance(result.get('items'), list):
            raise ValueError('calendar_identity_list_incomplete')
        items.extend(result['items'])
        token = result.get('nextPageToken')
        if not token:
            break
        if not isinstance(token, str) or token in seen:
            raise ValueError('calendar_identity_pagination_invalid')
        seen.add(token)
    else:
        raise ValueError('calendar_identity_pagination_limit')
    for cid in AVAILABILITY:
        matches = [c for c in items if c.get('id') == cid]
        if len(matches) != 1:
            raise ValueError('required_calendar_identity_missing_or_duplicate')
        row = matches[0]
        if cid in MANAGED and row.get('accessRole') not in ('owner', 'writer'):
            raise ValueError('managed_calendar_write_identity_unverified')
        if cid == ACRIBIC and (row.get('summary') != 'Acribic' or row.get('timeZone') != 'Europe/Berlin'):
            raise ValueError('acribic_calendar_identity_changed')
    return list(AVAILABILITY)
