"""Disposable, bounded candidate corpora and explainable local suggestions."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Callable, Literal

from arxiv_digest.atomic import atomic_write
from arxiv_digest.models import (
    OaiArticle,
    OaiTombstone,
    CategoryConfig,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.rate_limit import ArxivRequestCancelled
from arxiv_digest.sources.oai import OaiProtocolError
from arxiv_digest.sources.xml import parse_arxiv_id
from arxiv_digest.text import (
    build_tfidf_vectors,
    extract_ngrams,
    normalize_text,
)


_CACHE_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_STRATA_COUNT = 18


@dataclass(frozen=True, slots=True)
class CandidateDocument:
    paper: PaperMetadata
    versions: tuple[PaperVersion, ...]
    eligible_categories: tuple[str, ...]
    evidence_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.paper, PaperMetadata):
            raise TypeError("paper must be PaperMetadata")
        for field in ("versions", "eligible_categories", "evidence_dates"):
            if not isinstance(getattr(self, field), tuple):
                raise TypeError(f"{field} must be a tuple")
        if any(not isinstance(value, PaperVersion) for value in self.versions):
            raise TypeError("versions must contain PaperVersion values")
        if any(
            previous.number >= current.number
            for previous, current in zip(self.versions, self.versions[1:])
        ):
            raise ValueError("candidate versions must be strictly increasing")
        if not self.eligible_categories:
            raise ValueError("eligible_categories must not be empty")
        if any(not isinstance(value, str) for value in self.eligible_categories):
            raise TypeError("eligible categories must be strings")
        normalized = tuple(value.strip() for value in self.eligible_categories)
        if any(not value for value in normalized):
            raise ValueError("eligible categories must not be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("eligible categories must be unique")
        if any(type(value) is not date for value in self.evidence_dates):
            raise TypeError("evidence_dates must contain dates")
        if tuple(sorted(set(self.evidence_dates))) != self.evidence_dates:
            raise ValueError("evidence_dates must be sorted and unique")


@dataclass(frozen=True, slots=True)
class CandidateCategoryShard:
    schema_version: int
    category: str
    set_spec: str
    window_start: date
    window_end: date
    created_at: datetime
    source_hashes: tuple[str, ...]
    documents: tuple[CandidateDocument, ...]
    completed_strata: tuple[int, ...]
    continuation_by_stratum: tuple[tuple[int, str], ...]
    pages_fetched: int
    exhausted: bool
    # These implementation-state fields make the public per-stratum limits
    # exactly resumable.  Defaults preserve the plan's documented constructor.
    pages_by_stratum: tuple[tuple[int, int], ...] = ()
    accepted_ids_by_stratum: tuple[tuple[int, tuple[str, ...]], ...] = ()
    truncated_strata: tuple[int, ...] = ()
    local_source_hash: str | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported candidate cache schema version")
        if (
            not isinstance(self.category, str)
            or not isinstance(self.set_spec, str)
            or not self.category.strip()
            or not self.set_spec.strip()
        ):
            raise ValueError("candidate shard category and set spec must not be blank")
        if self.window_end - self.window_start != timedelta(days=89):
            raise ValueError("candidate shard window must span 90 calendar days")
        _require_utc(self.created_at, "created_at")
        for field in (
            "source_hashes",
            "documents",
            "completed_strata",
            "continuation_by_stratum",
            "pages_by_stratum",
            "accepted_ids_by_stratum",
            "truncated_strata",
        ):
            if not isinstance(getattr(self, field), tuple):
                raise TypeError(f"{field} must be a tuple")
        if any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in self.source_hashes
        ):
            raise ValueError("source_hashes must contain lowercase SHA-256 values")
        if (
            self.local_source_hash is not None
            and (
                not isinstance(self.local_source_hash, str)
                or _SHA256_RE.fullmatch(self.local_source_hash) is None
            )
        ):
            raise ValueError("local_source_hash must be a lowercase SHA-256 value")
        _require_strata(self.completed_strata, "completed_strata")
        _require_strata(self.truncated_strata, "truncated_strata")
        _require_index_pairs(self.continuation_by_stratum, "continuation_by_stratum")
        _require_index_pairs(self.pages_by_stratum, "pages_by_stratum")
        _require_index_pairs(self.accepted_ids_by_stratum, "accepted_ids_by_stratum")
        if any(
            not isinstance(token, str) or not token
            for _, token in self.continuation_by_stratum
        ):
            raise ValueError("continuation tokens must be nonblank strings")
        if any(
            type(count) is not int or not 1 <= count <= 10
            for _, count in self.pages_by_stratum
        ):
            raise ValueError("per-stratum page counts must be between 1 and 10")
        for _, ids in self.accepted_ids_by_stratum:
            if not isinstance(ids, tuple) or len(ids) > 28 or len(ids) != len(set(ids)):
                raise ValueError("accepted stratum IDs must be a unique tuple of at most 28")
            for arxiv_id in ids:
                if not isinstance(arxiv_id, str):
                    raise ValueError("accepted stratum IDs must be strings")
                base, version = parse_arxiv_id(arxiv_id)
                if version is not None or base != arxiv_id:
                    raise ValueError("accepted stratum IDs must be unversioned")
        if not set(self.truncated_strata).issubset(self.completed_strata):
            raise ValueError("truncated strata must also be completed")
        if set(dict(self.continuation_by_stratum)) & set(self.completed_strata):
            raise ValueError("completed strata cannot retain continuation tokens")
        if type(self.pages_fetched) is not int or self.pages_fetched < 0:
            raise ValueError("pages_fetched must be nonnegative")
        if type(self.exhausted) is not bool:
            raise ValueError("exhausted must be boolean")
        if self.exhausted and (
            self.completed_strata != tuple(range(_STRATA_COUNT))
            or self.truncated_strata
        ):
            raise ValueError("exhausted shards require all strata without truncation")
        if any(not isinstance(document, CandidateDocument) for document in self.documents):
            raise TypeError("documents must contain CandidateDocument values")
        ids = tuple(document.paper.arxiv_id for document in self.documents)
        if len(ids) != len(set(ids)):
            raise ValueError("candidate shard documents must have unique arXiv IDs")


@dataclass(frozen=True, slots=True)
class CandidateCorpus:
    schema_version: int
    categories: tuple[str, ...]
    window_start: date
    window_end: date
    created_at: datetime
    source_hashes: tuple[str, ...]
    documents: tuple[CandidateDocument, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported candidate corpus schema version")
        for field in ("categories", "source_hashes", "documents"):
            if not isinstance(getattr(self, field), tuple):
                raise TypeError(f"{field} must be a tuple")
        if (
            not self.categories
            or any(not isinstance(value, str) or not value.strip() for value in self.categories)
        ):
            raise ValueError("candidate corpus categories must not be empty or blank")
        if len({value.casefold() for value in self.categories}) != len(self.categories):
            raise ValueError("candidate corpus categories must be unique")
        if self.window_end - self.window_start != timedelta(days=89):
            raise ValueError("candidate corpus window must span 90 calendar days")
        _require_utc(self.created_at, "created_at")
        if any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in self.source_hashes
        ):
            raise ValueError("source_hashes must contain lowercase SHA-256 values")
        if len(self.documents) > corpus_limits()[1]:
            raise ValueError("candidate corpus exceeds the global document cap")
        if any(not isinstance(document, CandidateDocument) for document in self.documents):
            raise TypeError("documents must contain CandidateDocument values")
        if any(
            sum(category in document.eligible_categories for document in self.documents)
            > corpus_limits()[0]
            for category in self.categories
        ):
            raise ValueError("candidate corpus exceeds a per-category document cap")
        ids = tuple(document.paper.arxiv_id for document in self.documents)
        if len(ids) != len(set(ids)):
            raise ValueError("candidate corpus documents must have unique arXiv IDs")


@dataclass(frozen=True, slots=True)
class PaperSuggestion:
    paper: PaperMetadata
    score: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_suggestion(self.score, self.reasons)


@dataclass(frozen=True, slots=True)
class TermSuggestion:
    value: str
    kind: Literal["keyword", "phrase"]
    score: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("suggested term must not be blank")
        if self.kind not in {"keyword", "phrase"}:
            raise ValueError("term suggestion kind must be keyword or phrase")
        _validate_suggestion(self.score, self.reasons)


@dataclass(frozen=True, slots=True)
class AuthorSuggestion:
    name: str
    score: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("suggested author must not be blank")
        _validate_suggestion(self.score, self.reasons)


@dataclass(frozen=True, slots=True)
class CandidateSuggestions:
    papers: tuple[PaperSuggestion, ...]
    terms: tuple[TermSuggestion, ...]
    authors: tuple[AuthorSuggestion, ...]

    def __post_init__(self) -> None:
        for field in ("papers", "terms", "authors"):
            if not isinstance(getattr(self, field), tuple):
                raise TypeError(f"{field} must be a tuple")


@dataclass(frozen=True, slots=True)
class CandidateCategoryProgress:
    category: str
    completed_strata: int
    total_strata: int
    documents: int
    pages_fetched: int
    exhausted: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class CandidateCorpusDiagnostics:
    complete: bool
    reduced_breadth: bool
    setup_ready: bool
    minimum_met: bool
    pages_fetched: int
    progress: tuple[CandidateCategoryProgress, ...]
    corpus_hash: str
    can_resume: bool


@dataclass(frozen=True, slots=True)
class CandidateCorpusBuild:
    corpus: CandidateCorpus
    diagnostics: CandidateCorpusDiagnostics

    @property
    def complete(self) -> bool:
        return self.diagnostics.complete

    @property
    def reduced_breadth(self) -> bool:
        return self.diagnostics.reduced_breadth

    @property
    def setup_ready(self) -> bool:
        return self.diagnostics.setup_ready

    @property
    def minimum_met(self) -> bool:
        return self.diagnostics.minimum_met

    @property
    def pages_fetched(self) -> int:
        return self.diagnostics.pages_fetched

    @property
    def progress(self) -> tuple[CandidateCategoryProgress, ...]:
        return self.diagnostics.progress

    @property
    def corpus_hash(self) -> str:
        return self.diagnostics.corpus_hash

    @property
    def can_resume(self) -> bool:
        return self.diagnostics.can_resume


def _validate_suggestion(score: float, reasons: tuple[str, ...]) -> None:
    if not isinstance(score, (int, float)) or not isfinite(score) or score < 0:
        raise ValueError("suggestion score must be finite and nonnegative")
    if not isinstance(reasons, tuple) or not reasons:
        raise ValueError("suggestion reasons must be a nonempty tuple")
    if any(not reason.strip() for reason in reasons):
        raise ValueError("suggestion reasons must not be blank")


class CandidateCacheError(ValueError):
    """A candidate-only cache entry failed bounded, strict validation."""


class CandidateLookupError(ValueError):
    """A manual arXiv seed lookup failed in a user-displayable way."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CandidateBuildCancelled(RuntimeError):
    """A disposable corpus build stopped at a cancellation boundary."""

    code = "cancelled"


