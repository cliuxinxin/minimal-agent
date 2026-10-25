"""A loopback-only web host for the same real-model Agent used by the CLI."""
from __future__ import annotations

import argparse
from collections import deque
import copy
from datetime import datetime, timezone
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import webbrowser

from pydantic import ValidationError

from . import __version__
from .builtin_tools import build_tools
from .config import _Config, load_agent
from .diagnostics import diagnostic_fields, exception_diagnostic, write_diagnostic
from .model import AnthropicModel, OpenAIModel
from .programming import ProgrammingTools
from .runtime import runtime_identity
from .search import build_search_tools
from .tracing import JsonlTrace


EXTENSIONS = {'.txt', '.md', '.json', '.csv'}
WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)}
ACTIVE = {'running', 'stopping'}
UPLOAD_LIMIT = 2 * 1024 * 1024
TRACE_EVENT_LIMIT = 100
TRACE_PREVIEW_LIMIT = 4000
SEGMENT_LIMIT = 80
SEGMENT_TEXT_LIMIT = 64 * 1024
SSE_JOURNAL_LIMIT = 512
SSE_HEARTBEAT_SECONDS = 8
DELTA_EVENTS = {'text_delta', 'reasoning_delta', 'tool_call_delta'}


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
    for key in ('task', 'arguments', 'answer', 'text'):
        if isinstance(data.get(key), str) and len(data[key]) > TRACE_PREVIEW_LIMIT:
            data[key] = data[key][:TRACE_PREVIEW_LIMIT] + '\n…（完整内容见本机 runs 日志）'
            data['truncated'] = True
    for key in ('result', 'error'):
        if key not in data:
            continue
        serialized = json.dumps(data[key], ensure_ascii=False, allow_nan=False)
        if len(serialized) > TRACE_PREVIEW_LIMIT:
            data[key] = {'truncated': True, 'preview': serialized[:TRACE_PREVIEW_LIMIT],
                         'note': '仅显示预览；完整内容见本机 runs 日志。'}
    return displayed


class StreamRedactor:
    """Delay a possible key prefix until a later delta proves it safe to publish."""
    def __init__(self, secrets):
        self.secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        self.max_length = max(map(len, self.secrets), default=0)
        self.pending = ''

    def feed(self, text, *, final=False):
        text = self.pending + text
        output, position = [], 0
        while position < len(text):
            remaining = len(text) - position
            # A complete shorter key may also be the prefix of a longer key.
            if not final and remaining < self.max_length:
                tail = text[position:]
                if any(secret.startswith(tail) and len(secret) > remaining for secret in self.secrets):
                    break
            match = next((secret for secret in self.secrets if text.startswith(secret, position)), None)
            if match:
                output.append('[redacted]')
                position += len(match)
            else:
                output.append(text[position])
                position += 1
        self.pending = text[position:]
        return ''.join(output)


def bounded_text(text, limit=SEGMENT_TEXT_LIMIT):
    encoded = text.encode('utf-8')
    return encoded[:limit].decode('utf-8', errors='ignore'), len(encoded) > limit


