"""Behavioral tests for scripts/web_search.py against a local mock of the search endpoint."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import web_search  # noqa: E402

STATE = {'limited': 0, 'server': 0, 'last': {}}


def web_payload() -> dict:
    return {
        'Result': {
            'ResultCount': 2,
            'TimeCost': 42,
            'WebResults': [
                {
                    'SortId': 1,
                    'Title': 'DeepSeek V4 发布',
                    'SiteName': 'Example News',
                    'AuthInfoDes': '权威',
                    'Url': 'https://example.com/v4',
                    'Summary': '摘要内容',
                },
                {
                    'SortId': 2,
                    'Title': '长摘要结果',
                    'SiteName': 'Forum',
                    'Url': 'https://forum.example/1',
                    'Summary': '长' * 1200,
                },
            ],
        },
    }


IMAGE_PAYLOAD = {
    'Result': {
        'ResultCount': 1,
        'TimeCost': 7,
        'WebResults': [],
        'ImageResults': [
            {
                'SortId': 1,
                'Title': '故宫',
                'Image': {'Url': 'https://img.example/1.jpg', 'Width': 800, 'Height': 600, 'Shape': '横图'},
            },
        ],
    },
}


class Handler(BaseHTTPRequestHandler):
    """Mock search endpoint; behavior is selected by a word in the query."""

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length) or b'{}')
        STATE['last'] = payload
        query = str(payload.get('Query', ''))

        if 'empty' in query:
            return self._send(200, {'Result': {'ResultCount': 0, 'TimeCost': 1, 'WebResults': []}})
        if 'limited' in query:
            STATE['limited'] += 1
            if STATE['limited'] == 1:
                return self._send(429, {'message': 'rate limited'})
        if 'server' in query:
            STATE['server'] += 1
            if STATE['server'] == 1:
                return self._send(503, {'message': 'service unavailable'})
        if 'envelope' in query:
            return self._send(200, {'ResponseMetadata': {'Error': {'Code': '10403', 'Message': 'invalid api key'}}})
        if 'badjson' in query:
            return self._send(200, b'<html>not json</html>')
        if payload.get('SearchType') == 'image':
            return self._send(200, IMAGE_PAYLOAD)
        return self._send(200, web_payload())


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = web_search.main(list(argv))
        except SystemExit as exit_error:
            code = exit_error.code if isinstance(exit_error.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.url = f'http://127.0.0.1:{cls.server.server_address[1]}/search'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Deterministic credentials: no dotenv files, no inherited key, and retries
        # that do not sleep.
        mock.patch.dict(os.environ, {}, clear=False).start()
        for name in web_search.CREDENTIAL_ENV_NAMES:
            os.environ.pop(name, None)
        mock.patch.object(web_search, 'credential_file_paths', return_value=[]).start()
        mock.patch.object(web_search.time, 'sleep', return_value=None).start()
        mock.patch.object(web_search, 'INTERNAL_API_URL', self.url).start()
        self.addCleanup(mock.patch.stopall)
        STATE['limited'] = 0
        STATE['server'] = 0
        STATE['last'] = {}

    def test_web_search_prints_results(self):
        code, out, err = run_cli('普通查询', '--api-key', 'test-key')
        self.assertEqual(code, 0, err)
        self.assertIn('结果数: 2', out)
        self.assertIn('DeepSeek V4 发布', out)
        self.assertIn('https://example.com/v4', out)
        self.assertIn('Example News', out)

    def test_long_summary_is_marked_when_cut(self):
        code, out, _ = run_cli('普通查询', '--api-key', 'test-key')
        self.assertEqual(code, 0)
        self.assertIn(web_search.TRUNCATION_MARK, out)

    def test_max_chars_bounds_the_text_view(self):
        code, out, _ = run_cli('普通查询', '--api-key', 'test-key', '--max-chars', '40')
        self.assertEqual(code, 0)
        self.assertIn('输出已截断', out)
        self.assertLess(len(out), 300)

    def test_json_mode_returns_the_payload(self):
        code, out, _ = run_cli('普通查询', '--api-key', 'test-key', '--json')
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['Result']['ResultCount'], 2)
        self.assertEqual(payload['Result']['WebResults'][0]['Url'], 'https://example.com/v4')

    def test_image_search_formats_images(self):
        code, out, _ = run_cli('故宫', '--type', 'image', '--count', '1', '--api-key', 'test-key')
        self.assertEqual(code, 0)
        self.assertIn('https://img.example/1.jpg', out)
        self.assertIn('800x600', out)

    def test_empty_results_exit_zero(self):
        code, out, _ = run_cli('empty 查询', '--api-key', 'test-key')
        self.assertEqual(code, 0)
        self.assertIn('结果数: 0', out)

    def test_filters_reach_the_request(self):
        run_cli('普通查询', '--auth-level', '1', '--time-range', '2025-01-01..2025-03-01', '--api-key', 'test-key')
        body = STATE['last']
        self.assertEqual(body['Filter']['AuthInfoLevel'], 1)
        self.assertEqual(body['TimeRange'], '2025-01-01..2025-03-01')
        self.assertTrue(body['NeedSummary'])

    def test_query_rewrite_flag(self):
        run_cli('普通查询', '--query-rewrite', '--api-key', 'test-key')
        self.assertEqual(STATE['last']['QueryControl'], {'QueryRewrite': True})

    def test_429_is_retried_then_succeeds(self):
        code, out, err = run_cli('limited 查询', '--api-key', 'test-key')
        self.assertEqual(code, 0, err)
        self.assertEqual(STATE['limited'], 2)
        self.assertIn('结果数: 2', out)

    def test_503_is_retried_then_succeeds(self):
        code, _, err = run_cli('server 查询', '--api-key', 'test-key')
        self.assertEqual(code, 0, err)
        self.assertEqual(STATE['server'], 2)

    def test_error_envelope_is_reported(self):
        code, _, err = run_cli('envelope 查询', '--api-key', 'test-key')
        self.assertEqual(code, 1)
        self.assertIn('10403', err)
        self.assertIn('账号或权限异常', err)

    def test_unparsable_body_is_reported(self):
        code, _, err = run_cli('badjson 查询', '--api-key', 'test-key')
        self.assertEqual(code, 1)
        self.assertIn('不是合法 JSON', err)

    def test_missing_credentials_exit_one(self):
        code, _, err = run_cli('普通查询')
        self.assertEqual(code, 1)
        self.assertIn('未找到凭证', err)

    def test_usage_errors_exit_two(self):
        cases = {
            'blank query': ('   ',),
            'query too long': ('a' * 101,),
            'count below one': ('x', '--count', '0'),
            'web count too high': ('x', '--count', '51'),
            'image count too high': ('x', '--type', 'image', '--count', '6'),
            'bad range order': ('x', '--time-range', '2025-02-01..2025-01-01'),
            'malformed range': ('x', '--time-range', '2025-01-01'),
            'negative max chars': ('x', '--max-chars', '-1'),
        }
        for name, argv in cases.items():
            with self.subTest(case=name):
                code, _, _ = run_cli(*argv)
                self.assertEqual(code, 2)


class HelperTests(unittest.TestCase):
    def test_time_range_accepts_shortcuts_and_valid_ranges(self):
        for value in ('OneDay', 'OneWeek', 'OneMonth', 'OneYear', '2025-01-01..2025-12-31'):
            with self.subTest(value=value):
                self.assertEqual(web_search._validate_time_range(value), value)
        self.assertIsNone(web_search._validate_time_range(None))

    def test_time_range_rejects_invalid_values(self):
        for value in ('LastWeek', '2025-01-01', '2025-01-01..2025-13-01', '2025-02-01..2025-01-01'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_search._validate_time_range(value)

    def test_build_body_for_web_and_image(self):
        web = web_search.build_body('q', time_range='OneWeek', auth_level=1)
        self.assertEqual(web['TimeRange'], 'OneWeek')
        self.assertEqual(web['Filter'], {'AuthInfoLevel': 1})
        self.assertTrue(web['NeedSummary'])

        image = web_search.build_body('q', search_type='image', count=3)
        self.assertEqual(image['SearchType'], 'image')
        self.assertNotIn('NeedSummary', image)
        self.assertNotIn('TimeRange', image)

    def test_env_loader_reads_only_credential_variables(self):
        text = (
            '# comment\n'
            'WEB_SEARCH_API_KEY="quoted-key"\n'
            'export VOLCENGINE_ACCESS_KEY=ak-value\n'
            'SOME_OTHER_VARIABLE=leak\n'
        )
        with mock.patch('builtins.open', mock.mock_open(read_data=text)), \
                mock.patch.object(web_search.os.path, 'isfile', return_value=True):
            web_search.load_env_file('/nonexistent/.env')
        self.assertEqual(os.environ.get('WEB_SEARCH_API_KEY'), 'quoted-key')
        self.assertEqual(os.environ.get('VOLCENGINE_ACCESS_KEY'), 'ak-value')
        self.assertNotIn('SOME_OTHER_VARIABLE', os.environ)
        self.assertNotIn('VOLCENGINE_SECRET_KEY', os.environ)

    def test_resolve_api_key_prefers_the_flag(self):
        with mock.patch.dict(os.environ, {'WEB_SEARCH_API_KEY': 'env-key'}, clear=False):
            self.assertEqual(web_search.resolve_api_key('flag-key'), 'flag-key')
            self.assertEqual(web_search.resolve_api_key(None), 'env-key')
        os.environ.pop('WEB_SEARCH_API_KEY', None)
        self.assertIsNone(web_search.resolve_api_key(None))


if __name__ == '__main__':
    unittest.main()
