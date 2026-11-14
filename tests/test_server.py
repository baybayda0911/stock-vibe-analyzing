"""S4 HTTP boundary tests; the task manager is a fake and never invokes a model."""

import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.research_tasks import START_KEYS
from app.server import PlatformServer


TASK_ID = 'R4-' + 'a' * 32
REVISION = 'b' * 64


class FakeResearch:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.closed = 0

    def providers(self):
        return [{'provider': 'codex', 'available': True}, {'provider': 'deepseek', 'available': True}]

    def close(self):
        self.closed += 1

    def list(self):
        self.calls.append(('list', None))
        return {'tasks': [{'task_id': TASK_ID, 'status': 'waiting_input'}], 'providers': self.providers()}

    def get(self, task_id):
        if self.failure:
            raise self.failure
        self.calls.append(('get', task_id))
        return {'task': {'task_id': task_id, 'status': 'waiting_input'}}

    def _operation(self, operation, body):
        fields = (START_KEYS if operation == 'start' else {'task_id', 'revision_token', 'question_id', 'answers'}
                  if operation == 'answer' else {'task_id', 'revision_token'})
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError('Fake manager enforces the declared task contract')
        if self.failure:
            raise self.failure
        self.calls.append((operation, body))
        status = {'start': 'queued', 'cancel': 'cancelled', 'resume': 'queued', 'answer': 'queued'}[operation]
        return {'task': {'task_id': TASK_ID, 'status': status}, 'reused': operation == 'start'}

    def start(self, body):
        return self._operation('start', body)

    def cancel(self, body):
        return self._operation('cancel', body)

    def resume(self, body):
        return self._operation('resume', body)

    def answer(self, body):
        return self._operation('answer', body)


class ResearchRouteTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeResearch()
        with patch('app.server.ResearchTasks', return_value=self.manager):
            self.server = PlatformServer(('127.0.0.1', 0), SimpleNamespace(), Path.cwd())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def headers(self):
        return {'Cookie': 'p1_session=' + self.server.token, 'Origin': self.server.origin,
                'X-CSRF-Token': self.server.csrf, 'Content-Type': 'application/json'}

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    @staticmethod
    def start_body():
        return {'security_id': 'CN-A-300308-SZ', 'module': 'joint', 'provider': 'deepseek', 'mode': 'full',
                'research_cutoff': '2026-11-14', 'question': '虚构研究', 'scope': '冻结材料',
                'source_ids': [], 'item_ids': [], 'idempotency_key': 'fixture-start-1'}

    def test_task_reads_require_session_and_exact_query_without_mutating(self):
        for path in ('/api/research-tasks', '/api/research-task?task_id=' + TASK_ID):
            self.assertEqual(self.request('GET', path)[0], 401)
            self.assertEqual(self.request('GET', path, headers={**self.headers(), 'Host': 'evil.example'})[0], 403)
        for path in ('/api/research-tasks?security_id=CN-A-300308-SZ', '/api/research-task',
                     '/api/research-task?task_id=' + TASK_ID + '&provider=codex',
                     '/api/research-task?task_id=' + TASK_ID + '&task_id=another'):
            self.assertEqual(self.request('GET', path, headers=self.headers())[0], 400)
        self.assertEqual(self.manager.calls, [])
        status, listing = self.request('GET', '/api/research-tasks', headers=self.headers())
        self.assertEqual(status, 200)
        self.assertEqual(listing['tasks'][0]['task_id'], TASK_ID)
        status, selected = self.request('GET', '/api/research-task?task_id=' + TASK_ID, headers=self.headers())
        self.assertEqual(status, 200)
        self.assertEqual(selected['task']['status'], 'waiting_input')
        self.assertEqual(self.manager.calls, [('list', None), ('get', TASK_ID)])

    def test_all_task_writes_require_local_session_origin_and_csrf(self):
        for operation in ('start', 'cancel', 'resume', 'answer'):
            path = '/api/research-' + operation
            for headers in ({}, {**self.headers(), 'Cookie': 'p1_session=wrong'},
                            {**self.headers(), 'Origin': 'https://evil.example'},
                            {**self.headers(), 'X-CSRF-Token': ''},
                            {**self.headers(), 'Host': 'evil.example'}):
                with self.subTest(operation=operation, headers=list(headers)):
                    self.assertEqual(self.request('POST', path, '{}', headers)[0], 403)
        self.assertEqual(self.manager.calls, [])

    def test_research_bodies_framing_and_route_are_strict_before_dispatch(self):
        path = '/api/research-start'
        for body in ('{', '[]', 'null', json.dumps({**self.start_body(), 'output_file': '/outside/report.md'})):
            self.assertEqual(self.request('POST', path, body, self.headers())[0], 400)
        self.assertEqual(self.request('POST', path, '{}', {**self.headers(), 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', path, '{}', {**self.headers(), 'Transfer-Encoding': 'chunked'})[0], 413)
        self.assertEqual(self.request('POST', path, 'x' * 32769, self.headers())[0], 413)
        self.assertEqual(self.request('POST', path + '?provider=codex', '{}', self.headers())[0], 404)
        self.assertEqual(self.request('POST', '/api/research-run', '{}', self.headers())[0], 404)
        self.assertEqual(self.manager.calls, [])

    def test_declared_operations_dispatch_exact_payload_and_preserve_manager_response(self):
        base = {'task_id': TASK_ID, 'revision_token': REVISION}
        payloads = {'start': self.start_body(), 'cancel': base, 'resume': base,
                    'answer': {**base, 'question_id': TASK_ID + '-Q0001-1', 'answers': '按冻结材料继续'}}
        for operation, body in payloads.items():
            with self.subTest(operation=operation):
                status, result = self.request('POST', '/api/research-' + operation, json.dumps(body), self.headers())
                self.assertEqual(status, 200)
                self.assertEqual(result['task']['task_id'], TASK_ID)
                self.assertEqual(result['reused'], operation == 'start')
        self.assertEqual(self.manager.calls, list(payloads.items()))

    def test_s4_answer_body_uses_research_limit_and_other_operations_keep_small_limit(self):
        body = {'task_id': TASK_ID, 'revision_token': REVISION,
                'question_id': TASK_ID + '-Q0001-1', 'answers': 'a' * 4000}
        encoded = json.dumps(body)
        self.assertGreater(len(encoded), 4096)
        self.assertEqual(self.request('POST', '/api/research-answer', encoded, self.headers())[0], 200)
        self.assertEqual(self.manager.calls, [('answer', body)])
        self.assertEqual(self.request('POST', '/api/watchlist', encoded, self.headers())[0], 413)

    def test_task_errors_map_to_safe_http_status_without_exception_details(self):
        for error, expected in ((ValueError('private-engine-path'), 400),
                                (FileExistsError('private-session-value'), 409),
                                (KeyError('private-identifier'), 404), (OSError('private-key-path'), 503)):
            self.manager.failure = error
            with self.subTest(error=type(error).__name__):
                status, result = self.request('POST', '/api/research-start', json.dumps(self.start_body()), self.headers())
                self.assertEqual(status, expected)
                self.assertNotIn('private-', json.dumps(result))
        self.manager.failure = FileNotFoundError('private-task-path')
        status, result = self.request('GET', '/api/research-task?task_id=' + TASK_ID, headers=self.headers())
        self.assertEqual(status, 404)
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(self.manager.calls, [])


if __name__ == '__main__':
    unittest.main()
