"""Local transactional session snapshots and conservative native-history recovery."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import re
import sqlite3


_UNKNOWN = {'ok': False, 'error': {'code': 'interrupted_unknown_outcome', 'message':
    'The previous process stopped before recording this result. The tool may have run. '
    'Inspect existing files and results before deciding; do not blindly repeat writes.'}}


class SessionConflict(ValueError):
    """A different process has already written a newer session snapshot."""


def repair_history(history: list[dict], provider: str) -> tuple[list[dict], list[str]]:
    """Close only missing native results; this function never invokes a tool."""
    if provider not in {'openai', 'anthropic'}:
        raise ValueError('Unsupported saved provider.')
    output, pending, repaired = [], [], []

    def missing_blocks():
        repaired.extend(pending)
        payload = json.dumps(_UNKNOWN, ensure_ascii=False)
        if provider == 'openai':
            return [{'role': 'tool', 'tool_call_id': call_id, 'content': payload} for call_id in pending]
        return [{'type': 'tool_result', 'tool_use_id': call_id, 'content': payload, 'is_error': True}
                for call_id in pending]

    def flush():
        if pending:
            blocks = missing_blocks()
            output.extend(blocks if provider == 'openai' else [{'role': 'user', 'content': blocks}])
            pending.clear()

    for original in history:
        message = copy.deepcopy(original)
        if provider == 'openai' and message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            if call_id in pending:
                pending.remove(call_id)
            output.append(message)
            continue
        if provider == 'anthropic' and pending and message.get('role') == 'user':
            content = message.get('content')
            blocks = content if isinstance(content, list) else [{'type': 'text', 'text': str(content or '')}]
            present = {block.get('tool_use_id') for block in blocks
                       if isinstance(block, dict) and block.get('type') == 'tool_result'}
            pending[:] = [call_id for call_id in pending if call_id not in present]
            # Tool results must precede any ordinary user text.
            results = [block for block in blocks if isinstance(block, dict) and block.get('type') == 'tool_result']
            rest = [block for block in blocks if not (isinstance(block, dict) and block.get('type') == 'tool_result')]
            message['content'] = results + missing_blocks() + rest
            pending.clear()
            output.append(message)
            continue
        flush()
        output.append(message)
        if message.get('role') == 'assistant':
            if provider == 'openai':
                pending.extend(call['id'] for call in message.get('tool_calls', [])
                               if isinstance(call, dict) and isinstance(call.get('id'), str))
            elif isinstance(message.get('content'), list):
                pending.extend(block['id'] for block in message['content']
                               if isinstance(block, dict) and block.get('type') == 'tool_use'
                               and isinstance(block.get('id'), str))
    flush()
    return output, repaired


class SessionStore:
    """Open connections per transaction so HTTP/worker threads never share one."""

    def __init__(self, root: Path):
        directory = Path(root) / 'data'
        if directory.is_symlink():
            raise ValueError('Session storage must not be a symlink.')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / 'sessions.sqlite3'
        if self.path.is_symlink():
            raise ValueError('Session database must not be a symlink.')
        self.warnings = []
        with closing(self.connect()) as connection, connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('This session database belongs to a newer application.')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS sessions '
                               '(id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')
            if 'revision' not in {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}:
                connection.execute('ALTER TABLE sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            connection.execute('PRAGMA user_version=1')
        if os.name != 'nt':
            self.path.chmod(0o600)

    def connect(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('Unsafe session storage path.')
        return sqlite3.connect(self.path, timeout=5)

    def save(self, record: dict):
        sid = record['detail']['id']
        if not isinstance(sid, str) or not re.fullmatch(r'[a-f0-9]{32}', sid):
            raise ValueError('Invalid session identifier.')
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT revision FROM sessions WHERE id=?', (sid,)).fetchone()
            expected = record.get('revision', 0)
            if existing and existing[0] != expected:
                raise SessionConflict('The session changed in another process; load the newer snapshot.')
            revision = expected + 1
            body = json.dumps({**record, 'revision': revision}, ensure_ascii=False, allow_nan=False)
            connection.execute('INSERT INTO sessions(id,body,updated_at,revision) VALUES(?,?,?,?) '
                               'ON CONFLICT(id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at,revision=excluded.revision',
                               (sid, body, record['detail']['updated_at'], revision))
        return revision

    def load(self) -> list[dict]:
        records = []
        with closing(self.connect()) as connection:
            for sid, body in connection.execute('SELECT id,body FROM sessions ORDER BY updated_at DESC'):
                try:
                    record = json.loads(body)
                    detail, settings = record['detail'], record['settings']
                    if (detail['id'] != sid or not re.fullmatch(r'[a-f0-9]{32}', sid)
                            or settings['provider'] not in {'openai', 'anthropic'}
                            or not isinstance(detail['messages'], list)
                            or not isinstance(record['history'], list)):
                        raise ValueError
                    records.append(record)
                except (ValueError, TypeError, KeyError):
                    self.warnings.append('部分本地会话记录无法读取，原始记录已保留。')
        return records
