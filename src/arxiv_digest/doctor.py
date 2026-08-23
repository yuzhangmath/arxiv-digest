"""Strictly redacted, read-only diagnostics."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

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
    }
) | DURABLE_PROTOCOL_ERROR_CODES


def _redacted_sync_error_code(value: object) -> str:
    if isinstance(value, str) and value in _SAFE_SYNC_ERROR_CODES:
        return value
    return "sync_error"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    platform: str
    application_version: str
    initialized: bool
    profile_status: str
    database_status: str
    schema_version: int | None
    category_count: int
    checkpoint_count: int
    saved_paper_count: int
    destination_kind: str | None
    destination_writable: bool | None
    preference_counts: tuple[int, int, int, int]
    sync_error_codes: tuple[str, ...]


def _readonly_database(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def inspect_doctor(
    paths: AppPaths,
    *,
    platform: str | None = None,
    application_version: str = "0.1.0",
) -> DoctorReport:
    platform_name = platform or sys.platform
    profile = None
    profile_status = "missing"
    if paths.profile_path.is_file() and not paths.profile_path.is_symlink():
        try:
            profile = decode_profile(paths.profile_path.read_bytes())
            profile_status = "ok"
        except (OSError, ValueError):
            profile_status = "invalid"

    schema_version: int | None = None
    category_count = 0
    checkpoint_count = 0
    saved_count = 0
    error_codes: tuple[str, ...] = ()
    database_status = "missing"
    if paths.database_path.is_file() and not paths.database_path.is_symlink():
        try:
            connection = _readonly_database(paths.database_path)
            try:
                schema_version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                category_count = int(
                    connection.execute(
                        "SELECT count(*) FROM category_sync_state"
                    ).fetchone()[0]
                )
                checkpoint_count = int(
                    connection.execute(
                        """SELECT count(*) FROM category_sync_state
                           WHERE completed_through_utc IS NOT NULL"""
                    ).fetchone()[0]
                )
                saved_count = int(
                    connection.execute(
                        "SELECT count(*) FROM saved_papers"
                    ).fetchone()[0]
                )
                raw_error_codes = connection.execute(
                    """SELECT DISTINCT last_error_code
                       FROM category_sync_state
                       WHERE last_error_code IS NOT NULL
                       ORDER BY last_error_code"""
                )
                error_codes = tuple(
                    sorted(
                        {_redacted_sync_error_code(row[0]) for row in raw_error_codes}
                    )
                )
                database_status = "ok"
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            database_status = "invalid"

    destination_kind = None if profile is None else profile.pdf_destination.kind
    destination_writable = (
        None
        if profile is None
        else profile.pdf_destination.path.is_dir()
        and os.access(profile.pdf_destination.path, os.W_OK)
    )
    preference_counts = (
        (0, 0, 0, 0)
        if profile is None
        else (
            len(profile.keywords),
            len(profile.phrases),
            len(profile.authors),
            len(profile.seed_papers),
        )
    )
    return DoctorReport(
        platform=platform_name,
        application_version=application_version,
        initialized=profile_status == "ok" and database_status == "ok",
        profile_status=profile_status,
        database_status=database_status,
        schema_version=schema_version,
        category_count=category_count,
        checkpoint_count=checkpoint_count,
        saved_paper_count=saved_count,
        destination_kind=destination_kind,
        destination_writable=destination_writable,
        preference_counts=preference_counts,
        sync_error_codes=error_codes,
    )


def render_doctor(report: DoctorReport) -> str:
    lines = [
        f"arXiv Digest {report.application_version}",
        f"Platform: {report.platform}",
    ]
    if not report.initialized:
        lines.extend(
            (
                "Status: not initialized",
                f"Profile: {report.profile_status}",
                f"Database: {report.database_status}",
                "Next step: run arxiv-digest init.",
            )
        )
        return "\n".join(lines) + "\n"
    keyword_count, phrase_count, author_count, seed_count = (
        report.preference_counts
    )
    lines.extend(
        (
            "Status: initialized",
            f"Database schema: {report.schema_version}",
            f"Categories: {report.category_count}",
            f"Metadata checkpoints: {report.checkpoint_count}",
            f"Saved papers: {report.saved_paper_count}",
            f"PDF destination kind: {report.destination_kind}",
            "PDF destination writable: "
            + ("yes" if report.destination_writable else "no"),
            f"Keyword preferences: {keyword_count}",
            f"Phrase preferences: {phrase_count}",
            f"Author preferences: {author_count}",
            f"Seed-paper preferences: {seed_count}",
            "Synchronization error codes: "
            + (", ".join(report.sync_error_codes) or "none"),
        )
    )
    return "\n".join(lines) + "\n"
