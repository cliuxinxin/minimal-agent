"""Durable, visible direction updates at the HTTP/runner boundary."""
import copy
import http.client
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from miniagent.model import ModelReply, OpenAIModel
from miniagent.web import Handler, RequestError, RunPresentation, WebState


class SteeringWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = WebState(self.root)
        self.state.save_config({'provider': 'openai', 'model': 'fixture',
                                'api_key': 'private-steering-fixture',
                                'base_url': 'https://provider.example/v1'})
        sid = self.state.new_session()['id']
        self.session = self.state.get_session(sid)

    def active(self):
        run = {'id': 'active-run', 'status': 'running', 'step': 1, 'error': None,
               'events': [], 'event_count': 0, 'omitted_events': 0,
               'accepting_input': True, 'started_at': '2026-10-26T00:00:00+00:00'}
        self.session['detail']['run'] = run
        self.session['stop'] = threading.Event()
        self.state.assistant_message(self.session, run)
        self.state.checkpoint(self.session)
        return run

    def steer(self, message='只处理本地数据', input_id='input-1', run_id='active-run'):
        self.assertTrue(callable(getattr(self.state, 'steer', None)), 'Live steering is missing')
        with self.state.lock:
            return self.state.steer(self.session, {'run_id': run_id,
                                   'input_id': input_id, 'message': message})

    def test_accepted_correction_is_durable_idempotent_and_not_applied_by_http(self):
        run = self.active()
        before = copy.deepcopy(self.session['agent'].history)
        first = self.steer()
        self.assertEqual(first['input']['status'], 'pending')
        self.assertEqual(self.session['agent'].history, before)
        record = self.state.store.load()[0]
        self.assertEqual(record['detail']['steering'][0]['message'], '只处理本地数据')
        self.assertEqual(record['detail']['steering'][0]['status'], 'pending')
        self.assertEqual(self.steer()['input']['id'], first['input']['id'])
        self.assertEqual(len(self.session['detail']['steering']), 1)
        for _ in range(2):
            inputs = self.state.pending_inputs(self.session, run)
            self.assertEqual([item['id'] for item in inputs], ['input-1'])
        self.assertEqual(self.session['detail']['steering'][0]['status'], 'pending')
        with self.assertRaises(RequestError) as raised:
            self.steer('different content')
        self.assertEqual(raised.exception.status, 409)

    def test_stale_stopping_external_and_finalizing_runs_reject_new_input(self):
        run = self.active()
        for changes in ({'id': 'new-run'}, {'status': 'stopping'}, {'accepting_input': False}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(run)
                run.update(changes)
                with self.assertRaises(RequestError) as raised:
                    self.steer()
                self.assertEqual(raised.exception.status, 409)
                run.clear(); run.update(original)
        self.session['external_active'] = True
        with self.assertRaises(RequestError) as raised:
            self.steer()
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(self.session['detail'].get('steering'))

    def test_input_validation_and_pending_queue_limit_leave_existing_entries_intact(self):
        self.active()
        self.assertTrue(callable(getattr(self.state, 'steer', None)), 'Live steering is missing')
        for invalid in ('', ' ', 'x' * 8001, 17):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(RequestError) as raised:
                    self.steer(invalid)
                self.assertEqual(raised.exception.status, 400)
        for i in range(10):
            self.steer('correction ' + str(i), 'item-' + str(i))
        with self.assertRaises(RequestError) as raised:
            self.steer('one too many', 'overflow')
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(self.session['detail']['steering']), 10)

    def test_storage_failure_does_not_acknowledge_or_dispatch_new_input(self):
        run = self.active()
        with patch.object(self.state.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(RequestError) as raised:
                self.steer()
        self.assertEqual(raised.exception.status, 503)
        self.assertFalse(self.session['detail'].get('steering'))
        self.assertEqual(self.state.pending_inputs(self.session, run), [])

    def test_final_read_closes_acceptance_only_when_queue_is_empty(self):
        run = self.active()
        self.steer()
        inputs = self.state.pending_inputs(self.session, run, final=True)
        self.assertEqual([item['id'] for item in inputs], ['input-1'])
        self.assertTrue(run['accepting_input'])
        agent = self.session['agent']
        agent.history.append({'role': 'user', 'content': '只处理本地数据'})
        agent.history_archive = copy.deepcopy(agent.history)
        agent.applied_input_ids = ['input-1']
        self.state.checkpoint(self.session)
        self.assertEqual(self.state.pending_inputs(self.session, run, final=True), [])
        self.assertFalse(run['accepting_input'])
        with self.assertRaises(RequestError) as raised:
            self.steer('too late', 'late')
        self.assertEqual(raised.exception.status, 409)
        restored = WebState(self.root).get_session(self.session['detail']['id'])
        self.assertEqual(restored['detail']['steering'][0]['status'], 'applied')
        self.assertEqual(restored['agent'].applied_input_ids, ['input-1'])
        self.assertEqual(restored['agent'].history[0]['content'], '只处理本地数据')

    def test_pending_correction_survives_restart_and_precedes_next_explicit_request(self):
        self.active()
        self.steer()
        restored_state = WebState(self.root)
        restored = restored_state.get_session(self.session['detail']['id'])
        self.assertEqual(restored['detail']['run']['status'], 'interrupted')
        self.assertEqual(restored['detail']['steering'][0]['status'], 'pending')
        self.assertFalse(restored['agent'].history)
        new_run = {'id': 'next-run', 'status': 'running', 'accepting_input': True}
        restored['detail']['run'] = new_run
        inputs = restored_state.pending_inputs(restored, new_run)
        self.assertEqual(inputs[0]['message'], '只处理本地数据')
        self.assertTrue(inputs[0]['before_task'])

    def test_restart_before_worker_starts_preserves_older_correction_before_newer_goal(self):
        self.active()
        self.steer('Use USD', input_id='older-currency')
        self.session['detail']['run']['status'] = 'cancelled'
        self.state.checkpoint(self.session)
        with patch('miniagent.web.threading.Thread.start'):
            with self.state.lock:
                self.state.start(self.session, {'message': 'Use EUR instead'})
        self.assertFalse(self.session['agent'].history)
        restored_state = WebState(self.root)
        restored = restored_state.get_session(self.session['detail']['id'])
        self.assertEqual([item['content'] for item in restored['agent'].history],
                         ['Use USD', 'Use EUR instead'])
        self.assertEqual(restored['agent'].history, restored['agent'].history_archive)
        self.assertEqual(restored['agent'].applied_input_ids, ['older-currency'])
        self.assertEqual(restored['detail']['steering'][0]['status'], 'applied')
        again = WebState(self.root).get_session(self.session['detail']['id'])
        self.assertEqual([item['content'] for item in again['agent'].history],
                         ['Use USD', 'Use EUR instead'])

    def test_work_plan_is_redacted_in_sse_and_restored_with_message_snapshot(self):
        run = self.active()
        plan = {'goal': '核对 private-steering-fixture 的结果', 'success_criteria': ['只用官方原文'],
                'steps': [{'title': '读取原文', 'status': 'in_progress'}],
                'summary': '需要核实日期', 'evidence': [], 'uncertainties': ['发布日期'],
                'verification': {'status': 'not_checked', 'summary': ''},
                'revision': 1, 'updated_at': '2026-10-26T00:00:00+00:00'}
        self.session['agent'].work_plan = copy.deepcopy(plan)
        message = self.state.assistant_message(self.session, run)
        view = RunPresentation(self.state, self.session, run, message)
        view.on_event({'event': 'work_plan', 'step': 1, 'data': {'plan': plan}})
        view.on_event({'event': 'checkpoint', 'step': 1, 'data': {'reason': 'work_plan'}})
        self.assertIn('work_plan', self.session['detail'], 'Plan events are not presented')
        self.assertIn('[redacted]', self.session['detail']['work_plan']['goal'])
        snapshots = [entry for entry in self.session['journal'] if entry['event'] == 'snapshot']
        self.assertTrue(snapshots)
        self.assertNotIn('private-steering-fixture', json.dumps(snapshots))
        restored = WebState(self.root).get_session(self.session['detail']['id'])
        self.assertEqual(restored['agent'].work_plan['success_criteria'], ['只用官方原文'])
        self.assertEqual(restored['detail']['messages'][-1]['work_plan']['revision'], 1)

    def test_superseded_tool_is_shown_not_executed(self):
        run = self.active()
        message = self.state.assistant_message(self.session, run)
        view = RunPresentation(self.state, self.session, run, message)
        view.on_event({'event': 'tool_call', 'step': 1, 'data': {
            'id': 'old-action', 'name': 'run_python', 'arguments': '{}'}})
        view.on_event({'event': 'tool_result', 'step': 1, 'data': {
            'id': 'old-action', 'name': 'run_python', 'ok': False,
            'error': {'code': 'superseded_by_user', 'message': 'Skipped after direction update.'}}})
        self.assertEqual(message['segments'][0]['status'], 'not_executed')

    def test_saved_public_redaction_survives_unavailable_original_credentials(self):
        run = self.active()
        self.steer('Do not reveal private-steering-fixture')
        plan = {'goal': 'Do not reveal private-steering-fixture', 'revision': 1}
        self.session['agent'].work_plan = plan
        view = RunPresentation(self.state, self.session, run,
                               self.state.assistant_message(self.session, run))
        view.on_event({'event': 'work_plan', 'step': 1, 'data': {'plan': plan}})
        self.state.checkpoint(self.session)
        self.state.save_config({'provider': 'anthropic', 'model': 'other', 'api_key': 'different-key'})
        restored = WebState(self.root).get_session(self.session['detail']['id'])
        self.assertIsNone(restored['agent'])
        self.assertNotIn('private-steering-fixture', json.dumps(restored['detail']))
        self.assertIn('[redacted]', restored['detail']['steering'][0]['message'])
        self.assertIn('[redacted]', restored['detail']['work_plan']['goal'])

    def test_http_route_accepts_valid_direction_and_rejects_duplicate_payload_change(self):
        self.active()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.state = self.state
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        path = '/api/sessions/' + self.session['detail']['id'] + '/steer'
        for content, expected in [('Use CSV', 202), ('Use CSV', 202), ('Use JSON', 409)]:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
            connection.request('POST', path, json.dumps({'run_id': 'active-run',
                               'input_id': 'http-input', 'message': content}),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, expected, body)
        self.assertEqual(self.state.store.load()[0]['detail']['steering'][0]['message'], 'Use CSV')

    def test_correction_arriving_during_final_model_reply_changes_actual_next_request(self):
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def model(messages, tools, prompt, **kwargs):
            calls.append(copy.deepcopy(messages))
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('Test did not release provider')
                text = 'old answer'
            else:
                text = 'corrected answer'
            return ModelReply(text, [], {'role': 'assistant', 'content': text})
        original_publish = self.state.publish_status
        def published(session, run):
            original_publish(session, run)
            if run['status'] not in {'running', 'stopping'}:
                ended.set()
        with patch.object(OpenAIModel, 'stream', side_effect=model), \
                patch.object(self.state, 'publish_status', side_effect=published):
            with self.state.lock:
                run_id = self.state.start(self.session, {'message': 'Original task'})['run_id']
            try:
                self.assertTrue(entered.wait(2))
                self.steer('Use only official sources', run_id=run_id)
            finally:
                release.set()
            self.assertTrue(ended.wait(5))
        self.assertEqual(len(calls), 2)
        self.assertIn('Use only official sources', calls[1][-1]['content'])
        self.assertEqual(self.session['detail']['messages'][-1]['content'], 'corrected answer')
        self.assertEqual(self.session['detail']['steering'][0]['status'], 'applied')
        self.assertFalse(self.session['detail']['run']['accepting_input'])


if __name__ == '__main__':
    unittest.main()
