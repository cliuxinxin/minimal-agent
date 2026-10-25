"""Identify a loaded server so Start never silently reuses outdated code."""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import __version__


def runtime_identity(root: Path) -> dict:
    root = Path(root).resolve()
    revision = hashlib.sha256()
    code = list((root / 'miniagent').rglob('*.py'))
    launcher = root / 'launcher.py'
    if launcher.is_file():
        code.append(launcher)
    for file in sorted(code, key=lambda path: path.relative_to(root).as_posix()):
        if not file.is_file():
            continue
        relative = file.relative_to(root).as_posix().encode('utf-8')
        content = file.read_bytes()
        revision.update(len(relative).to_bytes(8, 'big'))
        revision.update(relative)
        revision.update(len(content).to_bytes(8, 'big'))
        revision.update(content)
    return {
        'app': 'minimal-agent-web',
        'version': __version__,
        'workspace_id': hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:12],
        'source_revision': revision.hexdigest(),
    }