class RunPresentation:
    """Reduce public Agent events into bounded, resumable conversation segments."""
    def __init__(self, state, session, run, message):
        self.state, self.session, self.run, self.message = state, session, run, message
        self.trace = JsonlTrace(state.root / 'runs')
        self.trace_error = None
        self.channels, self.identities, self.text_lengths, self.omitted_text_lengths = {}, {}, {}, {}
        self.counter = 0

    def log(self, event):
        if self.trace_error is not None:
            return
        try:
            self.trace(event)
        except OSError as failure:
            # A full/unwritable disk must not strand an active run or suppress
            # its in-memory diagnostic and terminal SSE snapshot.
            self.trace_error = type(failure).__name__

    def publish_segment(self, segment):
        self.state.publish(self.session, 'segment', {'run_id': self.run['id'], 'segment': segment,
                           'segments_omitted': self.message['segments_omitted']})

    def segment(self, kind, step, *, index=None, call_id='', name='', contiguous=False):
        identity = (kind, step, index)
        segment_id = self.identities.get(identity)
        parts = self.message['segments']
        found = next((part for part in parts if part['id'] == segment_id), None)
        if contiguous and found is not (parts[-1] if parts else None):
            found = None
        if kind == 'tool' and call_id:
            found = next((part for part in reversed(parts) if part['type'] == 'tool'
                          and part['step'] == step and part['call_id'] == call_id), found)
        if kind == 'tool' and index is None and not found:
            found = next((part for part in parts if part['type'] == 'tool' and part['step'] == step
                          and part['status'] == 'preparing' and not part['call_id']), None)
        if found:
            return found
        self.counter += 1
        part = {'id': f"{self.run['id']}-{self.counter}", 'type': kind, 'step': step}
        if kind == 'tool':
            part.update(call_id=call_id, name=name, status='preparing', arguments='')
        else:
            part['text'] = ''
        parts.append(part)
        excess = len(parts) - SEGMENT_LIMIT
        if excess > 0:
            for removed in parts[:excess]:
                if removed['type'] == 'text' and removed['step'] == step:
                    self.omitted_text_lengths[step] = (self.omitted_text_lengths.get(step, 0)
                                                       + self.text_lengths.get(removed['id'], 0))
            del parts[:excess]
            self.message['segments_omitted'] += excess
            retained = {item['id'] for item in parts}
            self.identities = {key: value for key, value in self.identities.items() if value in retained}
            self.text_lengths = {key: value for key, value in self.text_lengths.items() if key in retained}
        # Calls without a streamed index are identified by their native call id.
        if kind != 'tool' or index is not None:
            self.identities[identity] = part['id']
        self.publish_segment(part)
        return part

    def append_text(self, kind, step, text):
        if not text:
            return
        segment = self.segment(kind, step, contiguous=True)
        self.text_lengths[segment['id']] = self.text_lengths.get(segment['id'], 0) + len(text)
        previous = segment['text']
        segment['text'], truncated = bounded_text(previous + text)
        addition = segment['text'][len(previous):]
        if addition:
            self.state.publish(self.session, 'segment_delta', {'run_id': self.run['id'],
                               'segment_id': segment['id'], 'text': addition})
        if truncated and not segment.get('truncated'):
            segment['truncated'] = True
            self.publish_segment(segment)

    def canonical_text(self, step, text, *, publish=False):
        if not text:
            return
        parts = [part for part in self.message['segments'] if part['type'] == 'text' and part['step'] == step]
        if not parts:
            parts = [self.segment('text', step)]
        offset = self.omitted_text_lengths.get(step, 0)
        self.omitted_text_lengths = {step: offset} if offset else {}
        # Preserve text/tool/text block order while reconciling the provider's
        # full answer. Each earlier text block keeps its original span.
        for position, segment in enumerate(parts):
            length = self.text_lengths.get(segment['id'], len(segment['text']))
            fragment = text[offset:] if position == len(parts) - 1 else text[offset:offset + length]
            offset += length
            self.text_lengths[segment['id']] = len(fragment)
            preview, truncated = bounded_text(fragment)
            if publish or segment['text'] != preview or bool(segment.get('truncated')) != truncated:
                segment['text'] = preview
                if truncated:
                    segment['truncated'] = True
                else:
                    segment.pop('truncated', None)
                self.publish_segment(segment)

    def consume_delta(self, kind, step, data):
        if kind in {'text_delta', 'reasoning_delta'}:
            self.append_text('text' if kind == 'text_delta' else 'reasoning', step, data.get('text', ''))
            return
        segment = self.segment('tool', step, index=data['index'], call_id=data.get('id', ''),
                               name=data.get('name', ''))
        before = dict(segment)
        for source, target in (('id', 'call_id'), ('name', 'name')):
            if data.get(source):
                segment[target] = data[source]
        if not segment.get('arguments_truncated'):
            arguments = segment['arguments'] + data.get('arguments_delta', '')
            segment['arguments'] = arguments[:TRACE_PREVIEW_LIMIT]
            if len(arguments) > TRACE_PREVIEW_LIMIT:
                segment['arguments_truncated'] = True
        if before != segment:
            self.publish_segment(segment)

    def delta(self, event):
        kind, step, raw = event['event'], event.get('step', 0), event.get('data', {})
        index = raw.get('index', 0) if kind == 'tool_call_delta' else None
        key = (kind, step, index)
        channel = self.channels.setdefault(key, {'redactor': StreamRedactor(self.session['secret'])})
        data = {}
        field = 'arguments_delta' if kind == 'tool_call_delta' else 'text'
        if kind == 'tool_call_delta':
            data['index'] = index
            for name in ('id', 'name'):
                value = raw.get(name, '')
                # Names and ids are cumulative values, unlike argument deltas.
                data[name] = StreamRedactor(self.session['secret']).feed(value) if isinstance(value, str) else ''
        value = raw.get(field, '')
        data[field] = channel['redactor'].feed(value if isinstance(value, str) else '')
        safe = {key: value for key, value in event.items() if key != 'data'}
        safe.update(run_id=self.run['id'], data=data)
        channel.update(event=safe, field=field)
        self.log(safe)
        self.consume_delta(kind, step, data)

    def flush(self, step=None):
        for key, channel in list(self.channels.items()):
            if step is not None and key[1] != step:
                continue
            tail = channel['redactor'].feed('', final=True)
            if tail:
                event = copy.deepcopy(channel['event'])
                event['data'][channel['field']] = tail
                self.log(event)
                self.consume_delta(key[0], key[1], event['data'])
            del self.channels[key]

    def on_event(self, event):
        with self.state.lock:
            kind = event.get('event')
            self.run['step'] = event.get('step', self.run['step'])
            if kind in DELTA_EVENTS:
                self.delta(event)
                return
            if kind in {'model_complete', 'run_end'}:
                self.flush(event.get('step') if kind == 'model_complete' else None)
            safe = redact(event, self.session['secret'])
            safe['run_id'] = self.run['id']
            self.log(safe)
            if kind == 'model_output':
                return  # Native signed/encrypted blocks belong only in local history/logs.
            displayed = browser_event(safe)
            data, step = displayed.get('data', {}), self.run['step']
            if kind == 'model_complete':
                self.canonical_text(step, safe.get('data', {}).get('text', ''), publish=True)
            elif kind in {'tool_call', 'tool_result'}:
                segment = self.segment('tool', step, call_id=data.get('id', ''), name=data.get('name', ''))
                segment.update(call_id=data.get('id', segment['call_id']), name=data.get('name', segment['name']))
                if kind == 'tool_call':
                    arguments = safe.get('data', {}).get('arguments', '')
                    segment.update(status='running', arguments=arguments[:TRACE_PREVIEW_LIMIT])
                    if len(arguments) > TRACE_PREVIEW_LIMIT:
                        segment['arguments_truncated'] = True
                    else:
                        segment.pop('arguments_truncated', None)
                else:
                    error = data.get('error')
                    cancelled = isinstance(error, dict) and error.get('code') == 'cancelled'
                    segment['status'] = 'completed' if data.get('ok') else ('cancelled' if cancelled else 'error')
                    if 'result' in data:
                        segment['result'] = data['result']
                    if error is not None:
                        segment['error'] = error
                self.publish_segment(segment)
            self.run['events'].append(displayed)
            self.run['event_count'] += 1
            excess = len(self.run['events']) - TRACE_EVENT_LIMIT
            if excess > 0:
                del self.run['events'][:excess]
                self.run['omitted_events'] += excess
            self.state.publish(self.session, 'trace', {'run_id': self.run['id'], 'event': displayed,
                               'event_count': self.run['event_count'], 'omitted_events': self.run['omitted_events']})
            if kind in {'run_start', 'model_start'}:
                self.state.publish_status(self.session, self.run)


