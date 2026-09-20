from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
from time import monotonic

import pytest

from arxiv_digest import curl_transport
from arxiv_digest.curl_transport import CurlTransport, CurlTransportError, CurlUnavailable
from arxiv_digest.rate_limit import ArxivRequestCancelled


@pytest.fixture
def fake_curl(tmp_path, monkeypatch):
    processes = []
    real_popen = curl_transport.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(curl_transport.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(curl_transport, "getproxies", lambda: {})
    monkeypatch.setattr(curl_transport, "proxy_bypass", lambda host: False)

    def install(response=b"HTTP/2 200\r\nContent-Type: text/html\r\n\r\nhello", *, tail="", prefix=""):
        executable = tmp_path / "curl"
        capture = tmp_path / "capture.json"
        executable.write_text(
            f"#!{Path(sys.executable).resolve()}\n"
            "import base64, json, os, sys, time\n"
            f"with open({str(capture)!r}, 'w') as output:\n"
            "    json.dump({'args': sys.argv[1:], 'proxy_env': {key: value for key, value in os.environ.items() if key.lower().endswith('_proxy')}}, output)\n"
            + prefix + "\n"
            + f"sys.stdout.buffer.write(base64.b64decode({base64.b64encode(response)!r}))\n"
            "sys.stdout.buffer.flush()\n"
            + tail + "\n"
        )
        executable.chmod(0o700)
        monkeypatch.setattr(curl_transport, "_find_curl", lambda: str(executable))
        return capture, processes

    return install


def request(**kwargs):
    return CurlTransport().get(
        "https://arxiv.org/catchup/math/2000-01-01", headers={"User-Agent": "synthetic-client"},
        timeout=kwargs.pop("timeout", 2.0), max_bytes=kwargs.pop("max_bytes", 1024), **kwargs,
    )


def test_success_restricts_protocol_and_disables_configuration(fake_curl):
    capture, processes = fake_curl()
    response = request()
    assert response.status == 200
    assert response.headers["content-type"] == "text/html"
    assert response.body == b"hello"
    args = json.loads(capture.read_text())["args"]
    assert args[0] == "-q"
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--proto-redir") + 1] == "=https"
    assert "--globoff" in args
    assert "--suppress-connect-headers" in args
    assert "--location" not in args and "-L" not in args
    assert "--fail" not in args
    assert "User-Agent: synthetic-client" in args
    assert processes[0].returncode == 0


@pytest.mark.parametrize("status", [302, 406, 429])
def test_http_failure_and_redirect_metadata_are_returned(fake_curl, status):
    fake_curl(f"HTTP/1.1 {status} Response\r\nRetry-After: 120\r\nLocation: /next\r\n\r\nrate exceeded".encode())
    response = request()
    assert response.status == status
    assert response.headers["retry-after"] == "120"
    assert response.headers["location"] == "/next"
    assert response.body == (b"" if status == 429 else b"rate exceeded")


def test_informational_headers_are_skipped(fake_curl):
    fake_curl(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\nContent-Length: 2\r\n\r\nok")
    assert request().body == b"ok"


def test_missing_curl_fails_with_fixed_message(monkeypatch):
    monkeypatch.setattr(curl_transport, "_find_curl", lambda: None)
    with pytest.raises(CurlUnavailable, match="^curl is unavailable$"):
        request()


@pytest.mark.parametrize("response", [
    b"HTTP/2 200\r\n\r\n" + b"x" * 1025,
    b"HTTP/2 200\r\nX-Large: " + b"x" * (64 * 1024) + b"\r\n\r\nok",
    b"HTTP/2 200\r\nContent-Length: 2048\r\n\r\n",
    b"private malformed output",
    b"HTTP/2 200\r\nInvalid header\r\n\r\nok",
], ids=["body-limit", "header-limit", "declared-limit", "malformed-status", "malformed-header"])
def test_invalid_or_oversized_response_is_redacted_and_process_reaped(fake_curl, response):
    _, processes = fake_curl(response)
    with pytest.raises(CurlTransportError, match="^curl request failed$"):
        request()
    assert processes[0].poll() is not None


def test_failed_process_does_not_expose_stderr_or_partial_body(fake_curl):
    fake_curl(tail="sys.stderr.write('private proxy credentials and paths'); sys.exit(7)")
    with pytest.raises(CurlTransportError, match="^curl request failed$"):
        request()


def test_timeout_terminates_and_reaps_process(fake_curl):
    _, processes = fake_curl(prefix="time.sleep(30)")
    started = monotonic()
    with pytest.raises(CurlTransportError, match="^curl request failed$"):
        request(timeout=0.1)
    assert monotonic() - started < 2
    assert processes[0].poll() is not None


def test_cancellation_terminates_and_reaps_process(fake_curl):
    _, processes = fake_curl(prefix="time.sleep(30)")
    started = monotonic()
    with pytest.raises(ArxivRequestCancelled):
        request(cancelled=lambda: monotonic() - started > 0.1)
    assert monotonic() - started < 2
    assert processes[0].poll() is not None


def test_cancellation_before_launch_does_not_start_curl(fake_curl):
    _, processes = fake_curl()
    with pytest.raises(ArxivRequestCancelled):
        request(cancelled=lambda: True)
    assert processes == []


def test_cancellation_after_stdout_closes_still_reaps_process(fake_curl):
    _, processes = fake_curl(tail="os.close(1); time.sleep(30)")
    started = monotonic()
    with pytest.raises(ArxivRequestCancelled):
        request(cancelled=lambda: monotonic() - started > 0.5)
    assert monotonic() - started < 2
    assert processes[0].poll() is not None


def test_throttle_headers_stop_without_waiting_for_body(fake_curl):
    _, processes = fake_curl(
        b"HTTP/2 429\r\nRetry-After: 120\r\nContent-Length: 99999999\r\n\r\n",
        tail="time.sleep(30)",
    )
    started = monotonic()
    response = request()
    assert response.status == 429
    assert response.headers["retry-after"] == "120"
    assert response.body == b""
    assert monotonic() - started < 2
    assert processes[0].poll() is not None


def test_insecure_url_is_rejected_before_launch(fake_curl):
    _, processes = fake_curl()
    with pytest.raises(CurlTransportError, match="^curl request failed$"):
        CurlTransport().get("http://arxiv.org/", headers={}, timeout=1, max_bytes=1024)
    assert processes == []


def test_process_launch_error_is_redacted(fake_curl, monkeypatch):
    fake_curl()

    def failed_popen(*args, **kwargs):
        raise PermissionError("synthetic private executable path")

    monkeypatch.setattr(curl_transport.subprocess, "Popen", failed_popen)
    with pytest.raises(CurlTransportError, match="^curl request failed$"):
        request()


@pytest.mark.parametrize("bypass", [False, True])
def test_proxy_selection_uses_urllib_without_credentials_in_arguments(fake_curl, monkeypatch, bypass):
    capture, _ = fake_curl()
    proxy = "http://synthetic-user:synthetic-secret@proxy.invalid:8080"
    monkeypatch.setenv("ALL_PROXY", "http://ignored.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.invalid")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(curl_transport, "getproxies", lambda: {"https": proxy})
    monkeypatch.setattr(curl_transport, "proxy_bypass", lambda host: bypass)
    request()
    captured = json.loads(capture.read_text())
    assert all("synthetic-secret" not in arg for arg in captured["args"])
    if bypass:
        assert captured["proxy_env"] == {"no_proxy": "*"}
    else:
        assert captured["proxy_env"] == {"https_proxy": proxy, "no_proxy": ""}
