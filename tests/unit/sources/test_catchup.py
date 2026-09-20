from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from email.message import Message
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.response import addinfourl

import pytest

from arxiv_digest.models import (
    AnnounceType,
    CatchupDay,
    EnrichmentStatus,
    EvidenceSource,
)
from arxiv_digest.rate_limit import ArxivHttpClient, HttpResponse, Interface
from arxiv_digest.sources.catchup import (
    CatchupError,
    CatchupLayoutError,
    CatchupSource,
    catchup_observations,
    failed_day,
    parse_catchup_page,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "catchup"
MAILING_DATE = date(2026, 8, 20)
PAGE_1_URL = "https://arxiv.org/catchup/cs.CL/2026-08-20"
PAGE_2_URL = f"{PAGE_1_URL}?skip=3&show=3"
CURRENT_PAGE_1_URL = f"{PAGE_1_URL}?abs=True&page=1"
CURRENT_PAGE_2_URL = f"{PAGE_1_URL}?abs=True&page=2"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeHttpClient:
    def __init__(self, responses: dict[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Interface, str]] = []
        self.cancellations: list[object] = []

    def get(
        self,
        url: str,
        *,
        interface: Interface,
        accept: str,
        **_options: object,
    ) -> HttpResponse:
        self.requests.append((url, interface, accept))
        self.cancellations.append(_options.get("cancelled"))
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def response(url: str, body: bytes) -> HttpResponse:
    return HttpResponse(
        status=200,
        final_url=url,
        headers={"content-type": "text/html; charset=utf-8"},
        body=body,
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )


def test_parse_catchup_page_retains_new_announcement_evidence() -> None:
    payload = fixture("mixed-page-1.html")

    page = parse_catchup_page(payload, "cs.CL", MAILING_DATE, PAGE_1_URL)

    assert page.category == "cs.CL"
    assert page.mailing_date == MAILING_DATE
    assert page.page == 1
    assert page.total_pages == 2
    assert page.raw_sha256 == sha256(payload).hexdigest()
    entry = page.entries[0]
    assert entry.section is AnnounceType.NEW
    assert entry.position == 0
    assert entry.mailing_date == MAILING_DATE
    assert entry.announced_version is None
    assert entry.metadata.arxiv_id == "2608.11001"
    assert entry.metadata.title == "Synthetic Lanterns for Calm Parsing"
    assert entry.metadata.authors == ("Ada Fixture", "Ben Example")
    assert entry.metadata.abstract == "A wholly synthetic catch-up abstract."
    assert entry.metadata.primary_category == "cs.CL"
    assert entry.metadata.categories == ("cs.CL", "stat.ML")
    assert entry.metadata.comments == "Nine synthetic pages"
    assert entry.metadata.journal_ref == "Journal of Fixture Studies 2 (2026)"
    assert entry.metadata.doi == "10.0000/fixture.11001"


def test_catchup_day_normalizes_exact_daily_list_observations() -> None:
    page = parse_catchup_page(
        fixture("mixed-page-1.html"),
        "cs.CL",
        MAILING_DATE,
        PAGE_1_URL,
    )
    day = CatchupDay(
        category="cs.CL",
        mailing_date=MAILING_DATE,
        status=EnrichmentStatus.COMPLETE,
        pages=(page,),
        error_code=None,
        error_message=None,
    )
    observed_at = datetime(2026, 8, 22, tzinfo=timezone.utc)

    observations = catchup_observations(day, observed_at)

    first = observations[0]
    assert first.source_key == (
        "catchup:cs.CL:2026-08-20:2608.11001:0"
    )
    assert first.arxiv_id == "2608.11001"
    assert first.source is EvidenceSource.CATCHUP
    assert first.category == "cs.CL"
    assert first.announce_type is AnnounceType.NEW
    assert first.daily_list_date == MAILING_DATE
    assert first.announced_version is None
    assert first.list_position == 0
    assert first.oai_datestamp is None
    assert first.response_sha256 == page.raw_sha256
    assert first.observed_at == observed_at


def test_parse_catchup_page_maps_all_html_sections_in_list_order() -> None:
    page = parse_catchup_page(
        fixture("mixed-page-1.html"),
        "cs.CL",
        MAILING_DATE,
        PAGE_1_URL,
    )

    assert tuple(
        (entry.section, entry.position, entry.metadata.arxiv_id)
        for entry in page.entries
    ) == (
        (AnnounceType.NEW, 0, "2608.11001"),
        (AnnounceType.CROSS, 1, "2608.11002"),
        (AnnounceType.REPLACE, 2, "2608.11003"),
    )
    assert all(
        entry.section is not AnnounceType.REPLACE_CROSS
        for entry in page.entries
    )


def test_parse_current_nested_listing_without_abstracts() -> None:
    payload = fixture("current-page-1.html")

    page = parse_catchup_page(
        payload,
        "cs.CL",
        MAILING_DATE,
        CURRENT_PAGE_1_URL,
    )

    assert page.page == 1
    assert page.total_pages == 2
    assert tuple(entry.section for entry in page.entries) == (
        AnnounceType.NEW,
        AnnounceType.CROSS,
        AnnounceType.REPLACE,
    )
    entry = page.entries[0]
    assert entry.metadata.abstract == ""
    assert entry.metadata.primary_category == "cs.CL"
    assert entry.metadata.categories == ("cs.CL", "stat.ML", "cs.IT")


def test_parse_historical_abstract_disabled_page_remains_supported() -> None:
    payload = fixture("current-page-1.html").replace(
        b"abs=True", b"abs=placeholder",
    ).replace(b"abs=False", b"abs=True").replace(b"abs=placeholder", b"abs=False")

    page = parse_catchup_page(
        payload, "cs.CL", MAILING_DATE, CURRENT_PAGE_1_URL.replace("abs=True", "abs=False"),
    )

    assert page.total_pages == 2
    assert page.entries[0].metadata.abstract == ""


def test_current_page_number_comes_from_url_when_it_has_no_self_link() -> None:
    page = parse_catchup_page(
        fixture("current-page-2.html"),
        "cs.CL",
        MAILING_DATE,
        CURRENT_PAGE_2_URL,
    )

    assert page.page == 2
    assert page.total_pages == 2
    assert tuple(entry.position for entry in page.entries) == (3, 4)


def test_current_heading_count_must_match_the_parsed_section() -> None:
    payload = fixture("current-page-1.html").replace(
        b"New submissions (showing first 1 of 2 entries)",
        b"New submissions (showing 2 of 2 entries)",
        1,
    )

    with pytest.raises(CatchupLayoutError):
        parse_catchup_page(
            payload,
            "cs.CL",
            MAILING_DATE,
            CURRENT_PAGE_1_URL,
        )


def test_current_pagination_rejects_an_unreasonable_page_count() -> None:
    payload = fixture("current-page-1.html").replace(
        b"</nav>",
        b'<a href="?abs=True&amp;page=101">101</a></nav>',
        1,
    )

    with pytest.raises(CatchupLayoutError):
        parse_catchup_page(
            payload,
            "cs.CL",
            MAILING_DATE,
            CURRENT_PAGE_1_URL,
        )


def test_current_metadata_never_claims_a_historical_announced_version() -> None:
    page = parse_catchup_page(
        fixture("current-version-history.html"),
        "cs.CL",
        date(2024, 1, 12),
        "https://arxiv.org/catchup/cs.CL/2024-01-12",
    )

    entry = page.entries[0]
    assert entry.metadata.arxiv_id == "2401.01234"
    assert entry.metadata.title == "Current Metadata After Several Revisions"
    assert entry.metadata.primary_category == "cs.CL"
    assert entry.metadata.categories == ("cs.CL", "stat.ML")
    assert entry.announced_version is None
    assert not hasattr(entry.metadata, "version")


def test_fetch_day_follows_same_path_pages_in_source_order_once() -> None:
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                CURRENT_PAGE_1_URL,
                fixture("current-page-1.html"),
            ),
            CURRENT_PAGE_2_URL: response(
                CURRENT_PAGE_2_URL,
                fixture("current-page-2.html"),
            ),
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.COMPLETE
    assert day.error_code is None
    assert day.error_message is None
    assert tuple((page.page, page.total_pages) for page in day.pages) == (
        (1, 2),
        (2, 2),
    )
    assert tuple(
        entry.metadata.arxiv_id
        for page in day.pages
        for entry in page.entries
    ) == (
        "2608.11001",
        "2608.11002",
        "2608.11003",
        "2608.11004",
        "hep-th/9901001",
    )
    assert [request[0] for request in client.requests] == [
        CURRENT_PAGE_1_URL,
        CURRENT_PAGE_2_URL,
    ]
    assert all(request[1] is Interface.CATCHUP for request in client.requests)
    assert all("text/html" in request[2] for request in client.requests)


