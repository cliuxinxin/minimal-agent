"""Run Python in child processes and persist tested, reusable local tools."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal
import weakref

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .tools import Tool, ToolArgs, execution_stopped


OUTPUT_LIMIT = 64 * 1024
MANIFEST_LIMIT = 256 * 1024
RESERVED = {'list_files', 'read_file', 'run_python', 'create_tool', 'web_search', 'fetch_webpage'}
WINDOWS_DEVICES = {'con', 'prn', 'aux', 'nul'} | {
    f'{prefix}{number}' for prefix in ('com', 'lpt') for number in range(1, 10)}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class RunPythonArgs(ToolArgs):
    code: str = Field(min_length=1, max_length=50000)


class CreateToolArgs(RunPythonArgs):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    example_arguments: dict[str, JsonValue]
    expected_result: JsonValue
    replace_existing: bool = False


class SavedToolArgs(ToolArgs):
    arguments: dict[str, JsonValue]


class _Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1, max_length=50000)
    example_arguments: dict[str, JsonValue]
    expected_result: JsonValue
    updated_at: str
    version: int = Field(ge=1)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _validate_name(name):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name):
        raise ValueError('Tool name must be a short Python-style identifier.')
    if name.casefold() in RESERVED | WINDOWS_DEVICES:
        raise ValueError('This tool name is reserved.')


def _validate_code(code, *, tool=False):
    try:
        tree = ast.parse(code, filename='<generated-python>')
    except SyntaxError as error:
        raise ValueError(f'SyntaxError at line {error.lineno}: {error.msg}') from None
    if tool and not any(isinstance(node, ast.FunctionDef) and node.name == 'run' for node in tree.body):
        raise ValueError('Saved tools must define def run(arguments).')


def _terminate(process):
    if os.name == 'nt':
        if process.poll() is None:
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


class ProgrammingTools:
    def __init__(self, root: Path, workspace: Path, *, timeout_seconds=60):
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError('Python timeout must be positive.')
        self.root = Path(root).resolve()
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError('Workspace must be an existing directory.')
        self.directory = self.root / 'local_tools'
        if self.directory.is_symlink():
            raise ValueError('The local_tools directory must not be a symlink.')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.directory), threading.RLock())
        self._approved = weakref.WeakValueDictionary()
        self._cache = {}
        self._errors = []
        self._builtins = [
            self._allow(Tool('run_python',
                'Execute Python in a separate local process with workspace as the current directory. '
                'Use print() for text, or assign JSON-compatible data to result. '
                'Installed packages and standard Python are available. This runs with local user permissions; '
                'it is not an OS sandbox. Each execution has a 60-second timeout and 64-KiB output limit.',
                RunPythonArgs, self.run_python)),
            self._allow(Tool('create_tool',
                'Create a reusable local tool by writing Python with def run(arguments), where arguments is a dict. '
                'Provide example_arguments and expected_result; the test must pass before the tool is saved. '
                'The saved tool is available on the next model request and after restart. '
                'Set replace_existing=true only to deliberately update a tool. Tests execute real Python '
                'with local user permissions and may have side effects.',
                CreateToolArgs, self.create_tool)),
        ]

    def _allow(self, tool):
        self._approved[id(tool)] = tool
        return tool

    def approve(self, tool, arguments):
        return self._approved.get(id(tool)) is tool

    def builtin_tools(self):
        return list(self._builtins)

    def _execute(self, code, *, arguments=None, mode='script'):
        if execution_stopped():
            raise RuntimeError('Python execution cancelled.')
        _validate_code(code, tool=mode == 'tool')
        # Do not inherit provider credentials or Python import settings.
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
                   'LANG', 'LC_ALL', 'LC_CTYPE', 'TMP', 'TEMP', 'TMPDIR'}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        worker = Path(__file__).with_name('python_worker.py')
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        result_directory = tempfile.TemporaryDirectory(prefix='minimal-agent-python-')
        result_path = Path(result_directory.name) / 'result.json'
        try:
            process = subprocess.Popen([sys.executable, '-I', '-u', str(worker), str(result_path)],
                                       cwd=self.workspace, env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        except BaseException:
            result_directory.cleanup()
            raise
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        output_lock, exceeded = threading.Lock(), threading.Event()
        total = [0]

        def read_output(stream, label):
            try:
                while chunk := os.read(stream.fileno(), 8192):
                    with output_lock:
                        remaining = max(0, OUTPUT_LIMIT - total[0])
                        buffers[label].extend(chunk[:remaining])
                        total[0] += len(chunk)
                        if total[0] > OUTPUT_LIMIT:
                            exceeded.set()
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=read_output, args=(stream, label), daemon=True)
                   for stream, label in ((process.stdout, 'stdout'), (process.stderr, 'stderr'))]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            request = _json({'code': code, 'mode': mode, 'arguments': arguments or {}}).encode('utf-8') + b'\n'
            try:
                process.stdin.write(request)
                process.stdin.flush()
            except BrokenPipeError:
                pass
            while True:
                if execution_stopped():
                    raise RuntimeError('Python execution cancelled.')
                if exceeded.is_set():
                    raise RuntimeError('Python output exceeded 64 KiB.')
                completed = result_path.is_file()
                if completed:
                    _terminate(process)
                    for reader in readers:
                        reader.join(timeout=1)
                    break
                if process.poll() is not None:
                    _terminate(process)
                    for reader in readers:
                        reader.join(timeout=1)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Python execution timed out after {self.timeout_seconds:g} seconds.')
                time.sleep(0.02)
            if execution_stopped():
                raise RuntimeError('Python execution cancelled.')
            if exceeded.is_set():
                raise RuntimeError('Python output exceeded 64 KiB.')
            if not completed and process.returncode not in (None, 0):
                raise RuntimeError(f'Python process exited with status {process.returncode}.')
            try:
                with result_path.open('rb') as handle:
                    raw_response = handle.read(OUTPUT_LIMIT + 1)
            except OSError:
                raise RuntimeError('Python process returned an invalid result.') from None
            if total[0] + len(raw_response) > OUTPUT_LIMIT:
                raise RuntimeError('Python output exceeded 64 KiB.')
            try:
                response = json.loads(raw_response)
            except (UnicodeDecodeError, ValueError):
                raise RuntimeError('Python process returned an invalid result.') from None
            if not isinstance(response, dict) or type(response.get('ok')) is not bool:
                raise RuntimeError('Python process returned an invalid result.')
            if not response['ok']:
                raise RuntimeError(str(response.get('error', 'Python execution failed.'))[:2000])
            return {'result': response.get('result'),
                    'stdout': buffers['stdout'].decode('utf-8', errors='replace'),
                    'stderr': buffers['stderr'].decode('utf-8', errors='replace')}
        finally:
            _terminate(process)
            for reader in readers:
                reader.join(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
            result_directory.cleanup()

    def run_python(self, code):
        return self._execute(code)

    def _check_directory(self):
        if self.directory.is_symlink() or self.directory.resolve() != self.directory or not self.directory.is_dir():
            raise ValueError('The local_tools directory must remain a regular directory inside this project.')

    def _target(self, name):
        self._check_directory()
        _validate_name(name)
        target = self.directory / f'{name}.json'
        if target.is_symlink():
            raise ValueError('Tool manifests must not be symlinks.')
        return target

    def _read_bytes(self, path):
        with path.open('rb') as handle:
            content = handle.read(MANIFEST_LIMIT + 1)
        if len(content) > MANIFEST_LIMIT:
            raise ValueError('Tool manifest is too large.')
        return content

    def _manifest(self, path):
        if path.is_symlink():
            raise ValueError('Tool manifests must not be symlinks.')
        content = self._read_bytes(path)
        manifest = _Manifest.model_validate_json(content)
        _validate_name(manifest.name)
        if manifest.name != path.stem:
            raise ValueError('Tool name must match its filename.')
        _validate_code(manifest.code, tool=True)
        _json(manifest.example_arguments)
        _json(manifest.expected_result)
        return manifest, hashlib.sha256(content).hexdigest()

    def _manifests(self):
        manifests, errors, seen = [], [], set()
        try:
            self._check_directory()
        except (OSError, ValueError):
            self._errors = [{'file': 'local_tools', 'error': 'Invalid local tools directory; restore the project directory.'}]
            return []
        for path in sorted(self.directory.glob('*.json')):
            try:
                manifest, digest = self._manifest(path)
                if manifest.name.casefold() in seen:
                    raise ValueError('Tool names must be unique ignoring case.')
                seen.add(manifest.name.casefold())
                manifests.append((manifest, digest))
            except (OSError, ValueError, TypeError):
                errors.append({'file': path.name, 'error': 'Invalid local tool; correct or replace its manifest.'})
        self._errors = errors
        return manifests

    def load_tools(self):
        with self.lock:
            cache, tools = {}, []
            for manifest, digest in self._manifests():
                existing = self._cache.get(manifest.name)
                if existing and existing[0] == digest:
                    tool = existing[1]
                else:
                    def invoke(arguments, code=manifest.code):
                        return self._execute(code, arguments=arguments, mode='tool')['result']
                    example = _json(manifest.example_arguments)
                    description = manifest.description + '\nCall with an arguments object. Example: ' + example[:1200]
                    tool = self._allow(Tool(manifest.name, description, SavedToolArgs, invoke))
                cache[manifest.name] = (digest, tool)
                tools.append(tool)
            self._cache = cache
            return tools

    def summaries(self):
        with self.lock:
            return [{key: getattr(manifest, key) for key in
                     ('name', 'description', 'code', 'example_arguments', 'updated_at', 'version')}
                    for manifest, _ in self._manifests()]

    def errors(self):
        with self.lock:
            self._manifests()
            return list(self._errors)

    def create_tool(self, name, description, code, example_arguments, expected_result, replace_existing=False):
        target = self._target(name)
        _validate_code(code, tool=True)
        with self.lock:
            if any(path.stem.casefold() == name.casefold() and path.stem != name
                   for path in self.directory.glob('*.json')):
                raise ValueError('A tool with this name already exists with different capitalization.')
            previous = self._read_bytes(target) if target.exists() else None
            if previous is not None and not replace_existing:
                raise ValueError('Tool already exists. Use replace_existing=true for an intentional update.')
            version = 1
            if previous is not None:
                try:
                    version = _Manifest.model_validate_json(previous).version + 1
                except ValueError:
                    pass  # Explicit replacement can repair an invalid local manifest.
        tested = self._execute(code, arguments=example_arguments, mode='tool')
        if _json(tested['result']) != _json(expected_result):
            raise ValueError('Tool example test failed. Actual result: ' + _json(tested['result'])[:1000])
        if execution_stopped():
            raise RuntimeError('Tool creation cancelled before saving.')
        manifest = _Manifest(name=name, description=description, code=code,
                             example_arguments=example_arguments, expected_result=expected_result,
                             version=version, updated_at=datetime.now(timezone.utc).isoformat())
        encoded = _json(manifest.model_dump()).encode('utf-8')
        if len(encoded) > MANIFEST_LIMIT:
            raise ValueError('Tool manifest is too large.')
        with self.lock:
            self._target(name)
            if any(path.stem.casefold() == name.casefold() and path.stem != name
                   for path in self.directory.glob('*.json')):
                raise ValueError('A tool with this name appeared during its test; inspect it before retrying.')
            current = self._read_bytes(target) if target.exists() else None
            if current != previous:
                raise ValueError('Tool changed during its test; inspect the current version before retrying.')
            if execution_stopped():
                raise RuntimeError('Tool creation cancelled before saving.')
            descriptor, filename = tempfile.mkstemp(prefix='.tool-', suffix='.tmp', dir=self.directory)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(filename, target)
            finally:
                Path(filename).unlink(missing_ok=True)
        return {'name': name, 'version': version, 'saved': True, 'test_passed': True,
                'path': f'local_tools/{name}.json', 'test_result': tested['result'],
                'stdout': tested['stdout'], 'stderr': tested['stderr']}
