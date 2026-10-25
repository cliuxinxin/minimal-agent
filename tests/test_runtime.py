"""Runtime identity detects code updates without inspecting user data."""
import importlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


class RuntimeIdentityTests(unittest.TestCase):
    def identity(self, root):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.runtime'),
                             'Runtime identity is required to reject a stale server')
        return importlib.import_module('miniagent.runtime').runtime_identity(root)

    def test_code_changes_invalidate_reuse_but_user_data_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'miniagent'
            package.mkdir()
            code = package / 'model.py'
            code.write_text('MODEL_FORMAT = 1\n', encoding='utf-8')
            original = self.identity(root)
            (root / 'config.json').write_text('{"api_key":"local-private-key"}', encoding='utf-8')
            (root / 'workspace').mkdir()
            (root / 'workspace' / 'user.py').write_text('private customer code', encoding='utf-8')
            (root / 'local_tools').mkdir()
            (root / 'local_tools' / 'tool.json').write_text('private generated tool', encoding='utf-8')
            self.assertEqual(self.identity(root), original)
            code.write_text('MODEL_FORMAT = 2\n', encoding='utf-8')
            changed = self.identity(root)
            self.assertNotEqual(changed['source_revision'], original['source_revision'])
            self.assertEqual(changed['workspace_id'], original['workspace_id'])
            self.assertRegex(changed['source_revision'], r'^[0-9a-f]{64}$')
            self.assertNotIn('private', repr(changed))

    def test_new_or_removed_module_and_launcher_updates_invalidate_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'miniagent').mkdir()
            original = self.identity(root)
            module = root / 'miniagent' / 'streaming.py'
            module.write_text('STREAM_FORMAT = 2\n', encoding='utf-8')
            self.assertNotEqual(self.identity(root)['source_revision'], original['source_revision'])
            module.unlink()
            self.assertEqual(self.identity(root)['source_revision'], original['source_revision'])
            (root / 'launcher.py').write_text('LAUNCH_FORMAT = 2\n', encoding='utf-8')
            self.assertNotEqual(self.identity(root)['source_revision'], original['source_revision'])

    def test_workspace_identity_uses_resolved_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'subfolder').mkdir()
            self.assertEqual(self.identity(root), self.identity(root / 'subfolder' / '..'))
            self.assertNotEqual(self.identity(root)['workspace_id'],
                                self.identity(root / 'subfolder')['workspace_id'])


if __name__ == '__main__':
    unittest.main()
