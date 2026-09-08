from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ContextManager

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    CatchupDay,
    CatchupDayStatus,
    CategoryConfig,
    EnrichmentStatus,
    EvidenceSource,
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
    ReviewEvent,
    ReconciliationResult,
    SourceObservation,
    VersionResolution,
)
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.reconciliation import reconcile_paper


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


class ReviewSnapshotConflict(RuntimeError):
    """A Review mutation was created under a stale active projection."""

    code = "review_snapshot_stale"


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
    projection_revision: int
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
    projection_revision: int


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
class CatchupDayRecord:
    category: str
    daily_list_date: date
    status: CatchupDayStatus
    attempted_at: datetime | None
    response_sha256: str | None
    error_code: str | None


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
        return tuple(
            EnrichmentDayRecord(
                category=row.category,
                mailing_date=row.daily_list_date,
                source="catchup",
                status=EnrichmentStatus(row.status.value),
                fetched_at=row.attempted_at,
                raw_sha256=row.response_sha256,
                error_code=row.error_code,
                error_message=(
                    "The historical daily list could not be recovered."
                    if row.status is CatchupDayStatus.FAILED
                    else None
                ),
            )
            for row in self.catchup_day_records(category)
            if row.status is not CatchupDayStatus.PENDING
            and row.attempted_at is not None
        )

    def ensure_catchup_targets(
        self,
        category: str,
        dates: tuple[date, ...],
    ) -> None:
        if not isinstance(dates, tuple) or any(
            not isinstance(value, date) or isinstance(value, datetime)
            for value in dates
        ):
            raise TypeError("catch-up targets must be a tuple of dates")
        if not category.strip():
            raise ValueError("catch-up category must not be blank")
        normalized = tuple(sorted(set(dates)))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """INSERT INTO catchup_days(
                       category, daily_list_date, status
                   ) VALUES (?, ?, 'pending')
                   ON CONFLICT(category, daily_list_date) DO NOTHING""",
                (
                    (category, _date_text(daily_list_date))
                    for daily_list_date in normalized
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def catchup_day_records(
        self,
        category: str | None = None,
    ) -> tuple[CatchupDayRecord, ...]:
        connection = self._connect()
        try:
            if category is None:
                rows = connection.execute(
                    """SELECT * FROM catchup_days
                       ORDER BY daily_list_date, category"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM catchup_days WHERE category = ?
                       ORDER BY daily_list_date""",
                    (category,),
                ).fetchall()
            return tuple(
                CatchupDayRecord(
                    category=row["category"],
                    daily_list_date=_parse_date(row["daily_list_date"]),
                    status=CatchupDayStatus(row["status"]),
                    attempted_at=(
                        None
                        if row["attempted_at"] is None
                        else _parse_utc(row["attempted_at"])
                    ),
                    response_sha256=row["response_sha256"],
                    error_code=row["error_code"],
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
        """Return Atom feed or catch-up dates that support candidates."""

        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must not be blank")
        start_text = _date_text(window_start)
        end_text = _date_text(window_end)
        if window_start > window_end:
            raise ValueError("candidate evidence window is reversed")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT DISTINCT arxiv_id, daily_list_date
                   FROM source_observations
                   WHERE category = ?
                     AND source IN ('atom', 'catchup')
                     AND daily_list_date BETWEEN ? AND ?
                   ORDER BY arxiv_id, daily_list_date""",
                (category, start_text, end_text),
            ).fetchall()
            return tuple(
                (row["arxiv_id"], _parse_date(row["daily_list_date"]))
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
        observations: tuple[SourceObservation, ...],
        raw_sha256: str,
        observed_at: datetime,
    ) -> tuple[ReviewEvent, ...]:
        _require_sha256(raw_sha256)
        if any(
            observation.source is not EvidenceSource.OAI
            or observation.category is not None
            for observation in observations
        ):
            raise ValueError("OAI pages require global OAI observations")
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
            if any(
                observation.arxiv_id not in metadata_by_id
                for observation in observations
            ):
                raise ValueError("OAI observation does not belong to the page")
            self._upsert_observations(connection, observations)
            event_ids = self._reconcile_affected_papers(
                connection,
                set(metadata_by_id),
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
        if result.source == "atom":
            # Atom observations are durable provenance; an Atom day is not a
            # daily-list coverage outcome in the confirmed model.
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO catchup_days(
                       category, daily_list_date, status, attempted_at,
                       response_sha256, error_code
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(category, daily_list_date) DO UPDATE SET
                       status = excluded.status,
                       attempted_at = excluded.attempted_at,
                       response_sha256 = excluded.response_sha256,
                       error_code = excluded.error_code""",
                (
                    result.category,
                    _date_text(result.mailing_date),
                    result.status.value,
                    _utc_text(result.fetched_at),
                    result.raw_sha256,
                    result.error_code,
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
    def _upsert_observations(
        connection: sqlite3.Connection,
        observations: tuple[SourceObservation, ...],
    ) -> None:
        for observation in observations:
            existing = connection.execute(
                """SELECT arxiv_id, source FROM source_observations
                   WHERE source_key = ?""",
                (observation.source_key,),
            ).fetchone()
            identity = (observation.arxiv_id, observation.source.value)
            if existing is not None and tuple(existing) != identity:
                raise ValueError("source observation key changed identity")
            connection.execute(
                """INSERT INTO source_observations(
                       source_key, arxiv_id, source, category, announce_type,
                       daily_list_date, announced_version, list_position,
                       oai_datestamp, response_sha256, observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                       category = excluded.category,
                       announce_type = excluded.announce_type,
                       daily_list_date = excluded.daily_list_date,
                       announced_version = excluded.announced_version,
                       list_position = excluded.list_position,
                       oai_datestamp = excluded.oai_datestamp,
                       response_sha256 = excluded.response_sha256,
                       observed_at = excluded.observed_at""",
                (
                    observation.source_key,
                    observation.arxiv_id,
                    observation.source.value,
                    observation.category,
                    (
                        None
                        if observation.announce_type is None
                        else observation.announce_type.value
                    ),
                    (
                        None
                        if observation.daily_list_date is None
                        else _date_text(observation.daily_list_date)
                    ),
                    observation.announced_version,
                    observation.list_position,
                    (
                        None
                        if observation.oai_datestamp is None
                        else _date_text(observation.oai_datestamp)
                    ),
                    observation.response_sha256,
                    _utc_text(observation.observed_at),
                ),
            )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> SourceObservation:
        return SourceObservation(
            source_key=row["source_key"],
            arxiv_id=row["arxiv_id"],
            source=EvidenceSource(row["source"]),
            category=row["category"],
            announce_type=(
                None
                if row["announce_type"] is None
                else AnnounceType(row["announce_type"])
            ),
            daily_list_date=(
                None
                if row["daily_list_date"] is None
                else _parse_date(row["daily_list_date"])
            ),
            announced_version=row["announced_version"],
            list_position=row["list_position"],
            oai_datestamp=(
                None
                if row["oai_datestamp"] is None
                else _parse_date(row["oai_datestamp"])
            ),
            response_sha256=row["response_sha256"],
            observed_at=_parse_utc(row["observed_at"]),
        )

    @staticmethod
    def _versions_from_connection(
        connection: sqlite3.Connection, arxiv_id: str
    ) -> tuple[PaperVersion, ...]:
        return tuple(
            PaperVersion(
                number=int(row["version"]),
                submitted_at=_parse_utc(row["submitted_at"]),
                size=row["size"],
                source_type=row["source_type"],
            )
            for row in connection.execute(
                """SELECT version, submitted_at, size, source_type
                   FROM article_versions WHERE arxiv_id = ?
                   ORDER BY version""",
                (arxiv_id,),
            )
        )

    @classmethod
    def _observations_from_connection(
        cls, connection: sqlite3.Connection, arxiv_id: str
    ) -> tuple[SourceObservation, ...]:
        return tuple(
            cls._observation_from_row(row)
            for row in connection.execute(
                """SELECT * FROM source_observations
                   WHERE arxiv_id = ? ORDER BY source_key""",
                (arxiv_id,),
            )
        )

    @staticmethod
    def _recompute_reconciliation_diagnostics(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM reconciliation_diagnostics")
        connection.execute(
            """INSERT INTO reconciliation_diagnostics(
                   diagnostic_code, occurrence_count, last_observed_at
               )
               SELECT c.conflict_code, COUNT(*), MAX(o.observed_at)
               FROM canonical_events AS c
               JOIN canonical_event_observations AS link
                 ON link.event_id = c.event_id
               JOIN source_observations AS o
                 ON o.observation_id = link.observation_id
               WHERE c.conflict_code = 'version_evidence_conflict'
               GROUP BY c.conflict_code"""
        )

    @classmethod
    def _assert_canonical_catchup_support(
        cls, connection: sqlite3.Connection
    ) -> None:
        missing = connection.execute(
            """SELECT c.event_id
               FROM canonical_events AS c
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM canonical_event_observations AS link
                   JOIN source_observations AS o
                     ON o.observation_id = link.observation_id
                   WHERE link.event_id = c.event_id
                     AND o.source = 'catchup'
                     AND o.daily_list_date = c.daily_list_date
               )
               LIMIT 1"""
        ).fetchone()
        if missing is not None:
            raise RuntimeError(
                "canonical event is missing exact-date catch-up support"
            )

    @classmethod
    def _apply_reconciliation_result(
        cls,
        connection: sqlite3.Connection,
        result: ReconciliationResult,
    ) -> tuple[int, ...]:
        existing = {
            _parse_date(row["daily_list_date"]): row
            for row in connection.execute(
                """SELECT * FROM canonical_events
                   WHERE arxiv_id = ? ORDER BY daily_list_date""",
                (result.arxiv_id,),
            )
        }
        desired_by_date = {
            event.daily_list_date: event for event in result.events
        }

        # A source correction can move a uniquely identified concrete
        # announcement. Preserve its event identity and reviewed state.
        stale_concrete: dict[int, list[sqlite3.Row]] = {}
        for old_date, row in existing.items():
            version = row["announced_version"]
            if old_date not in desired_by_date and version is not None:
                stale_concrete.setdefault(int(version), []).append(row)
        for daily_list_date, desired in desired_by_date.items():
            if daily_list_date in existing or desired.announced_version is None:
                continue
            candidates = stale_concrete.get(desired.announced_version, [])
            if len(candidates) != 1:
                continue
            row = candidates[0]
            old_date = _parse_date(row["daily_list_date"])
            connection.execute(
                """UPDATE canonical_events SET daily_list_date = ?
                   WHERE event_id = ?""",
                (_date_text(daily_list_date), row["event_id"]),
            )
            old_state = connection.execute(
                """SELECT * FROM review_date_state
                   WHERE daily_list_date = ?""",
                (_date_text(old_date),),
            ).fetchone()
            new_state = connection.execute(
                """SELECT * FROM review_date_state
                   WHERE daily_list_date = ?""",
                (_date_text(daily_list_date),),
            ).fetchone()
            if old_state is not None and new_state is None:
                connection.execute(
                    """UPDATE review_date_state SET daily_list_date = ?
                       WHERE daily_list_date = ?""",
                    (_date_text(daily_list_date), _date_text(old_date)),
                )
            elif old_state is not None and new_state is not None:
                anchor_event_id = (
                    new_state["anchor_event_id"]
                    if new_state["anchor_event_id"] is not None
                    else old_state["anchor_event_id"]
                )
                finished_values = tuple(
                    value
                    for value in (
                        old_state["last_finished_at"],
                        new_state["last_finished_at"],
                    )
                    if value is not None
                )
                revision_values = tuple(
                    int(value)
                    for value in (
                        old_state["last_finished_revision"],
                        new_state["last_finished_revision"],
                    )
                    if value is not None
                )
                connection.execute(
                    "DELETE FROM review_date_state WHERE daily_list_date = ?",
                    (_date_text(old_date),),
                )
                connection.execute(
                    """UPDATE review_date_state
                       SET anchor_event_id = ?, profile_revision = ?,
                           last_finished_at = ?, last_finished_revision = ?
                       WHERE daily_list_date = ?""",
                    (
                        anchor_event_id,
                        max(
                            int(old_state["profile_revision"]),
                            int(new_state["profile_revision"]),
                        ),
                        max(finished_values) if finished_values else None,
                        max(revision_values) if revision_values else None,
                        _date_text(daily_list_date),
                    ),
                )
            existing[daily_list_date] = connection.execute(
                "SELECT * FROM canonical_events WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()
            del existing[old_date]
            stale_concrete[desired.announced_version] = []

        event_ids: list[int] = []
        for daily_list_date in sorted(desired_by_date):
            desired = desired_by_date[daily_list_date]
            row = existing.get(daily_list_date)
            if row is None:
                queue_revision = cls._next_queue_revision(connection)
                finished = connection.execute(
                    """SELECT 1 FROM review_date_state
                       WHERE daily_list_date = ? AND last_finished_at IS NOT NULL""",
                    (_date_text(daily_list_date),),
                ).fetchone()
                cursor = connection.execute(
                    """INSERT INTO canonical_events(
                           arxiv_id, daily_list_date, announced_version,
                           version_resolution, queue_revision,
                           recovered_after_finish, conflict_code
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        desired.arxiv_id,
                        _date_text(daily_list_date),
                        desired.announced_version,
                        desired.version_resolution.value,
                        queue_revision,
                        int(finished is not None),
                        desired.conflict_code,
                    ),
                )
                event_id = int(cursor.lastrowid)
            else:
                event_id = int(row["event_id"])
                old_version = row["announced_version"]
                concrete_replacement = (
                    old_version is not None
                    and desired.announced_version is not None
                    and int(old_version) != desired.announced_version
                )
                if concrete_replacement:
                    queue_revision = cls._next_queue_revision(connection)
                    reviewed_at = None
                else:
                    queue_revision = int(row["queue_revision"])
                    reviewed_at = row["reviewed_at"]
                connection.execute(
                    """UPDATE canonical_events
                       SET announced_version = ?, version_resolution = ?,
                           queue_revision = ?, reviewed_at = ?, conflict_code = ?
                       WHERE event_id = ?""",
                    (
                        desired.announced_version,
                        desired.version_resolution.value,
                        queue_revision,
                        reviewed_at,
                        desired.conflict_code,
                        event_id,
                    ),
                )

            connection.execute(
                "DELETE FROM canonical_event_observations WHERE event_id = ?",
                (event_id,),
            )
            observation_rows = connection.execute(
                """SELECT observation_id, source, daily_list_date
                   FROM source_observations
                   WHERE source_key IN ({})""".format(
                    ",".join("?" for _ in desired.observation_keys)
                ),
                desired.observation_keys,
            ).fetchall()
            if len(observation_rows) != len(desired.observation_keys):
                raise RuntimeError("reconciler returned an unknown observation")
            if not any(
                value["source"] == EvidenceSource.CATCHUP.value
                and value["daily_list_date"] == _date_text(daily_list_date)
                for value in observation_rows
            ):
                raise RuntimeError(
                    "reconciler returned an event without exact catch-up support"
                )
            connection.executemany(
                """INSERT INTO canonical_event_observations(
                       event_id, observation_id
                   ) VALUES (?, ?)""",
                (
                    (event_id, int(value["observation_id"]))
                    for value in observation_rows
                ),
            )
            event_ids.append(event_id)

        desired_dates = set(desired_by_date)
        for daily_list_date, row in existing.items():
            if daily_list_date in desired_dates:
                continue
            cls._next_queue_revision(connection)
            connection.execute(
                "DELETE FROM canonical_events WHERE event_id = ?",
                (row["event_id"],),
            )
        return tuple(event_ids)

    @classmethod
    def _reconcile_affected_papers(
        cls,
        connection: sqlite3.Connection,
        arxiv_ids: set[str],
    ) -> tuple[int, ...]:
        event_ids: list[int] = []
        for arxiv_id in sorted(arxiv_ids):
            result = reconcile_paper(
                arxiv_id,
                cls._observations_from_connection(connection, arxiv_id),
                cls._versions_from_connection(connection, arxiv_id),
            )
            event_ids.extend(
                cls._apply_reconciliation_result(connection, result)
            )
        cls._recompute_reconciliation_diagnostics(connection)
        cls._assert_canonical_catchup_support(connection)
        return tuple(event_ids)

    def apply_atom_batch(
        self,
        batch: AtomBatch,
        observations: tuple[SourceObservation, ...],
    ) -> tuple[ReviewEvent, ...]:
        if any(
            observation.source is not EvidenceSource.ATOM
            for observation in observations
        ):
            raise ValueError("Atom batches require Atom observations")
        metadata_by_id = {
            entry.metadata.arxiv_id: entry.metadata for entry in batch.entries
        }
        if any(
            observation.arxiv_id not in metadata_by_id
            or observation.category != batch.category
            for observation in observations
        ):
            raise ValueError("Atom observation does not belong to the batch")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for arxiv_id in sorted(metadata_by_id):
                self._upsert_article(connection, metadata_by_id[arxiv_id])
            # Atom's published timestamp is a feed timestamp, not an arXiv
            # submission timestamp. OAI remains authoritative for versions.
            self._upsert_observations(connection, observations)
            event_ids = self._reconcile_affected_papers(
                connection, set(metadata_by_id)
            )
            events = tuple(
                self._event_from_connection(connection, event_id)
                for event_id in event_ids
            )
            connection.commit()
            return events
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_catchup_day(
        self,
        result: CatchupDay,
        observations: tuple[SourceObservation, ...],
        attempted_at: datetime,
    ) -> tuple[ReviewEvent, ...]:
        attempted_text = _utc_text(attempted_at)
        day_text = _date_text(result.mailing_date)
        if any(
            page.category != result.category
            or page.mailing_date != result.mailing_date
            for page in result.pages
        ):
            raise ValueError("catch-up page does not belong to its day")
        if result.status is EnrichmentStatus.FAILED:
            if observations:
                raise ValueError("failed catch-up day cannot contain observations")
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO catchup_days(
                           category, daily_list_date, status, attempted_at,
                           response_sha256, error_code
                       ) VALUES (?, ?, 'failed', ?, NULL, ?)
                       ON CONFLICT(category, daily_list_date) DO UPDATE SET
                           status = excluded.status,
                           attempted_at = excluded.attempted_at,
                           response_sha256 = NULL,
                           error_code = excluded.error_code""",
                    (
                        result.category,
                        day_text,
                        attempted_text,
                        result.error_code or "catchup_failed",
                    ),
                )
                connection.commit()
                return ()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        entries = tuple(
            entry for page in result.pages for entry in page.entries
        )
        expected = Counter(
            (
                entry.metadata.arxiv_id,
                entry.section,
                entry.mailing_date,
                entry.position,
            )
            for entry in entries
        )
        actual = Counter(
            (
                observation.arxiv_id,
                observation.announce_type,
                observation.daily_list_date,
                observation.list_position,
            )
            for observation in observations
        )
        if expected != actual or any(
            observation.source is not EvidenceSource.CATCHUP
            or observation.category != result.category
            for observation in observations
        ):
            raise ValueError(
                "catch-up observations do not match the complete parsed day"
            )
        metadata_by_id: dict[str, PaperMetadata] = {}
        for entry in entries:
            existing = metadata_by_id.get(entry.metadata.arxiv_id)
            if existing is not None and existing != entry.metadata:
                raise ValueError("catch-up day contains conflicting metadata")
            metadata_by_id[entry.metadata.arxiv_id] = entry.metadata
        response_sha256: str | None
        page_hashes = tuple(page.raw_sha256 for page in result.pages)
        if len(page_hashes) == 1:
            response_sha256 = page_hashes[0]
        elif page_hashes:
            response_sha256 = hashlib.sha256(
                "\n".join(page_hashes).encode("ascii")
            ).hexdigest()
        else:
            response_sha256 = None
        stored_status = (
            "empty"
            if result.status is EnrichmentStatus.EMPTY or not observations
            else "complete"
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior_rows = connection.execute(
                """SELECT observation_id, source_key, arxiv_id
                   FROM source_observations
                   WHERE source = 'catchup' AND category = ?
                     AND daily_list_date = ?""",
                (result.category, day_text),
            ).fetchall()
            incoming_keys = {item.source_key for item in observations}
            stale_rows = [
                row for row in prior_rows if row["source_key"] not in incoming_keys
            ]
            affected_ids = {
                row["arxiv_id"] for row in prior_rows
            } | {item.arxiv_id for item in observations}
            if stale_rows:
                stale_ids = tuple(int(row["observation_id"]) for row in stale_rows)
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    "DELETE FROM canonical_event_observations "
                    f"WHERE observation_id IN ({placeholders})",
                    stale_ids,
                )
                connection.execute(
                    "DELETE FROM source_observations "
                    f"WHERE observation_id IN ({placeholders})",
                    stale_ids,
                )
            for arxiv_id in sorted(metadata_by_id):
                self._upsert_article(connection, metadata_by_id[arxiv_id])
            self._upsert_observations(connection, observations)
            connection.execute(
                """INSERT INTO catchup_days(
                       category, daily_list_date, status, attempted_at,
                       response_sha256, error_code
                   ) VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(category, daily_list_date) DO UPDATE SET
                       status = excluded.status,
                       attempted_at = excluded.attempted_at,
                       response_sha256 = excluded.response_sha256,
                       error_code = NULL""",
                (
                    result.category,
                    day_text,
                    stored_status,
                    attempted_text,
                    response_sha256,
                ),
            )
            event_ids = self._reconcile_affected_papers(
                connection, affected_ids
            )
            events = tuple(
                self._event_from_connection(connection, event_id)
                for event_id in event_ids
            )
            connection.commit()
            return events
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def canonical_event_count(self) -> int:
        connection = self._connect()
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_events"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def source_observations(
        self, arxiv_id: str | None = None
    ) -> tuple[SourceObservation, ...]:
        connection = self._connect()
        try:
            if arxiv_id is None:
                rows = connection.execute(
                    """SELECT * FROM source_observations
                       ORDER BY source_key"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM source_observations
                       WHERE arxiv_id = ? ORDER BY source_key""",
                    (arxiv_id,),
                ).fetchall()
            return tuple(self._observation_from_row(row) for row in rows)
        finally:
            connection.close()

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
    def _event_from_connection(
        connection: sqlite3.Connection, event_id: int
    ) -> ReviewEvent:
        row = connection.execute(
            "SELECT * FROM canonical_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        observation_rows = connection.execute(
            """SELECT o.*
               FROM source_observations AS o
               JOIN canonical_event_observations AS link
                 ON link.observation_id = o.observation_id
               WHERE link.event_id = ?
               ORDER BY o.source_key""",
            (event_id,),
        ).fetchall()
        return ReviewEvent(
            event_id=int(row["event_id"]),
            arxiv_id=row["arxiv_id"],
            daily_list_date=_parse_date(row["daily_list_date"]),
            announced_version=row["announced_version"],
            version_resolution=VersionResolution(row["version_resolution"]),
            observations=tuple(
                Store._observation_from_row(value)
                for value in observation_rows
            ),
            queue_revision=int(row["queue_revision"]),
            reviewed_at=(
                None
                if row["reviewed_at"] is None
                else _parse_utc(row["reviewed_at"])
            ),
            recovered_after_finish=bool(row["recovered_after_finish"]),
            conflict_code=row["conflict_code"],
        )

    @staticmethod
    def _project_event(
        event: ReviewEvent,
        active_configs: tuple[CategoryConfig, ...] | None,
    ) -> ReviewEvent | None:
        if active_configs is None:
            return event
        if any(
            not isinstance(config, CategoryConfig)
            for config in active_configs
        ):
            raise TypeError("active configs must be CategoryConfig values")
        coverage = {config.category: config.coverage_start for config in active_configs}
        if len(coverage) != len(active_configs):
            raise ValueError("active category configurations must be unique")
        active_observations = tuple(
            observation
            for observation in event.observations
            if observation.source is EvidenceSource.CATCHUP
            and observation.category in coverage
            and observation.daily_list_date == event.daily_list_date
            and event.daily_list_date >= coverage[observation.category]
        )
        if not active_observations:
            return None
        return replace(event, observations=active_observations)

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

    def apply_article_snapshot(
        self,
        metadata: PaperMetadata,
        versions: tuple[PaperVersion, ...],
    ) -> tuple[ReviewEvent, ...]:
        """Store current article data and reconcile existing observations."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_article(connection, metadata)
            self._upsert_versions(connection, metadata.arxiv_id, versions)
            event_ids = self._reconcile_affected_papers(
                connection, {metadata.arxiv_id}
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

    def review_queue_revision(self) -> int:
        connection = self._connect()
        try:
            return int(
                connection.execute(
                    "SELECT queue_revision FROM state_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def review_revisions(self) -> tuple[int, int]:
        """Return the queue and active-projection revisions together."""

        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT queue_revision, projection_revision
                   FROM state_meta WHERE singleton = 1"""
            ).fetchone()
            return int(row["queue_revision"]), int(row["projection_revision"])
        finally:
            connection.close()

    @staticmethod
    def _current_profile_projection_revisions(
        connection: sqlite3.Connection,
    ) -> tuple[int, int]:
        profile_row = connection.execute(
            """SELECT pending_revision FROM profile_publication
               WHERE singleton = 1 AND status = 'published'"""
        ).fetchone()
        profile_revision = (
            0
            if profile_row is None or profile_row[0] is None
            else int(profile_row[0])
        )
        projection_revision = int(
            connection.execute(
                """SELECT projection_revision FROM state_meta
                   WHERE singleton = 1"""
            ).fetchone()[0]
        )
        return profile_revision, projection_revision

    def review_snapshot(
        self,
        day: date,
        *,
        seed_ids: tuple[str, ...] = (),
        through_revision: int | None = None,
        active_configs: tuple[CategoryConfig, ...] | None = None,
        profile_revision: int | None = None,
    ) -> StoreReviewSnapshot:
        day_text = _date_text(day)
        if not isinstance(seed_ids, tuple) or any(
            not isinstance(value, str) for value in seed_ids
        ):
            raise TypeError("seed IDs must be a tuple of strings")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            current_revision = int(
                connection.execute(
                    "SELECT queue_revision FROM state_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            if through_revision is None:
                snapshot_revision = current_revision
            elif type(through_revision) is not int or through_revision < 0:
                raise ValueError("snapshot revision must be nonnegative")
            elif through_revision > current_revision:
                raise ValueError("snapshot revision is from the future")
            else:
                snapshot_revision = through_revision
            event_rows = connection.execute(
                """SELECT event_id FROM canonical_events
                   WHERE daily_list_date = ? AND queue_revision <= ?
                   ORDER BY queue_revision, event_id""",
                (day_text, snapshot_revision),
            ).fetchall()
            events = tuple(
                projected
                for row in event_rows
                if (
                    projected := self._project_event(
                        self._event_from_connection(connection, int(row[0])),
                        active_configs,
                    )
                )
                is not None
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
                   FROM review_date_state WHERE daily_list_date = ?""",
                (day_text,),
            ).fetchone()
            stored_profile_revision, projection_revision = (
                self._current_profile_projection_revisions(connection)
            )
            if (
                profile_revision is not None
                and stored_profile_revision not in {0, profile_revision}
            ):
                raise ReviewSnapshotConflict(
                    "active profile changed; reopen this Review date"
                )
            effective_profile_revision = (
                stored_profile_revision
                if profile_revision is None
                else profile_revision
            )
            active_event_ids = {event.event_id for event in events}
            anchor_event_id = None
            if (
                state is not None
                and state[0] in active_event_ids
                and int(state[1]) == effective_profile_revision
            ):
                anchor_event_id = int(state[0])
            connection.commit()
            return StoreReviewSnapshot(
                day=day,
                snapshot_revision=snapshot_revision,
                events=events,
                anchor_event_id=anchor_event_id,
                profile_revision=effective_profile_revision,
                projection_revision=projection_revision,
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

    def list_review_dates(
        self,
        *,
        through_revision: int | None = None,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> tuple[date, ...]:
        if through_revision is not None and (
            type(through_revision) is not int or through_revision < 0
        ):
            raise ValueError("snapshot revision must be nonnegative")
        connection = self._connect()
        try:
            if through_revision is None:
                rows = connection.execute(
                    """SELECT event_id, daily_list_date
                       FROM canonical_events
                       ORDER BY daily_list_date, queue_revision, event_id"""
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT event_id, daily_list_date
                       FROM canonical_events
                       WHERE queue_revision <= ?
                       ORDER BY daily_list_date, queue_revision, event_id""",
                    (through_revision,),
                ).fetchall()
            dates: dict[date, None] = {}
            for row in rows:
                event = self._event_from_connection(
                    connection, int(row["event_id"])
                )
                if self._project_event(event, active_configs) is not None:
                    dates.setdefault(_parse_date(row["daily_list_date"]), None)
            return tuple(dates)
        finally:
            connection.close()

    def review_date_links(
        self,
        day: date,
        *,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> ReviewDateLinks:
        dates = self.list_review_dates(active_configs=active_configs)
        previous = tuple(value for value in dates if value < day)
        following = tuple(value for value in dates if value > day)
        return ReviewDateLinks(
            previous_date=previous[-1] if previous else None,
            next_date=following[0] if following else None,
        )

    def record_position(
        self,
        day: date,
        snapshot_revision: int,
        anchor_event_id: int,
        profile_revision: int,
        projection_revision: int | None = None,
        *,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> ReviewPosition:
        if (
            snapshot_revision < 0
            or profile_revision < 0
            or (projection_revision is not None and projection_revision < 0)
        ):
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
            current_profile, current_projection = (
                self._current_profile_projection_revisions(connection)
            )
            if (
                projection_revision is not None
                and (
                    (
                        current_profile != 0
                        and profile_revision != current_profile
                    )
                    or projection_revision != current_projection
                )
            ):
                raise ReviewSnapshotConflict(
                    "active profile changed; reopen this Review date"
                )
            anchor_row = connection.execute(
                """SELECT event_id FROM canonical_events
                   WHERE event_id = ? AND daily_list_date = ?
                     AND queue_revision <= ?""",
                (anchor_event_id, day_text, snapshot_revision),
            ).fetchone()
            anchor = (
                None
                if anchor_row is None
                else self._project_event(
                    self._event_from_connection(connection, anchor_event_id),
                    active_configs,
                )
            )
            if anchor is None:
                raise ValueError("anchor event is not in the review snapshot")
            connection.execute(
                """INSERT INTO review_date_state(
                       daily_list_date, anchor_event_id, profile_revision
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(daily_list_date) DO UPDATE SET
                       anchor_event_id = excluded.anchor_event_id,
                       profile_revision = excluded.profile_revision""",
                (day_text, anchor_event_id, profile_revision),
            )
            connection.commit()
            return ReviewPosition(
                day,
                snapshot_revision,
                anchor_event_id,
                profile_revision,
                current_projection,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def events_for_date(
        self,
        day: date,
        *,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> tuple[ReviewEvent, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT event_id FROM canonical_events
                   WHERE daily_list_date = ?
                   ORDER BY queue_revision, event_id""",
                (day.isoformat(),),
            ).fetchall()
            return tuple(
                projected
                for row in rows
                if (
                    projected := self._project_event(
                        self._event_from_connection(connection, int(row[0])),
                        active_configs,
                    )
                )
                is not None
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
                              (SELECT MIN(submitted_at) FROM article_versions
                               WHERE arxiv_id = a.arxiv_id) AS first_submitted_at,
                              bm25(papers_fts, 10.0, 6.0, 5.0, 1.0) AS rank
                       FROM papers_fts
                       JOIN articles AS a ON a.rowid = papers_fts.rowid
                       JOIN saved_papers AS s ON s.arxiv_id = a.arxiv_id
                       WHERE papers_fts MATCH ?
                       ORDER BY rank, first_submitted_at DESC, a.arxiv_id DESC
                       LIMIT ? OFFSET ?""",
                    (match_query, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT a.arxiv_id, s.saved_version, a.is_deleted,
                              MAX(v.version) AS latest_version,
                              MIN(v.submitted_at) AS first_submitted_at
                       FROM saved_papers AS s
                       JOIN articles AS a ON a.arxiv_id = s.arxiv_id
                       LEFT JOIN article_versions AS v
                           ON v.arxiv_id = a.arxiv_id
                       GROUP BY a.arxiv_id
                       ORDER BY first_submitted_at DESC, a.arxiv_id DESC
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
        profile_revision: int | None = None,
        projection_revision: int | None = None,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> FinishResult:
        if through_revision < 0:
            raise ValueError("through_revision must be nonnegative")
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("finished_at must be aware UTC")
        if finished_at.utcoffset().total_seconds() != 0:
            raise ValueError("finished_at must be aware UTC")
        if (profile_revision is None) != (projection_revision is None):
            raise ValueError(
                "profile and projection revisions must be supplied together"
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(
                connection.execute(
                    """SELECT queue_revision FROM state_meta
                       WHERE singleton = 1"""
                ).fetchone()[0]
            )
            if through_revision > current_revision:
                raise ValueError("snapshot revision is from the future")
            current_profile, current_projection = (
                self._current_profile_projection_revisions(connection)
            )
            if profile_revision is not None and (
                (
                    current_profile != 0
                    and profile_revision != current_profile
                )
                or projection_revision != current_projection
            ):
                raise ReviewSnapshotConflict(
                    "active profile changed; reopen this Review date"
                )
            rows = connection.execute(
                """SELECT event_id FROM canonical_events
                   WHERE daily_list_date = ? AND queue_revision <= ?
                   ORDER BY event_id""",
                (_date_text(day), through_revision),
            ).fetchall()
            event_ids = tuple(
                int(row["event_id"])
                for row in rows
                if self._project_event(
                    self._event_from_connection(
                        connection, int(row["event_id"])
                    ),
                    active_configs,
                )
                is not None
            )
            reviewed_count = 0
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                cursor = connection.execute(
                    "UPDATE canonical_events SET reviewed_at = ? "
                    f"WHERE event_id IN ({placeholders}) "
                    "AND reviewed_at IS NULL",
                    (_utc_text(finished_at), *event_ids),
                )
                reviewed_count = cursor.rowcount
            connection.execute(
                """INSERT INTO review_date_state(
                       daily_list_date, profile_revision, last_finished_at,
                       last_finished_revision
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(daily_list_date) DO UPDATE SET
                       profile_revision = excluded.profile_revision,
                       last_finished_at = excluded.last_finished_at,
                       last_finished_revision = MAX(
                           COALESCE(review_date_state.last_finished_revision, 0),
                           excluded.last_finished_revision
                       )""",
                (
                    day.isoformat(),
                    current_profile if profile_revision is None else profile_revision,
                    _utc_text(finished_at),
                    through_revision,
                ),
            )
            connection.commit()
            return FinishResult(reviewed_count, through_revision)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_all(
        self,
        *,
        through_revision: int,
        finished_at: datetime,
        profile_revision: int | None = None,
        projection_revision: int | None = None,
        active_configs: tuple[CategoryConfig, ...] | None = None,
    ) -> FinishResult:
        if type(through_revision) is not int or through_revision < 0:
            raise ValueError("through_revision must be nonnegative")
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("finished_at must be aware UTC")
        if finished_at.utcoffset().total_seconds() != 0:
            raise ValueError("finished_at must be aware UTC")
        if (profile_revision is None) != (projection_revision is None):
            raise ValueError(
                "profile and projection revisions must be supplied together"
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_revision = int(
                connection.execute(
                    "SELECT queue_revision FROM state_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            if through_revision > current_revision:
                raise ValueError("snapshot revision is from the future")
            current_profile, current_projection = (
                self._current_profile_projection_revisions(connection)
            )
            if profile_revision is not None and (
                (
                    current_profile != 0
                    and profile_revision != current_profile
                )
                or projection_revision != current_projection
            ):
                raise ReviewSnapshotConflict(
                    "active profile changed; reopen Review"
                )
            rows = connection.execute(
                """SELECT event_id, daily_list_date FROM canonical_events
                   WHERE queue_revision <= ?
                   ORDER BY daily_list_date, event_id""",
                (through_revision,),
            ).fetchall()
            selected = tuple(
                (int(row["event_id"]), row["daily_list_date"])
                for row in rows
                if self._project_event(
                    self._event_from_connection(
                        connection, int(row["event_id"])
                    ),
                    active_configs,
                )
                is not None
            )
            reviewed_count = 0
            if selected:
                event_ids = tuple(event_id for event_id, _day in selected)
                placeholders = ",".join("?" for _ in event_ids)
                cursor = connection.execute(
                    "UPDATE canonical_events SET reviewed_at = ? "
                    f"WHERE event_id IN ({placeholders}) "
                    "AND reviewed_at IS NULL",
                    (_utc_text(finished_at), *event_ids),
                )
                reviewed_count = cursor.rowcount
                finished_profile = (
                    current_profile
                    if profile_revision is None
                    else profile_revision
                )
                connection.executemany(
                    """INSERT INTO review_date_state(
                           daily_list_date, profile_revision,
                           last_finished_at, last_finished_revision
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(daily_list_date) DO UPDATE SET
                           profile_revision = excluded.profile_revision,
                           last_finished_at = excluded.last_finished_at,
                           last_finished_revision = MAX(
                               COALESCE(
                                   review_date_state.last_finished_revision, 0
                               ),
                               excluded.last_finished_revision
                           )""",
                    (
                        (
                            daily_list_date,
                            finished_profile,
                            _utc_text(finished_at),
                            through_revision,
                        )
                        for daily_list_date in dict.fromkeys(
                            day for _event_id, day in selected
                        )
                    ),
                )
            connection.commit()
            return FinishResult(reviewed_count, through_revision)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
