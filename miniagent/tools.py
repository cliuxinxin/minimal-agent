"""Typed tool contracts and the boundary before side effects."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError


_STOP_CHECK: ContextVar[Callable[[], bool] | None] = ContextVar('tool_stop_check', default=None)


def execution_stopped() -> bool:
    """Let a running tool observe cancellation without sharing state across sessions."""
    check = _STOP_CHECK.get()
    return bool(check()) if check is not None else False


@contextmanager
def tool_execution_context(should_stop: Callable[[], bool]):
    token = _STOP_CHECK.set(should_stop)
    try:
        yield
    finally:
        _STOP_CHECK.reset(token)


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


def failure(code: str, message: str) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': message[:1000]}}


def validation_message(error: ValidationError) -> str:
    return '; '.join(
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    )[:1000]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_schema: type[ToolArgs]
    handler: Callable[..., Any]
    read_only: bool = False

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', self.name):
            raise ValueError('Tool name must be a short Python-style identifier.')
        if not issubclass(self.args_schema, ToolArgs):
            raise TypeError('Tool arguments must inherit ToolArgs.')
        if not callable(self.handler):
            raise TypeError('Tool handler must be callable.')

    def definition(self) -> dict:
        return {'name': self.name, 'description': self.description,
                'parameters': self.args_schema.model_json_schema(),
                'read_only': self.read_only}

    def execute(self, arguments: dict, approve: Callable | None = None) -> dict:
        try:
            validated = self.args_schema.model_validate(arguments).model_dump()
        except ValidationError as error:
            return failure('invalid_arguments', validation_message(error))
        if not self.read_only:
            if approve is None:
                return failure('approval_required', 'This tool needs host approval before execution.')
            try:
                permitted = approve(self, validated) is True
            except Exception:
                return failure('approval_failed', 'The host approval check failed; no action was taken.')
            if not permitted:
                return failure('permission_denied', 'The host denied this action. Do not repeat it unchanged.')
        try:
            result = self.handler(**validated)
        except Exception as error:
            return failure('tool_error', f'{type(error).__name__}: {error}')
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return failure('invalid_tool_result',
                           'The handler ran but returned non-JSON data. Do not repeat a write automatically.')
        return {'ok': True, 'result': result}
