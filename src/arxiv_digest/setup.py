"""Resumable first-run setup and crash-recoverable profile publication."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import ContextManager, Literal

from arxiv_digest.candidates import (
    CandidateCorpusBuild,
    CandidateDocument,
    candidate_corpus_hash,
)
from arxiv_digest.atomic import atomic_write, exclusive_flock
from arxiv_digest.desktop_launcher import LauncherCollisionError
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.models import CategoryConfig
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileRepository,
    ProfileRevisionError,
    decode_profile,
    encode_profile,
)
from arxiv_digest.storage.store import Store


_SCHEMA_VERSION = 1
DESKTOP_LAUNCHER_PROMPT = (
    "Would you like a desktop launcher so you can open arXiv Digest without "
    "using the terminal?"
)
CREATE_DESKTOP_LAUNCHER_LABEL = "Create desktop launcher"
NOT_NOW_LABEL = "Not now"
INFERENCE_COVERAGE_WARNING = (
    "Older results will be inferred metadata/version events rather than exact "
    "announcements and may include large arXiv bulk-update dates."
)


class SetupStep(StrEnum):
    CATEGORIES = "categories"
    INITIAL_COVERAGE = "initial_coverage"
    CANDIDATE_CORPUS = "candidate_corpus"
    SEED_PAPERS = "seed_papers"
    KEYWORDS_AND_PHRASES = "keywords_and_phrases"
    AUTHORS = "authors"
    PDF_DESTINATION = "pdf_destination"
    REVIEW = "review"
    DESKTOP_LAUNCHER = "desktop_launcher"


class SetupRoute(StrEnum):
    SETUP = "setup"
    INTERESTS = "interests"


@dataclass(frozen=True, slots=True)
class CategorySelection:
    category: str
    set_spec: str

    def __post_init__(self) -> None:
        category = self.category.strip()
        set_spec = self.set_spec.strip()
        if not category or not set_spec:
            raise ValueError("category and OAI set specification must not be blank")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "set_spec", set_spec)


LauncherChoice = Literal["create", "not_now"]


@dataclass(frozen=True, slots=True)
class SetupDraft:
    schema_version: int
    revision: int
    current_step: SetupStep
    categories: tuple[CategorySelection, ...]
    coverage_start: date | None
    coverage_warning: str | None
    corpus_hash: str | None
    corpus_categories: tuple[str, ...]
    corpus_complete: bool
    corpus_reduced_breadth: bool
    seed_papers: tuple[CandidateDocument, ...]
    keywords: tuple[str, ...]
    phrases: tuple[str, ...]
    authors: tuple[str, ...]
    pdf_destination: PdfDestination | None
    destination_tested: bool
    review_confirmed: bool
    launcher_choice: LauncherChoice | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SetupReview:
    categories: tuple[str, ...]
    coverage_start: date
    seed_papers: tuple[str, ...]
    keywords: tuple[str, ...]
    phrases: tuple[str, ...]
    authors: tuple[str, ...]
    pdf_destination: PdfDestination


@dataclass(frozen=True, slots=True)
class LauncherSettings:
    operation: Literal["none", "create_pending", "create_failed"]
    error_code: str | None

    @property
    def retry_available(self) -> bool:
        return self.operation == "create_failed"


class SetupRevisionError(RuntimeError):
    def __init__(self, expected: int, actual: int | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"setup draft revision mismatch: expected {expected}, actual {actual}"
        )


class SetupStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SetupRecoveryError(RuntimeError):
    """A publication marker cannot safely be reconciled automatically."""


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
        raise ValueError("setup clock must return aware UTC timestamps")
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("stored setup timestamp must be normalized UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _utc_text(parsed) != value:
        raise ValueError("stored setup timestamp must be normalized UTC")
    return parsed


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate setup JSON key: {key}")
        result[key] = value
    return result


def _empty_draft(now: datetime) -> SetupDraft:
    return SetupDraft(
        schema_version=_SCHEMA_VERSION,
        revision=0,
        current_step=SetupStep.CATEGORIES,
        categories=(),
        coverage_start=None,
        coverage_warning=None,
        corpus_hash=None,
        corpus_categories=(),
        corpus_complete=False,
        corpus_reduced_breadth=False,
        seed_papers=(),
        keywords=(),
        phrases=(),
        authors=(),
        pdf_destination=None,
        destination_tested=False,
        review_confirmed=False,
        launcher_choice=None,
        created_at=now,
        updated_at=now,
    )


def _payload_value(draft: SetupDraft) -> dict[str, object]:
    return {
        "authors": list(draft.authors),
        "categories": [
            {"category": item.category, "set_spec": item.set_spec}
            for item in draft.categories
        ],
        "corpus_categories": list(draft.corpus_categories),
        "corpus_complete": draft.corpus_complete,
        "corpus_hash": draft.corpus_hash,
        "corpus_reduced_breadth": draft.corpus_reduced_breadth,
        "coverage_start": (
            None if draft.coverage_start is None else draft.coverage_start.isoformat()
        ),
        "coverage_warning": draft.coverage_warning,
        "destination_tested": draft.destination_tested,
        "keywords": list(draft.keywords),
        "launcher_choice": draft.launcher_choice,
        "pdf_destination": (
            None
            if draft.pdf_destination is None
            else {
                "kind": draft.pdf_destination.kind,
                "path": str(draft.pdf_destination.path),
            }
        ),
        "phrases": list(draft.phrases),
        "review_confirmed": draft.review_confirmed,
        "seed_papers": [_document_value(value) for value in draft.seed_papers],
    }


_PAYLOAD_KEYS = frozenset(_payload_value(_empty_draft(datetime.now(timezone.utc))))


def _encode_payload(draft: SetupDraft) -> str:
    return json.dumps(
        _payload_value(draft), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _require_exact_object(
    value: object, expected: frozenset[str] | set[str], label: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"invalid {label} fields")
    return value


def _decode_row(row: sqlite3.Row) -> SetupDraft:
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        raise ValueError("unsupported setup draft schema version")
    payload = _require_exact_object(
        json.loads(row["payload_json"], object_pairs_hook=_strict_object),
        _PAYLOAD_KEYS,
        "setup payload",
    )
    raw_categories = payload["categories"]
    if not isinstance(raw_categories, list):
        raise ValueError("setup categories must be a list")
    categories = tuple(
        CategorySelection(
            **_require_exact_object(item, {"category", "set_spec"}, "category")
        )
        for item in raw_categories
    )
    coverage = payload["coverage_start"]
    destination = payload["pdf_destination"]
    if destination is not None:
        destination = _require_exact_object(
            destination, {"kind", "path"}, "PDF destination"
        )
        destination_value = PdfDestination(
            destination["kind"], Path(destination["path"])
        )
    else:
        destination_value = None
    launcher_choice = payload["launcher_choice"]
    if launcher_choice not in {None, "create", "not_now"}:
        raise ValueError("invalid launcher choice")
    for field in (
        "corpus_complete",
        "corpus_reduced_breadth",
        "destination_tested",
        "review_confirmed",
    ):
        if type(payload[field]) is not bool:
            raise ValueError(f"{field} must be boolean")
    return SetupDraft(
        schema_version=row["schema_version"],
        revision=row["revision"],
        current_step=SetupStep(row["current_step"]),
        categories=categories,
        coverage_start=None if coverage is None else date.fromisoformat(coverage),
        coverage_warning=payload["coverage_warning"],
        corpus_hash=payload["corpus_hash"],
        corpus_categories=tuple(payload["corpus_categories"]),
        corpus_complete=payload["corpus_complete"],
        corpus_reduced_breadth=payload["corpus_reduced_breadth"],
        seed_papers=tuple(_decode_document(item) for item in payload["seed_papers"]),
        keywords=tuple(payload["keywords"]),
        phrases=tuple(payload["phrases"]),
        authors=tuple(payload["authors"]),
        pdf_destination=destination_value,
        destination_tested=payload["destination_tested"],
        review_confirmed=payload["review_confirmed"],
        launcher_choice=launcher_choice,
        created_at=_parse_utc(row["created_at"]),
        updated_at=_parse_utc(row["updated_at"]),
    )


class SetupService:
    def __init__(
        self,
        database_path: Path | object,
        profile_repository: ProfileRepository,
        *,
        launcher_manager: object | None = None,
        clock: Callable[[], datetime] | None = None,
        crash_injector: Callable[[str], None] | None = None,
        maintenance: MaintenanceBarrier | None = None,
    ) -> None:
        candidate = getattr(database_path, "database_path", database_path)
        if not isinstance(candidate, Path):
            candidate = Path(candidate)
        self.database_path = candidate
        self.profile_repository = profile_repository
        self.launcher_manager = launcher_manager
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.crash_injector = crash_injector or (lambda _phase: None)
        inherited_maintenance = getattr(database_path, "maintenance", None)
        self.maintenance = maintenance or inherited_maintenance

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
        connection._maintenance_lease = lease
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except Exception:
            connection.close()
            raise

    def load_draft(self) -> SetupDraft | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM setup_draft WHERE singleton = 1"
            ).fetchone()
            return None if row is None else _decode_row(row)
        finally:
            connection.close()

    def start(self) -> SetupDraft:
        self.recover()
        if self.profile_repository.load() is not None:
            raise SetupStateError(
                "already_configured", "setup is complete; open Interests"
            )
        current = self.load_draft()
        if current is not None:
            return current
        now = self.clock()
        _utc_text(now)
        draft = _empty_draft(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO setup_draft(
                       singleton, schema_version, revision, current_step,
                       payload_json, created_at, updated_at
                   ) VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(singleton) DO NOTHING""",
                (
                    draft.schema_version,
                    draft.revision,
                    draft.current_step.value,
                    _encode_payload(draft),
                    _utc_text(draft.created_at),
                    _utc_text(draft.updated_at),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        loaded = self.load_draft()
        if loaded is None:
            raise RuntimeError("setup draft was not created")
        return loaded

    def select_categories(
        self,
        expected_revision: int,
        categories: Iterable[CategorySelection | CategoryConfig],
    ) -> SetupDraft:
        normalized = _normalize_categories(categories)
        if not normalized:
            raise SetupStateError("categories_required", "select at least one category")
        current = self._require_revision(expected_revision)
        now = self.clock()
        revised = replace(
            current,
            revision=current.revision + 1,
            current_step=SetupStep.INITIAL_COVERAGE,
            categories=normalized,
            coverage_start=None,
            coverage_warning=None,
            corpus_hash=None,
            corpus_categories=(),
            corpus_complete=False,
            corpus_reduced_breadth=False,
            seed_papers=(),
            keywords=(),
            phrases=(),
            authors=(),
            pdf_destination=None,
            destination_tested=False,
            review_confirmed=False,
            launcher_choice=None,
            updated_at=now,
        )
        self._save_draft(revised, expected_revision=expected_revision)
        return revised

    def set_initial_coverage(
        self,
        expected_revision: int,
        coverage_start: date,
        *,
        earliest_datestamp: date,
    ) -> SetupDraft:
        if (
            type(coverage_start) is not date
            or type(earliest_datestamp) is not date
        ):
            raise TypeError("coverage values must be calendar dates")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.INITIAL_COVERAGE)
        today = self.clock().date()
        if coverage_start < earliest_datestamp:
            raise SetupStateError(
                "coverage_before_earliest",
                "initial coverage cannot precede OAI earliestDatestamp",
            )
        if coverage_start > today:
            raise SetupStateError(
                "coverage_in_future", "initial coverage cannot begin in the future"
            )
        revised = replace(
            current,
            revision=current.revision + 1,
            current_step=SetupStep.CANDIDATE_CORPUS,
            coverage_start=coverage_start,
            coverage_warning=(
                INFERENCE_COVERAGE_WARNING
                if (today - coverage_start).days > 90
                else None
            ),
            updated_at=self.clock(),
        )
        self._save_draft(revised, expected_revision=expected_revision)
        return revised

    def accept_candidate_corpus(
        self,
        expected_revision: int,
        build: CandidateCorpusBuild,
        *,
        corpus_hash: str,
    ) -> SetupDraft:
        if not isinstance(build, CandidateCorpusBuild):
            raise TypeError("build must be a CandidateCorpusBuild")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.CANDIDATE_CORPUS)
        selected = {value.category.casefold() for value in current.categories}
        provided = {value.casefold() for value in build.corpus.categories}
        if selected != provided or len(build.corpus.categories) != len(selected):
            raise SetupStateError(
                "corpus_category_mismatch",
                "candidate corpus belongs to different selected categories",
            )
        actual_hash = candidate_corpus_hash(build.corpus)
        if build.corpus_hash != actual_hash or corpus_hash != actual_hash:
            raise SetupStateError(
                "stale_corpus", "candidate corpus changed; regenerate or retry"
            )
        reduced_ready = (
            build.reduced_breadth and build.minimum_met and not build.complete
        )
        if not build.setup_ready or not (build.complete or reduced_ready):
            raise SetupStateError(
                "corpus_not_ready",
                "candidate corpus is incomplete; resume or retry generation",
            )
        revised = replace(
            current,
            revision=current.revision + 1,
            current_step=SetupStep.SEED_PAPERS,
            corpus_hash=actual_hash,
            corpus_categories=tuple(build.corpus.categories),
            corpus_complete=build.complete,
            corpus_reduced_breadth=reduced_ready,
            updated_at=self.clock(),
        )
        self._save_draft(revised, expected_revision=expected_revision)
        return revised

    # The short alias is convenient for application adapters while retaining
    # the more explicit public name used by the setup domain.
    accept_corpus = accept_candidate_corpus

    def select_seed_papers(
        self,
        expected_revision: int,
        papers: Iterable[CandidateDocument],
    ) -> SetupDraft:
        selected = tuple(papers)
        if any(not isinstance(value, CandidateDocument) for value in selected):
            raise TypeError("seed papers must contain CandidateDocument values")
        ids = tuple(value.paper.arxiv_id for value in selected)
        if len(ids) != len(set(ids)):
            raise ValueError("seed papers must have unique arXiv IDs")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.SEED_PAPERS)
        categories = {value.category for value in current.categories}
        if any(
            not categories.intersection(document.eligible_categories)
            for document in selected
        ):
            raise SetupStateError(
                "seed_category_mismatch",
                "seed paper is not eligible for a selected category",
            )
        return self._advance(
            current,
            expected_revision,
            SetupStep.KEYWORDS_AND_PHRASES,
            seed_papers=selected,
        )

    def select_terms(
        self,
        expected_revision: int,
        *,
        keywords: Iterable[str],
        phrases: Iterable[str],
    ) -> SetupDraft:
        normalized_keywords = _normalize_text_values(keywords, "keywords")
        normalized_phrases = _normalize_text_values(phrases, "phrases")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.KEYWORDS_AND_PHRASES)
        return self._advance(
            current,
            expected_revision,
            SetupStep.AUTHORS,
            keywords=normalized_keywords,
            phrases=normalized_phrases,
        )

    def select_authors(
        self,
        expected_revision: int,
        authors: Iterable[str],
    ) -> SetupDraft:
        normalized = _normalize_text_values(authors, "authors")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.AUTHORS)
        return self._advance(
            current,
            expected_revision,
            SetupStep.PDF_DESTINATION,
            authors=normalized,
        )

    def set_pdf_destination(
        self,
        expected_revision: int,
        destination: PdfDestination,
        *,
        tested: bool,
    ) -> SetupDraft:
        if not isinstance(destination, PdfDestination):
            raise TypeError("destination must be a PdfDestination")
        if type(tested) is not bool or not tested:
            raise SetupStateError(
                "destination_not_tested",
                "PDF destination must pass the folder write test",
            )
        if not destination.path.is_absolute():
            raise SetupStateError(
                "destination_not_absolute", "PDF destination must be absolute"
            )
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.PDF_DESTINATION)
        return self._advance(
            current,
            expected_revision,
            SetupStep.REVIEW,
            pdf_destination=destination,
            destination_tested=True,
        )

    def review_summary(self, draft: SetupDraft | None = None) -> SetupReview:
        current = self.load_draft() if draft is None else draft
        if current is None:
            raise SetupStateError("draft_missing", "setup draft does not exist")
        if current.coverage_start is None or current.pdf_destination is None:
            raise SetupStateError("review_not_ready", "setup review is incomplete")
        return SetupReview(
            categories=tuple(value.category for value in current.categories),
            coverage_start=current.coverage_start,
            seed_papers=tuple(value.paper.arxiv_id for value in current.seed_papers),
            keywords=current.keywords,
            phrases=current.phrases,
            authors=current.authors,
            pdf_destination=current.pdf_destination,
        )

    def confirm_review(self, expected_revision: int) -> SetupDraft:
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.REVIEW)
        # Constructing the review enforces that every reviewed prerequisite is
        # populated before the explicit confirmation is persisted.
        self.review_summary(current)
        return self._advance(
            current,
            expected_revision,
            SetupStep.DESKTOP_LAUNCHER,
            review_confirmed=True,
        )

    def route(self) -> SetupRoute:
        self.recover()
        return (
            SetupRoute.INTERESTS
            if self.profile_repository.load() is not None
            else SetupRoute.SETUP
        )

    def complete(
        self,
        expected_revision: int,
        *,
        launcher_choice: LauncherChoice | None,
    ) -> Profile:
        if launcher_choice is None:
            raise SetupStateError(
                "launcher_choice_required",
                "choose Create desktop launcher or Not now",
            )
        if launcher_choice not in {"create", "not_now"}:
            raise SetupStateError("launcher_choice_invalid", "invalid launcher choice")
        current = self._require_revision(expected_revision)
        self._require_step(current, SetupStep.DESKTOP_LAUNCHER)
        self._validate_completion(current)
        chosen = self._advance(
            current,
            expected_revision,
            SetupStep.DESKTOP_LAUNCHER,
            launcher_choice=launcher_choice,
        )
        assert chosen.coverage_start is not None
        assert chosen.pdf_destination is not None
        profile = Profile(
            schema_version=1,
            revision=1,
            categories=tuple(value.category for value in chosen.categories),
            keywords=chosen.keywords,
            phrases=chosen.phrases,
            authors=chosen.authors,
            seed_papers=tuple(
                value.paper.arxiv_id for value in chosen.seed_papers
            ),
            pdf_destination=chosen.pdf_destination,
        )
        configs = tuple(
            CategoryConfig(value.category, value.set_spec, chosen.coverage_start)
            for value in chosen.categories
        )
        self._publish_profile(
            profile,
            configs,
            seed_papers=chosen.seed_papers,
            expected_revision=None,
            completing_setup=True,
            launcher_choice=launcher_choice,
        )
        if launcher_choice == "create":
            self._attempt_pending_launcher()
        return profile

    def publish_profile(
        self,
        profile: Profile,
        category_configs: Iterable[CategoryConfig],
        *,
        seed_papers: Iterable[CandidateDocument] = (),
        expected_revision: int | None,
    ) -> Profile:
        """Publish an Interests edit through the same recoverable protocol."""

        configs = tuple(category_configs)
        seeds = tuple(seed_papers)
        if tuple(value.category for value in configs) != profile.categories:
            raise ValueError("profile categories must match category configurations")
        if profile.revision != (1 if expected_revision is None else expected_revision + 1):
            raise ValueError("profile revision must immediately follow expected revision")
        self._publish_profile(
            profile,
            configs,
            seed_papers=seeds,
            expected_revision=expected_revision,
            completing_setup=False,
            launcher_choice=None,
        )
        return profile

    def launcher_settings(self) -> LauncherSettings:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT launcher_operation, launcher_last_error_code "
                "FROM application_settings WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("application settings singleton is missing")
            return LauncherSettings(row[0], row[1])
        finally:
            connection.close()

    def retry_launcher(self) -> LauncherSettings:
        current = self.launcher_settings()
        if current.operation != "create_failed":
            raise SetupStateError(
                "launcher_retry_unavailable", "no failed launcher creation to retry"
            )
        self._set_launcher_settings("create_pending", None)
        self._attempt_pending_launcher()
        return self.launcher_settings()

    def dismiss_launcher_failure(self) -> LauncherSettings:
        self._set_launcher_settings("none", None)
        return self.launcher_settings()

    # Settings calls this semantic action "Not now".
    launcher_not_now = dismiss_launcher_failure

    def recover(self) -> None:
        should_install = False
        operation = (
            nullcontext()
            if self.maintenance is None
            else self.maintenance.operation()
        )
        with operation:
            with exclusive_flock(self.profile_repository.lock_path):
                self._reconcile_locked()
                should_install = (
                    self.profile_repository.load() is not None
                    and self.launcher_settings().operation == "create_pending"
                )
        if should_install:
            self._attempt_pending_launcher()

    def _validate_completion(self, draft: SetupDraft) -> None:
        if not draft.categories:
            raise SetupStateError("categories_required", "select at least one category")
        if draft.coverage_start is None:
            raise SetupStateError("coverage_required", "select initial coverage")
        selected = {value.category.casefold() for value in draft.categories}
        corpus = {value.casefold() for value in draft.corpus_categories}
        if (
            draft.corpus_hash is None
            or selected != corpus
            or not (draft.corpus_complete or draft.corpus_reduced_breadth)
        ):
            raise SetupStateError(
                "corpus_not_ready", "accept the current candidate corpus"
            )
        # Empty tuples are valid explicit choices. Reaching this step proves
        # each paper/term/author stage was visited in order.
        _normalize_text_values(draft.keywords, "keywords")
        _normalize_text_values(draft.phrases, "phrases")
        _normalize_text_values(draft.authors, "authors")
        if draft.pdf_destination is None or not draft.destination_tested:
            raise SetupStateError(
                "destination_not_tested", "test the PDF destination"
            )
        if not draft.review_confirmed:
            raise SetupStateError("review_required", "confirm the reviewed profile")

    def _publish_profile(
        self,
        profile: Profile,
        configs: tuple[CategoryConfig, ...],
        *,
        seed_papers: tuple[CandidateDocument, ...],
        expected_revision: int | None,
        completing_setup: bool,
        launcher_choice: LauncherChoice | None,
    ) -> None:
        if not configs:
            raise ValueError("profile publication requires category configurations")
        if len({value.category.casefold() for value in configs}) != len(configs):
            raise ValueError("category configurations must be unique")
        pending_path = self._pending_path
        payload = encode_profile(profile)
        digest = hashlib.sha256(payload).hexdigest()
        operation = (
            nullcontext()
            if self.maintenance is None
            else self.maintenance.operation()
        )
        with operation:
            with exclusive_flock(self.profile_repository.lock_path):
                self._reconcile_locked()
                active = self.profile_repository.load()
                actual = None if active is None else active.revision
                if actual != expected_revision:
                    raise ProfileRevisionError(expected_revision, actual)

                # Phase 1: a complete same-directory pending profile is durable.
                atomic_write(pending_path, payload, mode=0o600)
                self.crash_injector("pending_file_fsynced")

                # Phase 2: all relational state and the exact file identity
                # become durable in one SQLite transaction.
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for config in configs:
                        connection.execute(
                            """INSERT INTO category_sync_state(
                                   category, set_spec, coverage_start
                               ) VALUES (?, ?, ?)
                               ON CONFLICT(category) DO UPDATE SET
                                   set_spec = excluded.set_spec,
                                   coverage_start = excluded.coverage_start""",
                            (
                                config.category,
                                config.oai_set_spec,
                                config.coverage_start.isoformat(),
                            ),
                        )
                    for document in seed_papers:
                        Store._upsert_article(connection, document.paper)
                        Store._upsert_versions(
                            connection, document.paper.arxiv_id, document.versions
                        )
                    connection.execute(
                        """UPDATE profile_publication
                           SET pending_revision = ?, pending_sha256 = ?,
                               status = 'pending'
                           WHERE singleton = 1""",
                        (profile.revision, digest),
                    )
                    if completing_setup:
                        operation_value = (
                            "create_pending"
                            if launcher_choice == "create"
                            else "none"
                        )
                        connection.execute(
                            """UPDATE application_settings
                               SET launcher_operation = ?,
                                   launcher_last_error_code = NULL
                               WHERE singleton = 1""",
                            (operation_value,),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                self.crash_injector("sqlite_pending_committed")

                # Phase 3: publish the exact bytes named by the marker.
                os.replace(pending_path, self.profile_repository.path)
                _fsync_directory(self.profile_repository.path.parent)
                self.crash_injector("profile_replaced")

                # Phase 4: acknowledge publication; setup state is no longer
                # needed once both sides of the logical commit are durable.
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE profile_publication SET status = 'published' "
                        "WHERE singleton = 1 AND pending_revision = ? "
                        "AND pending_sha256 = ?",
                        (profile.revision, digest),
                    )
                    if completing_setup:
                        connection.execute(
                            "DELETE FROM setup_draft WHERE singleton = 1"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
                self.crash_injector("publication_marked")

    @property
    def _pending_path(self) -> Path:
        path = self.profile_repository.path
        return path.with_name(f"{path.name}.pending")

    def _reconcile_locked(self) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT pending_revision, pending_sha256, status "
                "FROM profile_publication WHERE singleton = 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SetupRecoveryError("profile publication marker is missing")
        revision, digest, status = row
        pending_path = self._pending_path
        active_path = self.profile_repository.path
        if status == "none":
            # A crash after phase 1 has no relational side effects. The old
            # active state remains authoritative.
            pending_path.unlink(missing_ok=True)
            return
        if (
            type(revision) is not int
            or revision < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(value not in "0123456789abcdef" for value in digest)
        ):
            raise SetupRecoveryError(
                "profile publication marker is invalid; restore a known-good backup"
            )

        active_matches = _profile_file_matches(active_path, revision, digest)
        pending_matches = _profile_file_matches(pending_path, revision, digest)
        if not active_matches and pending_matches:
            os.replace(pending_path, active_path)
            _fsync_directory(active_path.parent)
            active_matches = True
        if not active_matches:
            raise SetupRecoveryError(
                "profile publication cannot be reconciled automatically; preserve "
                "profile and database files and restore a known-good backup"
            )
        pending_path.unlink(missing_ok=True)
        if status == "pending":
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE profile_publication SET status = 'published' "
                    "WHERE singleton = 1 AND pending_revision = ? "
                    "AND pending_sha256 = ?",
                    (revision, digest),
                )
                connection.execute("DELETE FROM setup_draft WHERE singleton = 1")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _attempt_pending_launcher(self) -> None:
        if self.launcher_settings().operation != "create_pending":
            return
        try:
            if self.launcher_manager is None:
                raise RuntimeError("launcher manager unavailable")
            self.launcher_manager.install()
        except LauncherCollisionError:
            self._set_launcher_settings("create_failed", "launcher_collision")
        except OSError:
            self._set_launcher_settings("create_failed", "launcher_io_error")
        except Exception:
            self._set_launcher_settings("create_failed", "launcher_install_failed")
        else:
            self._set_launcher_settings("none", None)

    def _set_launcher_settings(
        self,
        operation: Literal["none", "create_pending", "create_failed"],
        error_code: str | None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE application_settings
                   SET launcher_operation = ?, launcher_last_error_code = ?
                   WHERE singleton = 1""",
                (operation, error_code),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_revision(self, expected_revision: int) -> SetupDraft:
        current = self.load_draft()
        actual = None if current is None else current.revision
        if current is None or actual != expected_revision:
            raise SetupRevisionError(expected_revision, actual)
        return current

    @staticmethod
    def _require_step(current: SetupDraft, expected: SetupStep) -> None:
        if current.current_step is not expected:
            raise SetupStateError(
                "wrong_step",
                f"setup is at {current.current_step.value}, not {expected.value}",
            )

    def _advance(
        self,
        current: SetupDraft,
        expected_revision: int,
        next_step: SetupStep,
        **changes: object,
    ) -> SetupDraft:
        revised = replace(
            current,
            revision=current.revision + 1,
            current_step=next_step,
            updated_at=self.clock(),
            **changes,
        )
        self._save_draft(revised, expected_revision=expected_revision)
        return revised

    def _save_draft(self, draft: SetupDraft, *, expected_revision: int) -> None:
        _utc_text(draft.updated_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE setup_draft
                   SET schema_version = ?, revision = ?, current_step = ?,
                       payload_json = ?, updated_at = ?
                   WHERE singleton = 1 AND revision = ?""",
                (
                    draft.schema_version,
                    draft.revision,
                    draft.current_step.value,
                    _encode_payload(draft),
                    _utc_text(draft.updated_at),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT revision FROM setup_draft WHERE singleton = 1"
                ).fetchone()
                actual = None if row is None else int(row[0])
                raise SetupRevisionError(expected_revision, actual)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _normalize_categories(
    values: Iterable[CategorySelection | CategoryConfig],
) -> tuple[CategorySelection, ...]:
    result = []
    for value in values:
        if isinstance(value, CategorySelection):
            result.append(value)
        elif isinstance(value, CategoryConfig):
            result.append(CategorySelection(value.category, value.oai_set_spec))
        else:
            raise TypeError("categories must contain CategorySelection values")
    folded = tuple(value.category.casefold() for value in result)
    if len(folded) != len(set(folded)):
        raise ValueError("selected categories must be unique")
    return tuple(result)


def _normalize_text_values(values: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field} must be an iterable of strings")
    original = tuple(values)
    if any(not isinstance(value, str) for value in original):
        raise TypeError(f"{field} must contain strings")
    result = tuple(" ".join(value.split()) for value in original)
    if any(not value for value in result):
        raise ValueError(f"{field} must not contain blank values")
    if len({value.casefold() for value in result}) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _profile_file_matches(path: Path, revision: int, digest: str) -> bool:
    try:
        payload = path.read_bytes()
        profile = decode_profile(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        profile.revision == revision
        and hashlib.sha256(payload).hexdigest() == digest
    )


def _document_value(document: CandidateDocument) -> dict[str, object]:
    paper = document.paper
    return {
        "eligible_categories": list(document.eligible_categories),
        "evidence_dates": [value.isoformat() for value in document.evidence_dates],
        "paper": {
            "abstract": paper.abstract,
            "arxiv_id": paper.arxiv_id,
            "authors": list(paper.authors),
            "categories": list(paper.categories),
            "comments": paper.comments,
            "doi": paper.doi,
            "journal_ref": paper.journal_ref,
            "primary_category": paper.primary_category,
            "title": paper.title,
        },
        "versions": [
            {
                "number": version.number,
                "size": version.size,
                "source_type": version.source_type,
                "submitted_at": version.submitted_at.isoformat(),
            }
            for version in document.versions
        ],
    }


def _decode_document(value: object) -> CandidateDocument:
    # Imported lazily here to keep the public setup model focused on setup state.
    from arxiv_digest.models import PaperMetadata, PaperVersion

    item = _require_exact_object(
        value,
        {"eligible_categories", "evidence_dates", "paper", "versions"},
        "seed paper",
    )
    raw_paper = _require_exact_object(
        item["paper"],
        {
            "abstract",
            "arxiv_id",
            "authors",
            "categories",
            "comments",
            "doi",
            "journal_ref",
            "primary_category",
            "title",
        },
        "seed paper metadata",
    )
    versions = []
    for raw in item["versions"]:
        version = _require_exact_object(
            raw, {"number", "size", "source_type", "submitted_at"}, "seed version"
        )
        versions.append(
            PaperVersion(
                number=version["number"],
                submitted_at=datetime.fromisoformat(version["submitted_at"]),
                size=version["size"],
                source_type=version["source_type"],
            )
        )
    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id=raw_paper["arxiv_id"],
            title=raw_paper["title"],
            authors=tuple(raw_paper["authors"]),
            abstract=raw_paper["abstract"],
            primary_category=raw_paper["primary_category"],
            categories=tuple(raw_paper["categories"]),
            comments=raw_paper["comments"],
            journal_ref=raw_paper["journal_ref"],
            doi=raw_paper["doi"],
        ),
        versions=tuple(versions),
        eligible_categories=tuple(item["eligible_categories"]),
        evidence_dates=tuple(date.fromisoformat(raw) for raw in item["evidence_dates"]),
    )
