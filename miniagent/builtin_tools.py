"""Small read-only tools; no eval, shell, or arbitrary imports."""

from __future__ import annotations

import ast
import math
import operator
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


class CalculateArgs(ToolArgs):
    expression: str = Field(min_length=1, max_length=200)


def calculate(expression: str) -> float:
    tree = ast.parse(expression, mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError('Expression is too complex.')
    binary = {ast.Add: operator.add, ast.Sub: operator.sub,
              ast.Mult: operator.mul, ast.Div: operator.truediv}
    unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = float(node.value)
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            value = binary[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and type(node.op) in unary:
            value = unary[type(node.op)](visit(node.operand))
        else:
            raise ValueError('Only numbers, parentheses, +, -, *, and / are supported.')
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError('Number exceeds the supported range.')
        return value

    return visit(tree.body)


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
        Tool('calculate', 'Evaluate arithmetic using numbers, parentheses, +, -, *, /. '
             'For example: (200 + 100) * 0.9. Returns a floating-point number.',
             CalculateArgs, calculate, read_only=True),
    ]