def test_fetch_day_recovers_abstracts_with_daily_membership_in_the_same_requests() -> None:
    ending = b"            </div>\n          </dd>"
    abstract = b"<p class=\"mathjax\">A synthetic abstract with <em>inline text</em>.</p>"
    client = FakeHttpClient({
        url: response(url, fixture(name).replace(ending, abstract + ending))
        for url, name in (
            (CURRENT_PAGE_1_URL, "current-page-1.html"),
            (CURRENT_PAGE_2_URL, "current-page-2.html"),
        )
    })

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.COMPLETE
    assert [request[0] for request in client.requests] == [CURRENT_PAGE_1_URL, CURRENT_PAGE_2_URL]
    entries = [entry for page in day.pages for entry in page.entries]
    assert len(entries) == 5
    assert all(entry.metadata.abstract == "A synthetic abstract with inline text ." for entry in entries)
    assert all(entry.announced_version is None for entry in entries)


def test_abstract_text_and_links_cannot_change_daily_list_count_or_pagination() -> None:
    ending = b"            </div>\n          </dd>"
    abstract = (
        b'<p class="mathjax">We study a total of 7 entries and cite '
        b'<a href="?abs=True&amp;page=100">a synthetic reference</a>.</p>'
    )
    client = FakeHttpClient({
        url: response(url, fixture(name).replace(ending, abstract + ending))
        for url, name in (
            (CURRENT_PAGE_1_URL, "current-page-1.html"),
            (CURRENT_PAGE_2_URL, "current-page-2.html"),
        )
    })

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.COMPLETE
    assert [request[0] for request in client.requests] == [CURRENT_PAGE_1_URL, CURRENT_PAGE_2_URL]
    assert len(catchup_observations(day, datetime(2026, 8, 22, tzinfo=timezone.utc))) == 5


