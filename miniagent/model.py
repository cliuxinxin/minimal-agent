"""Standard OpenAI Chat Completions and Anthropic Messages tool calling."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from .sse import SSECancelled, SSEError, open_sse


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    message: dict


class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict], system_prompt: str) -> ModelReply: ...

    def tool_messages(self, results: list[dict]) -> list[dict]: ...


class ModelError(Exception):
    """The provider failed or returned an incomplete or malformed response."""

    def __init__(self, message: str, *, diagnostic: dict | None = None):
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


class ModelCancelled(ModelError):
    """The caller stopped an in-flight provider request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials must never be forwarded to another endpoint.
        return None


def _endpoint(base_url: str, suffix: str) -> str:
    try:
        parts = urllib.parse.urlsplit(base_url)
        valid = (
            parts.scheme in {"http", "https"} and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and "?" not in base_url and "#" not in base_url
            and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in base_url)
        )
        parts.port
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("base_url must be an HTTP(S) URL without credentials, query, or fragment")
    base_url = base_url.rstrip("/")
    return base_url if base_url.endswith(suffix) else base_url + suffix


def _validate_credentials(model: str, api_key: str) -> None:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a nonempty string")
    if not isinstance(api_key, str) or not api_key.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key):
        raise ValueError("api_key must be a nonempty string without control characters")


