import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import load_agent
from miniagent.cli import main
from miniagent.model import AnthropicModel, OpenAIModel
from miniagent.tracing import JsonlTrace


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def write(self, provider='openai', **extra):
        content = {'provider': provider, 'model': 'test-model', 'api_key': 'test-secret', **extra}
        Path('config.json').write_text(json.dumps(content), encoding='utf-8')

    def test_openai_default_and_builtin_registration(self):
        self.write()
        agent = load_agent()
        self.assertIsInstance(agent.model, OpenAIModel)
        self.assertEqual(set(agent.tools), {'list_files', 'read_file', 'calculate'})

    def test_anthropic_default_and_custom_base(self):
        self.write('anthropic', base_url='https://gateway.example')
        agent = load_agent()
        self.assertIsInstance(agent.model, AnthropicModel)

    def test_unknown_configuration_is_rejected_without_secret_echo(self):
        self.write(custom_auth='test-secret')
        with self.assertRaises(ValueError) as caught:
            load_agent()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_provider_and_blank_key_are_rejected(self):
        for data in [dict(provider='unknown'), dict(api_key='')]:
            with self.subTest(data=data):
                self.write(**data)
                with self.assertRaises(ValueError):
                    load_agent()

    def test_missing_configuration_has_actionable_error(self):
        with self.assertRaises(ValueError) as caught:
            load_agent()
        self.assertIn('config.example.json', str(caught.exception))

    def test_cli_missing_config_exits_before_request(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            status = main(['hello'])
        self.assertEqual(status, 2)
        self.assertIn('config.example.json', output.getvalue())

    def test_cli_help_does_not_need_configuration(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--help'])
        self.assertEqual(caught.exception.code, 0)

    def test_trace_records_separate_valid_json_lines(self):
        trace = JsonlTrace(Path('runs'))
        for kind in ('run_start', 'run_end'):
            trace({'run_id': 'abc', 'event': kind, 'step': 1, 'data': {}})
        lines = Path('runs/abc.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual([json.loads(line)['event'] for line in lines], ['run_start', 'run_end'])


if __name__ == '__main__':
    unittest.main()
