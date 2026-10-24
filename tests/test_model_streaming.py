"""Real local HTTP streams exercise provider assembly, bounds, and cancellation."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socketserver
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from miniagent.agent import Agent
from miniagent.model import AnthropicModel, ChatModel, ModelCancelled, ModelError, ModelReply, OpenAIModel, ToolCall


TOOLS = [{"name": "read_file", "description": "Read text", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


def frame(payload, event=None, newline="\n"):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if not isinstance(payload, str) else payload
    return ((f"event: {event}{newline}" if event else "") + f"data: {data}{newline}{newline}").encode()


def openai(delta=None, reason=None):
    return {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}


def anthropic(event, **fields):
    return frame({"type": event, **fields}, event)


def anthropic_text(text="你好", terminal=True, reason="end_turn"):
    data = [
        anthropic("message_start", message={"role": "assistant", "content": [], "stop_reason": None}),
        anthropic("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        anthropic("content_block_delta", index=0, delta={"type": "text_delta", "text": text}),
        anthropic("content_block_stop", index=0),
        anthropic("message_delta", delta={"stop_reason": reason}),
    ]
    if terminal:
        data.append(anthropic("message_stop"))
    return data


@contextmanager
def provider_server(chunks=(), *, status=200, headers=None, before_headers=None):
    """All network I/O stays on loopback. Server-side gates are released by tests."""
    requests = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "body": json.loads(body), "headers": dict(self.headers)})
            received.set()
            if before_headers:
                before_headers()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


class StreamingTests(unittest.TestCase):
    def stream(self, model, *, on_delta=None, should_stop=None, messages=None):
        stream = getattr(model, "stream", None)
        self.assertTrue(callable(stream), "Provider must expose the native streaming API")
        return stream(messages or [{"role": "user", "content": "hi"}], TOOLS, "Be helpful", on_delta=on_delta or (lambda _: None), should_stop=should_stop)

    def test_legacy_protocol_subclass_keeps_nonstream_fallback(self):
        class LegacyModel(ChatModel):
            def complete(self, messages, tools, system_prompt):
                return ModelReply("Legacy answer", [], {"role": "assistant", "content": "Legacy answer"})

            def tool_messages(self, results):
                return []

        events = []
        result = Agent(LegacyModel(), stream=True, on_event=events.append).run("hi")
        self.assertEqual((result.status, result.answer), ("completed", "Legacy answer"))
        self.assertEqual([e["data"]["text"] for e in events if e["event"] == "text_delta"], ["Legacy answer"])

    def test_openai_split_utf8_multiline_frames_and_reasoning_history(self):
        # Dropping partial UTF-8 bytes, multi-data lines, or reasoning breaks this.
        multiline = b'data: {"choices":\r\ndata: [{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\r\n\r\n'
        raw = b"\xef\xbb\xbf: keepalive\r\n\r\n" + multiline
        raw += frame(openai({"reasoning_content": "先看"}), newline="\r")
        raw += frame(openai({"reasoning_content": "问题。"}), newline="\r\n")
        raw += frame(openai({"content": "你好"})) + frame(openai({"content": "，世界"}))
        raw += frame(openai(reason="stop")) + frame({"choices": [], "usage": {"total_tokens": 9}}) + frame("[DONE]")
        deltas = []
        with provider_server((raw[i:i + 1] for i in range(len(raw)))) as (base, requests, _):
            reply = self.stream(OpenAIModel("test", "private-key", base), on_delta=deltas.append)
        self.assertEqual(reply.text, "你好，世界")
        self.assertEqual(reply.message, {"role": "assistant", "content": "你好，世界", "reasoning_content": "先看问题。"})
        self.assertEqual("".join(d["text"] for d in deltas if d["type"] == "text_delta"), "你好，世界")
        self.assertEqual("".join(d["text"] for d in deltas if d["type"] == "reasoning_delta"), "先看问题。")
        self.assertTrue(requests[0]["body"]["stream"])
        self.assertNotIn("thinking", requests[0]["body"])
        self.assertNotIn("private-key", json.dumps(deltas))

    def test_openai_interleaved_tool_fragments_keep_stable_indexes(self):
        chunks = [
            frame(openai({"tool_calls": [{"index": 1, "id": "call_1", "type": "function", "function": {"name": "other", "arguments": "{}"}}]})),
            frame(openai({"tool_calls": [{"index": 0, "id": "call_", "type": "function", "function": {"name": "read_", "arguments": '{"path":"'}}]})),
            frame(openai({"tool_calls": [{"index": 0, "id": "0", "function": {"name": "file", "arguments": '文件.py"}'}}]})),
            frame(openai(reason="tool_calls")), frame("[DONE]"),
        ]
        deltas = []
        with provider_server(chunks) as (base, _, _):
            reply = self.stream(OpenAIModel("test", "key", base), on_delta=deltas.append)
        self.assertEqual(reply.tool_calls, [ToolCall("call_0", "read_file", '{"path":"文件.py"}'), ToolCall("call_1", "other", "{}")])
        self.assertEqual(deltas[-1], {"type": "tool_call_delta", "index": 0, "id": "call_0", "name": "read_file", "arguments_delta": '文件.py"}'})
        self.assertEqual(reply.message["tool_calls"][0]["function"]["arguments"], '{"path":"文件.py"}')

    def test_openai_never_returns_unfinished_tool_arguments(self):
        for arguments in ('{"path":', '[]', '{"value":NaN}'):
            chunks = [frame(openai({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "read_file", "arguments": arguments}}]}, "tool_calls")), frame("[DONE]")]
            with self.subTest(arguments=arguments), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(OpenAIModel("test", "key", base))

    def test_openai_rejects_duplicate_tool_argument_keys(self):
        for arguments in ('{"a":1,"a":99,"b":2}', '{"nested":{"a":1,"a":99}}'):
            chunks = [
                frame(openai({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "add", "arguments": arguments}}]}, "tool_calls")),
                frame("[DONE]"),
            ]
            with self.subTest(arguments=arguments), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(OpenAIModel("test", "key", base))

    def test_openai_rejects_falsey_non_array_tool_fragments(self):
        for calls in ({}, "", 0, False):
            with self.subTest(calls=calls), provider_server([frame(openai({"content": "done", "tool_calls": calls}, "stop")), frame("[DONE]")]) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(OpenAIModel("test", "key", base))

    def test_https_stream_uses_supported_stdlib_connection_options(self):
        # Model the completed TLS handshake only; exercise the real HTTPS handler,
        # connection constructor, HTTP request, and stream on the loopback server.
        class ConnectedTLS:
            def __init__(self, sock, **_kwargs):
                self.sock = sock

            def do_handshake(self):
                pass

            def __getattr__(self, name):
                return getattr(self.sock, name)

        with provider_server([frame(openai({"content": "done"}, "stop")), frame("[DONE]")]) as (base, requests, _):
            with patch.object(ssl.SSLContext, "wrap_socket", side_effect=ConnectedTLS):
                reply = self.stream(OpenAIModel("test", "key", base.replace("http:", "https:")))
        self.assertEqual(reply.text, "done")
        self.assertEqual(len(requests), 1)

    def test_anthropic_preserves_opaque_blocks_and_assembles_tools(self):
        chunks = [
            anthropic("message_start", message={"role": "assistant", "content": []}),
            anthropic("content_block_start", index=0, content_block={"type": "thinking", "thinking": "", "signature": ""}),
            anthropic("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "先检查文件"}),
            anthropic("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "opaque-"}),
            anthropic("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "signature"}),
            anthropic("content_block_stop", index=0),
            anthropic("content_block_start", index=1, content_block={"type": "redacted_thinking", "data": "encrypted-private-data"}),
            anthropic("content_block_stop", index=1),
            anthropic("ping"),
            anthropic("future_event", metadata="ignored"),
            anthropic("content_block_start", index=2, content_block={"type": "text", "text": ""}),
            anthropic("content_block_delta", index=2, delta={"type": "text_delta", "text": "正在读取"}),
            anthropic("content_block_stop", index=2),
            anthropic("content_block_start", index=3, content_block={"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}),
            anthropic("content_block_delta", index=3, delta={"type": "input_json_delta", "partial_json": '{"path":'}),
            anthropic("content_block_delta", index=3, delta={"type": "input_json_delta", "partial_json": '"你好.py"}'}),
            anthropic("content_block_stop", index=3),
            anthropic("message_delta", delta={"stop_reason": "tool_use"}),
            anthropic("message_stop"),
        ]
        deltas = []
        with provider_server(chunks) as (base, requests, _):
            reply = self.stream(AnthropicModel("test", "private-key", base), on_delta=deltas.append)
        self.assertEqual(reply.text, "正在读取")
        self.assertEqual(reply.tool_calls, [ToolCall("toolu_1", "read_file", '{"path": "你好.py"}')])
        self.assertEqual(reply.message["content"], [
            {"type": "thinking", "thinking": "先检查文件", "signature": "opaque-signature"},
            {"type": "redacted_thinking", "data": "encrypted-private-data"},
            {"type": "text", "text": "正在读取"},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "你好.py"}},
        ])
        visible = json.dumps(deltas, ensure_ascii=False)
        for hidden in ("opaque-signature", "encrypted-private-data", "private-key"):
            self.assertNotIn(hidden, visible)
        self.assertEqual([d["text"] for d in deltas if d["type"] == "reasoning_delta"], ["先检查文件"])
        self.assertTrue(requests[0]["body"]["stream"])
        self.assertNotIn("thinking", requests[0]["body"])

    def test_anthropic_error_named_tool_argument_is_regular_input(self):
        chunks = [
            anthropic("message_start", message={"role": "assistant", "content": []}),
            anthropic("content_block_start", index=0, content_block={"type": "tool_use", "id": "a", "name": "write_file", "input": {}}),
            anthropic("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"error":"expected"}'}),
            anthropic("content_block_stop", index=0),
            anthropic("message_delta", delta={"stop_reason": "tool_use"}), anthropic("message_stop"),
        ]
        with provider_server(chunks) as (base, _, _):
            reply = self.stream(AnthropicModel("test", "key", base))
        self.assertEqual(reply.tool_calls, [ToolCall("a", "write_file", '{"error": "expected"}')])

    def test_anthropic_rejects_duplicate_tool_argument_keys(self):
        for arguments in ('{"a":1,"a":99,"b":2}', '{"nested":{"a":1,"a":99}}'):
            chunks = [
                anthropic("message_start", message={"role": "assistant", "content": []}),
                anthropic("content_block_start", index=0, content_block={"type": "tool_use", "id": "a", "name": "add", "input": {}}),
                anthropic("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": arguments}),
                anthropic("content_block_stop", index=0),
                anthropic("message_delta", delta={"stop_reason": "tool_use"}),
                anthropic("message_stop"),
            ]
            with self.subTest(arguments=arguments), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(AnthropicModel("test", "key", base))

    def test_missing_terminal_marker_never_returns_a_completed_reply(self):
        for cls, chunks in (
            (OpenAIModel, [frame(openai({"content": "partial"})), frame(openai(reason="stop"))]),
            (AnthropicModel, anthropic_text("partial", terminal=False)),
        ):
            deltas = []
            with self.subTest(provider=cls.__name__), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(cls("test", "key", base), on_delta=deltas.append)
            self.assertTrue(any(d.get("text") == "partial" for d in deltas))

    def test_finish_reason_must_be_complete_and_match_tools(self):
        for reason in (None, "length", "content_filter", "aborted", "tool_calls"):
            with self.subTest(reason=reason), provider_server([frame(openai({"content": "partial"}, reason)), frame("[DONE]")]) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(OpenAIModel("test", "key", base))
        for reason in (None, "max_tokens", "pause_turn", "refusal", "tool_use"):
            with self.subTest(reason=reason), provider_server(anthropic_text(reason=reason)) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(AnthropicModel("test", "key", base))

    def test_provider_errors_do_not_expose_error_bodies(self):
        for cls, chunks in (
            (OpenAIModel, [frame({"error": {"message": "private-key and raw request"}})]),
            (AnthropicModel, [anthropic("error", error={"type": "overloaded_error", "message": "private-key and raw request"})]),
        ):
            deltas = []
            with self.subTest(provider=cls.__name__), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError) as caught:
                    self.stream(cls("test", "private-key", base), on_delta=deltas.append)
                self.assertNotIn("private-key", str(caught.exception))
                self.assertNotIn("raw request", str(caught.exception))
                self.assertEqual(deltas, [])

    def test_http_errors_and_redirects_are_not_retried_or_forwarded(self):
        for status in (401, 302):
            with self.subTest(status=status), provider_server([b"private-key"], status=status, headers={"Location": "/stolen"}) as (base, requests, _):
                with self.assertRaises(ModelError) as caught:
                    self.stream(OpenAIModel("test", "private-key", base))
                self.assertEqual(len(requests), 1)
                self.assertNotIn("private-key", str(caught.exception))

    def test_invalid_utf8_json_and_unfinished_tool_input_are_rejected(self):
        bad_anthropic = [
            anthropic("message_start", message={"role": "assistant", "content": []}),
            anthropic("content_block_start", index=0, content_block={"type": "tool_use", "id": "a", "name": "read_file", "input": {}}),
            anthropic("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"path":'}),
            anthropic("content_block_stop", index=0),
            anthropic("message_delta", delta={"stop_reason": "tool_use"}), anthropic("message_stop"),
        ]
        cases = [(OpenAIModel, [b"data: \xff\n\n"]), (OpenAIModel, [b"data: broken\n\n"]), (AnthropicModel, bad_anthropic)]
        for cls, chunks in cases:
            with self.subTest(provider=cls.__name__, chunks=chunks[:1]), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError):
                    self.stream(cls("test", "key", base))

    def test_event_and_total_size_limits_are_enforced(self):
        # A single unterminated line and many small comments exercise both bounds.
        for chunks in ([b"data: " + b"x" * (300 * 1024)], [b":" + b"x" * 1022 + b"\n\n"] * 2100):
            with self.subTest(chunks=len(chunks)), provider_server(chunks) as (base, _, _):
                with self.assertRaises(ModelError) as caught:
                    self.stream(OpenAIModel("test", "key", base))
                self.assertIn("limit", str(caught.exception).lower())

    def test_delta_arrives_before_upstream_finishes(self):
        release = threading.Event()
        first_delta = threading.Event()
        results = []

        def chunks():
            yield frame(openai({"content": "first"}))
            release.wait(2)
            yield frame(openai({"content": " second"}, "stop"))
            yield frame("[DONE]")

        with provider_server(chunks()) as (base, _, _):
            model = OpenAIModel("test", "key", base)
            self.assertTrue(callable(getattr(model, "stream", None)), "Provider must stream before completion")
            worker = threading.Thread(target=lambda: results.append(self.stream(model, on_delta=lambda _: first_delta.set())), daemon=True)
            worker.start()
            try:
                self.assertTrue(first_delta.wait(1), "A complete SSE frame must be delivered without waiting for EOF")
                self.assertTrue(worker.is_alive(), "The upstream final frame has not been released")
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertEqual(results[0].text, "first second")

    def test_cancel_interrupts_blocked_reads_and_cleans_up_watcher(self):
        for wait_for_headers in (False, True):
            stop = threading.Event()
            release = threading.Event()
            first_delta = threading.Event()
            outcome = []

            def chunks():
                yield frame(openai({"content": "first"}))
                release.wait(3)

            with self.subTest(headers=wait_for_headers), provider_server(chunks(), before_headers=(lambda: release.wait(3)) if wait_for_headers else None) as (base, _, received):
                model = OpenAIModel("test", "key", base)
                self.assertTrue(callable(getattr(model, "stream", None)), "Provider must support cancellable streaming")
                before = {t.ident for t in threading.enumerate() if t.name.startswith("miniagent-model-stream")}

                def call():
                    try:
                        outcome.append(self.stream(model, on_delta=lambda _: first_delta.set(), should_stop=stop.is_set))
                    except Exception as error:
                        outcome.append(error)

                worker = threading.Thread(target=call, daemon=True)
                worker.start()
                try:
                    self.assertTrue((received if wait_for_headers else first_delta).wait(1))
                    started = time.monotonic()
                    stop.set()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive(), "Cancellation must wake a blocked socket read")
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertIsInstance(outcome[0], ModelError)
                    self.assertEqual(type(outcome[0]).__name__, "ModelCancelled")
                    after = {t.ident for t in threading.enumerate() if t.name.startswith("miniagent-model-stream")}
                    self.assertEqual(after, before)
                finally:
                    release.set()
                    worker.join(timeout=2)

    def test_cancel_interrupts_a_stalled_tls_handshake(self):
        stop, release, connected = threading.Event(), threading.Event(), threading.Event()
        outcome = []

        class HandshakeStall(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(8192)  # ClientHello has been sent.
                connected.set()
                release.wait(3)

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandshakeStall)
        server.daemon_threads = True
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        serving.start()
        model = OpenAIModel("test", "key", f"https://127.0.0.1:{server.server_address[1]}")

        def call():
            try:
                outcome.append(self.stream(model, should_stop=stop.is_set))
            except Exception as error:
                outcome.append(error)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        try:
            self.assertTrue(connected.wait(1))
            stop.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "A stalled TLS read must also be cancellable")
            self.assertEqual(type(outcome[0]).__name__, "ModelCancelled")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=1)
            worker.join(timeout=2)

    def test_cancel_interrupts_a_stalled_proxy_connect_and_cleans_up_watcher(self):
        stop, release, connected = threading.Event(), threading.Event(), threading.Event()
        outcome = []

        class TunnelStall(socketserver.BaseRequestHandler):
            def handle(self):
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = self.request.recv(8192)
                    if not chunk:
                        return
                    request += chunk
                if request.startswith(b"CONNECT example.com:443 HTTP/1."):
                    connected.set()
                release.wait(3)

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), TunnelStall)
        server.daemon_threads = True
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        serving.start()
        before = {t.ident for t in threading.enumerate() if t.name.startswith("miniagent-model-stream")}

        def call():
            try:
                model = OpenAIModel("test", "key", "https://example.com/v1")
                outcome.append(self.stream(model, should_stop=stop.is_set))
            except Exception as error:
                outcome.append(error)

        worker = threading.Thread(target=call, daemon=True)
        proxy = {"https": f"http://127.0.0.1:{server.server_address[1]}"}
        try:
            with patch("urllib.request.getproxies", return_value=proxy), patch("urllib.request.proxy_bypass", return_value=False):
                worker.start()
                self.assertTrue(connected.wait(1), "The local proxy must receive CONNECT")
                started = time.monotonic()
                stop.set()
                worker.join(timeout=0.8)
                self.assertFalse(worker.is_alive(), "Cancellation must wake a blocked proxy CONNECT read")
                self.assertLess(time.monotonic() - started, 1)
                self.assertIsInstance(outcome[0], ModelCancelled)
                after = {t.ident for t in threading.enumerate() if t.name.startswith("miniagent-model-stream")}
                self.assertEqual(after, before)
        finally:
            release.set()
            worker.join(timeout=2)
            server.shutdown()
            server.server_close()
            serving.join(timeout=1)

    def test_cancel_before_request_does_not_contact_provider(self):
        with provider_server([]) as (base, requests, _):
            with self.assertRaises(ModelError) as caught:
                self.stream(OpenAIModel("test", "key", base), should_stop=lambda: True)
            self.assertEqual(type(caught.exception).__name__, "ModelCancelled")
            self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
