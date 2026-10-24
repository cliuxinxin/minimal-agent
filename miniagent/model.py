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
def _stream_events(endpoint, headers, body, should_stop):
    try:
        with open_sse(endpoint, headers, body, should_stop) as events:
            yield events
    except SSECancelled:
        raise ModelCancelled("Model request cancelled") from None
    except SSEError as error:
        raise ModelError(str(error)) from None


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
        raise ModelError("Model stream contained invalid JSON") from None
    return payload


def _stream_payload(data: str) -> dict:
    payload = _json_object(data)
    if payload.get("error") or payload.get("type") == "error":
        raise ModelError("Model endpoint reported a streaming error")
    return payload


def _index(value):
    if type(value) is not int or not 0 <= value < 1024:
        raise ValueError
    return value


def _string(value):
    if not isinstance(value, str):
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
        with _stream_events(self.endpoint, {"Authorization": f"Bearer {self._api_key}"}, body, should_stop) as events:
            for event in events:
                if event.data.strip() == "[DONE]":
                    terminal = True
                    break
                payload = _stream_payload(event.data)
                try:
                    choices = payload["choices"]
                    if not isinstance(choices, list):
                        raise ValueError
                    for choice in choices:
                        if _index(choice["index"]) != 0:
                            continue
                        delta = choice["delta"]
                        if not isinstance(delta, dict):
                            raise ValueError
                        if reason is not None and delta:
                            raise ValueError
                        for field, event_type in (("content", "text_delta"), ("reasoning_content", "reasoning_delta")):
                            value = delta.get(field)
                            if value is not None:
                                value = _string(value)
                                message[field] = (message.get(field) or "") + value
                                if value:
                                    on_delta({"type": event_type, "text": value})
                        fragments = delta.get("tool_calls")
                        if fragments is None:
                            fragments = []
                        if not isinstance(fragments, list):
                            raise ValueError
                        for fragment in fragments:
                            index = _index(fragment["index"])
                            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if fragment.get("type", "function") != "function":
                                raise ValueError
                            if "id" in fragment:
                                call["id"] += _string(fragment["id"])
                            function = fragment.get("function", {})
                            if not isinstance(function, dict):
                                raise ValueError
                            if "name" in function:
                                call["function"]["name"] += _string(function["name"])
                            arguments = _string(function.get("arguments", ""))
                            call["function"]["arguments"] += arguments
                            on_delta({"type": "tool_call_delta", "index": index, "id": call["id"], "name": call["function"]["name"], "arguments_delta": arguments})
                        finish = choice.get("finish_reason")
                        if finish is not None:
                            if reason is not None and reason != finish:
                                raise ValueError
                            reason = _string(finish)
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    raise ModelError("OpenAI stream contained invalid message or tool-call fields") from None
        if not terminal:
            raise ModelError("OpenAI stream ended before its terminal marker")
        if calls:
            for call in calls.values():
                _json_object(call["function"]["arguments"])
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return self._reply({"choices": [{"message": message, "finish_reason": reason}]})

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
        with _stream_events(self.endpoint, headers, body, should_stop) as events:
            for event in events:
                payload = _stream_payload(event.data)
                try:
                    kind = payload["type"]
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
                            reason = _string(finish)
                    elif kind == "message_stop":
                        if not started or len(closed) != len(blocks):
                            raise ValueError
                        terminal = True
                        break
                    # ping and future event types carry no local tool side effects.
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    raise ModelError("Anthropic stream contained invalid content or tool-use fields") from None
        if not terminal:
            raise ModelError("Anthropic stream ended before its terminal marker")
        return self._reply({"content": [blocks[index] for index in sorted(blocks)], "stop_reason": reason})

    def tool_messages(self, results: list[dict]) -> list[dict]:
        if not results:
            return []
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": result["id"], "content": json.dumps(result["payload"], ensure_ascii=False), "is_error": not result["payload"]["ok"]}
            for result in results
        ]}]