def _post(endpoint: str, headers: dict, body: dict) -> dict:
    """One bounded request, with no retries and no provider error-body logging."""
    limit = 2 * 1024 * 1024
    try:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json", "Accept": "application/json", **headers}, method="POST")
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(limit)
    except urllib.error.HTTPError as error:
        error.close()
        raise ModelError(f"Model endpoint returned HTTP {error.code}") from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        raise ModelError("Model request failed or timed out") from None
    except (TypeError, ValueError, UnicodeError):
        raise ModelError("Model request could not be encoded") from None
    if len(raw) >= limit:
        raise ModelError("Model response exceeded the size limit")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ModelError("Model response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise ModelError("Model response must be an object")
    return payload


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@contextmanager
def _stream_events(endpoint, headers, body, should_stop, *, diagnostic=None):
    diagnostic = {} if diagnostic is None else diagnostic
    try:
        with open_sse(endpoint, headers, body, should_stop, metadata=diagnostic) as events:
            yield events
    except SSECancelled:
        raise ModelCancelled("Model request cancelled", diagnostic={**diagnostic, "code": "cancelled", "phase": "transport"}) from None
    except SSEError as error:
        raise ModelError(str(error), diagnostic={**diagnostic, **error.diagnostic}) from None
    except ModelError as error:
        error.diagnostic = {**diagnostic, **error.diagnostic}
        raise


_MISSING = object()


def _value_type(value):
    if value is _MISSING:
        return "missing"
    return {type(None): "null", bool: "boolean", int: "integer", float: "number",
            str: "string", list: "array", dict: "object"}.get(type(value), "unknown")


def _chunk_shape(payload):
    """Bounded structural diagnostics; never retain keys or values from content."""
    keys = {"choices", "delta", "index", "finish_reason", "content", "reasoning_content",
            "tool_calls", "id", "type", "function", "name", "arguments", "role", "refusal",
            "error", "message", "usage", "object", "content_block", "partial_json",
            "signature", "thinking", "input", "stop_reason", "data", "text"}
    opaque = {"content", "reasoning_content", "arguments", "refusal", "error", "partial_json",
              "signature", "thinking", "input", "data", "text"}
    remaining = 64

    def describe(value, depth=0, inspect=True):
        nonlocal remaining
        result = {"type": _value_type(value)}
        if isinstance(value, (dict, list, str)):
            result["length"] = len(value)
        remaining -= 1
        if not inspect or depth >= 8 or remaining <= 0:
            return result
        if isinstance(value, dict):
            fields = {}
            for key in value:
                if key in keys and remaining > 0:
                    fields[key] = describe(value[key], depth + 1, key not in opaque)
            if fields:
                result["fields"] = fields
            if len(value) > len(fields):
                result["omitted_fields"] = len(value) - len(fields)
        elif isinstance(value, list):
            result["items"] = [describe(item, depth + 1) for item in value[:3] if remaining > 0]
        return result

    return describe(payload)


class _StreamFieldError(ValueError):
    def __init__(self, field, expected, value, *, code="invalid_stream_field"):
        super().__init__()
        self.diagnostic = {"code": code, "field": field, "expected": expected, "actual_type": _value_type(value)}


def _typed(value, kind, field, expected=None):
    if not isinstance(value, kind):
        raise _StreamFieldError(field, expected or {dict: "object", list: "array", str: "string"}[kind], value)
    return value


def _finish_reason(value):
    known = {"stop", "tool_calls", "length", "content_filter", "function_call", "end_turn",
             "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"}
    return value if value is None or isinstance(value, str) and value in known else "unknown"


def _json_object(data: str) -> dict:
    def invalid_constant(_value):
        raise ValueError

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        payload = json.loads(data, parse_constant=invalid_constant, object_pairs_hook=unique_object)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ModelError("Model stream contained invalid JSON", diagnostic={"code": "invalid_stream_json", "phase": "stream_decode"}) from None
    return payload


def _stream_payload(data: str) -> dict:
    payload = _json_object(data)
    if payload.get("error") or payload.get("type") == "error":
        raise ModelError("Model endpoint reported a streaming error", diagnostic={"code": "provider_stream_error", "phase": "stream_event", "chunk_shape": _chunk_shape(payload)})
    return payload


def _index(value, field=None):
    if type(value) is not int or not 0 <= value < 1024:
        if field is not None:
            raise _StreamFieldError(field, "integer from 0 to 1023", value)
        raise ValueError
    return value


def _string(value, field=None, expected="string"):
    if not isinstance(value, str):
        if field is not None:
            raise _StreamFieldError(field, expected, value)
        raise ValueError
    return value


class OpenAIModel:
    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        _validate_credentials(model, api_key)
        self.model = model
        self._api_key = api_key
        self.endpoint = _endpoint(base_url if base_url is not None else "https://api.openai.com/v1", "/chat/completions")

    def complete(self, messages: list[dict], tools: list[dict], system_prompt: str) -> ModelReply:
        body = self._body(messages, tools, system_prompt)
        payload = _post(self.endpoint, {"Authorization": f"Bearer {self._api_key}"}, body)
        return self._reply(payload)

    def _body(self, messages, tools, system_prompt, *, stream=False):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_completion_tokens": 4096,
            "stream": stream,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["parameters"]}} for tool in tools]
        return body

    @staticmethod
    def _reply(payload):
        try:
            choice = payload["choices"][0]
            reason = choice["finish_reason"]
            message = choice["message"]
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            raw_calls = message.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if content is not None and not isinstance(content, str):
                raise ValueError
            if reasoning is not None and not isinstance(reasoning, str):
                raise ValueError
            if not isinstance(raw_calls, list):
                raise ValueError
            calls = []
            normalized_calls = []
            for call in raw_calls:
                function = call["function"]
                if call["type"] != "function" or not _nonempty(call["id"]) or not _nonempty(function["name"]) or not isinstance(function["arguments"], str):
                    raise ValueError
                calls.append(ToolCall(call["id"], function["name"], function["arguments"]))
                normalized_calls.append({"id": call["id"], "type": "function", "function": {"name": function["name"], "arguments": function["arguments"]}})
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ModelError("OpenAI response contained invalid message or tool-call fields") from None
        if not ((reason == "stop" and not calls and _nonempty(content)) or (reason == "tool_calls" and calls)):
            raise ModelError("OpenAI response did not finish with complete text or tool calls")
        assistant = {"role": "assistant", "content": content}
        if "reasoning_content" in message:
            # Compatible providers need this exact returned field on later turns.
            assistant["reasoning_content"] = reasoning
        if normalized_calls:
            assistant["tool_calls"] = normalized_calls
        return ModelReply(content or "", calls, assistant)

    def stream(self, messages: list[dict], tools: list[dict], system_prompt: str, *, on_delta: Callable[[dict], None], should_stop: Callable[[], bool] | None = None) -> ModelReply:
        body = self._body(messages, tools, system_prompt, stream=True)
        message = {"role": "assistant", "content": None}
        calls: dict[int, dict] = {}
        reason = None
        terminal = False
        diagnostic = {"phase": "stream_delta", "chunk_index": 0, "finish_reason": None}
        with _stream_events(self.endpoint, {"Authorization": f"Bearer {self._api_key}"}, body, should_stop, diagnostic=diagnostic) as events:
            for chunk_index, event in enumerate(events, 1):
                diagnostic["chunk_index"] = chunk_index
                if event.data.strip() == "[DONE]":
                    terminal = True
                    break
                diagnostic["event_type"] = "chat.completion.chunk"
                diagnostic.pop("chunk_shape", None)
                payload = _stream_payload(event.data)
                diagnostic["chunk_shape"] = _chunk_shape(payload)
                try:
                    choices = _typed(payload.get("choices", _MISSING), list, "choices")
                    for choice_position, choice in enumerate(choices):
                        choice_path = f"choices[{choice_position}]"
                        _typed(choice, dict, choice_path)
                        if _index(choice.get("index", _MISSING), f"{choice_path}.index") != 0:
                            continue
                        finish = choice.get("finish_reason")
                        if finish is not None:
                            diagnostic["finish_reason"] = _finish_reason(finish)
                        delta_path = f"{choice_path}.delta"
                        delta = _typed(choice.get("delta", _MISSING), dict, delta_path)
                        if reason is not None and delta:
                            raise _StreamFieldError(delta_path, "empty delta after finish_reason", delta, code="data_after_finish")
                        for field, event_type in (("content", "text_delta"), ("reasoning_content", "reasoning_delta")):
                            value = delta.get(field)
                            if value is not None:
                                value = _string(value, f"{delta_path}.{field}", "string or null")
                                message[field] = (message.get(field) or "") + value
                                if value:
                                    on_delta({"type": event_type, "text": value})
                        fragments = delta.get("tool_calls")
                        if fragments is None:
                            fragments = []
                        _typed(fragments, list, f"{delta_path}.tool_calls", "array or null")
                        for fragment_position, fragment in enumerate(fragments):
                            fragment_path = f"{delta_path}.tool_calls[{fragment_position}]"
                            _typed(fragment, dict, fragment_path)
                            index = _index(fragment.get("index", _MISSING), f"{fragment_path}.index")
                            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            # Stream fields are optional/nullable: null means no
                            # update, not a null value in the completed tool call.
                            if fragment.get("type") not in (None, "function"):
                                raise _StreamFieldError(f"{fragment_path}.type", "function or null", fragment["type"])
                            if fragment.get("id") is not None:
                                call["id"] += _string(fragment["id"], f"{fragment_path}.id", "string or null")
                            function = fragment.get("function")
                            if function is None:
                                function = {}
                            _typed(function, dict, f"{fragment_path}.function", "object or null")
                            if function.get("name") is not None:
                                call["function"]["name"] += _string(function["name"], f"{fragment_path}.function.name", "string or null")
                            arguments = function.get("arguments")
                            arguments = "" if arguments is None else _string(arguments, f"{fragment_path}.function.arguments", "string or null")
                            call["function"]["arguments"] += arguments
                            on_delta({"type": "tool_call_delta", "index": index, "id": call["id"], "name": call["function"]["name"], "arguments_delta": arguments})
                        if finish is not None:
                            _string(finish, f"{choice_path}.finish_reason", "string or null")
                            if reason is not None and reason != finish:
                                raise _StreamFieldError(f"{choice_path}.finish_reason", "unchanged finish_reason", finish, code="conflicting_finish_reason")
                            reason = finish
                except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
                    details = getattr(error, "diagnostic", {"code": "invalid_stream_field"})
                    raise ModelError("OpenAI stream contained invalid message or tool-call fields", diagnostic=details) from None
        if not terminal:
            raise ModelError("OpenAI stream ended before its terminal marker", diagnostic={**diagnostic, "code": "missing_terminal_marker", "phase": "stream_finish"})
        if calls:
            for index, call in calls.items():
                try:
                    _json_object(call["function"]["arguments"])
                except ModelError as error:
                    error.diagnostic = {**diagnostic, "code": "invalid_tool_arguments", "phase": "tool_arguments", "field": f"tool_calls[{index}].function.arguments", "expected": "JSON object with unique keys", "actual_type": "string"}
                    raise
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        try:
            return self._reply({"choices": [{"message": message, "finish_reason": reason}]})
        except ModelError as error:
            error.diagnostic = {**diagnostic, "code": "incomplete_response", "phase": "stream_finish"}
            raise

    def tool_messages(self, results: list[dict]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": result["id"], "content": json.dumps(result["payload"], ensure_ascii=False)} for result in results]


class AnthropicModel:
    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        _validate_credentials(model, api_key)
        self.model = model
        self._api_key = api_key
        base = base_url if base_url is not None else "https://api.anthropic.com"
        suffix = "/messages" if isinstance(base, str) and base.rstrip("/").endswith("/v1") else "/v1/messages"
        self.endpoint = _endpoint(base, suffix)

    def complete(self, messages: list[dict], tools: list[dict], system_prompt: str) -> ModelReply:
        body = self._body(messages, tools, system_prompt)
        payload = _post(self.endpoint, {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}, body)
        return self._reply(payload)

    def _body(self, messages, tools, system_prompt, *, stream=False):
        body = {"model": self.model, "system": system_prompt, "messages": messages, "max_tokens": 4096}
        if stream:
            body["stream"] = True
        if tools:
            body["tools"] = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]} for tool in tools]
        return body

    @staticmethod
    def _reply(payload):
        try:
            reason = payload["stop_reason"]
            blocks = payload["content"]
            if not isinstance(blocks, list):
                raise ValueError
            texts = []
            calls = []
            for block in blocks:
                block_type = block["type"]
                if block_type == "text":
                    if not isinstance(block["text"], str):
                        raise ValueError
                    texts.append(block["text"])
                elif block_type == "tool_use":
                    if not _nonempty(block["id"]) or not _nonempty(block["name"]) or not isinstance(block["input"], dict):
                        raise ValueError
                    calls.append(ToolCall(block["id"], block["name"], json.dumps(block["input"], ensure_ascii=False, allow_nan=False)))
        except (KeyError, TypeError, ValueError):
            raise ModelError("Anthropic response contained invalid content or tool-use fields") from None
        text = "\n".join(texts)
        if not ((reason == "end_turn" and not calls and _nonempty(text)) or (reason == "tool_use" and calls)):
            raise ModelError("Anthropic response did not finish with complete text or tool use")
        # Replay every original block, including opaque thinking signatures.
        return ModelReply(text, calls, {"role": "assistant", "content": blocks})

    def stream(self, messages: list[dict], tools: list[dict], system_prompt: str, *, on_delta: Callable[[dict], None], should_stop: Callable[[], bool] | None = None) -> ModelReply:
        body = self._body(messages, tools, system_prompt, stream=True)
        blocks: dict[int, dict] = {}
        arguments: dict[int, str] = {}
        closed = set()
        started = terminal = False
        reason = None
        headers = {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}
        diagnostic = {"phase": "stream_event", "chunk_index": 0, "finish_reason": None}
        known_events = {"message_start", "content_block_start", "content_block_delta", "content_block_stop",
                        "message_delta", "message_stop", "ping", "error"}
        with _stream_events(self.endpoint, headers, body, should_stop, diagnostic=diagnostic) as events:
            for chunk_index, event in enumerate(events, 1):
                diagnostic["chunk_index"] = chunk_index
                diagnostic["event_type"] = event.event if event.event in known_events else "unknown"
                diagnostic.pop("chunk_shape", None)
                payload = _stream_payload(event.data)
                diagnostic["chunk_shape"] = _chunk_shape(payload)
                try:
                    kind = payload["type"]
                    diagnostic["event_type"] = kind if isinstance(kind, str) and kind in known_events else "unknown"
                    if kind == "message_start":
                        if started or payload["message"].get("content") != []:
                            raise ValueError
                        started = True
                    elif kind in {"content_block_start", "content_block_delta", "content_block_stop"}:
                        if not started or reason is not None:
                            raise ValueError
                        index = _index(payload["index"])
                        if kind == "content_block_start":
                            if index in blocks:
                                raise ValueError
                            block = payload["content_block"]
                            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                                raise ValueError
                            block = dict(block)
                            blocks[index] = block
                            if block["type"] == "text":
                                value = _string(block.get("text", ""))
                                if any(i != index and b["type"] == "text" for i, b in blocks.items()):
                                    on_delta({"type": "text_delta", "text": "\n"})
                                if value:
                                    on_delta({"type": "text_delta", "text": value})
                            elif block["type"] == "thinking":
                                value = _string(block.get("thinking", ""))
                                _string(block.get("signature", ""))
                                if value:
                                    on_delta({"type": "reasoning_delta", "text": value})
                            elif block["type"] == "tool_use":
                                if not _nonempty(block.get("id")) or not _nonempty(block.get("name")) or not isinstance(block.get("input"), dict):
                                    raise ValueError
                                initial = json.dumps(block["input"], ensure_ascii=False, allow_nan=False) if block["input"] else ""
                                on_delta({"type": "tool_call_delta", "index": index, "id": block["id"], "name": block["name"], "arguments_delta": initial})
                        elif kind == "content_block_delta":
                            if index not in blocks or index in closed:
                                raise ValueError
                            block = blocks[index]
                            delta = payload["delta"]
                            delta_type = delta["type"]
                            if delta_type in {"text_delta", "thinking_delta", "signature_delta"}:
                                field, expected = {"text_delta": ("text", "text"), "thinking_delta": ("thinking", "thinking"), "signature_delta": ("signature", "thinking")}[delta_type]
                                if block["type"] != expected:
                                    raise ValueError
                                value = _string(delta[field])
                                block[field] = block.get(field, "") + value
                                if value and delta_type != "signature_delta":
                                    on_delta({"type": "text_delta" if field == "text" else "reasoning_delta", "text": value})
                            elif delta_type == "input_json_delta":
                                if block["type"] != "tool_use" or block["input"]:
                                    raise ValueError
                                value = _string(delta["partial_json"])
                                arguments[index] = arguments.get(index, "") + value
                                on_delta({"type": "tool_call_delta", "index": index, "id": block["id"], "name": block["name"], "arguments_delta": value})
                            elif delta_type == "citations_delta" and block["type"] == "text":
                                citation = delta["citation"]
                                if not isinstance(citation, dict):
                                    raise ValueError
                                block.setdefault("citations", []).append(citation)
                        else:
                            if index not in blocks or index in closed:
                                raise ValueError
                            if index in arguments:
                                blocks[index]["input"] = _json_object(arguments[index])
                            closed.add(index)
                    elif kind == "message_delta":
                        if not started or len(closed) != len(blocks):
                            raise ValueError
                        finish = payload["delta"].get("stop_reason")
                        if finish is not None:
                            diagnostic["finish_reason"] = _finish_reason(finish)
                            reason = _string(finish)
                    elif kind == "message_stop":
                        if not started or len(closed) != len(blocks):
                            raise ValueError
                        terminal = True
                        break
                    # ping and future event types carry no local tool side effects.
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    raise ModelError("Anthropic stream contained invalid content or tool-use fields", diagnostic={"code": "invalid_stream_field"}) from None
        if not terminal:
            raise ModelError("Anthropic stream ended before its terminal marker", diagnostic={**diagnostic, "code": "missing_terminal_marker", "phase": "stream_finish"})
        try:
            return self._reply({"content": [blocks[index] for index in sorted(blocks)], "stop_reason": reason})
        except ModelError as error:
            error.diagnostic = {**diagnostic, "code": "incomplete_response", "phase": "stream_finish"}
            raise

    def tool_messages(self, results: list[dict]) -> list[dict]:
        if not results:
            return []
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": result["id"], "content": json.dumps(result["payload"], ensure_ascii=False), "is_error": not result["payload"]["ok"]}
            for result in results
        ]}]
