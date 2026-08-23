from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ContextManager

from arxiv_digest.models import (
    AnnounceType,
    Confidence,
    DateBasis,
    EnrichmentStatus,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
    ReviewEvent,
)
from arxiv_digest.maintenance import MaintenanceBarrier


class _LeasedConnection(sqlite3.Connection):
    _maintenance_lease: ContextManager[None] | None = None

    def close(self) -> None:
        lease = self._maintenance_lease
        self._maintenance_lease = None
        try:
            super().close()
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be aware UTC")
    return value.isoformat().replace("+00:00", "Z")


def _date_text(value: date) -> str:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("calendar date required")
    return value.isoformat()


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("stored calendar date is invalid") from error
    if parsed.isoformat() != value:
        raise ValueError("stored calendar date is not normalized")
    return parsed


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("stored timestamp is not UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("stored timestamp is invalid") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("stored timestamp is not UTC")
    return parsed


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("SHA-256 value must be 64 lowercase hexadecimal characters")


def _metadata_hash(metadata: PaperMetadata) -> str:
    payload = {
        "abstract": metadata.abstract,
        "arxiv_id": metadata.arxiv_id,
        "authors": metadata.authors,
        "categories": metadata.categories,
        "comments": metadata.comments,
        "doi": metadata.doi,
        "journal_ref": metadata.journal_ref,
        "primary_category": metadata.primary_category,
        "title": metadata.title,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FinishResult:
    reviewed_count: int
    through_revision: int


@dataclass(frozen=True, slots=True)
class StoredArticleSnapshot:
    category: str
    metadata: PaperMetadata
    versions: tuple[PaperVersion, ...]
    observed_categories: tuple[str, ...]
    last_oai_datestamp: date
    last_raw_sha256: str
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    metadata: PaperMetadata
    saved_version: int | None
    latest_version: int | None
    paper_available: bool
    local_pdf_versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StoreReviewSnapshot:
    day: date
    snapshot_revision: int
    events: tuple[ReviewEvent, ...]
    anchor_event_id: int | None
    profile_revision: int
    last_finished_revision: int | None
    papers: tuple[PaperMetadata, ...]
    seed_papers: tuple[PaperMetadata, ...]
    saved_papers: tuple[PaperMetadata, ...]


@dataclass(frozen=True, slots=True)
class ReviewDateLinks:
    previous_date: date | None
    next_date: date | None


@dataclass(frozen=True, slots=True)
class ReviewPosition:
    day: date
    snapshot_revision: int
    anchor_event_id: int
    profile_revision: int


@dataclass(frozen=True, slots=True)
class EnrichmentDayRecord:
    category: str
    mailing_date: date
    source: str
    status: EnrichmentStatus
    fetched_at: datetime
    raw_sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"atom", "catchup"}:
            raise ValueError("enrichment source must be atom or catchup")
        _date_text(self.mailing_date)
        _utc_text(self.fetched_at)
        if self.raw_sha256 is not None:
            _require_sha256(self.raw_sha256)
        if self.status is EnrichmentStatus.FAILED:
            if not self.error_code or not self.error_message:
                raise ValueError("failed enrichment requires safe error fields")
        elif self.error_code is not None or self.error_message is not None:
            raise ValueError("successful enrichment cannot carry an error")


@dataclass(frozen=True, slots=True)
class DownloadFileRecord:
    arxiv_id: str
    version: int
    filename: str
    byte_count: int
    sha256: str
    last_verified_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("download version must be positive")
        if self.byte_count < 1:
            raise ValueError("download byte count must be positive")
        if (
            not self.filename
            or self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ValueError("download filename must be destination-relative")
        _require_sha256(self.sha256)
        _utc_text(self.last_verified_at)


@dataclass(frozen=True, slots=True)
class CategorySyncRecord:
    category: str
    set_spec: str
    coverage_start: date
    completed_through_utc: date | None
    pending_backfill_start: date | None
    pending_backfill_until: date | None
    status: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class SyncRunRecord:
    run_id: int
    category: str
    run_kind: str
    requested_from: date
    requested_until: date | None
    status: str
    pages_applied: int
    records_applied: int
    final_response_at: datetime | None
    error_code: str | None
    error_message: str | None


class Store:
    def __init__(
        self,
        database_path: Path,
        *,
        maintenance: MaintenanceBarrier | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.maintenance = maintenance

    def _connect(self) -> sqlite3.Connection:
        lease = None if self.maintenance is None else self.maintenance.operation()
        if lease is not None:
            lease.__enter__()
        try:
            connection = sqlite3.connect(
                self.database_path,
                factory=_LeasedConnection,
            )
        except Exception:
            if lease is not None:
                lease.__exit__(None, None, None)
            raise
        if isinstance(connection, _LeasedConnection):
            connection._maintenance_lease = lease
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except Exception:
            connection.close()
            raise

    def ensure_category_state(
        self, category: str, set_spec: str, coverage_start: date
    ) -> CategorySyncRecord:
        if not category.strip() or not set_spec.strip():
            raise ValueError("category and set specification must not be blank")
        coverage_text = _date_text(coverage_start)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO category_sync_state(
                       category, set_spec, coverage_start
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(category) DO NOTHING""",
                (category, set_spec, coverage_text),
            )
            row = connection.execute(
                "SELECT * FROM category_sync_state WHERE category = ?",
                (category,),
            ).fetchone()
            if row is None:
                raise RuntimeError("category synchronization state was not created")
            if row["set_spec"] != set_spec:
                raise ValueError("category OAI set specification changed")
            connection.commit()
            return self._category_sync_record(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _category_sync_record(row: sqlite3.Row) -> CategorySyncRecord:
        return CategorySyncRecord(
            category=row["category"],
            set_spec=row["set_spec"],
            coverage_start=_parse_date(row["coverage_start"]),
            completed_through_utc=(
                None
                if row["completed_through_utc"] is None
                else _parse_date(row["completed_through_utc"])
            ),
            pending_backfill_start=(
                None
                if row["pending_backfill_start"] is None
                else _parse_date(row["pending_backfill_start"])
            ),
            pending_backfill_until=(
                None
                if row["pending_backfill_until"] is None
                else _parse_date(row["pending_backfill_until"])
            ),
            status=row["status"],
            last_attempt_at=(
                None
                if row["last_attempt_at"] is None
                else _parse_utc(row["last_attempt_at"])
            ),
            last_success_at=(
                None
                if row["last_success_at"] is None
                else _parse_utc(row["last_success_at"])
            ),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
        )

    def category_sync_state(self, category: str) -> CategorySyncRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM category_sync_state WHERE category = ?",
                (category,),
            ).fetchone()
            if row is None:
                raise KeyError(category)
            return self._category_sync_record(row)
        finally:
            connection.close()

    def latest_sync_run(
        self, category: str, run_kind: str
    ) -> SyncRunRecord | None:
        if run_kind not in {"incremental", "coverage_backfill"}:
            raise ValueError("invalid sync run kind")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM sync_runs
                   WHERE category = ? AND run_kind = ?
                   ORDER BY run_id DESC LIMIT 1""",
                (category, run_kind),
            ).fetchone()
            if row is None:
                return None
            return SyncRunRecord(
                run_id=int(row["run_id"]),
                category=row["category"],
                run_kind=row["run_kind"],
                requested_from=_parse_date(row["requested_from"]),
                requested_until=(
                    None
                    if row["requested_until"] is None
                    else _parse_date(row["requested_until"])
                ),
                status=row["status"],
                pages_applied=int(row["pages_applied"]),
                records_applied=int(row["records_applied"]),
                final_response_at=(
                    None
                    if row["final_response_at"] is None
                    else _parse_utc(row["final_response_at"])
                ),
                error_code=row["error_code"],
                error_message=row["error_message"],
            )
        finally:
            connection.close()

    def enrichment_records(
        self, category: str
    ) -> tuple[EnrichmentDayRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM enrichment_days WHERE category = ?
                   ORDER BY mailing_date, source""",
                (category,),
            ).fetchall()
            return tuple(
                EnrichmentDayRecord(
                    category=row["category"],
                    mailing_date=_parse_date(row["mailing_date"]),
                    source=row["source"],
                    status=EnrichmentStatus(row["status"]),
                    fetched_at=_parse_utc(row["fetched_at"]),
                    raw_sha256=row["raw_sha256"],
                    error_code=row["error_code"],
                    error_message=row["error_message"],
                )
                for row in rows
            )
        finally:
            connection.close()

    def candidate_mailing_evidence(
        self,
        category: str,
        window_start: date,
        window_end: date,
    ) -> tuple[tuple[str, date], ...]:
        """Return corroborating Atom/catch-up mailing dates for candidates."""

        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must not be blank")
        start_text = _date_text(window_start)
        end_text = _date_text(window_end)
        if window_start > window_end:
            raise ValueError("candidate evidence window is reversed")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT DISTINCT r.arxiv_id, e.mailing_date
                   FROM review_events AS r
                   JOIN event_evidence AS e ON e.event_id = r.event_id
                   WHERE e.category = ?
                     AND e.source IN ('atom', 'catchup')
                     AND e.mailing_date BETWEEN ? AND ?
                   ORDER BY r.arxiv_id, e.mailing_date""",
                (category, start_text, end_text),
            ).fetchall()
            return tuple(
                (row["arxiv_id"], _parse_date(row["mailing_date"]))
                for row in rows
            )
        finally:
            connection.close()

    def begin_sync_run(
        self,
        category: str,
        run_kind: str,
        requested_from: date,
        requested_until: date | None,
        started_at: datetime,
    ) -> int:
        if run_kind not in {"incremental", "coverage_backfill"}:
            raise ValueError("invalid sync run kind")
        if (run_kind == "incremental") != (requested_until is None):
            raise ValueError("sync run kind does not match requested until date")
        from_text = _date_text(requested_from)
        until_text = (
            None if requested_until is None else _date_text(requested_until)
        )
        started_text = _utc_text(started_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO sync_runs(
                       category, run_kind, requested_from, requested_until,
                       started_at, status
                   ) VALUES (?, ?, ?, ?, ?, 'running')""",
                (category, run_kind, from_text, until_text, started_text),
            )
            if run_kind == "incremental":
                connection.execute(
                    """UPDATE category_sync_state
                       SET status = 'syncing', last_attempt_at = ?,
                           last_error_code = NULL, last_error_message = NULL
                       WHERE category = ?""",
                    (started_text, category),
                )
            connection.commit()
            return int(cursor.lastrowid)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_incremental_run(
        self,
        run_id: int,
        checkpoint_date: date,
        final_response_at: datetime,
        completed_at: datetime,
    ) -> None:
        checkpoint_text = _date_text(checkpoint_date)
        final_text = _utc_text(final_response_at)
        completed_text = _utc_text(completed_at)
        if final_response_at.date() != checkpoint_date:
            raise ValueError("checkpoint date must match final response UTC day")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT category, run_kind, status FROM sync_runs
                   WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            if run is None or run["run_kind"] != "incremental":
                raise ValueError("incremental sync run not found")
            if run["status"] != "running":
                raise ValueError("sync run is not running")
            connection.execute(
                """UPDATE sync_runs
                   SET status = 'completed', final_response_at = ?,
                       completed_at = ?
                   WHERE run_id = ?""",
                (final_text, completed_text, run_id),
            )
            connection.execute(
                """UPDATE category_sync_state
                   SET completed_through_utc = ?, status = 'idle',
                       last_success_at = ?, last_error_code = NULL,
                       last_error_message = NULL
                   WHERE category = ?""",
                (checkpoint_text, completed_text, run["category"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_pending_backfill(
        self,
        category: str,
        new_start: date,
        old_coverage_start: date,
    ) -> None:
        if new_start >= old_coverage_start:
            raise ValueError("backfill start must extend coverage earlier")
        new_text = _date_text(new_start)
        old_text = _date_text(old_coverage_start)
        until_text = _date_text(old_coverage_start - timedelta(days=1))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE category_sync_state
                   SET pending_backfill_start = ?, pending_backfill_until = ?
                   WHERE category = ? AND coverage_start = ?""",
                (new_text, until_text, category, old_text),
            )
            if cursor.rowcount != 1:
                raise ValueError("category coverage start changed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_backfill_run(
        self,
        run_id: int,
        new_coverage_start: date,
        final_response_at: datetime,
        completed_at: datetime,
    ) -> None:
        new_text = _date_text(new_coverage_start)
        final_text = _utc_text(final_response_at)
        completed_text = _utc_text(completed_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT r.category, r.run_kind, r.status, r.requested_from,
                          r.requested_until, c.pending_backfill_start,
                          c.pending_backfill_until
                   FROM sync_runs AS r
                   JOIN category_sync_state AS c ON c.category = r.category
                   WHERE r.run_id = ?""",
                (run_id,),
            ).fetchone()
            if run is None or run["run_kind"] != "coverage_backfill":
                raise ValueError("coverage backfill run not found")
            if run["status"] != "running":
                raise ValueError("sync run is not running")
            if (
                run["requested_from"] != new_text
                or run["pending_backfill_start"] != new_text
                or run["requested_until"] != run["pending_backfill_until"]
            ):
                raise ValueError("backfill run does not match pending interval")
            connection.execute(
                """UPDATE sync_runs
                   SET status = 'completed', final_response_at = ?,
                       completed_at = ?
                   WHERE run_id = ?""",
                (final_text, completed_text, run_id),
            )
            connection.execute(
                """UPDATE category_sync_state
                   SET coverage_start = ?, pending_backfill_start = NULL,
                       pending_backfill_until = NULL
                   WHERE category = ?""",
                (new_text, run["category"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_sync_run(
        self,
        run_id: int,
        error_code: str,
        message: str,
        failed_at: datetime,
    ) -> None:
        if not error_code.strip():
            raise ValueError("error code must not be blank")
        if not message.strip():
            raise ValueError("error message must not be blank")
        failed_text = _utc_text(failed_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT category, run_kind, status FROM sync_runs
                   WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            if run is None or run["status"] != "running":
                raise ValueError("running sync run not found")
            connection.execute(
                """UPDATE sync_runs
                   SET status = 'failed', failed_at = ?, error_code = ?,
                       error_message = ?
                   WHERE run_id = ?""",
                (failed_text, error_code, message, run_id),
            )
            if run["run_kind"] == "incremental":
                connection.execute(
                    """UPDATE category_sync_state
                       SET status = 'failed', last_error_code = ?,
                           last_error_message = ?
                       WHERE category = ?""",
                    (error_code, message, run["category"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_oai_page(
        self,
        run_id: int,
        category: str,
        records: tuple[OaiArticle | OaiTombstone, ...],
        candidates: tuple[EventCandidate, ...],
        raw_sha256: str,
        observed_at: datetime,
    ) -> tuple[ReviewEvent, ...]:
        _require_sha256(raw_sha256)
        observed_text = _utc_text(observed_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT category, status FROM sync_runs WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or run["category"] != category
                or run["status"] != "running"
            ):
                raise ValueError("OAI page does not match a running sync")
            metadata_by_id: dict[str, PaperMetadata] = {}
            for record in records:
                if isinstance(record, OaiTombstone):
                    set_specs_json = json.dumps(
                        record.set_specs,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    candidate_id = record.oai_identifier.rsplit(":", 1)[-1]
                    arxiv_id = (
                        candidate_id
                        if connection.execute(
                            "SELECT 1 FROM articles WHERE arxiv_id = ?",
                            (candidate_id,),
                        ).fetchone()
                        is not None
                        else None
                    )
                    connection.execute(
                        """INSERT INTO oai_tombstones(
                               oai_identifier, arxiv_id, oai_datestamp,
                               set_specs_json, observed_at
                           ) VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(oai_identifier) DO UPDATE SET
                               arxiv_id = excluded.arxiv_id,
                               oai_datestamp = excluded.oai_datestamp,
                               set_specs_json = excluded.set_specs_json,
                               observed_at = excluded.observed_at""",
                        (
                            record.oai_identifier,
                            arxiv_id,
                            _date_text(record.oai_datestamp),
                            set_specs_json,
                            observed_text,
                        ),
                    )
                    if arxiv_id is not None:
                        connection.execute(
                            """UPDATE articles
                               SET is_deleted = 1, deleted_at = ?
                               WHERE arxiv_id = ?""",
                            (observed_text, arxiv_id),
                        )
                    continue
                metadata = record.metadata
                metadata_by_id[metadata.arxiv_id] = metadata
                self._upsert_article(connection, metadata)
                self._upsert_versions(
                    connection, metadata.arxiv_id, record.versions
                )
                datestamp_text = _date_text(record.oai_datestamp)
                connection.execute(
                    """UPDATE articles SET last_oai_datestamp = ?
                       WHERE arxiv_id = ?""",
                    (datestamp_text, metadata.arxiv_id),
                )
                categories_json = json.dumps(
                    metadata.categories,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                category_hash = hashlib.sha256(
                    json.dumps(
                        sorted(metadata.categories),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """INSERT INTO category_article_state(
                           category, arxiv_id, last_oai_datestamp,
                           category_set_hash, observed_categories_json,
                           last_raw_sha256, last_seen_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(category, arxiv_id) DO UPDATE SET
                           last_oai_datestamp = excluded.last_oai_datestamp,
                           category_set_hash = excluded.category_set_hash,
                           observed_categories_json = excluded.observed_categories_json,
                           last_raw_sha256 = excluded.last_raw_sha256,
                           last_seen_at = excluded.last_seen_at""",
                    (
                        category,
                        metadata.arxiv_id,
                        datestamp_text,
                        category_hash,
                        categories_json,
                        raw_sha256,
                        observed_text,
                    ),
                )
            event_ids = self._apply_candidates(
                connection, metadata_by_id, candidates
            )
            connection.execute(
                """UPDATE sync_runs
                   SET pages_applied = pages_applied + 1,
                       records_applied = records_applied + ?
                   WHERE run_id = ?""",
                (len(records), run_id),
            )
            result = tuple(
                self._event_from_connection(connection, event_id)
                for event_id in event_ids
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_enrichment_day(self, result: EnrichmentDayRecord) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO enrichment_days(
                       category, mailing_date, source, status, fetched_at,
                       raw_sha256, error_code, error_message
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(category, mailing_date, source) DO UPDATE SET
                       status = excluded.status,
                       fetched_at = excluded.fetched_at,
                       raw_sha256 = excluded.raw_sha256,
                       error_code = excluded.error_code,
                       error_message = excluded.error_message""",
                (
                    result.category,
                    _date_text(result.mailing_date),
                    result.source,
                    result.status.value,
                    _utc_text(result.fetched_at),
                    result.raw_sha256,
                    result.error_code,
                    result.error_message,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _upsert_article(
        connection: sqlite3.Connection, metadata: PaperMetadata
    ) -> None:
        connection.execute(
            """INSERT INTO articles(
                   arxiv_id, title, abstract, primary_category, comments,
                   journal_ref, doi, metadata_hash, is_deleted
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(arxiv_id) DO UPDATE SET
                   title = excluded.title,
                   abstract = excluded.abstract,
                   primary_category = excluded.primary_category,
                   comments = excluded.comments,
                   journal_ref = excluded.journal_ref,
                   doi = excluded.doi,
                   metadata_hash = excluded.metadata_hash""",
            (
                metadata.arxiv_id,
                metadata.title,
                metadata.abstract,
                metadata.primary_category,
                metadata.comments,
                metadata.journal_ref,
                metadata.doi,
                _metadata_hash(metadata),
            ),
        )
        connection.execute(
            "DELETE FROM article_authors WHERE arxiv_id = ?",
            (metadata.arxiv_id,),
        )
        connection.executemany(
            """INSERT INTO article_authors(arxiv_id, position, name)
               VALUES (?, ?, ?)""",
            (
                (metadata.arxiv_id, position, name)
                for position, name in enumerate(metadata.authors)
            ),
        )
        connection.execute(
            "DELETE FROM article_categories WHERE arxiv_id = ?",
            (metadata.arxiv_id,),
        )
        connection.executemany(
            """INSERT INTO article_categories(arxiv_id, category, is_primary)
               VALUES (?, ?, ?)""",
            (
                (
                    metadata.arxiv_id,
                    category,
                    int(category == metadata.primary_category),
                )
                for category in metadata.categories
            ),
        )

    @staticmethod
    def _upsert_versions(
        connection: sqlite3.Connection,
        arxiv_id: str,
        versions: tuple[PaperVersion, ...],
    ) -> None:
        connection.executemany(
            """INSERT INTO article_versions(
                   arxiv_id, version, submitted_at, size, source_type
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(arxiv_id, version) DO UPDATE SET
                   submitted_at = excluded.submitted_at,
                   size = excluded.size,
                   source_type = excluded.source_type""",
            (
                (
                    arxiv_id,
                    version.number,
                    _utc_text(version.submitted_at),
                    version.size,
                    version.source_type,
                )
                for version in versions
            ),
        )

    @staticmethod
    def _validate_candidate(
        metadata: PaperMetadata, candidate: EventCandidate
    ) -> None:
        evidence = candidate.evidence
        if candidate.arxiv_id != metadata.arxiv_id:
            raise ValueError("event candidate belongs to a different article")
        if (
            evidence.announced_version is not None
            and evidence.announced_version != candidate.announced_version
        ):
            raise ValueError("event evidence version does not match candidate")
        if not evidence.source_key.strip():
            raise ValueError("event source key must not be blank")

    @staticmethod
    def _next_queue_revision(connection: sqlite3.Connection) -> int:
        connection.execute(
            "UPDATE state_meta SET queue_revision = queue_revision + 1 WHERE singleton = 1"
        )
        row = connection.execute(
            "SELECT queue_revision FROM state_meta WHERE singleton = 1"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _associated_event_ids(
        connection: sqlite3.Connection, candidate: EventCandidate
    ) -> set[int]:
        """Return only event identities supported by the explicit join rules."""

        matched = {
            int(row[0])
            for row in connection.execute(
                """SELECT event_id FROM review_events
                   WHERE arxiv_id = ?
                     AND COALESCE(announced_version, 0) = COALESCE(?, 0)
                     AND effective_date = ?""",
                (
                    candidate.arxiv_id,
                    candidate.announced_version,
                    _date_text(candidate.effective_date),
                ),
            )
        }
        source = candidate.evidence.source
        version = candidate.announced_version

        if version is not None and source in {
            EvidenceSource.ATOM,
            EvidenceSource.CATCHUP,
        }:
            provisional = {
                int(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT r.event_id
                       FROM review_events AS r
                       JOIN event_evidence AS e ON e.event_id = r.event_id
                       WHERE r.arxiv_id = ? AND r.announced_version = ?
                         AND e.source = 'oai'""",
                    (candidate.arxiv_id, version),
                )
            }
            if len(provisional) == 1:
                matched.update(provisional)

        if version is not None and source is EvidenceSource.OAI:
            corroborated = {
                int(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT r.event_id
                       FROM review_events AS r
                       JOIN event_evidence AS e ON e.event_id = r.event_id
                       WHERE r.arxiv_id = ? AND r.announced_version = ?
                         AND e.source IN ('atom', 'catchup')""",
                    (candidate.arxiv_id, version),
                )
            }
            if len(corroborated) == 1:
                matched.update(corroborated)

        if source is EvidenceSource.ATOM:
            matched.update(
                int(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT r.event_id
                       FROM review_events AS r
                       JOIN event_evidence AS e ON e.event_id = r.event_id
                       WHERE r.arxiv_id = ? AND r.announced_version IS NULL
                         AND r.effective_date = ? AND e.source = 'catchup'
                         AND e.category = ?""",
                    (
                        candidate.arxiv_id,
                        _date_text(candidate.effective_date),
                        candidate.evidence.category,
                    ),
                )
            )

        if source is EvidenceSource.CATCHUP and version is None:
            compatible = {
                int(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT r.event_id
                       FROM review_events AS r
                       JOIN event_evidence AS e ON e.event_id = r.event_id
                       WHERE r.arxiv_id = ? AND r.announced_version IS NOT NULL
                         AND r.effective_date = ?
                         AND e.source IN ('atom', 'oai')
                         AND e.category = ?""",
                    (
                        candidate.arxiv_id,
                        _date_text(candidate.effective_date),
                        candidate.evidence.category,
                    ),
                )
            }
            if len(compatible) == 1:
                matched.update(compatible)

        return matched

    @classmethod
    def _merge_associated_events(
        cls,
        connection: sqlite3.Connection,
        event_ids: set[int],
        candidate: EventCandidate,
    ) -> int:
        placeholders = ",".join("?" for _ in event_ids)
        rows = connection.execute(
            f"""SELECT event_id, announced_version, effective_date,
                       date_basis, confidence, queue_revision, reviewed_at
                FROM review_events WHERE event_id IN ({placeholders})""",
            tuple(sorted(event_ids)),
        ).fetchall()
        if len(rows) != len(event_ids):
            raise ValueError("associated review event disappeared")

        rank = {
            Confidence.CURRENT.value: 0,
            Confidence.RECOVERED.value: 1,
            Confidence.INFERRED.value: 2,
        }
        strongest_existing = min(
            rows, key=lambda row: (rank[row["confidence"]], row["event_id"])
        )
        incoming_rank = rank[candidate.evidence.confidence.value]
        if incoming_rank < rank[strongest_existing["confidence"]]:
            canonical_version = candidate.announced_version
            if (
                canonical_version is None
                and candidate.evidence.source is EvidenceSource.CATCHUP
            ):
                supported_versions = {
                    int(row["announced_version"])
                    for row in rows
                    if row["announced_version"] is not None
                }
                if len(supported_versions) == 1:
                    canonical_version = supported_versions.pop()
            canonical_date = _date_text(candidate.effective_date)
            canonical_basis = candidate.date_basis.value
            canonical_confidence = candidate.evidence.confidence.value
        else:
            canonical_version = strongest_existing["announced_version"]
            canonical_date = strongest_existing["effective_date"]
            canonical_basis = strongest_existing["date_basis"]
            canonical_confidence = strongest_existing["confidence"]

        survivor = min(event_ids)
        survivor_row = next(row for row in rows if row["event_id"] == survivor)
        losers = sorted(event_ids - {survivor})
        reviewed_values = sorted(
            row["reviewed_at"] for row in rows if row["reviewed_at"] is not None
        )
        reviewed_at = reviewed_values[0] if reviewed_values else None
        relocating = any(
            row["announced_version"] != canonical_version
            or row["effective_date"] != canonical_date
            for row in rows
        )
        queue_revision = int(survivor_row["queue_revision"])
        if relocating and reviewed_at is None:
            queue_revision = cls._next_queue_revision(connection)

        for loser in losers:
            connection.execute(
                """UPDATE review_date_state SET anchor_event_id = ?
                   WHERE anchor_event_id = ?""",
                (survivor, loser),
            )
            connection.execute(
                "UPDATE event_evidence SET event_id = ? WHERE event_id = ?",
                (survivor, loser),
            )
            connection.execute(
                "DELETE FROM review_events WHERE event_id = ?", (loser,)
            )

        connection.execute(
            """UPDATE review_events
               SET announced_version = ?, effective_date = ?, date_basis = ?,
                   confidence = ?, queue_revision = ?, reviewed_at = ?
               WHERE event_id = ?""",
            (
                canonical_version,
                canonical_date,
                canonical_basis,
                canonical_confidence,
                queue_revision,
                reviewed_at,
                survivor,
            ),
        )
        return survivor

    @classmethod
    def _apply_candidates(
        cls,
        connection: sqlite3.Connection,
        metadata_by_id: dict[str, PaperMetadata],
        candidates: tuple[EventCandidate, ...],
    ) -> list[int]:
        event_ids: list[int] = []
        for candidate in candidates:
            try:
                metadata = metadata_by_id[candidate.arxiv_id]
            except KeyError as error:
                raise ValueError(
                    "event candidate has no metadata in the batch"
                ) from error
            cls._validate_candidate(metadata, candidate)
            article_state = connection.execute(
                "SELECT is_deleted FROM articles WHERE arxiv_id = ?",
                (candidate.arxiv_id,),
            ).fetchone()
            if article_state is not None and bool(article_state[0]):
                continue
            existing_evidence = connection.execute(
                "SELECT event_id FROM event_evidence WHERE source_key = ?",
                (candidate.evidence.source_key,),
            ).fetchone()
            if existing_evidence is not None:
                event_id = int(existing_evidence[0])
            else:
                matched_ids = cls._associated_event_ids(connection, candidate)
                if not matched_ids:
                    revision = cls._next_queue_revision(connection)
                    cursor = connection.execute(
                        """INSERT INTO review_events(
                               arxiv_id, announced_version, effective_date,
                               date_basis, confidence, queue_revision
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            candidate.arxiv_id,
                            candidate.announced_version,
                            candidate.effective_date.isoformat(),
                            candidate.date_basis.value,
                            candidate.evidence.confidence.value,
                            revision,
                        ),
                    )
                    event_id = int(cursor.lastrowid)
                else:
                    event_id = cls._merge_associated_events(
                        connection, matched_ids, candidate
                    )
                evidence = candidate.evidence
                connection.execute(
                    """INSERT INTO event_evidence(
                           event_id, source_key, source, confidence, category,
                           announce_type, mailing_date, announced_version,
                           list_position, oai_datestamp, raw_sha256, observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        evidence.source_key,
                        evidence.source.value,
                        evidence.confidence.value,
                        evidence.category,
                        (
                            None
                            if evidence.announce_type is None
                            else evidence.announce_type.value
                        ),
                        (
                            None
                            if evidence.mailing_date is None
                            else evidence.mailing_date.isoformat()
                        ),
                        evidence.announced_version,
                        evidence.list_position,
                        (
                            None
                            if evidence.oai_datestamp is None
                            else evidence.oai_datestamp.isoformat()
                        ),
                        evidence.raw_sha256,
                        _utc_text(evidence.observed_at),
                    ),
                )
            event_ids = [
                value
                for value in event_ids
                if connection.execute(
                    "SELECT 1 FROM review_events WHERE event_id = ?", (value,)
                ).fetchone()
                is not None
            ]
            if event_id not in event_ids:
                event_ids.append(event_id)
        return event_ids

    @staticmethod
    def _event_from_connection(
        connection: sqlite3.Connection, event_id: int
    ) -> ReviewEvent:
        row = connection.execute(
            "SELECT * FROM review_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        evidence_rows = connection.execute(
            """SELECT * FROM event_evidence
               WHERE event_id = ? ORDER BY evidence_id""",
            (event_id,),
        ).fetchall()
        evidence = tuple(
            EventEvidence(
                source_key=value["source_key"],
                source=EvidenceSource(value["source"]),
                confidence=Confidence(value["confidence"]),
                category=value["category"],
                announce_type=(
                    None
                    if value["announce_type"] is None
                    else AnnounceType(value["announce_type"])
                ),
                mailing_date=(
                    None
                    if value["mailing_date"] is None
                    else _parse_date(value["mailing_date"])
                ),
                announced_version=value["announced_version"],
                list_position=value["list_position"],
                oai_datestamp=(
                    None
                    if value["oai_datestamp"] is None
                    else _parse_date(value["oai_datestamp"])
                ),
                raw_sha256=value["raw_sha256"],
                observed_at=_parse_utc(value["observed_at"]),
            )
            for value in evidence_rows
        )
        return ReviewEvent(
            event_id=row["event_id"],
            arxiv_id=row["arxiv_id"],
            announced_version=row["announced_version"],
            effective_date=_parse_date(row["effective_date"]),
            date_basis=DateBasis(row["date_basis"]),
            confidence=Confidence(row["confidence"]),
            evidence=evidence,
            queue_revision=row["queue_revision"],
            reviewed_at=(
                None
                if row["reviewed_at"] is None
                else _parse_utc(row["reviewed_at"])
            ),
        )

    @staticmethod
    def _metadata_from_connection(
        connection: sqlite3.Connection, arxiv_id: str
    ) -> PaperMetadata:
        row = connection.execute(
            "SELECT * FROM articles WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        if row is None:
            raise KeyError(arxiv_id)
        authors = tuple(
            value[0]
            for value in connection.execute(
                """SELECT name FROM article_authors
                   WHERE arxiv_id = ? ORDER BY position""",
                (arxiv_id,),
            )
        )
        categories = tuple(
            value[0]
            for value in connection.execute(
                """SELECT category FROM article_categories
                   WHERE arxiv_id = ? ORDER BY is_primary DESC, category""",
                (arxiv_id,),
            )
        )
        return PaperMetadata(
            arxiv_id=row["arxiv_id"],
            title=row["title"],
            authors=authors,
            abstract=row["abstract"],
            primary_category=row["primary_category"],
            categories=categories,
            comments=row["comments"],
            journal_ref=row["journal_ref"],
            doi=row["doi"],
        )

    def apply_event_batch(
        self,
        metadata: PaperMetadata,
        versions: tuple[PaperVersion, ...],
        candidates: tuple[EventCandidate, ...],
    ) -> tuple[ReviewEvent, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_article(connection, metadata)
            self._upsert_versions(connection, metadata.arxiv_id, versions)
            event_ids = self._apply_candidates(
                connection, {metadata.arxiv_id: metadata}, candidates
            )
            result = tuple(
                self._event_from_connection(connection, event_id)
                for event_id in event_ids
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def article_snapshots(
        self,
        category: str,
        arxiv_ids: set[str] | tuple[str, ...] | None = None,
    ) -> tuple[StoredArticleSnapshot, ...]:
        parameters: list[object] = [category]
        filter_sql = ""
        if arxiv_ids is not None:
            identifiers = sorted(set(arxiv_ids))
            if not identifiers:
                return ()
            if any(not isinstance(value, str) for value in identifiers):
                raise TypeError("arXiv IDs must be strings")
            filter_sql = (
                " AND s.arxiv_id IN ("
                + ",".join("?" for _ in identifiers)
                + ")"
            )
            parameters.extend(identifiers)
        connection = self._connect()
        try:
            state_rows = connection.execute(
                """SELECT s.*, a.title, a.abstract, a.primary_category,
                          a.comments, a.journal_ref, a.doi
                   FROM category_article_state AS s
                   JOIN articles AS a ON a.arxiv_id = s.arxiv_id
                   WHERE s.category = ? AND a.is_deleted = 0"""
                + filter_sql
                + " ORDER BY s.arxiv_id",
                parameters,
            ).fetchall()
            snapshots: list[StoredArticleSnapshot] = []
            for row in state_rows:
                authors = tuple(
                    value[0]
                    for value in connection.execute(
                        """SELECT name FROM article_authors
                           WHERE arxiv_id = ? ORDER BY position""",
                        (row["arxiv_id"],),
                    )
                )
                categories = tuple(
                    value[0]
                    for value in connection.execute(
                        """SELECT category FROM article_categories
                           WHERE arxiv_id = ?
                           ORDER BY is_primary DESC, category""",
                        (row["arxiv_id"],),
                    )
                )
                versions = tuple(
                    PaperVersion(
                        number=value["version"],
                        submitted_at=_parse_utc(value["submitted_at"]),
                        size=value["size"],
                        source_type=value["source_type"],
                    )
                    for value in connection.execute(
                        """SELECT version, submitted_at, size, source_type
                           FROM article_versions WHERE arxiv_id = ?
                           ORDER BY version""",
                        (row["arxiv_id"],),
                    )
                )
                try:
                    observed_raw = json.loads(row["observed_categories_json"])
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "stored observed categories are invalid"
                    ) from error
                if not isinstance(observed_raw, list) or any(
                    not isinstance(value, str) for value in observed_raw
                ):
                    raise ValueError("stored observed categories are invalid")
                _require_sha256(row["last_raw_sha256"])
                snapshots.append(
                    StoredArticleSnapshot(
                        category=row["category"],
                        metadata=PaperMetadata(
                            arxiv_id=row["arxiv_id"],
                            title=row["title"],
                            authors=authors,
                            abstract=row["abstract"],
                            primary_category=row["primary_category"],
                            categories=categories,
                            comments=row["comments"],
                            journal_ref=row["journal_ref"],
                            doi=row["doi"],
                        ),
                        versions=versions,
                        observed_categories=tuple(observed_raw),
                        last_oai_datestamp=_parse_date(
                            row["last_oai_datestamp"]
                        ),
                        last_raw_sha256=row["last_raw_sha256"],
                        last_seen_at=_parse_utc(row["last_seen_at"]),
                    )
                )
            return tuple(snapshots)
        finally:
            connection.close()

    def review_event(self, event_id: int) -> ReviewEvent:
        connection = self._connect()
        try:
            return self._event_from_connection(connection, event_id)
        finally:
            connection.close()

    def article_metadata(self, arxiv_id: str) -> PaperMetadata:
        connection = self._connect()
        try:
            return self._metadata_from_connection(connection, arxiv_id)
        finally:
            connection.close()

    def article_versions(self, arxiv_id: str) -> tuple[PaperVersion, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT version, submitted_at, size, source_type
                   FROM article_versions
                   WHERE arxiv_id = ? ORDER BY version""",
                (arxiv_id,),
            ).fetchall()
            return tuple(
                PaperVersion(
                    number=int(row["version"]),
                    submitted_at=_parse_utc(row["submitted_at"]),
                    size=row["size"],
                    source_type=row["source_type"],
                )
                for row in rows
            )
        finally:
            connection.close()

    def article_version(self, arxiv_id: str, version: int) -> PaperVersion:
        if version < 1:
            raise ValueError("article version must be positive")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT version, submitted_at, size, source_type
                   FROM article_versions
                   WHERE arxiv_id = ? AND version = ?""",
                (arxiv_id, version),
            ).fetchone()
            if row is None:
                raise KeyError((arxiv_id, version))
            return PaperVersion(
                number=int(row["version"]),
                submitted_at=_parse_utc(row["submitted_at"]),
                size=row["size"],
                source_type=row["source_type"],
            )
        finally:
            connection.close()

    def download_file(
        self, arxiv_id: str, version: int
    ) -> DownloadFileRecord | None:
        if version < 1:
            raise ValueError("download version must be positive")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT arxiv_id, version, filename, byte_count, sha256,
                          last_verified_at
                   FROM download_files
                   WHERE arxiv_id = ? AND version = ?""",
                (arxiv_id, version),
            ).fetchone()
            if row is None:
                return None
            return DownloadFileRecord(
                arxiv_id=row["arxiv_id"],
                version=int(row["version"]),
                filename=row["filename"],
                byte_count=int(row["byte_count"]),
                sha256=row["sha256"],
                last_verified_at=_parse_utc(row["last_verified_at"]),
            )
        finally:
            connection.close()

    def download_file_candidates(self) -> tuple[tuple[str, int, str], ...]:
        connection = self._connect()
        try:
            return tuple(
                (row["arxiv_id"], int(row["version"]), row["title"])
                for row in connection.execute(
                    """SELECT a.arxiv_id, v.version, a.title
                       FROM articles AS a
                       JOIN article_versions AS v
                         ON v.arxiv_id = a.arxiv_id
                       ORDER BY a.arxiv_id, v.version"""
                )
            )
        finally:
            connection.close()

    def replace_download_files(
        self, records: tuple[DownloadFileRecord, ...]
    ) -> None:
        if any(not isinstance(record, DownloadFileRecord) for record in records):
            raise TypeError("download records must be DownloadFileRecord values")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM download_files")
            connection.executemany(
                """INSERT INTO download_files(
                       arxiv_id, version, filename, byte_count, sha256,
                       last_verified_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    (
                        record.arxiv_id,
                        record.version,
                        record.filename,
                        record.byte_count,
                        record.sha256,
                        _utc_text(record.last_verified_at),
                    )
                    for record in records
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_download_file(self, record: DownloadFileRecord) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO download_files(
                       arxiv_id, version, filename, byte_count, sha256,
                       last_verified_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(arxiv_id, version) DO UPDATE SET
                       filename = excluded.filename,
                       byte_count = excluded.byte_count,
                       sha256 = excluded.sha256,
                       last_verified_at = excluded.last_verified_at""",
                (
                    record.arxiv_id,
                    record.version,
                    record.filename,
                    record.byte_count,
                    record.sha256,
                    _utc_text(record.last_verified_at),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def review_snapshot(
        self,
        day: date,
        *,
        seed_ids: tuple[str, ...] = (),
    ) -> StoreReviewSnapshot:
        day_text = _date_text(day)
        if not isinstance(seed_ids, tuple) or any(
            not isinstance(value, str) for value in seed_ids
        ):
            raise TypeError("seed IDs must be a tuple of strings")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            snapshot_revision = int(
                connection.execute(
                    "SELECT queue_revision FROM state_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            event_rows = connection.execute(
                """SELECT event_id FROM review_events
                   WHERE effective_date = ? AND queue_revision <= ?
                   ORDER BY queue_revision, event_id""",
                (day_text, snapshot_revision),
            ).fetchall()
            events = tuple(
                self._event_from_connection(connection, int(row[0]))
                for row in event_rows
            )
            papers = tuple(
                self._metadata_from_connection(connection, arxiv_id)
                for arxiv_id in dict.fromkeys(
                    event.arxiv_id for event in events
                )
            )
            seed_papers = tuple(
                self._metadata_from_connection(connection, arxiv_id)
                for arxiv_id in dict.fromkeys(seed_ids)
            )
            saved_papers = tuple(
                self._metadata_from_connection(connection, row[0])
                for row in connection.execute(
                    "SELECT arxiv_id FROM saved_papers ORDER BY arxiv_id"
                )
            )
            state = connection.execute(
                """SELECT anchor_event_id, profile_revision,
                          last_finished_revision
                   FROM review_date_state WHERE effective_date = ?""",
                (day_text,),
            ).fetchone()
            connection.commit()
            return StoreReviewSnapshot(
                day=day,
                snapshot_revision=snapshot_revision,
                events=events,
                anchor_event_id=None if state is None else state[0],
                profile_revision=0 if state is None else state[1],
                last_finished_revision=None if state is None else state[2],
                papers=papers,
                seed_papers=seed_papers,
                saved_papers=saved_papers,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_review_dates(self) -> tuple[date, ...]:
        connection = self._connect()
        try:
            return tuple(
                _parse_date(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT effective_date FROM review_events
                       ORDER BY effective_date"""
                )
            )
        finally:
            connection.close()

    def review_date_links(self, day: date) -> ReviewDateLinks:
        day_text = _date_text(day)
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT
                       (SELECT MAX(effective_date) FROM review_events
                        WHERE effective_date < ?),
                       (SELECT MIN(effective_date) FROM review_events
                        WHERE effective_date > ?)""",
                (day_text, day_text),
            ).fetchone()
            return ReviewDateLinks(
                previous_date=None if row[0] is None else _parse_date(row[0]),
                next_date=None if row[1] is None else _parse_date(row[1]),
            )
        finally:
            connection.close()

    def record_position(
        self,
        day: date,
        snapshot_revision: int,
        anchor_event_id: int,
        profile_revision: int,
    ) -> ReviewPosition:
        if snapshot_revision < 0 or profile_revision < 0:
            raise ValueError("revisions must be nonnegative")
        day_text = _date_text(day)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(
                connection.execute(
                    "SELECT queue_revision FROM state_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            if snapshot_revision > current_revision:
                raise ValueError("snapshot revision is from the future")
            anchor = connection.execute(
                """SELECT 1 FROM review_events
                   WHERE event_id = ? AND effective_date = ?
                     AND queue_revision <= ?""",
                (anchor_event_id, day_text, snapshot_revision),
            ).fetchone()
            if anchor is None:
                raise ValueError("anchor event is not in the review snapshot")
            connection.execute(
                """INSERT INTO review_date_state(
                       effective_date, anchor_event_id, profile_revision
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(effective_date) DO UPDATE SET
                       anchor_event_id = excluded.anchor_event_id,
                       profile_revision = excluded.profile_revision""",
                (day_text, anchor_event_id, profile_revision),
            )
            connection.commit()
            return ReviewPosition(
                day, snapshot_revision, anchor_event_id, profile_revision
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def events_for_date(self, day: date) -> tuple[ReviewEvent, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT event_id FROM review_events
                   WHERE effective_date = ?
                   ORDER BY queue_revision, event_id""",
                (day.isoformat(),),
            ).fetchall()
            return tuple(
                self._event_from_connection(connection, int(row[0]))
                for row in rows
            )
        finally:
            connection.close()

    def save_paper(self, arxiv_id: str, version: int | None) -> None:
        if version is not None and version < 1:
            raise ValueError("saved version must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO saved_papers(arxiv_id, saved_version)
                   VALUES (?, ?)
                   ON CONFLICT(arxiv_id) DO UPDATE SET
                       saved_version = excluded.saved_version""",
                (arxiv_id, version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def remove_saved_paper(self, arxiv_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM saved_papers WHERE arxiv_id = ?", (arxiv_id,)
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def search_library(
        self, query: str, limit: int, offset: int
    ) -> tuple[LibraryEntry, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("library search limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("library search offset must be nonnegative")
        tokens = tuple(re.findall(r"\w+", query.casefold(), flags=re.UNICODE))
        connection = self._connect()
        try:
            if query.strip() and not tokens:
                return ()
            if tokens:
                match_query = " ".join(
                    '"' + token.replace('"', '""') + '"' for token in tokens
                )
                rows = connection.execute(
                    """SELECT a.arxiv_id, s.saved_version, a.is_deleted,
                              (SELECT MAX(version) FROM article_versions
                               WHERE arxiv_id = a.arxiv_id) AS latest_version,
                              bm25(papers_fts, 10.0, 6.0, 5.0, 1.0) AS rank
                       FROM papers_fts
                       JOIN articles AS a ON a.rowid = papers_fts.rowid
                       JOIN saved_papers AS s ON s.arxiv_id = a.arxiv_id
                       WHERE papers_fts MATCH ?
                       ORDER BY rank, a.arxiv_id
                       LIMIT ? OFFSET ?""",
                    (match_query, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT a.arxiv_id, s.saved_version, a.is_deleted,
                              MAX(v.version) AS latest_version
                       FROM saved_papers AS s
                       JOIN articles AS a ON a.arxiv_id = s.arxiv_id
                       LEFT JOIN article_versions AS v
                           ON v.arxiv_id = a.arxiv_id
                       GROUP BY a.arxiv_id
                       ORDER BY a.arxiv_id
                       LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
            return tuple(
                LibraryEntry(
                    metadata=self._metadata_from_connection(
                        connection, row["arxiv_id"]
                    ),
                    saved_version=row["saved_version"],
                    latest_version=row["latest_version"],
                    paper_available=not bool(row["is_deleted"]),
                    local_pdf_versions=tuple(
                        int(value[0])
                        for value in connection.execute(
                            """SELECT version FROM download_files
                               WHERE arxiv_id = ? ORDER BY version""",
                            (row["arxiv_id"],),
                        )
                    ),
                )
                for row in rows
            )
        finally:
            connection.close()

    def saved_paper_metadata(self) -> tuple[PaperMetadata, ...]:
        connection = self._connect()
        try:
            identifiers = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT arxiv_id FROM saved_papers ORDER BY arxiv_id"
                )
            )
            return tuple(
                self._metadata_from_connection(connection, arxiv_id)
                for arxiv_id in identifiers
            )
        finally:
            connection.close()

    def finish_date(
        self,
        day: date,
        *,
        through_revision: int,
        finished_at: datetime,
    ) -> FinishResult:
        if through_revision < 0:
            raise ValueError("through_revision must be nonnegative")
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("finished_at must be aware UTC")
        if finished_at.utcoffset().total_seconds() != 0:
            raise ValueError("finished_at must be aware UTC")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE review_events
                   SET reviewed_at = ?
                   WHERE effective_date = ?
                     AND queue_revision <= ?
                     AND reviewed_at IS NULL""",
                (_utc_text(finished_at), day.isoformat(), through_revision),
            )
            reviewed_count = cursor.rowcount
            connection.execute(
                """INSERT INTO review_date_state(
                       effective_date, last_finished_at, last_finished_revision
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(effective_date) DO UPDATE SET
                       last_finished_at = excluded.last_finished_at,
                       last_finished_revision = MAX(
                           COALESCE(review_date_state.last_finished_revision, 0),
                           excluded.last_finished_revision
                       )""",
                (day.isoformat(), _utc_text(finished_at), through_revision),
            )
            connection.commit()
            return FinishResult(reviewed_count, through_revision)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
