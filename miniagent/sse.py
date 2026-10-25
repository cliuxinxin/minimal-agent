"""Bounded, cancellable SSE transport using only the Python standard library."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator


MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024


class SSEError(Exception):
    """A fixed, safe error message; never contains a provider response body."""

    def __init__(self, message: str, *, diagnostic: dict | None = None):
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


class SSECancelled(SSEError):
    pass


@dataclass
class SSEEvent:
    event: str | None
    data: str


class _Cancellation:
    """Wake blocked header/body reads by shutting down the active socket."""

    def __init__(self, should_stop: Callable[[], bool] | None):
        self.should_stop = should_stop
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.worker: threading.Thread | None = None

    def check(self):
        if self.cancelled.is_set() or (self.should_stop and self.should_stop()):
            self.cancelled.set()
            raise SSECancelled("Model request cancelled")

    def track(self, sock):
        with self.lock:
            self.sock = sock
        if self.cancelled.is_set():
            self._interrupt()
        self.check()

    def _interrupt(self):
        with self.lock:
            sock = self.sock
        if sock is not None:
            try:
                # close() alone need not wake a concurrent buffered read.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def start(self):
        if self.should_stop is None:
            return

        def watch():
            while not self.done.wait(0.05):
                if self.should_stop():
                    self.cancelled.set()
                    self._interrupt()
                    return

        self.worker = threading.Thread(target=watch, name="miniagent-model-stream-cancel", daemon=True)
        self.worker.start()

    def close(self):
        self.done.set()
        if self.worker is not None:
            self.worker.join()
        with self.lock:
            self.sock = None


def _opener(cancellation: _Cancellation):
    class HTTPConnection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            cancellation.track(self.sock)

    class HTTPSConnection(http.client.HTTPSConnection):
        def _tunnel(self):
            # HTTPConnection.connect waits for the proxy's CONNECT response
            # before returning, so make that socket cancellable first.
            cancellation.track(self.sock)
            super()._tunnel()

        def connect(self):
            http.client.HTTPConnection.connect(self)
            cancellation.track(self.sock)
            # Track the SSL socket before its handshake starts blocking. Keep the
            # standard HTTPS context, hostname checks, and certificate validation.
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self._tunnel_host or self.host,
                do_handshake_on_connect=False,
            )
            cancellation.track(self.sock)
            self.sock.do_handshake()
            cancellation.check()

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(HTTPConnection, request)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(HTTPSConnection, request, context=self._context)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(NoRedirect(), HTTPHandler(), HTTPSHandler())


def _events(response, cancellation: _Cancellation) -> Iterator[SSEEvent]:
    """Split bytes before decoding, so UTF-8 and CRLF may cross socket reads."""
    line = bytearray()
    data: list[str] = []
    event = None
    total = size = 0
    skip_lf = False
    first_line = True
    while True:
        cancellation.check()
        # read1 returns available bytes rather than waiting to fill an 8 KiB read.
        chunk = response.read1(8192)
        cancellation.check()
        if not chunk:
            return  # An unterminated event is not an SSE event.
        total += len(chunk)
        if total >= MAX_STREAM_BYTES:
            raise SSEError("Model stream exceeded the size limit", diagnostic={"code": "stream_size_limit", "phase": "transport"})
        for byte in chunk:
            if skip_lf:
                skip_lf = False
                if byte == 10:
                    continue
            size += 1
            if size > MAX_EVENT_BYTES:
                raise SSEError("Model stream event exceeded the size limit", diagnostic={"code": "event_size_limit", "phase": "transport"})
            if byte not in (10, 13):
                line.append(byte)
                continue
            skip_lf = byte == 13
            try:
                text = line.decode("utf-8")
            except UnicodeError:
                raise SSEError("Model stream was not valid UTF-8", diagnostic={"code": "invalid_stream_utf8", "phase": "transport"}) from None
            line.clear()
            if first_line:
                text = text.removeprefix("\ufeff")
                first_line = False
            if not text:
                if data:
                    cancellation.check()
                    yield SSEEvent(event, "\n".join(data))
                data = []
                event = None
                size = 0
            elif not text.startswith(":"):
                field, separator, value = text.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value or None


def _response_metadata(response, request_headers, metadata):
    """Keep only status and a bounded request identifier, never arbitrary headers."""
    status = getattr(response, "status", None)
    if type(status) is int:
        metadata["http_status"] = status
    secrets = [value.removeprefix("Bearer ") for name, value in request_headers.items()
               if name.lower() in {"authorization", "x-api-key", "api-key"} and isinstance(value, str) and value]
    for name in ("x-request-id", "request-id"):
        value = response.headers.get(name)
        if (isinstance(value, str) and 0 < len(value) <= 200
                and all(ch.isascii() and (ch.isalnum() or ch in "-_.:") for ch in value)
                and not any(secret and secret in value for secret in secrets)):
            metadata["request_id"] = value
            break


@contextmanager
def open_sse(endpoint: str, headers: dict, body: dict, should_stop: Callable[[], bool] | None = None, *, metadata: dict | None = None):
    """One streaming POST; no redirects, retries, or provider error-body exposure."""
    cancellation = _Cancellation(should_stop)
    metadata = {} if metadata is None else metadata
    started = time.monotonic()
    try:
        cancellation.check()
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json", "Accept": "text/event-stream", **headers}, method="POST")
        except (TypeError, ValueError, UnicodeError):
            raise SSEError("Model request could not be encoded", diagnostic={"code": "request_encoding_error", "phase": "request"}) from None
        cancellation.start()
        with _opener(cancellation).open(request, timeout=60) as response:
            _response_metadata(response, headers, metadata)
            cancellation.check()
            yield _events(response, cancellation)
            cancellation.check()
    except urllib.error.HTTPError as error:
        _response_metadata(error, headers, metadata)
        error.close()
        cancellation.check()
        raise SSEError(f"Model endpoint returned HTTP {error.code}", diagnostic={"code": "http_error", "phase": "response_headers"}) from None
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
        cancellation.check()
        raise SSEError("Model request failed or timed out", diagnostic={"code": "transport_error", "phase": "transport"}) from None
    finally:
        cancellation.close()
        metadata["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
