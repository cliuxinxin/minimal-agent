"""A loopback-only web host for the same real-model Agent used by the CLI."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

from pydantic import ValidationError

from .builtin_tools import build_tools
from .config import _Config, load_agent
from .model import AnthropicModel, OpenAIModel
from .programming import ProgrammingTools
from .search import build_search_tools
from .tracing import JsonlTrace


EXTENSIONS = {'.txt', '.md', '.json', '.csv'}
WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)}
ACTIVE = {'running', 'stopping'}
UPLOAD_LIMIT = 2 * 1024 * 1024
TRACE_EVENT_LIMIT = 100
TRACE_PREVIEW_LIMIT = 4000


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def now():
    return datetime.now(timezone.utc).isoformat()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def redact(value, secret):
    if isinstance(secret, (list, tuple)):
        for item in secret:
            value = redact(value, item)
        return value
    if isinstance(value, str):
        return value.replace(secret, '[redacted]') if secret else value
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret) if isinstance(key, str) else key: redact(item, secret)
                for key, item in value.items()}
    return value


def browser_event(event):
    """Bound the UI trace while preserving the complete event for disk logging."""
    displayed = copy.deepcopy(event)
    data = displayed.get('data', {})
    for key in ('task', 'arguments', 'answer'):
        if isinstance(data.get(key), str) and len(data[key]) > TRACE_PREVIEW_LIMIT:
            data[key] = data[key][:TRACE_PREVIEW_LIMIT] + '\n…（完整内容见本机 runs 日志）'
            data['truncated'] = True
    if 'result' in data:
        serialized = json.dumps(data['result'], ensure_ascii=False, allow_nan=False)
        if len(serialized) > TRACE_PREVIEW_LIMIT:
            data['result'] = {'truncated': True, 'preview': serialized[:TRACE_PREVIEW_LIMIT],
                              'note': '仅显示预览；完整内容见本机 runs 日志。'}
    return displayed


class WebState:
    def __init__(self, root):
        self.root = root.resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions = {}

    def read_config(self):
        try:
            return _Config.model_validate_json((self.root / 'config.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    @staticmethod
    def model(config):
        cls = OpenAIModel if config.provider == 'openai' else AnthropicModel
        return cls(config.model, config.api_key.get_secret_value(), config.base_url)

    def masked_config(self):
        config = self.read_config()
        valid = False
        if config:
            try:
                self.model(config)
                valid = True
            except ValueError:
                pass
        return {'provider': config.provider if config else 'openai',
                'model': config.model if config else '',
                'base_url': (config.base_url or '') if config else '',
                'has_api_key': bool(config and config.api_key.get_secret_value().strip()),
                'max_steps': config.max_steps if config else 80,
                'has_search_api_key': bool(config and config.search_api_key and
                                           config.search_api_key.get_secret_value().strip()),
                'configured': valid}

    def save_config(self, data):
        if set(data) - {'provider', 'model', 'api_key', 'base_url', 'max_steps', 'search_api_key'}:
            raise RequestError(400, '配置中包含不支持的字段。')
        old = self.read_config()
        data = dict(data)
        data.setdefault('max_steps', old.max_steps if old else 80)
        old_search_key = old.search_api_key.get_secret_value() if old and old.search_api_key else None
        if 'search_api_key' not in data or data['search_api_key'] == '':
            data['search_api_key'] = old_search_key
        elif data['search_api_key'] is not None:
            if not isinstance(data['search_api_key'], str):
                raise RequestError(400, 'DeepSeek 搜索 API Key 必须为文本。')
            data['search_api_key'] = data['search_api_key'].strip() or old_search_key
        if isinstance(data.get('base_url'), str):
            data['base_url'] = data['base_url'].strip().rstrip('/') or None
        if 'api_key' in data and not isinstance(data['api_key'], str):
            raise RequestError(400, 'API Key 必须是文本。')
        if not data.get('api_key', '').strip():
            if not old or data.get('provider') != old.provider:
                raise RequestError(400, '更改服务商或接口地址后，请重新填写 API Key。')
            data['api_key'] = old.api_key.get_secret_value()
            try:
                candidate = _Config.model_validate(data)
                if self.model(candidate).endpoint != self.model(old).endpoint:
                    raise ValueError
            except (ValidationError, ValueError):
                raise RequestError(400, '更改服务商或接口地址后，请重新填写 API Key。') from None
        try:
            config = _Config.model_validate(data)
            self.model(config)
            if config.search_api_key:
                build_search_tools(config.search_api_key.get_secret_value())
        except (ValidationError, ValueError):
            raise RequestError(400, '配置无效，请检查模型设置、密钥和调用次数上限。') from None
        stored = config.model_dump(exclude={'api_key', 'search_api_key'})
        stored['api_key'] = config.api_key.get_secret_value()
        stored['search_api_key'] = config.search_api_key.get_secret_value() if config.search_api_key else None
        descriptor, filename = tempfile.mkstemp(prefix='.config-', dir=self.root)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(stored, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, self.root / 'config.json')
        finally:
            Path(filename).unlink(missing_ok=True)
        return self.masked_config()

    def tool_inventory(self):
        programming = ProgrammingTools(self.root, self.workspace)
        tools = build_tools(self.workspace) + programming.builtin_tools()
        config = self.read_config()
        if config and config.search_api_key and config.search_api_key.get_secret_value().strip():
            tools.extend(build_search_tools(config.search_api_key.get_secret_value()))
        saved = programming.summaries()
        return {'tools': [{'name': tool.name, 'description': tool.description} for tool in tools],
                'saved_tools': saved, 'tool_errors': programming.errors()}

    def files(self):
        return [{'name': path.name, 'size': path.stat().st_size}
                for path in sorted(self.workspace.iterdir())
                if path.suffix.lower() in EXTENSIONS and path.is_file()
                and not path.is_symlink() and not path.name.startswith('.')]

    def upload(self, data):
        name, content = data.get('name'), data.get('content')
        if (set(data) != {'name', 'content'} or not isinstance(name, str) or not name
                or len(name) > 200 or any(c in name for c in '/\\:"<>|?*')
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or name.endswith(('.', ' '))
                or name.split('.')[0].rstrip().upper() in WINDOWS_DEVICES
                or name.startswith('.') or Path(name).suffix.lower() not in EXTENSIONS
                or not isinstance(content, str)):
            raise RequestError(400, '文件名须为普通文件名；只支持 UTF-8 txt、md、json、csv 文件。')
        try:
            encoded = content.encode('utf-8')
        except UnicodeError:
            raise RequestError(400, '文件内容必须是有效 UTF-8 文本。') from None
        if len(encoded) > UPLOAD_LIMIT:
            raise RequestError(413, '每个文件最多 2 MiB。')
        original = Path(name)
        workspace = self.workspace.resolve()
        for index in range(10000):
            filename = name if index == 0 else f'{original.stem}-{index}{original.suffix}'
            candidate = workspace / filename
            try:
                if not candidate.resolve().is_relative_to(workspace):
                    raise RequestError(400, '文件路径必须位于工作区内。')
            except (OSError, RuntimeError):
                raise RequestError(400, '文件路径无效。') from None
            try:
                with candidate.open('xb') as handle:
                    handle.write(encoded)
                return {'name': filename, 'size': len(encoded)}
            except FileExistsError:
                continue
        raise RequestError(409, '同名文件过多，请更换文件名。')

    def new_session(self):
        try:
            agent = load_agent(self.root / 'config.json', workspace=self.workspace, approve=None)
            config = self.read_config()
        except (ValueError, OSError):
            raise RequestError(400, '请先保存有效的模型配置。') from None
        timestamp, sid = now(), uuid4().hex
        detail = {'id': sid, 'title': '新对话', 'provider': config.provider, 'model': config.model,
                  'max_steps': config.max_steps, 'search_enabled': bool(config.search_api_key and
                      config.search_api_key.get_secret_value().strip()),
                  'created_at': timestamp, 'updated_at': timestamp, 'messages': [], 'run': None}
        self.sessions[sid] = {'detail': detail, 'agent': agent,
                              'secret': [config.api_key.get_secret_value(),
                                         config.search_api_key.get_secret_value() if config.search_api_key else ''],
                              'stop': None}
        return detail

    def get_session(self, sid):
        if sid not in self.sessions:
            raise RequestError(404, '未找到该对话。')
        return self.sessions[sid]

    def summaries(self):
        summaries = []
        for session in self.sessions.values():
            detail = session['detail']
            summary = {key: detail[key] for key in ('id', 'title', 'provider', 'model', 'created_at', 'updated_at')}
            summary['status'] = detail['run']['status'] if detail['run'] else 'idle'
            summaries.append(summary)
        return sorted(summaries, key=lambda item: item['updated_at'], reverse=True)

    def start(self, session, data):
        message = data.get('message')
        if set(data) != {'message'} or not isinstance(message, str) or not message.strip() or len(message) > 60000:
            raise RequestError(400, '消息必须为 1–60000 字符的非空文本。')
        detail = session['detail']
        if detail['run'] and detail['run']['status'] in ACTIVE:
            raise RequestError(409, '该对话仍在运行，请等待或停止。')
        run = {'id': uuid4().hex, 'status': 'running', 'step': 0, 'error': None, 'events': [],
               'event_count': 0, 'omitted_events': 0}
        session['stop'] = threading.Event()
        detail['run'] = run
        if not detail['messages']:
            detail['title'] = message.strip()[:28]
        detail['messages'].append({'role': 'user', 'content': message})
        detail['updated_at'] = now()
        threading.Thread(target=self.work, args=(session, message, run), daemon=True).start()
        return {'session_id': detail['id'], 'run_id': run['id']}

    def work(self, session, message, run):
        trace = JsonlTrace(self.root / 'runs')
        def on_event(event):
            event = redact(event, session['secret'])
            event['run_id'] = run['id']
            trace(event)
            if event['event'] != 'model_output':
                with self.lock:
                    run['events'].append(browser_event(event))
                    run['event_count'] += 1
                    excess = len(run['events']) - TRACE_EVENT_LIMIT
                    if excess > 0:
                        del run['events'][:excess]
                        run['omitted_events'] += excess
                    run['step'] = event.get('step', run['step'])
        session['agent'].on_event = on_event
        try:
            result = session['agent'].run(message, should_stop=session['stop'].is_set)
            status = result.status if result.status in {'completed', 'cancelled'} else 'error'
            error = redact(result.error, session['secret'])
            content = result.answer if status == 'completed' else ('运行已停止。' if status == 'cancelled' else f'运行失败：{error or "模型未返回最终答案。"}')
        except Exception:
            status, error, content = 'error', '运行遇到内部错误，请检查配置后重试。', '运行遇到内部错误，请检查配置后重试。'
        with self.lock:
            run.update(status=status, error=error)
            session['detail']['messages'].append({'role': 'assistant', 'content': redact(content, session['secret']), 'status': status})
            session['detail']['updated_at'] = now()


class Handler(BaseHTTPRequestHandler):
    server_version = 'MinimalAgent/0.3.0'

    def log_message(self, *args):
        pass  # Do not log bodies, keys, or user messages.

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, status, payload, mime='application/json; charset=utf-8'):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8') if isinstance(payload, dict) else payload
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def check_headers(self):
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts or len(self.headers.get_all('Host', [])) != 1:
            raise RequestError(403, '只允许本机访问。')
        origin = self.headers.get('Origin')
        if origin is not None and (origin not in {f'http://{host}' for host in hosts}
                                   or len(self.headers.get_all('Origin', [])) != 1):
            raise RequestError(403, '请求来源不受支持。')

    def read_body(self):
        if self.headers.get_content_type() != 'application/json':
            raise RequestError(415, '请求必须使用 application/json。')
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise RequestError(400, '请求长度无效。')
        try:
            length = int(self.headers['Content-Length'])
            if length < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise RequestError(400, '请求长度无效。') from None
        if length > 16 * 1024 * 1024:
            raise RequestError(413, '请求过大。')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            data = json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise RequestError(400, '请求必须是有效 JSON 对象。') from None

    def handle_request(self, method):
        try:
            self.check_headers()
            path = urlsplit(self.path).path
            data = self.read_body() if method == 'POST' else None
            with self.server.state.lock:
                status, payload, mime = self.route(method, path, data)
                payload = copy.deepcopy(payload)
            self.send(status, payload, mime)
        except RequestError as error:
            self.send(error.status, {'error': error.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.send(500, {'error': '请求处理失败，请稍后重试。'})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def route(self, method, path, data):
        state = self.server.state
        status, result = 200, None
        if method == 'GET':
            if path == '/favicon.ico':
                return 204, b'', 'image/x-icon'
            if path == '/api/health':
                result = {'app': 'minimal-agent-web', 'version': '0.3.0',
                          'workspace_id': hashlib.sha256(str(state.root).encode()).hexdigest()[:12]}
            elif path in {'/api/bootstrap', '/api/config'}:
                result = {'config': state.masked_config()}
                if path == '/api/bootstrap':
                    result.update(sessions=state.summaries(), files=state.files(), **state.tool_inventory())
            elif path == '/api/tools':
                result = state.tool_inventory()
            elif path == '/api/sessions':
                result = {'sessions': state.summaries()}
            elif path == '/api/files':
                result = {'files': state.files()}
            elif path.startswith('/api/sessions/') and path.count('/') == 3:
                result = {'session': state.get_session(path.rsplit('/', 1)[-1])['detail']}
            elif path in {'/', '/index.html', '/app.js', '/style.css'}:
                name = 'index.html' if path == '/' else path[1:]
                file = state.root / 'web' / name
                if file.is_file() and not file.is_symlink():
                    mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name]
                    return 200, file.read_bytes(), mime + '; charset=utf-8'
        elif method == 'POST':
            if path == '/api/config':
                result = {'config': state.save_config(data)}
            elif path == '/api/files':
                status, result = 201, {'file': state.upload(data)}
            elif path == '/api/sessions':
                if data:
                    raise RequestError(400, '创建对话不接受额外字段。')
                status, result = 201, {'session': state.new_session()}
            elif path.startswith('/api/sessions/') and path.count('/') == 4:
                sid, action = path.split('/')[3:]
                session = state.get_session(sid)
                if action == 'messages':
                    status, result = 202, state.start(session, data)
                elif action == 'stop':
                    run = session['detail']['run']
                    if set(data) != {'run_id'} or not run or data['run_id'] != run['id'] or run['status'] not in ACTIVE:
                        raise RequestError(409, '运行已结束或运行编号已过期。')
                    session['stop'].set()
                    run['status'] = 'stopping'
                    result = {'session': session['detail']}
        if result is None:
            raise RequestError(404, '未找到该地址。')
        return status, result, 'application/json; charset=utf-8'


def create_server(root: Path, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.state = WebState(Path(root))
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='最小 Agent 本地网页')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        server = create_server(root, args.port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        server = create_server(root, 0)
    url = f'http://127.0.0.1:{server.server_address[1]}'
    print(f'最小 Agent：{url}\n关闭此窗口即可停止服务。', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
