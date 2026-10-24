"""Provider protocol tests at the urllib boundary; no live network or keys."""
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from miniagent.model import AnthropicModel, ModelError, ModelReply, OpenAIModel, ToolCall


TOOLS = [{"name": "read_file", "description": "Read text", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, "read_only": True}]
RESULTS = [
    {"id": "call_1", "name": "read_file", "payload": {"ok": True, "result": "hello"}},
    {"id": "call_2", "name": "read_file", "payload": {"ok": False, "error": "missing"}},
]


class ModelTests(unittest.TestCase):
    def openai_body(self, content="done", calls=None, reason="stop"):
        message = {"role": "assistant", "content": content}
        if calls is not None:
            message["tool_calls"] = calls
        return {"choices": [{"message": message, "finish_reason": reason}]}

    def anthropic_body(self, blocks=None, reason="end_turn"):
        return {"role": "assistant", "content": blocks if blocks is not None else [{"type": "text", "text": "done"}], "stop_reason": reason}

    def complete(self, model, body=None, raw=None, tools=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw if raw is not None else json.dumps(body).encode()
        with patch("miniagent.model.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            reply = model.complete([{"role": "user", "content": "hi"}], TOOLS if tools is None else tools, "Be helpful")
            return reply, build, response

    def test_openai_request_and_final_response(self):
        reply, build, _ = self.complete(OpenAIModel("gpt-test", "secret"), self.openai_body())
        self.assertEqual(reply, ModelReply("done", [], {"role": "assistant", "content": "done"}))
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 60)
        self.assertEqual(json.loads(request.data), {
            "model": "gpt-test", "messages": [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "hi"}],
            "max_completion_tokens": 4096, "stream": False,
            "tools": [{"type": "function", "function": {"name": "read_file", "description": "Read text", "parameters": TOOLS[0]["parameters"]}}],
        })

    def test_openai_multiple_calls_preserve_invalid_argument_json(self):
        calls = [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "not-json"}},
        ]
        reply, _, _ = self.complete(OpenAIModel("m", "k"), self.openai_body(None, calls, "tool_calls"))
        self.assertEqual(reply.tool_calls, [ToolCall("call_1", "read_file", '{"path":"a"}'), ToolCall("call_2", "read_file", "not-json")])
        self.assertEqual(reply.text, "")
        self.assertEqual(reply.message, {"role": "assistant", "content": None, "tool_calls": calls})

    def test_openai_tool_results_are_individual_tool_messages(self):
        self.assertEqual(OpenAIModel("m", "k").tool_messages(RESULTS), [
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(RESULTS[0]["payload"], ensure_ascii=False)},
            {"role": "tool", "tool_call_id": "call_2", "content": json.dumps(RESULTS[1]["payload"], ensure_ascii=False)},
        ])

    def test_anthropic_request_and_final_response(self):
        reply, build, _ = self.complete(AnthropicModel("claude-test", "secret"), self.anthropic_body())
        self.assertEqual(reply.text, "done")
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual(reply.message, {"role": "assistant", "content": [{"type": "text", "text": "done"}]})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(json.loads(request.data), {
            "model": "claude-test", "system": "Be helpful", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4096,
            "tools": [{"name": "read_file", "description": "Read text", "input_schema": TOOLS[0]["parameters"]}],
        })

    def test_anthropic_multiple_calls_preserve_thinking_and_signatures(self):
        blocks = [
            {"type": "thinking", "thinking": "reasoning", "signature": "opaque-signature"},
            {"type": "text", "text": "Reading"},
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a"}},
            {"type": "tool_use", "id": "call_2", "name": "read_file", "input": {"path": 7}},
        ]
        reply, _, _ = self.complete(AnthropicModel("m", "k"), self.anthropic_body(blocks, "tool_use"))
        self.assertEqual(reply.tool_calls, [ToolCall("call_1", "read_file", '{"path": "a"}'), ToolCall("call_2", "read_file", '{"path": 7}')])
        self.assertEqual(reply.text, "Reading")
        self.assertEqual(reply.message, {"role": "assistant", "content": blocks})

    def test_anthropic_tool_results_form_one_user_message_with_error_flags(self):
        self.assertEqual(AnthropicModel("m", "k").tool_messages(RESULTS), [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": json.dumps(RESULTS[0]["payload"], ensure_ascii=False), "is_error": False},
            {"type": "tool_result", "tool_use_id": "call_2", "content": json.dumps(RESULTS[1]["payload"], ensure_ascii=False), "is_error": True},
        ]}])
        self.assertEqual(AnthropicModel("m", "k").tool_messages([]), [])

    def test_configured_base_paths_and_empty_tools(self):
        cases = [(OpenAIModel, "http://localhost:8000/v1/", "http://localhost:8000/v1/chat/completions", self.openai_body()),
                 (OpenAIModel, "https://gateway.test/custom/chat/completions/", "https://gateway.test/custom/chat/completions", self.openai_body()),
                 (AnthropicModel, "https://gateway.test/v1", "https://gateway.test/v1/messages", self.anthropic_body()),
                 (AnthropicModel, "https://gateway.test/v1/messages/", "https://gateway.test/v1/messages", self.anthropic_body())]
        for cls, base, expected, body in cases:
            with self.subTest(base=base):
                _, build, _ = self.complete(cls("m", "k", base), body, tools=[])
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, expected)
                self.assertNotIn("tools", json.loads(request.data))

    def test_invalid_configuration_is_clean(self):
        for cls in (OpenAIModel, AnthropicModel):
            for url in ("ftp://example.test", "https://u:secret@example.test", "https://example.test?secret", "https://example.test#secret", "example.test", "https://example.test/\nsecret"):
                with self.subTest(cls=cls, url=url), self.assertRaises(ValueError) as caught:
                    cls("m", "k", url)
                self.assertNotIn("secret", str(caught.exception))

    def test_bad_json_wrong_shapes_and_size_are_rejected(self):
        for cls in (OpenAIModel, AnthropicModel):
            for raw in (b"not-json", b"\xff", b"null", b"[]", b"{}", b" " * (2 * 1024 * 1024)):
                with self.subTest(cls=cls, size=len(raw)), self.assertRaises(ModelError):
                    self.complete(cls("m", "secret"), raw=raw)
        _, _, response = self.complete(OpenAIModel("m", "k"), self.openai_body())
        response.read.assert_called_once_with(2 * 1024 * 1024)

    def test_truncated_filtered_and_unknown_reasons_are_rejected(self):
        cases = [(OpenAIModel, self.openai_body(reason=reason)) for reason in ("length", "content_filter", None, "secret")]
        cases += [(AnthropicModel, self.anthropic_body(reason=reason)) for reason in ("max_tokens", "refusal", "pause_turn", None, "secret")]
        for cls, body in cases:
            with self.subTest(cls=cls, body=body), self.assertRaises(ModelError) as caught:
                self.complete(cls("m", "secret"), body)
            self.assertNotIn("secret", str(caught.exception))

    def test_finish_reason_must_match_content_or_tools(self):
        cases = [(OpenAIModel, self.openai_body(None)), (OpenAIModel, self.openai_body(" ")), (OpenAIModel, self.openai_body(None, [], "tool_calls")),
                 (OpenAIModel, self.openai_body("text", [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}], "stop")),
                 (AnthropicModel, self.anthropic_body([], "end_turn")), (AnthropicModel, self.anthropic_body([], "tool_use")),
                 (AnthropicModel, self.anthropic_body([{"type": "tool_use", "id": "a", "name": "x", "input": {}}], "end_turn"))]
        for cls, body in cases:
            with self.subTest(cls=cls, body=body), self.assertRaises(ModelError):
                self.complete(cls("m", "k"), body)

    def test_invalid_call_structure_is_rejected(self):
        for field, value in (("id", ""), ("name", ""), ("arguments", {})):
            call = {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            (call if field == "id" else call["function"])[field] = value
            with self.subTest(field=field), self.assertRaises(ModelError):
                self.complete(OpenAIModel("m", "k"), self.openai_body(None, [call], "tool_calls"))
        for field, value in (("id", ""), ("name", ""), ("input", [])):
            call = {"type": "tool_use", "id": "a", "name": "x", "input": {}}
            call[field] = value
            with self.subTest(field=field), self.assertRaises(ModelError):
                self.complete(AnthropicModel("m", "k"), self.anthropic_body([call], "tool_use"))

    def test_openai_tool_calls_must_be_array_even_when_falsey(self):
        for calls in ({}, "", 0, False):
            with self.subTest(calls=calls), self.assertRaises(ModelError):
                self.complete(OpenAIModel("m", "k"), self.openai_body("text", calls))

    def test_http_timeout_errors_are_clean_and_never_retried(self):
        secret = "private-test-key"
        for cls in (OpenAIModel, AnthropicModel):
            for error in (HTTPError("https://example.test", 401, secret, {}, io.BytesIO(secret.encode())), URLError(secret), TimeoutError(secret), OSError(secret)):
                with self.subTest(cls=cls, error=type(error).__name__), patch("miniagent.model.urllib.request.build_opener") as build:
                    build.return_value.open.side_effect = error
                    with self.assertRaises(ModelError) as caught:
                        cls("m", secret).complete([], [], "system")
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertEqual(build.return_value.open.call_count, 1)

    def test_redirect_is_refused(self):
        with patch("miniagent.model.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = HTTPError("https://example.test", 302, "Found", {}, io.BytesIO())
            with self.assertRaises(ModelError):
                OpenAIModel("m", "secret").complete([], [], "system")
            self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "Found", {}, "https://other.test"))
            self.assertEqual(build.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
