"""Compatibility and integration boundaries for optional agent capabilities."""
import json
import tempfile
import unittest
from pathlib import Path

from miniagent.config import load_agent
from miniagent.tools import Tool, ToolArgs
from miniagent.web import RequestError, WebState


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = WebState(self.root)
        self.original = {'provider': 'openai', 'model': 'test-model', 'api_key': 'model-secret'}

    def save(self, **changes):
        return self.state.save_config({**self.original, **changes})

    def stored(self):
        return json.loads((self.root / 'config.json').read_text())

    def test_old_four_field_config_keeps_working_and_gains_programming(self):
        (self.root / 'config.json').write_text(json.dumps(self.original))
        agent = load_agent(self.root / 'config.json', workspace=self.root / 'workspace')
        self.assertEqual(agent.max_steps, 80)
        self.assertEqual(set(agent.tools), {'list_files', 'read_file', 'run_python', 'create_tool', 'update_work_plan'})
        self.assertNotIn('calculate', agent.tools)

    def test_default_and_unlimited_limit_roundtrip_without_keys_in_response(self):
        self.assertEqual(self.state.masked_config().get('max_steps'), 80)
        self.save(max_steps=None, search_api_key='search-secret')
        masked = self.state.masked_config()
        self.assertIsNone(masked['max_steps'])
        self.assertTrue(masked['has_search_api_key'])
        self.assertNotIn('search-secret', json.dumps(masked))
        self.assertNotIn('model-secret', json.dumps(masked))
        agent = load_agent(self.root / 'config.json', workspace=self.root / 'workspace')
        self.assertIsNone(agent.max_steps)
        self.assertIn('web_search', agent.tools)
        self.assertIn('fetch_webpage', agent.tools)

    def test_omitted_new_settings_and_empty_search_key_keep_existing_values(self):
        self.save(max_steps=800, search_api_key='search-secret')
        self.save(api_key='')
        self.assertEqual(self.stored()['max_steps'], 800)
        self.assertEqual(self.stored()['search_api_key'], 'search-secret')
        self.save(search_api_key='')
        self.assertEqual(self.stored()['search_api_key'], 'search-secret')

    def test_explicit_null_removes_search_key_without_changing_model_key(self):
        self.save(search_api_key='search-secret')
        response = self.save(api_key='', search_api_key=None)
        self.assertFalse(response['has_search_api_key'])
        self.assertIsNone(self.stored()['search_api_key'])
        self.assertEqual(self.stored()['api_key'], 'model-secret')

    def test_invalid_limit_or_search_key_does_not_overwrite_config(self):
        self.save()
        baseline = self.stored()
        for field, value in [('max_steps', 0), ('max_steps', -1), ('max_steps', True),
                             ('max_steps', '800'), ('search_api_key', 42)]:
            with self.subTest(field=field, value=value), self.assertRaises(RequestError):
                self.save(**{field: value})
            self.assertEqual(self.stored(), baseline)

    def test_host_approves_programming_but_not_unrelated_write_tool(self):
        self.save()
        agent = load_agent(self.root / 'config.json', workspace=self.root / 'workspace')
        self.assertIn('run_python', agent.tools)
        self.assertTrue(agent.approve(agent.tools['run_python'], {'code': 'print(1)'}))
        other = Tool('unrelated_write', 'Not pre-authorized.', ToolArgs, lambda: 'changed')
        self.assertFalse(agent.approve(other, {}))

    def test_tool_inventory_available_before_model_configuration(self):
        self.assertTrue(callable(getattr(self.state, 'tool_inventory', None)))
        inventory = self.state.tool_inventory()
        self.assertEqual(inventory['saved_tools'], [])
        self.assertIn('run_python', [tool['name'] for tool in inventory['tools']])
        self.assertNotIn('web_search', [tool['name'] for tool in inventory['tools']])

    def test_secret_redaction_also_covers_arbitrary_json_object_keys(self):
        from miniagent.web import redact
        payload = {'result': {'search-secret': ['model-secret', {'search-secret': 1}]}}
        sanitized = redact(payload, ['model-secret', 'search-secret'])
        self.assertNotIn('search-secret', json.dumps(sanitized))
        self.assertNotIn('model-secret', json.dumps(sanitized))

    def test_browser_trace_uses_bounded_previews_without_changing_log_event(self):
        import miniagent.web as web
        self.assertTrue(callable(getattr(web, 'browser_event', None)))
        event = {'event': 'tool_result', 'data': {'ok': True, 'name': 'run_python',
                 'result': {'stdout': 'x' * 50000}}}
        preview = web.browser_event(event)
        self.assertTrue(preview['data']['result']['truncated'])
        self.assertLess(len(json.dumps(preview)), 5000)
        self.assertEqual(len(event['data']['result']['stdout']), 50000)

    def test_long_run_keeps_recent_browser_events_and_complete_disk_log(self):
        import threading
        from miniagent.agent import RunResult
        self.save()
        detail = self.state.new_session()
        session = self.state.get_session(detail['id'])
        class EventSource:
            history = []
            history_archive = []
            context_summary = ''
            compaction_count = 0

            def run(self, task, should_stop, pending_input=None):
                for step in range(150):
                    self.on_event({'event': 'tool_result', 'step': step,
                                   'data': {'name': 'run_python', 'ok': True,
                                            'result': {'stdout': 'x' * 12000}}})
                return RunResult('completed', 'done', 150, 150)
        session['agent'] = EventSource()
        session['stop'] = threading.Event()
        run = {'id': 'long-run', 'status': 'running', 'step': 0, 'events': [],
               'event_count': 0, 'omitted_events': 0}
        self.state.work(session, 'long task', run)
        self.assertEqual(run['event_count'], 150)
        self.assertEqual(len(run['events']), 100)
        self.assertEqual(run['omitted_events'], 50)
        self.assertEqual(run['events'][0]['step'], 50)
        self.assertLess(len(json.dumps(run)), 500000)
        lines = (self.root / 'runs' / 'long-run.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 150)
        self.assertEqual(len(json.loads(lines[0])['data']['result']['stdout']), 12000)


if __name__ == '__main__':
    unittest.main()
