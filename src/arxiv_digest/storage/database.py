from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path


class CorruptDatabaseError(RuntimeError):
    pass


class LegacyDataGenerationError(CorruptDatabaseError):
    pass


_LEGACY_DATA_GENERATION_MESSAGE = (
    "This arXiv Digest data belongs to an incompatible release. Quit the app "
    "and follow Clean reset with recovery copy; the existing data was not "
    "modified."
)


_BASE_TABLES = frozenset(
    {
        "schema_migrations",
        "state_meta",
        "articles",
        "oai_tombstones",
        "article_versions",
        "article_authors",
        "article_categories",
        "category_sync_state",
        "sync_runs",
        "saved_papers",
        "papers_fts",
        "category_article_state",
    }
)
_LEGACY_REVIEW_TABLES = frozenset(
    {"review_events", "event_evidence", "review_date_state", "enrichment_days"}
)
_CONFIRMED_REVIEW_TABLES = frozenset(
    {
        "application_generation",
        "source_observations",
        "catchup_days",
        "canonical_events",
        "canonical_event_observations",
        "review_date_state",
        "reconciliation_diagnostics",
    }
)
_REQUIRED_TRIGGERS = frozenset(
    {
        "articles_fts_insert",
        "articles_fts_update",
        "articles_fts_delete",
        "article_authors_fts_insert",
        "article_authors_fts_update",
        "article_authors_fts_delete",
    }
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _preflight_existing_database(path: Path) -> None:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        marker = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table' AND name = 'application_generation'"""
        ).fetchone()
        if marker is None:
            raise LegacyDataGenerationError(_LEGACY_DATA_GENERATION_MESSAGE)
        generation_rows = connection.execute(
            "SELECT singleton, generation FROM application_generation"
        ).fetchall()
        if generation_rows != [(1, 2)]:
            raise LegacyDataGenerationError(_LEGACY_DATA_GENERATION_MESSAGE)
        applied = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        current_version = len(applied)
        if (
            applied != [(version,) for version in range(1, current_version + 1)]
            or current_version < 4
            or current_version > len(_migration_scripts())
        ):
            raise CorruptDatabaseError("database schema version is unsupported")
    finally:
        connection.close()


def _migration_scripts() -> tuple[tuple[int, str], ...]:
    migrations: list[tuple[int, str]] = []
    for resource in files("arxiv_digest.storage.migrations").iterdir():
        match = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", resource.name)
        if match is not None:
            migrations.append(
                (int(match.group(1)), resource.read_text(encoding="utf-8"))
            )
    migrations.sort()
    expected = list(range(1, len(migrations) + 1))
    if [version for version, _ in migrations] != expected:
        raise CorruptDatabaseError("database migrations are not contiguous")
    return tuple(migrations)


def _apply_migration(
    connection: sqlite3.Connection,
    version: int,
    script: str,
) -> None:
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + script)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (
                version,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _check(connection: sqlite3.Connection, expected_version: int) -> None:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise CorruptDatabaseError("database quick check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise CorruptDatabaseError("database foreign key check failed")
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    required_tables = _BASE_TABLES
    if expected_version < 4:
        required_tables = required_tables | _LEGACY_REVIEW_TABLES
    else:
        required_tables = required_tables | _CONFIRMED_REVIEW_TABLES
    if expected_version >= 2:
        required_tables = required_tables | {"download_files"}
    if expected_version >= 3:
        required_tables = required_tables | {
            "setup_draft",
            "profile_publication",
            "application_settings",
        }
    if not required_tables <= present:
        raise CorruptDatabaseError("database schema is incomplete")
    triggers = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    if not _REQUIRED_TRIGGERS <= triggers:
        raise CorruptDatabaseError("database maintenance triggers are incomplete")
    versions = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    expected_versions = [(version,) for version in range(1, expected_version + 1)]
    if versions != expected_versions:
        raise CorruptDatabaseError("database schema version is unsupported")
    connection.execute(
        "INSERT INTO papers_fts(papers_fts) VALUES ('integrity-check')"
    )
    connection.rollback()
    authoritative = connection.execute(
        """SELECT a.rowid, a.arxiv_id, a.title,
                  COALESCE((
                      SELECT group_concat(name, ' ')
                      FROM (
                          SELECT name FROM article_authors
                          WHERE arxiv_id = a.arxiv_id ORDER BY position
                      )
                  ), ''),
                  a.abstract
           FROM articles AS a ORDER BY a.rowid"""
    ).fetchall()
    indexed = connection.execute(
        """SELECT rowid, arxiv_id, title, authors, abstract
           FROM papers_fts ORDER BY rowid"""
    ).fetchall()
    if indexed != authoritative:
        raise CorruptDatabaseError("database search index is stale")


def _create_database(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = _connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            for version, script in _migration_scripts():
                _apply_migration(connection, version, script)
            _check(connection, len(_migration_scripts()))
        finally:
            connection.close()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _interrupt_stale_runs(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """UPDATE category_sync_state
               SET status = 'failed',
                   last_error_code = 'interrupted',
                   last_error_message = 'previous incremental sync interrupted'
               WHERE EXISTS (
                   SELECT 1 FROM sync_runs
                   WHERE sync_runs.category = category_sync_state.category
                     AND sync_runs.run_kind = 'incremental'
                     AND sync_runs.status = 'running'
               )"""
        )
        connection.execute(
            """UPDATE sync_runs
               SET status = 'interrupted', error_code = 'interrupted'
               WHERE status = 'running'"""
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def open_database(path: Path) -> sqlite3.Connection:
    path = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        if not path.exists():
            _create_database(path)
        else:
            _preflight_existing_database(path)
        connection = _connect(path)
        scripts = _migration_scripts()
        applied = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        current_version = len(applied)
        if applied != [(version,) for version in range(1, current_version + 1)]:
            raise CorruptDatabaseError("database schema version is unsupported")
        if current_version < 1 or current_version > len(scripts):
            raise CorruptDatabaseError("database schema version is unsupported")
        _check(connection, current_version)
        for version, script in scripts[current_version:]:
            _apply_migration(connection, version, script)
        _check(connection, len(scripts))
        connection.execute("PRAGMA journal_mode = WAL")
        _interrupt_stale_runs(connection)
        return connection
    except (sqlite3.DatabaseError, OSError) as error:
        if connection is not None:
            connection.close()
        raise CorruptDatabaseError("database could not be opened safely") from error
