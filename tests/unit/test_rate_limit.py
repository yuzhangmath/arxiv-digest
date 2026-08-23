from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from io import BytesIO
from threading import Event, Lock, Thread
from urllib.error import HTTPError
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

import pytest

import arxiv_digest.rate_limit as rate_limit
from arxiv_digest.rate_limit import (
    ArxivHttpClient,
    ArxivRedirectHandler,
    DEFAULT_POLICIES,
    Interface,
    RequestPolicy,
)


class FakeResponse:
    def __init__(self) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/xml", "X-Test": "yes"}
        self._body = b"<ok />"
        self._offset = 0

    def geturl(self) -> str:
        return "https://export.arxiv.org/oai2"

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def open(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        response = FakeResponse()
        response.headers["Content-Type"] = request.get_header("Accept")
        return response


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def http_error(status: int, **headers: str) -> HTTPError:
    message = Message()
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    return HTTPError(
        "https://export.arxiv.org/oai2",
        status,
        "fixture error",
        message,
        BytesIO(b"fixture error body"),
    )


def test_get_returns_normalized_response_and_identifies_the_application() -> None:
    opener = FakeOpener()
    observed_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    policy = RequestPolicy(
        minimum_delay=0,
        max_attempts=1,
        max_total_retry_seconds=0,
        max_response_bytes=1_024,
        allowed_content_types=("application/xml",),
    )
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: observed_at,
        sleeper=lambda _: None,
        policies={Interface.OAI: policy},
    )

    response = client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert response.status == 200
    assert response.final_url == "https://export.arxiv.org/oai2"
    assert response.headers == {
        "content-type": "application/xml",
        "x-test": "yes",
    }
    assert response.body == b"<ok />"
    assert response.observed_at == observed_at
    with pytest.raises(TypeError):
        response.headers["x-test"] = "changed"
    request = opener.requests[0]
    assert request.get_header("User-agent") == (
        "arxiv-digest/0.1 (+https://example.invalid/contact)"
    )
    assert request.get_header("Accept") == "application/xml"


def test_transport_open_uses_the_validated_request_timeout() -> None:
    class TimeoutOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float | None] = []

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.timeouts.append(timeout)
            return super().open(request, timeout)

    opener = TimeoutOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        request_timeout=7.5,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert opener.timeouts == [7.5]


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), float("nan")])
def test_request_timeout_must_be_finite_positive_and_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="request timeout"):
        ArxivHttpClient(
            user_agent="arxiv-digest/0.1",
            contact_url="https://example.invalid/contact",
            opener=FakeOpener(),
            request_timeout=timeout,
        )


def test_response_read_stops_at_a_cancellation_boundary() -> None:
    cancellation = Event()

    class CancellingResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__()
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            cancellation.set()
            return b"first chunk"

    response = CancellingResponse()

    class CancellingOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            return response

    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=CancellingOpener(),
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                256 * 1024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(rate_limit.ArxivRequestCancelled):
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
            cancelled=cancellation.is_set,
        )

    assert response.read_sizes == [64 * 1024]


def test_http_response_read1_checks_cancellation_between_available_chunks() -> None:
    cancellation = Event()

    class SlowDripResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__()
            self.read1_calls = 0

        def read(self, size: int = -1) -> bytes:
            raise AssertionError("read() would wait for a much larger buffer")

        def read1(self, size: int = -1) -> bytes:
            self.read1_calls += 1
            cancellation.set()
            return b"x"

    response = SlowDripResponse()

    class SlowDripOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            return response

    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=SlowDripOpener(),
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(rate_limit.ArxivRequestCancelled):
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
            cancelled=cancellation.is_set,
        )

    assert response.read1_calls == 1


def test_cancellation_before_first_request_never_opens_the_transport() -> None:
    cancellation = Event()
    cancellation.set()
    opener = FakeOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(rate_limit.ArxivRequestCancelled):
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
            cancelled=cancellation.is_set,
        )

    assert opener.requests == []


def test_requests_are_serialized_across_worker_threads() -> None:
    first_request_entered = Event()
    release_first_request = Event()

    class BlockingOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self._state_lock = Lock()
            self._active = 0
            self.max_active = 0

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            with self._state_lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
                call_number = len(self.requests) + 1
                self.requests.append(request)
            if call_number == 1:
                first_request_entered.set()
                assert release_first_request.wait(timeout=1)
            with self._state_lock:
                self._active -= 1
            return FakeResponse()

    opener = BlockingOpener()
    policy = RequestPolicy(0, 1, 0, 1_024, ("application/xml",))
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={Interface.OAI: policy},
    )
    errors: list[BaseException] = []

    def request() -> None:
        try:
            client.get(
                "https://export.arxiv.org/oai2",
                interface=Interface.OAI,
                accept="application/xml",
            )
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    first = Thread(target=request)
    second = Thread(target=request)
    first.start()
    assert first_request_entered.wait(timeout=1)
    second.start()
    assert second.is_alive()
    release_first_request.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(opener.requests) == 2
    assert opener.max_active == 1


