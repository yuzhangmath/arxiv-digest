from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest

from arxiv_digest.models import AnnounceType, EnrichmentStatus
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.sources.catchup import (
    CatchupError,
    CatchupLayoutError,
    CatchupSource,
    failed_day,
    parse_catchup_page,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "catchup"
MAILING_DATE = date(2026, 8, 20)
PAGE_1_URL = "https://arxiv.org/catchup/cs.CL/2026-08-20"
PAGE_2_URL = f"{PAGE_1_URL}?skip=3&show=3"


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
            PAGE_1_URL: response(PAGE_1_URL, fixture("mixed-page-1.html")),
            PAGE_2_URL: response(PAGE_2_URL, fixture("mixed-page-2.html")),
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
    assert [request[0] for request in client.requests] == [PAGE_1_URL, PAGE_2_URL]
    assert all(request[1] is Interface.CATCHUP for request in client.requests)
    assert all("text/html" in request[2] for request in client.requests)


def test_fetch_day_forwards_one_cancellation_signal_to_every_page() -> None:
    client = FakeHttpClient(
        {
            PAGE_1_URL: response(PAGE_1_URL, fixture("mixed-page-1.html")),
            PAGE_2_URL: response(PAGE_2_URL, fixture("mixed-page-2.html")),
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
    url = "https://arxiv.org/catchup/cs.CL/2026-08-16"
    payload = fixture("empty-day.html")
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


def test_fetch_day_is_failed_when_an_advertised_page_does_not_succeed() -> None:
    client = FakeHttpClient(
        {
            PAGE_1_URL: response(PAGE_1_URL, fixture("mixed-page-1.html")),
            PAGE_2_URL: RuntimeError("private transport detail"),
        }
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert len(day.pages) == 1
    assert day.pages[0].page == 1
    assert day.error_code == "catchup_fetch_failed"
    assert day.error_message == "The arXiv catch-up page could not be retrieved."
    assert "private transport detail" not in day.error_message


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
        {PAGE_1_URL: response(PAGE_1_URL, fixture("schema-changed.html"))}
    )

    day = CatchupSource(client).fetch_day("cs.CL", MAILING_DATE)

    assert day.status is EnrichmentStatus.FAILED
    assert day.pages == ()
    assert day.error_code == "catchup_layout_changed"
    assert day.error_message == (
        "The arXiv catch-up page layout was not recognized."
    )