def test_fetch_day_rejects_a_day_wide_entry_count_mismatch() -> None:
    page_one = fixture("current-page-1.html").replace(
        b"Total of 5 entries",
        b"Total of 6 entries",
    )
    page_two = fixture("current-page-2.html").replace(
        b"Total of 5 entries",
        b"Total of 6 entries",
    )
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(CURRENT_PAGE_1_URL, page_one),
            CURRENT_PAGE_2_URL: response(CURRENT_PAGE_2_URL, page_two),
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.error_code == "catchup_layout_changed"
    assert len(day.pages) == 2


def test_fetch_day_synthesizes_unadvertised_intermediate_pages() -> None:
    page_3_url = f"{PAGE_1_URL}?abs=True&page=3"
    first_payload = fixture("current-page-1.html").replace(
        b"?abs=True&amp;page=2",
        b"?abs=True&amp;page=3",
        1,
    ).replace(b"Total of 5 entries", b"Total of 7 entries").replace(
        b"first 1 of 2 entries", b"first 1 of 3 entries"
    )
    middle_payload = fixture("current-page-2.html").replace(
        b"</nav>",
        b'<a href="?abs=True&amp;page=3">3</a></nav>',
        1,
    ).replace(b"Total of 5 entries", b"Total of 7 entries").replace(
        b"last 1 of 2 entries", b"1 of 3 entries"
    )
    last_payload = (
        fixture("current-page-2.html")
        .replace(b"Total of 5 entries", b"Total of 7 entries")
        .replace(b"last 1 of 2 entries", b"last 1 of 3 entries")
        .replace(b'name="item4"', b'name="item6"')
        .replace(b'name="item5"', b'name="item7"')
    )
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(CURRENT_PAGE_1_URL, first_payload),
            CURRENT_PAGE_2_URL: response(CURRENT_PAGE_2_URL, middle_payload),
            page_3_url: response(page_3_url, last_payload),
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.COMPLETE
    assert [request[0] for request in client.requests] == [
        CURRENT_PAGE_1_URL,
        CURRENT_PAGE_2_URL,
        page_3_url,
    ]
    assert tuple(page.page for page in day.pages) == (1, 2, 3)


