"""Artifact snapshots preserve immutable, bounded workspace-only results."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.artifacts'), 'Artifact registry must exist')
        from miniagent.artifacts import ArtifactStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = ArtifactStore(self.root, self.workspace)

    def test_created_script_can_be_previewed_and_downloaded_after_restart(self):
        before = self.store.scan()
        (self.workspace / 'summary.py').write_text('print("hello")\n', encoding='utf-8')
        items = self.store.capture(before, run_id='run-1')
        self.assertEqual(len(items), 1)
        artifact = items[0]
        self.assertEqual(artifact['name'], 'summary.py')
        self.assertEqual(artifact['change'], 'created')
        from miniagent.artifacts import ArtifactStore
        restarted = ArtifactStore(self.root, self.workspace)
        metadata, raw = restarted.get(artifact['id'])
        self.assertEqual(raw, b'print("hello")\n')
        self.assertEqual(metadata, artifact)
        self.assertEqual(restarted.preview(artifact['id'])['content'], 'print("hello")\n')

    def test_modified_file_has_diff_and_old_download_stays_immutable(self):
        before = self.store.scan()
        path = self.workspace / 'report.md'
        path.write_text('# Before\n', encoding='utf-8')
        first = self.store.capture(before, run_id='one')[0]
        before = self.store.scan()
        path.write_text('# After\n', encoding='utf-8')
        second = self.store.capture(before, run_id='two')[0]
        self.assertEqual(second['change'], 'modified')
        self.assertEqual(self.store.get(first['id'])[1], b'# Before\n')
        preview = self.store.preview(second['id'])
        self.assertIn('-# Before', preview['diff'])
        self.assertIn('+# After', preview['diff'])

    def test_csv_preview_uses_quoted_fields_and_limits_rows(self):
        (self.workspace / 'table.csv').write_text('name,value\n"a,b",3\n' + 'x,2\n' * 40)
        artifact = self.store.capture({}, run_id='csv')[0]
        preview = self.store.preview(artifact['id'])
        self.assertEqual(preview['columns'], ['name', 'value'])
        self.assertEqual(preview['rows'][0], ['a,b', '3'])
        self.assertEqual(len(preview['rows']), 30)

    def test_hidden_symlink_binary_and_large_files_are_not_captured(self):
        (self.root / 'private.txt').write_text('private')
        (self.workspace / 'linked.txt').symlink_to(self.root / 'private.txt')
        (self.workspace / '.private.txt').write_text('private')
        (self.workspace / 'large.py').write_text('x' * (2 * 1024 * 1024 + 1))
        (self.workspace / 'binary.txt').write_bytes(b'\xff\x00')
        self.assertEqual(self.store.capture({}, run_id='bad'), [])

    def test_nested_files_and_html_are_text_only(self):
        folder = self.workspace / 'output'
        folder.mkdir()
        (folder / 'page.html').write_text('<script>alert(1)</script>')
        artifact = self.store.capture({}, run_id='html')[0]
        self.assertEqual(artifact['name'], 'output/page.html')
        preview = self.store.preview(artifact['id'])
        self.assertEqual(preview['content'], '<script>alert(1)</script>')
        self.assertNotIn('html', {key for key in preview if key != 'content'})

    def test_ids_and_storage_symlinks_cannot_read_local_files(self):
        for value in ('../config', '/etc/passwd', '', 'x' * 32, '0' * 31, '0' * 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.get(value)
        (self.workspace / 'ok.txt').write_text('ok')
        item = self.store.capture({}, run_id='a')[0]
        directory = self.root / 'data/artifacts'
        files = list(directory.rglob('*'))
        binary = next(p for p in files if p.is_file() and p.suffix != '.json')
        binary.unlink()
        binary.symlink_to(self.root / 'private.txt')
        (self.root / 'private.txt').write_text('private')
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.store.get(item['id'])

    def test_unchanged_files_are_not_registered_and_capture_is_bounded(self):
        for number in range(60):
            (self.workspace / f'{number}.txt').write_text(str(number))
        self.assertEqual(self.store.capture(self.store.scan(), run_id='same'), [])
        self.assertEqual(len(self.store.capture({}, run_id='bounded')), 50)

    def test_symlink_workspace_is_rejected_before_resolving_it(self):
        from miniagent.artifacts import ArtifactStore
        linked = self.root / 'linked-workspace'
        linked.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            ArtifactStore(self.root, linked)

    def test_empty_directories_also_count_toward_scan_budget(self):
        for index in range(1001):
            (self.workspace / f'{index:04d}').mkdir()
        (self.workspace / '1000/end.txt').write_text('beyond traversal budget')
        self.assertEqual(self.store.scan(), {})

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory mode assertion')
    def test_artifact_storage_is_private_by_default(self):
        self.assertEqual((self.root / 'data').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'data/artifacts').stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
