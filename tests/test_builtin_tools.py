import tempfile
import unittest
from pathlib import Path

from miniagent.builtin_tools import build_tools


class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'hello.txt').write_text('你好，世界。abcdef', encoding='utf-8')
        self.tools = {tool.name: tool for tool in build_tools(self.root)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_pagination_preserves_unicode_characters(self):
        result = self.tools['read_file'].execute({'path': 'hello.txt', 'offset': 3, 'limit': 3})
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['text'], '世界。')
        self.assertEqual(result['result']['next_offset'], 6)

    def test_traversal_is_denied(self):
        (self.root.parent / 'secret.txt').write_text('secret')
        result = self.tools['read_file'].execute({'path': '../secret.txt'})
        self.assertFalse(result['ok'])
        self.assertNotIn('secret\"', str(result))

    def test_symlink_escape_is_denied(self):
        target = self.root.parent / 'outside.txt'
        target.write_text('outside')
        try:
            (self.root / 'link.txt').symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks not available on this platform')
        self.assertFalse(self.tools['read_file'].execute({'path': 'link.txt'})['ok'])

    def test_calculator_arithmetic(self):
        result = self.tools['calculate'].execute({'expression': '(200 + 100) * 0.9'})
        self.assertEqual(result, {'ok': True, 'result': 270.0})

    def test_calculator_rejects_code_and_unbounded_arithmetic(self):
        for expression in ['__import__("os").getcwd()', '2 ** 1000000', 'True + 1', '1 / 0']:
            with self.subTest(expression=expression):
                self.assertFalse(self.tools['calculate'].execute({'expression': expression})['ok'])

    def test_list_files_returns_relative_names(self):
        result = self.tools['list_files'].execute({})
        self.assertTrue(result['ok'])
        self.assertIn('hello.txt', [entry['path'] for entry in result['result']['entries']])


if __name__ == '__main__':
    unittest.main()
