from __future__ import annotations

import errno
import http.client
import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import crossover_electron_bridge.browser as browser_module

from crossover_electron_bridge.browser import (
    ApiRequest,
    ApiResponse,
    BrowserServer,
    StaticAdapter,
)
from crossover_electron_bridge.errors import OperationalError
from crossover_electron_bridge.profiles import (
    GameProfile,
    RendererConfig,
    SteamBuild,
    SteamConfig,
)


class RecordingAdapter:
    def __init__(self, bridge: str = "window.__bridgeToken = TOKEN;") -> None:
        self.bridge = bridge
        self.started = 0
        self.requests: list[ApiRequest] = []

    def before_start(self) -> None:
        self.started += 1

    def bridge_script(self, token: str) -> str:
        return self.bridge.replace("TOKEN", repr(token))

    def handle_api(self, request: ApiRequest) -> ApiResponse | None:
        self.requests.append(request)
        if request.path == "/api/known":
            if request.method != "GET":
                return ApiResponse(405, {"error": "method"})
            return ApiResponse(200, {"ok": True})
        if request.path == "/api/json":
            if request.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return ApiResponse(415, {"error": "content-type"})
            return ApiResponse(200, {"body": request.body.decode("utf-8")})
        if request.path == "/api/echo":
            return ApiResponse(200, {"body": request.body.decode("utf-8")})
        return None


class _FakeServer:
    def __init__(self, port: int) -> None:
        self.server_address = ("127.0.0.1", port)
        self.daemon_threads = False
        self.browser_owner: BrowserServer | None = None
        self.closed = False

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self.closed = True


class _MemoryRequest:
    def __init__(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.path = path
        self.command = method
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int, _message: str | None = None) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name] = value

    def end_headers(self) -> None:
        return None


class _ProbeResponse:
    status = 200

    def __init__(self, payload: bytes = b"", error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.read_amounts: list[int | None] = []
        self.headers = Message()

    def read(self, amount: int | None = None) -> bytes:
        self.read_amounts.append(amount)
        if self.error is not None:
            raise self.error
        return self.payload

    def getheader(self, _name: str, default: Any = None) -> Any:
        return default


class _ProbeConnection:
    def __init__(self, response: _ProbeResponse) -> None:
        self.response = response
        self.closed = False

    def request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def getresponse(self) -> _ProbeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class _AddressInUseFactory:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EADDRINUSE, "address already in use")


class BrowserHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.install = (
            self.home
            / "Library/Application Support/CrossOver/Bottles/Steam/drive_c"
            / "Program Files (x86)/Steam/steamapps/common/Test Game"
        )
        self.assets = self.install / "resources/app/dist"
        self.assets.mkdir(parents=True)
        (self.assets / "index.html").write_text(
            "<html><head></head><body><script type=\"module\" src=\"/app.js\"></script>"
            "<script type=\"module\">second()</script></body></html>",
            encoding="utf-8",
        )
        (self.assets / "app.js").write_text("export default 1;", encoding="utf-8")
        (self.assets / "style.css").write_text("body { color: red; }", encoding="utf-8")
        (self.assets / "data.json").write_text('{"ok":true}', encoding="utf-8")
        self.profile = GameProfile(
            schema_version=1,
            id="browser-test",
            display_name="Browser Test",
            mode="browser-only",
            bottle="Steam",
            steam=SteamConfig(123, "Test Game", (SteamBuild("1"),)),
            renderer=RendererConfig(
                "resources/app/dist", "127.0.0.1", "localhost", 0
            ),
            adapter="static",
            state_key="browser-test",
            adapter_config={"max_request_bytes": 4},
        )
        self.server: BrowserServer | None = None
        self.thread: threading.Thread | None = None
        self.opened: list[str] = []

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def start(self, adapter: Any | None = None, *, profile: GameProfile | None = None) -> BrowserServer:
        self.server = BrowserServer(
            profile or self.profile,
            adapter or RecordingAdapter(),
            home=self.home,
            opener=self.opened.append,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"open_browser": False},
            daemon=True,
        )
        self.thread.start()
        return self.server

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        port: int | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        server = self.server
        assert server is not None
        connection = http.client.HTTPConnection("127.0.0.1", port or server.actual_port, timeout=2)
        request_headers = dict(headers or {})
        if "Host" not in request_headers:
            request_headers["Host"] = f"{server.profile.renderer.browser_host}:{server.actual_port}"
        if body is not None and isinstance(body, str):
            body = body.encode("utf-8")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def api_headers(self, **extra: str) -> dict[str, str]:
        assert self.server is not None
        headers = {
            "Host": f"{self.profile.renderer.browser_host}:{self.server.actual_port}",
            "X-Crossover-Token": self.server.token,
        }
        headers.update(extra)
        return headers

    def raw_exchange(
        self,
        request: bytes,
        *,
        timeout: float = 1.5,
        expect_close: bool = True,
    ) -> bytes:
        assert self.server is not None
        with socket.create_connection(
            ("127.0.0.1", self.server.actual_port), timeout=timeout
        ) as client:
            client.settimeout(timeout)
            client.sendall(request)
            response = bytearray()
            try:
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
            except socket.timeout as exc:
                if expect_close:
                    self.fail(f"HTTP peer did not close bounded exchange: {exc}")
            return bytes(response)

    @staticmethod
    def parse_raw_response(response: bytes) -> tuple[int, dict[str, str], bytes]:
        head, body = response.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
        return status, headers, body

    def test_static_adapter_implements_runtime_protocol_and_request_is_immutable(self) -> None:
        from crossover_electron_bridge.browser import BrowserAdapter

        self.assertIsInstance(StaticAdapter(), BrowserAdapter)
        request = ApiRequest("GET", "/api/test", {"X-Test": "1"}, b"")
        with self.assertRaises(TypeError):
            request.headers["X-Test"] = "2"  # type: ignore[index]

    def test_index_injects_adapter_before_first_module_script(self) -> None:
        adapter = RecordingAdapter("window.__bridgeToken = TOKEN;")
        server = self.start(adapter)
        status, headers, content = self.request("GET", "/")
        text = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(server.token, text)
        injection = "<script>\nwindow.__bridgeToken = " + repr(server.token)
        self.assertEqual(text.count(injection), 1)
        self.assertLess(text.index(injection), text.index('<script type="module"'))
        self.assertEqual(text.count('<script type="module"'), 2)
        self.assertEqual(adapter.started, 1)

    def test_static_mime_and_head(self) -> None:
        self.start(StaticAdapter())
        status, headers, content = self.request("GET", "/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(content, b"body { color: red; }")
        status, head_headers, head_content = self.request("HEAD", "/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(head_headers["Content-Type"], headers["Content-Type"])
        self.assertEqual(head_headers["Content-Length"], str(len(content)))
        self.assertEqual(head_content, b"")

    def test_traversal_and_symlink_escape_are_rejected(self) -> None:
        outside = self.home / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.assets / "outside-link").symlink_to(outside)
        self.start(StaticAdapter())
        status, _headers, _content = self.request("GET", "/%2e%2e/outside.txt")
        self.assertEqual(status, 400)
        status, _headers, _content = self.request("GET", "/outside-link")
        self.assertEqual(status, 400)
        status, _headers, _content = self.request("GET", "/missing.js")
        self.assertEqual(status, 404)

    def test_health_identity_is_public_and_exact(self) -> None:
        self.start(StaticAdapter())
        self.assertEqual(self.opened, [])
        status, headers, content = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(content),
            {
                "ok": True,
                "service": "browser-test",
                "host": "127.0.0.1",
                "port": self.server.actual_port,  # type: ignore[union-attr]
            },
        )
        status, _headers, content = self.request("HEAD", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(content, b"")

    def test_port_80_uses_canonical_host_and_origin_values(self) -> None:
        profile = replace(
            self.profile,
            renderer=replace(self.profile.renderer, port=80),
        )
        fake_server = _FakeServer(80)
        server = BrowserServer(
            profile,
            RecordingAdapter(),
            home=self.home,
            server=fake_server,
        )
        self.server = server

        canonical = _MemoryRequest(
            "/api/known",
            headers={
                "Host": "localhost",
                "Origin": "http://localhost",
                "X-Crossover-Token": server.token,
            },
        )
        server._dispatch(canonical, "GET")
        self.assertEqual(canonical.status, 200)

        bind_host = _MemoryRequest(
            "/api/known",
            headers={
                "Host": "127.0.0.1",
                "X-Crossover-Token": server.token,
            },
        )
        server._dispatch(bind_host, "GET")
        self.assertEqual(bind_host.status, 200)

    def test_malformed_or_incomplete_health_probe_maps_to_port_conflict(self) -> None:
        renderer = replace(self.profile.renderer, port=43127)
        profile = replace(self.profile, renderer=renderer)
        for response in (
            _ProbeResponse(payload=b"not-json"),
            _ProbeResponse(error=http.client.IncompleteRead(b'{"ok":')),
        ):
            connection = _ProbeConnection(response)

            def connection_factory(*_args: Any, **_kwargs: Any) -> _ProbeConnection:
                return connection

            with self.subTest(response=response):
                with patch.object(
                    browser_module.http.client,
                    "HTTPConnection",
                    connection_factory,
                ):
                    with self.assertRaisesRegex(
                        OperationalError,
                        r"^Port 43127 is already in use by another process$",
                    ):
                        BrowserServer(
                            profile,
                            StaticAdapter(),
                            home=self.home,
                            server=_AddressInUseFactory(),
                        )
            self.assertTrue(connection.closed)

    def test_slow_dribble_health_probe_honors_total_deadline(self) -> None:
        renderer = replace(self.profile.renderer, port=43129)
        profile = replace(self.profile, renderer=renderer)

        class DribbleResponse(_ProbeResponse):
            def __init__(self) -> None:
                super().__init__()
                self.read1_amounts: list[int | None] = []
                self.bytes_read = 0
                self.buffered_reads = 0

            def read(self, amount: int | None = None) -> bytes:
                self.buffered_reads += 1
                if amount is None or amount <= 0:
                    raise AssertionError("health probe read without a positive bound")
                if self.buffered_reads > 1:
                    return b""
                return json.dumps(
                    {
                        "ok": True,
                        "service": "browser-test",
                        "host": "127.0.0.1",
                        "port": 43129,
                    }
                ).encode("utf-8")

            def read1(self, amount: int | None = None) -> bytes:
                self.read1_amounts.append(amount)
                if amount is None or amount <= 0:
                    raise AssertionError("health probe read without a positive bound")
                self.bytes_read += 1
                return b"x"

        response = DribbleResponse()
        connection = _ProbeConnection(response)

        def connection_factory(*_args: Any, **_kwargs: Any) -> _ProbeConnection:
            return connection

        clock_calls = 0

        def monotonic() -> float:
            nonlocal clock_calls
            value = min(clock_calls * 0.001, 0.005)
            clock_calls += 1
            return value

        with patch.object(browser_module.time, "monotonic", monotonic):
            with patch.object(BrowserServer, "PROBE_TIMEOUT_SECONDS", 0.005):
                with patch.object(
                    browser_module.http.client,
                    "HTTPConnection",
                    connection_factory,
                ):
                    with self.assertRaisesRegex(
                        OperationalError,
                        r"^Port 43129 is already in use by another process$",
                    ):
                        BrowserServer(
                            profile,
                            StaticAdapter(),
                            home=self.home,
                            server=_AddressInUseFactory(),
                        )

        self.assertGreaterEqual(len(response.read1_amounts), 2)
        self.assertLessEqual(
            response.bytes_read, BrowserServer.HEALTH_RESPONSE_MAX_BYTES
        )
        self.assertEqual(response.buffered_reads, 0)
        self.assertTrue(connection.closed)

    def test_streaming_health_probe_reads_a_bounded_amount(self) -> None:
        renderer = replace(self.profile.renderer, port=43128)
        profile = replace(self.profile, renderer=renderer)

        class StreamingResponse(_ProbeResponse):
            def read(self, amount: int | None = None) -> bytes:
                self.read_amounts.append(amount)
                if amount is None:
                    raise AssertionError("health probe read without a bound")
                if len(self.read_amounts) > 1:
                    raise AssertionError("health probe consumed an unbounded stream")
                self.assert_bounded(amount)
                return b"x" * (amount + 1)

            @staticmethod
            def assert_bounded(amount: int) -> None:
                if amount <= 0 or amount > 1024 * 1024:
                    raise AssertionError(f"unexpected health probe bound: {amount}")

        response = StreamingResponse()
        connection = _ProbeConnection(response)

        def connection_factory(*_args: Any, **_kwargs: Any) -> _ProbeConnection:
            return connection

        with patch.object(
            browser_module.http.client,
            "HTTPConnection",
            connection_factory,
        ):
            with self.assertRaisesRegex(
                OperationalError,
                r"^Port 43128 is already in use by another process$",
            ):
                BrowserServer(
                    profile,
                    StaticAdapter(),
                    home=self.home,
                    server=_AddressInUseFactory(),
                )
        self.assertEqual(len(response.read_amounts), 1)
        self.assertIsNotNone(response.read_amounts[0])
        self.assertTrue(connection.closed)

    def test_api_host_origin_then_token_order(self) -> None:
        self.start(RecordingAdapter())
        status, _headers, _content = self.request(
            "GET", "/api/known", headers={"Host": "evil.test"}
        )
        self.assertEqual(status, 403)
        status, _headers, _content = self.request(
            "GET",
            "/api/known",
            headers={"Host": "localhost:" + str(self.server.actual_port), "Origin": "http://evil"},  # type: ignore[union-attr]
        )
        self.assertEqual(status, 403)
        status, _headers, _content = self.request(
            "GET", "/api/known", headers={"Host": "localhost:" + str(self.server.actual_port)}  # type: ignore[union-attr]
        )
        self.assertEqual(status, 401)
        status, _headers, _content = self.request(
            "GET", "/api/known", headers=self.api_headers(**{"X-Crossover-Token": "wrong"})
        )
        self.assertEqual(status, 401)
        status, _headers, content = self.request("GET", "/api/known", headers=self.api_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"ok": True})

    def test_limits_transfer_encoding_and_content_length(self) -> None:
        self.start(RecordingAdapter())
        status, _headers, _content = self.request(
            "POST",
            "/api/echo",
            headers=self.api_headers(**{"Content-Length": "5"}),
            body=b"12345",
        )
        self.assertEqual(status, 413)
        status, _headers, _content = self.request(
            "POST",
            "/api/echo",
            headers=self.api_headers(**{"Transfer-Encoding": "chunked"}),
            body=b"0\r\n\r\n",
        )
        self.assertEqual(status, 400)
        status, _headers, _content = self.request(
            "POST",
            "/api/echo",
            headers=self.api_headers(**{"Content-Length": "abc"}),
            body=b"",
        )
        self.assertEqual(status, 400)

    def test_incomplete_content_length_is_rejected(self) -> None:
        self.start(RecordingAdapter())
        assert self.server is not None
        request = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "Content-Length: 4\r\n"
            "Connection: close\r\n\r\n"
            "x"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.server.actual_port), timeout=2) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_bridge_script_failure_is_contained_as_json_500(self) -> None:
        class BrokenBridgeAdapter(RecordingAdapter):
            def bridge_script(self, _token: str) -> str:
                raise RuntimeError("bridge exploded")

        self.start(BrokenBridgeAdapter())
        status, _headers, content = self.request("GET", "/")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(content), {"error": "Internal server error"})
        self.assertNotIn(b"bridge exploded", content)
        self.assertNotIn(b"Traceback", content)

    def test_slow_dribble_body_honors_total_deadline(self) -> None:
        profile = replace(self.profile, adapter_config={"max_request_bytes": 16})
        self.start(RecordingAdapter(), profile=profile)
        assert self.server is not None
        request_head = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "Content-Length: 8\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")

        started = browser_module.time.monotonic()
        response = bytearray()
        with patch.object(BrowserServer, "BODY_READ_TIMEOUT_SECONDS", 0.05):
            with socket.create_connection(
                ("127.0.0.1", self.server.actual_port), timeout=1
            ) as client:
                client.settimeout(1)
                client.sendall(request_head)
                for index, byte in enumerate(b"abcdefgh"):
                    try:
                        client.sendall(bytes((byte,)))
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    if index != 7:
                        # Each interval is below the per-read timeout, while
                        # the complete body necessarily exceeds the deadline.
                        browser_module.time.sleep(0.01)
                try:
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
                except (ConnectionResetError, socket.timeout):
                    pass
        elapsed = browser_module.time.monotonic() - started
        status, headers, _body = self.parse_raw_response(bytes(response))
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection", "").lower(), "close")
        self.assertLess(elapsed, 0.25)

    def test_partial_authenticated_body_times_out_and_closes(self) -> None:
        self.start(RecordingAdapter())
        assert self.server is not None
        request = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "Content-Length: 4\r\n"
            "\r\n"
            "x"
        ).encode("ascii")
        with patch.object(BrowserServer, "BODY_READ_TIMEOUT_SECONDS", 0.05):
            response = self.raw_exchange(request, timeout=1.5)
        status, headers, _body = self.parse_raw_response(response)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection", "").lower(), "close")

        assert self.server is not None
        request = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "Content-Length: 4\r\n"
            "\r\n"
            "x"
        ).encode("ascii")
        with patch.object(BrowserServer, "BODY_READ_TIMEOUT_SECONDS", 0.05):
            response = self.raw_exchange(request, timeout=1.5)
        status, headers, _body = self.parse_raw_response(response)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection", "").lower(), "close")

    def test_rejected_auth_body_closes_without_desynchronizing_next_request(self) -> None:
        self.start(RecordingAdapter())
        assert self.server is not None
        first = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            "X-Crossover-Token: wrong\r\n"
            "Content-Length: 3\r\n"
            "\r\n"
            "bad"
        ).encode("ascii")
        second = (
            f"GET /api/known HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "\r\n"
        ).encode("ascii")
        response = self.raw_exchange(first + second)
        status, headers, _body = self.parse_raw_response(response)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("connection", "").lower(), "close")
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertNotIn(b'"ok":true', response)

    def test_rejected_framing_body_closes_without_desynchronizing_next_request(self) -> None:
        self.start(RecordingAdapter())
        assert self.server is not None
        first = (
            f"POST /api/echo HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "Transfer-Encoding:\r\n"
            "Content-Length: 3\r\n"
            "\r\n"
            "bad"
        ).encode("ascii")
        second = (
            f"GET /api/known HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "\r\n"
        ).encode("ascii")
        response = self.raw_exchange(first + second)
        status, headers, _body = self.parse_raw_response(response)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection", "").lower(), "close")
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertNotIn(b'"ok":true', response)

    def test_lowercase_content_type_is_case_insensitive_to_adapter(self) -> None:
        class HeaderAdapter(RecordingAdapter):
            def handle_api(self, request: ApiRequest) -> ApiResponse | None:
                if request.path != "/api/json":
                    return super().handle_api(request)
                return ApiResponse(
                    200,
                    {
                        "upper": request.headers["Content-Type"],
                        "lower": request.headers["content-type"],
                    },
                )

        self.start(HeaderAdapter())
        assert self.server is not None
        request = (
            f"POST /api/json HTTP/1.1\r\n"
            f"Host: localhost:{self.server.actual_port}\r\n"
            f"X-Crossover-Token: {self.server.token}\r\n"
            "content-type: application/json\r\n"
            "Content-Length: 2\r\n"
            "Connection: close\r\n\r\n"
            "{}"
        ).encode("ascii")
        response = self.raw_exchange(request)
        status, _headers, body = self.parse_raw_response(response)
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"upper": "application/json", "lower": "application/json"},
        )

    def test_empty_transfer_encoding_and_duplicate_lengths_are_rejected(self) -> None:
        self.start(RecordingAdapter())
        assert self.server is not None
        cases = (
            (
                "Transfer-Encoding:\r\n"
                "Content-Length: 0\r\n",
                b"",
            ),
            (
                "Content-Length: 3\r\n"
                "Content-Length: 3\r\n",
                b"abc",
            ),
            (
                "Content-Length: 3\r\n"
                "Content-Length: 4\r\n",
                b"abc",
            ),
        )
        for framing, body in cases:
            with self.subTest(framing=framing):
                request = (
                    f"POST /api/echo HTTP/1.1\r\n"
                    f"Host: localhost:{self.server.actual_port}\r\n"
                    f"X-Crossover-Token: {self.server.token}\r\n"
                    f"{framing}"
                    "Connection: keep-alive\r\n\r\n"
                ).encode("ascii") + body
                response = self.raw_exchange(request)
                status, headers, _response_body = self.parse_raw_response(response)
                self.assertEqual(status, 400)
                self.assertEqual(headers.get("connection", "").lower(), "close")

    def test_api_status_and_content_type_errors(self) -> None:
        self.start(RecordingAdapter())
        status, _headers, _content = self.request(
            "POST", "/api/known", headers=self.api_headers()
        )
        self.assertEqual(status, 405)
        status, _headers, _content = self.request(
            "POST",
            "/api/json",
            headers=self.api_headers(**{"Content-Type": "text/plain"}),
            body=b"{}",
        )
        self.assertEqual(status, 415)
        status, _headers, _content = self.request(
            "GET", "/api/no-such", headers=self.api_headers()
        )
        self.assertEqual(status, 404)

    def test_same_profile_detection_and_conflict(self) -> None:
        first = self.start(StaticAdapter())
        fixed_renderer = replace(self.profile.renderer, port=first.actual_port)
        same_profile = replace(self.profile, renderer=fixed_renderer)
        opened: list[str] = []
        second = BrowserServer(
            same_profile,
            StaticAdapter(),
            home=self.home,
            opener=opened.append,
        )
        self.assertTrue(second.existing)
        second.serve_forever(open_browser=True)
        self.assertEqual(opened, [first.url])
        other_profile = replace(same_profile, id="different-profile")
        with self.assertRaisesRegex(
            OperationalError,
            rf"^Port {first.actual_port} is already in use by another process$",
        ):
            BrowserServer(other_profile, StaticAdapter(), home=self.home)

    def test_context_and_shutdown_hooks_release_port(self) -> None:
        server = self.start(StaticAdapter())
        port = server.actual_port
        server.shutdown()
        assert self.thread is not None
        self.thread.join(timeout=2)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
