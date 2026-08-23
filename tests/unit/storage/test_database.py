import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from arxiv_digest.storage.database import CorruptDatabaseError, open_database


def _current_migration_versions() -> tuple[int, ...]:
    migrations = files("arxiv_digest.storage.migrations")
    return tuple(
        sorted(
            int(resource.name[:4])
            for resource in migrations.iterdir()
            if resource.name[:4].isdigit() and resource.name.endswith(".sql")
        )
    )


def _insert_article(connection: sqlite3.Connection, arxiv_id: str) -> None:
    connection.execute(
        """INSERT INTO articles(
               arxiv_id, title, abstract, metadata_hash
           ) VALUES (?, ?, ?, ?)""",
        (arxiv_id, "Synthetic title", "Synthetic abstract", "0" * 64),
    )


def _create_version_one_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        files("arxiv_digest.storage.migrations")
        .joinpath("0001_initial.sql")
        .read_text(encoding="utf-8")
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (1, "2026-08-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()


def test_open_database_creates_required_tables(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    connection.close()
    assert {
        "schema_migrations",
        "state_meta",
        "articles",
        "oai_tombstones",
        "article_versions",
        "article_authors",
        "article_categories",
        "category_sync_state",
        "sync_runs",
        "review_events",
        "event_evidence",
        "review_date_state",
        "enrichment_days",
        "saved_papers",
        "papers_fts",
        "category_article_state",
    } <= names


def test_new_database_applies_download_state_migration(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3")

    versions = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("download_files",),
    ).fetchone()
    connection.close()

    assert versions == [(version,) for version in _current_migration_versions()]
    assert table == ("download_files",)


def test_existing_version_one_database_is_upgraded_transactionally(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    _create_version_one_database(path)

    connection = open_database(path)

    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [
        (version,) for version in _current_migration_versions()
    ]
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("download_files",),
    ).fetchone() == ("download_files",)
    connection.close()


def test_failed_upgrade_rolls_back_to_an_untouched_version_one_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    _create_version_one_database(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TRIGGER reject_version_two
        BEFORE INSERT ON schema_migrations
        WHEN new.version = 2
        BEGIN
            SELECT RAISE(ABORT, 'synthetic migration interruption');
        END;
        """
    )
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)

    unchanged = sqlite3.connect(path)
    assert unchanged.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,)]
    assert unchanged.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("download_files",),
    ).fetchone() is None
    unchanged.close()


def test_corrupt_database_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    original = b"not sqlite"
    path.write_bytes(original)
    with pytest.raises(CorruptDatabaseError):
        open_database(path)
    assert path.read_bytes() == original


def test_new_database_is_single_file_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = open_database(path)
    assert connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall() == [
        (version,) for version in _current_migration_versions()
    ]
    connection.close()

    reopened = sqlite3.connect(path)
    try:
        assert reopened.execute("SELECT count(*) FROM articles").fetchone() == (0,)
    finally:
        reopened.close()

    names = {item.name for item in tmp_path.iterdir()}
    assert names == {"state.sqlite3"}


@pytest.mark.parametrize(
    "statement, parameters",
    [
        (
            "INSERT INTO saved_papers(arxiv_id, saved_version) VALUES (?, ?)",
            ("2608.00001", 0),
        ),
        (
            """INSERT INTO review_events(
                   arxiv_id, announced_version, effective_date,
                   date_basis, confidence, queue_revision
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "2608.00001",
                0,
                "2026-08-01",
                "feed_mailing",
                "current",
                1,
            ),
        ),
    ],
)
def test_version_zero_is_rejected(
    tmp_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    _insert_article(connection, "2608.00001")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement, parameters)
    connection.close()


def test_saved_version_must_reference_an_actual_article_version(
    tmp_path: Path,
) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    _insert_article(connection, "2608.00002")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO saved_papers(arxiv_id, saved_version) VALUES (?, ?)",
            ("2608.00002", 1),
        )
    connection.close()


def test_review_version_must_reference_the_same_papers_actual_version(
    tmp_path: Path,
) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    for arxiv_id in ("2608.00003", "2608.00004"):
        _insert_article(connection, arxiv_id)
    connection.execute(
        """INSERT INTO article_versions(arxiv_id, version, submitted_at)
           VALUES (?, ?, ?)""",
        ("2608.00003", 1, "2026-08-01T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO review_events(
                   arxiv_id, announced_version, effective_date,
                   date_basis, confidence, queue_revision
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "2608.00004",
                1,
                "2026-08-01",
                "feed_mailing",
                "current",
                1,
            ),
        )
    connection.close()


