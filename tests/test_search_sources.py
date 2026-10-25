"""Search evidence retains URL identity and caches fetched pages within one run."""

import unittest
from unittest.mock import patch

from miniagent.search import DeepSeekSearch
from miniagent.tools import tool_execution_context


class SearchSourceTests(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekSearch('source-test-key')

    def test_search_and_fetch_share_a_stable_observed_source_id(self):
        url = 'https://example.com/article'
        row = {'title': 'Article', 'url': url, 'summary': 'Summary', 'snippet': 'Snippet', 'date': None}
        with patch.object(self.client, '_post', side_effect=[{'results': [row]},
                                                           {'title': 'Article', 'content': 'Original text'}]):
            searched = self.client.search('article')['results'][0]
            fetched = self.client.fetch(url)
        self.assertTrue(searched.get('source_id'), 'Search needs stable source IDs')
        self.assertEqual(searched['source_id'], fetched['source_id'])
        self.assertFalse(searched['read'])
        self.assertTrue(fetched['read'])

    def test_fetch_cache_is_shared_across_offsets_but_not_across_runs(self):
        page = {'title': 'Original', 'content': 'abcdef'}
        with patch.object(self.client, '_post', return_value=page) as request:
            with tool_execution_context(lambda: False, run_id='one'):
                first = self.client.fetch('https://example.com/article', limit=3)
                second = self.client.fetch('https://example.com/article', offset=3, limit=3)
            self.assertFalse(first.get('cached', True), 'The first fetch must disclose a network read')
            self.assertTrue(second.get('cached'), 'Same-run rereads should reuse the fetched document')
            self.assertEqual(second['content'], 'def')
            self.assertEqual(request.call_count, 1)
            with tool_execution_context(lambda: False, run_id='two'):
                third = self.client.fetch('https://example.com/article')
            self.assertFalse(third['cached'])
            self.assertEqual(request.call_count, 2)

    def test_no_execution_context_never_caches_and_reflected_key_is_redacted(self):
        with patch.object(self.client, '_post', return_value={'title': 'source-test-key',
                                                             'content': 'source-test-key'}) as request:
            first = self.client.fetch('https://example.com/article')
            self.client.fetch('https://example.com/article')
        self.assertEqual(request.call_count, 2)
        self.assertFalse(first.get('cached', True))
        self.assertNotIn('source-test-key', str(first))


if __name__ == '__main__':
    unittest.main()