def test_queued_request_observes_cancellation_before_the_gate_opens() -> None:
    first_request_entered = Event()
    release_first_request = Event()
    cancellation = Event()

    class BlockingOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                first_request_entered.set()
                assert release_first_request.wait(timeout=2)
            return FakeResponse()

    opener = BlockingOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )
    second_finished = Event()
    second_errors: list[BaseException] = []

    first = Thread(
        target=lambda: client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )
    )

    def second_request() -> None:
        try:
            client.get(
                "https://export.arxiv.org/oai2",
                interface=Interface.OAI,
                accept="application/xml",
                cancelled=cancellation.is_set,
            )
        except BaseException as error:
            second_errors.append(error)
        finally:
            second_finished.set()

    second = Thread(target=second_request)
    first.start()
    assert first_request_entered.wait(timeout=1)
    second.start()
    cancellation.set()

    assert second_finished.wait(timeout=1)
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], rate_limit.ArxivRequestCancelled)
    assert len(opener.requests) == 1

    release_first_request.set()
    first.join(timeout=1)
    second.join(timeout=1)


def test_every_interface_uses_one_shared_request_start_schedule() -> None:
    clock = FakeClock()

    class TimedOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.started_at: list[float] = []

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.started_at.append(clock.monotonic())
            return super().open(request, timeout)

    opener = TimedOpener()
    xml_policy = RequestPolicy(3, 1, 0, 1_024, ("application/xml",))
    catchup_policy = RequestPolicy(15, 1, 0, 1_024, ("text/html",))
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: xml_policy,
            Interface.ATOM: xml_policy,
            Interface.CATCHUP: catchup_policy,
        },
    )

    client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )
    client.get(
        "https://export.arxiv.org/api/query",
        interface=Interface.ATOM,
        accept="application/xml",
    )
    client.get(
        "https://arxiv.org/list/math/recent",
        interface=Interface.CATCHUP,
        accept="text/html",
    )

    assert opener.started_at == [0.0, 3.0, 18.0]
    assert clock.sleeps == [3.0, 15.0]


def test_larger_retry_after_delta_overrides_the_normal_delay() -> None:
    clock = FakeClock()

    class RetryOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.started_at: list[float] = []
            self.outcomes: list[HTTPError | FakeResponse] = [
                http_error(429, Retry_After="12"),
                FakeResponse(),
            ]

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.started_at.append(clock.monotonic())
            self.requests.append(request)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

    opener = RetryOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                3,
                2,
                30,
                1_024,
                ("application/xml",),
            )
        },
    )

    response = client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert response.status == 200
    assert opener.started_at == [0.0, 12.0]
    assert clock.sleeps == [12.0]


def test_retry_after_http_date_uses_the_injected_wall_clock() -> None:
    clock = FakeClock()
    wall_start = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    class RetryOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.started_at: list[float] = []
            self.outcomes: list[HTTPError | FakeResponse] = [
                http_error(
                    429,
                    Retry_After=format_datetime(wall_start + timedelta(seconds=20)),
                ),
                FakeResponse(),
            ]

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.started_at.append(clock.monotonic())
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

    opener = RetryOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: wall_start + timedelta(seconds=clock.monotonic()),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                3,
                2,
                30,
                1_024,
                ("application/xml",),
            )
        },
    )

    client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert opener.started_at == [0.0, 20.0]
    assert clock.sleeps == [20.0]


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retryable_statuses_use_exponential_backoff(status: int) -> None:
    clock = FakeClock()

    class RetryOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.started_at: list[float] = []
            self.outcomes: list[HTTPError | FakeResponse] = [
                http_error(status),
                FakeResponse(),
            ]

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.started_at.append(clock.monotonic())
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

    opener = RetryOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                2,
                10,
                1_024,
                ("application/xml",),
            )
        },
    )

    response = client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert response.status == 200
    assert opener.started_at == [0.0, 1.0]


def test_non_retryable_http_error_fails_immediately() -> None:
    clock = FakeClock()
    original_error = http_error(404, Retry_After="60")

    class FailingOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            raise original_error

    opener = FailingOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                5,
                120,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(HTTPError) as caught:
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )

    assert caught.value is original_error
    assert len(opener.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("max_attempts", "max_retry_seconds", "expected_starts"),
    [
        (3, 100.0, [0.0, 1.0, 3.0]),
        (5, 2.5, [0.0, 1.0]),
    ],
)
def test_retries_stop_at_the_attempt_and_total_time_limits(
    max_attempts: int,
    max_retry_seconds: float,
    expected_starts: list[float],
) -> None:
    clock = FakeClock()

    class FailingOpener(FakeOpener):
        def __init__(self) -> None:
            super().__init__()
            self.started_at: list[float] = []
            self.errors = [http_error(503) for _ in range(max_attempts)]

        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.started_at.append(clock.monotonic())
            raise self.errors[len(self.started_at) - 1]

    opener = FailingOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                max_attempts,
                max_retry_seconds,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(HTTPError) as caught:
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )

    assert opener.started_at == expected_starts
    assert caught.value is opener.errors[len(expected_starts) - 1]


