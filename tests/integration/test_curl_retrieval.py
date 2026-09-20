from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.response import addinfourl

import pytest

from arxiv_digest.curl_transport import CurlResponse
from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    CategoryConfig,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    OaiArticle,
)
from arxiv_digest.rate_limit import ArxivHttpClient
from arxiv_digest.sources.catchup import CatchupSource, catchup_observations
from arxiv_digest.sources.oai import OaiSource, parse_get_record
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store
from arxiv_digest.sync import SyncService


FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
DAY = date(2026, 8, 20)
PAGE_URLS = tuple(
    f"https://arxiv.org/catchup/cs.CL/{DAY}?abs=True&page={page}"
    for page in (1, 2)
)


def _daily_pages() -> dict[str, bytes]:
    ending = b"            </div>\n          </dd>"
    abstract = b'<p class="mathjax">A deterministic inline abstract.</p>'
    return {
        url: (FIXTURES / "catchup" / f"current-page-{page}.html").read_bytes().replace(
            ending, abstract + ending
        )
        for page, url in enumerate(PAGE_URLS, start=1)
    }


class _Opener:
    def __init__(self, responses: dict[str, bytes | int], content_type: str) -> None:
        self.responses = responses
        self.content_type = content_type
        self.urls: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        value = self.responses.get(url, 406)
        headers = Message()
        headers["Content-Type"] = self.content_type
        if isinstance(value, int):
            raise HTTPError(url, value, "Synthetic refusal", headers, BytesIO(b"Not acceptable"))
        return addinfourl(BytesIO(value), headers, url, 200)


class _CurlTransport:
    def __init__(self, responses: dict[str, bytes | int], content_type: str) -> None:
        self.responses = responses
        self.content_type = content_type
        self.urls: list[str] = []

    def get(self, url, *, headers, timeout, max_bytes, cancelled=None):
        self.urls.append(url)
        assert timeout > 0
        assert max_bytes > 0
        assert cancelled is None or not cancelled()
        value = self.responses[url]
        return CurlResponse(
            status=value if isinstance(value, int) else 200,
            headers={"content-type": self.content_type},
            body=b"Not acceptable" if isinstance(value, int) else value,
        )


def _client(opener: _Opener, curl: _CurlTransport) -> ArxivHttpClient:
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    client = ArxivHttpClient(
        user_agent="arxiv-digest/test",
        contact_url="https://example.invalid/contact",
        opener=opener,
        curl_transport=curl,
        monotonic=lambda: elapsed[0],
        sleeper=sleep,
        wall_clock=lambda: NOW,
    )
    return client


@pytest.mark.parametrize("refused_page", (1, 2))
def test_daily_list_curl_fallback_retains_inline_abstracts_and_complete_pagination(
    refused_page: int,
) -> None:
    pages = _daily_pages()
    refused_url = PAGE_URLS[refused_page - 1]
    opener = _Opener({**pages, refused_url: 406}, "text/html")
    curl = _CurlTransport({refused_url: pages[refused_url]}, "text/html")

    result = CatchupSource(_client(opener, curl)).fetch_day("cs.CL", DAY)

    assert result.status is EnrichmentStatus.COMPLETE
    assert opener.urls == list(PAGE_URLS)
    assert curl.urls == [refused_url]
    assert len(result.pages) == 2
    observations = catchup_observations(result, NOW)
    assert len(observations) == 5
    assert {item.daily_list_date for item in observations} == {DAY}
    assert all(
        entry.metadata.abstract == "A deterministic inline abstract."
        for page in result.pages
        for entry in page.entries
    )