@pytest.mark.parametrize(
    "run_kind, requested_until",
    [
        ("incremental", "2026-08-01"),
        ("coverage_backfill", None),
        ("unknown", None),
    ],
)
def test_sync_run_kind_and_until_combinations_are_enforced(
    tmp_path: Path, run_kind: str, requested_until: str | None
) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    connection.execute(
        """INSERT INTO category_sync_state(category, set_spec, coverage_start)
           VALUES (?, ?, ?)""",
        ("cs.LO", "cs:LO", "2026-07-01"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO sync_runs(
                   category, run_kind, requested_from, requested_until,
                   started_at, status
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "cs.LO",
                run_kind,
                "2026-07-01",
                requested_until,
                "2026-08-01T00:00:00Z",
                "running",
            ),
        )
    connection.close()


def test_startup_interrupts_stale_runs_without_advancing_checkpoints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    connection = open_database(path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               status, last_attempt_at
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "cs.LO",
            "cs:LO",
            "2026-07-01",
            "2026-08-01",
            "syncing",
            "2026-08-02T00:00:00Z",
        ),
    )
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               pending_backfill_start, pending_backfill_until, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "math.LO",
            "math:LO",
            "2026-07-01",
            "2026-08-01",
            "2026-06-01",
            "2026-06-30",
            "idle",
        ),
    )
    connection.execute(
        """INSERT INTO sync_runs(
               category, run_kind, requested_from, started_at, status
           ) VALUES (?, 'incremental', ?, ?, 'running')""",
        ("cs.LO", "2026-08-01", "2026-08-02T00:00:00Z"),
    )
    connection.execute(
        """INSERT INTO sync_runs(
               category, run_kind, requested_from, requested_until,
               started_at, status
           ) VALUES (?, 'coverage_backfill', ?, ?, ?, 'running')""",
        (
            "math.LO",
            "2026-06-01",
            "2026-06-30",
            "2026-08-02T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    reopened = open_database(path)
    runs = reopened.execute(
        "SELECT run_kind, status FROM sync_runs ORDER BY run_id"
    ).fetchall()
    assert runs == [
        ("incremental", "interrupted"),
        ("coverage_backfill", "interrupted"),
    ]
    incremental = reopened.execute(
        """SELECT completed_through_utc, status, last_error_code
           FROM category_sync_state WHERE category = 'cs.LO'"""
    ).fetchone()
    assert incremental == ("2026-08-01", "failed", "interrupted")
    backfill = reopened.execute(
        """SELECT completed_through_utc, pending_backfill_start,
                  pending_backfill_until, status, last_error_code
           FROM category_sync_state WHERE category = 'math.LO'"""
    ).fetchone()
    assert backfill == (
        "2026-08-01",
        "2026-06-01",
        "2026-06-30",
        "idle",
        None,
    )
    reopened.close()


def test_missing_required_schema_object_is_reported_as_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE enrichment_days")
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)


@pytest.mark.parametrize(
    "table_name",
    ("setup_draft", "profile_publication", "application_settings"),
)
def test_missing_setup_schema_object_is_reported_as_corruption(
    tmp_path: Path,
    table_name: str,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(f"DROP TABLE {table_name}")
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)


def test_missing_fts_maintenance_trigger_is_reported_as_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER article_authors_fts_update")
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)


def test_newer_schema_version_is_not_opened(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (
            _current_migration_versions()[-1] + 1,
            "2026-08-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)


def test_foreign_key_corruption_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """INSERT INTO article_versions(arxiv_id, version, submitted_at)
           VALUES (?, ?, ?)""",
        ("2608.09999", 1, "2026-08-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)


def test_logically_stale_fts_projection_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = open_database(path)
    _insert_article(connection, "2608.00005")
    connection.execute(
        "INSERT INTO article_authors(arxiv_id, position, name) VALUES (?, ?, ?)",
        ("2608.00005", 0, "Ada Example"),
    )
    connection.execute(
        "UPDATE papers_fts SET title = ? WHERE arxiv_id = ?",
        ("Stale title", "2608.00005"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorruptDatabaseError):
        open_database(path)
