from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from http.client import HTTPException
from io import BytesIO
from math import isfinite
import re
from threading import Lock
from time import monotonic as system_monotonic, sleep as system_sleep
from types import MappingProxyType
from typing import Iterator, Protocol
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from arxiv_digest.arxiv_access import (
    ArxivCooldown, ArxivCooldownUnavailable, ArxivRateLimited, retry_after_seconds,
)
from arxiv_digest.curl_transport import CurlTransport, CurlTransportError, CurlUnavailable

_RETRYABLE_HTTP_STATUSES = frozenset({502, 503, 504})
_ARXIV_HOSTS = frozenset(
    {"arxiv.org", "export.arxiv.org", "oaipmh.arxiv.org", "rss.arxiv.org"}
)
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_REQUEST_TIMEOUT_SECONDS = 120.0
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024
_THROTTLE_BODY_LIMIT = 4096
_THROTTLE_CONTENT_TYPES = frozenset({"", "text/plain", "text/html", "application/xhtml+xml"})


class Interface(Enum):
    ATOM = "atom"
    OAI = "oai"
    CATCHUP = "catchup"
    PDF = "pdf"


class ArxivRequestCancelled(InterruptedError):
    """An arXiv response read stopped at a cooperative cancellation boundary."""


@dataclass(frozen=True, slots=True)
class RequestPolicy:
    minimum_delay: float
    max_attempts: int
    max_total_retry_seconds: float
    max_response_bytes: int
    allowed_content_types: tuple[str, ...]


