import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_watcher as watcher
import email_realtime as realtime
import reauthorize_stealth as reauth


class OutageTests(unittest.TestCase):
    def test_reauth_rejects_wrong_redirect_and_state(self):
        with self.assertRaises(ValueError):
            reauth.validate_callback('https://evil.example/?code=x&state=ok', 'ok')
        with self.assertRaises(ValueError):
            reauth.validate_callback('http://localhost:1/?code=x&state=wrong', 'ok')
        self.assertEqual(reauth.validate_callback('http://localhost:1/?code=x&state=ok', 'ok'),
                         'http://localhost:1/?code=x&state=ok')

    def test_revoked_stealth_keeps_code_polling_without_advancing_stealth_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            prior = {'last_check': 1000, 'seen': {'code': [], 'stealth': []},
                     'pending': [], 'event': {'seq': 0, 'messages': []}}
            (data / 'state.json').write_text(json.dumps(prior))
            code = MagicMock()
            code.users().messages().get().execute.return_value = {'id': 'new-code', 'threadId': 'thread', 'labelIds': ['INBOX']}
            def get_service(alias):
                if alias == 'stealth':
                    raise RuntimeError('invalid_grant: Token has been expired or revoked.')
                return code
            output = io.StringIO()
            with patch.object(watcher, 'DATA', data), patch.object(watcher, 'STATE', data / 'state.json'), \
                 patch.object(watcher, 'service', side_effect=get_service), \
                 patch.object(watcher, 'list_inbox', return_value=[{'id': 'new-code'}]), \
                 patch.object(watcher, 'candidate', return_value=True), \
                 patch.object(watcher, 'summary', return_value={'account': 'code', 'id': 'new-code', 'internalDate': '1500'}), \
                 patch.object(watcher.time, 'time', return_value=2000), redirect_stdout(output):
                watcher.poll()
            result = json.loads(output.getvalue())
            saved = json.loads((data / 'state.json').read_text())
            self.assertEqual(result['errors'], {'stealth': 'authorization_expired'})
            self.assertEqual(result['messages'][0]['id'], 'new-code')
            self.assertEqual(saved['last_check_by_account']['code'], 2000)
            self.assertEqual(saved['last_check_by_account']['stealth'], 1000)
            self.assertEqual(saved['seen']['stealth'], [])
            self.assertEqual(saved['seen']['code'], ['new-code'])

    def test_revoked_token_is_backed_off_but_new_token_retries_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / 'state.json'
            state_path.write_text(json.dumps({'last_check': 1000, 'seen': {'code': [], 'stealth': []},
                                              'pending': [], 'event': {'seq': 0, 'messages': []}}))
            token = root / 'stealth.json'
            token.write_text('old')
            old_mtime = token.stat().st_mtime_ns
            calls = []

            def svc(alias):
                calls.append(alias)
                if alias == 'stealth' and len([c for c in calls if c == 'stealth']) == 1:
                    raise RuntimeError('invalid_grant: Token has been expired or revoked.')
                return alias

            with patch.object(watcher, 'STATE', state_path), patch.object(watcher, 'ACCOUNTS', {'code': root / 'code.json', 'stealth': token}), \
                 patch.object(watcher, 'service', side_effect=svc), patch.object(watcher, 'list_inbox', return_value=[]), \
                 patch.object(watcher.time, 'time', side_effect=[2000, 2010, 2020]):
                results = []
                for index in range(3):
                    if index == 2:
                        token.write_text('new')
                        os.utime(token, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
                    with redirect_stdout(io.StringIO()) as out:
                        watcher.poll()
                    results.append(json.loads(out.getvalue()))
            self.assertEqual(calls.count('stealth'), 2)
            self.assertEqual(results[0]['errors'], {'stealth': 'authorization_expired'})
            self.assertEqual(results[1]['errors'], {'stealth': 'authorization_expired'})
            self.assertEqual(results[2]['errors'], {})
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved['last_check_by_account']['stealth'], 2020)

    def test_outage_notice_once_per_continuous_failure_and_recovery(self):
        state = {'notifications': []}
        for _ in range(6):
            realtime.update_account_health(state, {'stealth': 'authorization_expired'})
        self.assertEqual(len(state['notifications']), 1)
        self.assertIn('Stealth', state['notifications'][0])
        self.assertIn('neu', state['notifications'][0].lower())
        self.assertNotIn('CODE', state['notifications'][0])
        realtime.update_account_health(state, {})
        self.assertEqual(len(state['notifications']), 2)
        self.assertIn('wiederhergestellt', state['notifications'][1])
        realtime.update_account_health(state, {})
        self.assertEqual(len(state['notifications']), 2)

    def test_generic_poll_failure_not_repeated_every_thirty_minutes(self):
        state = {'notifications': []}
        for _ in range(8):
            realtime.update_poll_health(state, failed=True)
        self.assertEqual(len(state['notifications']), 1)
        realtime.update_poll_health(state, failed=False)
        self.assertEqual(len(state['notifications']), 2)
        self.assertIn('wiederhergestellt', state['notifications'][1])
        realtime.update_poll_health(state, failed=False)
        self.assertEqual(len(state['notifications']), 2)


if __name__ == '__main__':
    unittest.main()
