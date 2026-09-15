from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from arxiv_digest.library import LibraryService
from arxiv_digest.models import OaiTombstone, PaperMetadata, PaperVersion
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import DownloadFileRecord, Store


def seed_article(
    store: Store,
    arxiv_id: str,
    title: str,
    *,
    submitted_at: datetime = datetime(2026, 8, 1, tzinfo=timezone.utc),
    revised_at: datetime = datetime(2026, 8, 20, tzinfo=timezone.utc),
) -> None:
    store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id=arxiv_id,
            title=title,
            authors=("Aster Example",),
            abstract="A fictional library-search abstract.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        (
            PaperVersion(
                1,
                submitted_at,
            ),
            PaperVersion(
                2,
                revised_at,
            ),
        ),
    )


def test_saved_paper_exposes_a_newer_available_version(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    seed_article(store, "2608.31001", "Fictional Library Lanterns")
    service = LibraryService(store)

    service.save("2608.31001", version=1)
    page = service.search("Lanterns", limit=20, offset=0)

    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.metadata.arxiv_id == "2608.31001"
    assert entry.saved_version == 1
    assert entry.latest_version == 2
    assert entry.new_version_available is True


def test_tombstoned_paper_and_local_pdf_presence_are_independent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    seed_article(store, "2608.31008", "Archived Fictional Paper")
    store.save_paper("2608.31008", 1)
    store.record_download_file(
        DownloadFileRecord(
            arxiv_id="2608.31008",
            version=1,
            filename="2608.31008v1 - Archived Fictional Paper.pdf",
            byte_count=12,
            sha256="a" * 64,
            last_verified_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
    )
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 8, 1))
    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 8, 1),
        None,
        datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiTombstone(
                oai_identifier="oai:arXiv.org:2608.31008",
                oai_datestamp=date(2026, 8, 22),
                set_specs=("cs:SE",),
            ),
        ),
        (),
        "b" * 64,
        datetime(2026, 8, 22, tzinfo=timezone.utc),
    )

    entry = LibraryService(store).search("", limit=20, offset=0).entries[0]

    assert entry.paper_available is False
    assert entry.local_pdf_versions == (1,)
    assert entry.new_version_available is False


def test_remove_hides_the_paper_from_the_saved_library(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    seed_article(store, "2608.31002", "Removable Fictional Paper")
    service = LibraryService(store)
    service.save("2608.31002", version=2)

    service.remove("2608.31002")

    assert service.search("", limit=20, offset=0).entries == ()


def test_library_lists_newest_papers_first_by_latest_version_date(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    service = LibraryService(store)
    seed_article(
        store,
        "2608.31007",
        "Older paper with a newer revision",
        submitted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        revised_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    seed_article(
        store,
        "2608.31008",
        "Newer paper",
        submitted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        revised_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    service.save("2608.31007", version=1)
    service.save("2608.31008", version=2)

    page = service.search("", limit=20, offset=0)

    assert [entry.metadata.arxiv_id for entry in page.entries] == [
        "2608.31007",
        "2608.31008",
    ]


def test_library_search_uses_latest_version_date_as_the_relevance_tie_breaker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    service = LibraryService(store)
    seed_article(
        store,
        "2608.31009",
        "Shared search phrase",
        submitted_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        revised_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    seed_article(
        store,
        "2608.31010",
        "Shared search phrase",
        submitted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        revised_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    service.save("2608.31009", version=1)
    service.save("2608.31010", version=2)

    page = service.search("Shared", limit=20, offset=0)

    assert [entry.metadata.arxiv_id for entry in page.entries] == [
        "2608.31009",
        "2608.31010",
    ]


@pytest.mark.parametrize("query", ("", "Shared"))
def test_library_orders_equal_dates_stably_and_missing_dates_last(
    tmp_path: Path, query: str
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    service = LibraryService(store)
    for arxiv_id in ("2608.31012", "2608.31013", "2608.31011"):
        metadata = PaperMetadata(
            arxiv_id=arxiv_id,
            title="Shared search phrase",
            authors=("Aster Example",),
            abstract="A fictional library-search abstract.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        )
        versions = (
            ()
            if arxiv_id == "2608.31013"
            else (PaperVersion(1, datetime(2026, 8, 20, tzinfo=timezone.utc)),)
        )
        store.apply_article_snapshot(metadata, versions)
        service.save(arxiv_id, version=None)

    first = service.search(query, limit=2, offset=0)
    second = service.search(query, limit=2, offset=first.next_offset or 0)

    assert [entry.metadata.arxiv_id for entry in first.entries] == [
        "2608.31012",
        "2608.31011",
    ]
    assert first.next_offset == 2
    assert [entry.metadata.arxiv_id for entry in second.entries] == [
        "2608.31013"
    ]
    assert second.entries[0].latest_version is None
    assert second.next_offset is None


def test_full_last_page_does_not_offer_an_empty_next_page(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    service = LibraryService(store)
    for suffix in ("03", "04"):
        arxiv_id = f"2608.310{suffix}"
        seed_article(store, arxiv_id, f"Synthetic Page {suffix}")
        service.save(arxiv_id, version=2)

    page = service.search("", limit=2, offset=0)

    assert [entry.metadata.arxiv_id for entry in page.entries] == [
        "2608.31004",
        "2608.31003",
    ]
    assert page.next_offset is None


def test_pagination_offers_and_consumes_a_real_next_page(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    store = Store(path)
    service = LibraryService(store)
    for suffix in ("05", "06", "07"):
        arxiv_id = f"2608.310{suffix}"
        seed_article(store, arxiv_id, f"Synthetic Page {suffix}")
        service.save(arxiv_id, version=2)

    first = service.search("", limit=2, offset=0)
    second = service.search("", limit=2, offset=first.next_offset or 0)

    assert first.next_offset == 2
    assert [entry.metadata.arxiv_id for entry in first.entries] == [
        "2608.31007",
        "2608.31006",
    ]
    assert [entry.metadata.arxiv_id for entry in second.entries] == [
        "2608.31005"
    ]
    assert second.next_offset is None
