"""Local regression checks: no model inference, Telegram sends, or live queued messages."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import codex_usage as codex
import webapp

SID = '11111111-1111-4111-8111-111111111111'


class UsageTests(unittest.TestCase):
    def test_unique_responses_cache_and_partial_tail(self):
        def record(response, thread=SID):
            return {'timestamp': '2026-09-16T12:00:00Z', 'type': 'token_usage_record',
                    'payload': {'thread_id': thread, 'response_id': response,
                                'usage': {'input_tokens': 100, 'cached_input_tokens': 80,
                                          'output_tokens': 20, 'reasoning_output_tokens': 10}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            line = json.dumps(record('r1')) + '\n'
            pending = json.dumps(record('r2'))
            path.write_text(line + line + json.dumps(record('parent', 'other')) + '\n' + pending[:50])
            first = codex.parse_usage(path, SID)
            self.assertEqual(sum(sum(x.values()) for x in first['daily'].values()), 120)
            with path.open('a') as stream:
                stream.write(pending[50:] + '\n')
            second = codex.parse_usage(path, SID, first)
            self.assertEqual(sum(sum(x.values()) for x in second['daily'].values()), 240)
            third = codex.parse_usage(path, SID, second)
            self.assertEqual(third['daily'], second['daily'])
            self.assertEqual(next(iter(third['daily'].values()))['cache_read'], 160)

    def test_legacy_cumulative_events_are_not_summed(self):
        events = []
        for total in (120, 120, 240, 240):
            events.append({'timestamp': '2026-09-16T12:00:00Z', 'type': 'event_msg',
                           'payload': {'type': 'token_count', 'info': {
                               'total_token_usage': {'total_tokens': total},
                               'last_token_usage': {'input_tokens': 100, 'output_tokens': 20}}}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            path.write_text(''.join(json.dumps(e) + '\n' for e in events))
            data = codex.parse_usage(path, SID)
            self.assertEqual(sum(sum(x.values()) for x in data['daily'].values()), 240)

    def test_queue_argument_vector_and_failures(self):
        prompt = 'Повороты "test" `literal` $(no shell)\nnext line'
        with patch.object(codex, 'thread_info', return_value={'id': SID}), \
                patch.object(codex.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(codex.queue_message(SID, prompt)['queued'])
            self.assertEqual(run.call_args.args[0][-1], prompt)
            self.assertFalse(run.call_args.kwargs.get('shell', False))
            run.return_value = subprocess.CompletedProcess([], 1, '', 'queue failed')
            with self.assertRaises(RuntimeError):
                codex.queue_message(SID, prompt)
        with patch.object(codex, 'thread_info', return_value=None):
            with self.assertRaises(ValueError):
                codex.queue_message(SID, prompt)


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = webapp.ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, data, remote=False):
        headers = {'Content-Type': 'application/json'}
        if remote:
            headers['X-Forwarded-For'] = '192.0.2.1'
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.server.server_port}{webapp.BASE}/api/chat',
            data=json.dumps(data).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_auth_before_routing_and_explicit_provider(self):
        data = {'provider': 'codex', 'session_id': SID, 'prompt': 'test'}
        with patch.object(codex, 'queue_message', return_value={'ok': True, 'queued': True, 'response': 'queued'}) as queue, \
                patch.object(webapp, 'inject_live') as inject:
            self.assertEqual(self.post(data, remote=True)[0], 403)
            queue.assert_not_called()
            code, response = self.post(data)
            self.assertEqual(code, 200)
            self.assertTrue(response['queued'])
            queue.assert_called_once_with(SID, 'test')
            inject.assert_not_called()

    def test_provider_mismatch_cannot_start_claude(self):
        with patch.object(codex, 'thread_info', return_value={'id': SID}), \
                patch.object(webapp, 'inject_live') as inject:
            code, _ = self.post({'provider': 'claude', 'session_id': SID, 'prompt': 'test'})
            self.assertEqual(code, 400)
            inject.assert_not_called()

    def test_provider_pagination_preserves_total_and_open_session(self):
        snapshot = {'live_count': 0, 'session_count': 3, 'totals': {'tokens_total': 900},
                    'sessions': [{'session_id': str(i), 'provider': 'codex'} for i in range(3)]}
        with patch.object(webapp.cu, 'scan', return_value={'providers': {'codex': snapshot}}):
            url = f'http://127.0.0.1:{self.server.server_port}{webapp.BASE}/api/usage?provider=codex&limit=1&session_id=2'
            with urllib.request.urlopen(url, timeout=3) as response:
                result = json.load(response)
            self.assertEqual([s['session_id'] for s in result['sessions']], ['0', '2'])
            self.assertEqual(result['session_count'], 3)
            self.assertEqual(result['totals']['tokens_total'], 900)
            self.assertNotIn('providers', result)


if __name__ == '__main__':
    unittest.main()
