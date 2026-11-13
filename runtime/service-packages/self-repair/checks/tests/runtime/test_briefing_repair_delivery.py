"""Item 6: repaired briefing never replays an attempted send; delivered only with a verified receipt.
Characterization of recover.briefing's send_attempted branch (passes on first run, no code change)."""
import datetime as dt, json, sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "brain" / "src")]
from self_repair import recover
sys.path.insert(0, str(ROOT.parent / "gtm"))  # repo copy; recover.py adds the live PAYLOAD path at runtime
from gtm import briefing_daemon as b


@dataclass
class Due:
    starts_at: object
    event_id: str
    identity: str


class Reads:
    def __init__(self, *a, **k): pass
    def current_event(self, _id): return {'start': 'S', 'id': 'e1'}
    def business_mapping(self, event): return None


class FakeStore:
    def __init__(self, root, row): self.root, self.row, self.calls = root, row, []
    def get(self, identity): return self.row
    def recover_send(self, identity):
        self.calls.append('recover'); return self.row
    def claim(self, *a, **k): self.calls.append('claim'); return True
    def step(self, *a, **k): self.calls.append('step')


def run(tmp_path, row):
    store = FakeStore(tmp_path, row)
    due = Due(starts_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1), event_id='e1', identity='x')
    snapshot = {'context': {'calendar': {'id': 'c', 'start': 'S'}}, 'identity': 'id1'}
    with patch.object(b, 'ExistingReads', Reads), patch.object(b, 'classify', lambda e: ('call', None)), \
         patch.object(b, 'event_calendar', lambda e: 'c'), patch.object(b, 'Store', lambda root: store), \
         patch('gtm.briefing.due_briefing', lambda *a, **k: due), \
         patch.object(b, 'Runtime', side_effect=AssertionError('runtime must not be built after an attempted send')):
        with patch.object(sys, 'path', list(sys.path)):
            return recover.briefing(snapshot, 'sig'), store


def test_attempted_send_is_never_replayed_and_unknown_is_not_restored(tmp_path):
    result, store = run(tmp_path, {'send_attempted': 1, 'state': 'delivery_unknown'})
    assert result == {'state': 'needs_delivery_readback', 'replayed': False}
    assert store.calls == ['recover']


def test_verified_receipt_state_is_restored(tmp_path):
    result, _ = run(tmp_path, {'send_attempted': 1, 'state': 'delivered'})
    assert result['state'] == 'restored' and result['replayed'] is False