@pytest.mark.parametrize("bad_page", ("malformed-first", "malformed-second", "incomplete-second"))
def test_curl_response_cannot_confirm_a_malformed_or_partial_daily_list(
    tmp_path: Path, bad_page: str,
) -> None:
    pages = _daily_pages()
    refused_url = PAGE_URLS[0 if bad_page == "malformed-first" else 1]
    if bad_page == "incomplete-second":
        payload = pages[PAGE_URLS[1]].replace(
            b'<a name="item5">[5]</a>', b'<a name="item6">[6]</a>'
        )
    else:
        payload = b"<html><body>Unexpected intermediary response</body></html>"
    opener = _Opener({**pages, refused_url: 406}, "text/html")
    curl = _CurlTransport({refused_url: payload}, "text/html")
    source = CatchupSource(_client(opener, curl))
    database = tmp_path / "state.sqlite3"
    open_database(database).close()
    store = Store(database)
    config = CategoryConfig("cs.CL", "cs:CL", DAY)
    service = SyncService(store, None, None, source, clock=lambda: NOW)

    report = service.retry_failed_dates((config,), {config.category: (DAY,)})

    assert curl.urls == [refused_url]
    assert report.failed_dates == 1
    assert report.dates_with_papers == report.empty_dates == 0
    assert store.events_for_date(DAY) == ()
    assert store.catchup_day_records(config.category)[0].error_code == "catchup_layout_changed"
    with open_database(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "empty,observed_at,status",
    (
        (True, datetime(2026, 8, 20, 23, 59, 59, tzinfo=timezone.utc), None),
        (False, datetime(2026, 8, 20, 23, 59, 59, tzinfo=timezone.utc), None),
        (True, datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc), "empty"),
        (False, datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc), "complete"),
    ),
)
def test_curl_fallback_preserves_daily_list_finalization(
    tmp_path: Path, empty: bool, observed_at: datetime, status: str | None,
) -> None:
    pages = _daily_pages()
    if empty:
        pages = {PAGE_URLS[0]: (
            FIXTURES / "catchup" / "current-empty-day.html"
        ).read_bytes().replace(b"2026-08-16", b"2026-08-20")}
    opener = _Opener({url: 406 for url in pages}, "text/html")
    curl = _CurlTransport(pages, "text/html")
    source = CatchupSource(_client(opener, curl))
    database = tmp_path / "state.sqlite3"
    open_database(database).close()
    store = Store(database)
    config = CategoryConfig("cs.CL", "cs:CL", DAY)
    service = SyncService(store, None, None, source, clock=lambda: observed_at)

    service.retry_failed_dates((config,), {config.category: (DAY,)})

    records = store.catchup_day_records(config.category)
    if status is None:
        assert opener.urls == curl.urls == []
        assert records == ()
        assert store.events_for_date(DAY) == ()
    else:
        assert opener.urls == curl.urls == list(pages)
        assert records[0].status.value == status
        assert len(store.events_for_date(DAY)) == (0 if empty else 5)


def test_missing_abstract_retry_uses_curl_get_record_and_preserves_review_and_library(
    tmp_path: Path,
) -> None:
    payload = (FIXTURES / "oai" / "get-record.xml").read_bytes()
    record = parse_get_record(payload)
    assert isinstance(record, OaiArticle)
    config = CategoryConfig("synthetic.quantum", "synthetic:quantum-gardens", DAY)
    database = tmp_path / "state.sqlite3"
    open_database(database).close()
    store = Store(database)
    store.ensure_category_state(config.category, config.oai_set_spec, DAY)
    daily_list = CatchupDay(
        category=config.category,
        mailing_date=DAY,
        status=EnrichmentStatus.COMPLETE,
        pages=(CatchupPage(
            category=config.category,
            mailing_date=DAY,
            page=1,
            total_pages=1,
            entries=(CatchupEntry(replace(record.metadata, abstract=""), AnnounceType.NEW, DAY, 0),),
            raw_sha256="c" * 64,
        ),),
        error_code=None,
        error_message=None,
    )
    store.apply_catchup_day(daily_list, catchup_observations(daily_list, NOW), NOW)
    store.save_paper(record.metadata.arxiv_id, None)
    store.finish_date(DAY, through_revision=store.review_queue_revision(), finished_at=NOW)
    events_before = store.events_for_date(DAY)
    coverage_before = store.catchup_day_records(config.category)
    state_before = store.category_sync_state(config.category)
    revision_before = store.review_queue_revision()
    url = "https://oaipmh.arxiv.org/oai?" + urlencode({
        "verb": "GetRecord",
        "metadataPrefix": "arXivRaw",
        "identifier": record.oai_identifier,
    })
    opener = _Opener({url: 406}, "application/xml")
    curl = _CurlTransport({url: payload}, "application/xml")
    source = OaiSource(_client(opener, curl))
    service = SyncService(store, source, None, None, clock=lambda: NOW)

    report = service.retry_missing_abstracts((config,), day=DAY)

    assert report.abstract_retry is not None
    assert (report.abstract_retry.total, report.abstract_retry.recovered, report.abstract_retry.remaining) == (1, 1, 0)
    assert report.abstract_retry.error_codes == ()
    assert opener.urls == curl.urls == [url]
    assert store.article_metadata(record.metadata.arxiv_id) == record.metadata
    assert store.article_versions(record.metadata.arxiv_id) == record.versions
    assert store.saved_paper_metadata() == (record.metadata,)
    assert store.review_queue_revision() == revision_before
    assert store.catchup_day_records(config.category) == coverage_before
    assert store.category_sync_state(config.category) == state_before
    assert [(event.event_id, event.daily_list_date, event.reviewed_at) for event in store.events_for_date(DAY)] == [
        (event.event_id, event.daily_list_date, event.reviewed_at) for event in events_before
    ]
    assert all(event.reviewed_at == NOW for event in store.events_for_date(DAY))
    assert service.retry_missing_abstracts((config,), day=DAY).abstract_retry.total == 0
    assert opener.urls == curl.urls == [url]


