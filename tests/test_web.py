"""Loopback HTTP boundary tests; model requests are replaced only in tests."""
import http.client
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.model import ModelReply, OpenAIModel, ToolCall


class WebTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.web'), 'Web backend must exist')
        from miniagent.web import create_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = create_server(self.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, data=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        request_headers = {'Content-Type': 'application/json', **(headers or {})}
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        content = response.read()
        status = response.status
        conn.close()
        return status, json.loads(content) if content else None

    def configure(self, **changes):
        return self.request('POST', '/api/config', {'provider': 'openai', 'model': 'test-model',
                                                   'api_key': 'secret-not-for-browser', **changes})

    def session(self):
        status, body = self.request('POST', '/api/sessions', {})
        self.assertEqual(status, 201, body)
        return body['session']['id']

    def wait_done(self, sid):
        for _ in range(200):
            status, body = self.request('GET', '/api/sessions/' + sid)
            if body['session']['run']['status'] not in ('running', 'stopping'):
                return body['session']
            time.sleep(.01)
        self.fail('Run did not finish')

    def test_config_masks_key_and_reuses_only_same_endpoint(self):
        self.assertFalse(self.request('GET', '/api/bootstrap')[1]['config']['configured'])
        status, body = self.configure(base_url='https://gateway.example/v1/')
        self.assertEqual(status, 200, body)
        self.assertNotIn('secret-not-for-browser', json.dumps(body))
        self.assertTrue(body['config']['has_api_key'])
        status, _ = self.configure(api_key='', base_url='https://gateway.example/v1')
        self.assertEqual(status, 200)
        stored = json.loads((self.root / 'config.json').read_text())
        self.assertEqual(stored['api_key'], 'secret-not-for-browser')
        self.assertEqual((self.root / 'config.json').stat().st_mode & 0o777, 0o600)
        for change in ({'provider': 'anthropic'}, {'base_url': 'https://another.example'}):
            self.assertEqual(self.configure(api_key='', **change)[0], 400)
        self.assertNotIn('secret-not-for-browser', json.dumps(self.request('GET', '/api/config')[1]))

    def test_invalid_config_is_not_saved_or_echoed(self):
        for changes in ({'provider': 'secret-not-for-browser'}, {'base_url': 'ftp://secret-not-for-browser'},
                        {'model': ''}, {'unknown': 'secret-not-for-browser'}):
            status, body = self.configure(**changes)
            self.assertEqual(status, 400)
            self.assertNotIn('secret-not-for-browser', json.dumps(body))
        self.assertFalse((self.root / 'config.json').exists())
        self.assertEqual(self.request('POST', '/api/sessions', {})[0], 400)

    def test_tool_inventory_and_extended_settings_over_http(self):
        status, inventory = self.request('GET', '/api/tools')
        self.assertEqual(status, 200)
        self.assertEqual(inventory['saved_tools'], [])
        self.assertNotIn('calculate', [item['name'] for item in inventory['tools']])
        status, body = self.configure(max_steps=None, search_api_key='search-secret-not-for-browser')
        self.assertEqual(status, 200, body)
        self.assertIsNone(body['config']['max_steps'])
        self.assertTrue(body['config']['has_search_api_key'])
        self.assertNotIn('search-secret-not-for-browser', json.dumps(body))
        inventory = self.request('GET', '/api/tools')[1]
        self.assertIn('web_search', [item['name'] for item in inventory['tools']])
        sid = self.session()
        detail = self.request('GET', '/api/sessions/' + sid)[1]['session']
        self.assertIsNone(detail['max_steps'])
        self.assertTrue(detail['search_enabled'])

    def test_create_and_use_python_tool_in_same_run_and_reload(self):
        self.configure()
        sid = self.session()
        calls = []
        def reply(name, arguments, call_id):
            encoded = json.dumps(arguments)
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': encoded}}]}
            return ModelReply('', [ToolCall(call_id, name, encoded)], message)
        def complete(messages, tools, system_prompt):
            names = [tool['name'] for tool in tools]
            calls.append(names)
            if len(calls) == 1:
                return reply('create_tool', {
                    'name': 'repeat_text', 'description': 'Repeat text a chosen number of times.',
                    'code': 'def run(arguments):\n    return arguments["text"] * arguments["count"]\n',
                    'example_arguments': {'text': 'hi', 'count': 2}, 'expected_result': 'hihi',
                }, 'create')
            if len(calls) == 2:
                self.assertIn('repeat_text', names)
                return reply('repeat_text', {'arguments': {'text': 'go', 'count': 3}}, 'invoke')
            self.assertEqual(json.loads(messages[-1]['content'])['result'], 'gogogo')
            return ModelReply('工具已保存并执行：gogogo', [], {'role': 'assistant', 'content': '工具已保存并执行：gogogo'})
        with patch.object(OpenAIModel, 'complete', side_effect=complete):
            self.request('POST', f'/api/sessions/{sid}/messages', {'message': '创建并运行文字重复工具'})
            detail = self.wait_done(sid)
        self.assertEqual(detail['run']['status'], 'completed', detail)
        inventory = self.request('GET', '/api/tools')[1]
        self.assertEqual(inventory['saved_tools'][0]['name'], 'repeat_text')
        self.assertIn('def run(arguments)', inventory['saved_tools'][0]['code'])
        from miniagent.config import load_agent
        restarted = load_agent(self.root / 'config.json', workspace=self.root / 'workspace')
        with patch.object(OpenAIModel, 'complete', return_value=ModelReply('ok', [], {'role': 'assistant', 'content': 'ok'})):
            self.assertEqual(restarted.run('加载已保存工具').status, 'completed')
        saved = restarted.tools['repeat_text']
        self.assertEqual(saved.execute({'arguments': {'text': 'persist', 'count': 1}}, restarted.approve),
                         {'ok': True, 'result': 'persist'})

    def test_search_secret_is_redacted_from_errors_events_and_disk_trace(self):
        secret = 'search-secret-not-for-browser'
        self.configure(search_api_key=secret)
        sid = self.session()
        with patch.object(OpenAIModel, 'complete', side_effect=RuntimeError('upstream echoed ' + secret)):
            self.request('POST', f'/api/sessions/{sid}/messages', {'message': '搜索'})
            detail = self.wait_done(sid)
        self.assertEqual(detail['run']['status'], 'error')
        self.assertNotIn(secret, json.dumps(detail))
        self.assertIn('[redacted]', detail['run']['error'])
        for path in (self.root / 'runs').glob('*.jsonl'):
            self.assertNotIn(secret, path.read_text())

    def test_real_agent_run_session_history_and_trace(self):
        self.configure()
        sid = self.session()
        reply = ModelReply('模型回答', [], {'role': 'assistant', 'content': '模型回答', 'thinking': 'private'})
        with patch.object(OpenAIModel, 'complete', return_value=reply):
            self.assertEqual(self.request('POST', f'/api/sessions/{sid}/messages', {'message': '用户问题'})[0], 202)
            detail = self.wait_done(sid)
        self.assertEqual([m['content'] for m in detail['messages']], ['用户问题', '模型回答'])
        self.assertEqual(detail['run']['status'], 'completed')
        self.assertEqual(detail['title'], '用户问题')
        self.assertNotIn('model_output', [e['event'] for e in detail['run']['events']])
        self.assertNotIn('private', json.dumps(detail))
        self.assertTrue(list((self.root / 'runs').glob('*.jsonl')))
        self.configure(model='new-model')
        self.assertEqual(self.request('GET', '/api/sessions/' + sid)[1]['session']['model'], 'test-model')

    def test_duplicate_run_rejected_and_stop_waits_for_inflight_request(self):
        self.configure()
        sid = self.session()
        entered, release = threading.Event(), threading.Event()
        def complete(*args):
            entered.set()
            release.wait(4)
            return ModelReply('late answer', [], {'role': 'assistant', 'content': 'late answer'})
        with patch.object(OpenAIModel, 'complete', side_effect=complete):
            status, body = self.request('POST', f'/api/sessions/{sid}/messages', {'message': 'hello'})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.request('POST', f'/api/sessions/{sid}/messages', {'message': 'again'})[0], 409)
                self.assertEqual(self.request('POST', f'/api/sessions/{sid}/stop', {'run_id': 'stale'})[0], 409)
                status, stopped = self.request('POST', f'/api/sessions/{sid}/stop', {'run_id': body['run_id']})
                self.assertEqual(status, 200)
                self.assertEqual(stopped['session']['run']['status'], 'stopping')
            finally:
                release.set()
            detail = self.wait_done(sid)
        self.assertEqual(detail['run']['status'], 'cancelled')
        self.assertEqual(len(detail['messages']), 2)
        self.assertNotEqual(detail['messages'][1]['content'], 'late answer')

    def test_upload_collision_traversal_and_size(self):
        for name in ('../bad.txt', 'a/b.txt', 'a\\b.txt', 'bad\0.txt', 'bad.py', '.'):
            self.assertEqual(self.request('POST', '/api/files', {'name': name, 'content': 'x'})[0], 400)
        for _ in range(2):
            self.assertEqual(self.request('POST', '/api/files', {'name': 'notes.md', 'content': '你好'})[0], 201)
        files = self.request('GET', '/api/files')[1]['files']
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f['size'] == 6 for f in files))
        self.assertNotEqual(files[0]['name'], files[1]['name'])
        self.assertEqual(self.request('POST', '/api/files', {'name': 'large.txt', 'content': 'x' * (2*1024*1024+1)})[0], 413)
        (self.root / 'workspace' / 'link.txt').symlink_to(self.root / 'config.json')
        self.assertNotIn('link.txt', [f['name'] for f in self.request('GET', '/api/files')[1]['files']])

    def test_upload_rejects_windows_paths_devices_and_unsafe_characters(self):
        names = ('D:outside.txt', 'file.txt:stream.txt', 'CON.txt', 'aux.md',
                 'PRN.json', 'NUL.csv', 'COM1.txt', 'lpt9.md', 'CON.extra.txt',
                 'bad?.txt', 'bad*.txt', 'bad<.txt', 'bad>.txt', 'bad|.txt',
                 'bad".txt', 'bad\x01.txt', 'bad\x7f.txt', 'trailing.txt.', 'trailing.txt ')
        for name in names:
            with self.subTest(name=name):
                status, _ = self.request('POST', '/api/files', {'name': name, 'content': 'x'})
                self.assertEqual(status, 400)
        self.assertEqual(list((self.root / 'workspace').iterdir()), [])

    def test_upload_rejects_destination_symlink_escape(self):
        outside = self.root / 'outside.txt'
        outside.write_text('untouched', encoding='utf-8')
        (self.root / 'workspace' / 'link.txt').symlink_to(outside)
        status, _ = self.request('POST', '/api/files', {'name': 'link.txt', 'content': 'changed'})
        self.assertEqual(status, 400)
        self.assertEqual(outside.read_text(encoding='utf-8'), 'untouched')

    def test_nonstring_api_keys_cannot_reuse_saved_key(self):
        self.configure()
        for key in (False, 0, [], None):
            status, body = self.configure(api_key=key)
            self.assertEqual(status, 400, body)

    def test_uploaded_file_is_read_by_real_tool_and_events_are_recorded(self):
        self.configure()
        self.request('POST', '/api/files', {'name': 'facts.txt', 'content': 'known content'})
        sid = self.session()
        call = ToolCall('read-1', 'read_file', '{"path":"facts.txt"}')
        replies = [ModelReply('', [call], {'role': 'assistant', 'content': ''}),
                   ModelReply('read successfully', [], {'role': 'assistant', 'content': 'read successfully'})]
        with patch.object(OpenAIModel, 'complete', side_effect=replies):
            self.request('POST', f'/api/sessions/{sid}/messages', {'message': 'Read facts.txt'})
            detail = self.wait_done(sid)
        result = next(e for e in detail['run']['events'] if e['event'] == 'tool_result')
        self.assertTrue(result['data']['ok'])
        self.assertEqual(result['data']['result']['text'], 'known content')
        self.assertEqual(detail['run']['step'], 2)

    def test_favicon_returns_empty_success(self):
        self.assertEqual(self.request('GET', '/favicon.ico'), (204, None))

    def test_headers_malformed_routes_and_health(self):
        self.assertEqual(self.request('GET', '/api/health')[1]['app'], 'minimal-agent-web')
        for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'},
                        {'Origin': f'http://127.0.0.1:{self.port+1}'}):
            self.assertEqual(self.request('POST', '/api/config', {}, headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/config', {}, {'Content-Type': 'text/plain'})[0], 415)
        for raw in (b'{', b'[]', b'{"provider":"openai","provider":"anthropic"}'):
            self.assertEqual(self.request('POST', '/api/config', raw=raw)[0], 400)
        for path in ('/config.json', '/api/sessions/missing', '/../config.json', '/web/../config.json'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.configure()
        sid = self.session()
        self.assertEqual(self.request('POST', f'/api/sessions/{sid}/messages', {'message':'x'*60001})[0], 400)


if __name__ == '__main__':
    unittest.main()
