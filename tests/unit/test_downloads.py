from __future__ import annotations

import os
import stat
import threading
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers import assert_private_file

import arxiv_digest.downloads as downloads_module
from arxiv_digest.downloads import (
    DownloadError,
    DownloadManager,
    DownloadResult,
    arxiv_pdf_url,
    safe_pdf_filename,
)


@pytest.mark.parametrize("title", ["CON", "nul", "LPT1", "COM9", "AUX.notes", "COM¹"])
def test_new_pdf_names_avoid_windows_reserved_device_names(title):
    filename = safe_pdf_filename("2608.32002", 1, title)
    assert filename.startswith("paper ")
    assert filename.endswith(".pdf")
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
    authors: tuple[str, ...] = ("Mira Example",),
    versions: tuple[int, ...] = (1,),
) -> Store:
    open_database(path).close()
    store = Store(path)
    store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id=arxiv_id,
            title=title,
            authors=authors,
            abstract="A synthetic PDF-download record.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        tuple(
            PaperVersion(
                version,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            for version in versions
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
    ("authors", "expected"),
    [
        (("Mira Example",), "Example"),
        (("Mira Example", "Rowan Sample"), "Example Sample"),
        (("Mira de la Example", "Rowan van der Sample"), "de la Example van der Sample"),
        (("Example, Mira", "Rowan Sample Jr.", "Taylor Fiction, III"), "Example Sample Fiction"),
        (("M. Example-Sample", "Rowan O'Fiction", "測試"), "Example-Sample O'Fiction 測試"),
    ],
)
def test_safe_filename_uses_all_author_surnames_then_title(
    authors: tuple[str, ...],
    expected: str,
) -> None:
    filename = safe_pdf_filename(
        "2608.32001",
        2,
        "A Synthetic / Title: with * reserved? characters",
        authors,
    )

    assert filename == f"{expected} A Synthetic Title with reserved characters.pdf"
    assert not any(character in filename for character in '/\\:*?"<>|')


def test_dot_only_title_uses_a_visible_fallback() -> None:
    filename = safe_pdf_filename("2608.32002", 1, " . .. ... ")

    assert filename == "paper.pdf"


def test_unicode_title_is_preserved_within_a_bounded_filename() -> None:
    filename = safe_pdf_filename(
        "2608.32003",
        3,
        "Ｆictional 測試 🌌 " + "長い題名" * 100,
        ("Mira Ｅxample",),
    )

    assert filename.startswith("Example Fictional 測試 🌌")
    assert filename.endswith(".pdf")
    assert len(filename.encode("utf-8")) <= 180


def test_filename_without_authors_uses_the_title_for_legacy_ids() -> None:
    assert safe_pdf_filename("astro-ph/9912345", 1, "Synthetic Title") == (
        "Synthetic Title.pdf"
    )


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
        filename="Example Atomic Fictional Petals.pdf",
        byte_count=len(PDF_BYTES),
        sha256=sha256(PDF_BYTES).hexdigest(),
        reused_existing=False,
    )
    published = destination / result.filename
    assert published.read_bytes() == PDF_BYTES
    assert_private_file(published)
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

    assert fsynced_directory == ([False] if os.name == "nt" else [False, True])


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
    base_name = "Example Atomic Fictional Petals.pdf"
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
        "Example Atomic Fictional Petals (2).pdf"
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
    base_name = safe_pdf_filename("2608.32010", 1, title, ("Mira Example",))
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


@pytest.mark.parametrize("same_paper", [False, True])
def test_identical_names_and_bytes_keep_separate_paper_and_version_records(
    tmp_path: Path,
    same_paper: bool,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = seeded_store(database, versions=(1, 2))
    other_id = "2608.32010" if same_paper else "2608.32011"
    other_version = 2 if same_paper else 1
    if not same_paper:
        seeded_store(database, arxiv_id=other_id)
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )

    first = manager.download("2608.32010", 1)
    second = manager.download(other_id, other_version)

    assert first.filename == "Example Atomic Fictional Petals.pdf"
    assert second.filename == "Example Atomic Fictional Petals (2).pdf"
    assert second.reused_existing is False
    assert store.download_file("2608.32010", 1).filename == first.filename
    assert store.download_file(other_id, other_version).filename == second.filename
    manager.recompute_presence(destination)
    assert store.download_file("2608.32010", 1).filename == first.filename
    assert store.download_file(other_id, other_version).filename == second.filename


def test_missing_file_does_not_reassign_another_papers_recorded_filename(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = seeded_store(database)
    seeded_store(database, arxiv_id="2608.32011")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )
    first = manager.download("2608.32010", 1)
    (destination / first.filename).unlink()

    second = manager.download("2608.32011", 1)

    assert second.filename == "Example Atomic Fictional Petals (2).pdf"
    assert store.download_file("2608.32010", 1).filename == first.filename


def test_unrecorded_author_title_file_is_not_claimed_even_if_bytes_match(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    existing = destination / "Example Atomic Fictional Petals.pdf"
    existing.write_bytes(PDF_BYTES)
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )

    result = manager.download("2608.32010", 1)

    assert result.filename == "Example Atomic Fictional Petals (2).pdf"
    assert result.reused_existing is False
    assert existing.read_bytes() == PDF_BYTES