class WebState:
    def __init__(self, root):
        self.root = root.resolve()
        # Freeze the identity of this process. Static browser assets may change
        # on disk after an update while these Python modules remain loaded.
        self.runtime = runtime_identity(self.root)
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
            agent = load_agent(self.root / 'config.json', workspace=self.workspace, approve=None, stream=True)
            config = self.read_config()
        except (ValueError, OSError):
            raise RequestError(400, '请先保存有效的模型配置。') from None
        timestamp, sid = now(), uuid4().hex
        detail = {'id': sid, 'title': '新对话', 'provider': config.provider, 'model': config.model,
                  'max_steps': config.max_steps, 'search_enabled': bool(config.search_api_key and
                      config.search_api_key.get_secret_value().strip()),
                  'created_at': timestamp, 'updated_at': timestamp, 'messages': [], 'run': None}
        self.sessions[sid] = {'detail': detail, 'agent': agent,
                              'runtime': dict(self.runtime),
                              'endpoint': self.model(config).endpoint,
                              'secret': [config.api_key.get_secret_value(),
                                         config.search_api_key.get_secret_value() if config.search_api_key else ''],
                              'stop': None, 'sequence': 0, 'run_sequence': 0,
                              'journal': deque(maxlen=SSE_JOURNAL_LIMIT),
                              'changed': threading.Condition(self.lock)}
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

    def publish(self, session, kind, data):
        """Capture an immutable event while holding the state lock; never do socket IO here."""
        with self.lock:
            session['sequence'] += 1
            session['journal'].append({'id': session['sequence'], 'event': kind, 'data': copy.deepcopy(data)})
            session['changed'].notify_all()

    def publish_status(self, session, run):
        self.publish(session, 'run_status', {'run_id': run['id'], **{
            key: run[key] for key in ('status', 'step', 'error', 'event_count', 'omitted_events')}})

    @staticmethod
    def assistant_message(session, run):
        for message in reversed(session['detail']['messages']):
            if message.get('role') == 'assistant' and message.get('run_id') == run['id']:
                return message
        message = {'role': 'assistant', 'run_id': run['id'], 'content': '', 'status': 'running',
                   'segments': [], 'segments_omitted': 0}
        session['detail']['messages'].append(message)
        return message

    def event_batch(self, session, cursor):
        """Return a snapshot at one frontier, or every retained event after a cursor."""
        sequence, journal = session['sequence'], session['journal']
        available = cursor is not None and session['run_sequence'] <= cursor <= sequence and (
            cursor == sequence or (journal and cursor >= journal[0]['id'] - 1))
        if not available:
            return [{'id': sequence, 'event': 'snapshot',
                     'data': {'session': copy.deepcopy(session['detail'])}}]
        return [frame for frame in journal if frame['id'] > cursor]

    def start(self, session, data):
        message = data.get('message')
        if set(data) != {'message'} or not isinstance(message, str) or not message.strip() or len(message) > 60000:
            raise RequestError(400, '消息必须为 1–60000 字符的非空文本。')
        detail = session['detail']
        if detail['run'] and detail['run']['status'] in ACTIVE:
            raise RequestError(409, '该对话仍在运行，请等待或停止。')
        run = {'id': uuid4().hex, 'status': 'running', 'step': 0, 'error': None, 'events': [],
               'started_at': now(),
               'event_count': 0, 'omitted_events': 0}
        session['stop'] = threading.Event()
        detail['run'] = run
        if not detail['messages']:
            detail['title'] = redact(message.strip(), session['secret'])[:28]
        detail['messages'].append({'role': 'user', 'content': redact(message, session['secret'])})
        self.assistant_message(session, run)
        detail['updated_at'] = now()
        # Keep only this run's replay history. Older cursors need a new snapshot
        # containing the new user/assistant pair, even at the adjacent sequence.
        session['journal'].clear()
        session['run_sequence'] = session['sequence'] + 1
        self.publish_status(session, run)
        threading.Thread(target=self.work, args=(session, message, run), daemon=True).start()
        return {'session_id': detail['id'], 'run_id': run['id']}

    def work(self, session, message, run):
        with self.lock:
            session['detail']['run'] = run
            assistant = self.assistant_message(session, run)
            presentation = RunPresentation(self, session, run, assistant)
        session['agent'].on_event = presentation.on_event
        diagnostic = None
        try:
            result = session['agent'].run(message, should_stop=session['stop'].is_set)
            status = result.status if result.status in {'completed', 'cancelled'} else 'error'
            error = redact(result.error, session['secret'])
            answer = redact(result.answer, session['secret'])
            diagnostic = diagnostic_fields(getattr(result, 'diagnostic', None))
        except Exception as failure:
            status, error, answer = 'error', '运行遇到内部错误，请检查配置后重试。', ''
            diagnostic = exception_diagnostic(failure, phase='host_run')
        with self.lock:
            presentation.flush()
            if status == 'completed':
                presentation.canonical_text(run['step'], answer)
                content = answer
            else:
                partial = '\n\n'.join(part['text'] for part in assistant['segments'] if part['type'] == 'text')
                notice = '运行已停止。' if status == 'cancelled' else f'运行失败：{error or "模型未返回最终答案。"}'
                content = partial + '\n\n' + notice if partial else notice
            for segment in assistant['segments']:
                if segment['type'] == 'tool' and segment['status'] in {'preparing', 'running'}:
                    unstarted = segment['status'] == 'preparing'
                    segment['status'] = 'not_executed' if unstarted else 'interrupted'
                    segment['error'] = {
                        'code': 'cancelled' if status == 'cancelled' else 'run_incomplete',
                        'message': ('工具参数尚未生成完整，本次工具未执行。' if unstarted else
                                    '运行已中断，尚未收到工具执行结果。'),
                    }
                    presentation.publish_segment(segment)
            run.update(status=status, error=error)
            assistant.update(content=content, status=status)
            session['detail']['updated_at'] = now()
            if status == 'error':
                report = self.failure_report(session, run, assistant, diagnostic,
                                             trace_error=presentation.trace_error)
                try:
                    write_diagnostic(self.root, report)
                except (OSError, ValueError, TypeError) as failure:
                    report['diagnostic_file'] = None
                    report['storage_error'] = type(failure).__name__
                run['diagnostic'] = assistant['diagnostic'] = report
            self.publish_status(session, run)
            self.publish(session, 'snapshot', {'session': session['detail']})

    @staticmethod
    def failure_report(session, run, assistant, diagnostic, *, trace_error=None):
        detail = session['detail']
        error = diagnostic_fields(diagnostic)
        error_message = run['error']
        if error.get('code') == 'unexpected_error':
            # Unexpected exceptions may embed user text, file contents or other
            # business data. The local trace retains them; the shareable report
            # keeps the exception class, stage and code location instead.
            error_message = f"Unexpected {error.get('type', 'error')} during {error.get('phase', 'agent_run')}."
        report = {
            'schema_version': 1, 'app_version': __version__,
            'runtime': session['runtime'],
            'run_id': run['id'], 'session_id': detail['id'],
            'started_at': run['started_at'], 'finished_at': detail['updated_at'],
            'status': run['status'], 'step': run['step'],
            'connection': {'provider': detail['provider'], 'model': detail['model'],
                           'endpoint': session['endpoint'], 'stream': True},
            'error': {'code': 'run_incomplete', 'phase': 'agent_run',
                      **error, 'message': error_message},
            'tools': [{
                'name': part['name'], 'call_id': part['call_id'], 'status': part['status'],
                'step': part['step'], 'arguments_chars': len(part['arguments']),
                'has_result': 'result' in part,
            } for part in assistant['segments'] if part['type'] == 'tool'],
            'events': [{key: event[key] for key in ('event', 'step', 'time') if key in event}
                       for event in run['events']],
            'omitted_events': run['omitted_events'],
            'log_file': None if trace_error else f"runs/{run['id']}.jsonl",
            'diagnostic_file': f"runs/{run['id']}.diagnostic.json",
        }
        if trace_error:
            report['log_error'] = trace_error
        return redact(report, session['secret'])