@pytest.mark.parametrize(
    "url",
    [
        "http://arxiv.org/list/math/recent",
        "https://example.invalid/oai2?token=private",
        "https://user@arxiv.org/list/math/recent",
        "https://arxiv.org:444/list/math/recent",
    ],
)
def test_disallowed_source_urls_are_rejected_before_opening(url: str) -> None:
    opener = FakeOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(ValueError, match="allowed arXiv HTTPS endpoint") as caught:
        client.get(url, interface=Interface.OAI, accept="application/xml")

    assert "private" not in str(caught.value)
    assert opener.requests == []


def test_external_redirect_target_is_rejected_before_transport_open() -> None:
    calls: list[str] = []

    class RedirectingHttpsHandler(BaseHandler):
        handler_order = 100

        def https_open(self, request: object) -> object:
            calls.append(request.full_url)
            if request.full_url != "https://export.arxiv.org/oai2":
                raise AssertionError("external redirect target was opened")
            headers = Message()
            headers["Location"] = "https://example.invalid/collect?token=private"
            headers["Content-Type"] = "text/plain"
            response = addinfourl(
                BytesIO(b"redirect"),
                headers,
                request.full_url,
                code=302,
            )
            response.msg = "Found"
            return response

    opener = build_opener(ArxivRedirectHandler(), RedirectingHttpsHandler())
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(ValueError, match="allowed arXiv HTTPS endpoint") as caught:
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )

    assert "private" not in str(caught.value)
    assert calls == ["https://export.arxiv.org/oai2"]


def test_disallowed_final_url_is_rejected_as_defense_in_depth() -> None:
    class ExternalFinalResponse(FakeResponse):
        def geturl(self) -> str:
            return "https://example.invalid/collect?token=private"

    class ExternalFinalOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            return ExternalFinalResponse()

    opener = ExternalFinalOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(ValueError, match="allowed arXiv HTTPS endpoint") as caught:
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )

    assert "private" not in str(caught.value)
    assert len(opener.requests) == 1


def test_unexpected_content_type_is_rejected_before_body_read() -> None:
    class HtmlResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__()
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, size: int = -1) -> bytes:
            raise AssertionError("body was read before content type validation")

    class HtmlOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            return HtmlResponse()

    opener = HtmlOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(ValueError, match="unexpected response content type"):
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
        )

    assert len(opener.requests) == 1


def test_response_larger_than_the_effective_interface_limit_is_rejected() -> None:
    class LargeResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__()
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return b"12345"

    response = LargeResponse()

    class LargeOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            return response

    opener = LargeOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                10,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(ValueError, match="response exceeded byte limit"):
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
            max_bytes=4,
        )

    assert response.read_sizes == [5]


def test_cancellation_before_retry_preserves_the_original_error() -> None:
    clock = FakeClock()
    cancellation = Event()
    original_error = http_error(503)

    class CancellingOpener(FakeOpener):
        def open(
            self,
            request: object,
            timeout: float | None = None,
        ) -> FakeResponse:
            self.requests.append(request)
            cancellation.set()
            raise original_error

    opener = CancellingOpener()
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                3,
                30,
                1_024,
                ("application/xml",),
            )
        },
    )

    with pytest.raises(HTTPError) as caught:
        client.get(
            "https://export.arxiv.org/oai2",
            interface=Interface.OAI,
            accept="application/xml",
            cancelled=cancellation.is_set,
        )

    assert caught.value is original_error
    assert len(opener.requests) == 1
    assert clock.sleeps == []


def test_default_policies_are_centralized_and_immutable() -> None:
    assert DEFAULT_POLICIES[Interface.ATOM].minimum_delay == 3
    assert DEFAULT_POLICIES[Interface.OAI].minimum_delay == 3
    assert DEFAULT_POLICIES[Interface.CATCHUP].minimum_delay == 15
    assert DEFAULT_POLICIES[Interface.PDF].minimum_delay == 3

    with pytest.raises(TypeError):
        DEFAULT_POLICIES[Interface.OAI] = RequestPolicy(
            0,
            1,
            0,
            1,
            ("application/xml",),
        )


def test_default_opener_installs_the_validating_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_handlers: list[object] = []
    opener = FakeOpener()

    def fake_build_opener(*handlers: object) -> FakeOpener:
        captured_handlers.extend(handlers)
        return opener

    monkeypatch.setattr(rate_limit, "build_opener", fake_build_opener)
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1_024,
                ("application/xml",),
            )
        },
    )

    response = client.get(
        "https://export.arxiv.org/oai2",
        interface=Interface.OAI,
        accept="application/xml",
    )

    assert response.status == 200
    assert len(captured_handlers) == 1
    assert isinstance(captured_handlers[0], ArxivRedirectHandler)