class CandidateCache:
    """Atomic disposable cache of exact category/set-spec shards."""

    def __init__(
        self,
        cache_dir: Path | object,
        *,
        clock: Callable[[], datetime] | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        max_age: timedelta = timedelta(days=7),
    ) -> None:
        candidate = getattr(cache_dir, "cache_dir", cache_dir)
        if not isinstance(candidate, Path):
            raise TypeError("cache_dir must be a pathlib.Path or AppPaths")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if not isinstance(max_age, timedelta) or not (
            timedelta(0) < max_age <= timedelta(days=7)
        ):
            raise ValueError("max_age must be positive and at most seven days")
        self.root = candidate / "candidate-corpus"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_bytes = max_bytes
        self.max_age = max_age

    def shard_path(self, category: str, set_spec: str) -> Path:
        key = sha256(f"{category}\0{set_spec}".encode("utf-8")).hexdigest()
        return self.root / "shards" / f"{key}.json"

    def save_shard(self, shard: CandidateCategoryShard) -> None:
        payload = _encode_shard(shard)
        if len(payload) > self.max_bytes:
            raise CandidateCacheError("candidate shard exceeds cache size limit")
        atomic_write(
            self.shard_path(shard.category, shard.set_spec),
            payload,
            mode=0o600,
        )

    def load_shard(
        self,
        category: str,
        set_spec: str,
        *,
        window_start: date | None = None,
        window_end: date | None = None,
        now: datetime | None = None,
    ) -> CandidateCategoryShard | None:
        path = self.shard_path(category, set_spec)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return None
        if size > self.max_bytes:
            raise CandidateCacheError("candidate shard exceeds cache size limit")
        try:
            shard = _decode_shard(path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, CandidateCacheError):
                raise
            raise CandidateCacheError("invalid candidate shard") from error
        if (shard.category, shard.set_spec) != (category, set_spec):
            raise CandidateCacheError("candidate shard key does not match its path")
        if window_start is not None and shard.window_start != window_start:
            return None
        if window_end is not None and shard.window_end != window_end:
            return None
        observed_at = self.clock() if now is None else now
        _require_utc(observed_at, "cache clock")
        age = observed_at - shard.created_at
        if age < timedelta(0) or age > self.max_age:
            return None
        return shard

    def invalidate_shard(self, category: str, set_spec: str) -> None:
        self.shard_path(category, set_spec).unlink(missing_ok=True)

    def derive_corpus(
        self,
        shards: tuple[CandidateCategoryShard, ...],
        *,
        categories: tuple[str, ...] | None = None,
    ) -> CandidateCorpus:
        return derive_candidate_corpus(shards, categories=categories)

    def load_accepted_corpus(
        self,
        categories: Iterable[CategoryConfig],
        *,
        expected_hash: str,
    ) -> CandidateCorpus | None:
        """Hydrate an accepted corpus only when its persisted identity matches."""

        configs = tuple(categories)
        _validate_category_configs(configs)
        if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(
            expected_hash
        ) is None:
            raise ValueError("expected candidate corpus hash must be lowercase SHA-256")
        shards = []
        for config in configs:
            shard = self.load_shard(config.category, config.oai_set_spec)
            if shard is None:
                return None
            shards.append(shard)
        corpus = derive_candidate_corpus(
            tuple(shards),
            categories=tuple(config.category for config in configs),
        )
        return corpus if candidate_corpus_hash(corpus) == expected_hash else None


