"""Fake-module tests of the disabled completion adapter. They prove local request shape and refusals,
not behaviour of the installed Hermes."""
import json, sys, types, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
from gtm.codex_completion import make_complete, PREP_SCHEMA, prep_factory, strict_schema, ROUTE  # noqa: E402
from gtm.prep_daemon_entry import build_drafter  # noqa: E402


def fakes(runtime=None, output='{"known":[],"open_questions":[]}', calls=()):
    log = {'requests': [], 'closed': False, 'finished': False}

    class Client:
        class responses:
            @staticmethod
            def create(**kw):
                log['requests'].append(kw); return object()
        def close(self): log['closed'] = True

    class Guard:
        def __init__(self, c, t, no_progress_timeout): pass
        def start(self): pass
        def adopt_stream(self, s): pass
        def on_event(self, e): pass
        def release_stream(self, s): pass
        def finish(self): log['finished'] = True

    rt = runtime or dict(provider=ROUTE[0], auth_mode=ROUTE[1], base_url=ROUTE[2] + '/', api_key='k')
    from contextlib import contextmanager
    @contextmanager
    def allowed(p): log['allowed'] = p; yield
    mods = dict(resolve_codex_runtime_credentials=lambda: rt, resolve_provider_client=lambda *a, **k: (Client(), None),
                parse_final=lambda f: ([output], list(calls), None), StreamGuard=Guard, consume=lambda s, model, on_event: object(),
                bypass=lambda r: r, allowed=allowed, wire_model=lambda m: 'wire-' + m)
    return (lambda: mods), log


class T(unittest.TestCase):
    def test_request_has_no_tools_strict_schema_and_untrusted_instruction(self):
        load, log = fakes()
        out = make_complete(PREP_SCHEMA, model='m1', load=load)('prompt text')
        req = log['requests'][0]
        self.assertEqual((req['tools'], req['tool_choice'], req['store'], req['stream']), ([], 'none', False, True))
        self.assertIn('untrusted', req['instructions']); self.assertEqual(req['model'], 'wire-m1')
        self.assertEqual(req['input'][0]['content'], 'prompt text')
        self.assertTrue(req['text']['format']['strict'] and req['text']['format']['schema']['additionalProperties'] is False)
        self.assertEqual(log['allowed'], {'openai-codex'}); self.assertTrue(log['closed'] and log['finished'])
        self.assertEqual(json.loads(out), {'known': [], 'open_questions': []})

    def test_other_route_is_refused_before_any_request(self):
        load, log = fakes(runtime=dict(provider='openai', auth_mode='api_key', base_url='https://api.openai.com/v1', api_key='k'))
        with self.assertRaises(RuntimeError):
            make_complete(PREP_SCHEMA, model='m1', load=load)('x')
        self.assertEqual(log['requests'], [])

    def test_tool_calls_and_oversize_and_bad_json_are_refused_and_cleaned_up(self):
        for kw in (dict(calls=[object()]), dict(output='x' * 300_000), dict(output='kein json')):
            load, log = fakes(**kw)
            with self.assertRaises(ValueError):
                make_complete(PREP_SCHEMA, model='m1', load=load)('x')
            self.assertTrue(log['closed'] and log['finished'])

    def test_model_must_come_from_configuration(self):
        for bad in (None, '', '  '):
            with self.assertRaises(ValueError):
                make_complete(PREP_SCHEMA, model=bad)
        with self.assertRaises(ValueError):
            prep_factory({'synthesis': {'mode': 'on'}})

    def test_building_the_adapter_calls_nothing_and_drafter_still_needs_approval(self):
        called = []
        factory = lambda p: called.append(1) or make_complete(PREP_SCHEMA, model='m1', load=lambda: called.append('load') or {})
        from gtm.prep_adapters import ReviewRequired
        with self.assertRaises(ReviewRequired):
            build_drafter({'synthesis': {'mode': 'on', 'model': 'm1'}}, factory)
        self.assertEqual(called, [])
        d = build_drafter({'synthesis': {'mode': 'dry_run', 'model': 'm1', 'transmission_approved': True}}, factory, ledger=object())
        self.assertEqual(called, [1])   # the factory built a closure; the Hermes modules are still not loaded

    def test_strict_schema_requires_all_properties(self):
        s = strict_schema(PREP_SCHEMA)
        self.assertEqual(s['required'], ['known', 'open_questions'])
        self.assertEqual(s['properties']['known']['items']['required'], ['text', 'ref', 'quote'])

if __name__ == '__main__':
    unittest.main()