DEFAULT_POLICIES: Mapping[Interface, RequestPolicy] = MappingProxyType(
    {
        Interface.ATOM: RequestPolicy(
            3.0,
            4,
            120.0,
            5 * 1024 * 1024,
            ("application/atom+xml", "application/xml", "text/xml"),
        ),
        Interface.OAI: RequestPolicy(
            3.0,
            4,
            120.0,
            16 * 1024 * 1024,
            ("application/xml", "text/xml"),
        ),
        Interface.CATCHUP: RequestPolicy(
            15.0,
            3,
            120.0,
            8 * 1024 * 1024,
            ("text/html", "application/xhtml+xml"),
        ),
        Interface.PDF: RequestPolicy(
            3.0,
            3,
            120.0,
            256 * 1024 * 1024,
            ("application/pdf",),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes
    observed_at: datetime


class _Response(Protocol):
    status: int
    headers: Mapping[str, str]

    def geturl(self) -> str: ...

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


class _Opener(Protocol):
    def open(
        self,
        request: Request,
        timeout: float | None = None,
    ) -> _Response: ...


def _retry_after_seconds(value: str | None, now: datetime) -> float:
    return retry_after_seconds(value, now)


class _ThrottleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _is_rate_exceeded(body: bytes, media_type: str) -> bool:
    if len(body) > _THROTTLE_BODY_LIMIT or media_type not in _THROTTLE_CONTENT_TYPES:
        return False
    text = body.decode("utf-8", errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _ThrottleText()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    # Match the entire short error page, never an occurrence in paper content.
    return re.fullmatch(r"(?:rate\s+exceeded[.!]?\s*){1,2}", text.strip(), re.IGNORECASE) is not None


def _read_throttle_body(
    response: _Response,
    limit: int,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    try:
        return _read_response_body(response, min(limit, _THROTTLE_BODY_LIMIT), cancelled)
    except ValueError:
        return b""


def _validate_arxiv_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL is not an allowed arXiv HTTPS endpoint") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname not in _ARXIV_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL is not an allowed arXiv HTTPS endpoint")


class ArxivRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> Request | None:
        _validate_arxiv_url(new_url)
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_response_body(
    response: _Response,
    limit: int,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    body = bytearray()
    read_available = getattr(response, "read1", None)
    reader = read_available if callable(read_available) else response.read
    while True:
        if cancelled is not None and cancelled():
            raise ArxivRequestCancelled("arXiv request was cancelled")
        read_size = min(_RESPONSE_READ_CHUNK_BYTES, limit + 1 - len(body))
        chunk = reader(read_size)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError("response exceeded byte limit")
        if cancelled is not None and cancelled():
            raise ArxivRequestCancelled("arXiv request was cancelled")


class ArxivHttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        contact_url: str,
        opener: _Opener | None = None,
        monotonic: Callable[[], float] = system_monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = system_sleep,
        policies: Mapping[Interface, RequestPolicy] = DEFAULT_POLICIES,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        cooldown: ArxivCooldown | None = None,
        curl_transport: CurlTransport | None = None,
    ) -> None:
        if (
            not isfinite(request_timeout)
            or request_timeout <= 0
            or request_timeout > _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("request timeout must be finite and between 0 and 120 seconds")
        self._user_agent = user_agent
        self._contact_url = contact_url
        self._opener = opener or build_opener(ArxivRedirectHandler())
        # An injected opener owns its transport unless an alternate is supplied.
        self._curl_transport = (
            curl_transport if curl_transport is not None
            else CurlTransport() if opener is None else None
        )
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._policies = policies
        self._request_timeout = float(request_timeout)
        self._cooldown = cooldown if cooldown is not None else ArxivCooldown(wall_clock=wall_clock)
        self._gate = Lock()
        self._last_request_started: float | None = None

    @contextmanager
    def _request_gate(
        self,
        cancelled: Callable[[], bool] | None,
    ) -> Iterator[None]:
        if cancelled is None:
            self._gate.acquire()
        else:
            while True:
                if cancelled():
                    raise ArxivRequestCancelled("arXiv request was cancelled")
                if self._gate.acquire(timeout=0.1):
                    break
        try:
            if cancelled is not None and cancelled():
                raise ArxivRequestCancelled("arXiv request was cancelled")
            yield
        finally:
            self._gate.release()

    def _wait_to_start(
        self,
        policy: RequestPolicy,
        *,
        retry_not_before: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        cancellation_error: Exception | None = None,
    ) -> None:
        now = self._monotonic()
        not_before = now
        if self._last_request_started is not None:
            not_before = max(
                not_before,
                self._last_request_started + policy.minimum_delay,
            )
        if retry_not_before is not None:
            not_before = max(not_before, retry_not_before)
        if not_before > now and cancelled is None:
            self._sleeper(not_before - now)
        while not_before > now and cancelled is not None:
            if cancelled():
                if cancellation_error is not None:
                    raise cancellation_error
                raise ArxivRequestCancelled("arXiv request was cancelled")
            self._sleeper(min(0.1, not_before - now))
            now = self._monotonic()
        self._last_request_started = self._monotonic()

    def _check_cooldown(self, *, attempted: bool) -> None:
        try:
            self._cooldown.raise_if_active()
        except (ArxivRateLimited, ArxivCooldownUnavailable) as error:
            # A pause between retries still belongs to an attempted fetch.
            error.attempted = error.attempted or attempted
            raise

    def _curl_fallback(
        self,
        request: Request,
        *,
        failed_url: str,
        interface: Interface,
        policy: RequestPolicy,
        response_limit: int,
        first_attempt_started: float,
        cancelled: Callable[[], bool] | None,
    ) -> HttpResponse | None:
        eligible = interface is Interface.CATCHUP or (
            interface is Interface.OAI
            and parse_qs(urlsplit(request.full_url).query).get("verb")
            in (["GetRecord"], ["ListRecords"])
        )
        if self._curl_transport is None or not eligible:
            return None
        deadline = first_attempt_started + policy.max_total_retry_seconds
        url = failed_url
        for redirects in range(6):
            _validate_arxiv_url(url)
            self._check_cooldown(attempted=True)
            if cancelled is not None and cancelled():
                raise ArxivRequestCancelled("arXiv request was cancelled")
            next_start = max(
                self._monotonic(),
                (self._last_request_started or 0.0) + policy.minimum_delay,
            )
            if next_start >= deadline:
                return None
            self._wait_to_start(policy, cancelled=cancelled)
            self._check_cooldown(attempted=True)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            try:
                response = self._curl_transport.get(
                    url,
                    headers=dict(request.header_items()),
                    timeout=min(self._request_timeout, remaining),
                    max_bytes=response_limit,
                    cancelled=cancelled,
                )
            except (CurlUnavailable, CurlTransportError):
                # Keep the authoritative original status when curl cannot help.
                return None
            headers = {name.casefold(): value for name, value in response.headers.items()}
            media_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
            if response.status == 429 or _is_rate_exceeded(response.body, media_type):
                raise self._cooldown.record(
                    retry_after=headers.get("retry-after"), http_status=response.status,
                )
            if cancelled is not None and cancelled():
                raise ArxivRequestCancelled("arXiv request was cancelled")
            if response.status in {301, 302, 303, 307, 308}:
                if redirects == 5 or not headers.get("location"):
                    return None
                # Curl never follows redirects itself. Validate each next target
                # before another paced request can leave this process.
                url = urljoin(url, headers["location"])
                continue
            if response.status != 200:
                raise HTTPError(
                    url, response.status, "arXiv request failed", headers,
                    BytesIO(response.body),
                )
            if media_type not in {value.casefold() for value in policy.allowed_content_types}:
                raise ValueError("unexpected response content type")
            if len(response.body) > response_limit:
                raise ValueError("response exceeded byte limit")
            return HttpResponse(
                status=response.status, final_url=url,
                headers=MappingProxyType(headers), body=response.body,
                observed_at=self._wall_clock(),
            )
        return None

    def get(
        self,
        url: str,
        *,
        interface: Interface,
        accept: str,
        max_bytes: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> HttpResponse:
        _validate_arxiv_url(url)
        with self._request_gate(cancelled):
            policy = self._policies[interface]
            response_limit = policy.max_response_bytes
            if max_bytes is not None:
                response_limit = min(response_limit, max_bytes)
            if response_limit <= 0:
                raise ValueError("response byte limit must be positive")
            request = Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": f"{self._user_agent} (+{self._contact_url})",
                },
                method="GET",
            )
            retry_not_before: float | None = None
            first_attempt_started: float | None = None
            last_error: HTTPError | None = None
            for attempt in range(policy.max_attempts):
                if cancelled is not None and cancelled():
                    if last_error is not None:
                        raise last_error
                    raise ArxivRequestCancelled("arXiv request was cancelled")
                self._check_cooldown(attempted=first_attempt_started is not None)
                self._wait_to_start(
                    policy,
                    retry_not_before=retry_not_before,
                    cancelled=cancelled,
                    cancellation_error=last_error,
                )
                if last_error is not None and cancelled is not None and cancelled():
                    raise last_error
                self._check_cooldown(attempted=first_attempt_started is not None)
                if first_attempt_started is None:
                    first_attempt_started = self._monotonic()
                try:
                    with self._opener.open(
                        request,
                        timeout=self._request_timeout,
                    ) as response:
                        final_url = response.geturl()
                        _validate_arxiv_url(final_url)
                        headers = {
                            name.casefold(): value
                            for name, value in response.headers.items()
                        }
                        if response.status == 429:
                            raise self._cooldown.record(
                                retry_after=headers.get("retry-after"), http_status=429,
                            )
                        content_type = headers.get("content-type", "")
                        media_type = content_type.partition(";")[0].strip().casefold()
                        allowed_types = {
                            value.casefold() for value in policy.allowed_content_types
                        }
                        if media_type not in allowed_types:
                            if media_type in _THROTTLE_CONTENT_TYPES:
                                diagnostic = _read_throttle_body(response, response_limit, cancelled)
                                if _is_rate_exceeded(diagnostic, media_type):
                                    raise self._cooldown.record(
                                        retry_after=headers.get("retry-after"),
                                        http_status=response.status,
                                    )
                            raise ValueError("unexpected response content type")
                        body = _read_response_body(
                            response,
                            response_limit,
                            cancelled,
                        )
                        if _is_rate_exceeded(body, media_type):
                            raise self._cooldown.record(
                                retry_after=headers.get("retry-after"),
                                http_status=response.status,
                            )
                        return HttpResponse(
                            status=response.status,
                            final_url=final_url,
                            headers=MappingProxyType(headers),
                            body=body,
                            observed_at=self._wall_clock(),
                        )
                except HTTPError as error:
                    last_error = error
                    headers = {
                        name.casefold(): value
                        for name, value in (error.headers or {}).items()
                    }
                    if error.code == 429:
                        error.close()
                        raise self._cooldown.record(
                            retry_after=headers.get("retry-after"), http_status=429,
                        ) from None
                    # Preserve existing cancellation/error precedence for ordinary
                    # HTTP failures; cancellation while reading a body propagates.
                    if cancelled is None or not cancelled():
                        media_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
                        if media_type in _THROTTLE_CONTENT_TYPES:
                            try:
                                diagnostic = _read_throttle_body(error, response_limit, cancelled)
                            except ArxivRequestCancelled:
                                raise
                            except (OSError, HTTPException):
                                # The status is authoritative even when optional
                                # error-page inspection cannot finish.
                                diagnostic = b""
                            finally:
                                error.close()
                            if _is_rate_exceeded(diagnostic, media_type):
                                raise self._cooldown.record(
                                    retry_after=headers.get("retry-after"), http_status=error.code,
                                ) from None
                    if error.code == 406:
                        error.close()
                        fallback = self._curl_fallback(
                            request, failed_url=error.geturl(), interface=interface,
                            policy=policy, response_limit=response_limit,
                            first_attempt_started=first_attempt_started,
                            cancelled=cancelled,
                        )
                        if fallback is not None:
                            return fallback
                    if (
                        error.code not in _RETRYABLE_HTTP_STATUSES
                        or attempt + 1 >= policy.max_attempts
                    ):
                        raise
                    if cancelled is not None and cancelled():
                        raise
                    retry_after = headers.get("retry-after")
                    delay = max(
                        2.0**attempt,
                        _retry_after_seconds(retry_after, self._wall_clock()),
                    )
                    retry_not_before = self._monotonic() + delay
                    normal_not_before = (
                        self._last_request_started + policy.minimum_delay
                    )
                    next_start = max(retry_not_before, normal_not_before)
                    if (
                        next_start - first_attempt_started
                        > policy.max_total_retry_seconds
                    ):
                        raise
            raise AssertionError("unreachable")
