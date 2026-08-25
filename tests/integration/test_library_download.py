from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from arxiv_digest.downloads import DownloadError, DownloadManager
from arxiv_digest.library import LibraryService
from arxiv_digest.models import PaperMetadata, PaperVersion
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import DownloadFileRecord, Store


class InvalidPdfClient:
    def get(
        self,
        url: str,
        *,
        interface: Interface,
        accept: str,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        return HttpResponse(
            status=200,
            final_url=url,
            headers={"content-type": "text/html"},
            body=b"synthetic failure",
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )


def configured_services(
    root: Path,
) -> tuple[Store, LibraryService, DownloadManager]:
    database_path = root / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id="2608.33001",
            title="Persistent Synthetic Save",
            authors=("Rowan Example",),
            abstract="A fictional partial-failure integration record.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        (
            PaperVersion(
                1,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            ),
        ),
    )
    destination = root / "PDFs"
    destination.mkdir()
    profiles = ProfileRepository(root / "profile.json", root / "profile.lock")
    profiles.save_atomic(
        Profile(
            schema_version=2,
            revision=1,
            category_coverage=(
                ProfileCategory("cs.SE", date(2026, 8, 1)),
            ),
            keywords=(),
            phrases=(),
            authors=(),
            seed_papers=(),
            pdf_destination=PdfDestination("custom", destination),
        ),
        expected_revision=None,
    )
    return (
        store,
        LibraryService(store),
        DownloadManager(
            store,
            profiles,
            InvalidPdfClient(),
            max_pdf_bytes=1024,
        ),
    )


def test_save_plus_pdf_keeps_the_library_save_when_download_fails(
    tmp_path: Path,
) -> None:
    _, library, downloads = configured_services(tmp_path)

    with pytest.raises(DownloadError):
        downloads.download("2608.33001", 1, save_first=True)

    page = library.search("Persistent", limit=20, offset=0)
    assert [entry.metadata.arxiv_id for entry in page.entries] == [
        "2608.33001"
    ]
    assert page.entries[0].saved_version == 1


def test_unconfirmed_save_plus_pdf_saves_unpinned_before_download_fails(
    tmp_path: Path,
) -> None:
    _, library, downloads = configured_services(tmp_path)

    with pytest.raises(DownloadError):
        downloads.download(
            "2608.33001",
            1,
            save_first=True,
            save_version=None,
        )

    page = library.search("Persistent", limit=20, offset=0)
    assert [entry.metadata.arxiv_id for entry in page.entries] == [
        "2608.33001"
    ]
    assert page.entries[0].saved_version is None


def test_download_state_rejects_a_version_missing_from_the_stored_article(
    tmp_path: Path,
) -> None:
    store, _, _ = configured_services(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        store.record_download_file(
            DownloadFileRecord(
                arxiv_id="2608.33001",
                version=2,
                filename="2608.33001v2 - Synthetic.pdf",
                byte_count=10,
                sha256=sha256(b"synthetic").hexdigest(),
                last_verified_at=datetime(
                    2026,
                    8,
                    22,
                    tzinfo=timezone.utc,
                ),
            )
        )
