"""Deterministic, portable backup and crash-safe restore support."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import quote

from arxiv_digest.atomic import atomic_write, exclusive_flock
from arxiv_digest.folders import DestinationKind, FolderChoice, FolderService
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.paths import AppPaths
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    decode_profile,
    encode_profile,
)
from arxiv_digest.storage.database import open_database


FORMAT_NAME = "arxiv-digest-backup"
FORMAT_VERSION = 2
RECORD_SCHEMA_VERSION = 2

PORTABLE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "article": (
            "arxiv_id",
            "title",
            "abstract",
            "primary_category",
            "comments",
            "journal_ref",
            "doi",
            "metadata_hash",
            "last_oai_datestamp",
            "is_deleted",
            "deleted_at",
        ),
        "oai_tombstone": (
            "oai_identifier",
            "arxiv_id",
            "oai_datestamp",
            "set_specs_json",
            "observed_at",
        ),
        "version": (
            "arxiv_id",
            "version",
            "submitted_at",
            "size",
            "source_type",
        ),
        "author": ("arxiv_id", "position", "name"),
        "category": ("arxiv_id", "category", "is_primary"),
        "category_sync": (
            "category",
            "set_spec",
            "coverage_start",
            "completed_through_utc",
            "pending_backfill_start",
            "pending_backfill_until",
            "last_success_at",
        ),
        "category_article_state": (
            "category",
            "arxiv_id",
            "last_oai_datestamp",
            "category_set_hash",
            "observed_categories_json",
            "last_raw_sha256",
            "last_seen_at",
        ),
        "source_observation": (
            "observation_id",
            "source_key",
            "arxiv_id",
            "source",
            "category",
            "announce_type",
            "daily_list_date",
            "announced_version",
            "list_position",
            "oai_datestamp",
            "response_sha256",
            "observed_at",
        ),
        "catchup_day": (
            "category",
            "daily_list_date",
            "status",
            "attempted_at",
            "response_sha256",
            "error_code",
        ),
        "canonical_event": (
            "event_id",
            "arxiv_id",
            "announced_version",
            "daily_list_date",
            "version_resolution",
            "queue_revision",
            "reviewed_at",
            "recovered_after_finish",
            "conflict_code",
        ),
        "canonical_event_observation": (
            "event_id",
            "observation_id",
        ),
        "review_date_state": (
            "daily_list_date",
            "anchor_event_id",
            "profile_revision",
            "last_finished_at",
            "last_finished_revision",
        ),
        "saved_paper": ("arxiv_id", "saved_version"),
        "download_file": (
            "arxiv_id",
            "version",
            "filename",
            "byte_count",
            "sha256",
            "last_verified_at",
        ),
    }
)

_PORTABLE_TABLES: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType(
    {
        "article": ("articles", ("arxiv_id",)),
        "oai_tombstone": ("oai_tombstones", ("oai_identifier",)),
        "version": ("article_versions", ("arxiv_id", "version")),
        "author": ("article_authors", ("arxiv_id", "position")),
        "category": ("article_categories", ("arxiv_id", "category")),
        "category_sync": ("category_sync_state", ("category",)),
        "category_article_state": (
            "category_article_state",
            ("category", "arxiv_id"),
        ),
        "source_observation": (
            "source_observations",
            ("observation_id",),
        ),
        "catchup_day": (
            "catchup_days",
            ("category", "daily_list_date"),
        ),
        "canonical_event": ("canonical_events", ("event_id",)),
        "canonical_event_observation": (
            "canonical_event_observations",
            ("event_id", "observation_id"),
        ),
        "review_date_state": (
            "review_date_state",
            ("daily_list_date",),
        ),
        "saved_paper": ("saved_papers", ("arxiv_id",)),
        "download_file": ("download_files", ("arxiv_id", "version")),
    }
)


class BackupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BackupMember:
    name: Literal["profile.json", "state.jsonl"]
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_name: Literal["arxiv-digest-backup"]
    format_version: int
    application_generation: int
    application_version: str
    created_at: datetime
    members: tuple[BackupMember, ...]


@dataclass(frozen=True, slots=True)
class PortableProfile:
    schema_version: int
    revision: int
    category_coverage: tuple[ProfileCategory, ...]
    keywords: tuple[str, ...]
    phrases: tuple[str, ...]
    authors: tuple[str, ...]
    seed_papers: tuple[str, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(item.category for item in self.category_coverage)


@dataclass(frozen=True, slots=True)
class PortableRecord:
    record_type: str
    schema_version: int
    payload: tuple[tuple[str, str | int | None], ...]


@dataclass(frozen=True, slots=True)
class BackupInspection:
    path: Path
    archive_sha256: str
    manifest: BackupManifest
    profile: PortableProfile
    records: tuple[PortableRecord, ...]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    pre_restore_path: Path | None
    profile_revision: int


@dataclass(frozen=True, slots=True)
class BackupLimits:
    max_members: int = 16
    max_member_bytes: int = 512 * 1024 * 1024
    max_expanded_bytes: int = 512 * 1024 * 1024
    max_manifest_bytes: int = 256 * 1024
    max_profile_bytes: int = 4 * 1024 * 1024
    max_state_bytes: int = 128 * 1024 * 1024


DEFAULT_BACKUP_LIMITS = BackupLimits()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("backup clock must return an aware UTC datetime")
    return value.isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("invalid_json", "backup JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise BackupError("invalid_json", "backup JSON contains an invalid number")


def _decode_json(payload: bytes, label: str) -> object:
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except BackupError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError("invalid_json", f"{label} is not valid JSON") from error


def _require_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise BackupError("unsupported_schema", f"{label} fields are unsupported")
    return value


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BackupError("unsupported_schema", "backup timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BackupError("unsupported_schema", "backup timestamp is invalid") from error
    if _utc_text(parsed) != value:
        raise BackupError("unsupported_schema", "backup timestamp is invalid")
    return parsed


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BackupError("unsupported_schema", f"{label} SHA-256 is invalid")
    return value


def _is_portable_filename(value: object) -> bool:
    return (
        type(value) is str
        and value not in {"", ".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _parse_manifest(payload: bytes) -> BackupManifest:
    value = _require_object(
        _decode_json(payload, "manifest"),
        {
            "format_name",
            "format_version",
            "application_generation",
            "application_version",
            "created_at",
            "members",
        },
        "manifest",
    )
    if (
        value["format_name"] != FORMAT_NAME
        or value["format_version"] != FORMAT_VERSION
        or value["application_generation"] != 2
    ):
        raise BackupError("unsupported_schema", "backup format is unsupported")
    application_version = value["application_version"]
    if not isinstance(application_version, str) or not application_version.strip():
        raise BackupError("unsupported_schema", "application version is invalid")
    raw_members = value["members"]
    if not isinstance(raw_members, list) or len(raw_members) != 2:
        raise BackupError("unsupported_schema", "manifest members are invalid")
    members: list[BackupMember] = []
    for raw in raw_members:
        member = _require_object(
            raw,
            {"name", "byte_count", "sha256"},
            "manifest member",
        )
        name = member["name"]
        if name not in {"profile.json", "state.jsonl"}:
            raise BackupError("unsupported_schema", "manifest member is unknown")
        byte_count = member["byte_count"]
        if type(byte_count) is not int or byte_count < 0:
            raise BackupError("unsupported_schema", "manifest byte count is invalid")
        members.append(
            BackupMember(
                name,
                byte_count,
                _require_sha256(member["sha256"], str(name)),
            )
        )
    if tuple(member.name for member in members) != (
        "profile.json",
        "state.jsonl",
    ):
        raise BackupError("unsupported_schema", "manifest members are invalid")
    return BackupManifest(
        FORMAT_NAME,
        FORMAT_VERSION,
        2,
        application_version,
        _parse_utc(value["created_at"]),
        tuple(members),
    )


def _portable_text_values(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise BackupError("unsupported_schema", f"portable {label} are invalid")
    normalized = tuple(" ".join(item.split()) for item in value)
    if any(not item for item in normalized) or list(normalized) != value:
        raise BackupError("unsupported_schema", f"portable {label} are invalid")
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise BackupError("unsupported_schema", f"portable {label} are invalid")
    return normalized


def _parse_portable_profile(payload: bytes) -> PortableProfile:
    value = _require_object(
        _decode_json(payload, "portable profile"),
        {
            "schema_version",
            "revision",
            "category_coverage",
            "keywords",
            "phrases",
            "authors",
            "seed_papers",
        },
        "portable profile",
    )
    if value["schema_version"] != 2:
        raise BackupError("unsupported_schema", "portable profile is unsupported")
    revision = value["revision"]
    if type(revision) is not int or revision < 1:
        raise BackupError("unsupported_schema", "portable profile revision is invalid")
    raw_coverage = value["category_coverage"]
    if not isinstance(raw_coverage, list) or not raw_coverage:
        raise BackupError(
            "unsupported_schema", "portable category coverage is required"
        )
    category_coverage: list[ProfileCategory] = []
    for raw in raw_coverage:
        item = _require_object(
            raw,
            {"category", "coverage_start"},
            "portable category coverage",
        )
        category = item["category"]
        coverage_start = item["coverage_start"]
        if type(category) is not str or type(coverage_start) is not str:
            raise BackupError(
                "unsupported_schema", "portable category coverage is invalid"
            )
        try:
            parsed_start = datetime.strptime(coverage_start, "%Y-%m-%d").date()
            parsed = ProfileCategory(category, parsed_start)
        except ValueError as error:
            raise BackupError(
                "unsupported_schema", "portable category coverage is invalid"
            ) from error
        if parsed.category != category or parsed_start.isoformat() != coverage_start:
            raise BackupError(
                "unsupported_schema", "portable category coverage is invalid"
            )
        category_coverage.append(parsed)
    if len({item.category.casefold() for item in category_coverage}) != len(
        category_coverage
    ):
        raise BackupError(
            "unsupported_schema", "portable category coverage is invalid"
        )
    return PortableProfile(
        2,
        revision,
        tuple(category_coverage),
        _portable_text_values(value["keywords"], "keywords"),
        _portable_text_values(value["phrases"], "phrases"),
        _portable_text_values(value["authors"], "authors"),
        _portable_text_values(value["seed_papers"], "seed papers"),
    )


def _parse_records(payload: bytes) -> tuple[PortableRecord, ...]:
    records: list[PortableRecord] = []
    for position, line in enumerate(payload.splitlines(), start=1):
        if not line:
            raise BackupError("invalid_json", "state contains a blank record")
        value = _require_object(
            _decode_json(line, f"state record {position}"),
            {"record_type", "schema_version", "payload"},
            "state record",
        )
        record_type = value["record_type"]
        if not isinstance(record_type, str) or record_type not in PORTABLE_FIELDS:
            raise BackupError("unknown_record", "state record type is unsupported")
        if value["schema_version"] != RECORD_SCHEMA_VERSION:
            raise BackupError("unsupported_schema", "state record schema is unsupported")
        fields = PORTABLE_FIELDS[record_type]
        record_payload = _require_object(
            value["payload"], set(fields), f"{record_type} payload"
        )
        normalized: list[tuple[str, str | int | None]] = []
        for field in fields:
            item = record_payload[field]
            if item is not None and type(item) not in {str, int}:
                raise BackupError(
                    "unsupported_schema", "state record value is invalid"
                )
            normalized.append((field, item))
        records.append(
            PortableRecord(record_type, RECORD_SCHEMA_VERSION, tuple(normalized))
        )
    return tuple(records)


def _validate_cross_records(
    profile: PortableProfile,
    records: tuple[PortableRecord, ...],
) -> None:
    synchronized = {
        dict(record.payload)["category"]: dict(record.payload)["coverage_start"]
        for record in records
        if record.record_type == "category_sync"
    }
    if any(
        item.category not in synchronized
        or synchronized[item.category] != item.coverage_start.isoformat()
        for item in profile.category_coverage
    ):
        raise BackupError(
            "cross_record_invalid",
            "active category coverage does not match synchronization state",
        )
    articles = {
        dict(record.payload)["arxiv_id"]
        for record in records
        if record.record_type == "article"
    }
    if any(seed not in articles for seed in profile.seed_papers):
        raise BackupError(
            "cross_record_invalid",
            "a selected seed has no portable article metadata",
        )
    observations = {
        dict(record.payload)["observation_id"]: dict(record.payload)
        for record in records
        if record.record_type == "source_observation"
    }
    event_observations: dict[int, set[int]] = {}
    for record in records:
        if record.record_type != "canonical_event_observation":
            continue
        payload = dict(record.payload)
        event_observations.setdefault(int(payload["event_id"]), set()).add(
            int(payload["observation_id"])
        )
    for record in records:
        if record.record_type != "canonical_event":
            continue
        payload = dict(record.payload)
        linked = event_observations.get(int(payload["event_id"]), set())
        if not any(
            observation_id in observations
            and observations[observation_id]["source"] == "catchup"
            and observations[observation_id]["daily_list_date"]
            == payload["daily_list_date"]
            for observation_id in linked
        ):
            raise BackupError(
                "cross_record_invalid",
                "a canonical event has no linked exact-date catch-up observation",
            )
    for record in records:
        if record.record_type == "download_file":
            payload = dict(record.payload)
            filename = payload["filename"]
            if not _is_portable_filename(filename):
                raise BackupError(
                    "unsupported_schema",
                    "portable download filename is invalid",
                )
            if (
                type(payload["version"]) is not int
                or payload["version"] < 1
                or type(payload["byte_count"]) is not int
                or payload["byte_count"] < 1
            ):
                raise BackupError(
                    "unsupported_schema",
                    "portable download metadata is invalid",
                )
            _require_sha256(payload["sha256"], "portable download")
            _parse_utc(payload["last_verified_at"])
        if record.record_type != "category_sync":
            continue
        payload = dict(record.payload)
        start = payload["pending_backfill_start"]
        until = payload["pending_backfill_until"]
        if (start is None) != (until is None) or (
            isinstance(start, str)
            and isinstance(until, str)
            and start > until
        ):
            raise BackupError(
                "cross_record_invalid",
                "pending backfill bounds are invalid",
            )


def _read_archive_members(
    path: Path,
    limits: BackupLimits,
) -> tuple[str, dict[str, bytes]]:
    if not path.is_file() or path.is_symlink():
        raise BackupError("archive_invalid", "backup must be a regular file")
    digest = _sha256_file(path)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_members:
                raise BackupError("archive_too_large", "backup has too many members")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BackupError("duplicate_member", "backup has duplicate members")
            allowed = {"manifest.json", "profile.json", "state.jsonl"}
            semantic_limits = {
                "manifest.json": limits.max_manifest_bytes,
                "profile.json": limits.max_profile_bytes,
                "state.jsonl": limits.max_state_bytes,
            }
            expanded = 0
            for info in infos:
                parts = info.filename.split("/")
                if (
                    info.filename.startswith("/")
                    or "\\" in info.filename
                    or ".." in parts
                ):
                    raise BackupError("path_traversal", "backup member path is unsafe")
                if info.filename not in allowed:
                    raise BackupError("unknown_member", "backup has an unknown member")
                if info.flag_bits & 0x1:
                    raise BackupError("encrypted_member", "encrypted backups are unsupported")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise BackupError("symlink_member", "backup symlinks are unsupported")
                member_limit = min(
                    limits.max_member_bytes,
                    semantic_limits[info.filename],
                )
                if info.file_size > member_limit:
                    raise BackupError("archive_too_large", "backup member is too large")
                expanded += info.file_size
                if expanded > limits.max_expanded_bytes:
                    raise BackupError("archive_too_large", "backup is too large")
            if set(names) != allowed or len(names) != len(allowed):
                raise BackupError("missing_member", "backup members are incomplete")
            members: dict[str, bytes] = {}
            for info in infos:
                member_limit = min(
                    limits.max_member_bytes,
                    semantic_limits[info.filename],
                )
                with archive.open(info, "r") as handle:
                    chunks: list[bytes] = []
                    byte_count = 0
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        byte_count += len(chunk)
                        if (
                            byte_count > member_limit
                            or byte_count > info.file_size
                        ):
                            raise BackupError(
                                "archive_too_large", "backup member is too large"
                            )
                        chunks.append(chunk)
                    members[info.filename] = b"".join(chunks)
            return digest, members
    except BackupError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise BackupError("archive_invalid", "backup ZIP is invalid") from error


def inspect_backup(
    path: Path,
    *,
    limits: BackupLimits = DEFAULT_BACKUP_LIMITS,
) -> BackupInspection:
    """Validate and inspect an archive without touching application state."""

    path = Path(path)
    digest, members = _read_archive_members(path, limits)
    manifest = _parse_manifest(members["manifest.json"])
    for expected in manifest.members:
        payload = members[expected.name]
        if len(payload) != expected.byte_count:
            raise BackupError("checksum_mismatch", "backup member size is incorrect")
        if hashlib.sha256(payload).hexdigest() != expected.sha256:
            raise BackupError("checksum_mismatch", "backup member checksum is incorrect")
    profile = _parse_portable_profile(members["profile.json"])
    records = _parse_records(members["state.jsonl"])
    _validate_cross_records(profile, records)
    return BackupInspection(
        path,
        digest,
        manifest,
        profile,
        records,
    )


def _record_payload(record: PortableRecord) -> dict[str, str | int | None]:
    return dict(record.payload)


def _insert_portable_records(
    connection: sqlite3.Connection,
    records: tuple[PortableRecord, ...],
) -> None:
    grouped = {
        record_type: [
            record for record in records if record.record_type == record_type
        ]
        for record_type in PORTABLE_FIELDS
    }
    for record_type, fields in PORTABLE_FIELDS.items():
        if record_type == "download_file":
            continue
        table = _PORTABLE_TABLES[record_type][0]
        placeholders = ", ".join("?" for _ in fields)
        columns = ", ".join(fields)
        for record in grouped[record_type]:
            payload = _record_payload(record)
            connection.execute(
                f"INSERT INTO {table}({columns}) VALUES ({placeholders})",
                tuple(payload[field] for field in fields),
            )


def _recompute_local_downloads(
    connection: sqlite3.Connection,
    destination: Path,
    now: datetime,
    records: tuple[PortableRecord, ...],
) -> None:
    timestamp = _utc_text(now)
    for record in records:
        if record.record_type != "download_file":
            continue
        payload = _record_payload(record)
        filename = payload["filename"]
        if not _is_portable_filename(filename):
            continue
        candidate = destination / filename
        if candidate.is_symlink() or not candidate.is_file():
            continue
        digest = hashlib.sha256()
        prefix = b""
        byte_count = 0
        with candidate.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                if len(prefix) < 5:
                    prefix += chunk[: 5 - len(prefix)]
                byte_count += len(chunk)
                digest.update(chunk)
        if (
            byte_count < 1
            or not prefix.startswith(b"%PDF-")
            or byte_count != payload["byte_count"]
            or digest.hexdigest() != payload["sha256"]
        ):
            continue
        connection.execute(
            """INSERT INTO download_files(
                   arxiv_id, version, filename, byte_count, sha256,
                   last_verified_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                payload["arxiv_id"],
                payload["version"],
                filename,
                byte_count,
                digest.hexdigest(),
                timestamp,
            ),
        )