def test_fetch_day_forwards_one_cancellation_signal_to_every_page() -> None:
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                CURRENT_PAGE_1_URL,
                fixture("current-page-1.html"),
            ),
            CURRENT_PAGE_2_URL: response(
                CURRENT_PAGE_2_URL,
                fixture("current-page-2.html"),
            ),
        }
    )
    cancellation = Event()
    cancelled = cancellation.is_set

    CatchupSource(client).fetch_day(
        "cs.CL", MAILING_DATE, cancelled=cancelled
    )

    assert client.cancellations == [cancelled, cancelled]


def test_old_style_detail_id_is_versionless_replacement_evidence() -> None:
    page = parse_catchup_page(
        fixture("mixed-page-2.html"),
        "cs.CL",
        MAILING_DATE,
        PAGE_2_URL,
    )

    entry = page.entries[1]
    assert entry.metadata.arxiv_id == "hep-th/9901001"
    assert entry.section is AnnounceType.REPLACE
    assert entry.section is not AnnounceType.REPLACE_CROSS
    assert entry.position == 4
    assert entry.announced_version is None


def test_fetch_day_distinguishes_an_explicitly_empty_date() -> None:
    mailing_date = date(2026, 8, 16)
    url = "https://arxiv.org/catchup/cs.CL/2026-08-16?abs=True&page=1"
    payload = fixture("current-empty-day.html")
    client = FakeHttpClient({url: response(url, payload)})

    day = CatchupSource(client).fetch_day("cs.CL", mailing_date)

    assert day.status is EnrichmentStatus.EMPTY
    assert day.error_code is None
    assert day.error_message is None
    assert len(day.pages) == 1
    assert day.pages[0].entries == ()
    assert day.pages[0].raw_sha256 == sha256(payload).hexdigest()


def test_failed_day_preserves_only_safe_diagnostics() -> None:
    error = CatchupError("synthetic_failure", "A safe synthetic failure.")

    day = failed_day("cs.CL", MAILING_DATE, error)

    assert day.category == "cs.CL"
    assert day.mailing_date == MAILING_DATE
    assert day.status is EnrichmentStatus.FAILED
    assert day.pages == ()
    assert day.error_code == "synthetic_failure"
    assert day.error_message == "A safe synthetic failure."


@pytest.mark.parametrize("status", (403, 406, 503))
@pytest.mark.parametrize("http_error", (False, True))
def test_fetch_day_retains_http_status_without_private_response_details(
    status: int, http_error: bool,
) -> None:
    if http_error:
        result = HTTPError(
            CURRENT_PAGE_1_URL, status, "private transport detail", {}, None
        )
    else:
        result = HttpResponse(
            status=status, final_url=CURRENT_PAGE_1_URL,
            headers={"content-type": "text/html"},
            body=b"private transport detail",
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
    client = FakeHttpClient({
        CURRENT_PAGE_1_URL: result,
        f"{PAGE_1_URL}?abs=False&page=1": result,
    })

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.error_code == f"catchup_http_{status}"
    assert day.error_message == f"The arXiv catch-up request returned HTTP {status}."
    assert len(client.requests) == (2 if status == 406 else 1)


@pytest.mark.parametrize("http_error", (False, True))
def test_initial_406_recovers_complete_membership_with_abstracts_disabled(
    http_error: bool,
) -> None:
    first = f"{PAGE_1_URL}?abs=False&page=1"
    second = f"{PAGE_1_URL}?abs=False&page=2"
    payload = fixture("current-page-1.html").replace(
        b'<a href="?abs=False&amp;page=3">abstracts disabled</a>', b""
    ).replace(b"abs=True", b"abs=False")
    failure = (
        HTTPError(CURRENT_PAGE_1_URL, 406, "private transport detail", {}, None)
        if http_error else replace(response(CURRENT_PAGE_1_URL, b""), status=406)
    )
    client = FakeHttpClient({
        CURRENT_PAGE_1_URL: failure,
        first: response(first, payload),
        second: response(
            second, fixture("current-page-2.html").replace(b"abs=True", b"abs=False")
        ),
    })
    cancelled = Event().is_set

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE, cancelled=cancelled)

    assert day.status is EnrichmentStatus.COMPLETE
    assert [request[0] for request in client.requests] == [
        CURRENT_PAGE_1_URL, first, second,
    ]
    assert all(request[1] is Interface.CATCHUP for request in client.requests)
    assert client.cancellations == [cancelled] * 3
    assert len(catchup_observations(day, datetime(2026, 8, 22, tzinfo=timezone.utc))) == 5
    assert all(not entry.metadata.abstract for page in day.pages for entry in page.entries)


