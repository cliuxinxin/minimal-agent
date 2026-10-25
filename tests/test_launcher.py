"""Double-click startup must not reuse a stale Python process after an upgrade."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import launcher
from miniagent import __version__


class HealthResponse:
    """Replace only the local HTTP boundary; all launcher decisions stay real."""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode()
        self.requests = []

    def open(self, url, *, timeout):
        self.requests.append((url, timeout))
        return io.BytesIO(self.body)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(launcher, 'ROOT', self.root).start()
        self.browser = patch.object(launcher.webbrowser, 'open').start()
        self.output = io.StringIO()
        patch('sys.stdout', self.output).start()

    def serve_health(self, health=None, **changes):
        health = dict(health) if health is not None else {
            'app': 'minimal-agent-web',
            'version': '0.4.0',
            'workspace_id': hashlib.sha256(str(self.root).encode()).hexdigest()[:12],
        }
        health.update(changes)
        response = HealthResponse(health)
        patch.object(launcher, 'build_opener', return_value=response).start()
        return response

    def current_health(self):
        from miniagent.runtime import runtime_identity
        return runtime_identity(self.root)

    def test_old_same_workspace_process_is_not_reopened(self):
        self.serve_health()

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()
        self.assertIn('更新', self.output.getvalue())

    def test_same_version_with_different_source_is_not_reopened(self):
        self.serve_health(version=__version__, source_revision='previous-source-revision')

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_old_version_with_current_source_revision_is_not_reopened(self):
        self.serve_health(self.current_health(), version='0.4.0')

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_same_version_without_source_revision_is_not_reopened(self):
        self.serve_health(version=__version__)

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_legacy_server_without_version_is_not_reopened(self):
        health = self.current_health()
        health.pop('version')
        self.serve_health(health)

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_matching_version_and_source_reopens_without_spawning_a_process(self):
        response = self.serve_health(self.current_health())
        with patch.object(launcher.subprocess, 'call') as launch:
            result = launcher.main()

        self.assertEqual(result, 0)
        self.browser.assert_called_once_with(launcher.URL)
        launch.assert_not_called()
        self.assertEqual(response.requests, [(launcher.URL + '/api/health', 0.5)])

    def test_other_app_or_workspace_is_never_reopened(self):
        for changes in ({'app': 'another-app'}, {'workspace_id': 'another-workspace'}):
            with self.subTest(changes=changes):
                self.serve_health(self.current_health(), **changes)

                self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()
        self.assertEqual(self.output.getvalue(), '')

    def test_health_larger_than_probe_limit_is_not_reopened(self):
        self.serve_health({'padding': 'x' * 4096, **self.current_health()})

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_malformed_health_is_not_reopened(self):
        response = self.serve_health()
        response.body = b'{not valid JSON'

        self.assertFalse(launcher.reopen_running_app())

        self.browser.assert_not_called()

    def test_start_launches_fresh_process_once_when_existing_server_is_stale(self):
        response = self.serve_health()
        python = self.root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        python.parent.mkdir(parents=True)
        python.touch()
        with patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(launcher.subprocess, 'call', return_value=23) as launch:
            result = launcher.main()

        self.assertEqual(result, 23)
        launch.assert_called_once_with([str(python), '-m', 'miniagent.web'], cwd=self.root)
        self.assertEqual(response.requests, [(launcher.URL + '/api/health', 0.5)])
        self.browser.assert_not_called()

    def test_registered_free_port_is_reused_after_default_port_was_occupied(self):
        directory = self.root / 'data'
        directory.mkdir()
        (directory / 'server.json').write_text(json.dumps({'port': 39127}))
        response = self.serve_health(self.current_health())
        self.assertTrue(launcher.reopen_running_app())
        self.browser.assert_called_once_with('http://127.0.0.1:39127')
        self.assertEqual(response.requests, [('http://127.0.0.1:39127/api/health', .5)])

    def test_untrusted_registry_cannot_redirect_probe_to_an_external_host(self):
        directory = self.root / 'data'
        directory.mkdir()
        (directory / 'server.json').write_text(json.dumps({'port': 'https://external.example'}))
        response = self.serve_health(self.current_health())
        self.assertTrue(launcher.reopen_running_app())
        self.assertEqual(response.requests, [(launcher.URL + '/api/health', .5)])


if __name__ == '__main__':
    unittest.main()