def _build_restored_state(
    root: Path,
    inspection: BackupInspection,
    destination: PdfDestination,
    profile_revision: int,
    now: datetime,
) -> tuple[Path, Path]:
    database_path = root / "state.sqlite3"
    profile_path = root / "profile.json"
    open_database(database_path).close()
    profile = Profile(
        schema_version=inspection.profile.schema_version,
        revision=profile_revision,
        category_coverage=inspection.profile.category_coverage,
        keywords=inspection.profile.keywords,
        phrases=inspection.profile.phrases,
        authors=inspection.profile.authors,
        seed_papers=inspection.profile.seed_papers,
        pdf_destination=destination,
    )
    profile_payload = encode_profile(profile)
    atomic_write(profile_path, profile_payload, mode=0o600)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _insert_portable_records(connection, inspection.records)
        restored_queue_revision = max(
            (
                *(
                    int(_record_payload(record)["queue_revision"])
                    for record in inspection.records
                    if record.record_type == "canonical_event"
                ),
                *(
                    int(value)
                    for record in inspection.records
                    if record.record_type == "review_date_state"
                    for value in (_record_payload(record)["last_finished_revision"],)
                    if value is not None
                ),
            ),
            default=0,
        )
        connection.execute(
            "UPDATE state_meta SET queue_revision = ? WHERE singleton = 1",
            (restored_queue_revision,),
        )
        connection.execute(
            """UPDATE profile_publication
               SET pending_revision = ?, pending_sha256 = ?, status = 'published'
               WHERE singleton = 1""",
            (
                profile_revision,
                hashlib.sha256(profile_payload).hexdigest(),
            ),
        )
        _recompute_local_downloads(
            connection,
            destination.path,
            now,
            inspection.records,
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    open_database(database_path).close()
    decode_profile(profile_path.read_bytes())
    return database_path, profile_path


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise BackupError(
                "restore_collision", "restore staging filename already exists"
            ) from error
        _fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _checkpoint_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()


def _journal_payload(value: Mapping[str, object]) -> bytes:
    return _json_bytes(dict(value))


def _write_restore_journal(path: Path, value: Mapping[str, object]) -> None:
    atomic_write(path, _journal_payload(value), mode=0o600)


def _verify_database_readonly(path: Path) -> None:
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise BackupError("restore_invalid", "restored database is invalid")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError(
                "restore_invalid", "restored database references are invalid"
            )
    finally:
        connection.close()


def _verify_published_pair(paths: AppPaths) -> None:
    if (
        not paths.profile_path.is_file()
        or paths.profile_path.is_symlink()
        or not paths.database_path.is_file()
        or paths.database_path.is_symlink()
    ):
        raise BackupError("restore_invalid", "restored state files are missing")
    profile_payload = paths.profile_path.read_bytes()
    profile = decode_profile(profile_payload)
    digest = hashlib.sha256(profile_payload).hexdigest()
    _verify_database_readonly(paths.database_path)
    encoded = quote(str(paths.database_path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)
    try:
        marker = connection.execute(
            "SELECT pending_revision, pending_sha256, status "
            "FROM profile_publication WHERE singleton = 1"
        ).fetchone()
        if marker != (profile.revision, digest, "published"):
            raise BackupError(
                "restore_invalid", "restored profile publication is inconsistent"
            )
    finally:
        connection.close()


def _remove_and_fsync(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
        _fsync_directory(path.parent)


def _restore_old_file(
    active: Path,
    rollback: Path | None,
    old_digest: str | None,
    new_digest: str,
) -> None:
    if old_digest is None:
        if active.exists() and _sha256_file(active) != new_digest:
            raise BackupError(
                "rollback_failed", "active restore file changed unexpectedly"
            )
        _remove_and_fsync(active)
        return
    if rollback is None or not rollback.is_file() or rollback.is_symlink():
        raise BackupError("rollback_failed", "restore rollback file is missing")
    if _sha256_file(rollback) != old_digest:
        raise BackupError("rollback_failed", "restore rollback hash is invalid")
    os.replace(rollback, active)
    _fsync_directory(active.parent)
    if _sha256_file(active) != old_digest:
        raise BackupError("rollback_failed", "restore rollback did not verify")


def _publish_restored_state(
    paths: AppPaths,
    replacement_database: Path,
    replacement_profile: Path,
    *,
    suffix: str,
    crash_injector: Callable[[str], None],
) -> None:
    if paths.database_path.exists():
        _checkpoint_database(paths.database_path)
    old_database_digest = (
        _sha256_file(paths.database_path) if paths.database_path.exists() else None
    )
    old_profile_digest = (
        _sha256_file(paths.profile_path) if paths.profile_path.exists() else None
    )
    new_database_digest = _sha256_file(replacement_database)
    new_profile_digest = _sha256_file(replacement_profile)
    rollback_database = (
        None
        if old_database_digest is None
        else paths.database_path.with_name(
            f".{paths.database_path.name}.restore-{suffix}.rollback"
        )
    )
    rollback_profile = (
        None
        if old_profile_digest is None
        else paths.profile_path.with_name(
            f".{paths.profile_path.name}.restore-{suffix}.rollback"
        )
    )
    staged_database = paths.database_path.with_name(
        f".{paths.database_path.name}.restore-{suffix}.new"
    )
    staged_profile = paths.profile_path.with_name(
        f".{paths.profile_path.name}.restore-{suffix}.new"
    )
    if rollback_database is not None:
        try:
            os.link(paths.database_path, rollback_database)
        except FileExistsError as error:
            raise BackupError(
                "restore_collision", "restore rollback filename already exists"
            ) from error
        _fsync_directory(paths.database_path.parent)
    if rollback_profile is not None:
        try:
            os.link(paths.profile_path, rollback_profile)
        except FileExistsError as error:
            if rollback_database is not None:
                _remove_and_fsync(rollback_database)
            raise BackupError(
                "restore_collision", "restore rollback filename already exists"
            ) from error
        _fsync_directory(paths.profile_path.parent)
    try:
        _copy_file_exclusive(replacement_database, staged_database)
        _copy_file_exclusive(replacement_profile, staged_profile)
    except Exception:
        for path in (
            staged_database,
            staged_profile,
            rollback_database,
            rollback_profile,
        ):
            if path is not None:
                _remove_and_fsync(path)
        raise
    journal: dict[str, object] = {
        "schema_version": 1,
        "phase": "prepared",
        "old_database_sha256": old_database_digest,
        "old_profile_sha256": old_profile_digest,
        "new_database_sha256": new_database_digest,
        "new_profile_sha256": new_profile_digest,
        "rollback_database": (
            None if rollback_database is None else rollback_database.name
        ),
        "rollback_profile": (
            None if rollback_profile is None else rollback_profile.name
        ),
        "staged_database": staged_database.name,
        "staged_profile": staged_profile.name,
    }
    _write_restore_journal(paths.restore_journal_path, journal)
    try:
        crash_injector("journal_fsynced")
        os.replace(staged_database, paths.database_path)
        _fsync_directory(paths.database_path.parent)
        journal["phase"] = "database_published"
        _write_restore_journal(paths.restore_journal_path, journal)
        crash_injector("database_published")
        os.replace(staged_profile, paths.profile_path)
        _fsync_directory(paths.profile_path.parent)
        journal["phase"] = "profile_published"
        _write_restore_journal(paths.restore_journal_path, journal)
        crash_injector("profile_published")
        if _sha256_file(paths.database_path) != new_database_digest:
            raise BackupError("restore_invalid", "restored database hash changed")
        if _sha256_file(paths.profile_path) != new_profile_digest:
            raise BackupError("restore_invalid", "restored profile hash changed")
        _verify_published_pair(paths)
    except Exception as error:
        try:
            _restore_old_file(
                paths.database_path,
                rollback_database,
                old_database_digest,
                new_database_digest,
            )
            _restore_old_file(
                paths.profile_path,
                rollback_profile,
                old_profile_digest,
                new_profile_digest,
            )
            if old_database_digest is not None:
                _verify_database_readonly(paths.database_path)
            if old_profile_digest is not None:
                decode_profile(paths.profile_path.read_bytes())
        except Exception as rollback_error:
            raise BackupError(
                "rollback_failed",
                "restore failed and automatic rollback could not be verified",
            ) from rollback_error
        finally:
            for path in (staged_database, staged_profile):
                _remove_and_fsync(path)
            _remove_and_fsync(paths.restore_journal_path)
        raise BackupError(
            "restore_failed", "restore failed; previous state was restored"
        ) from error
    for path in (
        rollback_database,
        rollback_profile,
        staged_database,
        staged_profile,
    ):
        if path is not None:
            _remove_and_fsync(path)
    _remove_and_fsync(paths.restore_journal_path)


_RESTORE_JOURNAL_FIELDS = {
    "schema_version",
    "phase",
    "old_database_sha256",
    "old_profile_sha256",
    "new_database_sha256",
    "new_profile_sha256",
    "rollback_database",
    "rollback_profile",
    "staged_database",
    "staged_profile",
}


def _journal_filename(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise BackupError("restore_journal_invalid", f"{label} is invalid")
    return value


def _restore_name_suffix(
    value: object,
    active_name: str,
    ending: Literal["new", "rollback"],
    label: str,
) -> str:
    name = _journal_filename(value, label)
    match = re.fullmatch(
        rf"\.{re.escape(active_name)}\.restore-([0-9a-f]{{16}})\.{ending}",
        name,
    )
    if match is None:
        raise BackupError("restore_journal_invalid", f"{label} is invalid")
    return match.group(1)


def _parse_restore_journal(paths: AppPaths) -> dict[str, object]:
    path = paths.restore_journal_path
    if not path.is_file() or path.is_symlink():
        raise BackupError("restore_journal_invalid", "restore journal is invalid")
    value = _require_object(
        _decode_json(path.read_bytes(), "restore journal"),
        _RESTORE_JOURNAL_FIELDS,
        "restore journal",
    )
    if value["schema_version"] != 1 or value["phase"] not in {
        "prepared",
        "database_published",
        "profile_published",
    }:
        raise BackupError("restore_journal_invalid", "restore journal is invalid")
    for field in (
        "new_database_sha256",
        "new_profile_sha256",
    ):
        _require_sha256(value[field], field)
    suffixes: list[str] = []
    for digest_field, rollback_field, active_name in (
        (
            "old_database_sha256",
            "rollback_database",
            paths.database_path.name,
        ),
        ("old_profile_sha256", "rollback_profile", paths.profile_path.name),
    ):
        digest = value[digest_field]
        rollback = value[rollback_field]
        if digest is None:
            if rollback is not None:
                raise BackupError(
                    "restore_journal_invalid", "restore journal is inconsistent"
                )
        else:
            _require_sha256(digest, digest_field)
            suffixes.append(
                _restore_name_suffix(
                    rollback,
                    active_name,
                    "rollback",
                    rollback_field,
                )
            )
    suffixes.extend(
        (
            _restore_name_suffix(
                value["staged_database"],
                paths.database_path.name,
                "new",
                "staged database",
            ),
            _restore_name_suffix(
                value["staged_profile"],
                paths.profile_path.name,
                "new",
                "staged profile",
            ),
        )
    )
    if len(set(suffixes)) != 1:
        raise BackupError(
            "restore_journal_invalid", "restore journal filenames disagree"
        )
    return value


def _matches_digest(path: Path, digest: str) -> bool:
    return path.is_file() and not path.is_symlink() and _sha256_file(path) == digest


def _cleanup_recovery_files(
    paths: AppPaths,
    rollback_database: Path | None,
    rollback_profile: Path | None,
    staged_database: Path,
    staged_profile: Path,
) -> None:
    for path in (
        rollback_database,
        rollback_profile,
        staged_database,
        staged_profile,
    ):
        if path is not None:
            _remove_and_fsync(path)
    _remove_and_fsync(paths.restore_journal_path)


def _recover_restore_locked(paths: AppPaths) -> None:
    journal = _parse_restore_journal(paths)
    old_database_digest = journal["old_database_sha256"]
    old_profile_digest = journal["old_profile_sha256"]
    new_database_digest = str(journal["new_database_sha256"])
    new_profile_digest = str(journal["new_profile_sha256"])
    rollback_database = (
        None
        if journal["rollback_database"] is None
        else paths.database_path.parent / str(journal["rollback_database"])
    )
    rollback_profile = (
        None
        if journal["rollback_profile"] is None
        else paths.profile_path.parent / str(journal["rollback_profile"])
    )
    staged_database = paths.database_path.parent / str(journal["staged_database"])
    staged_profile = paths.profile_path.parent / str(journal["staged_profile"])

    database_is_new = _matches_digest(paths.database_path, new_database_digest)
    profile_is_new = _matches_digest(paths.profile_path, new_profile_digest)
    if database_is_new and not profile_is_new and _matches_digest(
        staged_profile, new_profile_digest
    ):
        os.replace(staged_profile, paths.profile_path)
        _fsync_directory(paths.profile_path.parent)
        profile_is_new = True
    if database_is_new and profile_is_new:
        _verify_published_pair(paths)
        _cleanup_recovery_files(
            paths,
            rollback_database,
            rollback_profile,
            staged_database,
            staged_profile,
        )
        return

    _restore_old_file(
        paths.database_path,
        rollback_database,
        old_database_digest if isinstance(old_database_digest, str) else None,
        new_database_digest,
    )
    _restore_old_file(
        paths.profile_path,
        rollback_profile,
        old_profile_digest if isinstance(old_profile_digest, str) else None,
        new_profile_digest,
    )
    if old_database_digest is not None or old_profile_digest is not None:
        _verify_published_pair(paths)
    _cleanup_recovery_files(
        paths,
        rollback_database,
        rollback_profile,
        staged_database,
        staged_profile,
    )


def recover_restore(
    paths: AppPaths,
    *,
    maintenance: MaintenanceBarrier | None = None,
    timeout: float | None = None,
) -> None:
    """Complete or roll back a hash-verified interrupted restore."""

    if not paths.restore_journal_path.exists():
        return
    lease = (
        nullcontext()
        if maintenance is None
        else maintenance.exclusive(cancel_active=False, timeout=timeout)
    )
    with lease:
        with exclusive_flock(paths.profile_lock_path):
            if paths.restore_journal_path.exists():
                _recover_restore_locked(paths)


def restore_backup(
    paths: AppPaths,
    inspection: BackupInspection,
    destination: PdfDestination,
    *,
    maintenance: MaintenanceBarrier | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    random_bytes: Callable[[int], bytes] = os.urandom,
    cancel_active: bool = False,
    timeout: float | None = None,
    crash_injector: Callable[[str], None] | None = None,
) -> RestoreResult:
    """Revalidate and restore a portable snapshot into confirmed local state."""

    fresh = inspect_backup(inspection.path)
    if fresh.archive_sha256 != inspection.archive_sha256:
        raise BackupError("archive_changed", "backup changed after inspection")
    inject_crash = crash_injector or (lambda _: None)
    validated_destination = FolderService().validate(
        FolderChoice(
            DestinationKind(destination.kind),
            destination.path,
            destination.path.name,
        )
    )
    now = clock()
    _utc_text(now)
    lease = (
        nullcontext()
        if maintenance is None
        else maintenance.exclusive(
            cancel_active=cancel_active,
            timeout=timeout,
        )
    )
    with lease:
        with exclusive_flock(paths.profile_lock_path):
            if _sha256_file(inspection.path) != fresh.archive_sha256:
                raise BackupError(
                    "archive_changed", "backup changed after inspection"
                )
            if paths.profile_path.exists() != paths.database_path.exists():
                raise BackupError(
                    "local_state_incomplete",
                    "local state is incomplete and cannot be replaced safely",
                )
            current = (
                None
                if not paths.profile_path.exists()
                else decode_profile(paths.profile_path.read_bytes())
            )
            previous_revision = 0 if current is None else current.revision
            restored_revision = max(
                previous_revision + 1,
                fresh.profile.revision,
            )
            paths.ensure()
            os.chmod(paths.backup_dir, 0o700)
            pre_restore_path: Path | None = None
            if current is not None:
                for _ in range(100):
                    suffix = random_bytes(8)
                    if not isinstance(suffix, bytes) or len(suffix) != 8:
                        raise ValueError("backup random source must return 8 bytes")
                    candidate = paths.backup_dir / (
                        f"pre-restore-{now:%Y%m%dT%H%M%SZ}-{suffix.hex()}"
                        ".arxiv-digest-backup.zip"
                    )
                    try:
                        _snapshot_payloads(
                            paths,
                            candidate,
                            application_version="0.2.0",
                            created_at=now,
                        )
                    except BackupError as error:
                        if error.code == "destination_exists":
                            continue
                        raise
                    pre_restore_path = candidate
                    inspect_backup(candidate)
                    break
                if pre_restore_path is None:
                    raise BackupError(
                        "name_exhausted",
                        "could not allocate a recovery-backup filename",
                    )
            with tempfile.TemporaryDirectory(
                prefix=".restore-build-",
                dir=paths.data_dir,
            ) as temporary_name:
                replacement_database, replacement_profile = _build_restored_state(
                    Path(temporary_name),
                    fresh,
                    validated_destination,
                    restored_revision,
                    now,
                )
                publication_suffix = random_bytes(8)
                if (
                    not isinstance(publication_suffix, bytes)
                    or len(publication_suffix) != 8
                ):
                    raise ValueError("backup random source must return 8 bytes")
                _publish_restored_state(
                    paths,
                    replacement_database,
                    replacement_profile,
                    suffix=publication_suffix.hex(),
                    crash_injector=inject_crash,
                )
            return RestoreResult(pre_restore_path, restored_revision)


def _portable_profile_bytes(profile_payload: bytes) -> bytes:
    profile = decode_profile(profile_payload)
    return _json_bytes(
        {
            "schema_version": profile.schema_version,
            "revision": profile.revision,
            "category_coverage": [
                {
                    "category": item.category,
                    "coverage_start": item.coverage_start.isoformat(),
                }
                for item in profile.category_coverage
            ],
            "keywords": list(profile.keywords),
            "phrases": list(profile.phrases),
            "authors": list(profile.authors),
            "seed_papers": list(profile.seed_papers),
        }
    )


def _state_bytes(connection: sqlite3.Connection) -> bytes:
    lines: list[bytes] = []
    for record_type, fields in PORTABLE_FIELDS.items():
        table, order_fields = _PORTABLE_TABLES[record_type]
        selected = ", ".join(fields)
        ordering = ", ".join(order_fields)
        rows = connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY {ordering}"
        )
        for row in rows:
            lines.append(
                _json_bytes(
                    {
                        "payload": {
                            field: row[field]
                            for field in fields
                        },
                        "record_type": record_type,
                        "schema_version": RECORD_SCHEMA_VERSION,
                    }
                )
            )
    return b"".join(lines)


def _manifest_value(manifest: BackupManifest) -> dict[str, object]:
    return {
        "format_name": manifest.format_name,
        "format_version": manifest.format_version,
        "application_generation": manifest.application_generation,
        "application_version": manifest.application_version,
        "created_at": _utc_text(manifest.created_at),
        "members": [
            {
                "name": member.name,
                "byte_count": member.byte_count,
                "sha256": member.sha256,
            }
            for member in manifest.members
        ],
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_archive(
    path: Path,
    manifest: BackupManifest,
    profile_payload: bytes,
    state_payload: bytes,
) -> None:
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        for name, payload in (
            ("manifest.json", _json_bytes(_manifest_value(manifest))),
            ("profile.json", profile_payload),
            ("state.jsonl", state_payload),
        ):
            archive.writestr(_zip_info(name), payload)


def _verify_written_archive(
    path: Path,
    manifest: BackupManifest,
    profile_payload: bytes,
    state_payload: bytes,
) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        if archive.namelist() != [
            "manifest.json",
            "profile.json",
            "state.jsonl",
        ]:
            raise BackupError("archive_invalid", "backup member order is invalid")
        expected = {
            "manifest.json": _json_bytes(_manifest_value(manifest)),
            "profile.json": profile_payload,
            "state.jsonl": state_payload,
        }
        for name, payload in expected.items():
            if archive.read(name) != payload:
                raise BackupError("archive_invalid", "backup verification failed")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("archive_invalid", "backup is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot_payloads(
    paths: AppPaths,
    destination: Path,
    *,
    application_version: str,
    created_at: datetime,
) -> BackupManifest:
    if not paths.profile_path.is_file() or paths.profile_path.is_symlink():
        raise BackupError("not_initialized", "a valid profile is required")
    if not paths.database_path.is_file() or paths.database_path.is_symlink():
        raise BackupError("not_initialized", "a valid database is required")
    connection = sqlite3.connect(paths.database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        connection.execute("BEGIN")
        try:
            generation = connection.execute(
                "SELECT singleton, generation FROM application_generation"
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise BackupError(
                "unsupported_schema",
                "application data generation is unsupported",
            ) from error
        if generation is None or tuple(generation) != (1, 2):
            raise BackupError(
                "unsupported_schema",
                "application data generation is unsupported",
            )
        marker = connection.execute(
            "SELECT pending_revision, pending_sha256, status "
            "FROM profile_publication WHERE singleton = 1"
        ).fetchone()
        profile_payload = paths.profile_path.read_bytes()
        profile = decode_profile(profile_payload)
        digest = hashlib.sha256(profile_payload).hexdigest()
        if (
            marker is None
            or marker["status"] != "published"
            or marker["pending_revision"] != profile.revision
            or marker["pending_sha256"] != digest
        ):
            raise BackupError(
                "publication_incomplete",
                "profile publication must be reconciled before export",
            )
        portable_profile = _portable_profile_bytes(profile_payload)
        state_payload = _state_bytes(connection)
        members = (
            BackupMember(
                "profile.json",
                len(portable_profile),
                hashlib.sha256(portable_profile).hexdigest(),
            ),
            BackupMember(
                "state.jsonl",
                len(state_payload),
                hashlib.sha256(state_payload).hexdigest(),
            ),
        )
        manifest = BackupManifest(
            FORMAT_NAME,
            FORMAT_VERSION,
            generation["generation"],
            application_version,
            created_at,
            members,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            _write_archive(
                temporary,
                manifest,
                portable_profile,
                state_payload,
            )
            _verify_written_archive(
                temporary,
                manifest,
                portable_profile,
                state_payload,
            )
            _fsync_file(temporary)
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise BackupError(
                    "destination_exists",
                    "backup destination already exists",
                ) from error
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest
    finally:
        connection.rollback()
        connection.close()


def export_backup(
    paths: AppPaths,
    destination: Path,
    *,
    maintenance: MaintenanceBarrier | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    application_version: str = "0.2.0",
) -> BackupManifest:
    """Export a verified portable snapshot without overwriting a target."""

    destination = Path(destination)
    created_at = clock()
    _utc_text(created_at)
    lease = (
        nullcontext()
        if maintenance is None
        else maintenance.exclusive(cancel_active=False)
    )
    with lease:
        with exclusive_flock(paths.profile_lock_path):
            return _snapshot_payloads(
                paths,
                destination,
                application_version=application_version,
                created_at=created_at,
            )