class CandidateCorpusBuilder:
    """Construct a bounded 90-day suggestion sample with resumable shards."""

    def __init__(
        self,
        source: object,
        cache: CandidateCache,
        *,
        clock: Callable[[], datetime] | None = None,
        local_documents: Iterable[CandidateDocument] = (),
        local_document_provider: Callable[
            [tuple[CategoryConfig, ...], date, date],
            Iterable[CandidateDocument],
        ]
        | None = None,
        page_budget: int = 60,
        time_budget: timedelta = timedelta(minutes=5),
    ) -> None:
        if type(page_budget) is not int or not 1 <= page_budget <= 60:
            raise ValueError("candidate page budget must be between 1 and 60")
        if not isinstance(time_budget, timedelta) or not (
            timedelta(0) < time_budget <= timedelta(minutes=5)
        ):
            raise ValueError("candidate time budget must be at most five minutes")
        self.source = source
        self.cache = cache
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.local_documents = tuple(local_documents)
        if any(not isinstance(value, CandidateDocument) for value in self.local_documents):
            raise TypeError("local_documents must contain CandidateDocument values")
        if local_document_provider is not None and not callable(
            local_document_provider
        ):
            raise TypeError("local document provider must be callable")
        self.local_document_provider = local_document_provider
        self.page_budget = page_budget
        self.time_budget = time_budget

    def build(
        self,
        categories: Iterable[CategoryConfig],
        *,
        accept_reduced_breadth: bool = False,
        restart: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> CandidateCorpusBuild:
        if cancelled is not None and not callable(cancelled):
            raise TypeError("candidate cancellation signal must be callable")

        def check_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise CandidateBuildCancelled("candidate corpus build was cancelled")

        check_cancelled()
        configs = tuple(categories)
        _validate_category_configs(configs)
        started_at = self.clock()
        _require_utc(started_at, "candidate builder clock")

        def request_cancelled() -> bool:
            if cancelled is not None and cancelled():
                return True
            observed = self.clock()
            _require_utc(observed, "candidate builder clock")
            return observed - started_at >= self.time_budget

        window_end = started_at.date()
        window_start = window_end - timedelta(days=89)
        selected = tuple(config.category for config in configs)
        local_documents = (
            self.local_documents
            if self.local_document_provider is None
            else tuple(
                self.local_document_provider(
                    configs,
                    window_start,
                    window_end,
                )
            )
        )
        check_cancelled()
        if any(
            not isinstance(value, CandidateDocument)
            for value in local_documents
        ):
            raise TypeError(
                "local document provider must return CandidateDocument values"
            )
        local_occurrences: dict[str, list[CandidateDocument]] = {}
        for document in local_documents:
            local_occurrences.setdefault(document.paper.arxiv_id, []).append(
                document
            )
        local_by_id = {
            arxiv_id: _merge_candidate_documents(documents, selected)
            for arxiv_id, documents in local_occurrences.items()
        }
        local_documents = tuple(local_by_id.values())

        shards: dict[str, CandidateCategoryShard] = {}
        local_ids_by_category: dict[str, set[str]] = {}
        for config in configs:
            if restart:
                self.cache.invalidate_shard(config.category, config.oai_set_spec)
            local_values = _eligible_local_documents(
                local_documents,
                config.category,
                window_start,
                window_end,
            )
            local_source_hash = _candidate_documents_hash(local_values)
            shard = self.cache.load_shard(
                config.category,
                config.oai_set_spec,
                window_start=window_start,
                window_end=window_end,
                now=started_at,
            )
            if (
                shard is not None
                and shard.local_source_hash != local_source_hash
            ):
                self.cache.invalidate_shard(config.category, config.oai_set_spec)
                shard = None
            local_ids_by_category[config.category] = {
                value.paper.arxiv_id for value in local_values
            }
            if shard is None:
                shard = CandidateCategoryShard(
                    schema_version=_CACHE_SCHEMA_VERSION,
                    category=config.category,
                    set_spec=config.oai_set_spec,
                    window_start=window_start,
                    window_end=window_end,
                    created_at=started_at,
                    source_hashes=(local_source_hash,),
                    documents=tuple(_date_stratified(list(local_values))[:500]),
                    completed_strata=(),
                    continuation_by_stratum=(),
                    pages_fetched=0,
                    exhausted=False,
                    local_source_hash=local_source_hash,
                )
                self.cache.save_shard(shard)
            shards[config.category] = shard

        category_position = {
            config.category: index for index, config in enumerate(configs)
        }
        work_items = [
            (config.category, stratum)
            for stratum in range(_STRATA_COUNT)
            for config in configs
            if stratum not in shards[config.category].completed_strata
        ]
        work_items.sort(
            key=lambda item: (
                dict(shards[item[0]].pages_by_stratum).get(item[1], 0),
                item[1],
                category_position[item[0]],
            )
        )
        work = deque(work_items)
        if accept_reduced_breadth and work:
            cached_corpus = derive_candidate_corpus(
                tuple(shards[config.category] for config in configs),
                categories=selected,
            )
            if _corpus_meets_minimum(cached_corpus):
                # Acceptance is a local decision about the already-visible
                # hash.  It must not silently widen the sample first.
                work.clear()
        config_by_category = {config.category: config for config in configs}
        invocation_pages = 0
        while work and invocation_pages < self.page_budget:
            check_cancelled()
            observed_at = self.clock()
            _require_utc(observed_at, "candidate builder clock")
            if observed_at - started_at >= self.time_budget:
                break
            category, stratum = work.popleft()
            shard = shards[category]
            if stratum in shard.completed_strata:
                continue
            if len(shard.documents) >= corpus_limits()[0]:
                shard = _truncate_unfinished_category(shard)
                shards[category] = shard
                self.cache.save_shard(shard)
                continue

            continuations = dict(shard.continuation_by_stratum)
            try:
                if stratum in continuations:
                    page = self.source.next_page(  # type: ignore[attr-defined]
                        continuations[stratum],
                        cancelled=request_cancelled,
                    )
                else:
                    lower, upper = _stratum_range(window_start, stratum)
                    page = self.source.sample_first_page(  # type: ignore[attr-defined]
                        config_by_category[category].oai_set_spec,
                        lower,
                        upper,
                        cancelled=request_cancelled,
                    )
            except ArxivRequestCancelled as error:
                raise CandidateBuildCancelled(
                    "candidate corpus build was cancelled"
                ) from error
            except OaiProtocolError as error:
                if error.code != "badResumptionToken" or stratum not in continuations:
                    raise
                shard = _reset_expired_stratum(
                    shard,
                    stratum,
                    local_ids_by_category[category],
                )
                shards[category] = shard
                self.cache.save_shard(shard)
                work.append((category, stratum))
                continue

            check_cancelled()
            invocation_pages += 1
            shard = _accept_candidate_page(
                shard,
                stratum,
                page,
                local_by_id=local_by_id,
            )
            shards[category] = shard
            # A successful page and its exact next token are durable before
            # another request is attempted.
            self.cache.save_shard(shard)
            if stratum not in shard.completed_strata:
                work.append((category, stratum))

        ordered_shards = tuple(shards[config.category] for config in configs)
        corpus = derive_candidate_corpus(ordered_shards, categories=selected)
        complete = all(shard.exhausted for shard in ordered_shards)
        category_counts = {
            category: sum(
                category in document.eligible_categories
                for document in corpus.documents
            )
            for category in selected
        }
        minimum_met = _corpus_meets_minimum(corpus)
        reduced_breadth = bool(
            accept_reduced_breadth and minimum_met and not complete
        )
        progress = tuple(
            CandidateCategoryProgress(
                category=shard.category,
                completed_strata=len(shard.completed_strata),
                total_strata=_STRATA_COUNT,
                documents=category_counts[shard.category],
                pages_fetched=shard.pages_fetched,
                exhausted=shard.exhausted,
                truncated=bool(shard.truncated_strata),
            )
            for shard in ordered_shards
        )
        diagnostics = CandidateCorpusDiagnostics(
            complete=complete,
            reduced_breadth=reduced_breadth,
            setup_ready=complete or reduced_breadth,
            minimum_met=minimum_met,
            pages_fetched=invocation_pages,
            progress=progress,
            corpus_hash=candidate_corpus_hash(corpus),
            can_resume=any(
                len(shard.completed_strata) < _STRATA_COUNT
                for shard in ordered_shards
            ),
        )
        return CandidateCorpusBuild(corpus, diagnostics)

    def resume(
        self,
        categories: Iterable[CategoryConfig],
        *,
        accept_reduced_breadth: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> CandidateCorpusBuild:
        return self.build(
            categories,
            accept_reduced_breadth=accept_reduced_breadth,
            cancelled=cancelled,
        )

    def retry(
        self,
        categories: Iterable[CategoryConfig],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> CandidateCorpusBuild:
        return self.build(categories, restart=True, cancelled=cancelled)

    def lookup(self, arxiv_id: str) -> CandidateDocument:
        return lookup_candidate_paper(self.source, arxiv_id)

    lookup_paper = lookup


CandidateBuildResult = CandidateCorpusBuild


def _corpus_meets_minimum(corpus: CandidateCorpus) -> bool:
    return len(corpus.documents) >= 30 and all(
        sum(
            category in document.eligible_categories
            for document in corpus.documents
        )
        >= 5
        for category in corpus.categories
    )


def _validate_category_configs(configs: tuple[CategoryConfig, ...]) -> None:
    if not configs:
        raise ValueError("at least one candidate category is required")
    if any(not isinstance(config, CategoryConfig) for config in configs):
        raise TypeError("candidate categories must be CategoryConfig values")
    categories = tuple(config.category for config in configs)
    if len(categories) != len(set(categories)):
        raise ValueError("candidate categories must be unique")
    keys = tuple((config.category, config.oai_set_spec) for config in configs)
    if len(keys) != len(set(keys)):
        raise ValueError("candidate category/set-spec keys must be unique")


def _eligible_local_documents(
    documents: tuple[CandidateDocument, ...],
    category: str,
    window_start: date,
    window_end: date,
) -> tuple[CandidateDocument, ...]:
    values = []
    for document in documents:
        if category not in document.eligible_categories:
            continue
        projected = replace(document, eligible_categories=(category,))
        if is_candidate_eligible(
            projected,
            window_start=window_start,
            window_end=window_end,
        ):
            values.append(projected)
    return tuple(values)


def _candidate_documents_hash(
    documents: Iterable[CandidateDocument],
) -> str:
    value = [
        _document_value(document)
        for document in sorted(
            documents,
            key=lambda document: document.paper.arxiv_id,
        )
    ]
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _stratum_range(window_start: date, stratum: int) -> tuple[date, date]:
    if not 0 <= stratum < _STRATA_COUNT:
        raise ValueError("invalid candidate stratum")
    lower = window_start + timedelta(days=5 * stratum)
    return lower, lower + timedelta(days=4)


def _accept_candidate_page(
    shard: CandidateCategoryShard,
    stratum: int,
    page: object,
    *,
    local_by_id: dict[str, CandidateDocument],
) -> CandidateCategoryShard:
    documents = {value.paper.arxiv_id: value for value in shard.documents}
    accepted_by = {
        index: list(ids) for index, ids in shard.accepted_ids_by_stratum
    }
    accepted_ids = accepted_by.setdefault(stratum, [])
    remaining = max(0, 28 - len(accepted_ids))
    records = tuple(getattr(page, "records"))
    articles = sorted(
        (record for record in records if isinstance(record, OaiArticle)),
        key=lambda record: record.metadata.arxiv_id,
    )
    for record in articles:
        if remaining <= 0 or len(documents) >= corpus_limits()[0]:
            break
        arxiv_id = record.metadata.arxiv_id
        if arxiv_id in accepted_ids:
            continue
        candidate = CandidateDocument(
            paper=record.metadata,
            versions=record.versions,
            eligible_categories=(shard.category,),
            evidence_dates=(
                ()
                if arxiv_id not in local_by_id
                else local_by_id[arxiv_id].evidence_dates
            ),
        )
        local = local_by_id.get(arxiv_id)
        if local is not None:
            candidate = replace(
                _merge_candidate_documents(
                    [candidate, replace(local, eligible_categories=(shard.category,))],
                    (shard.category,),
                ),
                paper=record.metadata,
            )
        if not is_candidate_eligible(
            candidate,
            window_start=shard.window_start,
            window_end=shard.window_end,
        ):
            continue
        current = documents.get(arxiv_id)
        documents[arxiv_id] = (
            candidate
            if current is None
            else _merge_candidate_documents(
                [current, candidate],
                (shard.category,),
            )
        )
        accepted_ids.append(arxiv_id)
        remaining -= 1

    pages_by = dict(shard.pages_by_stratum)
    pages_by[stratum] = pages_by.get(stratum, 0) + 1
    continuations = dict(shard.continuation_by_stratum)
    continuations.pop(stratum, None)
    completed = set(shard.completed_strata)
    truncated = set(shard.truncated_strata)
    token = getattr(page, "resumption_token")
    natural_end = token is None
    limit_end = not natural_end and (
        pages_by[stratum] >= 10
        or len(accepted_ids) >= 28
        or len(documents) >= corpus_limits()[0]
    )
    if natural_end or limit_end:
        completed.add(stratum)
    if limit_end:
        truncated.add(stratum)
    elif not natural_end:
        if not isinstance(token, str) or not token:
            raise ValueError("candidate continuation token must be opaque nonblank text")
        continuations[stratum] = token
    exhausted = len(completed) == _STRATA_COUNT and not truncated
    digest = getattr(page, "raw_sha256")
    return replace(
        shard,
        source_hashes=shard.source_hashes + (digest,),
        documents=tuple(
            sorted(documents.values(), key=lambda value: value.paper.arxiv_id)
        ),
        completed_strata=tuple(sorted(completed)),
        continuation_by_stratum=tuple(sorted(continuations.items())),
        pages_fetched=shard.pages_fetched + 1,
        exhausted=exhausted,
        pages_by_stratum=tuple(sorted(pages_by.items())),
        accepted_ids_by_stratum=tuple(
            (index, tuple(sorted(set(ids))))
            for index, ids in sorted(accepted_by.items())
        ),
        truncated_strata=tuple(sorted(truncated)),
    )


def _truncate_unfinished_category(
    shard: CandidateCategoryShard,
) -> CandidateCategoryShard:
    unfinished = set(range(_STRATA_COUNT)) - set(shard.completed_strata)
    if not unfinished:
        return shard
    return replace(
        shard,
        completed_strata=tuple(range(_STRATA_COUNT)),
        continuation_by_stratum=(),
        exhausted=False,
        truncated_strata=tuple(sorted(set(shard.truncated_strata) | unfinished)),
    )


def _reset_expired_stratum(
    shard: CandidateCategoryShard,
    stratum: int,
    local_ids: set[str],
) -> CandidateCategoryShard:
    accepted_by = dict(shard.accepted_ids_by_stratum)
    accepted_by.pop(stratum, None)
    retained_ids = set(local_ids)
    for ids in accepted_by.values():
        retained_ids.update(ids)
    continuations = dict(shard.continuation_by_stratum)
    continuations.pop(stratum, None)
    pages_by = dict(shard.pages_by_stratum)
    pages_by.pop(stratum, None)
    return replace(
        shard,
        documents=tuple(
            document
            for document in shard.documents
            if document.paper.arxiv_id in retained_ids
        ),
        completed_strata=tuple(
            value for value in shard.completed_strata if value != stratum
        ),
        continuation_by_stratum=tuple(sorted(continuations.items())),
        exhausted=False,
        pages_by_stratum=tuple(sorted(pages_by.items())),
        accepted_ids_by_stratum=tuple(
            (index, ids) for index, ids in sorted(accepted_by.items())
        ),
        truncated_strata=tuple(
            value for value in shard.truncated_strata if value != stratum
        ),
    )


def candidate_corpus_hash(corpus: CandidateCorpus) -> str:
    """Return a deterministic identity for exact visible corpus contents."""

    value = {
        "categories": list(corpus.categories),
        "documents": [_document_value(document) for document in corpus.documents],
        "schema_version": corpus.schema_version,
        "source_hashes": list(corpus.source_hashes),
        "window_end": corpus.window_end.isoformat(),
        "window_start": corpus.window_start.isoformat(),
    }
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")


def _require_strata(values: tuple[int, ...], field: str) -> None:
    if any(type(value) is not int or not 0 <= value < _STRATA_COUNT for value in values):
        raise ValueError(f"{field} contains an invalid stratum")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{field} must be sorted and unique")


def _require_index_pairs(values: tuple[tuple[int, object], ...], field: str) -> None:
    if any(not isinstance(pair, tuple) or len(pair) != 2 for pair in values):
        raise ValueError(f"{field} must contain two-item tuples")
    indexes = tuple(pair[0] for pair in values)
    _require_strata(indexes, field)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateCacheError(f"duplicate candidate cache key: {key}")
        result[key] = value
    return result


def _require_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CandidateCacheError(f"invalid {label} keys")
    return value


def _paper_value(paper: PaperMetadata) -> dict[str, object]:
    return {
        "abstract": paper.abstract,
        "arxiv_id": paper.arxiv_id,
        "authors": list(paper.authors),
        "categories": list(paper.categories),
        "comments": paper.comments,
        "doi": paper.doi,
        "journal_ref": paper.journal_ref,
        "primary_category": paper.primary_category,
        "title": paper.title,
    }


def _document_value(document: CandidateDocument) -> dict[str, object]:
    return {
        "eligible_categories": list(document.eligible_categories),
        "evidence_dates": [value.isoformat() for value in document.evidence_dates],
        "paper": _paper_value(document.paper),
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


def _encode_shard(shard: CandidateCategoryShard) -> bytes:
    value = {
        "accepted_ids_by_stratum": [
            [index, list(ids)] for index, ids in shard.accepted_ids_by_stratum
        ],
        "category": shard.category,
        "completed_strata": list(shard.completed_strata),
        "continuation_by_stratum": [
            [index, token] for index, token in shard.continuation_by_stratum
        ],
        "created_at": shard.created_at.isoformat(),
        "documents": [_document_value(document) for document in shard.documents],
        "exhausted": shard.exhausted,
        "local_source_hash": shard.local_source_hash,
        "pages_by_stratum": [list(value) for value in shard.pages_by_stratum],
        "pages_fetched": shard.pages_fetched,
        "schema_version": shard.schema_version,
        "set_spec": shard.set_spec,
        "source_hashes": list(shard.source_hashes),
        "truncated_strata": list(shard.truncated_strata),
        "window_end": shard.window_end.isoformat(),
        "window_start": shard.window_start.isoformat(),
    }
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _decode_shard(payload: bytes) -> CandidateCategoryShard:
    value = _require_keys(
        json.loads(payload, object_pairs_hook=_strict_object),
        {
            "accepted_ids_by_stratum",
            "category",
            "completed_strata",
            "continuation_by_stratum",
            "created_at",
            "documents",
            "exhausted",
            "local_source_hash",
            "pages_by_stratum",
            "pages_fetched",
            "schema_version",
            "set_spec",
            "source_hashes",
            "truncated_strata",
            "window_end",
            "window_start",
        },
        "candidate shard",
    )
    return CandidateCategoryShard(
        schema_version=value["schema_version"],  # type: ignore[arg-type]
        category=value["category"],  # type: ignore[arg-type]
        set_spec=value["set_spec"],  # type: ignore[arg-type]
        window_start=date.fromisoformat(value["window_start"]),  # type: ignore[arg-type]
        window_end=date.fromisoformat(value["window_end"]),  # type: ignore[arg-type]
        created_at=datetime.fromisoformat(value["created_at"]),  # type: ignore[arg-type]
        source_hashes=tuple(value["source_hashes"]),  # type: ignore[arg-type]
        documents=tuple(_decode_document(item) for item in value["documents"]),  # type: ignore[union-attr]
        completed_strata=tuple(value["completed_strata"]),  # type: ignore[arg-type]
        continuation_by_stratum=tuple(
            (item[0], item[1]) for item in value["continuation_by_stratum"]  # type: ignore[union-attr]
        ),
        pages_fetched=value["pages_fetched"],  # type: ignore[arg-type]
        exhausted=value["exhausted"],  # type: ignore[arg-type]
        local_source_hash=value["local_source_hash"],  # type: ignore[arg-type]
        pages_by_stratum=tuple(
            (item[0], item[1]) for item in value["pages_by_stratum"]  # type: ignore[union-attr]
        ),
        accepted_ids_by_stratum=tuple(
            (item[0], tuple(item[1]))
            for item in value["accepted_ids_by_stratum"]  # type: ignore[union-attr]
        ),
        truncated_strata=tuple(value["truncated_strata"]),  # type: ignore[arg-type]
    )


def _decode_document(value: object) -> CandidateDocument:
    item = _require_keys(
        value,
        {"eligible_categories", "evidence_dates", "paper", "versions"},
        "candidate document",
    )
    paper_value = _require_keys(
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
        "candidate paper",
    )
    versions = []
    for raw_version in item["versions"]:  # type: ignore[union-attr]
        version = _require_keys(
            raw_version,
            {"number", "size", "source_type", "submitted_at"},
            "candidate version",
        )
        versions.append(
            PaperVersion(
                number=version["number"],  # type: ignore[arg-type]
                submitted_at=datetime.fromisoformat(version["submitted_at"]),  # type: ignore[arg-type]
                size=version["size"],  # type: ignore[arg-type]
                source_type=version["source_type"],  # type: ignore[arg-type]
            )
        )
    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id=paper_value["arxiv_id"],  # type: ignore[arg-type]
            title=paper_value["title"],  # type: ignore[arg-type]
            authors=tuple(paper_value["authors"]),  # type: ignore[arg-type]
            abstract=paper_value["abstract"],  # type: ignore[arg-type]
            primary_category=paper_value["primary_category"],  # type: ignore[arg-type]
            categories=tuple(paper_value["categories"]),  # type: ignore[arg-type]
            comments=paper_value["comments"],  # type: ignore[arg-type]
            journal_ref=paper_value["journal_ref"],  # type: ignore[arg-type]
            doi=paper_value["doi"],  # type: ignore[arg-type]
        ),
        versions=tuple(versions),
        eligible_categories=tuple(item["eligible_categories"]),  # type: ignore[arg-type]
        evidence_dates=tuple(
            date.fromisoformat(raw) for raw in item["evidence_dates"]  # type: ignore[union-attr]
        ),
    )


def is_candidate_eligible(
    document: CandidateDocument,
    *,
    window_start: date,
    window_end: date,
) -> bool:
    """Return whether version or corroborating mailing evidence is in-window."""

    if window_start > window_end:
        raise ValueError("candidate window start must not follow its end")
    version_dates = {version.submitted_at.date() for version in document.versions}
    supported = version_dates | set(document.evidence_dates)
    return any(window_start <= day <= window_end for day in supported)


def corpus_limits() -> tuple[int, int]:
    """Return the public per-category and global disposable-corpus caps."""

    return 500, 2_000


def search_candidate_papers(
    corpus: CandidateCorpus,
    query: str,
    *,
    offset: int = 0,
    limit: int = 30,
) -> tuple[CandidateDocument, ...]:
    """Search disposable candidates by ID, title, or author text."""

    if type(offset) is not int or offset < 0:
        raise ValueError("candidate search offset must be nonnegative")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("candidate search limit must be between 1 and 100")
    needle = normalize_text(query)
    matches = []
    for document in corpus.documents:
        paper = document.paper
        haystack = normalize_text(
            " ".join((paper.arxiv_id, paper.title, *paper.authors))
        )
        if not needle or needle in haystack:
            matches.append(document)
    return tuple(matches[offset : offset + limit])


search_candidates = search_candidate_papers


def build_suggestions(
    corpus: CandidateCorpus,
    accepted_seed_ids: tuple[str, ...],
    accepted_terms: tuple[str, ...],
    accepted_authors: tuple[str, ...],
) -> CandidateSuggestions:
    """Build deterministic suggestions without accepting or persisting any."""

    for value, field in (
        (accepted_seed_ids, "accepted_seed_ids"),
        (accepted_terms, "accepted_terms"),
        (accepted_authors, "accepted_authors"),
    ):
        if not isinstance(value, tuple):
            raise TypeError(f"{field} must be a tuple")
    known_ids = {document.paper.arxiv_id for document in corpus.documents}
    unknown = set(accepted_seed_ids) - known_ids
    if unknown:
        raise ValueError(f"accepted seed IDs are outside the candidate corpus: {sorted(unknown)}")
    vectors = build_tfidf_vectors(corpus.documents, ngram_range=(1, 3))
    return CandidateSuggestions(
        papers=suggest_diverse_papers(corpus, vectors, accepted_seed_ids),
        terms=suggest_distinctive_terms(
            corpus,
            vectors,
            accepted_seed_ids,
            accepted_terms,
        ),
        authors=suggest_diverse_authors(
            corpus,
            vectors,
            accepted_seed_ids,
            accepted_authors,
        ),
    )


def suggest_diverse_papers(
    corpus: CandidateCorpus,
    vectors: dict[str, dict[str, float]],
    accepted_seed_ids: tuple[str, ...],
    *,
    limit: int = 30,
) -> tuple[PaperSuggestion, ...]:
    """Rank related papers, then select round-robin across categories/dates."""

    if not 1 <= limit <= 100:
        raise ValueError("paper suggestion limit must be between 1 and 100")
    seed_set = set(accepted_seed_ids)
    seed_vectors = [vectors[value] for value in accepted_seed_ids if value in vectors]
    scored: dict[str, tuple[float, CandidateDocument]] = {}
    similarities: dict[str, float] = {}
    for document in corpus.documents:
        arxiv_id = document.paper.arxiv_id
        if arxiv_id in seed_set:
            continue
        vector = vectors.get(arxiv_id, {})
        similarity = max(
            (_sparse_dot(vector, seed) for seed in seed_vectors),
            default=0.0,
        )
        distinctiveness = sum(sorted(vector.values(), reverse=True)[:5])
        score = similarity * 4.0 + distinctiveness
        scored[arxiv_id] = (score, document)
        similarities[arxiv_id] = similarity

    queues: dict[str, list[tuple[float, CandidateDocument]]] = {}
    for category in corpus.categories:
        queues[category] = sorted(
            (
                item
                for item in scored.values()
                if category in item[1].eligible_categories
            ),
            key=lambda item: (
                -item[0],
                -_candidate_day(item[1]).toordinal(),
                item[1].paper.arxiv_id,
            ),
        )
    positions = {category: 0 for category in corpus.categories}
    selected: list[PaperSuggestion] = []
    selected_ids: set[str] = set()
    author_counts: Counter[str] = Counter()
    date_counts: Counter[date] = Counter()
    deferred: list[tuple[float, CandidateDocument]] = []
    while len(selected) < limit:
        progressed = False
        for category in corpus.categories:
            queue = queues[category]
            choice: tuple[float, CandidateDocument] | None = None
            while positions[category] < len(queue):
                item = queue[positions[category]]
                positions[category] += 1
                document = item[1]
                if document.paper.arxiv_id in selected_ids:
                    continue
                # Bound repeated-author dominance when alternatives exist.
                if any(author_counts[normalize_text(author)] >= 3 for author in document.paper.authors):
                    deferred.append(item)
                    continue
                choice = item
                break
            if choice is None:
                continue
            score, document = choice
            day = _candidate_day(document)
            adjusted = score / (1.0 + 0.15 * date_counts[day])
            reasons = [f"Adds breadth from {category}"]
            if seed_vectors and similarities[document.paper.arxiv_id] > 0:
                reasons.insert(0, "Textually related to selected seed papers")
            else:
                reasons.insert(0, "Distinctive title and abstract text")
            selected.append(
                PaperSuggestion(document.paper, adjusted, tuple(reasons))
            )
            selected_ids.add(document.paper.arxiv_id)
            date_counts[day] += 1
            author_counts.update(normalize_text(author) for author in document.paper.authors)
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break

    if len(selected) < limit:
        for score, document in sorted(
            deferred,
            key=lambda item: (-item[0], item[1].paper.arxiv_id),
        ):
            if document.paper.arxiv_id in selected_ids:
                continue
            selected.append(
                PaperSuggestion(
                    document.paper,
                    score,
                    ("Distinctive text; included after diversity balancing",),
                )
            )
            selected_ids.add(document.paper.arxiv_id)
            if len(selected) >= limit:
                break
    return tuple(selected)


def suggest_distinctive_terms(
    corpus: CandidateCorpus,
    vectors: dict[str, dict[str, float]],
    accepted_seed_ids: tuple[str, ...],
    accepted_terms: tuple[str, ...],
    *,
    limit_per_kind: int = 12,
) -> tuple[TermSuggestion, ...]:
    """Suggest recurring 1--3 grams absent from accepted seed titles."""

    if not 1 <= limit_per_kind <= 50:
        raise ValueError("term suggestion limit must be between 1 and 50")
    documents_by_id = {
        document.paper.arxiv_id: document for document in corpus.documents
    }
    seed_title_terms = {
        term
        for seed_id in accepted_seed_ids
        if seed_id in documents_by_id
        for term in extract_ngrams(
            documents_by_id[seed_id].paper.title,
            ngram_range=(1, 3),
        )
    }
    excluded = {normalize_text(value) for value in accepted_terms}
    document_frequency: Counter[str] = Counter()
    total_weight: Counter[str] = Counter()
    categories_by_term: dict[str, set[str]] = defaultdict(set)
    for document in corpus.documents:
        vector = vectors.get(document.paper.arxiv_id, {})
        for term, weight in vector.items():
            document_frequency[term] += 1
            total_weight[term] += weight
            categories_by_term[term].update(document.eligible_categories)

    by_kind: dict[str, list[TermSuggestion]] = {"keyword": [], "phrase": []}
    for term in sorted(document_frequency):
        word_count = len(term.split())
        if not 1 <= word_count <= 3:
            continue
        if term in excluded or term in seed_title_terms:
            continue
        # A recurring signal is less likely to be a one-paper proper noun or
        # accidental phrase.  Tiny corpora still get useful local suggestions.
        if len(corpus.documents) > 1 and document_frequency[term] < 2:
            continue
        kind = "keyword" if word_count == 1 else "phrase"
        category_count = len(categories_by_term[term] & set(corpus.categories))
        score = total_weight[term] * (
            1.0 + 0.2 * (document_frequency[term] - 1)
        ) * (1.0 + 0.25 * max(0, category_count - 1))
        reasons = [f"Appears in {document_frequency[term]} candidate papers"]
        if category_count > 1:
            reasons.append(f"Connects {category_count} selected categories")
        by_kind[kind].append(
            TermSuggestion(term, kind, score, tuple(reasons))  # type: ignore[arg-type]
        )
    for values in by_kind.values():
        values.sort(key=lambda item: (-item.score, item.value))
    return tuple(by_kind["keyword"][:limit_per_kind] + by_kind["phrase"][:limit_per_kind])


def suggest_diverse_authors(
    corpus: CandidateCorpus,
    vectors: dict[str, dict[str, float]],
    accepted_seed_ids: tuple[str, ...],
    accepted_authors: tuple[str, ...],
    *,
    limit: int = 20,
) -> tuple[AuthorSuggestion, ...]:
    """Suggest seed coauthors and related recurring cross-category authors."""

    if not 1 <= limit <= 100:
        raise ValueError("author suggestion limit must be between 1 and 100")
    excluded = {normalize_text(value) for value in accepted_authors}
    documents_by_id = {
        document.paper.arxiv_id: document for document in corpus.documents
    }
    seed_documents = [
        documents_by_id[value]
        for value in accepted_seed_ids
        if value in documents_by_id
    ]
    seed_author_keys = {
        normalize_text(author)
        for document in seed_documents
        for author in document.paper.authors
    }
    seed_vectors = [vectors[document.paper.arxiv_id] for document in seed_documents]
    displays: dict[str, str] = {}
    occurrences: Counter[str] = Counter()
    categories: dict[str, set[str]] = defaultdict(set)
    relatedness: Counter[str] = Counter()
    seed_presence: set[str] = set()
    for document in corpus.documents:
        vector = vectors.get(document.paper.arxiv_id, {})
        similarity = max(
            (_sparse_dot(vector, seed) for seed in seed_vectors),
            default=0.0,
        )
        for author in document.paper.authors:
            key = normalize_text(author)
            displays.setdefault(key, " ".join(author.split()))
            occurrences[key] += 1
            categories[key].update(document.eligible_categories)
            relatedness[key] += similarity
            if document in seed_documents:
                seed_presence.add(key)

    suggestions: list[AuthorSuggestion] = []
    for key, display in displays.items():
        if key in excluded:
            continue
        category_count = len(categories[key] & set(corpus.categories))
        score = (
            occurrences[key]
            + 1.5 * max(0, category_count - 1)
            + 3.0 * relatedness[key]
            + (2.0 if key in seed_presence else 0.0)
        )
        reasons = []
        if key in seed_presence:
            reasons.append("Author or coauthor of a selected seed paper")
        if relatedness[key] > 0 and key not in seed_presence:
            reasons.append("Author of textually related candidate papers")
        if category_count > 1:
            reasons.append(f"Recurs across {category_count} selected categories")
        if occurrences[key] > 1 and not reasons:
            reasons.append(f"Appears on {occurrences[key]} candidate papers")
        if not reasons:
            reasons.append("Author of a distinctive candidate paper")
        suggestions.append(AuthorSuggestion(display, score, tuple(reasons)))
    suggestions.sort(
        key=lambda item: (
            -item.score,
            normalize_text(item.name) not in seed_author_keys,
            normalize_text(item.name),
        )
    )
    return tuple(suggestions[:limit])


def _sparse_dot(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(term, 0.0) for term, weight in left.items())


def lookup_candidate_paper(source: object, arxiv_id: str) -> CandidateDocument:
    """Resolve an explicit unversioned seed through OAI GetRecord."""

    try:
        base_id, version = parse_arxiv_id(arxiv_id)
    except (TypeError, ValueError) as error:
        raise CandidateLookupError("invalid_arxiv_id", "Invalid arXiv ID") from error
    if version is not None:
        raise CandidateLookupError(
            "versioned_arxiv_id",
            "Enter an arXiv ID without a version suffix",
        )
    try:
        record = source.get_record(base_id)  # type: ignore[attr-defined]
    except OaiProtocolError as error:
        if error.code in {"idDoesNotExist", "noRecordsMatch"}:
            raise CandidateLookupError(
                "unknown_paper",
                f"arXiv paper {base_id} was not found",
            ) from error
        raise
    if isinstance(record, OaiTombstone):
        raise CandidateLookupError(
            "deleted_paper",
            f"arXiv paper {base_id} has been deleted",
        )
    if not isinstance(record, OaiArticle):
        raise CandidateLookupError("invalid_response", "Invalid arXiv metadata response")
    return CandidateDocument(
        paper=record.metadata,
        versions=record.versions,
        eligible_categories=record.metadata.categories,
        evidence_dates=(),
    )


lookup_manual_paper = lookup_candidate_paper


def validate_custom_arxiv_id(value: str) -> str:
    """Validate a custom seed and return its unversioned base ID."""

    if not isinstance(value, str):
        raise ValueError("arXiv ID must be text")
    try:
        base_id, version = parse_arxiv_id(value)
    except ValueError as error:
        raise ValueError("invalid arXiv ID") from error
    if version is not None:
        raise ValueError("custom arXiv ID must not include a version")
    return base_id


def _custom_text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field} is too long")
    if any(
        unicodedata.category(character) == "Cc" and not character.isspace()
        for character in value
    ):
        raise ValueError(f"{field} contains a control character")
    return normalized


def validate_custom_keyword(value: str) -> str:
    normalized = _custom_text(value, field="keyword", max_length=100)
    if len(normalize_text(normalized).split()) != 1:
        raise ValueError("custom keyword must contain one word")
    return normalized


def validate_custom_phrase(value: str) -> str:
    normalized = _custom_text(value, field="phrase", max_length=200)
    word_count = len(normalize_text(normalized).split())
    if not 2 <= word_count <= 12:
        raise ValueError("custom phrase must contain between 2 and 12 words")
    return normalized


def validate_custom_author(value: str) -> str:
    return _custom_text(value, field="author", max_length=200)


def validate_custom_term(
    value: str,
    *,
    kind: Literal["keyword", "phrase"],
) -> str:
    if kind == "keyword":
        return validate_custom_keyword(value)
    if kind == "phrase":
        return validate_custom_phrase(value)
    raise ValueError("custom term kind must be keyword or phrase")


def derive_candidate_corpus(
    shards: tuple[CandidateCategoryShard, ...],
    *,
    categories: tuple[str, ...] | None = None,
) -> CandidateCorpus:
    """Merge category shards, retaining all labels before deterministic caps."""

    if not isinstance(shards, tuple) or not shards:
        raise ValueError("at least one candidate shard is required")
    selected = categories or tuple(shard.category for shard in shards)
    if not isinstance(selected, tuple) or not selected:
        raise ValueError("candidate categories must be a nonempty tuple")
    by_category = {shard.category: shard for shard in shards}
    if set(by_category) != set(selected) or len(by_category) != len(shards):
        raise ValueError("candidate shards must exactly match selected categories")
    first = shards[0]
    if any(
        (shard.window_start, shard.window_end)
        != (first.window_start, first.window_end)
        for shard in shards
    ):
        raise ValueError("candidate shard windows must match")

    occurrences: dict[str, list[CandidateDocument]] = {}
    for category in selected:
        for document in by_category[category].documents:
            occurrences.setdefault(document.paper.arxiv_id, []).append(document)
    merged = {
        arxiv_id: _merge_candidate_documents(values, selected)
        for arxiv_id, values in occurrences.items()
    }

    per_category_limit, global_limit = corpus_limits()
    category_queues: dict[str, list[CandidateDocument]] = {}
    for category in selected:
        eligible = [
            document
            for document in merged.values()
            if category in document.eligible_categories
        ]
        category_queues[category] = _date_stratified(eligible)[:per_category_limit]

    chosen: list[CandidateDocument] = []
    chosen_ids: set[str] = set()
    category_counts = {category: 0 for category in selected}
    positions = {category: 0 for category in selected}
    while len(chosen) < global_limit:
        progressed = False
        for category in selected:
            queue = category_queues[category]
            while positions[category] < len(queue):
                candidate = queue[positions[category]]
                positions[category] += 1
                if candidate.paper.arxiv_id in chosen_ids:
                    continue
                if any(
                    label in category_counts
                    and category_counts[label] >= per_category_limit
                    for label in candidate.eligible_categories
                ):
                    continue
                chosen.append(candidate)
                chosen_ids.add(candidate.paper.arxiv_id)
                for label in candidate.eligible_categories:
                    if label in category_counts:
                        category_counts[label] += 1
                progressed = True
                break
            if len(chosen) >= global_limit:
                break
        if not progressed:
            break

    return CandidateCorpus(
        schema_version=_CACHE_SCHEMA_VERSION,
        categories=selected,
        window_start=first.window_start,
        window_end=first.window_end,
        created_at=max(shard.created_at for shard in shards),
        source_hashes=tuple(
            sorted({digest for shard in shards for digest in shard.source_hashes})
        ),
        documents=tuple(chosen),
    )


def _merge_candidate_documents(
    documents: list[CandidateDocument],
    category_order: tuple[str, ...],
) -> CandidateDocument:
    papers = sorted(
        (document.paper for document in documents),
        key=lambda paper: (
            paper.arxiv_id,
            paper.title,
            paper.authors,
            paper.abstract,
            paper.categories,
        ),
    )
    versions_by_number: dict[int, PaperVersion] = {}
    for document in documents:
        for version in document.versions:
            current = versions_by_number.get(version.number)
            if current is None or version.submitted_at > current.submitted_at:
                versions_by_number[version.number] = version
    labels = {label for document in documents for label in document.eligible_categories}
    ordered_labels = tuple(label for label in category_order if label in labels) + tuple(
        sorted(labels - set(category_order))
    )
    return CandidateDocument(
        paper=papers[0],
        versions=tuple(versions_by_number[index] for index in sorted(versions_by_number)),
        eligible_categories=ordered_labels,
        evidence_dates=tuple(
            sorted({value for document in documents for value in document.evidence_dates})
        ),
    )


def _candidate_day(document: CandidateDocument) -> date:
    supported = [version.submitted_at.date() for version in document.versions]
    supported.extend(document.evidence_dates)
    return max(supported, default=date.min)


def _date_stratified(documents: list[CandidateDocument]) -> list[CandidateDocument]:
    """Round-robin exact support dates so dense recent days cannot dominate."""

    buckets: dict[date, list[CandidateDocument]] = {}
    for document in documents:
        buckets.setdefault(_candidate_day(document), []).append(document)
    for values in buckets.values():
        values.sort(key=lambda document: document.paper.arxiv_id)
    days = sorted(buckets, reverse=True)
    result: list[CandidateDocument] = []
    offset = 0
    while True:
        added = False
        for day in days:
            values = buckets[day]
            if offset < len(values):
                result.append(values[offset])
                added = True
        if not added:
            return result
        offset += 1