def test_another_paper_can_win_the_same_name_publish_race_with_identical_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = seeded_store(database)
    seeded_store(database, arxiv_id="2608.32011")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )
    real_link = downloads_module.os.link
    raced = False

    def publish_other_paper_then_link(source: Path, target: Path) -> None:
        nonlocal raced
        if not raced:
            raced = True
            manager.download("2608.32011", 1)
        real_link(source, target)

    monkeypatch.setattr(downloads_module.os, "link", publish_other_paper_then_link)

    result = manager.download("2608.32010", 1)

    assert result.filename == "Example Atomic Fictional Petals (2).pdf"
    assert store.download_file("2608.32011", 1).filename == (
        "Example Atomic Fictional Petals.pdf"
    )
    assert len(tuple(destination.iterdir())) == 2


@pytest.mark.parametrize(
    "filename",
    [
        "2608.32010v1 - Atomic Fictional Petals.pdf",
        "2608.32010v1 - Atomic Fictional Petals (2).pdf",
        "Example Atomic Fictional Petals (2).pdf",
    ],
)
def test_recompute_and_download_keep_verified_recorded_names(
    tmp_path: Path,
    filename: str,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    existing = destination / filename
    existing.write_bytes(PDF_BYTES)
    store.record_download_file(
        DownloadFileRecord(
            arxiv_id="2608.32010", version=1, filename=filename,
            byte_count=len(PDF_BYTES), sha256=sha256(PDF_BYTES).hexdigest(),
            last_verified_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
    )

    class OfflineClient:
        def get(self, *_: object, **__: object) -> HttpResponse:
            raise AssertionError("verified local file must avoid the network")

    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), OfflineClient(),
    )
    manager.recompute_presence(destination)
    result = manager.download("2608.32010", 1)

    assert result.filename == filename
    assert result.reused_existing is True
    assert tuple(destination.iterdir()) == (existing,)


def test_recompute_rejects_recorded_pdf_with_changed_bytes(tmp_path: Path) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )
    result = manager.download("2608.32010", 1)
    (destination / result.filename).write_bytes(b"%PDF-1.7\n% changed fixture\n")

    manager.recompute_presence(destination)

    assert store.download_file("2608.32010", 1) is None


def test_recompute_recovers_unrecorded_legacy_id_version_filename(
    tmp_path: Path,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    filename = "2608.32010v1 - Atomic Fictional Petals.pdf"
    (destination / filename).write_bytes(PDF_BYTES)
    manager = DownloadManager(
        store, profile_repository(tmp_path, destination), PdfClient(),
    )

    manager.recompute_presence(destination)

    assert store.download_file("2608.32010", 1).filename == filename


def test_download_retries_when_another_writer_wins_the_publish_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seeded_store(tmp_path / "state.sqlite3")
    destination = tmp_path / "PDFs"
    destination.mkdir()
    base_name = "Example Atomic Fictional Petals.pdf"
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
        "Example Atomic Fictional Petals (2).pdf"
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


def test_recompute_presence_retains_mapping_for_an_empty_new_destination(
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


@pytest.mark.parametrize("recompute_before_download", [False, True])
def test_folder_switch_and_restart_preserve_offline_pdf_identity(
    tmp_path: Path, recompute_before_download: bool,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = seeded_store(database)
    original_destination = tmp_path / "Original PDFs"
    empty_destination = tmp_path / "Empty PDFs"
    original_destination.mkdir()
    empty_destination.mkdir()
    profiles = profile_repository(tmp_path, original_destination)
    manager = DownloadManager(store, profiles, PdfClient())
    first = manager.download("2608.32010", 1, save_first=True)
    profiles.save_atomic(
        replace(
            profiles.load(), revision=2,
            pdf_destination=PdfDestination("custom", empty_destination),
        ),
        expected_revision=1,
    )
    manager.recompute_presence(empty_destination)
    assert store.download_file("2608.32010", 1) is None
    assert store.search_library("", limit=20, offset=0)[0].local_pdf_versions == ()

    class OfflineClient:
        def get(self, *_: object, **__: object) -> HttpResponse:
            raise AssertionError("returning to a verified PDF must avoid network")

    open_database(database).close()
    reopened = Store(database)
    profiles.save_atomic(
        replace(
            profiles.load(), revision=3,
            pdf_destination=PdfDestination("custom", original_destination),
        ),
        expected_revision=2,
    )
    restored = DownloadManager(reopened, profiles, OfflineClient())
    assert reopened.download_files()[0].filename == first.filename
    if recompute_before_download:
        restored.recompute_presence(original_destination)
    result = restored.download("2608.32010", 1)
    assert result.reused_existing is True
    assert result.filename == first.filename
    assert reopened.search_library("", limit=20, offset=0)[0].local_pdf_versions == (1,)


@pytest.mark.parametrize(
    ("first_author", "second_author"),
    [("Mira Example", "Rowan example"), ("Mira Éxample", "Rowan E\u0301xample")],
)
def test_absent_filename_mapping_reserves_case_and_unicode_equivalent_names(
    tmp_path: Path,
    first_author: str,
    second_author: str,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = seeded_store(database, authors=(first_author,))
    seeded_store(database, arxiv_id="2608.32011", authors=(second_author,))
    destination = tmp_path / "PDFs"
    destination.mkdir()
    manager = DownloadManager(store, profile_repository(tmp_path, destination), PdfClient())
    first = manager.download("2608.32010", 1)
    (destination / first.filename).unlink()
    # Imported metadata and older files may use decomposed Unicode even though
    # newly generated names are normalized.
    recorded = store.download_file("2608.32010", 1)
    store.record_download_file(
        replace(recorded, filename=unicodedata.normalize("NFD", recorded.filename))
    )
    manager.recompute_presence(destination)

    second = manager.download("2608.32011", 1)

    assert second.filename.endswith(" (2).pdf")
    assert len(store.download_files()) == 2


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
