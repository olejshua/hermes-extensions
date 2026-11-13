from datetime import datetime
import tempfile
import unittest
from gtm.prep import PrepConfig
from gtm.prep_daemon import PrepStore, permitted_evidence, prepare_note, tick

OWN = ('me@example.invalid',)
NOW = datetime.fromisoformat('2026-11-13T09:00:00+01:00')


def make_event(start='2026-11-13T14:00:00+01:00', end='2026-11-13T14:30:00+01:00', **extra):
    ev = {'organizer': {'email': 'me@example.invalid'}, 'id': 'e1', 'summary': 'Fundraising Session', 'start': {'dateTime': start}, 'end': {'dateTime': end},
          'attendees': [{'email': 'me@example.invalid', 'self': True},
                        {'email': 'd@outside.invalid', 'displayName': 'David'}]}
    ev.update(extra)
    return ev


class Reads:
    def __init__(self, event): self.event = event; self.fresh = None
    def events(self, now): return [self.event]
    def calendar_id(self, event): return 'cal'
    def current_event(self, event_id): return self.fresh or self.event


class Evidence:
    def __init__(self, items): self.items = items; self.asked = []
    def collect(self, event, allowed): self.asked.append(allowed); return self.items


class Sender:
    def __init__(self, *results): self.results = list(results); self.sent = []
    def send(self, identity, message):
        self.sent.append((identity, message))
        result = self.results.pop(0)
        if isinstance(result, Exception): raise result
        return result


def drafter(event, evidence):
    ref = evidence[0]['ref']
    return {'known': [{'text': 'Letzte Mail: Pre-Seed Runde.', 'ref': ref}, {'text': 'Erfunden.', 'ref': 'nope'}],
            'open_questions': [{'text': 'Welche Summe nennst du?', 'ref': ref}]}


ITEMS = [{'source': 'mail', 'ref': 'm1', 'text': 'Pre-Seed Thread'}, {'source': 'crm', 'ref': 'c1', 'text': 'x'},
         {'source': 'mail', 'ref': '', 'text': 'no ref'}]
OK = {'success': True, 'message_id': 'm-1'}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.store = PrepStore(self.dir.name + '/prep.sqlite3')
        self.reads = Reads(make_event())

    def run_tick(self, sender, now=NOW, activate=True, evidence=None, **kw):
        return tick(self.reads, self.store, sender, evidence or Evidence(ITEMS), drafter, now=now,
                    allowed_sources=kw.pop('allowed', ('mail',)), own_addresses=OWN, activate=activate, **kw)


