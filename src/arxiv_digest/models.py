"""Normalized immutable domain values for arXiv metadata and review events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from .sources.xml import parse_arxiv_id


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def _require_base_arxiv_id(value: str) -> None:
    base, version = parse_arxiv_id(value)
    if value != base or version is not None:
        raise ValueError("arXiv identifier must be an unversioned base ID")


def _require_nonblank(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")


def _require_nonempty_nonblank(values: tuple[str, ...], field: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field} must be a tuple")
    if not values:
        raise ValueError(f"{field} must contain at least one value")
    for value in values:
        _require_nonblank(value, field)
    normalized = tuple(value.strip().casefold() for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} values must be unique")


def _require_optional_positive(value: int | None, field: str) -> None:
    if value is not None and value < 1:
        raise ValueError(f"{field} must be positive")


def _require_nonnegative(value: int | None, field: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{field} must be nonnegative")


def _require_sha256(value: str, field: str = "raw_sha256") -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a 64-character SHA-256")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")


def _require_tuple(value: object, field: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")


class AnnounceType(StrEnum):
    NEW = "new"
    CROSS = "cross"
    REPLACE = "replace"
    REPLACE_CROSS = "replace-cross"


class EvidenceSource(StrEnum):
    ATOM = "atom"
    CATCHUP = "catchup"
    OAI = "oai"


class VersionResolution(StrEnum):
    ATOM_CONFIRMED = "atom_confirmed"
    CHRONOLOGY_MATCHED = "chronology_matched"
    UNCONFIRMED = "unconfirmed"


def _require_version_resolution(
    announced_version: int | None,
    version_resolution: VersionResolution,
) -> None:
    if (announced_version is None) != (
        version_resolution is VersionResolution.UNCONFIRMED
    ):
        raise ValueError(
            "announced_version must be absent exactly when "
            "version_resolution is unconfirmed"
        )


class CatchupDayStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    EMPTY = "empty"
    FAILED = "failed"


class EnrichmentStatus(StrEnum):
    COMPLETE = "complete"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaperMetadata:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    abstract: str
    primary_category: str | None
    categories: tuple[str, ...]
    comments: str = ""
    journal_ref: str = ""
    doi: str | None = None

    def __post_init__(self) -> None:
        _require_base_arxiv_id(self.arxiv_id)
        _require_nonblank(self.title, "title")
        _require_nonempty_nonblank(self.authors, "authors")
        _require_nonempty_nonblank(self.categories, "categories")
        if self.primary_category is not None:
            _require_nonblank(self.primary_category, "primary_category")


@dataclass(frozen=True, slots=True)
class PaperVersion:
    number: int
    submitted_at: datetime
    size: str | None = None
    source_type: str | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("version number must be positive")
        if (
            self.submitted_at.tzinfo is None
            or self.submitted_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("submitted_at must be UTC")


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source_key: str
    arxiv_id: str
    source: EvidenceSource
    category: str | None
    announce_type: AnnounceType | None
    daily_list_date: date | None
    announced_version: int | None
    list_position: int | None
    oai_datestamp: date | None
    response_sha256: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_base_arxiv_id(self.arxiv_id)
        _require_nonblank(self.source_key, "source_key")
        if self.category is not None:
            _require_nonblank(self.category, "category")
        _require_optional_positive(self.announced_version, "announced_version")
        _require_nonnegative(self.list_position, "list_position")
        _require_sha256(self.response_sha256, "response_sha256")
        _require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class ReconciledEvent:
    arxiv_id: str
    daily_list_date: date
    announced_version: int | None
    version_resolution: VersionResolution
    observation_keys: tuple[str, ...]
    conflict_code: str | None = None

    def __post_init__(self) -> None:
        _require_base_arxiv_id(self.arxiv_id)
        _require_optional_positive(self.announced_version, "announced_version")
        _require_version_resolution(
            self.announced_version, self.version_resolution
        )
        _require_nonempty_nonblank(self.observation_keys, "observation_keys")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    arxiv_id: str
    events: tuple[ReconciledEvent, ...]
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_base_arxiv_id(self.arxiv_id)
        _require_tuple(self.events, "events")
        _require_tuple(self.diagnostic_codes, "diagnostic_codes")


@dataclass(frozen=True, slots=True)
class CategoryConfig:
    category: str
    oai_set_spec: str
    coverage_start: date

    def __post_init__(self) -> None:
        _require_nonblank(self.category, "category")
        _require_nonblank(self.oai_set_spec, "oai_set_spec")


@dataclass(frozen=True, slots=True)
class OaiArticle:
    oai_identifier: str
    oai_datestamp: date
    set_specs: tuple[str, ...]
    metadata: PaperMetadata
    versions: tuple[PaperVersion, ...]

    def __post_init__(self) -> None:
        _require_tuple(self.set_specs, "set_specs")
        _require_tuple(self.versions, "versions")
        if any(
            previous.number >= current.number
            for previous, current in zip(self.versions, self.versions[1:])
        ):
            raise ValueError("OAI version history must be strictly increasing")


@dataclass(frozen=True, slots=True)
class OaiTombstone:
    oai_identifier: str
    oai_datestamp: date
    set_specs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_tuple(self.set_specs, "set_specs")


@dataclass(frozen=True, slots=True)
class AtomEntry:
    metadata: PaperMetadata
    announced_version: int
    published_at: datetime
    announce_type: AnnounceType
    mailing_date: date
    position: int

    def __post_init__(self) -> None:
        _require_optional_positive(self.announced_version, "announced_version")
        _require_utc(self.published_at, "published_at")
        _require_nonnegative(self.position, "position")

    @property
    def version(self) -> PaperVersion:
        """Compatibility view; ``published_at`` is not a submission timestamp."""

        return PaperVersion(self.announced_version, self.published_at)


@dataclass(frozen=True, slots=True)
class AtomBatch:
    category: str
    mailing_date: date
    entries: tuple[AtomEntry, ...]
    raw_sha256: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        _require_nonblank(self.category, "category")
        _require_tuple(self.entries, "entries")
        _require_sha256(self.raw_sha256)
        _require_utc(self.fetched_at, "fetched_at")


@dataclass(frozen=True, slots=True)
class CatchupEntry:
    metadata: PaperMetadata
    section: AnnounceType
    mailing_date: date
    position: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.position, "position")

    @property
    def announced_version(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class CatchupPage:
    category: str
    mailing_date: date
    page: int
    total_pages: int
    entries: tuple[CatchupEntry, ...]
    raw_sha256: str

    def __post_init__(self) -> None:
        _require_nonblank(self.category, "category")
        _require_tuple(self.entries, "entries")
        _require_sha256(self.raw_sha256)


@dataclass(frozen=True, slots=True)
class CatchupDay:
    category: str
    mailing_date: date
    status: CatchupDayStatus | EnrichmentStatus
    pages: tuple[CatchupPage, ...]
    error_code: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        _require_nonblank(self.category, "category")
        _require_tuple(self.pages, "pages")


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    event_id: int
    arxiv_id: str
    daily_list_date: date
    announced_version: int | None
    version_resolution: VersionResolution
    observations: tuple[SourceObservation, ...]
    queue_revision: int
    reviewed_at: datetime | None
    recovered_after_finish: bool = False
    conflict_code: str | None = None

    def __post_init__(self) -> None:
        _require_base_arxiv_id(self.arxiv_id)
        _require_tuple(self.observations, "observations")
        _require_optional_positive(self.announced_version, "announced_version")
        _require_version_resolution(
            self.announced_version, self.version_resolution
        )
        if self.reviewed_at is not None:
            _require_utc(self.reviewed_at, "reviewed_at")
