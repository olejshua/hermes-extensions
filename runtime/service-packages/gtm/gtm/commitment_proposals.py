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

from gtm.gmail_mail_source import thread_sources
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

    def pending(self):
        db = self._db()
        try:
            return [dict(r) for r in db.execute("SELECT * FROM proposal WHERE state='pending' ORDER BY rowid")]
        finally:
            db.close()

    def decide(self, ref, state):
        if state not in ('confirmed', 'dismissed'):
            raise ValueError('unknown_state')
        db = self._db()
        try:
            with db:
                return db.execute("UPDATE proposal SET state=? WHERE ref=? AND state='pending'", (state, ref)).rowcount == 1
        finally:
            db.close()


def confirm(proposals: ProposalStore, commitments, ref, *, current_thread_hash, acknowledge_flags=False):
    """Owner action. Refuses a stale thread; flagged proposals need an explicit acknowledgement."""
    row = next((r for r in proposals.pending() if r['ref'] == ref), None)
    if row is None:
        raise ValueError('no_pending_proposal')
    if row['thread_hash'] != current_thread_hash:
        raise ValueError('thread_changed_since_proposal')
    if row['flags'] and not acknowledge_flags:
        raise ValueError('flagged_proposal_needs_acknowledgement')
    commitments.add(row['title'], row['ref'], row['due'], 'date' if row['due'] else 'time')
    proposals.decide(ref, 'confirmed')


def propose_from_thread(service, owner_address, thread_id, proposals: ProposalStore, *, complete=None, mode='off',
                        transmission_approved=False, ledger=None, scope=''):
    """Reader -> extractor -> flagged proposals. Disabled unless mode is dry_run/on with approval.

    dry_run stores nothing. on stores review-only proposals, never reminders. Returns a report.
    """
    if mode == 'off' or complete is None:
        return dict(status='disabled', proposed=0)
    sources, gaps = thread_sources(service, owner_address, thread_id)
    digest = thread_hash((sources, gaps))
    report = dict(status='ok', proposed=0, gaps=gaps, flagged=0, thread_hash=digest, results=[])
    if gaps:
        report['status'] = 'incomplete_thread'; return report
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
