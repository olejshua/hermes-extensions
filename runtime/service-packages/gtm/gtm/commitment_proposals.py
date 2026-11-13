"""Review-only proposed commitments from the owner's sent mail.

Substring + SENT proves the owner wrote the words, not that they are a live obligation: a sent mail
can quote someone else's request, withdraw an earlier promise, be conditional, or be superseded by a
later message. So mail items never enter the reminder store on their own. They land here as
proposals with explicit flags, and only an owner confirmation moves one to CommitmentStore, and only if the
thread is unchanged since the proposal. Flags are heuristics that route to review; they never
approve anything. Provenance limits (imported/inserted SENT, client-set internalDate) stay with
gmail_mail_source. Nothing here reads mail, calls a model or sends.
"""
from __future__ import annotations
import hashlib
import re
import sqlite3
from pathlib import Path

from gtm.gmail_mail_source import thread_sources, thread_fingerprint
from gtm.mail_commitments import MailSource, plan_mail_commitments, MailCommitment
from gtm.synthesis import extract_mail_commitments

_CONDITIONAL = re.compile(r'\b(falls|wenn|sofern|vielleicht|eventuell|könnte|koennte|würde|wuerde|if|maybe|might|unless|provided)\b', re.I)
_WITHDRAW = re.compile(r'(doch nicht|nicht mehr|kann ich nicht|schaffe ich nicht|sage ich ab|absage|stornier|cancel|withdraw|no longer|can\'t|cannot|won\'t|vergiss)', re.I)
_QUOTE_LINE = re.compile(r'^\s*>')
_ATTRIB = re.compile(r'^\s*(Am .{3,80} schrieb|On .{3,80} wrote|-----Original)', re.I)


def _quoted_region(body):
    """True for text after a reply attribution line, plus '>' lines."""
    marked, hit = [], False
    for line in body.split('\n'):
        hit = hit or bool(_ATTRIB.match(line))
        marked.append((line, hit or bool(_QUOTE_LINE.match(line))))
    return marked


def thread_hash(sources_and_gaps):
    sources, gaps = sources_and_gaps
    parts = [f'{s.message_id}:{hashlib.sha256(s.body.encode()).hexdigest()}' for s in sources] + sorted(gaps)
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def flags_for(source: MailSource, item: MailCommitment, later):
    flags = []
    quoted_only = True
    sentence = ''
    for line, quoted in _quoted_region(source.body):
        if item.quote in line:
            quoted_only = quoted_only and quoted; sentence = line
    if item.quote in source.body and not sentence:  # multi-line quote: look at containing lines
        sentence = item.quote
        quoted_only = all(q for line, q in _quoted_region(source.body) if line.strip() and line.strip() in item.quote) or False
    if quoted_only:
        flags.append('quoted_text')
    if _CONDITIONAL.search(item.quote) or _CONDITIONAL.search(sentence):
        flags.append('conditional')
    if _WITHDRAW.search(item.quote):
        flags.append('withdrawal_in_quote')
    for later_source in later:
        fresh = '\n'.join(line for line, q in _quoted_region(later_source.body) if not q)
        if _WITHDRAW.search(fresh):
            flags.append('later_own_message_may_withdraw'); break
    return flags


