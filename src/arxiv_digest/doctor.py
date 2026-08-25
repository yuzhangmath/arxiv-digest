"""Strictly redacted, read-only diagnostics."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from arxiv_digest.paths import AppPaths
from arxiv_digest.profile import decode_profile
from arxiv_digest.sources.oai import DURABLE_PROTOCOL_ERROR_CODES


_SAFE_SYNC_ERROR_CODES = frozenset(
    {
        "cancelled",
        "catchup_fetch_failed",
        "catchup_layout_changed",
        "interrupted",
        "sync_error",
        "version_evidence_conflict",
    }
) | DURABLE_PROTOCOL_ERROR_CODES


def _redacted_sync_error_code(value: object) -> str:
    if isinstance(value, str) and value in _SAFE_SYNC_ERROR_CODES:
        return value
    return "sync_error"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    application_version: str
    initialized: bool
    profile_status: str
    database_status: str
    application_generation: int | None
    schema_version: int | None
    profile_revision: int | None
    projection_revision: int | None
    active_category_count: int
    metadata_checkpoint_count: int
    daily_list_target_count: int
    daily_list_checked_count: int
    daily_list_with_papers_count: int
    daily_list_empty_count: int
    daily_list_failed_count: int
    daily_list_pending_count: int
    daily_list_unavailable_count: int
    daily_list_gap_count: int
    canonical_event_count: int
    atom_confirmed_count: int
    chronology_matched_count: int
    unconfirmed_count: int
    saved_paper_count: int
    downloaded_pdf_count: int
    candidate_cache_status: str
    candidate_cache_file_count: int
    maintenance_state: str
    sync_error_codes: tuple[str, ...]


def _readonly_database(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _aggregate_day_status(
    statuses: list[str],
) -> str:
    if "failed" in statuses:
        return "failed"
    if "pending" in statuses:
        return "pending"
    if "complete" in statuses:
        return "complete"
    return "empty"


def _candidate_cache_state(paths: AppPaths) -> tuple[str, int]:
    root = paths.cache_dir / "candidate-corpus"
    try:
        info = root.lstat()
    except OSError:
        return "missing", 0
    if not info or root.is_symlink() or not root.is_dir():
        return "invalid", 0
    count = 0
    try:
        for walk_root, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            directory_names[:] = [
                name
                for name in directory_names
                if not (Path(walk_root) / name).is_symlink()
            ]
            count += sum(
                (Path(walk_root) / name).is_file()
                and not (Path(walk_root) / name).is_symlink()
                for name in file_names
            )
    except OSError:
        return "invalid", 0
    return ("ready" if count else "empty"), count


def _maintenance_state(paths: AppPaths) -> str:
    try:
        info = paths.restore_journal_path.lstat()
    except FileNotFoundError:
        return "idle"
    except OSError:
        return "invalid"
    return "recovery_pending" if info and paths.restore_journal_path.is_file() and not paths.restore_journal_path.is_symlink() else "invalid"


def inspect_doctor(
    paths: AppPaths,
    *,
    platform: str | None = None,
    application_version: str = "0.2.0",
    today: date | None = None,
) -> DoctorReport:
    del platform
    observed_date = today or datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).date()
    coverage_min = observed_date - timedelta(days=89)
    profile = None
    profile_status = "missing"
    if paths.profile_path.is_file() and not paths.profile_path.is_symlink():
        try:
            profile = decode_profile(paths.profile_path.read_bytes())
            profile_status = "ok"
        except (OSError, ValueError):
            profile_status = "invalid"

    active_coverage = (
        {}
        if profile is None
        else {
            item.category: item.coverage_start
            for item in profile.category_coverage
        }
    )
    application_generation: int | None = None
    schema_version: int | None = None
    projection_revision: int | None = None
    checkpoint_count = 0
    daily_counts = {
        "complete": 0,
        "empty": 0,
        "failed": 0,
        "pending": 0,
        "unavailable": 0,
    }
    resolution_counts = {
        "atom_confirmed": 0,
        "chronology_matched": 0,
        "unconfirmed": 0,
    }
    saved_count = 0
    downloaded_pdf_count = 0
    error_codes: tuple[str, ...] = ()
    database_status = "missing"
    if paths.database_path.is_file() and not paths.database_path.is_symlink():
        try:
            connection = _readonly_database(paths.database_path)
            try:
                schema_version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                application_generation = int(
                    connection.execute(
                        "SELECT generation FROM application_generation WHERE singleton = 1"
                    ).fetchone()[0]
                )
                projection_revision = int(
                    connection.execute(
                        "SELECT projection_revision FROM state_meta WHERE singleton = 1"
                    ).fetchone()[0]
                )
                category_rows = connection.execute(
                    """SELECT category, completed_through_utc, last_error_code
                       FROM category_sync_state"""
                ).fetchall()
                checkpoint_count = sum(
                    row["category"] in active_coverage
                    and row["completed_through_utc"] is not None
                    for row in category_rows
                )
                raw_codes = {
                    row["last_error_code"]
                    for row in category_rows
                    if row["category"] in active_coverage
                    and row["last_error_code"] is not None
                }
                statuses_by_category: dict[str, dict[date, str]] = {}
                for row in connection.execute(
                    """SELECT category, daily_list_date, status, error_code
                       FROM catchup_days"""
                ):
                    category = row["category"]
                    if category not in active_coverage:
                        continue
                    day = date.fromisoformat(row["daily_list_date"])
                    if day < active_coverage[category]:
                        continue
                    statuses_by_category.setdefault(category, {})[day] = row[
                        "status"
                    ]
                    if row["error_code"] is not None:
                        raw_codes.add(row["error_code"])
                target_days = sorted(
                    {
                        day
                        for statuses in statuses_by_category.values()
                        for day in statuses
                    }
                )
                for day in target_days:
                    statuses = [
                        statuses_by_category.get(category, {}).get(
                            day, "pending"
                        )
                        for category, start in active_coverage.items()
                        if start <= day
                    ]
                    if statuses:
                        daily_counts[_aggregate_day_status(statuses)] += 1
                        if day < coverage_min and any(
                            status in {"failed", "pending"}
                            for status in statuses
                        ):
                            daily_counts["unavailable"] += 1
                for row in connection.execute(
                    """SELECT version_resolution, COUNT(*) AS aggregate_count
                       FROM canonical_events GROUP BY version_resolution"""
                ):
                    resolution_counts[row["version_resolution"]] = int(
                        row["aggregate_count"]
                    )
                saved_count = int(
                    connection.execute(
                        "SELECT count(*) FROM saved_papers"
                    ).fetchone()[0]
                )
                downloaded_pdf_count = int(
                    connection.execute(
                        "SELECT count(*) FROM download_files"
                    ).fetchone()[0]
                )
                raw_codes.update(
                    row[0]
                    for row in connection.execute(
                        """SELECT diagnostic_code
                           FROM reconciliation_diagnostics
                           WHERE occurrence_count > 0"""
                    )
                )
                error_codes = tuple(
                    sorted(
                        {_redacted_sync_error_code(code) for code in raw_codes}
                    )
                )
                database_status = "ok"
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            database_status = "invalid"

    cache_status, cache_file_count = _candidate_cache_state(paths)
    target_count = sum(
        daily_counts[status]
        for status in ("complete", "empty", "failed", "pending")
    )
    checked_count = (
        daily_counts["complete"]
        + daily_counts["empty"]
        + daily_counts["failed"]
    )
    return DoctorReport(
        application_version=application_version,
        initialized=profile_status == "ok" and database_status == "ok",
        profile_status=profile_status,
        database_status=database_status,
        application_generation=application_generation,
        schema_version=schema_version,
        profile_revision=None if profile is None else profile.revision,
        projection_revision=projection_revision,
        active_category_count=len(active_coverage),
        metadata_checkpoint_count=checkpoint_count,
        daily_list_target_count=target_count,
        daily_list_checked_count=checked_count,
        daily_list_with_papers_count=daily_counts["complete"],
        daily_list_empty_count=daily_counts["empty"],
        daily_list_failed_count=daily_counts["failed"],
        daily_list_pending_count=daily_counts["pending"],
        daily_list_unavailable_count=daily_counts["unavailable"],
        daily_list_gap_count=(daily_counts["failed"] + daily_counts["pending"]),
        canonical_event_count=sum(resolution_counts.values()),
        atom_confirmed_count=resolution_counts["atom_confirmed"],
        chronology_matched_count=resolution_counts["chronology_matched"],
        unconfirmed_count=resolution_counts["unconfirmed"],
        saved_paper_count=saved_count,
        downloaded_pdf_count=downloaded_pdf_count,
        candidate_cache_status=cache_status,
        candidate_cache_file_count=cache_file_count,
        maintenance_state=_maintenance_state(paths),
        sync_error_codes=error_codes,
    )


def render_doctor(report: DoctorReport) -> str:
    lines = [f"arXiv Digest {report.application_version}"]
    if not report.initialized:
        lines.extend(
            (
                "Status: not initialized",
                f"Profile: {report.profile_status}",
                f"Database: {report.database_status}",
                f"Candidate cache: {report.candidate_cache_status}",
                f"Maintenance: {report.maintenance_state}",
                "Next step: run arxiv-digest init.",
            )
        )
        return "\n".join(lines) + "\n"
    lines.extend(
        (
            "Status: initialized",
            f"Application generation: {report.application_generation}",
            f"Database schema: {report.schema_version}",
            f"Profile revision: {report.profile_revision}",
            f"Projection revision: {report.projection_revision}",
            f"Active categories: {report.active_category_count}",
            f"Metadata checkpoints: {report.metadata_checkpoint_count}",
            f"Daily-list target dates: {report.daily_list_target_count}",
            f"Daily-list checked dates: {report.daily_list_checked_count}",
            f"Daily-list dates with papers: {report.daily_list_with_papers_count}",
            f"Daily-list empty dates: {report.daily_list_empty_count}",
            f"Daily-list failed dates: {report.daily_list_failed_count}",
            f"Daily-list pending dates: {report.daily_list_pending_count}",
            f"Daily-list unavailable dates: {report.daily_list_unavailable_count}",
            f"Daily-list coverage gaps: {report.daily_list_gap_count}",
            f"Canonical events: {report.canonical_event_count}",
            f"Atom-confirmed events: {report.atom_confirmed_count}",
            f"Chronology-matched events: {report.chronology_matched_count}",
            f"Unconfirmed events: {report.unconfirmed_count}",
            f"Saved papers: {report.saved_paper_count}",
            f"Downloaded PDFs present: {report.downloaded_pdf_count}",
            f"Candidate cache: {report.candidate_cache_status}",
            f"Candidate cache files: {report.candidate_cache_file_count}",
            f"Maintenance: {report.maintenance_state}",
            "Synchronization error codes: "
            + (", ".join(report.sync_error_codes) or "none"),
        )
    )
    return "\n".join(lines) + "\n"
