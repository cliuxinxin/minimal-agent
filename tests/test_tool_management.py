"""Saved tools are maintainable without executing code during inventory or rollback."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from miniagent.programming import ProgrammingTools


class ToolManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.manager = ProgrammingTools(self.root, self.workspace)

    def create(self, **changes):
        arguments = dict(name='double_number', description='Double a number.',
                         code='def run(arguments):\n    return arguments["number"] * 2',
                         example_arguments={'number': 3}, expected_result=6)
        arguments.update(changes)
        return self.manager.create_tool(**arguments)

    def invoke(self, arguments):
        tool = self.manager.load_tools()[0]
        return tool.execute({'arguments': arguments}, self.manager.approve)

    def test_disabled_tool_remains_visible_and_reloads_when_enabled(self):
        self.create()
        self.assertTrue(self.manager.summaries()[0].get('enabled'), 'Existing tools need an enabled default')
        self.manager.set_enabled('double_number', False)
        self.assertEqual(self.manager.load_tools(), [])
        fresh = ProgrammingTools(self.root, self.workspace)
        self.assertFalse(fresh.summaries()[0]['enabled'])
        fresh.set_enabled('double_number', True)
        self.assertEqual(len(fresh.load_tools()), 1)

    def test_multiple_tests_gate_replacement_and_keep_previous_version(self):
        self.create()
        path = self.root / 'local_tools/double_number.json'
        before = path.read_bytes()
        self.assertTrue(hasattr(self.manager, 'test_saved'), 'Saved tools need a test action')
        with self.assertRaises(ValueError):
            self.create(replace_existing=True, tests=[{'arguments': {'number': 0}, 'expected_result': 1}])
        self.assertEqual(path.read_bytes(), before)
        self.create(replace_existing=True, tests=[{'arguments': {'number': 0}, 'expected_result': 0}])
        summary = self.manager.summaries()[0]
        self.assertEqual(summary['test_count'], 2)
        self.assertEqual(summary['previous_version'], 1)
        self.assertEqual(self.manager.test_saved('double_number'),
                         {'passed': True, 'total': 2, 'version': 2, 'results': [
                             {'index': 1, 'passed': True}, {'index': 2, 'passed': True}]})

    def test_rollback_restores_code_and_can_restore_newer_version_again(self):
        self.create()
        self.create(code='def run(arguments):\n    return arguments["number"] * 3',
                    expected_result=9, replace_existing=True)
        self.assertTrue(hasattr(self.manager, 'rollback'), 'Tool versions must be recoverable')
        self.assertEqual(self.invoke({'number': 4})['result'], 12)
        self.manager.rollback('double_number')
        self.assertEqual(self.invoke({'number': 4})['result'], 8)
        self.assertEqual(self.manager.summaries()[0]['previous_version'], 2)
        self.manager.rollback('double_number')
        self.assertEqual(self.invoke({'number': 4})['result'], 12)

    def test_rollback_and_inventory_never_execute_generated_code(self):
        code = 'from pathlib import Path\nPath("marker.txt").write_text("ran")\n' \
               'def run(arguments):\n    return arguments["number"] * 2'
        self.create(code=code)
        self.create(replace_existing=True)
        (self.workspace / 'marker.txt').unlink()
        self.assertTrue(hasattr(self.manager, 'rollback'))
        self.manager.rollback('double_number')
        self.manager.summaries()
        self.manager.load_tools()
        self.assertFalse((self.workspace / 'marker.txt').exists())

    def test_explicit_schema_is_advertised_and_enforced_before_python(self):
        schema = {'type': 'object', 'properties': {'number': {'type': 'integer', 'minimum': 0}},
                  'required': ['number'], 'additionalProperties': False}
        self.assertIn('input_schema', self.manager.builtin_tools()[1].definition()['parameters']['properties'],
                      'create_tool must accept an explicit schema')
        self.create(input_schema=schema)
        tool = self.manager.load_tools()[0]
        self.assertEqual(tool.definition()['parameters']['properties']['arguments'], schema)
        for arguments in ({}, {'number': -1}, {'number': True}, {'number': '3'}, {'number': 3, 'extra': 1}):
            result = self.invoke(arguments)
            self.assertFalse(result['ok'], result)
            self.assertEqual(result['error']['code'], 'invalid_arguments')
        self.assertEqual(self.invoke({'number': 4})['result'], 8)

    def test_nested_schema_and_unsupported_keywords(self):
        self.assertIn('input_schema', self.manager.builtin_tools()[1].definition()['parameters']['properties'])
        schema = {'type': 'object', 'properties': {'values': {'type': 'array', 'minItems': 1,
                  'items': {'type': 'string', 'enum': ['a', 'b']}}}, 'required': ['values']}
        self.create(code='def run(arguments):\n    return len(arguments["values"])',
                    example_arguments={'values': ['a']}, expected_result=1, input_schema=schema)
        self.assertFalse(self.invoke({'values': ['other']})['ok'])
        self.assertFalse(self.invoke({'values': []})['ok'])
        self.assertEqual(self.invoke({'values': ['a', 'b']})['result'], 2)
        with self.assertRaises(ValueError):
            self.create(name='unsupported', input_schema={'type': 'object', 'oneOf': []})

    def test_failed_retest_reports_no_private_output(self):
        self.create()
        self.assertTrue(hasattr(self.manager, 'test_saved'))
        path = self.root / 'local_tools/double_number.json'
        body = json.loads(path.read_text())
        body['code'] = 'def run(arguments):\n    raise ValueError("private-business-data")'
        path.write_text(json.dumps(body))
        result = self.manager.test_saved('double_number')
        self.assertFalse(result['passed'])
        self.assertNotIn('private-business-data', json.dumps(result))

    def test_legacy_manifest_defaults_and_unsafe_history_are_rejected(self):
        self.create()
        path = self.root / 'local_tools/double_number.json'
        data = json.loads(path.read_text())
        for key in ('enabled', 'input_schema', 'tests', 'previous_version'):
            data.pop(key, None)
        path.write_text(json.dumps(data))
        self.assertTrue(self.manager.summaries()[0].get('enabled'))
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'local_tools/.history').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.create(replace_existing=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_running_tests_do_not_block_tool_inventory(self):
        self.create()
        entered, release, listed = threading.Event(), threading.Event(), threading.Event()

        def execute(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'result': 6}

        with patch.object(self.manager, '_execute', side_effect=execute):
            runner = threading.Thread(target=self.manager.test_saved, args=('double_number',))
            runner.start()
            self.assertTrue(entered.wait(1))
            reader = threading.Thread(target=lambda: (self.manager.summaries(), listed.set()))
            reader.start()
            try:
                self.assertTrue(listed.wait(.3), 'Tool inventory is blocked by Python test execution')
            finally:
                release.set()
                runner.join(3)
                reader.join(3)


if __name__ == '__main__':
    unittest.main()