def test_fallback_still_requires_every_advertised_page() -> None:
    first = f"{PAGE_1_URL}?abs=False&page=1"
    second = f"{PAGE_1_URL}?abs=False&page=2"
    payload = fixture("current-page-1.html").replace(
        b'<a href="?abs=False&amp;page=3">abstracts disabled</a>', b""
    ).replace(b"abs=True", b"abs=False")
    client = FakeHttpClient({
        CURRENT_PAGE_1_URL: HTTPError(CURRENT_PAGE_1_URL, 406, "unavailable", {}, None),
        first: response(first, payload),
        second: HTTPError(second, 406, "unavailable", {}, None),
    })

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.error_code == "catchup_http_406"
    assert len(day.pages) == 1
    assert catchup_observations(day, datetime(2026, 8, 22, tzinfo=timezone.utc)) == ()
    assert [request[0] for request in client.requests] == [
        CURRENT_PAGE_1_URL, first, second,
    ]


def test_later_406_does_not_restart_a_partially_recovered_list_in_another_mode() -> None:
    client = FakeHttpClient({
        CURRENT_PAGE_1_URL: response(CURRENT_PAGE_1_URL, fixture("current-page-1.html")),
        CURRENT_PAGE_2_URL: HTTPError(CURRENT_PAGE_2_URL, 406, "unavailable", {}, None),
    })

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.error_code == "catchup_http_406"
    assert [request[0] for request in client.requests] == [
        CURRENT_PAGE_1_URL, CURRENT_PAGE_2_URL,
    ]


@pytest.mark.parametrize("http_status", (406, 429))
def test_explicit_rate_limit_never_uses_abstract_free_fallback(http_status: int) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    error = ArxivRateLimited(
        retry_at=datetime(2026, 8, 22, 13, tzinfo=timezone.utc), http_status=http_status,
    )
    client = FakeHttpClient({CURRENT_PAGE_1_URL: error})

    with pytest.raises(ArxivRateLimited) as raised:
        CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert raised.value is error
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    ("status", "body", "fallback"),
    ((406, b"Not acceptable", True), (406, b"Rate exceeded", False), (429, b"Busy", False)),
)
def test_fallback_uses_shared_transport_pacing_and_obeys_throttle_responses(
    status: int, body: bytes, fallback: bool,
) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    mailing_date = date(2026, 8, 16)
    first = "https://arxiv.org/catchup/cs.CL/2026-08-16?abs=True&page=1"
    second = first.replace("abs=True", "abs=False")
    elapsed = [0.0]
    requests: list[tuple[str, float]] = []
    headers = Message()
    headers["Content-Type"] = "text/html"

    class Opener:
        def open(self, request, timeout=None):
            requests.append((request.full_url, elapsed[0]))
            if request.full_url == first:
                raise HTTPError(first, status, "failure", headers, BytesIO(body))
            assert request.full_url == second
            return addinfourl(BytesIO(fixture("current-empty-day.html")), headers, second, 200)

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    client = ArxivHttpClient(
        user_agent="arxiv-digest/test",
        contact_url="https://example.invalid/contact",
        opener=Opener(),
        monotonic=lambda: elapsed[0],
        sleeper=sleep,
    )
    if fallback:
        day = CatchupSource(client).fetch_day("cs.CL", mailing_date)
        assert day.status is EnrichmentStatus.EMPTY
        assert requests == [(first, 0.0), (second, 15.0)]
    else:
        with pytest.raises(ArxivRateLimited):
            CatchupSource(client).fetch_day("cs.CL", mailing_date)
        assert requests == [(first, 0.0)]


