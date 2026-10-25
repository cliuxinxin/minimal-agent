"""Immutable, bounded text artifacts captured from the local workspace."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from uuid import uuid4


FILE_LIMIT = 2 * 1024 * 1024
PREVIEW_LIMIT = 64 * 1024
DIFF_LIMIT = 32 * 1024
SCAN_LIMIT = 1000
SCAN_BYTES_LIMIT = 32 * 1024 * 1024
EXTENSIONS = {'.txt', '.md', '.json', '.csv', '.py', '.js', '.ts', '.tsx', '.jsx',
              '.css', '.html', '.yaml', '.yml'}


def _read_regular(path, limit):
    """Reject symlinks, including a last-component swap before opening."""
    if path.is_symlink() or not path.is_file():
        raise ValueError('Artifact files must be regular files.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Artifact files must be regular files.')
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Artifact file exceeds its size limit.')
    return raw


class ArtifactStore:
    def __init__(self, root: Path, workspace: Path):
        self.root = Path(root).resolve()
        if Path(workspace).is_symlink():
            raise ValueError('Workspace must not be a symlink.')
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError('Workspace must be an existing directory.')
        self.directory = self.root / 'data' / 'artifacts'
        self.lock = threading.RLock()
        self._check_storage(create=True)

    def _check_storage(self, *, create=False):
        for folder in (self.root / 'data', self.directory):
            if folder.is_symlink() or folder.resolve() != folder:
                raise ValueError('Artifact storage must be a regular project directory.')
            if create:
                folder.mkdir(mode=0o700, exist_ok=True)
            if not folder.is_dir():
                raise ValueError('Artifact storage is unavailable.')

    def _workspace_file(self, name):
        if (not isinstance(name, str) or not name or '\\' in name
                or any(ord(char) < 32 for char in name)):
            raise ValueError('Invalid workspace artifact name.')
        parts = PurePosixPath(name)
        if parts.is_absolute() or any(part.startswith('.') or ':' in part for part in parts.parts):
            raise ValueError('Invalid workspace artifact name.')
        target = self.workspace.joinpath(*parts.parts)
        for path in (self.workspace, *[self.workspace.joinpath(*parts.parts[:index])
                                       for index in range(1, len(parts.parts) + 1)]):
            if path.is_symlink():
                raise ValueError('Workspace artifacts must not follow symlinks.')
        if not target.resolve().is_relative_to(self.workspace):
            raise ValueError('Artifact path must stay inside workspace.')
        return target

    def scan(self):
        snapshots, inspected, total_bytes = {}, 0, 0
        if self.workspace.is_symlink() or self.workspace.resolve() != self.workspace:
            raise ValueError('Workspace must remain a regular directory.')
        for directory, folders, files in os.walk(self.workspace, followlinks=False):
            inspected += 1
            if inspected > SCAN_LIMIT:
                return snapshots
            current = Path(directory)
            depth = len(current.relative_to(self.workspace).parts)
            folders[:] = sorted(name for name in folders if not name.startswith('.')
                                and not (current / name).is_symlink()) if depth < 8 else []
            for filename in sorted(files):
                inspected += 1
                if inspected > SCAN_LIMIT or total_bytes >= SCAN_BYTES_LIMIT:
                    return snapshots
                path = current / filename
                if filename.startswith('.') or path.suffix.lower() not in EXTENSIONS:
                    continue
                name = path.relative_to(self.workspace).as_posix()
                try:
                    path = self._workspace_file(name)
                    if path.stat().st_size > FILE_LIMIT:
                        continue
                    raw = _read_regular(path, FILE_LIMIT)
                    text = raw.decode('utf-8')
                    if '\0' in text:
                        continue
                except (OSError, UnicodeError, ValueError):
                    continue
                total_bytes += len(raw)
                snapshots[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                                   'text': text[:PREVIEW_LIMIT], 'truncated': len(text) > PREVIEW_LIMIT}
        return snapshots

    @staticmethod
    def _write(path, raw):
        descriptor, name = tempfile.mkstemp(prefix='.artifact-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def capture(self, before: dict, *, run_id: str):
        if not isinstance(before, dict) or not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
            raise ValueError('Artifact capture requires a snapshot and run identifier.')
        if any(ord(char) < 32 for char in run_id):
            raise ValueError('Invalid run identifier.')
        after = self.scan()
        captured = []
        with self.lock:
            self._check_storage()
            for name, snapshot in after.items():
                previous = before.get(name)
                if isinstance(previous, dict) and previous.get('sha256') == snapshot['sha256']:
                    continue
                if len(captured) >= 50:
                    break
                try:
                    raw = _read_regular(self._workspace_file(name), FILE_LIMIT)
                    text = raw.decode('utf-8')
                    if '\0' in text:
                        continue
                except (OSError, UnicodeError, ValueError):
                    continue
                artifact_id = uuid4().hex
                metadata = {'id': artifact_id, 'name': name, 'size': len(raw),
                            'change': 'modified' if isinstance(previous, dict) else 'created',
                            'created_at': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
                            'download_url': f'/api/artifacts/{artifact_id}/download',
                            'preview_url': f'/api/artifacts/{artifact_id}'}
                record = {'artifact': metadata, 'sha256': hashlib.sha256(raw).hexdigest()}
                if isinstance(previous, dict) and isinstance(previous.get('text'), str):
                    difference = ''.join(difflib.unified_diff(previous['text'].splitlines(keepends=True),
                                         text[:PREVIEW_LIMIT].splitlines(keepends=True),
                                         fromfile=name + ' (before)', tofile=name + ' (after)'))
                    record['diff'] = difference[:DIFF_LIMIT]
                    record['diff_truncated'] = (len(difference) > DIFF_LIMIT or previous.get('truncated', False)
                                                or len(text) > PREVIEW_LIMIT)
                self._write(self.directory / f'{artifact_id}.bin', raw)
                self._write(self.directory / f'{artifact_id}.json',
                            json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                captured.append(metadata)
        return captured

    def _record(self, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(r'[a-f0-9]{32}', artifact_id):
            raise ValueError('Invalid artifact identifier.')
        self._check_storage()
        path = self.directory / f'{artifact_id}.json'
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError('Artifact was not found.')
        try:
            record = json.loads(_read_regular(path, 128 * 1024))
            metadata = record['artifact']
            if (not isinstance(metadata, dict) or metadata.get('id') != artifact_id
                    or type(metadata.get('size')) is not int or not 0 <= metadata['size'] <= FILE_LIMIT
                    or not isinstance(record.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', record['sha256'])):
                raise ValueError
            # Stored names are display labels, never storage paths.
            name = metadata.get('name')
            if (not isinstance(name, str) or not name or len(name) > 2048 or '\\' in name
                    or PurePosixPath(name).is_absolute()
                    or any(part.startswith('.') or ':' in part for part in PurePosixPath(name).parts)
                    or any(ord(char) < 32 for char in name)):
                raise ValueError
            safe = {key: metadata[key] for key in ('id', 'name', 'size', 'change', 'created_at', 'run_id')}
            if safe['change'] not in ('created', 'modified'):
                raise ValueError
            if any(not isinstance(safe[key], str) for key in ('created_at', 'run_id')):
                raise ValueError
            safe.update(download_url=f'/api/artifacts/{artifact_id}/download',
                        preview_url=f'/api/artifacts/{artifact_id}')
            record['artifact'] = safe
            return record
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            raise ValueError('Artifact metadata is invalid.') from None

    def get(self, artifact_id):
        with self.lock:
            record = self._record(artifact_id)
            path = self.directory / f'{artifact_id}.bin'
            if not path.exists() and not path.is_symlink():
                raise FileNotFoundError('Artifact content was not found.')
            raw = _read_regular(path, FILE_LIMIT)
            if (len(raw) != record['artifact']['size']
                    or hashlib.sha256(raw).hexdigest() != record['sha256']):
                raise ValueError('Artifact content no longer matches its saved snapshot.')
            return record['artifact'], raw

    def preview(self, artifact_id):
        with self.lock:
            metadata, raw = self.get(artifact_id)
            record = self._record(artifact_id)
            try:
                text = raw.decode('utf-8')
            except UnicodeError:
                raise ValueError('Artifact content is not UTF-8 text.') from None
            result = {'artifact': metadata, 'content': text[:PREVIEW_LIMIT],
                      'truncated': len(text) > PREVIEW_LIMIT}
            if isinstance(record.get('diff'), str):
                result['diff'] = record['diff'][:DIFF_LIMIT]
                result['diff_truncated'] = bool(record.get('diff_truncated'))
            if Path(metadata['name']).suffix.lower() == '.csv':
                try:
                    reader = csv.reader(io.StringIO(text))
                    result['columns'] = [cell[:2000] for cell in next(reader, [])[:100]]
                    result['rows'] = []
                    for _, row in zip(range(30), reader):
                        result['rows'].append([cell[:2000] for cell in row[:100]])
                except csv.Error:
                    result.pop('columns', None)
                    result.pop('rows', None)
            return result
