from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from arxiv_digest.models import (
    CategoryConfig,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import PdfDestination, Profile, ProfileRepository
from arxiv_digest.setup import SetupService
from arxiv_digest.storage.store import Store
from tests.unit.test_backup import (
    NOW,
    archive_payloads,
    initialized_paths,
    rewrite_archive_member,
)


def empty_paths(tmp_path: Path):
    return resolve_paths(
        platform="linux",
        home=tmp_path,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "empty-isolated"),
        },
    )


def test_portable_profile_library_and_local_presence_round_trip(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    inspection = inspect_backup(archive)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    confirmed_destination = tmp_path / "target" / "Confirmed PDFs"
    confirmed_destination.mkdir()
    expected_pdf = confirmed_destination / (
        "2608.41001v1 - Portable Fictional Lattices.pdf"
    )
    expected_pdf.write_bytes(b"%PDF-1.7\nsynthetic private local file\n")
    unrelated_pdf = confirmed_destination / "unrelated-private-file.pdf"
    unrelated_pdf.write_bytes(b"%PDF-1.7\nmust not be scanned\n")
    (target.cache_dir / "irrelevant-cache.json").write_text(
        "cache must not affect restore", encoding="utf-8"
    )

    result = restore_backup(
        target,
        inspection,
        PdfDestination("custom", confirmed_destination),
        clock=lambda: NOW,
    )

    profile = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert profile is not None
    assert profile.categories == ("cs.SE",)
    assert profile.keywords == ("fictional keyword",)
    assert profile.seed_papers == ("2608.41001",)
    assert profile.pdf_destination == PdfDestination(
        "custom", confirmed_destination.resolve()
    )
    store = Store(target.database_path)
    page = store.search_library("Portable", limit=20, offset=0)
    assert [entry.metadata.arxiv_id for entry in page] == ["2608.41001"]
    local = store.download_file("2608.41001", 1)
    assert local is not None
    assert local.filename == expected_pdf.name
    import sqlite3

    connection = sqlite3.connect(target.database_path)
    assert connection.execute("SELECT count(*) FROM download_files").fetchone() == (
        1,
    )
    connection.close()
    assert unrelated_pdf.read_bytes() == b"%PDF-1.7\nmust not be scanned\n"
    assert result.pre_restore_path is None
    assert result.profile_revision == 1


def test_generation_two_ledger_and_download_record_round_trip(
    tmp_path: Path,
) -> None:
    import sqlite3

    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup
    from arxiv_digest.profile import ProfileCategory

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    destination = tmp_path / "target" / "Confirmed PDFs"
    destination.mkdir()
    filename = "2608.41001v1 - Portable Fictional Lattices.pdf"
    pdf_payload = b"%PDF-1.7\nsynthetic private local file\n"
    (destination / filename).write_bytes(pdf_payload)

    restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", destination),
        clock=lambda: NOW,
    )

    profile = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert profile is not None
    assert profile.category_coverage == (
        ProfileCategory("cs.SE", date(2026, 8, 1)),
    )
    connection = sqlite3.connect(target.database_path)
    try:
        assert connection.execute(
            "SELECT generation FROM application_generation WHERE singleton = 1"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
        assert connection.execute(
            """SELECT observation_id, source, category, daily_list_date,
                      announced_version FROM source_observations
               ORDER BY observation_id"""
        ).fetchall() == [
            (51, "catchup", "cs.SE", "2026-08-20", None),
            (52, "atom", "cs.SE", None, 1),
        ]
        assert connection.execute(
            """SELECT category, daily_list_date, status
               FROM catchup_days"""
        ).fetchall() == [("cs.SE", "2026-08-20", "complete")]
        assert connection.execute(
            """SELECT event_id, arxiv_id, daily_list_date, announced_version,
                      version_resolution, queue_revision
               FROM canonical_events"""
        ).fetchall() == [
            (41, "2608.41001", "2026-08-20", 1, "atom_confirmed", 7)
        ]
        assert connection.execute(
            """SELECT event_id, observation_id
               FROM canonical_event_observations ORDER BY observation_id"""
        ).fetchall() == [(41, 51), (41, 52)]
        assert connection.execute(
            """SELECT daily_list_date, anchor_event_id, profile_revision
               FROM review_date_state"""
        ).fetchall() == [("2026-08-20", 41, 1)]
        assert connection.execute(
            "SELECT arxiv_id, saved_version FROM saved_papers"
        ).fetchall() == [("2608.41001", 1)]
        assert connection.execute(
            """SELECT arxiv_id, version, filename, byte_count, sha256
               FROM download_files"""
        ).fetchall() == [
            (
                "2608.41001",
                1,
                filename,
                len(pdf_payload),
                sha256(pdf_payload).hexdigest(),
            )
        ]
    finally:
        connection.close()


def test_restore_rejects_v1_before_modifying_current_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    current_archive = tmp_path / "current.zip"
    legacy_archive = tmp_path / "legacy-v1.zip"
    export_backup(source, current_archive, clock=lambda: NOW)
    manifest = json.loads(archive_payloads(current_archive)["manifest.json"])
    manifest["format_version"] = 1
    rewrite_archive_member(
        current_archive,
        legacy_archive,
        "manifest.json",
        (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )
    current_inspection = inspect_backup(current_archive)
    legacy_inspection = replace(
        current_inspection,
        path=legacy_archive,
        archive_sha256=sha256(legacy_archive.read_bytes()).hexdigest(),
    )
    target = initialized_paths(tmp_path / "target")
    destination = target.database_path
    before = {
        "archive": sha256(legacy_archive.read_bytes()).digest(),
        "database": sha256(target.database_path.read_bytes()).digest(),
        "profile": sha256(target.profile_path.read_bytes()).digest(),
    }

    with pytest.raises(BackupError) as raised:
        restore_backup(
            target,
            legacy_inspection,
            PdfDestination("custom", destination),
            clock=lambda: NOW,
        )

    assert raised.value.code == "unsupported_schema"
    assert sha256(legacy_archive.read_bytes()).digest() == before["archive"]
    assert sha256(target.database_path.read_bytes()).digest() == before["database"]
    assert sha256(target.profile_path.read_bytes()).digest() == before["profile"]
    assert not tuple(target.backup_dir.iterdir())


def test_restore_rejects_an_atom_only_canonical_event_before_modifying_current_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    valid_archive = tmp_path / "valid.zip"
    hostile_archive = tmp_path / "atom-only-event.zip"
    export_backup(source, valid_archive, clock=lambda: NOW)
    records = [
        json.loads(line)
        for line in archive_payloads(valid_archive)["state.jsonl"].splitlines()
    ]
    records = [
        record
        for record in records
        if not (
            record["record_type"] == "canonical_event_observation"
            and record["payload"]["observation_id"] == 51
        )
    ]
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(
        valid_archive,
        hostile_archive,
        "state.jsonl",
        state_payload,
    )
    accepted_before_revalidation = replace(
        inspect_backup(valid_archive),
        path=hostile_archive,
        archive_sha256=sha256(hostile_archive.read_bytes()).hexdigest(),
    )
    target = initialized_paths(tmp_path / "target")
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()
    before = {
        "database": sha256(target.database_path.read_bytes()).digest(),
        "profile": sha256(target.profile_path.read_bytes()).digest(),
    }

    with pytest.raises(BackupError) as raised:
        restore_backup(
            target,
            accepted_before_revalidation,
            PdfDestination("custom", confirmed),
            clock=lambda: NOW,
        )

    assert raised.value.code == "cross_record_invalid"
    assert sha256(target.database_path.read_bytes()).digest() == before["database"]
    assert sha256(target.profile_path.read_bytes()).digest() == before["profile"]
    assert not tuple(target.backup_dir.iterdir())


def test_restore_rejects_a_wrong_date_catchup_link_before_modifying_current_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    valid_archive = tmp_path / "valid.zip"
    hostile_archive = tmp_path / "wrong-date-catchup.zip"
    export_backup(source, valid_archive, clock=lambda: NOW)
    records = [
        json.loads(line)
        for line in archive_payloads(valid_archive)["state.jsonl"].splitlines()
    ]
    catchup = next(
        record
        for record in records
        if record["record_type"] == "source_observation"
        and record["payload"]["observation_id"] == 51
    )
    catchup["payload"]["daily_list_date"] = "2026-08-19"
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(
        valid_archive,
        hostile_archive,
        "state.jsonl",
        state_payload,
    )
    accepted_before_revalidation = replace(
        inspect_backup(valid_archive),
        path=hostile_archive,
        archive_sha256=sha256(hostile_archive.read_bytes()).hexdigest(),
    )
    target = initialized_paths(tmp_path / "target")
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()
    before = {
        "database": sha256(target.database_path.read_bytes()).digest(),
        "profile": sha256(target.profile_path.read_bytes()).digest(),
    }

    with pytest.raises(BackupError) as raised:
        restore_backup(
            target,
            accepted_before_revalidation,
            PdfDestination("custom", confirmed),
            clock=lambda: NOW,
        )

    assert raised.value.code == "cross_record_invalid"
    assert sha256(target.database_path.read_bytes()).digest() == before["database"]
    assert sha256(target.profile_path.read_bytes()).digest() == before["profile"]
    assert not tuple(target.backup_dir.iterdir())


def test_restore_skips_mismatched_download_and_does_not_scan_unrelated_pdfs(
    tmp_path: Path,
) -> None:
    import sqlite3

    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    destination = tmp_path / "target" / "Confirmed PDFs"
    destination.mkdir()
    expected_name = "2608.41001v1 - Portable Fictional Lattices.pdf"
    mismatched = b"%PDF-1.7\ndifferent local bytes\n"
    unrelated = b"%PDF-1.7\nsynthetic private local file\n"
    (destination / expected_name).write_bytes(mismatched)
    unrelated_path = destination / "unrelated.pdf"
    unrelated_path.write_bytes(unrelated)

    restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", destination),
        clock=lambda: NOW,
    )

    connection = sqlite3.connect(target.database_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM download_files"
        ).fetchone() == (0,)
    finally:
        connection.close()
    assert (destination / expected_name).read_bytes() == mismatched
    assert unrelated_path.read_bytes() == unrelated


def test_nonempty_restore_creates_and_verifies_a_private_recovery_backup(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = initialized_paths(tmp_path / "target")
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()

    result = restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", confirmed),
        clock=lambda: NOW,
        random_bytes=lambda size: b"\x12" * size,
    )

    assert result.pre_restore_path is not None
    assert result.pre_restore_path.parent == target.backup_dir
    assert result.pre_restore_path.name == (
        "pre-restore-20260822T120000Z-1212121212121212"
        ".arxiv-digest-backup.zip"
    )
    assert result.pre_restore_path.stat().st_mode & 0o777 == 0o600
    recovery = inspect_backup(result.pre_restore_path)
    assert recovery.profile.revision == 1
    restored = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert restored is not None
    assert restored.revision == 2
    assert result.profile_revision == 2


@pytest.mark.parametrize(
    "failure_phase",
    ("journal_fsynced", "database_published", "profile_published"),
)
def test_failed_publication_rolls_back_to_usable_old_profile_and_database(
    tmp_path: Path,
    failure_phase: str,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = initialized_paths(tmp_path / "target")
    old_store = Store(target.database_path)
    old_store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id="2608.41999",
            title="Old State Survivor",
            authors=("Robin Example",),
            abstract="Synthetic old state that must survive rollback.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        (PaperVersion(1, datetime(2026, 8, 2, tzinfo=timezone.utc)),),
    )
    old_store.save_paper("2608.41999", 1)
    old_profile = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()

    def fail_publication(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError("synthetic publication failure")

    with pytest.raises(BackupError) as raised:
        restore_backup(
            target,
            inspect_backup(archive),
            PdfDestination("custom", confirmed),
            clock=lambda: NOW,
            random_bytes=lambda size: b"\x34" * size,
            crash_injector=fail_publication,
        )

    assert raised.value.code == "restore_failed"
    assert ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load() == old_profile
    restored_old = Store(target.database_path).search_library(
        "Survivor", limit=20, offset=0
    )
    assert [entry.metadata.arxiv_id for entry in restored_old] == [
        "2608.41999"
    ]
    assert not target.restore_journal_path.exists()


@pytest.mark.parametrize(
    ("crash_phase", "completes_new_state"),
    [
        ("journal_fsynced", False),
        ("database_published", True),
        ("profile_published", True),
    ],
)
def test_startup_recovery_resolves_a_hash_verified_interrupted_restore(
    tmp_path: Path,
    crash_phase: str,
    completes_new_state: bool,
) -> None:
    from arxiv_digest.backup import (
        export_backup,
        inspect_backup,
        recover_restore,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = initialized_paths(tmp_path / "target")
    confirmed = tmp_path / "target" / "Recovered PDFs"
    confirmed.mkdir()

    class SimulatedCrash(BaseException):
        pass

    old_profile = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert old_profile is not None

    def crash_publication(phase: str) -> None:
        if phase == crash_phase:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        restore_backup(
            target,
            inspect_backup(archive),
            PdfDestination("custom", confirmed),
            clock=lambda: NOW,
            random_bytes=lambda size: b"\x56" * size,
            crash_injector=crash_publication,
        )

    assert target.restore_journal_path.is_file()

    recover_restore(target)

    recovered = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert recovered is not None
    if completes_new_state:
        assert recovered.revision == 2
        assert recovered.pdf_destination.path == confirmed.resolve()
    else:
        assert recovered == old_profile
    assert not target.restore_journal_path.exists()
    assert not tuple(target.config_dir.glob("*.restore-*"))
    assert not tuple(target.data_dir.glob("*.restore-*"))


def test_restore_reconstructs_queue_revision_and_finished_state(
    tmp_path: Path,
) -> None:
    import sqlite3

    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup

    source = initialized_paths(tmp_path / "source")
    connection = sqlite3.connect(source.database_path)
    connection.execute(
        """UPDATE review_date_state
           SET last_finished_at = ?, last_finished_revision = 100""",
        ("2026-08-21T12:00:00Z",),
    )
    connection.execute(
        "UPDATE state_meta SET queue_revision = 100 WHERE singleton = 1"
    )
    connection.commit()
    connection.close()
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()
    restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", confirmed),
        clock=lambda: NOW,
    )
    restored = sqlite3.connect(target.database_path)
    try:
        assert restored.execute(
            "SELECT queue_revision FROM state_meta WHERE singleton = 1"
        ).fetchone() == (100,)
        assert restored.execute(
            """SELECT last_finished_at, last_finished_revision
               FROM review_date_state WHERE daily_list_date = ?""",
            ("2026-08-20",),
        ).fetchone() == ("2026-08-21T12:00:00Z", 100)
    finally:
        restored.close()


def test_restore_refuses_active_work_then_explicitly_cancels_and_waits(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup
    from arxiv_digest.maintenance import MaintenanceBarrier, WorkActiveError

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    inspection = inspect_backup(archive)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()
    barrier = MaintenanceBarrier()
    worker_started = threading.Event()
    cancel_requested = threading.Event()
    worker_stopped = threading.Event()

    def worker() -> None:
        with barrier.worker("sync-restore-fixture", cancel_requested.set):
            worker_started.set()
            cancel_requested.wait(2)
        worker_stopped.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert worker_started.wait(2)

    with pytest.raises(WorkActiveError):
        restore_backup(
            target,
            inspection,
            PdfDestination("custom", confirmed),
            maintenance=barrier,
            clock=lambda: NOW,
        )
    assert not cancel_requested.is_set()
    assert not target.profile_path.exists()
    assert not target.database_path.exists()

    result = restore_backup(
        target,
        inspection,
        PdfDestination("custom", confirmed),
        maintenance=barrier,
        clock=lambda: NOW,
        cancel_active=True,
        timeout=2,
    )

    thread.join(2)
    assert cancel_requested.is_set()
    assert worker_stopped.is_set()
    assert not thread.is_alive()
    assert result.profile_revision == 1


def test_export_racing_profile_publication_captures_one_logical_snapshot(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup, inspect_backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    barrier = MaintenanceBarrier()
    repository = ProfileRepository(
        paths.profile_path,
        paths.profile_lock_path,
        maintenance=barrier,
    )
    current = repository.load()
    assert current is not None
    revised = Profile(
        schema_version=2,
        revision=2,
        category_coverage=current.category_coverage,
        keywords=("new atomic preference",),
        phrases=current.phrases,
        authors=current.authors,
        seed_papers=current.seed_papers,
        pdf_destination=current.pdf_destination,
    )
    sqlite_committed = threading.Event()
    allow_publication = threading.Event()
    publication_done = threading.Event()

    def pause_publication(phase: str) -> None:
        if phase == "sqlite_pending_committed":
            sqlite_committed.set()
            allow_publication.wait(2)

    service = SetupService(
        paths.database_path,
        repository,
        maintenance=barrier,
        crash_injector=pause_publication,
    )

    def publish() -> None:
        service.publish_profile(
            revised,
            (CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1)),),
            expected_revision=1,
        )
        publication_done.set()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert sqlite_committed.wait(2)
    archive = tmp_path / "racing-export.zip"
    export_done = threading.Event()

    def export() -> None:
        export_backup(paths, archive, maintenance=barrier, clock=lambda: NOW)
        export_done.set()

    exporter = threading.Thread(target=export)
    exporter.start()
    assert not export_done.wait(0.05)
    allow_publication.set()
    for thread in (publisher, exporter):
        thread.join(2)
        assert not thread.is_alive()

    inspection = inspect_backup(archive)
    assert publication_done.is_set()
    assert inspection.profile.revision == 2
    assert inspection.profile.keywords == ("new atomic preference",)


def test_recovery_rejects_a_journal_filename_outside_its_restore_namespace(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        recover_restore,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = initialized_paths(tmp_path / "target")
    old_profile = target.profile_path.read_bytes()
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()

    class SimulatedCrash(BaseException):
        pass

    def crash_after_journal(phase: str) -> None:
        if phase == "journal_fsynced":
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        restore_backup(
            target,
            inspect_backup(archive),
            PdfDestination("custom", confirmed),
            clock=lambda: NOW,
            random_bytes=lambda size: b"\x78" * size,
            crash_injector=crash_after_journal,
        )
    journal = json.loads(target.restore_journal_path.read_bytes())
    journal["staged_profile"] = target.profile_path.name
    target.restore_journal_path.write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BackupError) as raised:
        recover_restore(target)

    assert raised.value.code == "restore_journal_invalid"
    assert target.profile_path.read_bytes() == old_profile
    assert target.restore_journal_path.exists()


def test_restore_preserves_inactive_category_sync_history(
    tmp_path: Path,
) -> None:
    import sqlite3

    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup

    source = initialized_paths(tmp_path / "source")
    connection = sqlite3.connect(source.database_path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               status
           ) VALUES (?, ?, ?, ?, 'idle')""",
        ("math.LO", "math:LO", "2026-07-01", "2026-08-20"),
    )
    connection.commit()
    connection.close()
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = empty_paths(tmp_path / "target")
    target.ensure()
    confirmed = tmp_path / "target" / "Confirmed PDFs"
    confirmed.mkdir()

    restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", confirmed),
        clock=lambda: NOW,
    )

    restored = sqlite3.connect(target.database_path)
    categories = restored.execute(
        "SELECT category, status FROM category_sync_state ORDER BY category"
    ).fetchall()
    restored.close()
    assert categories == [("cs.SE", "idle"), ("math.LO", "idle")]


def test_restore_supports_valid_local_paths_with_uri_delimiters(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup

    source = initialized_paths(tmp_path / "source?portable")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    target = empty_paths(tmp_path / "target?restore")
    target.ensure()
    confirmed = tmp_path / "target?restore" / "Confirmed PDFs"
    confirmed.mkdir()

    restore_backup(
        target,
        inspect_backup(archive),
        PdfDestination("custom", confirmed),
        clock=lambda: NOW,
    )

    assert ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load() is not None
