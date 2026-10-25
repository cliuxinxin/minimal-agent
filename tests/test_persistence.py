"""Restart recovery preserves native context without replaying uncertain writes."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from miniagent.model import ModelReply, OpenAIModel
from miniagent.web import WebState


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def configured(self):
        state = WebState(self.root)
        state.save_config({'provider': 'openai', 'model': 'test-model',
                           'api_key': 'private-persistence-key', 'base_url': 'https://gateway.example/v1'})
        return state

    def complete_run(self, state, sid, text='first'):
        reply = ModelReply('saved answer', [], {'role': 'assistant', 'content': 'saved answer'})
        with patch.object(OpenAIModel, 'stream', return_value=reply):
            session = state.get_session(sid)
            with state.lock:
                state.start(session, {'message': text})
            for _ in range(300):
                with state.lock:
                    if session['detail']['run']['status'] not in {'running', 'stopping'}:
                        return session
                time.sleep(.01)
        self.fail('Run did not finish')

    def test_completed_conversation_and_native_history_survive_restart(self):
        state = self.configured()
        sid = state.new_session()['id']
        self.complete_run(state, sid)
        restarted = WebState(self.root)
        self.assertIn(sid, restarted.sessions, 'Restart lost the saved conversation')
        session = restarted.get_session(sid)
        self.assertEqual([m['content'] for m in session['detail']['messages']], ['first', 'saved answer'])
        self.assertTrue(session['detail']['resume_available'])
        self.assertEqual(session['agent'].history, [
            {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'saved answer'}])
        self.complete_run(restarted, sid, 'continue with the previous answer')
        again = WebState(self.root).get_session(sid)
        self.assertEqual(len(again['agent'].history), 4)
        self.assertEqual(again['detail']['run']['status'], 'completed')

    def test_changed_provider_keeps_history_readable_but_blocks_credential_reuse(self):
        state = self.configured()
        sid = state.new_session()['id']
        self.complete_run(state, sid)
        state.save_config({'provider': 'anthropic', 'model': 'other', 'api_key': 'new-private-key'})
        restarted = WebState(self.root)
        self.assertIn(sid, restarted.sessions, 'Saved history was not loaded')
        session = restarted.get_session(sid)
        self.assertFalse(session['detail']['resume_available'])
        self.assertEqual(session['detail']['messages'][-1]['content'], 'saved answer')
        with self.assertRaisesRegex(Exception, '接口|提供商|服务商|配置'):
            restarted.start(session, {'message': 'continue'})

    def test_session_config_does_not_duplicate_credentials(self):
        state = self.configured()
        sid = state.new_session()['id']
        self.complete_run(state, sid)
        self.assertTrue(hasattr(state, 'store'), 'There is no durable store')
        records = state.store.load()
        self.assertNotIn('private-persistence-key', json.dumps(records))
        self.assertEqual(records[0]['settings']['model'], 'test-model')

    def test_interrupted_tool_gets_unknown_outcome_without_execution(self):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.persistence'), 'Recovery module missing')
        from miniagent.persistence import repair_history
        history = [{'role': 'user', 'content': 'write file'},
                   {'role': 'assistant', 'content': None, 'tool_calls': [
                       {'id': 'written', 'type': 'function', 'function': {'name': 'run_python', 'arguments': '{}'}},
                       {'id': 'unknown', 'type': 'function', 'function': {'name': 'run_python', 'arguments': '{}'}}]},
                   {'role': 'tool', 'tool_call_id': 'written', 'content': '{"ok":true,"result":"done"}'}]
        original = copy.deepcopy(history)
        repaired, ids = repair_history(history, 'openai')
        self.assertEqual(ids, ['unknown'])
        self.assertEqual(repaired[-1]['tool_call_id'], 'unknown')
        self.assertEqual(json.loads(repaired[-1]['content'])['error']['code'], 'interrupted_unknown_outcome')
        self.assertEqual(repaired[:3], original)
        self.assertEqual(history, original)
        self.assertEqual(repair_history(repaired, 'openai'), (repaired, []))

    def test_anthropic_recovery_preserves_signed_blocks_and_completed_results(self):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.persistence'), 'Recovery module missing')
        from miniagent.persistence import repair_history
        signed = {'type': 'thinking', 'thinking': 'public reasoning', 'signature': 'opaque-signature'}
        history = [{'role': 'user', 'content': 'work'}, {'role': 'assistant', 'content': [signed,
                   {'type': 'tool_use', 'id': 'a', 'name': 'run_python', 'input': {}},
                   {'type': 'tool_use', 'id': 'b', 'name': 'run_python', 'input': {}}]},
                   {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'a', 'content': 'done'}]}]
        repaired, ids = repair_history(history, 'anthropic')
        self.assertEqual(ids, ['b'])
        self.assertEqual(repaired[1]['content'][0], signed)
        self.assertEqual(repaired[2]['content'][0], history[2]['content'][0])
        self.assertEqual(repaired[2]['content'][1]['tool_use_id'], 'b')
        self.assertTrue(repaired[2]['content'][1]['is_error'])

    def test_persistence_failure_finishes_run_and_exposes_warning(self):
        state = self.configured()
        sid = state.new_session()['id']
        self.assertTrue(hasattr(state, 'store'), 'There is no persistence warning path')
        with patch.object(state.store, 'save', side_effect=OSError('disk full')):
            session = self.complete_run(state, sid)
        self.assertEqual(session['detail']['run']['status'], 'completed')
        self.assertTrue(session['detail'].get('storage_warning'))

    def test_restart_marks_active_run_interrupted_and_does_not_replay_tool(self):
        state = self.configured()
        sid = state.new_session()['id']
        session = state.get_session(sid)
        session['agent'].history = [
            {'role': 'user', 'content': 'write once'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'uncertain', 'type': 'function', 'function': {'name': 'run_python', 'arguments': '{}'}}]}]
        session['agent'].history_archive = copy.deepcopy(session['agent'].history)
        session['detail']['run'] = {'id': 'old-run', 'status': 'running', 'step': 1}
        session['detail']['messages'] = [{'role': 'assistant', 'run_id': 'old-run',
                                         'content': 'partial', 'status': 'running', 'segments': []}]
        state.checkpoint(session)
        with patch.object(OpenAIModel, 'stream', side_effect=AssertionError('Must not resume on page load')):
            restored = WebState(self.root).get_session(sid)
        self.assertEqual(restored['detail']['run']['status'], 'interrupted')
        self.assertEqual(restored['detail']['messages'][0]['status'], 'interrupted')
        self.assertEqual(restored['agent'].history[-1]['tool_call_id'], 'uncertain')
        self.assertIn('interrupted_unknown_outcome', restored['agent'].history[-1]['content'])

    def test_stale_background_cannot_overwrite_a_newer_session_snapshot(self):
        state = self.configured()
        sid = state.new_session()['id']
        self.complete_run(state, sid)
        newer = WebState(self.root)
        self.complete_run(newer, sid, 'newer conversation turn')
        with self.assertRaisesRegex(Exception, '后台|会话|更新'):
            with state.lock:
                state.start(state.get_session(sid), {'message': 'stale turn'})
        persisted = WebState(self.root).get_session(sid)
        self.assertEqual(persisted['agent'].history[-2]['content'], 'newer conversation turn')

    def test_store_rejects_a_stale_writer_instead_of_losing_new_messages(self):
        from miniagent.persistence import SessionStore
        first, second = SessionStore(self.root), SessionStore(self.root)
        record = {'detail': {'id': 'a' * 32, 'messages': [], 'updated_at': '2026-10-25'},
                  'settings': {'provider': 'openai'}, 'history': []}
        first.save(record)
        latest, stale = first.load()[0], second.load()[0]
        latest['history'] = [{'role': 'user', 'content': 'new information'}]
        first.save(latest)
        with self.assertRaisesRegex(ValueError, 'changed|newer|session'):
            second.save(stale)
        self.assertEqual(first.load()[0]['history'][0]['content'], 'new information')

    def test_live_other_process_is_not_mistaken_for_a_crashed_task(self):
        state = self.configured()
        sid = state.new_session()['id']
        record = state.store.load()[0]
        record['detail']['run'] = {'id': 'live-run', 'status': 'running', 'step': 1}
        record['owner_pid'] = 987654
        state.store.save(record)
        with patch('miniagent.web.process_alive', return_value=True, create=True):
            restarted = WebState(self.root)
            detail = restarted.get_session(sid)['detail']
        self.assertEqual(detail['run']['status'], 'running_elsewhere')
        self.assertFalse(detail['resume_available'])
        self.assertEqual(state.store.load()[0]['detail']['run']['status'], 'running')

    def test_external_session_becomes_resumable_when_its_owner_finishes(self):
        state = self.configured()
        sid = state.new_session()['id']
        record = state.store.load()[0]
        record['detail']['run'] = {'id': 'live-run', 'status': 'running', 'step': 1}
        record['owner_pid'] = 987654
        state.store.save(record)
        with patch('miniagent.web.process_alive', return_value=True):
            restarted = WebState(self.root)
        record = state.store.load()[0]
        record['detail']['run']['status'] = 'completed'
        state.store.save(record)
        refreshed = restarted.get_session(sid)['detail']
        self.assertTrue(refreshed['resume_available'], 'The completed external session stayed permanently locked')
        self.assertEqual(refreshed['run']['status'], 'completed')

    def test_task_accepted_before_worker_start_keeps_original_goal_after_restart(self):
        state = self.configured()
        sid = state.new_session()['id']
        with patch('miniagent.web.threading.Thread.start'):
            with state.lock:
                state.start(state.get_session(sid), {'message': 'Create a monthly report for my uploaded orders'})
        restored = WebState(self.root).get_session(sid)
        self.assertEqual(restored['detail']['run']['status'], 'interrupted')
        self.assertEqual(restored['agent'].history, [
            {'role': 'user', 'content': 'Create a monthly report for my uploaded orders'}])


if __name__ == '__main__':
    unittest.main()
