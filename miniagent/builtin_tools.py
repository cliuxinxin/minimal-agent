"""Small read-only workspace tools."""

from __future__ import annotations

from itertools import islice
from pathlib import Path

from pydantic import Field

from .tools import Tool, ToolArgs


class ListArgs(ToolArgs):
    path: str = '.'


class ReadArgs(ToolArgs):
    path: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=4000, ge=1, le=10_000)


def build_tools(workspace: Path) -> list[Tool]:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError('Workspace must be an existing directory.')

    def resolve(path: str) -> Path:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError('Path must stay inside the configured workspace.')
        return target

    def list_files(path: str = '.') -> dict:
        target = resolve(path)
        entries = list(islice((p for p in target.iterdir() if not p.name.startswith('.')), 101))
        visible = [p for p in entries[:100] if p.resolve().is_relative_to(root)]
        return {'entries': [{'path': p.relative_to(root).as_posix(), 'is_dir': p.is_dir()}
                            for p in sorted(visible, key=lambda p: p.name)],
                'truncated': len(entries) > 100}

    def read_file(path: str, offset: int = 0, limit: int = 4000) -> dict:
        target = resolve(path)
        with target.open(encoding='utf-8') as handle:
            handle.read(offset)
            text = handle.read(limit + 1)
        more = len(text) > limit
        return {'path': target.relative_to(root).as_posix(), 'text': text[:limit],
                'next_offset': offset + limit if more else None, 'truncated': more}

    return [
        Tool('list_files', 'List up to 100 visible entries in a workspace directory.',
             ListArgs, list_files, read_only=True),
        Tool('read_file', 'Read a UTF-8 workspace file. Paths are relative to workspace. '
             'offset/limit count characters; use next_offset to read the next segment.',
             ReadArgs, read_file, read_only=True),
    ]
