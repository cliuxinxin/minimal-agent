"""Standard OpenAI Chat Completions and Anthropic Messages tool calling."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol


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


class OpenAIModel:
    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        _validate_credentials(model, api_key)
        self.model = model
        self._api_key = api_key
        self.endpoint = _endpoint(base_url if base_url is not None else "https://api.openai.com/v1", "/chat/completions")

    def complete(self, messages: list[dict], tools: list[dict], system_prompt: str) -> ModelReply:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_completion_tokens": 4096,
            "stream": False,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["parameters"]}} for tool in tools]
        payload = _post(self.endpoint, {"Authorization": f"Bearer {self._api_key}"}, body)
        try:
            choice = payload["choices"][0]
            reason = choice["finish_reason"]
            message = choice["message"]
            content = message.get("content")
            raw_calls = message.get("tool_calls")
            if raw_calls is None:
                raw_calls = []
            if content is not None and not isinstance(content, str):
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
        if normalized_calls:
            assistant["tool_calls"] = normalized_calls
        return ModelReply(content or "", calls, assistant)

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
        body = {"model": self.model, "system": system_prompt, "messages": messages, "max_tokens": 4096}
        if tools:
            body["tools"] = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]} for tool in tools]
        payload = _post(self.endpoint, {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}, body)
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

    def tool_messages(self, results: list[dict]) -> list[dict]:
        if not results:
            return []
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": result["id"], "content": json.dumps(result["payload"], ensure_ascii=False), "is_error": not result["payload"]["ok"]}
            for result in results
        ]}]
