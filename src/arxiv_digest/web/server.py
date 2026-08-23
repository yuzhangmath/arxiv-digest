"""Short-lived authenticated HTTP server bound only to IPv4 loopback."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from socket import SHUT_RDWR, socket
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from arxiv_digest.web.api import (
    BACKUP_BODY_LIMIT,
    ApiRequest,
    ApiResponse,
    ApiRouter,
    Handler,
    KnownPaper,
    error_response,
    request_body_limit,
)
from arxiv_digest.web.lifecycle import LifecycleController


LOOPBACK_HOST = "127.0.0.1"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "style-src-attr 'unsafe-inline'; font-src 'self'; connect-src 'self'; "
    "img-src 'none'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
_VIEWS = frozenset(
    {"setup", "review", "calendar", "library", "interests", "settings"}
)
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
_MAX_REQUEST_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_REQUEST_THREADS = 32
_MAX_REQUEST_THREADS = 128
_DEFAULT_MAX_PENDING_BACKUP_INSPECTIONS = 2


@dataclass(frozen=True, slots=True)
class StaticAsset:
    content_type: str
    data: bytes


_STATIC_SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class _DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        *,
        request_timeout: float,
        max_request_threads: int,
    ) -> None:
        self.request_timeout = request_timeout
        self._request_slots = threading.BoundedSemaphore(max_request_threads)
        self._deadline_lock = threading.Lock()
        self._input_deadlines: dict[socket, threading.Timer] = {}
        super().__init__(server_address, request_handler)

    def _expire_request_input(self, request: socket) -> None:
        with self._deadline_lock:
            if request not in self._input_deadlines:
                return
        try:
            request.shutdown(SHUT_RDWR)
        except OSError:
            pass

    def _arm_request_input_deadline(self, request: socket) -> None:
        timer = threading.Timer(
            self.request_timeout,
            self._expire_request_input,
            args=(request,),
        )
        timer.daemon = True
        with self._deadline_lock:
            self._input_deadlines[request] = timer
        timer.start()

    def finish_request_input(self, request: socket) -> None:
        with self._deadline_lock:
            timer = self._input_deadlines.pop(request, None)
        if timer is not None:
            timer.cancel()

    def get_request(self) -> tuple[socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(self.request_timeout)
        return connection, address

    def process_request(
        self,
        request: socket,
        client_address: tuple[str, int],
    ) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        self._arm_request_input_deadline(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.finish_request_input(request)
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.finish_request_input(request)
            self._request_slots.release()


class LoopbackServer:
    def __init__(
        self,
        *,
        handlers: Mapping[str, Handler],
        known_paper: KnownPaper | None = None,
        logger: Callable[[str], None] | None = None,
        static_assets: Mapping[str, StaticAsset] | None = None,
        lifecycle: LifecycleController | None = None,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_request_threads: int = _DEFAULT_MAX_REQUEST_THREADS,
        max_pending_backup_inspections: int = (
            _DEFAULT_MAX_PENDING_BACKUP_INSPECTIONS
        ),
        max_pending_backup_bytes: int = BACKUP_BODY_LIMIT,
    ) -> None:
        if (
            not isfinite(request_timeout)
            or request_timeout <= 0
            or request_timeout > _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "request timeout must be finite and between 0 and 60 seconds"
            )
        if (
            type(max_request_threads) is not int
            or max_request_threads < 1
            or max_request_threads > _MAX_REQUEST_THREADS
        ):
            raise ValueError(
                "maximum request threads must be between 1 and 128"
            )
        if (
            type(max_pending_backup_inspections) is not int
            or max_pending_backup_inspections < 1
            or max_pending_backup_inspections
            > _DEFAULT_MAX_PENDING_BACKUP_INSPECTIONS
        ):
            raise ValueError(
                "pending backup inspections must be between 1 and 2"
            )
        if (
            type(max_pending_backup_bytes) is not int
            or max_pending_backup_bytes < 1
            or max_pending_backup_bytes > BACKUP_BODY_LIMIT
        ):
            raise ValueError(
                "pending backup bytes must be between 1 and the backup body limit"
            )
        self.host = LOOPBACK_HOST
        self.port = 0
        self.token = secrets.token_urlsafe(32)
        self.startup_nonce = secrets.token_urlsafe(24)
        self.lifecycle = lifecycle or LifecycleController()
        self._handlers = dict(handlers)
        status_handler = self._handlers.get("status")

        def status(payload: dict[str, Any]) -> dict[str, Any]:
            supplied = {} if status_handler is None else status_handler(payload)
            if not isinstance(supplied, Mapping):
                raise TypeError("status handler must return a mapping")
            return {**supplied, "startup_nonce": self.startup_nonce}

        self._handlers["status"] = status
        self._handlers["tabs_connect"] = self._connect_tab
        self._handlers["tabs_heartbeat"] = self._heartbeat_tab
        self._handlers["tabs_disconnect"] = self._disconnect_tab
        self._handlers["application_quit"] = self._request_quit
        self._known_paper = known_paper
        self._logger = logger or (lambda message: None)
        self._static_assets = dict(static_assets or {})
        if any(
            not path.startswith("/")
            or path.endswith("/")
            or ".." in path.split("/")
            for path in self._static_assets
        ):
            raise ValueError("static asset paths must form an explicit allowlist")
        self._httpd: _DashboardHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._request_timeout = float(request_timeout)
        self._max_request_threads = max_request_threads
        self._max_pending_backup_inspections = max_pending_backup_inspections
        self._max_pending_backup_bytes = max_pending_backup_bytes
        self._backup_body_lock = threading.Lock()
        self._pending_backup_inspections = 0
        self._pending_backup_bytes = 0

    def _reserve_backup_body(self, byte_count: int) -> bool:
        with self._backup_body_lock:
            if (
                self._pending_backup_inspections
                >= self._max_pending_backup_inspections
                or self._pending_backup_bytes + byte_count
                > self._max_pending_backup_bytes
            ):
                return False
            self._pending_backup_inspections += 1
            self._pending_backup_bytes += byte_count
            return True

    def _release_backup_body(self, byte_count: int) -> None:
        with self._backup_body_lock:
            if (
                self._pending_backup_inspections < 1
                or byte_count < 0
                or byte_count > self._pending_backup_bytes
            ):
                raise RuntimeError("backup body admission accounting is invalid")
            self._pending_backup_inspections -= 1
            self._pending_backup_bytes -= byte_count

    def _connect_tab(self, payload: dict[str, Any]) -> dict[str, bool]:
        self.lifecycle.connect(payload["tab_id"])
        return {"connected": True}

    def _heartbeat_tab(self, payload: dict[str, Any]) -> dict[str, bool]:
        self.lifecycle.heartbeat(payload["tab_id"])
        return {"connected": True}

    def _disconnect_tab(self, payload: dict[str, Any]) -> dict[str, bool]:
        self.lifecycle.disconnect(payload["tab_id"])
        return {"connected": False}

    def _request_quit(self, payload: dict[str, Any]) -> dict[str, bool]:
        self.lifecycle.request_quit()
        return {"quitting": True}

    def _request_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                # One request per local connection keeps the total input
                # deadline unambiguous and avoids unread-body reuse.
                self.close_connection = True
                if not self.path.startswith("/api/v1"):
                    assert owner._httpd is not None
                    owner._httpd.finish_request_input(self.connection)
                    self._handle_static()
                    return
                assert owner._httpd is not None
                host = f"{owner.host}:{owner.port}"
                headers = {key: value for key, value in self.headers.items()}
                router = ApiRouter(
                    token=owner.token,
                    host=host,
                    handlers=owner._handlers,
                    known_paper=owner._known_paper,
                )
                framing_request = ApiRequest(
                    method=self.command,
                    target=self.path,
                    headers=headers,
                )
                preflight_error = router.preflight(framing_request)
                if preflight_error is not None:
                    self.close_connection = True
                    self._send(preflight_error)
                    return
                transfer_encoding = self.headers.get("Transfer-Encoding")
                length_text = self.headers.get("Content-Length", "0")
                try:
                    length = int(length_text)
                except ValueError:
                    length = -1
                if transfer_encoding is not None:
                    self.close_connection = True
                    self._send(
                        error_response(
                            400,
                            "unsupported_transfer_encoding",
                            "Transfer-Encoding is not supported.",
                        )
                    )
                    return
                if length < 0 or str(length) != length_text:
                    self.close_connection = True
                    self._send(
                        error_response(
                            400,
                            "invalid_content_length",
                            "Content-Length must be a nonnegative decimal integer.",
                        )
                    )
                    return
                if length > request_body_limit(self.command, self.path):
                    self.close_connection = True
                    self._send(
                        error_response(
                            413,
                            "body_too_large",
                            "The request body exceeds this route's limit.",
                        )
                    )
                    return
                is_backup_inspection = (
                    self.command == "POST"
                    and urlsplit(self.path).path
                    == "/api/v1/backup/inspect"
                )
                reserved_backup_bytes: int | None = None
                if is_backup_inspection:
                    if not owner._reserve_backup_body(length):
                        self.close_connection = True
                        self._send(
                            error_response(
                                429,
                                "backup_capacity",
                                "Another backup inspection is already in progress.",
                            )
                        )
                        return
                    reserved_backup_bytes = length
                try:
                    body = self.rfile.read(length) if length else b""
                    if len(body) != length:
                        self.close_connection = True
                        self._send(
                            error_response(
                                400,
                                "incomplete_body",
                                "The request body ended before Content-Length.",
                            )
                        )
                        return
                    owner._httpd.finish_request_input(self.connection)
                    with owner.lifecycle.transaction():
                        response = router.dispatch(
                            ApiRequest(
                                method=self.command,
                                target=self.path,
                                headers=headers,
                                body=body,
                            )
                        )
                        self._send(response)
                finally:
                    if reserved_backup_bytes is not None:
                        owner._release_backup_body(reserved_backup_bytes)
                owner._logger(f"{self.command} {self.path.split('?', 1)[0]}")

            def _handle_static(self) -> None:
                host = f"{owner.host}:{owner.port}"
                if self.headers.get("Host") != host:
                    self._send_static(400, b"Invalid host.\n", "text/plain")
                    return
                if self.command not in {"GET", "HEAD"}:
                    self._send_static(
                        405,
                        b"Method not allowed.\n",
                        "text/plain",
                        {"Allow": "GET, HEAD"},
                    )
                    return
                split = urlsplit(self.path)
                asset_path = "/index.html" if split.path == "/" else split.path
                query_pairs = parse_qsl(split.query, keep_blank_values=True)
                query_allowed = not split.query or (
                    asset_path == "/index.html"
                    and len(query_pairs) == 1
                    and query_pairs[0][0] == "view"
                    and query_pairs[0][1] in _VIEWS
                )
                asset = (
                    None
                    if not query_allowed or split.fragment
                    else owner._static_assets.get(asset_path)
                )
                if asset is None:
                    self._send_static(404, b"Not found.\n", "text/plain")
                    return
                self._send_static(200, asset.data, asset.content_type)
                owner._logger(f"{self.command} {split.path}")

            def _send_static(
                self,
                status: int,
                body: bytes,
                content_type: str,
                extra_headers: Mapping[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for name, value in _STATIC_SECURITY_HEADERS.items():
                    self.send_header(name, value)
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _send(self, response: ApiResponse) -> None:
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response.body)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle
            do_OPTIONS = _handle
            do_HEAD = _handle
            do_TRACE = _handle
            do_CONNECT = _handle

            def log_message(self, format: str, *args: Any) -> None:
                return None

        return RequestHandler

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("loopback server is already started")
        self._stopped.clear()
        self._httpd = _DashboardHttpServer(
            (self.host, 0),
            self._request_handler(),
            request_timeout=self._request_timeout,
            max_request_threads=self._max_request_threads,
        )
        address = self._httpd.server_address
        self.port = int(address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="arxiv-digest-loopback",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def wait(self, *, poll_interval: float = 0.25) -> None:
        """Wait until lifecycle policy requests a graceful server stop."""

        if self._httpd is None:
            raise RuntimeError("loopback server has not started")
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        while not self.lifecycle.should_stop():
            if self._stopped.wait(poll_interval):
                return

    def launch_url(self, view: str) -> str:
        if view not in _VIEWS:
            raise ValueError("unsupported dashboard view")
        if self.port < 1:
            raise RuntimeError("loopback server has not started")
        return (
            f"http://{self.host}:{self.port}/"
            f"#token={self.token}&view={view}"
        )