def _metadata_checkpoint(tmp_path: Path) -> tuple[Store, CategoryConfig, dict[str, bytes]]:
    database = tmp_path / "state.sqlite3"
    open_database(database).close()
    store = Store(database)
    config = CategoryConfig("synthetic.orbital", "synthetic:orbital-dynamics", date(2026, 8, 1))
    store.ensure_category_state(config.category, config.oai_set_spec, config.coverage_start)
    checkpoint_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    run_id = store.begin_sync_run(
        config.category, "incremental", config.coverage_start, None, checkpoint_at,
    )
    store.complete_incremental_run(run_id, DAY, checkpoint_at, checkpoint_at)
    parameters = (
        {
            "verb": "ListRecords", "metadataPrefix": "arXivRaw",
            "set": config.oai_set_spec, "from": "2026-08-19",
        },
        {"verb": "ListRecords", "resumptionToken": "opaque+/second=="},
    )
    pages = {
        "https://oaipmh.arxiv.org/oai?" + urlencode(query): (
            FIXTURES / "oai" / f"page-{page}.xml"
        ).read_bytes()
        for page, query in enumerate(parameters, start=1)
    }
    return store, config, pages


class _EmptyAtom:
    def fetch(self, category, *, cancelled=None):
        return AtomBatch(category, DAY, (), "a" * 64, NOW)


@pytest.mark.parametrize("refused_page", (1, 2))
def test_metadata_list_records_curl_fallback_completes_chain_before_advancing_checkpoint(
    tmp_path: Path, refused_page: int,
) -> None:
    store, config, pages = _metadata_checkpoint(tmp_path)
    urls = tuple(pages)
    refused_url = urls[refused_page - 1]
    checkpoints_before_continuation = []

    class CheckpointOpener(_Opener):
        def open(self, request, timeout=None):
            if request.full_url == urls[1]:
                checkpoints_before_continuation.append(
                    store.category_sync_state(config.category).completed_through_utc
                )
                assert store.latest_sync_run(config.category, "incremental").pages_applied == 1
            return super().open(request, timeout)

    opener = CheckpointOpener({**pages, refused_url: 406}, "application/xml")
    curl = _CurlTransport({refused_url: pages[refused_url]}, "application/xml")
    source = OaiSource(_client(opener, curl))
    service = SyncService(store, source, _EmptyAtom(), None, clock=lambda: NOW)

    report = service.sync((config,), catchup_dates={})

    assert report.metadata_complete is True
    assert opener.urls == list(urls)
    assert curl.urls == [refused_url]
    assert checkpoints_before_continuation == [DAY]
    assert report.categories[0].metadata_sync.completed_through_utc == date(2026, 8, 22)
    assert report.categories[0].metadata_sync.last_error_code is None
    run = store.latest_sync_run(config.category, "incremental")
    assert run.status == "completed"
    assert run.pages_applied == 2
    assert store.article_metadata("2608.90001").abstract
    assert store.events_for_date(DAY) == ()
    assert store.catchup_day_records(config.category) == ()


@pytest.mark.parametrize(
    "fallback,error_code",
    (
        (b"<intermediary/>", None),
        (406, "arxiv_http_406"),
    ),
    ids=("malformed-final-page", "refused-final-page"),
)
def test_incomplete_metadata_curl_chain_retains_existing_checkpoint(
    tmp_path: Path, fallback: bytes | int, error_code: str | None,
) -> None:
    store, config, pages = _metadata_checkpoint(tmp_path)
    urls = tuple(pages)
    opener = _Opener({**pages, urls[1]: 406}, "application/xml")
    curl = _CurlTransport({urls[1]: fallback}, "application/xml")
    source = OaiSource(_client(opener, curl))
    service = SyncService(store, source, _EmptyAtom(), None, clock=lambda: NOW)

    report = service.sync((config,), catchup_dates={})

    assert curl.urls == [urls[1]]
    assert opener.urls == list(urls)
    assert report.metadata_complete is False
    progress = report.categories[0].metadata_sync
    assert progress.status == "failed"
    assert progress.completed_through_utc == DAY
    assert progress.last_error_code is not None
    if error_code is not None:
        assert progress.last_error_code == error_code
    run = store.latest_sync_run(config.category, "incremental")
    assert run.status == "failed"
    assert run.pages_applied == 1
    assert store.article_metadata("2608.90001").abstract
    assert store.events_for_date(DAY) == ()
