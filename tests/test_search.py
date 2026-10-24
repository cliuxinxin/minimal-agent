"""DeepSeek search contracts at the urllib boundary; no live keys or network."""

import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from miniagent.search import DeepSeekSearch, SearchError, build_search_tools


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'private-search-test-key'
        self.client = DeepSeekSearch(self.secret)
        self.tools = {tool.name: tool for tool in build_search_tools(self.secret)}

    @staticmethod
    def item(**changes):
        result = {'title': 'A useful page', 'url': 'https://example.com/article',
                  'summary': 'A factual summary', 'snippet': 'Short preview',
                  'date': '2026-10-17T00:00:00+00:00'}
        result.update(changes)
        return result

    def call(self, action, body=None, raw=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw if raw is not None else json.dumps(body).encode()
        with patch('miniagent.search.urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            result = action()
        return result, build, response

    def test_search_uses_official_endpoint_auth_and_payload(self):
        result, build, response = self.call(lambda: self.client.search('最新文档'),
                                           {'results': [self.item()]})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/v1/tools/search')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.secret)
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data), {'query': '最新文档', 'count': 5,
                                                   'need_content': False})
        self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 30)
        response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
        for field in ('title', 'url', 'summary', 'snippet', 'date'):
            self.assertEqual(result['results'][0][field], self.item()[field])
        self.assertFalse(result['truncated'])
        self.assertNotIn(self.secret, json.dumps(result))

    def test_tools_are_read_only_and_do_not_expose_key(self):
        self.assertEqual(set(self.tools), {'web_search', 'fetch_webpage'})
        for tool in self.tools.values():
            self.assertTrue(tool.read_only)
            self.assertNotIn(self.secret, json.dumps(tool.definition()))

    def test_empty_search_is_successful(self):
        result, _, _ = self.call(lambda: self.tools['web_search'].execute({'query': 'unmatched'}),
                                 {'results': []})
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['results'], [])
        self.assertFalse(result['result']['truncated'])

    def test_search_argument_limits_are_validated_before_network(self):
        invalid = [{'query': ''}, {'query': '   '}, {'query': 'x' * 129},
                   {'query': 'valid', 'count': 0}, {'query': 'valid', 'count': 11},
                   {'query': 'valid', 'count': True}, {'query': 'valid', 'extra': 1}]
        with patch('miniagent.search.urllib.request.build_opener') as build:
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    result = self.tools['web_search'].execute(arguments)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error']['code'], 'invalid_arguments')
            build.assert_not_called()

    def test_invalid_response_shapes_are_rejected(self):
        bodies = [[], {}, {'results': None}, {'results': [None]},
                  {'results': [self.item(summary=None)]},
                  {'results': [self.item(date=123)]},
                  {'results': [{'title': 'Missing fields'}]}]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(SearchError):
                self.call(lambda: self.client.search('query'), body)

    def test_bad_json_and_oversized_responses_are_rejected(self):
        for raw in (b'not-json', b'\xff', b'{"results":NaN}',
                    b' ' * (2 * 1024 * 1024 + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(SearchError):
                self.call(lambda: self.client.search('query'), raw=raw)

    def test_http_errors_are_distinct_sanitized_and_not_retried(self):
        expected = {401: 'API Key', 402: 'balance', 429: 'rate limit',
                    404: 'page content', 500: 'HTTP 500', 302: 'redirect'}
        for status, message in expected.items():
            error = HTTPError('https://api.deepseek.com', status, self.secret, {},
                              io.BytesIO(self.secret.encode()))
            with self.subTest(status=status), patch('miniagent.search.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(SearchError) as caught:
                    self.client.search('query')
                self.assertIn(message, str(caught.exception))
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_transport_errors_do_not_leak_secrets(self):
        for error in (URLError(self.secret), TimeoutError(self.secret),
                      OSError(self.secret)):
            with self.subTest(error=type(error).__name__), patch('miniagent.search.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = error
                result = self.tools['web_search'].execute({'query': 'query'})
                self.assertFalse(result['ok'])
                self.assertNotIn(self.secret, json.dumps(result))
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_redirect_handler_refuses_following_redirects(self):
        _, build, _ = self.call(lambda: self.client.search('query'), {'results': []})
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, 'Found', {},
                                                   'https://another.example'))

    def test_unsafe_result_urls_are_never_exposed(self):
        for url in ('javascript:alert(1)', 'file:///etc/passwd',
                    'https://user:password@example.com', 'https://example.com/\npage',
                    'https://example.com:bad/', 'https://example.com\\evil'):
            with self.subTest(url=url), self.assertRaises(SearchError):
                self.call(lambda: self.client.search('query'),
                          {'results': [self.item(url=url)]})

    def test_search_text_budget_preserves_complete_urls(self):
        items = [self.item(title='标题' * 200, url=f'https://example.com/{i}',
                           summary='summary\\"' * 500, snippet='摘要' * 500)
                 for i in range(10)]
        result, _, _ = self.call(lambda: self.client.search('query', count=10),
                                {'results': items})
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(json.dumps({'ok': True, 'result': result}, ensure_ascii=False)), 7500)
        self.assertEqual([item['url'] for item in result['results']],
                         [item['url'] for item in items])
        self.assertTrue(all(item['truncated'] for item in result['results']))

    def test_unexpected_extra_results_are_explicitly_bounded(self):
        items = [self.item(url=f'https://example.com/{i}') for i in range(8)]
        result, _, _ = self.call(lambda: self.client.search('query', count=5),
                                {'results': items})
        self.assertEqual(len(result['results']), 5)
        self.assertEqual(result['omitted_results'], 3)
        self.assertTrue(result['truncated'])

    def test_very_long_valid_search_url_is_omitted_without_being_broken(self):
        result, _, _ = self.call(lambda: self.client.search('query'),
                                {'results': [self.item(url='https://example.com/' + 'a' * 7900)]})
        self.assertEqual(result['results'], [])
        self.assertEqual(result['omitted_results'], 1)
        self.assertTrue(result['truncated'])

    def test_secrets_reflected_in_successful_response_are_redacted(self):
        result, _, _ = self.call(lambda: self.client.search('query'),
                                {'results': [self.item(summary=self.secret)]})
        self.assertNotIn(self.secret, json.dumps(result))
        with self.assertRaises(SearchError):
            self.call(lambda: self.client.search('query'),
                      {'results': [self.item(url='https://example.com/' + self.secret)]})

    def test_fetch_uses_official_endpoint_and_character_pagination(self):
        page = {'title': '文档', 'content': '一二三四五六七八九十'}
        result, build, _ = self.call(lambda: self.client.fetch('https://example.com/article',
                                                             offset=2, limit=3), page)
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/v1/tools/fetch')
        self.assertEqual(json.loads(request.data), {'url': 'https://example.com/article'})
        self.assertEqual(result['url'], 'https://example.com/article')
        self.assertEqual(result['title'], '文档')
        self.assertEqual(result['content'], '三四五')
        self.assertEqual(result['next_offset'], 5)
        self.assertTrue(result['truncated'])
        last, _, _ = self.call(lambda: self.client.fetch('https://example.com/article',
                                                        offset=8, limit=3), page)
        self.assertEqual(last['content'], '九十')
        self.assertIsNone(last['next_offset'])
        self.assertFalse(last['truncated'])

    def test_fetch_validation_precedes_network(self):
        invalid = [{'url': 'ftp://example.com'}, {'url': 'https://u:p@example.com'},
                   {'url': 'https://example.com/\tpage'}, {'url': 'https://example.com:99999/'},
                   {'url': 'https://'}, {'url': 'https://example.com/' + 'a' * 8192},
                   {'url': 'https://example.com', 'offset': -1},
                   {'url': 'https://example.com', 'limit': 10001}]
        with patch('miniagent.search.urllib.request.build_opener') as build:
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    result = self.tools['fetch_webpage'].execute(arguments)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error']['code'], 'invalid_arguments')
            build.assert_not_called()

    def test_fetch_bad_schema_is_rejected(self):
        for body in ({'title': 'Missing content'}, {'title': 12, 'content': 'text'},
                     {'title': 'Title', 'content': None}):
            with self.subTest(body=body), self.assertRaises(SearchError):
                self.call(lambda: self.client.fetch('https://example.com/article'), body)

    def test_fetch_output_budget_returns_correct_next_offset(self):
        page = {'title': 'Title', 'content': '\\"' * 10000}
        result, _, _ = self.call(lambda: self.client.fetch('https://example.com/article',
                                                         offset=5, limit=10000), page)
        self.assertLessEqual(len(json.dumps({'ok': True, 'result': result}, ensure_ascii=False)), 7500)
        self.assertGreater(len(result['content']), 0)
        self.assertEqual(result['content'], page['content'][5:result['next_offset']])
        self.assertEqual(result['next_offset'], 5 + len(result['content']))
        self.assertTrue(result['truncated'])
        self.assertEqual(result['url'], 'https://example.com/article')

    def test_fetch_requires_progress_when_reference_uses_output_budget(self):
        url = 'https://example.com/' + 'a' * 7200
        with self.assertRaises(SearchError):
            self.call(lambda: self.client.fetch(url),
                      {'title': 't' * 200, 'content': 'real page text'})

    def test_fetch_redacts_reflected_search_key(self):
        result, _, _ = self.call(lambda: self.client.fetch('https://example.com/article'),
                                {'title': self.secret, 'content': 'prefix ' + self.secret})
        self.assertNotIn(self.secret, json.dumps(result))

    def test_invalid_key_is_rejected_without_echo(self):
        for key in ('', ' ', self.secret + '\n', None):
            with self.subTest(key_type=type(key).__name__), self.assertRaises(ValueError) as caught:
                DeepSeekSearch(key)
            self.assertNotIn(self.secret, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