@pytest.mark.parametrize("page_number", (1, 2))
def test_fetch_day_propagates_throttling_so_the_sync_batch_can_stop(page_number: int) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    error = ArxivRateLimited(
        retry_at=datetime(2026, 8, 22, 13, tzinfo=timezone.utc),
        http_status=429,
    )
    responses = {
        CURRENT_PAGE_1_URL: response(CURRENT_PAGE_1_URL, fixture("current-page-1.html")),
        (CURRENT_PAGE_1_URL if page_number == 1 else CURRENT_PAGE_2_URL): error,
    }
    client = FakeHttpClient(responses)

    with pytest.raises(ArxivRateLimited) as raised:
        CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert raised.value is error
    assert len(client.requests) == page_number


def test_fetch_day_propagates_unavailable_cooldown_state() -> None:
    from arxiv_digest.arxiv_access import ArxivCooldownUnavailable

    client = FakeHttpClient({CURRENT_PAGE_1_URL: ArxivCooldownUnavailable()})

    with pytest.raises(ArxivCooldownUnavailable):
        CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)


def test_cooldown_between_pages_still_counts_as_an_attempted_day() -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    error = ArxivRateLimited(
        retry_at=datetime(2026, 8, 22, 13, tzinfo=timezone.utc),
        http_status=429, attempted=False,
    )
    client = FakeHttpClient({
        CURRENT_PAGE_1_URL: response(CURRENT_PAGE_1_URL, fixture("current-page-1.html")),
        CURRENT_PAGE_2_URL: error,
    })

    with pytest.raises(ArxivRateLimited) as raised:
        CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert raised.value.attempted is True


def test_fetch_day_is_failed_when_an_advertised_page_does_not_succeed() -> None:
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                CURRENT_PAGE_1_URL,
                fixture("current-page-1.html"),
            ),
            CURRENT_PAGE_2_URL: RuntimeError("private transport detail"),
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert len(day.pages) == 1
    assert day.pages[0].page == 1
    assert day.error_code == "catchup_fetch_failed"
    assert day.error_message == "The arXiv catch-up page could not be retrieved."
    assert "private transport detail" not in day.error_message


def test_failed_multi_page_day_does_not_normalize_partial_observations() -> None:
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                CURRENT_PAGE_1_URL,
                fixture("current-page-1.html"),
            ),
            CURRENT_PAGE_2_URL: RuntimeError("private transport detail"),
        }
    )
    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    observations = catchup_observations(
        day,
        datetime(2026, 8, 22, tzinfo=timezone.utc),
    )

    assert observations == ()


def test_missing_structural_anchor_is_a_typed_layout_error() -> None:
    with pytest.raises(CatchupLayoutError) as caught:
        parse_catchup_page(
            fixture("schema-changed.html"),
            "cs.CL",
            MAILING_DATE,
            PAGE_1_URL,
        )

    assert caught.value.code == "catchup_layout_changed"
    assert caught.value.safe_message == (
        "The arXiv catch-up page layout was not recognized."
    )


def test_fetch_day_marks_a_layout_change_failed_instead_of_empty() -> None:
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                CURRENT_PAGE_1_URL,
                fixture("schema-changed.html"),
            )
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.pages == ()
    assert day.error_code == "catchup_layout_changed"
    assert day.error_message == (
        "The arXiv catch-up page layout was not recognized."
    )


def test_fetch_day_rejects_a_redirect_to_a_different_historical_date() -> None:
    redirected_url = (
        "https://arxiv.org/catchup/cs.CL/2026-08-19?abs=True&page=1"
    )
    client = FakeHttpClient(
        {
            CURRENT_PAGE_1_URL: response(
                redirected_url,
                fixture("current-page-1.html"),
            )
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.pages == ()
    assert day.error_code == "catchup_layout_changed"
