"""HTTP and event integration for explicit checks, outputs and recovery."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from miniagent.web import create_server, RunPresentation


class WorkbenchWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = create_server(self.root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, data=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, path, json.dumps(data) if data is not None else None,
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        status, headers, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, headers, body

    def configured_session(self):
        self.request('POST', '/api/config', {'provider': 'openai', 'model': 'fixture', 'api_key': 'fixture-secret'})
        _, _, body = self.request('POST', '/api/sessions', {})
        sid = json.loads(body)['session']['id']
        return self.server.state.get_session(sid)

    def test_check_endpoint_does_not_hold_global_state_lock(self):
        entered, release = threading.Event(), threading.Event()
        def check(_):
            entered.set()
            release.wait(3)
            return {'ok': True}
        outcome = []
        with patch.object(self.server.state, 'check_connection', side_effect=check):
            request = threading.Thread(target=lambda: outcome.append(self.request(
                'POST', '/api/check-connection', {'target': 'model', 'config': {}})))
            request.start()
            self.assertTrue(entered.wait(1))
            try:
                self.assertEqual(self.request('GET', '/api/health')[0], 200)
            finally:
                release.set()
                request.join(3)
        self.assertEqual(outcome[0][0], 200)

    def test_registered_artifact_is_an_immutable_download_and_escaped_preview_data(self):
        self.assertTrue(hasattr(self.server.state, 'artifacts'), 'Artifact host integration missing')
        store = self.server.state.artifacts
        before = store.scan()
        (self.root / 'workspace' / 'result.py').write_text('print("<script>not HTML</script>")\n')
        artifact = store.capture(before, run_id='fixture-run')[0]
        session = self.configured_session()
        session['detail']['messages'].append({'role': 'assistant', 'content': 'done', 'artifacts': [artifact]})
        files = json.loads(self.request('GET', '/api/files')[2])['files']
        self.assertTrue(any(item['name'] == 'result.py' and item.get('artifact', {}).get('id') == artifact['id']
                            for item in files), 'Generated source files should be accessible from the file drawer')
        (self.root / 'workspace' / 'result.py').write_text('changed')
        status, _, body = self.request('GET', artifact['preview_url'])
        self.assertEqual(status, 200)
        self.assertIn('<script>not HTML</script>', json.loads(body)['content'])
        status, headers, body = self.request('GET', artifact['download_url'])
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertTrue(headers['Content-Type'].startswith('application/octet-stream'))
        self.assertEqual(body, b'print("<script>not HTML</script>")\n')
        self.assertEqual(self.request('GET', '/api/artifacts/../config.json')[0], 404)

    def test_search_and_fetch_sources_are_attached_to_their_answer(self):
        session = self.configured_session()
        run = {'id': 'fixture-run', 'status': 'running', 'step': 0, 'error': None, 'events': [],
               'event_count': 0, 'omitted_events': 0}
        session['detail']['run'] = run
        message = self.server.state.assistant_message(session, run)
        presentation = RunPresentation(self.server.state, session, run, message)
        for name, result in [
            ('web_search', {'results': [{'source_id': 'S1234567890', 'url': 'https://example.com/a',
                                        'title': 'Source A', 'date': '2026-10-25'}]}),
            ('fetch_webpage', {'source_id': 'S1234567890', 'url': 'https://example.com/a',
                              'title': 'Source A', 'content': 'body'})]:
            presentation.on_event({'event': 'tool_result', 'step': 1,
                                   'data': {'id': name, 'name': name, 'ok': True, 'result': result}})
        self.assertEqual(len(message['sources']), 1, 'Search output was not integrated into message sources')
        self.assertTrue(message['sources'][0]['read'])
        self.assertEqual(message['sources'][0]['date'], '2026-10-25')
        self.assertEqual(message['sources'][0]['id'], 'S1234567890')


if __name__ == '__main__':
    unittest.main()
