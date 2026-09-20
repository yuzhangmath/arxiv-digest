"""Bounded, single-hop curl transport for an HTTP client's explicit fallback."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
import os
import re
import selectors
import shutil
import subprocess
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass


_MAX_HEADER_BYTES = 64 * 1024
_READ_BYTES = 64 * 1024
_POLL_SECONDS = 0.05


class CurlUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("curl is unavailable")


class CurlTransportError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("curl request failed")


@dataclass(frozen=True, slots=True)
class CurlResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _find_curl() -> str | None:
    return shutil.which("curl")


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        # The shared client imports this transport, so import its exception lazily.
        from arxiv_digest.rate_limit import ArxivRequestCancelled

        raise ArxivRequestCancelled("arXiv request was cancelled")


def _proxy_environment(url: str) -> dict[str, str]:
    # urllib includes platform settings (notably macOS System Configuration).
    # Select once, then neutralize curl's separate environment precedence rules.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.casefold().endswith("_proxy")
    }
    proxy = getproxies().get("https")
    if proxy and not proxy_bypass(urlsplit(url).netloc):
        environment["https_proxy"] = proxy
        environment["no_proxy"] = ""
    else:
        environment["no_proxy"] = "*"
    return environment


def _parse_headers(block: bytes) -> tuple[int, dict[str, str]]:
    lines = block.split(b"\r\n")
    match = re.fullmatch(rb"HTTP/(?:1\.[01]|2|3) ([1-5]\d{2})(?:[ \t].*)?", lines[0])
    if match is None:
        raise CurlTransportError()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None:
            raise CurlTransportError()
        if b"\r" in value or b"\n" in value or b"\x00" in value:
            raise CurlTransportError()
        key = name.decode("ascii").lower()
        decoded = value.decode("iso-8859-1").strip()
        if key in headers and headers[key] != decoded:
            # Conflicting framing, retry or redirect metadata is not safe to use.
            if key in {"content-length", "retry-after", "location", "content-type"}:
                raise CurlTransportError()
            decoded = headers[key] + ", " + decoded
        headers[key] = decoded
    return int(match[1]), headers


def _read_response(
    process: subprocess.Popen[bytes], *, deadline: float, max_bytes: int,
    cancelled: Callable[[], bool] | None,
) -> CurlResponse:
    assert process.stdout is not None
    pending = bytearray()
    body = bytearray()
    status: int | None = None
    headers: dict[str, str] = {}
    header_bytes = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            _check_cancelled(cancelled)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CurlTransportError()
            if not selector.select(min(_POLL_SECONDS, remaining)):
                continue
            chunk = os.read(process.stdout.fileno(), _READ_BYTES)
            if not chunk:
                break
            if status is None:
                pending.extend(chunk)
                while status is None:
                    end = pending.find(b"\r\n\r\n")
                    if end < 0:
                        if header_bytes + len(pending) > _MAX_HEADER_BYTES:
                            raise CurlTransportError()
                        break
                    header_bytes += end + 4
                    if header_bytes > _MAX_HEADER_BYTES:
                        raise CurlTransportError()
                    candidate, headers = _parse_headers(bytes(pending[:end]))
                    del pending[:end + 4]
                    if candidate >= 200:
                        status = candidate
                        if status == 429:
                            # Honor an authoritative throttle without waiting for
                            # an error body that may stall or exceed the limit.
                            return CurlResponse(status, headers, b"")
                        length = headers.get("content-length")
                        if length is not None and (
                            not re.fullmatch(r"[0-9]+", length) or int(length) > max_bytes
                        ):
                            raise CurlTransportError()
                if status is None:
                    continue
                chunk = bytes(pending)
                pending.clear()
            if len(body) + len(chunk) > max_bytes:
                raise CurlTransportError()
            body.extend(chunk)
        while process.poll() is None:
            _check_cancelled(cancelled)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CurlTransportError()
            try:
                process.wait(timeout=min(_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                pass
        _check_cancelled(cancelled)
        if status is None or process.returncode != 0:
            raise CurlTransportError()
    return CurlResponse(status, headers, bytes(body))


class CurlTransport:
    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float,
        max_bytes: int, cancelled: Callable[[], bool] | None = None,
    ) -> CurlResponse:
        _check_cancelled(cancelled)
        executable = _find_curl()
        if executable is None:
            raise CurlUnavailable()
        if not isfinite(timeout) or timeout <= 0 or max_bytes < 0:
            raise CurlTransportError()
        try:
            if urlsplit(url).scheme.casefold() != "https":
                raise CurlTransportError()
            environment = _proxy_environment(url)
        except (OSError, ValueError):
            raise CurlTransportError() from None
        command = [
            executable, "-q", "--globoff", "--silent", "--include",
            "--suppress-connect-headers", "--proto", "=https",
            "--proto-redir", "=https", "--max-time", str(timeout),
            "--connect-timeout", str(timeout),
        ]
        for name, value in headers.items():
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None or any(
                character in value for character in "\r\n\x00"
            ):
                raise CurlTransportError()
            command.extend(("--header", f"{name}: {value}"))
        command.extend(("--url", url))
        deadline = monotonic() + timeout
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=environment, close_fds=True,
            )
        except FileNotFoundError:
            raise CurlUnavailable() from None
        except (OSError, ValueError):
            raise CurlTransportError() from None
        try:
            return _read_response(
                process, deadline=deadline, max_bytes=max_bytes, cancelled=cancelled,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            from arxiv_digest.rate_limit import ArxivRequestCancelled

            if isinstance(error, ArxivRequestCancelled):
                raise
            raise CurlTransportError() from None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
