"""Real HTTP coverage of the SSE boundary; event sources stand in for paid APIs."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from miniagent.agent import RunResult
from miniagent.web import create_server


class EventSource:
    def __init__(self, program):
        self.program = program
        self.calls = 0

    def run(self, task, *, should_stop):
        self.calls += 1
        def emit(kind, step=1, **data):
            self.on_event({'event': kind, 'step': step, 'data': data})
        return self.program(emit, should_stop)


class SSEClient:
    def __init__(self, port, path, headers=None):
        self.connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        self.connection.request('GET', path, headers=headers or {})
        self.response = self.connection.getresponse()

    def frame(self):
        frame = {}
        while True:
            line = self.response.fp.readline()
            if not line:
                return None
            line = line.decode('utf-8').rstrip('\r\n')
            if not line:
                if frame:
                    return frame
                continue
            if line.startswith(':'):
                frame['comment'] = line[1:].strip()
            elif ': ' in line:
                key, value = line.split(': ', 1)
                frame[key] = json.loads(value) if key == 'data' else (int(value) if key == 'id' else value)

    def close(self):
        self.response.close()
        self.connection.close()


class WebStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = create_server(self.root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.request('POST', '/api/config', {'provider': 'openai', 'model': 'test-model',
                     'api_key': 'sk-very-secret-token', 'search_api_key': 'search-secret-token'})

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        connection.request(method, path, body=json.dumps(data) if data is not None else None,
                           headers={'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw) if raw else None

    def prepare(self, program):
        status, body = self.request('POST', '/api/sessions', {})
        self.assertEqual(status, 201, body)
        sid = body['session']['id']
        source = EventSource(program)
        self.server.state.sessions[sid]['agent'] = source
        return sid, source

    def start(self, sid):
        status, body = self.request('POST', f'/api/sessions/{sid}/messages', {'message': '开始'})
        self.assertEqual(status, 202, body)
        return body['run_id']

    def connect(self, sid, suffix='', headers=None):
        client = SSEClient(self.port, f'/api/sessions/{sid}/events{suffix}', headers)
        self.addCleanup(client.close)
        self.assertEqual(client.response.status, 200)
        self.assertTrue(client.response.getheader('Content-Type').startswith('text/event-stream'))
        return client

    def until(self, client, predicate, limit=100):
        frames = []
        for _ in range(limit):
            frame = client.frame()
            self.assertIsNotNone(frame, frames)
            frames.append(frame)
            if predicate(frame):
                return frames
        self.fail(f'Expected event was not delivered: {frames!r}')

    def done(self, sid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.request('GET', '/api/sessions/' + sid)[1]['session']
            if detail['run']['status'] not in ('running', 'stopping'):
                return detail
            time.sleep(.005)
        self.fail('Background run did not finish')

    def test_tokens_arrive_before_completion_and_finish_updates_the_placeholder(self):
        begin, finish = threading.Event(), threading.Event()
        self.addCleanup(begin.set)
        self.addCleanup(finish.set)
        def program(emit, stopping):
            emit('model_start')
            begin.wait(3)
            emit('reasoning_delta', text='实际返回的思考摘要')
            emit('text_delta', text='**正在生成**')
            finish.wait(3)
            emit('text_delta', text='答案')
            emit('model_complete', text='**正在生成**答案', has_tool_calls=False)
            emit('run_end', status='completed', answer='**正在生成**答案')
            return RunResult('completed', '**正在生成**答案', 1, 0)
        sid, _ = self.prepare(program)
        run_id = self.start(sid)
        client = self.connect(sid)
        initial = client.frame()
        self.assertEqual(initial['event'], 'snapshot')
        messages = initial['data']['session']['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]['run_id'], run_id)
        self.assertEqual(messages[-1]['status'], 'running')
        self.assertEqual(messages[-1]['content'], '')
        begin.set()
        early = self.until(client, lambda frame: frame.get('event') == 'segment_delta'
                           and frame['data']['text'] == '**正在生成**')
        self.assertTrue(any(frame.get('event') == 'segment_delta' and
                            frame['data']['text'] == '实际返回的思考摘要' for frame in early))
        self.assertEqual(self.request('GET', '/api/sessions/' + sid)[1]['session']['run']['status'], 'running')
        finish.set()
        ending = self.until(client, lambda frame: frame.get('event') == 'snapshot')
        detail = ending[-1]['data']['session']
        self.assertEqual(detail['run']['status'], 'completed')
        self.assertEqual(len(detail['messages']), 2)
        assistant = detail['messages'][-1]
        self.assertEqual(assistant['content'], '**正在生成**答案')
        self.assertEqual([part['type'] for part in assistant['segments']], ['reasoning', 'text'])
        self.assertEqual(assistant['segments'][-1]['text'], '**正在生成**答案')
        ids = [frame['id'] for frame in [initial, *early, *ending] if 'id' in frame]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertIsNone(client.frame())

    def test_reconnect_replays_frames_and_a_journal_gap_returns_current_snapshot(self):
        first, more, finish = threading.Event(), threading.Event(), threading.Event()
        emitted, flooded = threading.Event(), threading.Event()
        for gate in (first, more, finish):
            self.addCleanup(gate.set)
        def program(emit, stopping):
            first.wait(3)
            emit('text_delta', text='first')
            emitted.set()
            more.wait(3)
            for _ in range(600):
                emit('text_delta', text='x')
            flooded.set()
            finish.wait(3)
            return RunResult('completed', 'first' + 'x' * 600, 1, 0)
        sid, source = self.prepare(program)
        self.start(sid)
        initial_client = self.connect(sid)
        initial = initial_client.frame()
        cursor = initial['id']
        initial_client.close()
        first.set()
        self.assertTrue(emitted.wait(2))
        replay = self.connect(sid, headers={'Last-Event-ID': str(cursor)})
        frames = self.until(replay, lambda frame: frame.get('event') == 'segment_delta')
        self.assertTrue(all(frame.get('event') != 'snapshot' for frame in frames))
        self.assertTrue(all(frame['id'] > cursor for frame in frames))
        self.assertEqual(frames[-1]['data']['text'], 'first')
        replay.close()
        more.set()
        self.assertTrue(flooded.wait(2))
        recovered = self.connect(sid, '?after=' + str(cursor))
        snapshot = recovered.frame()
        self.assertEqual(snapshot['event'], 'snapshot')
        self.assertEqual(snapshot['data']['session']['messages'][-1]['segments'][0]['text'], 'first' + 'x' * 600)
        self.assertEqual(source.calls, 1)
        finish.set()
        self.done(sid)

    def test_idle_stream_has_heartbeats_and_stop_keeps_partial_output(self):
        ready, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def program(emit, stopping):
            emit('text_delta', text='已经收到的部分回答')
            ready.set()
            release.wait(3)
            return RunResult('cancelled' if stopping() else 'completed', '', 1, 0)
        sid, _ = self.prepare(program)
        run_id = self.start(sid)
        self.assertTrue(ready.wait(2))
        with patch('miniagent.web.SSE_HEARTBEAT_SECONDS', .03, create=True):
            client = self.connect(sid)
            self.assertEqual(client.frame()['event'], 'snapshot')
            self.assertIn('comment', client.frame())
            status, body = self.request('POST', f'/api/sessions/{sid}/stop', {'run_id': run_id})
            self.assertEqual(status, 200, body)
            stopped = self.until(client, lambda frame: frame.get('event') == 'run_status'
                                 and frame['data']['status'] == 'stopping')
            self.assertEqual(stopped[-1]['data']['run_id'], run_id)
            release.set()
            ending = self.until(client, lambda frame: frame.get('event') == 'snapshot')
        assistant = ending[-1]['data']['session']['messages'][-1]
        self.assertEqual(assistant['status'], 'cancelled')
        self.assertEqual(assistant['segments'][0]['text'], '已经收到的部分回答')
        self.assertIn('已经收到的部分回答', assistant['content'])

    def test_disconnect_does_not_cancel_or_repeat_background_tool_work(self):
        release = threading.Event()
        self.addCleanup(release.set)
        actions = []
        def program(emit, stopping):
            release.wait(3)
            self.assertFalse(stopping())
            actions.append('executed')
            emit('tool_call', id='call-1', name='run_python', arguments='{"code":"print(1)"}')
            emit('tool_result', id='call-1', name='run_python', ok=True, result={'stdout': '1'})
            return RunResult('completed', '完成', 1, 1)
        sid, source = self.prepare(program)
        self.start(sid)
        client = self.connect(sid)
        cursor = client.frame()['id']
        client.close()
        release.set()
        self.done(sid)
        reconnect = self.connect(sid, '?after=' + str(cursor))
        ending = self.until(reconnect, lambda frame: frame.get('event') == 'snapshot')
        self.assertEqual(ending[-1]['data']['session']['run']['status'], 'completed')
        self.assertEqual(actions, ['executed'])
        self.assertEqual(source.calls, 1)

    def test_new_turn_resets_old_cursors_without_retaining_old_history_snapshots(self):
        release = threading.Event()
        self.addCleanup(release.set)
        turns = []
        def program(emit, stopping):
            turns.append('turn')
            if len(turns) == 2:
                release.wait(3)
            return RunResult('completed', '已完成', 1, 0)
        sid, _ = self.prepare(program)
        self.start(sid)
        self.done(sid)
        first = self.connect(sid).frame()
        self.assertEqual(first['event'], 'snapshot')
        new_run_id = self.start(sid)
        recovered = self.connect(sid, '?after=' + str(first['id']))
        snapshot = recovered.frame()
        self.assertEqual(snapshot['event'], 'snapshot')
        self.assertGreater(snapshot['id'], first['id'])
        detail = snapshot['data']['session']
        self.assertEqual(detail['run']['id'], new_run_id)
        self.assertEqual(len(detail['messages']), 4)
        self.assertEqual(detail['messages'][-1]['run_id'], new_run_id)
        release.set()
        self.until(recovered, lambda frame: frame.get('event') == 'snapshot')
        with self.server.state.lock:
            journal = self.server.state.sessions[sid]['journal']
            snapshots = [frame for frame in journal if frame['event'] == 'snapshot']
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]['data']['session']['run']['id'], new_run_id)

    def test_chunked_secrets_are_redacted_without_publishing_native_opaque_blocks(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def program(emit, stopping):
            release.wait(3)
            for text in ('可见 sk-very-', 'secret-', 'token 内容'):
                emit('text_delta', text=text)
            for text in ('摘要 search-', 'secret-', 'token 结束'):
                emit('reasoning_delta', text=text)
            for index, text in enumerate(('{"key":"sk-very-', 'secret-', 'token"}')):
                emit('tool_call_delta', index=0, id='call-secret' if index else '',
                     name='run_python', arguments_delta=text)
            emit('model_output', message={'role': 'assistant', 'content': 'opaque-signature',
                 'thinking': {'signature': 'private-signature'}, 'encrypted_content': 'private-encrypted'})
            emit('model_complete', text='可见 sk-very-secret-token 内容', has_tool_calls=True)
            emit('tool_call', id='call-secret', name='run_python', arguments='{"key":"sk-very-secret-token"}')
            emit('tool_result', id='call-secret', name='run_python', ok=True,
                 result={'search-secret-token': 'sk-very-secret-token'})
            return RunResult('completed', '可见 sk-very-secret-token 内容', 1, 1)
        sid, _ = self.prepare(program)
        self.start(sid)
        client = self.connect(sid)
        initial = client.frame()
        release.set()
        frames = self.until(client, lambda frame: frame.get('event') == 'snapshot')
        browser = json.dumps([initial, *frames], ensure_ascii=False)
        for value in ('sk-very-secret-token', 'search-secret-token', 'opaque-signature',
                      'private-signature', 'private-encrypted'):
            self.assertNotIn(value, browser)
        detail = frames[-1]['data']['session']
        assistant = detail['messages'][-1]
        self.assertEqual(assistant['content'], '可见 [redacted] 内容')
        self.assertEqual(assistant['segments'][0]['text'], '可见 [redacted] 内容')
        self.assertEqual(assistant['segments'][1]['text'], '摘要 [redacted] 结束')
        tools = [part for part in assistant['segments'] if part['type'] == 'tool']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['status'], 'completed')
        self.assertEqual(tools[0]['arguments'], '{"key":"[redacted]"}')
        self.assertEqual(tools[0]['result'], {'[redacted]': '[redacted]'})
        self.assertTrue(all(event['event'] not in {'text_delta', 'reasoning_delta', 'tool_call_delta', 'model_output'}
                            for event in detail['run']['events']))
        for kind in ('text_delta', 'reasoning_delta'):
            collected = ''.join(frame['data']['text'] for frame in frames
                                if frame.get('event') == 'segment_delta'
                                and frame['data']['segment_id'] == assistant['segments'][0 if kind == 'text_delta' else 1]['id'])
            self.assertNotIn('sk-very-secret-token', collected)
            self.assertNotIn('search-secret-token', collected)
        log_events = [json.loads(line) for path in (self.root / 'runs').glob('*.jsonl')
                      for line in path.read_text().splitlines()]
        self.assertTrue(any(event['event'] == 'model_output' for event in log_events))
        for kind, field in (('text_delta', 'text'), ('reasoning_delta', 'text'), ('tool_call_delta', 'arguments_delta')):
            joined = ''.join(event['data'].get(field, '') for event in log_events if event['event'] == kind)
            self.assertNotIn('sk-very-secret-token', joined)
            self.assertNotIn('search-secret-token', joined)

    def test_long_runs_bound_segments_and_truncate_utf8_without_losing_final_answer(self):
        def program(emit, stopping):
            for step in range(1, 91):
                emit('text_delta', step=step, text=f'第 {step} 轮')
                emit('model_complete', step=step, text=f'第 {step} 轮', has_tool_calls=True)
            text = '中' * 30000
            emit('text_delta', step=91, text=text)
            emit('model_complete', step=91, text=text, has_tool_calls=False)
            return RunResult('completed', text, 91, 0)
        sid, _ = self.prepare(program)
        self.start(sid)
        detail = self.done(sid)
        assistant = detail['messages'][-1]
        self.assertEqual(len(assistant['segments']), 80)
        self.assertEqual(assistant['segments_omitted'], 11)
        self.assertTrue(assistant['segments'][-1]['truncated'])
        self.assertLessEqual(len(assistant['segments'][-1]['text'].encode('utf-8')), 64 * 1024)
        self.assertEqual(assistant['content'], '中' * 30000)

    def test_interleaved_native_text_and_tool_blocks_keep_conversation_order(self):
        def program(emit, stopping):
            emit('reasoning_delta', text='先检查数据')
            emit('text_delta', text='工具之前。')
            emit('tool_call_delta', index=1, id='block-call', name='list_files', arguments_delta='{}')
            emit('text_delta', text='工具之后。')
            emit('model_complete', text='工具之前。工具之后。', has_tool_calls=True)
            emit('tool_call', id='block-call', name='list_files', arguments='{}')
            emit('tool_result', id='block-call', name='list_files', ok=True, result=[])
            return RunResult('completed', '工具之前。工具之后。', 1, 1)
        sid, _ = self.prepare(program)
        self.start(sid)
        assistant = self.done(sid)['messages'][-1]
        parts = assistant['segments']
        self.assertEqual([part['type'] for part in parts], ['reasoning', 'text', 'tool', 'text'])
        self.assertEqual(parts[1]['text'], '工具之前。')
        self.assertEqual(parts[3]['text'], '工具之后。')
        self.assertEqual(parts[2]['status'], 'completed')
        self.assertEqual(''.join(part['text'] for part in parts if part['type'] == 'text'), assistant['content'])

    def test_failed_stream_retains_partial_text_and_finishes_preparing_tool(self):
        def program(emit, stopping):
            emit('text_delta', text='尚未结束的回答')
            emit('tool_call_delta', index=0, id='incomplete', name='run_python', arguments_delta='{"code":')
            raise RuntimeError('An interrupted upstream response')
        sid, _ = self.prepare(program)
        self.start(sid)
        detail = self.done(sid)
        assistant = detail['messages'][-1]
        self.assertEqual(detail['run']['status'], 'error')
        self.assertEqual(assistant['status'], 'error')
        self.assertEqual(assistant['segments'][0]['text'], '尚未结束的回答')
        self.assertEqual(assistant['segments'][1]['status'], 'error')
        self.assertIn('尚未结束的回答', assistant['content'])

    def test_model_complete_publishes_canonical_text_before_tools_finish(self):
        begin, finish = threading.Event(), threading.Event()
        self.addCleanup(begin.set)
        self.addCleanup(finish.set)
        def program(emit, stopping):
            begin.wait(3)
            emit('text_delta', text='准备调用工具')
            emit('model_complete', text='准备调用工具', has_tool_calls=True)
            finish.wait(3)
            return RunResult('completed', '准备调用工具', 1, 0)
        sid, _ = self.prepare(program)
        self.start(sid)
        client = self.connect(sid)
        client.frame()
        begin.set()
        frames = self.until(client, lambda frame: frame.get('event') == 'trace'
                            and frame['data']['event']['event'] == 'model_complete')
        canonical = [frame['data']['segment'] for frame in frames if frame.get('event') == 'segment'
                     and frame['data']['segment']['type'] == 'text']
        self.assertEqual(canonical[-1]['text'], '准备调用工具')
        finish.set()
        self.done(sid)

    def test_title_redacts_secret_before_truncating_user_text(self):
        sid, _ = self.prepare(lambda emit, stopping: RunResult('completed', '完成', 1, 0))
        message = '12345678901234567890sk-very-secret-token'
        status, _ = self.request('POST', f'/api/sessions/{sid}/messages', {'message': message})
        self.assertEqual(status, 202)
        detail = self.done(sid)
        self.assertNotIn('sk-very-', detail['title'])
        self.assertIn('[redact', detail['title'])
        self.assertEqual(detail['messages'][0]['content'], '12345678901234567890[redacted]')

    def test_prebuilt_chat_assets_are_served_without_exposing_build_sources(self):
        assets = self.root / 'web' / 'assets'
        assets.mkdir(parents=True)
        (assets / 'chat.js').write_text('export const ready = true;', encoding='utf-8')
        (assets / 'chat.css').write_text('.message { color: black; }', encoding='utf-8')
        (assets / 'private.js').write_text('private source', encoding='utf-8')
        for filename, content_type in (('chat.js', 'javascript'), ('chat.css', 'css')):
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            connection.request('GET', '/assets/' + filename)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(content_type, response.getheader('Content-Type'))
            self.assertTrue(response.read())
            connection.close()
        for path in ('/assets/private.js', '/assets/../private.js', '/frontend/package.json'):
            self.assertEqual(self.request('GET', path)[0], 404)

    def test_stream_checks_origin_and_rejects_malformed_cursor_before_headers(self):
        sid, _ = self.prepare(lambda emit, stopping: RunResult('completed', 'done', 1, 0))
        base = f'/api/sessions/{sid}/events'
        for suffix in ('?after=-1', '?after=x', '?after=1&after=2', '?api_key=do-not-accept'):
            self.assertEqual(self.request('GET', base + suffix)[0], 400)
        self.assertEqual(self.request('GET', base, headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/sessions/missing/events')[0], 404)
        future = self.connect(sid, '?after=999999')
        self.assertEqual(future.frame()['event'], 'snapshot')
        self.assertIsNone(future.frame())


if __name__ == '__main__':
    unittest.main()
