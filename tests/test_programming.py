import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from miniagent.tools import Tool, ToolArgs


class ProgrammingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('miniagent.programming'),
                             'The programmable tool module must exist.')
        self.module = importlib.import_module('miniagent.programming')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.manager = self.module.ProgrammingTools(self.root, self.workspace)

    def execute(self, tool_name, manager=None, **arguments):
        manager = manager or self.manager
        tool = next(t for t in manager.builtin_tools() + manager.load_tools() if t.name == tool_name)
        return tool.execute(arguments, manager.approve)

    def create(self, **changes):
        arguments = {'name': 'double_number', 'description': 'Double a number.',
                     'code': 'def run(arguments):\n    return arguments["number"] * 2\n',
                     'example_arguments': {'number': 3}, 'expected_result': 6}
        arguments.update(changes)
        return self.execute('create_tool', **arguments)

    def test_script_runs_in_real_subprocess_with_workspace(self):
        (self.workspace / 'input.txt').write_text('hello', encoding='utf-8')
        reply = self.execute('run_python', code='from pathlib import Path\nimport os\n'
                             'print(Path("input.txt").read_text())\n'
                             'result = {"pid": os.getpid(), "value": 2 + 3}')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['stdout'], 'hello\n')
        self.assertEqual(reply['result']['result']['value'], 5)
        self.assertNotEqual(reply['result']['result']['pid'], os.getpid())

    def test_normal_main_guard_runs_for_scripts(self):
        reply = self.execute('run_python', code='if __name__ == "__main__":\n    result = 42')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['result'], 42)

    def test_script_argparse_does_not_receive_transport_arguments(self):
        reply = self.execute('run_python', code='import argparse, sys\n'
                             'argparse.ArgumentParser().parse_args()\n'
                             'result = {"value": 42, "argv": sys.argv}')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['result'], {'value': 42, 'argv': ['<generated-python>']})

    def test_subprocess_stdout_is_output_not_control_protocol(self):
        code = 'import subprocess, sys\n' \
               'subprocess.run([sys.executable, "-c", "print(123)"], check=True)\n' \
               'result = 42'
        reply = self.execute('run_python', code=code)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['stdout'], '123\n')
        self.assertEqual(reply['result']['result'], 42)

    def test_json_printed_by_subprocess_cannot_fake_a_passing_test(self):
        code = 'import subprocess, sys, time\ndef run(arguments):\n' \
               '    subprocess.run([sys.executable, "-c", ' \
               '"import json; print(json.dumps({\'ok\': True, \'result\': 6}))"], check=True)\n' \
               '    time.sleep(0.2)\n    return 7\n'
        reply = self.create(code=code, expected_result=6)
        self.assertFalse(reply['ok'], reply)
        self.assertIn('Actual result: 7', reply['error']['message'])
        self.assertFalse((self.root / 'local_tools' / 'double_number.json').exists())

    def test_worker_protocol_is_utf8_under_ascii_stdio(self):
        request = {'code': 'print("你好，世界")\nresult = "中文结果"', 'mode': 'script', 'arguments': {}}
        worker = Path(self.module.__file__).with_name('python_worker.py')
        result_file = self.root / 'worker-result.json'
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        process = subprocess.Popen([sys.executable, '-I', '-X', 'utf8=0', str(worker), str(result_file)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=self.workspace, env={'LC_ALL': 'C', 'PATH': os.environ.get('PATH', '')},
                                   **options)
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False).encode('utf-8') + b'\n')
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while not result_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(result_file.exists())
        finally:
            process.stdin.close()
            process.stdin = None
            try:
                output, error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, error = process.communicate()
        parsed = json.loads(result_file.read_text(encoding='utf-8'))
        self.assertTrue(parsed['ok'], parsed)
        self.assertEqual(output.decode('utf-8'), '你好，世界\n')
        self.assertEqual(parsed['result'], '中文结果')

    def test_script_and_child_stdin_are_empty_not_lifetime_pipe(self):
        manager = self.module.ProgrammingTools(self.root, self.workspace, timeout_seconds=0.5)
        reply = self.execute('run_python', manager=manager, code='import os, subprocess, sys\n'
                             'child = subprocess.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], '
                             'capture_output=True, text=True, check=True)\n'
                             'result = {"input": sys.stdin.read(), "raw": os.read(0, 1).decode(), "child": child.stdout}')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['result'], {'input': '', 'raw': '', 'child': "''\n"})

    def test_syntax_error_is_recoverable(self):
        reply = self.execute('run_python', code='def broken(:')
        self.assertFalse(reply['ok'])
        self.assertIn('SyntaxError', reply['error']['message'])

    def test_failed_example_does_not_save_or_register(self):
        reply = self.create(expected_result=7)
        self.assertFalse(reply['ok'])
        self.assertFalse((self.root / 'local_tools' / 'double_number.json').exists())
        self.assertEqual(self.manager.load_tools(), [])

    def test_passed_example_persists_and_reload_is_callable(self):
        reply = self.create()
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['version'], 1)
        fresh = self.module.ProgrammingTools(self.root, self.workspace)
        result = self.execute('double_number', manager=fresh, arguments={'number': 21})
        self.assertEqual(result, {'ok': True, 'result': 42})
        summary = fresh.summaries()[0]
        self.assertEqual(summary['name'], 'double_number')
        self.assertIn('def run(arguments)', summary['code'])
        self.assertEqual(summary['example_arguments'], {'number': 3})

    def test_loading_manifest_never_executes_generated_code(self):
        code = 'from pathlib import Path\nPath("marker.txt").write_text("executed")\n' \
               'def run(arguments):\n    return 6\n'
        self.assertTrue(self.create(code=code)['ok'])
        (self.workspace / 'marker.txt').unlink()
        fresh = self.module.ProgrammingTools(self.root, self.workspace)
        self.assertEqual(len(fresh.load_tools()), 1)
        self.assertFalse((self.workspace / 'marker.txt').exists())

    def test_replacement_is_explicit_and_versioned(self):
        self.assertTrue(self.create()['ok'])
        self.assertFalse(self.create()['ok'])
        replaced = self.create(code='def run(arguments):\n    return arguments["number"] * 3',
                               expected_result=9, replace_existing=True)
        self.assertTrue(replaced['ok'], replaced)
        self.assertEqual(replaced['result']['version'], 2)
        self.assertEqual(self.execute('double_number', arguments={'number': 4})['result'], 12)

    def test_names_cannot_collide_or_escape(self):
        for name in ['list_files', 'read_file', 'run_python', 'create_tool', 'web_search',
                     'fetch_webpage', '../escape', 'a/b', 'CON', 'nul', 'name.json']:
            with self.subTest(name=name):
                self.assertFalse(self.create(name=name)['ok'])
        self.assertTrue(self.create()['ok'])
        self.assertFalse(self.create(name='DOUBLE_NUMBER')['ok'])

    def test_invalid_manifest_is_reported_without_breaking_valid_tools(self):
        self.assertTrue(self.create()['ok'])
        (self.root / 'local_tools' / 'broken.json').write_text('{"code":"private-value"}', encoding='utf-8')
        fresh = self.module.ProgrammingTools(self.root, self.workspace)
        self.assertEqual([t.name for t in fresh.load_tools()], ['double_number'])
        errors = fresh.errors()
        self.assertTrue(any('broken.json' in json.dumps(e) for e in errors))
        self.assertNotIn('private-value', json.dumps(errors))

    def test_non_finite_manifest_is_reported_without_breaking_valid_tools(self):
        self.assertTrue(self.create()['ok'])
        content = json.loads((self.root / 'local_tools' / 'double_number.json').read_text())
        content.update(name='invalid_number', example_arguments={'number': float('nan')})
        (self.root / 'local_tools' / 'invalid_number.json').write_text(json.dumps(content), encoding='utf-8')
        self.assertEqual([t.name for t in self.manager.load_tools()], ['double_number'])
        self.assertTrue(self.manager.errors())

    def test_symlink_manifest_is_not_loaded(self):
        self.assertTrue(self.create()['ok'])
        target = self.root / 'local_tools' / 'double_number.json'
        try:
            (self.root / 'local_tools' / 'linked.json').symlink_to(target)
        except OSError:
            self.skipTest('Symlinks are unavailable on this platform.')
        self.assertEqual([t.name for t in self.manager.load_tools()], ['double_number'])
        self.assertTrue(self.manager.errors())

    def test_directory_swap_cannot_redirect_tool_storage(self):
        original = self.root / 'local_tools'
        original.rename(self.root / 'original_local_tools')
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            original.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks are unavailable on this platform.')
        self.assertFalse(self.create()['ok'])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.manager.load_tools(), [])
        self.assertTrue(self.manager.errors())

    def test_timeout_terminates_child(self):
        manager = self.module.ProgrammingTools(self.root, self.workspace, timeout_seconds=0.2)
        start = time.monotonic()
        result = self.execute('run_python', manager=manager, code='import time\ntime.sleep(10)')
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['error']['message'].lower())
        self.assertLess(time.monotonic() - start, 3)

    def test_background_child_does_not_hold_successful_run_open(self):
        manager = self.module.ProgrammingTools(self.root, self.workspace, timeout_seconds=0.5)
        result = self.execute('run_python', manager=manager,
                              code='import subprocess, sys\n'
                              'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])\n'
                              'result = child.pid')
        self.assertTrue(result['ok'], result)
        pid = result['result']['result']
        if sys.platform.startswith('linux'):
            deadline = time.monotonic() + 1
            state = ''
            while time.monotonic() < deadline:
                try:
                    state = Path(f'/proc/{pid}/stat').read_text().split()[2]
                except FileNotFoundError:
                    break
                if state == 'Z':
                    break
                time.sleep(0.02)
            else:
                self.fail(f'Child process {pid} is still running with state {state}.')

    def test_stop_terminates_child(self):
        start = time.monotonic()
        with patch('miniagent.programming.execution_stopped', side_effect=lambda: time.monotonic() - start > 0.2):
            result = self.execute('run_python', code='import time\ntime.sleep(10)')
        self.assertFalse(result['ok'])
        self.assertIn('cancel', result['error']['message'].lower())
        self.assertLess(time.monotonic() - start, 3)

    @unittest.skipUnless(os.name == 'posix', 'Host interruption test uses POSIX signals.')
    def test_host_exit_terminates_python_from_daemon_thread(self):
        pid_file = self.workspace / 'host-exit-worker.pid'
        generated = 'import os, time\nfrom pathlib import Path\n' \
                    'Path("host-exit-worker.pid").write_text(str(os.getpid()))\ntime.sleep(30)'
        parent_code = 'import threading, time\nfrom pathlib import Path\n' \
                      'from miniagent.programming import ProgrammingTools\n' \
                      f'manager = ProgrammingTools(Path({str(self.root)!r}), Path({str(self.workspace)!r}), timeout_seconds=1)\n' \
                      f'threading.Thread(target=manager.run_python, args=({generated!r},), daemon=True).start()\n' \
                      'time.sleep(30)\n'
        parent = subprocess.Popen([sys.executable, '-c', parent_code],
                                  cwd=Path(self.module.__file__).parent.parent,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  start_new_session=True)
        worker_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), 'The generated worker did not start.')
            worker_pid = int(pid_file.read_text())
            parent.send_signal(signal.SIGINT)
            parent.communicate(timeout=3)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(worker_pid, 0)
                    if sys.platform.startswith('linux'):
                        if Path(f'/proc/{worker_pid}/stat').read_text().split()[2] == 'Z':
                            break
                except (ProcessLookupError, FileNotFoundError):
                    break
                time.sleep(0.02)
            else:
                self.fail('Generated Python survived its host process exit.')
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.communicate(timeout=3)
            if worker_pid is not None:
                try:
                    os.killpg(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_combined_output_is_bounded(self):
        result = self.execute('run_python', code='import sys\nprint("x" * 40000)\nprint("y" * 40000, file=sys.stderr)')
        self.assertFalse(result['ok'])
        self.assertIn('output', result['error']['message'].lower())
        self.assertLess(len(json.dumps(result)), 2000)

    def test_result_size_is_bounded(self):
        result = self.execute('run_python', code='result = "x" * 1000000')
        self.assertFalse(result['ok'])
        self.assertIn('output', result['error']['message'].lower())

    def test_raw_pipe_output_is_bounded(self):
        result = self.execute('run_python', code='import os\nos.write(1, b"x" * 1000000)')
        self.assertFalse(result['ok'])
        self.assertIn('output', result['error']['message'].lower())
        self.assertLess(len(json.dumps(result)), 2000)

    def test_child_does_not_inherit_api_key_environment(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sensitive-openai',
                                     'DEEPSEEK_API_KEY': 'sensitive-deepseek'}):
            result = self.execute('run_python', code='import os\nresult = [os.getenv("OPENAI_API_KEY"), os.getenv("DEEPSEEK_API_KEY")]')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['result'], [None, None])

    def test_approval_is_for_created_tool_identity_only(self):
        tool = self.manager.builtin_tools()[0]
        self.assertFalse(tool.read_only)
        self.assertFalse(tool.execute({'code': 'result = 1'})['ok'])
        clone = Tool(tool.name, tool.description, tool.args_schema, tool.handler)
        self.assertTrue(self.manager.approve(tool, {}))
        self.assertFalse(self.manager.approve(clone, {}))

    def test_test_result_compares_json_types(self):
        self.assertFalse(self.create(code='def run(arguments):\n    return 1', expected_result=True)['ok'])

    def test_non_json_result_does_not_save(self):
        self.assertFalse(self.create(code='def run(arguments):\n    return {1, 2, 3}')['ok'])
        self.assertEqual(self.manager.load_tools(), [])

    def test_concurrent_create_does_not_overwrite(self):
        other = self.module.ProgrammingTools(self.root, self.workspace)
        code = 'import time\ndef run(arguments):\n    time.sleep(0.1)\n    return 6'
        outcomes = []
        def create(manager):
            outcomes.append(self.execute('create_tool', manager=manager, name='concurrent',
                                        description='Concurrent test.', code=code,
                                        example_arguments={}, expected_result=6))
        threads = [threading.Thread(target=create, args=(manager,)) for manager in (self.manager, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(item['ok'] for item in outcomes), 1)
        self.assertEqual(json.loads((self.root / 'local_tools' / 'concurrent.json').read_text())['version'], 1)


if __name__ == '__main__':
    unittest.main()
