"""Shared, authenticated browser host for CrossOver game profiles.

The host deliberately owns the HTTP boundary: adapters only see decoded API
requests after Host/Origin/token checks and a bounded body read.  Static files
are resolved beneath the profile's installed renderer directory and are never
served through the current working directory.
"""

from __future__ import annotations

import errno
import http.client
import http.server
import json
import math
import mimetypes
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .errors import OperationalError
from .profiles import GameProfile, ResolvedProfile, resolve_profile


class _ImmutableHeaders(Mapping[str, str]):
    """Immutable, case-insensitive HTTP headers exposed to adapters."""

    def __init__(self, headers: Mapping[str, Any] | Any) -> None:
        values: dict[str, str] = {}
        items = headers.items() if hasattr(headers, "items") else headers
        for key, value in items:
            values[str(key).lower()] = str(value)
        self._values = MappingProxyType(values)

    def __getitem__(self, key: str) -> str:
        return self._values[str(key).lower()]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class ApiRequest:
    """An authenticated API request delivered to a game adapter."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not isinstance(self.path, str):
            raise TypeError("ApiRequest method and path must be strings")
        if not isinstance(self.body, bytes):
            raise TypeError("ApiRequest body must be bytes")
        # HTTPMessage is mutable and would otherwise let an adapter mutate the
        # request after dispatch.  Normalize names while retaining an immutable
        # case-insensitive mapping for adapter lookups.
        object.__setattr__(self, "headers", _ImmutableHeaders(self.headers))


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """A JSON response produced by an adapter."""

    status: int
    body: Any


@runtime_checkable
class BrowserAdapter(Protocol):
    """The exact Python adapter ABI used by :class:`BrowserServer`."""

    def before_start(self) -> None:
        """Perform adapter startup work before opening the browser."""

    def bridge_script(self, token: str) -> str:
        """Return JavaScript source injected into the renderer index."""

    def handle_api(self, request: ApiRequest) -> ApiResponse | None:
        """Handle an authenticated API route, or return ``None`` if unknown."""


class StaticAdapter:
    """A no-op adapter for profiles that only need static browser assets."""

    def before_start(self) -> None:
        return None

    def bridge_script(self, token: str) -> str:
        return ""

    def handle_api(self, request: ApiRequest) -> ApiResponse | None:
        return None


_MODULE_SCRIPT_RE = re.compile(
    r"<script\b[^>]*(?:\s+)type\s*=\s*(?:(['\"])module\1|module)(?:\s|>)",
    re.IGNORECASE,
)


class _BrowserRequestHandler(http.server.BaseHTTPRequestHandler):
    """Small request-handler shim delegating all policy to its BrowserServer."""

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        # Bound header parsing and body reads even when a client dribbles bytes.
        try:
            self.connection.settimeout(BrowserServer._request_timeout_seconds())
        except (OSError, OverflowError, ValueError):
            # A failed timeout setup must not leave the accepted socket alive
            # without a handler or a finite read deadline.
            try:
                self.connection.close()
            except OSError:
                pass
            raise

    def _owner(self) -> "BrowserServer":
        return self.server.browser_owner  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._owner()._dispatch(self, "GET")

    def do_HEAD(self) -> None:
        self._owner()._dispatch(self, "HEAD")

    def do_POST(self) -> None:
        self._owner()._dispatch(self, "POST")

    def do_PUT(self) -> None:
        self._owner()._dispatch(self, "PUT")

    def do_PATCH(self) -> None:
        self._owner()._dispatch(self, "PATCH")

    def do_DELETE(self) -> None:
        self._owner()._dispatch(self, "DELETE")

    def do_OPTIONS(self) -> None:
        self._owner()._dispatch(self, "OPTIONS")

    def log_message(self, format: str, *args: object) -> None:
        # The game server is intentionally quiet.  Applications may replace
        # this handler when embedding BrowserServer if request logging is wanted.
        return None


class BrowserServer:
    """Authenticated static/API HTTP server for one :class:`GameProfile`.

    Binding occurs in the constructor so callers can inspect ``url`` and use
    the server's actual ephemeral port before starting a serving thread.  The
    injected ``server`` may be a ``ThreadingHTTPServer``-compatible factory or
    an already-created compatible server object, which keeps port-0 tests and
    embedding straightforward.
    """

    DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
    READ_CHUNK_BYTES = 64 * 1024
    REQUEST_TIMEOUT_SECONDS = 10.0
    BODY_READ_TIMEOUT_SECONDS = 10.0
    PROBE_TIMEOUT_SECONDS = 0.6
    HEALTH_RESPONSE_MAX_BYTES = 64 * 1024

    def __init__(
        self,
        profile: GameProfile,
        adapter: BrowserAdapter,
        *,
        home: Path | None = None,
        opener: Callable[[str], Any] | None = None,
        server: Any | None = None,
    ) -> None:
        self.profile = profile
        self.adapter = adapter
        self.resolved: ResolvedProfile = resolve_profile(profile, home or Path.home())
        self.assets = (self.resolved.install / profile.renderer.assets).resolve()
        self.token = secrets.token_urlsafe(32)
        self._opener = webbrowser.open if opener is None else opener
        self._server: Any | None = None
        self._serving = False
        self._closed = False
        self._existing = False
        self._started = False
        self._lock = threading.Lock()

        server_factory_or_instance = (
            http.server.ThreadingHTTPServer if server is None else server
        )
        try:
            if self._is_server_instance(server_factory_or_instance):
                self._server = server_factory_or_instance
            else:
                self._server = server_factory_or_instance(
                    (profile.renderer.bind_host, profile.renderer.port),
                    _BrowserRequestHandler,
                )
            # ThreadingHTTPServer already sets this, but setting it on an
            # injected compatible server makes the lifecycle guarantee
            # explicit and harmless for test doubles.
            try:
                self._server.daemon_threads = True
            except (AttributeError, TypeError):
                pass
            self._server.browser_owner = self
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            if self._probe_existing(profile.renderer.port):
                self._existing = True
            else:
                raise OperationalError(
                    f"Port {profile.renderer.port} is already in use by another process"
                ) from exc

        self.actual_port = (
            profile.renderer.port
            if self._existing
            else self._extract_port(self._server.server_address)
        )
        self.url = f"http://{profile.renderer.browser_host}:{self.actual_port}"
        self._allowed_hosts = {
            f"{profile.renderer.browser_host}:{self.actual_port}",
            f"{profile.renderer.bind_host}:{self.actual_port}",
        }
        self._allowed_origin = f"http://{profile.renderer.browser_host}:{self.actual_port}"
        if self.actual_port == 80:
            # HTTP's canonical default port is omitted by browsers in Host and
            # Origin.  Never accept the portless form on any other listener.
            self._allowed_hosts.update(
                {profile.renderer.browser_host, profile.renderer.bind_host}
            )
            self._allowed_origin = f"http://{profile.renderer.browser_host}"
        self._allowed_hosts = frozenset(self._allowed_hosts)
        self.max_request_bytes = self._configured_max_request_bytes(profile)

    @staticmethod
    def _is_server_instance(value: Any) -> bool:
        # Classes and factories are callable; an injected live server is not.
        return not callable(value) and hasattr(value, "serve_forever")

    @staticmethod
    def _extract_port(server_address: Any) -> int:
        try:
            port = int(server_address[1])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise OperationalError("browser server did not expose a TCP port") from exc
        return port

    @classmethod
    def _request_timeout_seconds(cls) -> float:
        """Return a finite, positive socket timeout for request handling."""

        try:
            timeout = float(cls.REQUEST_TIMEOUT_SECONDS)
        except (TypeError, ValueError, OverflowError):
            return 10.0
        if not math.isfinite(timeout) or timeout <= 0:
            return 10.0
        return timeout

    @classmethod
    def _body_timeout_seconds(cls) -> float:
        """Return a finite body deadline; zero is useful for deterministic tests."""

        try:
            timeout = float(cls.BODY_READ_TIMEOUT_SECONDS)
        except (TypeError, ValueError, OverflowError):
            return cls._request_timeout_seconds()
        if not math.isfinite(timeout) or timeout < 0:
            return cls._request_timeout_seconds()
        return timeout

    @staticmethod
    def _restore_request_timeout(connection: Any, timeout: float) -> bool:
        if connection is None:
            return True
        try:
            connection.settimeout(timeout)
        except (OSError, OverflowError, ValueError):
            return False
        return True

    @classmethod
    def _saved_request_timeout(cls, connection: Any) -> float:
        """Capture a usable prior timeout, falling back to the finite default."""

        fallback = cls._request_timeout_seconds()
        if connection is None or not hasattr(connection, "gettimeout"):
            return fallback
        try:
            current = connection.gettimeout()
            current_float = float(current)
        except (TypeError, ValueError, OverflowError, OSError):
            return fallback
        if not math.isfinite(current_float) or current_float <= 0:
            return fallback
        return current_float

    @classmethod
    def _configured_max_request_bytes(cls, profile: GameProfile) -> int:
        value = profile.adapter_config.get(
            "max_request_bytes", cls.DEFAULT_MAX_REQUEST_BYTES
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            # Profile validation intentionally leaves adapter-specific settings
            # to the adapter.  The host remains bounded even for a malformed
            # external setting rather than accepting an unlimited request.
            return cls.DEFAULT_MAX_REQUEST_BYTES
        return value

    @staticmethod
    def _read_once(stream: Any, amount: int) -> bytes:
        """Read at most ``amount`` without waiting for a full buffer.

        ``read1`` is the buffered-stream API for one underlying read.  The
        HTTP response and request streams in the supported runtimes expose it,
        but small test doubles and alternate wrappers may not.  Their ordinary
        bounded ``read`` is the safe fallback: it never receives an unbounded
        request, even though the wrapper may need more than one recv internally.
        """

        if amount <= 0:
            return b""
        read1 = getattr(stream, "read1", None)
        if callable(read1):
            return read1(amount)
        return stream.read(amount)

    @staticmethod
    def _prepare_socket_read(
        connection: Any,
        deadline: float,
        *,
        timeout_cap: float | None = None,
    ) -> bool:
        """Apply the remaining monotonic deadline before one network read."""

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if connection is not None:
            timeout = remaining if timeout_cap is None else min(remaining, timeout_cap)
            connection.settimeout(timeout)
        return True

    @property
    def server(self) -> Any | None:
        """The underlying HTTP server, exposed for embedding/tests."""

        return self._server

    @property
    def port(self) -> int:
        """The bound TCP port, including an ephemeral port-0 allocation."""

        return self.actual_port

    @property
    def existing(self) -> bool:
        """Whether this instance is reusing an already-running same profile."""

        return self._existing

    def _probe_existing(self, port: int) -> bool:
        if not port:
            return False
        host = self.profile.renderer.browser_host
        expected_host = f"{host}:{port}"
        connection: http.client.HTTPConnection | None = None
        deadline = time.monotonic() + self.PROBE_TIMEOUT_SECONDS
        try:
            connection = http.client.HTTPConnection(
                host, port, timeout=self.PROBE_TIMEOUT_SECONDS
            )
            connection.request(
                "GET",
                "/health",
                headers={"Host": expected_host, "Connection": "close"},
            )
            if not self._prepare_socket_read(
                getattr(connection, "sock", None), deadline
            ):
                return False
            response = connection.getresponse()
            if response.status != 200:
                return False
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                if not re.fullmatch(r"[0-9]+", content_length.strip()):
                    return False
                if int(content_length, 10) > self.HEALTH_RESPONSE_MAX_BYTES:
                    return False
            payload = bytearray()
            while len(payload) <= self.HEALTH_RESPONSE_MAX_BYTES:
                if not self._prepare_socket_read(
                    getattr(connection, "sock", None), deadline
                ):
                    return False
                chunk = self._read_once(
                    response,
                    self.HEALTH_RESPONSE_MAX_BYTES + 1 - len(payload),
                )
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > self.HEALTH_RESPONSE_MAX_BYTES:
                    return False
            decoded = bytes(payload).decode("utf-8")
            return json.loads(decoded) == {
                "ok": True,
                "service": self.profile.id,
                "host": self.profile.renderer.bind_host,
                "port": port,
            }
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            http.client.IncompleteRead,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def serve_forever(self, open_browser: bool = True) -> None:
        """Run the server until :meth:`shutdown` is called.

        A same-profile listener is not started a second time.  It is optionally
        opened and this method returns successfully, allowing a second launch
        command to behave like the first without replacing the existing host.
        """

        if self._existing:
            if open_browser:
                self._opener(self.url)
            return
        if self._server is None:
            raise OperationalError("browser server is not available")
        with self._lock:
            if not self._started:
                self.adapter.before_start()
                self._started = True
            self._serving = True
        try:
            if open_browser:
                self._opener(self.url)
            self._server.serve_forever()
        finally:
            with self._lock:
                self._serving = False

    def shutdown(self) -> None:
        """Stop serving and close the listener; safe for port-0 test cleanup."""

        if self._existing or self._server is None or self._closed:
            return
        with self._lock:
            serving = self._serving
            self._closed = True
        if serving:
            self._server.shutdown()
        self._server.server_close()

    def server_close(self) -> None:
        self.shutdown()

    def close(self) -> None:
        self.shutdown()

    def __enter__(self) -> "BrowserServer":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.shutdown()

    @staticmethod
    def _raw_path(request_handler: _BrowserRequestHandler) -> str:
        return urllib.parse.urlsplit(request_handler.path).path or "/"

    @staticmethod
    def _decode_path(raw_path: str) -> str:
        try:
            decoded = urllib.parse.unquote(raw_path, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise ValueError("invalid URL path") from exc
        if not decoded.startswith("/") or "\x00" in decoded or "\\" in decoded:
            raise ValueError("invalid URL path")
        if any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("path traversal is not allowed")
        return decoded

    def _resolve_static(self, decoded_path: str) -> Path:
        relative = "index.html" if decoded_path in {"/", "/index.html"} else decoded_path[1:]
        candidate = (self.assets / relative).resolve()
        try:
            candidate.relative_to(self.assets)
        except ValueError as exc:
            raise ValueError("path escapes asset root") from exc
        if not candidate.is_file():
            raise FileNotFoundError(decoded_path)
        return candidate

    @staticmethod
    def _mime_type(path: Path) -> str:
        suffix = path.suffix.lower()
        text_types = {
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".htm": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".map": "application/json; charset=utf-8",
            ".xml": "application/xml; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".svg": "image/svg+xml",
            ".webmanifest": "application/manifest+json; charset=utf-8",
        }
        binary_types = {
            ".avif": "image/avif",
            ".gif": "image/gif",
            ".ico": "image/x-icon",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
            ".png": "image/png",
            ".wav": "audio/wav",
            ".wasm": "application/wasm",
            ".webm": "video/webm",
            ".webp": "image/webp",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }
        if suffix in text_types:
            return text_types[suffix]
        if suffix in binary_types:
            return binary_types[suffix]
        guessed, _encoding = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    @staticmethod
    def _close_connection(request_handler: _BrowserRequestHandler) -> None:
        setattr(request_handler, "close_connection", True)

    def _send_json(
        self,
        request_handler: _BrowserRequestHandler,
        status: int,
        body: Any,
        *,
        head: bool = False,
        max_bytes: int | None = None,
    ) -> None:
        # These statuses can represent a rejection before a request body was
        # safely consumed.  Closing avoids letting bytes from a malformed
        # request become the next keep-alive request's request line.
        if status in {400, 401, 403, 405, 413, 415}:
            self._close_connection(request_handler)
        try:
            content = json.dumps(
                body,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if max_bytes is not None and len(content) > max_bytes:
                status = 500
                self._close_connection(request_handler)
                content = b'{"error":"Internal server error"}'
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            status = 500
            self._close_connection(request_handler)
            content = b'{"error":"Internal server error"}'
        request_handler.send_response(status)
        request_handler.send_header("Content-Type", "application/json; charset=utf-8")
        request_handler.send_header("Content-Length", str(len(content)))
        if getattr(request_handler, "close_connection", False):
            request_handler.send_header("Connection", "close")
        request_handler.end_headers()
        if not head:
            request_handler.wfile.write(content)

    def _send_error(
        self,
        request_handler: _BrowserRequestHandler,
        status: int,
        message: str,
        *,
        head: bool = False,
        close: bool = False,
    ) -> None:
        if close:
            self._close_connection(request_handler)
        self._send_json(request_handler, status, {"error": message}, head=head)

    def _authorize_api(
        self,
        request_handler: _BrowserRequestHandler,
        *,
        head: bool = False,
    ) -> bool:
        host = request_handler.headers.get("Host")
        if host not in self._allowed_hosts:
            self._send_error(
                request_handler, 403, "Invalid Host", head=head, close=True
            )
            return False
        origin = request_handler.headers.get("Origin")
        if origin is not None and origin != self._allowed_origin:
            self._send_error(
                request_handler, 403, "Invalid Origin", head=head, close=True
            )
            return False
        presented = request_handler.headers.get("X-Crossover-Token", "")
        if not secrets.compare_digest(presented, self.token):
            self._send_error(
                request_handler, 401, "Unauthorized", head=head, close=True
            )
            return False
        return True

    def _read_body(
        self,
        request_handler: _BrowserRequestHandler,
        *,
        head: bool = False,
    ) -> bytes | None:
        connection = getattr(request_handler, "connection", None)
        restore_timeout = self._saved_request_timeout(connection)

        def reject(status: int, message: str) -> None:
            # Body reads temporarily shorten the socket deadline.  Restore the
            # normal finite timeout before writing the response; otherwise a
            # timeout that fired while reading can also abort the 400 write.
            self._restore_request_timeout(connection, restore_timeout)
            self._send_error(request_handler, status, message, head=head, close=True)

        headers = request_handler.headers
        # get_all distinguishes an absent field from an explicitly empty one;
        # any Transfer-Encoding field is unsupported, including duplicates.
        transfer_encoding = headers.get_all("Transfer-Encoding")
        if transfer_encoding is not None:
            reject(400, "Unsupported Transfer-Encoding")
            return None

        content_lengths = headers.get_all("Content-Length")
        if content_lengths is None:
            return b""
        if len(content_lengths) != 1:
            reject(400, "Invalid Content-Length")
            return None
        length_header = content_lengths[0].strip()
        if not re.fullmatch(r"[0-9]+", length_header):
            reject(400, "Invalid Content-Length")
            return None
        try:
            length = int(length_header, 10)
        except ValueError:
            reject(400, "Invalid Content-Length")
            return None
        if length > self.max_request_bytes:
            reject(413, "Request body too large")
            return None

        body = bytearray()
        remaining = length
        deadline = time.monotonic() + self._body_timeout_seconds()
        while remaining:
            try:
                if not self._prepare_socket_read(
                    connection,
                    deadline,
                    timeout_cap=self._request_timeout_seconds(),
                ):
                    reject(400, "Incomplete request body")
                    return None
                chunk = self._read_once(
                    request_handler.rfile,
                    min(self.READ_CHUNK_BYTES, remaining),
                )
            except (socket.timeout, TimeoutError, http.client.IncompleteRead, OSError):
                reject(400, "Unable to read request body")
                return None
            if not chunk or len(chunk) > remaining:
                reject(400, "Incomplete request body")
                return None
            body.extend(chunk)
            remaining -= len(chunk)

        self._restore_request_timeout(connection, restore_timeout)
        return bytes(body)

    def _serve_static(
        self,
        request_handler: _BrowserRequestHandler,
        decoded_path: str,
        *,
        head: bool,
    ) -> None:
        try:
            path = self._resolve_static(decoded_path)
            content = path.read_bytes()
        except ValueError as exc:
            self._send_error(request_handler, 400, str(exc), head=head)
            return
        except (FileNotFoundError, NotADirectoryError):
            self._send_error(request_handler, 404, "Not found", head=head)
            return
        except OSError:
            self._send_error(request_handler, 404, "Not found", head=head)
            return

        if decoded_path in {"/", "/index.html"}:
            try:
                html = content.decode("utf-8")
            except UnicodeDecodeError:
                self._send_error(request_handler, 404, "Not found", head=head)
                return
            try:
                bridge = self.adapter.bridge_script(self.token)
                if bridge:
                    match = _MODULE_SCRIPT_RE.search(html)
                    if match:
                        injection = f"<script>\n{bridge}\n</script>\n"
                        html = html[: match.start()] + injection + html[match.start() :]
                        content = html.encode("utf-8")
            except Exception:
                self._send_error(
                    request_handler,
                    500,
                    "Internal server error",
                    head=head,
                    close=True,
                )
                return

        request_handler.send_response(200)
        request_handler.send_header("Content-Type", self._mime_type(path))
        request_handler.send_header("Content-Length", str(len(content)))
        request_handler.end_headers()
        if not head:
            request_handler.wfile.write(content)

    def _dispatch(self, request_handler: _BrowserRequestHandler, method: str) -> None:
        raw_path = self._raw_path(request_handler)
        is_api = raw_path.startswith("/api/")
        if is_api and not self._authorize_api(
            request_handler, head=method == "HEAD"
        ):
            return

        try:
            decoded_path = self._decode_path(raw_path)
        except ValueError as exc:
            self._send_error(
                request_handler,
                400,
                str(exc),
                head=method == "HEAD",
                close=True,
            )
            return

        if decoded_path == "/health":
            if method not in {"GET", "HEAD"}:
                self._send_error(
                    request_handler,
                    405,
                    "Method not allowed",
                    head=method == "HEAD",
                    close=True,
                )
                return
            if self._read_body(request_handler, head=method == "HEAD") is None:
                return
            self._send_json(
                request_handler,
                200,
                {
                    "ok": True,
                    "service": self.profile.id,
                    "host": self.profile.renderer.bind_host,
                    "port": self.actual_port,
                },
                head=method == "HEAD",
                max_bytes=self.HEALTH_RESPONSE_MAX_BYTES,
            )
            return

        if is_api:
            body = self._read_body(request_handler, head=method == "HEAD")
            if body is None:
                return
            headers = _ImmutableHeaders(request_handler.headers)
            request = ApiRequest(method, decoded_path, headers, body)
            try:
                response = self.adapter.handle_api(request)
            except Exception:
                self._send_error(
                    request_handler,
                    500,
                    "Internal server error",
                    head=method == "HEAD",
                )
                return
            if response is None:
                self._send_error(request_handler, 404, "Not found", head=method == "HEAD")
                return
            if not isinstance(response, ApiResponse):
                self._send_error(
                    request_handler,
                    500,
                    "Internal server error",
                    head=method == "HEAD",
                )
                return
            self._send_json(
                request_handler,
                response.status,
                response.body,
                head=method == "HEAD",
            )
            return

        if method not in {"GET", "HEAD"}:
            self._send_error(
                request_handler,
                405,
                "Method not allowed",
                head=method == "HEAD",
                close=True,
            )
            return
        if self._read_body(request_handler, head=method == "HEAD") is None:
            return
        self._serve_static(request_handler, decoded_path, head=method == "HEAD")


__all__ = [
    "ApiRequest",
    "ApiResponse",
    "BrowserAdapter",
    "StaticAdapter",
    "BrowserServer",
]
