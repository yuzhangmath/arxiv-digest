from __future__ import annotations

import stat
import threading
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote

import pytest

import arxiv_digest.downloads as downloads_module
from arxiv_digest.downloads import (
    DownloadError,
    DownloadManager,
    DownloadResult,
    arxiv_pdf_url,
    safe_pdf_filename,
)
from arxiv_digest.models import PaperMetadata, PaperVersion
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


PDF_BYTES = b"%PDF-1.7\n% synthetic fixture\n"


class PdfClient:
    def __init__(
        self,
        body: bytes = PDF_BYTES,
        content_type: str = "application/pdf",
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.calls: list[tuple[str, Interface, str, int | None]] = []

    def get(
        self,
        url: str,
        *,
        interface: Interface,
        accept: str,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append((url, interface, accept, max_bytes))
        return HttpResponse(
            status=200,
            final_url=url,
            headers={"content-type": self.content_type},
            body=self.body,
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )


def seeded_store(
    path: Path,
    arxiv_id: str = "2608.32010",
    title: str = "Atomic Fictional Petals",
) -> Store:
    open_database(path).close()
    store = Store(path)
    store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id=arxiv_id,
            title=title,
            authors=("Mira Example",),
            abstract="A synthetic PDF-download record.",
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
    return store


def profile_repository(root: Path, destination: Path) -> ProfileRepository:
    repository = ProfileRepository(
        root / "profile.json",
        root / "profile.lock",
    )
    repository.save_atomic(
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
    return repository


@pytest.mark.parametrize(
    ("arxiv_id", "version"),
    [
        ("2608.32001", 2),
        ("astro-ph/9912345", 1),
    ],
)
def test_safe_filename_contains_a_reversible_modern_or_legacy_id(
    arxiv_id: str,
    version: int,
) -> None:
    filename = safe_pdf_filename(
        arxiv_id,
        version,
        "A Synthetic / Title: with * reserved? characters",
    )

    encoded_id, separator, remainder = filename.partition(f"v{version} - ")
    assert separator
    assert unquote(encoded_id) == arxiv_id
    assert remainder.endswith(".pdf")
    assert not any(character in filename for character in '/\\:*?"<>|')


def test_dot_only_title_uses_a_visible_fallback() -> None:
    filename = safe_pdf_filename("2608.32002", 1, " . .. ... ")

    assert filename == "2608.32002v1 - paper.pdf"


def test_unicode_title_is_preserved_within_a_bounded_filename() -> None:
    filename = safe_pdf_filename(
        "2608.32003",
        3,
        "Ｆictional 測試 🌌 " + "長い題名" * 100,
    )

    assert filename.startswith("2608.32003v3 - Fictional 測試 🌌")
    assert filename.endswith(".pdf")
    assert len(filename.encode("utf-8")) <= 180


def test_identical_titles_for_different_papers_do_not_collide() -> None:
    first = safe_pdf_filename("2608.32004", 1, "Shared Synthetic Title")
    second = safe_pdf_filename("2608.32005", 1, "Shared Synthetic Title")

    assert first != second
    assert first.startswith("2608.32004v1 - ")
    assert second.startswith("2608.32005v1 - ")


@pytest.mark.parametrize(
    ("arxiv_id", "version", "expected"),
    [
        (
            "2608.32001",
            2,
            "https://arxiv.org/pdf/2608.32001v2",
        ),
        (
            "astro-ph/9912345",
            1,
            "https://arxiv.org/pdf/astro-ph/9912345v1",
        ),
    ],
)
def test_pdf_url_is_derived_on_the_allowlisted_arxiv_host(
    arxiv_id: str,
    version: int,
    expected: str,
) -> None:
    assert arxiv_pdf_url(arxiv_id, version) == expected


def test_download_uses_stored_paper_and_atomically_publishes_a_private_pdf(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    profiles = profile_repository(tmp_path, destination)
    client = PdfClient()
    manager = DownloadManager(
        store,
        profiles,
        client,
        max_pdf_bytes=1024,
        clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )

    result = manager.download("2608.32010", 1)

    assert result == DownloadResult(
        arxiv_id="2608.32010",
        version=1,
        filename="2608.32010v1 - Atomic Fictional Petals.pdf",
        byte_count=len(PDF_BYTES),
        sha256=sha256(PDF_BYTES).hexdigest(),
        reused_existing=False,
    )
    published = destination / result.filename
    assert published.read_bytes() == PDF_BYTES
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert tuple(path for path in destination.iterdir() if path != published) == ()
    assert client.calls == [
        (
            "https://arxiv.org/pdf/2608.32010v1",
            Interface.PDF,
            "application/pdf",
            1024,
        )
    ]


def test_download_cancelled_after_fetch_does_not_publish_or_record_pdf(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    profiles = profile_repository(tmp_path, destination)
    cancel_requested = threading.Event()

    class CancellingPdfClient:
        def get(
            self,
            url: str,
            *,
            interface: Interface,
            accept: str,
            max_bytes: int | None = None,
            cancelled=None,
        ) -> HttpResponse:
            assert cancelled is not None
            assert not cancelled()
            cancel_requested.set()
            return HttpResponse(
                status=200,
                final_url=url,
                headers={"content-type": "application/pdf"},
                body=PDF_BYTES,
                observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
            )

    manager = DownloadManager(
        store,
        profiles,
        CancellingPdfClient(),
        max_pdf_bytes=1024,
    )

    with pytest.raises(DownloadError, match="cancelled"):
        manager.download(
            "2608.32010",
            1,
            cancelled=cancel_requested.is_set,
        )

    assert tuple(destination.iterdir()) == ()
    assert store.download_file("2608.32010", 1) is None


def test_download_fsyncs_the_destination_directory_after_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    profiles = profile_repository(tmp_path, destination)
    real_fsync = downloads_module.os.fsync
    fsynced_directory: list[bool] = []

    def observe_fsync(descriptor: int) -> None:
        fsynced_directory.append(
            stat.S_ISDIR(downloads_module.os.fstat(descriptor).st_mode)
        )
        real_fsync(descriptor)

    monkeypatch.setattr(downloads_module.os, "fsync", observe_fsync)
    manager = DownloadManager(
        store,
        profiles,
        PdfClient(),
        max_pdf_bytes=1024,
    )

    manager.download("2608.32010", 1)

    assert fsynced_directory == [False, True]


def test_download_rejects_a_non_pdf_content_type_without_leaving_a_file(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(content_type="text/html"),
        max_pdf_bytes=1024,
    )

    with pytest.raises(DownloadError, match="content type"):
        manager.download("2608.32010", 1)

    assert tuple(destination.iterdir()) == ()
    assert store.download_file("2608.32010", 1) is None


def test_download_rejects_invalid_pdf_magic_without_leaving_a_file(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(body=b"<html>synthetic error</html>"),
        max_pdf_bytes=1024,
    )

    with pytest.raises(DownloadError, match="PDF signature"):
        manager.download("2608.32010", 1)

    assert tuple(destination.iterdir()) == ()
    assert store.download_file("2608.32010", 1) is None


def test_download_enforces_its_response_size_limit(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(body=b"%PDF-" + b"x" * 64),
        max_pdf_bytes=32,
    )

    with pytest.raises(DownloadError, match="size limit"):
        manager.download("2608.32010", 1)

    assert tuple(destination.iterdir()) == ()


def test_download_never_overwrites_a_different_existing_file(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    base_name = "2608.32010v1 - Atomic Fictional Petals.pdf"
    existing = destination / base_name
    different = b"%PDF-1.4\n% different local file\n"
    existing.write_bytes(different)
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(),
        max_pdf_bytes=1024,
    )

    result = manager.download("2608.32010", 1)

    assert result.filename == (
        "2608.32010v1 - Atomic Fictional Petals (2).pdf"
    )
    assert result.reused_existing is False
    assert existing.read_bytes() == different
    assert (destination / result.filename).read_bytes() == PDF_BYTES


def test_numbered_conflict_filename_remains_within_the_byte_limit(
    tmp_path: Path,
) -> None:
    title = "測試題名" * 100
    store = seeded_store(tmp_path / "state.sqlite3", title=title)
    destination = tmp_path / "PDFs"
    destination.mkdir()
    base_name = safe_pdf_filename("2608.32010", 1, title)
    (destination / base_name).write_bytes(b"different")
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(),
        max_pdf_bytes=1024,
    )

    result = manager.download("2608.32010", 1)

    assert result.filename.endswith(" (2).pdf")
    assert len(result.filename.encode("utf-8")) <= 180


def test_download_verifies_and_reuses_an_unrecorded_matching_file(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    existing = destination / (
        "2608.32010v1 - Atomic Fictional Petals.pdf"
    )
    existing.write_bytes(PDF_BYTES)
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(),
        max_pdf_bytes=1024,
    )

    result = manager.download("2608.32010", 1)

    assert result.reused_existing is True
    assert result.filename == existing.name
    assert tuple(destination.iterdir()) == (existing,)
    assert store.download_file("2608.32010", 1) is not None


def test_download_retries_when_another_writer_wins_the_publish_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    base_name = "2608.32010v1 - Atomic Fictional Petals.pdf"
    raced_file = destination / base_name
    raced_bytes = b"%PDF-1.4\n% concurrent differing file\n"
    real_link = downloads_module.os.link
    raced = False

    def race_then_link(source: Path, target: Path) -> None:
        nonlocal raced
        if not raced:
            raced = True
            Path(target).write_bytes(raced_bytes)
        real_link(source, target)

    monkeypatch.setattr(downloads_module.os, "link", race_then_link)
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(),
        max_pdf_bytes=1024,
    )

    result = manager.download("2608.32010", 1)

    assert raced is True
    assert result.filename == (
        "2608.32010v1 - Atomic Fictional Petals (2).pdf"
    )
    assert raced_file.read_bytes() == raced_bytes
    assert (destination / result.filename).read_bytes() == PDF_BYTES


def test_matching_recorded_file_is_recomputed_and_reused_without_network(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    profiles = profile_repository(tmp_path, destination)
    first = DownloadManager(
        store,
        profiles,
        PdfClient(),
        max_pdf_bytes=1024,
    ).download("2608.32010", 1)

    class OfflineClient:
        def get(self, *_: object, **__: object) -> HttpResponse:
            raise AssertionError("verified local file must avoid the network")

    second = DownloadManager(
        store,
        profiles,
        OfflineClient(),
        max_pdf_bytes=1024,
        clock=lambda: datetime(2026, 8, 23, tzinfo=timezone.utc),
    ).download("2608.32010", 1)

    assert second == DownloadResult(
        arxiv_id=first.arxiv_id,
        version=first.version,
        filename=first.filename,
        byte_count=first.byte_count,
        sha256=first.sha256,
        reused_existing=True,
    )
    assert tuple(destination.iterdir()) == (destination / first.filename,)


def test_local_presence_is_recomputed_after_destination_changes_and_restore(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    first_destination = tmp_path / "First PDFs"
    second_destination = tmp_path / "Second PDFs"
    first_destination.mkdir()
    second_destination.mkdir()
    profiles = profile_repository(tmp_path, first_destination)
    first = DownloadManager(
        store,
        profiles,
        PdfClient(),
        max_pdf_bytes=1024,
    ).download("2608.32010", 1)
    profiles.save_atomic(
        Profile(
            schema_version=2,
            revision=2,
            category_coverage=(
                ProfileCategory("cs.SE", date(2026, 8, 1)),
            ),
            keywords=(),
            phrases=(),
            authors=(),
            seed_papers=(),
            pdf_destination=PdfDestination("custom", second_destination),
        ),
        expected_revision=1,
    )

    second = DownloadManager(
        store,
        profiles,
        PdfClient(),
        max_pdf_bytes=1024,
    ).download("2608.32010", 1)

    assert second.reused_existing is False
    assert (second_destination / second.filename).read_bytes() == PDF_BYTES
    profiles.save_atomic(
        Profile(
            schema_version=2,
            revision=3,
            category_coverage=(
                ProfileCategory("cs.SE", date(2026, 8, 1)),
            ),
            keywords=(),
            phrases=(),
            authors=(),
            seed_papers=(),
            pdf_destination=PdfDestination("custom", first_destination),
        ),
        expected_revision=2,
    )

    class OfflineClient:
        def get(self, *_: object, **__: object) -> HttpResponse:
            raise AssertionError("restored local file must avoid the network")

    restored = DownloadManager(
        store,
        profiles,
        OfflineClient(),
        max_pdf_bytes=1024,
    ).download("2608.32010", 1)

    assert restored.reused_existing is True
    assert restored.filename == first.filename
    assert (first_destination / first.filename).read_bytes() == PDF_BYTES


def test_recompute_presence_clears_rows_for_an_empty_new_destination(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    first_destination = tmp_path / "First PDFs"
    empty_destination = tmp_path / "Empty PDFs"
    first_destination.mkdir()
    empty_destination.mkdir()
    profiles = profile_repository(tmp_path, first_destination)
    manager = DownloadManager(
        store,
        profiles,
        PdfClient(),
        max_pdf_bytes=1024,
    )
    first = manager.download("2608.32010", 1)
    assert store.download_file("2608.32010", 1) is not None

    manager.recompute_presence(empty_destination)

    assert store.download_file("2608.32010", 1) is None

    (empty_destination / first.filename).write_bytes(
        (first_destination / first.filename).read_bytes()
    )
    manager.recompute_presence(empty_destination)

    recomputed = store.download_file("2608.32010", 1)
    assert recomputed is not None
    assert recomputed.filename == first.filename


def test_interrupted_atomic_write_removes_its_temporary_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store,
        profile_repository(tmp_path, destination),
        PdfClient(),
        max_pdf_bytes=1024,
    )

    def interrupt(_: int) -> None:
        raise KeyboardInterrupt("synthetic interruption")

    monkeypatch.setattr(downloads_module.os, "fsync", interrupt)

    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        manager.download("2608.32010", 1)

    assert tuple(destination.iterdir()) == ()
    assert store.download_file("2608.32010", 1) is None
