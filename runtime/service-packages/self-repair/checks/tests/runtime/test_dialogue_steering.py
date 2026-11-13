"""SteeringBarrier: a correction that arrives during a long task must stop a not yet started tool call."""
import importlib.util
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5] / 'hermes-home/plugins/ole-dialogue/steering.py'
spec = importlib.util.spec_from_file_location('ole_steering', ROOT)
steering = importlib.util.module_from_spec(spec); spec.loader.exec_module(steering)


class Policy:
    def __init__(self, bound=True): self._bound = bound
    def bound(self, source): return self._bound and source == 'private'


class Actor:
    def __init__(self):
        self.session_id, self._current_turn_id, self._current_api_request_id = 's1', 't1', 'r1'
        self._pending_steer, self._pending_steer_lock = None, threading.Lock()
        self.interrupted = 0
    def interrupt(self): self.interrupted += 1


class Adapter:
    def __init__(self): self._pending_messages = {}


class Gateway:
    def __init__(self, actor, authorized=True):
        self.actor, self.authorized, self.adapter = actor, authorized, Adapter()
    def _is_user_authorized_for_source(self, source): return self.authorized
    def _peek_session_state(self, key): return types.SimpleNamespace(turn=types.SimpleNamespace(agent=self.actor))
    def _delivery_adapter_for(self, source): return self.adapter
    def _session_key_for_source(self, source): return 'key'


def event(source='private', internal=False, message_id='m1'):
    return types.SimpleNamespace(source=source, internal=internal, message_id=message_id)


def call(barrier): return barrier.before_tool(session_id='s1', turn_id='t1', api_request_id='r1')


class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.actor = Actor(); self.gateway = Gateway(self.actor); self.barrier = steering.SteeringBarrier(Policy())

    def observe(self, busy=True, **kw): self.barrier.observe(event(**kw), self.gateway, 'key', busy=busy)

    def test_no_pending_message_lets_tool_run(self):
        self.observe(busy=False)
        self.assertIsNone(call(self.barrier))

    def test_correction_seen_before_the_request_blocks_the_stale_tool(self):
        self.observe(busy=True)
        result = call(self.barrier)
        self.assertEqual(result['action'], 'block'); self.assertIn('Nutzernachricht', result['message'])

    def test_new_request_after_correction_is_not_blocked_unless_steer_pending(self):
        self.observe(busy=True)
        self.actor._current_api_request_id = 'r2'
        self.assertIsNone(self.barrier.before_tool(session_id='s1', turn_id='t1', api_request_id='r2'))
        self.actor._pending_steer = 'bitte erst X'
        self.assertEqual(self.barrier.before_tool(session_id='s1', turn_id='t1', api_request_id='r2')['action'], 'block')

    def test_queued_human_message_interrupts_and_blocks(self):
        self.observe(busy=False)
        self.gateway.adapter._pending_messages['key'] = event()
        self.assertEqual(call(self.barrier)['action'], 'block'); self.assertEqual(self.actor.interrupted, 1)

    def test_internal_queued_message_does_not_block(self):
        self.observe(busy=False)
        self.gateway.adapter._pending_messages['key'] = event(internal=True)
        self.assertIsNone(call(self.barrier)); self.assertEqual(self.actor.interrupted, 0)

    def test_unbound_or_unauthorized_sources_are_ignored(self):
        self.barrier.observe(event(source='group'), self.gateway, 'key', busy=True)
        self.assertEqual(self.barrier.routes, {})
        self.observe(busy=True); self.gateway.authorized = False
        self.assertIsNone(call(self.barrier))

    def test_other_session_tool_calls_are_untouched(self):
        self.observe(busy=True)
        self.assertIsNone(self.barrier.before_tool(session_id='other', turn_id='t1', api_request_id='r1'))
        self.assertIsNone(self.barrier.before_tool(session_id='', turn_id='', api_request_id=''))

    def test_current_inbound_rejects_stale_actor_and_wrong_session(self):
        self.observe(busy=True)
        with self.assertRaises(ValueError): self.barrier.current_inbound('key', self.actor, 's1')  # stale: same request
        self.actor._current_api_request_id = 'r2'
        self.assertEqual(self.barrier.current_inbound('key', self.actor, 's1'), 'm1')
        with self.assertRaises(ValueError): self.barrier.current_inbound('key', self.actor, 'other')


if __name__ == '__main__':
    unittest.main()