class Handler(BaseHTTPRequestHandler):
    server_version = f'MinimalAgent/{__version__}'

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
        self.streaming_started = False
        try:
            self.check_headers()
            path = urlsplit(self.path).path
            if method == 'GET' and path.startswith('/api/sessions/') and path.count('/') == 4 and path.endswith('/events'):
                self.stream_events(path.split('/')[3])
                return
            data = self.read_body() if method == 'POST' else None
            with self.server.state.lock:
                status, payload, mime = self.route(method, path, data)
                payload = copy.deepcopy(payload)
            self.send(status, payload, mime)
        except RequestError as error:
            if not self.streaming_started:
                self.send(error.status, {'error': error.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            if not self.streaming_started:
                self.send(500, {'error': '请求处理失败，请稍后重试。'})

    def stream_events(self, sid):
        try:
            parameters = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise RequestError(400, '事件游标无效。') from None
        if set(parameters) - {'after'} or any(len(values) != 1 for values in parameters.values()):
            raise RequestError(400, '事件地址只接受一个 after 游标。')
        if len(self.headers.get_all('Last-Event-ID', [])) > 1:
            raise RequestError(400, '事件游标无效。')
        value = parameters.get('after', [self.headers.get('Last-Event-ID')])[0]
        if value is not None and (not value.isascii() or not value.isdigit() or len(value) > 20):
            raise RequestError(400, '事件游标无效。')
        cursor = int(value) if value is not None else None
        state = self.server.state
        with state.lock:
            session = state.get_session(sid)
            frames = state.event_batch(session, cursor)
            run = session['detail']['run']
            terminal = not run or run['status'] not in ACTIVE
            if terminal and not frames:
                frames = state.event_batch(session, None)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.streaming_started = True
        self.close_connection = True
        while True:
            # All payloads were captured under the lock. Slow/disconnected clients
            # must never prevent another request, model delta or stop action.
            for frame in frames:
                data = json.dumps(frame['data'], ensure_ascii=False, allow_nan=False)
                raw = f"id: {frame['id']}\nevent: {frame['event']}\ndata: {data}\n\n"
                self.wfile.write(raw.encode('utf-8'))
                self.wfile.flush()
                cursor = frame['id']
            if terminal:
                return
            with session['changed']:
                frames = state.event_batch(session, cursor)
                run = session['detail']['run']
                terminal = not run or run['status'] not in ACTIVE
                if not frames and not terminal:
                    session['changed'].wait(timeout=SSE_HEARTBEAT_SECONDS)
                    frames = state.event_batch(session, cursor)
                    run = session['detail']['run']
                    terminal = not run or run['status'] not in ACTIVE
            if not frames and not terminal:
                self.wfile.write(b': keep-alive\n\n')
                self.wfile.flush()

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
                result = state.runtime
            elif path in {'/api/bootstrap', '/api/config'}:
                result = {'config': state.masked_config()}
                if path == '/api/bootstrap':
                    result.update(runtime=state.runtime, sessions=state.summaries(),
                                  files=state.files(), **state.tool_inventory())
            elif path == '/api/tools':
                result = state.tool_inventory()
            elif path == '/api/sessions':
                result = {'sessions': state.summaries()}
            elif path == '/api/files':
                result = {'files': state.files()}
            elif path.startswith('/api/sessions/') and path.count('/') == 3:
                result = {'session': state.get_session(path.rsplit('/', 1)[-1])['detail']}
            elif path in {'/', '/index.html', '/app.js', '/style.css', '/assets/chat.js', '/assets/chat.css'}:
                name = 'index.html' if path == '/' else path[1:]
                file = state.root / 'web' / name
                if (file.is_file() and not file.is_symlink()
                        and file.resolve().is_relative_to((state.root / 'web').resolve())):
                    mime = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}[file.suffix]
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
                    state.assistant_message(session, run)['status'] = 'stopping'
                    state.publish_status(session, run)
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
