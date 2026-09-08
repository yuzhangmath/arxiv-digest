from __future__ import annotations

import base64
import http.client
import json
import socket
import time
from threading import Event, Thread

import pytest
from urllib.parse import urlsplit


def _decode_token(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def test_server_binds_ephemeral_ipv4_loopback_with_fresh_256_bit_token() -> None:
    from arxiv_digest.web.server import LoopbackServer

    first = LoopbackServer(handlers={"status": lambda payload: {"state": "ready"}})
    second = LoopbackServer(handlers={})
    assert len(_decode_token(first.token)) == 32
    assert first.token != second.token

    first.start()
    try:
        assert first.host == "127.0.0.1"
        assert first.port > 0
        connection = http.client.HTTPConnection(first.host, first.port, timeout=2)
        connection.request(
            "GET",
            "/api/v1/status",
            headers={
                "Host": f"127.0.0.1:{first.port}",
                "Authorization": f"Bearer {first.token}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 200
        assert payload["data"] == {
            "state": "ready",
            "startup_nonce": first.startup_nonce,
        }
    finally:
        first.stop()


@pytest.mark.parametrize("method,target,authorized,expected_status", [
    ("GET", "/api/v1/status", True, 200),
    ("GET", "/api/v1/status", False, 401),
    ("POST", "/api/v1/update/receipt/" + "c" * 64 + "/ack", True, 500),
    ("GET", "/index.html", False, 200),
    ("GET", "/missing.html", False, 404),
])
def test_single_request_connections_advertise_close_before_client_reuse(
    method, target, authorized, expected_status,
) -> None:
    from arxiv_digest.web.server import LoopbackServer, StaticAsset

    def failed_acknowledgement(_payload):
        raise RuntimeError("synthetic failed acknowledgement")

    server = LoopbackServer(
        handlers={"status": lambda _: {"state": "ready"}, "update_receipt_ack": failed_acknowledgement},
        static_assets={"/index.html": StaticAsset(content_type="text/html", data=b"synthetic dashboard")},
    )
    server.start()
    connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
    try:
        headers = {"Host": f"{server.host}:{server.port}"}
        if authorized:
            headers["Authorization"] = f"Bearer {server.token}"
        if method == "POST":
            headers.update(Origin=f"http://{server.host}:{server.port}", **{"Content-Type": "application/json"})
        connection.request(method, target, body=b"{}" if method == "POST" else None, headers=headers)
        response = connection.getresponse()
        assert response.status == expected_status
        assert response.version == 11
        assert response.getheader("Connection") == "close"
        response.read()
        assert response.will_close
        assert connection.sock is None
        # The same HTTP client must open a fresh transport for its next request.
        connection.request("GET", "/api/v1/status", headers={
            "Host": f"{server.host}:{server.port}", "Authorization": f"Bearer {server.token}",
        })
        following = connection.getresponse()
        assert following.status == 200
        assert json.loads(following.read())["data"]["state"] == "ready"
    finally:
        connection.close()
        server.stop()


@pytest.mark.parametrize("incomplete_part", ["connection", "headers", "body"])
def test_incomplete_requests_are_closed_after_the_server_deadline(
    incomplete_part: str,
) -> None:
    from arxiv_digest.web.server import LoopbackServer

    server = LoopbackServer(handlers={}, request_timeout=0.1)
    server.start()
    try:
        connection = socket.create_connection((server.host, server.port), timeout=1)
        connection.settimeout(1)
        if incomplete_part == "headers":
            connection.sendall(b"GET / HTTP/1.1\r\nHost:")
        elif incomplete_part == "body":
            connection.sendall(
                (
                    "POST /api/v1/sync/start HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    f"Authorization: Bearer {server.token}\r\n"
                    f"Origin: http://{server.host}:{server.port}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 10\r\n\r\n{"
                ).encode("ascii")
            )

        assert connection.recv(1) == b""
        connection.close()
    finally:
        server.stop()


def test_excess_connections_do_not_spawn_unbounded_request_threads() -> None:
    from arxiv_digest.web.server import LoopbackServer

    first_entered = Event()
    release_first = Event()

    def status(_payload: dict) -> dict[str, str]:
        first_entered.set()
        assert release_first.wait(timeout=2)
        return {"state": "ready"}

    server = LoopbackServer(
        handlers={"status": status},
        max_request_threads=1,
    )
    server.start()
    first_errors: list[BaseException] = []

    def first_request() -> None:
        try:
            connection = http.client.HTTPConnection(
                server.host,
                server.port,
                timeout=2,
            )
            connection.request(
                "GET",
                "/api/v1/status",
                headers={
                    "Host": f"{server.host}:{server.port}",
                    "Authorization": f"Bearer {server.token}",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        except BaseException as error:  # pragma: no cover - reported below
            first_errors.append(error)

    thread = Thread(target=first_request)
    thread.start()
    try:
        assert first_entered.wait(timeout=1)
        excess = socket.create_connection((server.host, server.port), timeout=1)
        excess.settimeout(1)
        excess.sendall(
            (
                "GET /api/v1/status HTTP/1.1\r\n"
                f"Host: {server.host}:{server.port}\r\n"
                f"Authorization: Bearer {server.token}\r\n\r\n"
            ).encode("ascii")
        )

        try:
            closed = excess.recv(1)
        except ConnectionResetError:
            closed = b""
        assert closed == b""
        excess.close()
    finally:
        release_first.set()
        thread.join(timeout=2)
        server.stop()

    assert not thread.is_alive()
    assert first_errors == []


def test_backup_admission_rejects_before_reading_an_excess_body() -> None:
    from arxiv_digest.web.server import LoopbackServer

    first_entered = Event()
    release_first = Event()

    def inspect(_payload: dict) -> dict[str, bool]:
        first_entered.set()
        assert release_first.wait(timeout=2)
        return {"inspected": True}

    server = LoopbackServer(
        handlers={"backup_inspect": inspect},
        max_pending_backup_inspections=1,
    )
    server.start()
    first_errors: list[BaseException] = []

    def first_request() -> None:
        try:
            connection = http.client.HTTPConnection(
                server.host,
                server.port,
                timeout=2,
            )
            connection.request(
                "POST",
                "/api/v1/backup/inspect",
                body=b"first",
                headers={
                    "Host": f"{server.host}:{server.port}",
                    "Authorization": f"Bearer {server.token}",
                    "Origin": f"http://{server.host}:{server.port}",
                    "Content-Type": "application/zip",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        except BaseException as error:  # pragma: no cover - reported below
            first_errors.append(error)

    thread = Thread(target=first_request)
    thread.start()
    try:
        assert first_entered.wait(timeout=1)
        excess = http.client.HTTPConnection(server.host, server.port, timeout=1)
        excess.putrequest("POST", "/api/v1/backup/inspect", skip_host=True)
        excess.putheader("Host", f"{server.host}:{server.port}")
        excess.putheader("Authorization", f"Bearer {server.token}")
        excess.putheader("Origin", f"http://{server.host}:{server.port}")
        excess.putheader("Content-Type", "application/zip")
        excess.putheader("Content-Length", "1")
        excess.endheaders()

        response = excess.getresponse()
        response_payload = json.loads(response.read())
        assert response.status == 429
        assert response_payload["error"]["code"] == "backup_capacity"
        excess.close()
    finally:
        release_first.set()
        thread.join(timeout=2)
        server.stop()

    assert not thread.is_alive()
    assert first_errors == []


def test_backup_admission_enforces_aggregate_bytes() -> None:
    from arxiv_digest.web.server import LoopbackServer

    server = LoopbackServer(
        handlers={},
        max_pending_backup_inspections=2,
        max_pending_backup_bytes=10,
    )

    assert server._reserve_backup_body(7) is True
    assert server._reserve_backup_body(4) is False
    assert server._reserve_backup_body(3) is True
    server._release_backup_body(7)
    server._release_backup_body(3)
    assert server._reserve_backup_body(10) is True
    server._release_backup_body(10)


def test_slow_drip_cannot_extend_the_total_request_input_deadline() -> None:
    from arxiv_digest.web.server import LoopbackServer

    server = LoopbackServer(
        handlers={"status": lambda _payload: {"state": "ready"}},
        request_timeout=0.1,
        max_request_threads=1,
    )
    server.start()
    slow = socket.create_connection((server.host, server.port), timeout=1)
    slow.settimeout(1)
    stopped = Event()

    def drip() -> None:
        try:
            for byte in b"GET /api/v1/status HTTP/1.1\r\nHost:":
                slow.sendall(bytes((byte,)))
                if stopped.wait(0.04):
                    return
        except OSError:
            return

    thread = Thread(target=drip)
    thread.start()
    try:
        assert stopped.wait(0.25) is False
        connection = http.client.HTTPConnection(
            server.host,
            server.port,
            timeout=1,
        )
        connection.request(
            "GET",
            "/api/v1/status",
            headers={
                "Host": f"{server.host}:{server.port}",
                "Authorization": f"Bearer {server.token}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 200
        assert payload["data"]["state"] == "ready"
    finally:
        stopped.set()
        thread.join(timeout=1)
        slow.close()
        server.stop()

    assert not thread.is_alive()


def test_static_allowlist_csp_and_launch_fragment_do_not_expose_token() -> None:
    from arxiv_digest.web.server import CSP, LoopbackServer, StaticAsset

    server = LoopbackServer(
        handlers={},
        static_assets={
            "/index.html": StaticAsset("text/html; charset=utf-8", b"<main>Digest</main>"),
        },
    )
    server.start()
    try:
        connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{server.port}"})
        response = connection.getresponse()
        body = response.read()
        headers = {name.casefold(): value for name, value in response.getheaders()}
        connection.close()

        assert response.status == 200
        assert body == b"<main>Digest</main>"
        assert headers["content-security-policy"] == CSP
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"
        assert not any(name.startswith("access-control-") for name in headers)

        launch = urlsplit(server.launch_url("review"))
        assert launch.scheme == "http"
        assert launch.netloc == f"127.0.0.1:{server.port}"
        assert launch.query == ""
        assert launch.fragment == f"token={server.token}&view=review"
    finally:
        server.stop()


def test_server_wires_authenticated_tab_leases_and_graceful_quit() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController
    from arxiv_digest.web.server import LoopbackServer

    lifecycle = LifecycleController()
    server = LoopbackServer(handlers={}, lifecycle=lifecycle)
    server.start()
    try:
        def mutate(path: str, body: bytes) -> tuple[int, dict]:
            connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Host": f"127.0.0.1:{server.port}",
                    "Authorization": f"Bearer {server.token}",
                    "Origin": f"http://127.0.0.1:{server.port}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload

        status, connected = mutate(
            "/api/v1/tabs/connect", b'{"tab_id":"tab_abcd1234"}'
        )
        assert status == 200
        assert connected["data"] == {"connected": True}
        assert lifecycle.should_stop() is False

        status, quit_response = mutate("/api/v1/application/quit", b"{}")
        assert status == 200
        assert quit_response["data"] == {"quitting": True}
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if lifecycle.should_stop():
                break
            time.sleep(0.01)
        else:
            raise AssertionError("lifecycle did not stop after the quit response")
    finally:
        server.stop()


@pytest.mark.parametrize("method", ("TRACE", "CONNECT"))
def test_uncommon_http_methods_use_controlled_security_responses(
    method: str,
) -> None:
    from arxiv_digest.web.server import LoopbackServer, StaticAsset

    server = LoopbackServer(
        handlers={"status": lambda payload: {"state": "ready"}},
        static_assets={
            "/index.html": StaticAsset("text/html; charset=utf-8", b"index")
        },
    )
    server.start()
    try:
        def request(target: str, *, authenticated: bool):
            headers = {"Host": f"127.0.0.1:{server.port}"}
            if authenticated:
                headers["Authorization"] = f"Bearer {server.token}"
            connection = http.client.HTTPConnection(
                server.host, server.port, timeout=2
            )
            connection.request(method, target, headers=headers)
            response = connection.getresponse()
            response.read()
            values = {
                name.casefold(): value for name, value in response.getheaders()
            }
            connection.close()
            return response.status, values

        api_status, api_headers = request(
            "/api/v1/status", authenticated=True
        )
        static_status, static_headers = request("/", authenticated=False)

        assert api_status == 405
        assert api_headers["allow"] == "GET"
        assert api_headers["cache-control"] == "no-store"
        assert static_status == 405
        assert static_headers["allow"] == "GET, HEAD"
        assert static_headers["content-security-policy"]
        assert not any(
            name.startswith("access-control-")
            for name in (*api_headers, *static_headers)
        )
    finally:
        server.stop()


def test_malformed_http_body_metadata_gets_json_security_response() -> None:
    from arxiv_digest.web.server import LoopbackServer

    server = LoopbackServer(handlers={})
    server.start()
    try:
        connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
        connection.putrequest("POST", "/api/v1/sync/start", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{server.port}")
        connection.putheader("Authorization", f"Bearer {server.token}")
        connection.putheader("Origin", f"http://127.0.0.1:{server.port}")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()

        response = connection.getresponse()
        payload = json.loads(response.read())
        headers = {name.casefold(): value for name, value in response.getheaders()}
        connection.close()

        assert response.status == 400
        assert payload["error"]["code"] == "invalid_content_length"
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert not any(name.startswith("access-control-") for name in headers)
    finally:
        server.stop()


def test_index_allows_only_one_valid_token_free_view_query() -> None:
    from arxiv_digest.web.server import LoopbackServer, StaticAsset

    server = LoopbackServer(
        handlers={},
        static_assets={
            "/index.html": StaticAsset("text/html; charset=utf-8", b"index"),
            "/app.js": StaticAsset("text/javascript; charset=utf-8", b"app"),
        },
    )
    server.start()
    try:
        def status(target: str) -> int:
            connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
            connection.request(
                "GET",
                target,
                headers={"Host": f"127.0.0.1:{server.port}"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status

        assert status("/?view=review") == 200
        assert status("/?view=calendar") == 200
        assert status("/index.html?view=settings") == 200
        assert status("/?view=unknown") == 404
        assert status("/?view=review&view=library") == 404
        assert status("/?other=review") == 404
        assert status("/app.js?view=review") == 404
    finally:
        server.stop()


def test_packaged_static_allowlist_includes_manifest_fonts_and_license() -> None:
    from arxiv_digest.application import _packaged_static_assets

    assets = _packaged_static_assets()

    assert assets["/vendor/katex/katex-manifest.json"].content_type == (
        "application/json; charset=utf-8"
    )
    assert assets[
        "/vendor/katex/fonts/KaTeX_Main-Regular.ttf"
    ].content_type == "font/ttf"
    assert assets["/vendor/katex/LICENSE"].content_type == (
        "text/plain; charset=utf-8"
    )


def test_default_application_starts_once_from_an_unrelated_directory(
    tmp_path, monkeypatch
) -> None:
    from arxiv_digest.application import create_application
    from arxiv_digest.web.server import LoopbackServer

    root = tmp_path / "platform-state"
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("ARXIV_DIGEST_TESTING", "1")
    monkeypatch.setenv("ARXIV_DIGEST_TEST_ROOT", str(root))
    monkeypatch.setattr(LoopbackServer, "wait", lambda self: None)
    opened = []

    application = create_application(
        browser_open=lambda url: opened.append(url) or True,
    )

    assert application.open_dashboard("default") == 0
    assert len(opened) == 1
    launch = urlsplit(opened[0])
    assert launch.hostname == "127.0.0.1"
    assert launch.query == ""
    assert launch.fragment.endswith("&view=setup")
    assert (root / "data/state.sqlite3").is_file()
    assert not (root / "data/runtime.json").exists()


def test_server_stop_waits_for_admitted_handler_completion() -> None:
    from arxiv_digest.web.server import LoopbackServer

    entered = Event()
    release = Event()
    stopped = Event()
    responses = []
    def status(_payload):
        entered.set()
        assert release.wait(2)
        return {"finished": True}
    server = LoopbackServer(handlers={"status": status})
    server.start()
    def request():
        connection = http.client.HTTPConnection(server.host, server.port, timeout=3)
        connection.request("GET", "/api/v1/status", headers={
            "Host": f"{server.host}:{server.port}",
            "Authorization": f"Bearer {server.token}",
        })
        response = connection.getresponse()
        responses.append(json.loads(response.read())["data"])
        connection.close()
    requester = Thread(target=request)
    requester.start()
    assert entered.wait(2)
    def stop():
        server.stop()
        stopped.set()
    stopper = Thread(target=stop)
    stopper.start()
    try:
        assert not stopped.wait(0.6)
    finally:
        release.set()
        requester.join(3)
        stopper.join(3)
    assert stopped.is_set()
    assert responses == [{"finished": True, "startup_nonce": server.startup_nonce}]