class DaemonTests(Base):
    def test_dry_run_renders_but_delivers_nothing_and_records_nothing(self):
        sender = Sender()
        report = self.run_tick(sender, activate=False)
        self.assertEqual((report['rendered'], report['delivered'], sender.sent), (1, 0, []))
        self.assertTrue(report['dry_run'])
        self.assertIn('Welche Summe', report['notes'][0])
        self.assertIsNone(self.store.get(__import__('gtm.prep', fromlist=['x']).due_preparation(
            make_event(), calendar_id='cal', own_addresses=OWN, now=NOW).identity))

    def test_delivery_once_then_restart_does_not_repeat(self):
        sender = Sender(OK)
        self.assertEqual(self.run_tick(sender)['delivered'], 1)
        self.assertEqual(self.run_tick(sender)['delivered'], 0)
        self.assertEqual(len(sender.sent), 1)

    def test_uncited_claims_dropped_and_unpermitted_source_not_requested(self):
        sender = Sender(OK); evidence = Evidence(ITEMS)
        self.run_tick(sender, evidence=evidence)
        message = sender.sent[0][1]
        self.assertIn('Pre-Seed Runde', message); self.assertNotIn('Erfunden', message)
        self.assertEqual(evidence.asked, [('mail',)])
        self.assertIn('Nicht freigegebene Quellen wurden nicht verwendet: crm', message)

    def test_no_permitted_sources_still_gives_calendar_fact_and_one_question(self):
        sender = Sender(OK)
        self.run_tick(sender, allowed=())
        message = sender.sent[0][1]
        self.assertIn('Im Kalender: Fundraising Session', message)
        self.assertIn('Keine freigegebene Quelle', message)

    def test_rejected_send_retries_later_but_bounded(self):
        sender = Sender({'success': False}, {'success': False}, {'success': False}, {'success': False}, OK)
        self.assertEqual(self.run_tick(sender)['failed_delivery'], 1)
        self.assertEqual(self.run_tick(sender, now=NOW.replace(minute=5))['failed_delivery'], 0)  # too soon
        self.assertEqual(self.run_tick(sender, now=NOW.replace(minute=11))['failed_delivery'], 1)
        self.assertEqual(self.run_tick(sender, now=NOW.replace(minute=22))['failed_delivery'], 1)
        self.run_tick(sender, now=NOW.replace(minute=33))
        self.run_tick(sender, now=NOW.replace(minute=44))
        self.assertEqual(len(sender.sent), 3)  # initial send plus two retries, then it stops

    def test_uncertain_send_is_never_replayed_and_never_delivered(self):
        sender = Sender(RuntimeError('timeout'))
        report = self.run_tick(sender)
        self.assertEqual((report['unknown_delivery'], report['delivered']), (1, 0))
        self.run_tick(sender, now=NOW.replace(minute=30))
        self.assertEqual(len(sender.sent), 1)

    def test_receipt_only_for_delivered(self):
        self.run_tick(Sender(OK))
        row = self.store.get(self.store_identity())
        self.assertEqual((row['state'], row['receipt']), ('delivered', 'm-1'))
        with self.assertRaises(ValueError):
            self.store.set(row['identity'], 'rendered', 0, receipt='x')

    def store_identity(self):
        with self.store.connect() as db:
            return db.execute('SELECT identity FROM prep').fetchone()[0]

    def test_event_moved_before_send_blocks_stale_note(self):
        self.reads.fresh = make_event('2026-11-13T17:00:00+01:00', '2026-11-13T17:30:00+01:00')
        sender = Sender(OK)
        report = self.run_tick(sender)
        self.assertEqual((sender.sent, report['review']), ([], 1))

    def test_reschedule_gets_one_new_note(self):
        sender = Sender(OK, OK)
        self.run_tick(sender)
        self.reads.event = make_event('2026-11-13T15:00:00+01:00', '2026-11-13T15:30:00+01:00')
        self.run_tick(sender, now=NOW.replace(minute=30))
        self.assertEqual(len(sender.sent), 2)

    def test_default_does_not_prepare_every_outside_meeting(self):
        self.reads.event = make_event(summary='Austausch')
        self.assertEqual(self.run_tick(Sender(), activate=False)['due'], 0)
        self.reads.event = make_event(summary='Austausch')
        cfg = PrepConfig(important_terms=('austausch',), configured=frozenset({'important_terms'}))
        self.assertEqual(self.run_tick(Sender(), activate=False, config=cfg)['due'], 1)

    def test_source_rejection_degrades_to_calendar_note_with_gap(self):
        class Broken:
            def collect(self, event, allowed): raise RuntimeError('403')
        sender = Sender(OK)
        report = self.run_tick(sender, evidence=Broken())
        self.assertEqual(report['delivered'], 1)
        self.assertIn('nicht erreichbar', sender.sent[0][1]); self.assertIn('Im Kalender', sender.sent[0][1])

    def test_concurrent_ticks_send_once(self):
        import threading
        sender = Sender(OK, OK, OK, OK)
        reports = []
        threads = [threading.Thread(target=lambda: reports.append(self.run_tick(sender))) for _ in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len(sender.sent), 1)

    def test_crash_mid_send_becomes_unknown_never_replayed(self):
        from gtm.prep import due_preparation
        due = due_preparation(make_event(), calendar_id='cal', own_addresses=OWN, now=NOW)
        self.store.claim(due, NOW.timestamp())
        self.store.set(due.identity, 'rendered', NOW.timestamp(), message='note')
        self.assertTrue(self.store.begin_send(due.identity, NOW.timestamp()))
        self.assertFalse(self.store.begin_send(due.identity, NOW.timestamp()))
        sender = Sender(OK)
        later = NOW.replace(minute=30)
        report = self.run_tick(sender, now=later)
        self.assertEqual((sender.sent, report['stale_settled']), ([], 1))
        self.assertEqual(self.store.get(due.identity)['state'], 'delivery_unknown')

    def test_guarded_transitions_cannot_move_state_backwards(self):
        from gtm.prep import due_preparation
        due = due_preparation(make_event(), calendar_id='cal', own_addresses=OWN, now=NOW)
        self.store.claim(due, 1.0)
        self.assertFalse(self.store.set(due.identity, 'delivered', 2.0, receipt='r', only_from=('send_attempted',)))
        self.assertEqual(self.store.get(due.identity)['state'], 'claimed')
        self.store.set(due.identity, 'delivered', 3.0, receipt='r')
        self.assertFalse(self.store.set(due.identity, 'rendered', 4.0, only_from=('claimed',)))
        self.assertEqual(self.store.get(due.identity)['state'], 'delivered')

if __name__ == '__main__':
    unittest.main()
