"""On-demand checks validate draft settings and never execute user tools."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from miniagent.model import ModelReply, ModelError, OpenAIModel, ToolCall
from miniagent.web import WebState


class ConnectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = WebState(Path(self.tmp.name))
        self.config = {'provider': 'openai', 'model': 'fixture', 'api_key': 'connection-private-key'}

    def test_real_tool_round_trip_is_checked_without_saving_or_starting_session(self):
        self.assertTrue(hasattr(self.state, 'check_connection'), 'Connection checks are missing')
        def stream(messages, tools, prompt, **kwargs):
            if len(messages) == 1:
                call = ToolCall('check', 'connection_echo', '{"value":"minimal-agent-check"}')
                return ModelReply('', [call], {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments}}]})
            self.assertEqual(json.loads(messages[-1]['content'])['result'], 'minimal-agent-check')
            kwargs['on_delta']({'type': 'text_delta', 'text': 'OK'})
            return ModelReply('OK', [], {'role': 'assistant', 'content': 'OK'})
        with patch.object(OpenAIModel, 'stream', side_effect=stream):
            result = self.state.check_connection({'target': 'model', 'config': self.config})
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.state.root / 'config.json').exists())
        self.assertEqual(self.state.sessions, {})
        self.assertNotIn('connection-private-key', json.dumps(result))

    def test_auth_error_has_actionable_classification_and_no_body_or_secret(self):
        self.assertTrue(hasattr(self.state, 'check_connection'), 'Connection checks are missing')
        with patch.object(OpenAIModel, 'stream', side_effect=ModelError(
                'do not copy connection-private-key business data', diagnostic={'http_status': 401})):
            result = self.state.check_connection({'target': 'model', 'config': self.config})
        self.assertFalse(result['ok'])
        self.assertEqual(result['checks'][0]['code'], 'authentication')
        self.assertNotIn('business data', json.dumps(result))
        self.assertNotIn('connection-private-key', json.dumps(result))

    def test_tool_incompatible_model_is_reported_as_failed(self):
        self.assertTrue(hasattr(self.state, 'check_connection'), 'Connection checks are missing')
        reply = ModelReply('hello', [], {'role': 'assistant', 'content': 'hello'})
        with patch.object(OpenAIModel, 'stream', return_value=reply):
            result = self.state.check_connection({'target': 'model', 'config': self.config})
        self.assertFalse(result['ok'])
        self.assertTrue(any(c.get('code') == 'tool_call_unverified' for c in result['checks']))

    def test_search_can_be_checked_before_main_model_is_configured(self):
        from miniagent.search import DeepSeekSearch
        with patch.object(DeepSeekSearch, '_post', return_value={'results': []}):
            result = self.state.check_connection({'target': 'search', 'config': {'search_api_key': 'search-only-key'}})
        self.assertTrue(result['ok'])
        self.assertFalse((self.state.root / 'config.json').exists())


if __name__ == '__main__':
    unittest.main()
