"""Small, content-free failure reports suitable for copying from the browser."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile


_TEXT_FIELDS = {
    'type', 'code', 'phase', 'field', 'expected', 'actual_type', 'event_type',
    'finish_reason', 'request_id',
}
_NUMBER_FIELDS = {'chunk_index', 'http_status', 'elapsed_ms'}


def _bounded_structure(value, depth=0):
    """Bound adapter-produced shape metadata, never a native response body."""
    if depth > 9:
        return '[depth limit]'
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_bounded_structure(item, depth + 1) for item in value[:16]]
    if isinstance(value, dict):
        return {key[:120]: _bounded_structure(item, depth + 1)
                for key, item in list(value.items())[:24] if isinstance(key, str)}
    return type(value).__name__


def diagnostic_fields(value):
    """Accept only the diagnostic contract; discard headers and raw payloads."""
    if not isinstance(value, dict):
        return {}
    safe = {key: item[:1000] for key, item in value.items()
            if key in _TEXT_FIELDS and isinstance(item, str)}
    for key in _NUMBER_FIELDS:
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item):
            safe[key] = item
    if isinstance(value.get('chunk_shape'), dict):
        shape = _bounded_structure(value['chunk_shape'])
        if len(json.dumps(shape, ensure_ascii=False)) <= 12000:
            safe['chunk_shape'] = shape
        else:
            safe['chunk_shape'] = {'truncated': True}
    if isinstance(value.get('stack'), list):
        safe['stack'] = [
            {key: frame[key] for key in ('file', 'function', 'line') if key in frame}
            for frame in value['stack'][-8:] if isinstance(frame, dict)
        ]
    return safe


def exception_diagnostic(error, *, phase):
    """Keep code locations without local variables, source lines or full paths."""
    detail = diagnostic_fields(getattr(error, 'diagnostic', None))
    detail.setdefault('code', 'unexpected_error')
    detail.setdefault('phase', phase)
    detail['type'] = type(error).__name__
    frames, trace = [], error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        frames.append({'file': Path(code.co_filename).name,
                       'function': code.co_name, 'line': trace.tb_lineno})
        trace = trace.tb_next
    if frames:
        detail['stack'] = frames[-8:]
    return detail


def write_diagnostic(root, report):
    """Atomically persist the same report the browser can copy or download."""
    target = Path(root) / report['diagnostic_file']
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix='.diagnostic-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write('\n')
        os.replace(filename, target)
    finally:
        Path(filename).unlink(missing_ok=True)