class ProposalStore:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS notice(key TEXT PRIMARY KEY, state TEXT NOT NULL, receipt TEXT, at REAL NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS notified(version TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS proposal(ref TEXT PRIMARY KEY, title TEXT NOT NULL, quote TEXT NOT NULL, due TEXT, '
                       'flags TEXT NOT NULL, thread_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT "pending")')
        self.path.chmod(0o600)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=15); db.row_factory = sqlite3.Row
        return db

    def propose(self, ref, title, quote, due, flags, thread_hash_):
        db = self._db()
        try:
            with db:
                db.execute('INSERT OR IGNORE INTO proposal(ref,title,quote,due,flags,thread_hash) VALUES(?,?,?,?,?,?)',
                           (ref, title, quote, due, ','.join(flags), thread_hash_))
        finally:
            db.close()

    def in_state(self, state):
        db = self._db()
        try:
            return [dict(r) for r in db.execute('SELECT * FROM proposal WHERE state=? ORDER BY rowid', (state,))]
        finally:
            db.close()

    def pending(self):
        db = self._db()
        try:
            return [dict(r) for r in db.execute("SELECT * FROM proposal WHERE state='pending' ORDER BY rowid")]
        finally:
            db.close()

    def decide(self, ref, state, only_from='pending'):
        if state not in ('confirming', 'confirmed', 'dismissed', 'unknown_needs_review'):
            raise ValueError('unknown_state')
        db = self._db()
        try:
            with db:
                return db.execute("UPDATE proposal SET state=? WHERE ref=? AND state=?", (state, ref, only_from)).rowcount == 1
        finally:
            db.close()


def proposal_version(row):
    """Binds a confirmation to the exact proposal content, flags and thread state."""
    blob = '\x1f'.join(str(row[k]) for k in ('ref', 'title', 'quote', 'due', 'flags', 'thread_hash'))
    return hashlib.sha256(blob.encode()).hexdigest()


def confirmation_phrase(row):
    """What the owner sends on the verified owner channel. Flagged items need the explicit tail."""
    base = f"Bestätige {hashlib.sha256(row['ref'].encode()).hexdigest()[:8]} {proposal_version(row)[:10]}"
    return base + (' trotz Hinweisen' if row['flags'] else '')


class UnprovenEventContract:
    """Default: the upstream gateway event contract is not established from this repo, so every
    judgement is unknown and confirmation abstains. A trusted gateway adapter replaces this with
    explicit positive proof (is_human_inbound, is_current_unedited) taken from fields it has verified
    on the real event class. Absence of an attribute is never treated as proof."""

    def is_human_inbound(self, event):
        return None

    def is_current_unedited(self, event):
        return None


def native_confirmation_record(event, gateway, policy, contract=None):
    """Hermes-native evidence for one inbound Telegram event, or None (abstain).

    Needs: not internal (explicit False), source bound to the owner's private chat (DialoguePolicy.bound),
    gateway authorization, and POSITIVE proof from the event contract that the message is human inbound
    and current/unedited. Unknown proof abstains."""
    contract = contract or UnprovenEventContract()
    source = getattr(event, 'source', None)
    if getattr(event, 'internal', None) is not False or source is None or not policy.bound(source):
        return None
    if gateway._is_user_authorized_for_source(source) is not True:
        return None
    if contract.is_human_inbound(event) is not True or contract.is_current_unedited(event) is not True:
        return None
    mid = str(getattr(event, 'message_id', '') or '')
    text = getattr(event, 'text', None)
    if not mid or not isinstance(text, str):
        return None
    return dict(kind='message', message_id=mid, body=text, **{'from': policy.owner})


class ConsumedMessages:
    """Each owner message can attempt at most one confirmation, across restarts and threads."""

    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS consumed(message_id TEXT PRIMARY KEY)')
        self.path.chmod(0o600)

    def claim(self, message_id):
        with sqlite3.connect(self.path, timeout=15) as db:
            return db.execute('INSERT OR IGNORE INTO consumed(message_id) VALUES(?)', (message_id,)).rowcount == 1


def reconcile(proposals: ProposalStore, commitments):
    """Resolve proposals stuck in 'confirming' after a crash. Written commitment -> confirmed; none -> unknown_needs_review
    (not retried automatically, not shown as done)."""
    out = {'confirmed': 0, 'unknown': 0}
    for row in proposals.in_state('confirming'):
        with commitments.connect() as db:
            present = db.execute('SELECT 1 FROM commitment WHERE ref=?', (row['ref'],)).fetchone() is not None
        if present:
            proposals.decide(row['ref'], 'confirmed', only_from='confirming'); out['confirmed'] += 1
        else:
            proposals.decide(row['ref'], 'unknown_needs_review', only_from='confirming'); out['unknown'] += 1
    return out


def render_pending(proposals: ProposalStore):
    """Owner-private note section. Render only: no send. Phrases are provisional UX."""
    lines = []
    pending = proposals.pending()
    if pending:
        lines.append('Mögliche Zusagen aus deinen gesendeten Mails (noch nichts übernommen):')
        for row in pending:
            hint = f" [Hinweise: {row['flags'].replace(',', ', ')}]" if row['flags'] else ''
            due = f", Frist {row['due'][:10]}" if row['due'] else ''
            lines.append(f"- {row['title']}{due}{hint}\n  Beleg: „{row['quote'][:200]}“\n  Zum Übernehmen antworten: {confirmation_phrase(row)}")
    unclear = proposals.in_state('confirming') + proposals.in_state('unknown_needs_review')
    if unclear:
        lines.append('Unklar, ob übernommen (bitte prüfen):')
        lines += [f"- {row['title']}" for row in unclear]
    return '\n'.join(lines)


MIN_NOTICE_INTERVAL_SECONDS = 6 * 3600  # design default, not an owner preference


def deliver_notice(proposals: ProposalStore, sender, *, activate, allowed_now=True, now=None,
                   min_interval=MIN_NOTICE_INTERVAL_SECONDS):
    """Send the pending proposals to the owner-private chat only when something NEW needs attention.

    Material change = a pending proposal version never notified before. Reordering, a decided or
    dismissed proposal dropping out, or an unchanged set never creates a notice, and an empty set
    never sends. New items arriving within `min_interval` of the last notice wait and are batched
    into the next one. The claim is written before the send; only success plus a message id counts
    as delivered; anything else stays unknown and is not replayed. Dry-run reports only.
    The text lists all pending items in a canonical (sorted by ref) order.
    """
    import time as _time
    clock = _time.time() if now is None else now
    rows = sorted(proposals.pending(), key=lambda r: r['ref'])
    versions = [proposal_version(r) for r in rows]
    with sqlite3.connect(proposals.path, timeout=15) as db:
        known = {r[0] for r in db.execute('SELECT version FROM notified')}
        last = db.execute('SELECT max(at) FROM notice').fetchone()[0] or 0
    fresh = [v for v in versions if v not in known]
    report = dict(planned=bool(fresh), delivered=False, state='none', dry_run=not activate, new=len(fresh))
    if not fresh:
        return report
    if not activate:
        report['state'] = 'planned'; return report
    if not allowed_now:
        report['state'] = 'deferred'; return report
    if clock - last < min_interval:
        report['state'] = 'batched'; return report
    text = render_pending(proposals)
    key = hashlib.sha256('\n'.join(sorted(versions)).encode()).hexdigest()
    with sqlite3.connect(proposals.path, timeout=15) as db:
        if db.execute("INSERT OR IGNORE INTO notice(key,state,at) VALUES(?,'send_attempted',?)", (key, clock)).rowcount != 1:
            report['state'] = 'already_handled'; return report
        # attempted versions are never re-announced, whatever the outcome
        db.executemany('INSERT OR IGNORE INTO notified(version) VALUES(?)', [(v,) for v in versions])
    try:
        result = sender.send(key, text)
    except Exception:
        result = None
    if isinstance(result, dict) and result.get('success') is True and result.get('message_id'):
        state, receipt = 'delivered', str(result['message_id'])
    elif isinstance(result, dict) and result.get('success') is False and not result.get('message_id'):
        state, receipt = 'delivery_failed', None
    else:
        state, receipt = 'delivery_unknown', None
    with sqlite3.connect(proposals.path, timeout=15) as db:
        db.execute('UPDATE notice SET state=?, receipt=? WHERE key=?', (state, receipt, key))
    report.update(state=state, delivered=state == 'delivered')
    return report


def confirm_from_owner_event(proposals, commitments, service, owner_address, *, ref, event, gateway, policy, consumed, contract=None):
    """Hermes-native confirmation: the inbound gateway event is the evidence; no cross-system lookup."""
    record = native_confirmation_record(event, gateway, policy, contract)
    if record is None:
        raise ValueError('not_an_owner_private_inbound_message')
    if not consumed.claim(record['message_id']):
        raise ValueError('confirmation_message_already_used')
    confirm_from_owner_message(proposals, commitments, service, owner_address, ref=ref,
                               message_id=record['message_id'], lookup=lambda _mid: record, owner_handle=policy.owner)


def confirm_from_owner_message(proposals: ProposalStore, commitments, service, owner_address, *, ref, message_id,
                               lookup, owner_handle):
    """Commit a proposal only on authenticated owner-channel intent.

    `lookup(message_id)` is a trusted channel record reader (in Hermes: native_confirmation_record over the gateway event) and returns {'from': handle, 'body': text, 'kind': 'message'}.
    The sender must be the verified owner handle, the text must be exactly the phrase bound to this
    proposal's version and flags (so an old, edited or other proposal's confirmation does not match),
    and the current full thread (inbound included) is re-read and must match the proposal's thread
    digest. Anything stale or unknown stays pending. A model, a file or a flag argument cannot confirm.
    """
    row = next((r for r in proposals.pending() if r['ref'] == ref), None)
    if row is None:
        raise ValueError('no_pending_proposal')
    record = lookup(message_id)
    if (not isinstance(record, dict) or record.get('kind', 'message') != 'message' or record.get('from') != owner_handle
            or not isinstance(record.get('body'), str) or record['body'].strip() != confirmation_phrase(row)):
        raise ValueError('owner_confirmation_not_bound_to_this_proposal')
    thread_id = ref.split(':')[1]
    now, _count = thread_fingerprint(service, owner_address, thread_id)  # raises on unreadable: stays review
    if now != row['thread_hash']:
        raise ValueError('thread_changed_since_proposal')
    # claim first (single winner), write, then mark confirmed. A crash in between leaves 'confirming',
    # which reconcile() resolves from the store; it is never shown as completed.
    if not proposals.decide(ref, 'confirming'):
        raise ValueError('proposal_already_decided')
    commitments.add(row['title'], row['ref'], row['due'], 'date' if row['due'] else 'time')
    proposals.decide(ref, 'confirmed', only_from='confirming')


def propose_from_thread(service, owner_address, thread_id, proposals: ProposalStore, *, complete=None, mode='off',
                        transmission_approved=False, ledger=None, scope=''):
    """Reader -> extractor -> flagged proposals. Disabled unless mode is dry_run/on with approval.

    dry_run stores nothing. on stores review-only proposals, never reminders. Returns a report.
    """
    if mode == 'off' or complete is None:
        return dict(status='disabled', proposed=0)
    sources, gaps = thread_sources(service, owner_address, thread_id)
    if gaps:
        return dict(status='incomplete_thread', proposed=0, gaps=gaps, flagged=0, results=[])
    digest, _count = thread_fingerprint(service, owner_address, thread_id)
    report = dict(status='ok', proposed=0, gaps=gaps, flagged=0, thread_hash=digest, results=[])
    for index, source in enumerate(sources):
        result = extract_mail_commitments(owner_address, source, complete, mode,
                                          transmission_approved=transmission_approved, ledger=ledger, scope=scope)
        report['results'].append(result['status'])
        later = sources[index + 1:]
        for title, ref, due in result['planned']:
            key = ref.rsplit(':', 1)[1]
            quote = next((i['quote'] for i in result.get('items', []) if i['key'] == key), None)
            item = MailCommitment(key, title, quote or title)
            if quote is None:
                continue
            flags = flags_for(source, item, later)
            report['flagged'] += bool(flags)
            if mode == 'on':
                proposals.propose(ref, title, quote, due, flags, digest)
            report['proposed'] += 1
    return report
