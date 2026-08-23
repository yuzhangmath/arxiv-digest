from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from arxiv_digest.rate_limit import ArxivRequestCancelled
from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    CategoryConfig,
    CatchupDay,
    Confidence,
    DateBasis,
    EnrichmentStatus,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    OaiArticle,
)
from arxiv_digest.sources.oai import (
    OaiPage,
    OaiProtocolError,
    durable_protocol_error_code,
)
from arxiv_digest.storage.store import (
    CategorySyncRecord,
    EnrichmentDayRecord,
    Store,
    StoredArticleSnapshot,
)


_SOURCE_STRENGTH = {
    EvidenceSource.ATOM: 0,
    EvidenceSource.CATCHUP: 1,
    EvidenceSource.OAI: 2,
}
_MAILING_TIME_ZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class MetadataSyncProgress:
    status: str
    completed_through_utc: date | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class HistoricalBackfillProgress:
    status: str
    pending_start: date | None
    pending_until: date | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class CategoryProgress:
    category: str
    metadata_sync: MetadataSyncProgress
    historical_backfill: HistoricalBackfillProgress
    exact_start: date | None
    exact_end: date | None
    missing_exact_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class SyncReport:
    categories: tuple[CategoryProgress, ...]
    offline: bool
    metadata_complete: bool
    exact_start: date | None
    exact_end: date | None
    missing_exact_dates: tuple[date, ...]


class SyncCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AssociatedEvent:
    arxiv_id: str
    announced_version: int | None
    effective_date: date
    date_basis: DateBasis
    confidence: Confidence
    evidence: tuple[EventEvidence, ...]

    def as_candidates(self) -> tuple[EventCandidate, ...]:
        return tuple(
            EventCandidate(
                arxiv_id=self.arxiv_id,
                announced_version=self.announced_version,
                effective_date=self.effective_date,
                date_basis=self.date_basis,
                evidence=item,
            )
            for item in self.evidence
        )


def associate_evidence(
    candidates: tuple[EventCandidate, ...],
) -> tuple[AssociatedEvent, ...]:
    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            left_versions = {
                candidate.announced_version
                for index, candidate in enumerate(candidates)
                if find(index) == left_root
                and candidate.announced_version is not None
            }
            right_versions = {
                candidate.announced_version
                for index, candidate in enumerate(candidates)
                if find(index) == right_root
                and candidate.announced_version is not None
            }
            if len(left_versions | right_versions) > 1:
                return
            survivor = min(left_root, right_root)
            parents[max(left_root, right_root)] = survivor

    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if left.arxiv_id != right.arxiv_id:
                continue
            if (
                left.announced_version == right.announced_version
                and left.effective_date == right.effective_date
            ):
                union(left_index, right_index)
                continue
            pair = {left.evidence.source, right.evidence.source}
            if pair == {EvidenceSource.ATOM, EvidenceSource.OAI}:
                if (
                    left.announced_version is not None
                    and left.announced_version == right.announced_version
                ):
                    union(left_index, right_index)
            elif pair == {EvidenceSource.ATOM, EvidenceSource.CATCHUP}:
                # Resolve below only after checking all matching Atom versions.
                continue

    for catchup_index, catchup in enumerate(candidates):
        if catchup.evidence.source is not EvidenceSource.CATCHUP:
            continue
        compatible_atoms = [
            index
            for index, atom in enumerate(candidates)
            if atom.evidence.source is EvidenceSource.ATOM
            and atom.arxiv_id == catchup.arxiv_id
            and atom.effective_date == catchup.effective_date
            and atom.evidence.category == catchup.evidence.category
        ]
        atom_versions = {
            candidates[index].announced_version for index in compatible_atoms
        }
        if len(atom_versions) == 1:
            for atom_index in compatible_atoms:
                union(catchup_index, atom_index)

    for catchup_index, catchup in enumerate(candidates):
        if catchup.evidence.source is not EvidenceSource.CATCHUP:
            continue
        compatible = [
            index
            for index, inferred in enumerate(candidates)
            if inferred.evidence.source is EvidenceSource.OAI
            and inferred.arxiv_id == catchup.arxiv_id
            and inferred.effective_date == catchup.effective_date
            and inferred.evidence.category == catchup.evidence.category
            and inferred.announced_version is not None
        ]
        if len(compatible) == 1:
            union(catchup_index, compatible[0])

    groups: dict[int, list[tuple[int, EventCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(find(index), []).append((index, candidate))

    associated: list[tuple[int, AssociatedEvent]] = []
    for values in groups.values():
        first_index = min(index for index, _ in values)
        ordered = sorted(
            (candidate for _, candidate in values),
            key=lambda candidate: (
                _SOURCE_STRENGTH[candidate.evidence.source],
                candidate.evidence.source_key,
            ),
        )
        strongest = ordered[0]
        announced_version = strongest.announced_version
        if announced_version is None:
            announced_version = next(
                (
                    candidate.announced_version
                    for candidate in ordered
                    if candidate.announced_version is not None
                ),
                None,
            )
        associated.append(
            (
                first_index,
                AssociatedEvent(
                    arxiv_id=strongest.arxiv_id,
                    announced_version=announced_version,
                    effective_date=strongest.effective_date,
                    date_basis=strongest.date_basis,
                    confidence=strongest.evidence.confidence,
                    evidence=tuple(candidate.evidence for candidate in ordered),
                ),
            )
        )
    return tuple(value for _, value in sorted(associated, key=lambda item: item[0]))


def diff_oai_article(
    previous: StoredArticleSnapshot | None,
    current: OaiArticle,
    category: str,
    coverage_start: date,
    *,
    set_spec: str | None = None,
    raw_sha256: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[EventCandidate, ...]:
    if raw_sha256 is None or observed_at is None:
        if previous is None:
            raise ValueError("OAI inference requires response provenance")
        raw_sha256 = previous.last_raw_sha256
        observed_at = previous.last_seen_at
    effective_set_spec = category if set_spec is None else set_spec
    known_versions = (
        set() if previous is None else {version.number for version in previous.versions}
    )
    candidates: list[EventCandidate] = []
    for version in current.versions:
        effective_date = version.submitted_at.date()
        if version.number in known_versions or effective_date < coverage_start:
            continue
        announce_type = (
            AnnounceType.NEW if version.number == 1 else AnnounceType.REPLACE
        )
        evidence = EventEvidence(
            source_key=(
                f"oai:{effective_set_spec}:{current.metadata.arxiv_id}:"
                f"v{version.number}"
            ),
            source=EvidenceSource.OAI,
            confidence=Confidence.INFERRED,
            category=category,
            announce_type=announce_type,
            mailing_date=None,
            announced_version=version.number,
            list_position=None,
            oai_datestamp=current.oai_datestamp,
            raw_sha256=raw_sha256,
            observed_at=observed_at,
        )
        candidates.append(
            EventCandidate(
                arxiv_id=current.metadata.arxiv_id,
                announced_version=version.number,
                effective_date=effective_date,
                date_basis=DateBasis.VERSION_HISTORY_UTC,
                evidence=evidence,
            )
        )
    if (
        previous is not None
        and not candidates
        and category != current.metadata.primary_category
        and category in current.metadata.categories
        and category not in previous.observed_categories
        and current.versions
    ):
        supporting_version = current.versions[-1]
        effective_date = supporting_version.submitted_at.date()
        if effective_date >= coverage_start:
            category_set_hash = hashlib.sha256(
                json.dumps(
                    sorted(current.metadata.categories),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            evidence = EventEvidence(
                source_key=(
                    f"oai-category:{category}:{current.metadata.arxiv_id}:"
                    f"{category_set_hash}"
                ),
                source=EvidenceSource.OAI,
                confidence=Confidence.INFERRED,
                category=category,
                announce_type=AnnounceType.CROSS,
                mailing_date=None,
                announced_version=None,
                list_position=None,
                oai_datestamp=current.oai_datestamp,
                raw_sha256=raw_sha256,
                observed_at=observed_at,
            )
            candidates.append(
                EventCandidate(
                    arxiv_id=current.metadata.arxiv_id,
                    announced_version=None,
                    effective_date=effective_date,
                    date_basis=DateBasis.VERSION_HISTORY_UTC,
                    evidence=evidence,
                )
            )
    return tuple(candidates)


class SyncService:
    """Coordinate durable source phases without letting adapters own state."""

    def __init__(
        self,
        store: Store,
        oai_source: object,
        atom_source: object,
        catchup_source: object,
        *,
        clock: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
        cancelled: Callable[[], bool] | None = None,
        catchup_window_days: int = 90,
    ) -> None:
        if catchup_window_days < 1 or catchup_window_days > 90:
            raise ValueError("catch-up window must be between 1 and 90 days")
        self.store = store
        self.oai_source = oai_source
        self.atom_source = atom_source
        self.catchup_source = catchup_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._today = today or (
            lambda: self._now().astimezone(_MAILING_TIME_ZONE).date()
        )
        self._cancelled = cancelled or (lambda: False)
        self.catchup_window_days = catchup_window_days

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("synchronization clock must return UTC")
        return value

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise SyncCancelled("synchronization was cancelled")

    @staticmethod
    def _raise_cancelled(error: Exception) -> None:
        if isinstance(error, SyncCancelled):
            raise error
        if isinstance(error, ArxivRequestCancelled):
            raise SyncCancelled("synchronization was cancelled") from error

    @staticmethod
    def _error(error: Exception, phase: str) -> tuple[str, str]:
        if isinstance(error, (SyncCancelled, ArxivRequestCancelled)):
            return "cancelled", "Synchronization was cancelled."
        if isinstance(error, OaiProtocolError):
            return (
                durable_protocol_error_code(error.code),
                "The arXiv OAI service rejected the request.",
            )
        code = getattr(error, "code", None)
        safe_message = getattr(error, "safe_message", None)
        if isinstance(code, str) and code.strip() and isinstance(
            safe_message, str
        ):
            return code, safe_message
        return f"{phase}_failed", f"The arXiv {phase} phase did not complete."

    @staticmethod
    def _is_expired_token(error: Exception) -> bool:
        return isinstance(error, OaiProtocolError) and error.code in {
            "badResumptionToken",
            "bad_resumption_token",
        }

    def sync(
        self,
        configs: tuple[CategoryConfig, ...],
        *,
        catchup_dates: Mapping[str, Iterable[date]] | None = None,
    ) -> SyncReport:
        if not configs:
            return SyncReport((), False, True, None, None, ())
        if len({config.category for config in configs}) != len(configs):
            raise ValueError("selected categories must be unique")
        for config in configs:
            self.store.ensure_category_state(
                config.category, config.oai_set_spec, config.coverage_start
            )

        network_successes = 0
        network_failures = 0

        # Phase 1: finish each current OAI chain before slower enrichments.
        for config in configs:
            self._check_cancelled()
            if self._sync_incremental(config):
                network_successes += 1
            else:
                network_failures += 1

        # Phase 2: current Atom remains independent of every OAI result.
        for config in configs:
            self._check_cancelled()
            if self._sync_atom(config):
                network_successes += 1
            else:
                network_failures += 1

        # Phase 3: pending bounded history never blocks current checkpoints.
        for config in configs:
            self._check_cancelled()
            state = self.store.category_sync_state(config.category)
            if state.pending_backfill_start is None:
                continue
            if self._sync_backfill(config, state):
                network_successes += 1
            else:
                network_failures += 1

        # Phase 4: best-effort exact mailing evidence is deliberately last.
        for config in configs:
            self._check_cancelled()
            requested = self._eligible_catchup_dates(config, catchup_dates)
            for mailing_date in requested:
                self._check_cancelled()
                if self._sync_catchup_day(config, mailing_date):
                    network_successes += 1
                else:
                    network_failures += 1

        return self.progress(
            configs,
            offline=network_failures > 0 and network_successes == 0,
        )

    def _sync_incremental(self, config: CategoryConfig) -> bool:
        state = self.store.category_sync_state(config.category)
        requested_from = (
            state.coverage_start
            if state.completed_through_utc is None
            else max(
                state.coverage_start,
                state.completed_through_utc - timedelta(days=1),
            )
        )
        restarted = False
        while True:
            run_id = self.store.begin_sync_run(
                config.category,
                "incremental",
                requested_from,
                None,
                self._now(),
            )
            try:
                self._check_cancelled()
                page = self.oai_source.first_page(
                    config.oai_set_spec,
                    requested_from,
                    cancelled=self._cancelled,
                )
                final_response_at = self._apply_oai_chain(
                    run_id,
                    config,
                    page,
                    coverage_start=state.coverage_start,
                    coverage_end=None,
                )
                completed_at = self._now()
                self.store.complete_incremental_run(
                    run_id,
                    final_response_at.date(),
                    final_response_at,
                    completed_at,
                )
                return True
            except Exception as error:
                code, message = self._error(error, "metadata synchronization")
                self.store.fail_sync_run(run_id, code, message, self._now())
                self._raise_cancelled(error)
                if self._is_expired_token(error) and not restarted:
                    restarted = True
                    continue
                return False

    def _apply_oai_chain(
        self,
        run_id: int,
        config: CategoryConfig,
        first_page: OaiPage,
        *,
        coverage_start: date,
        coverage_end: date | None,
    ) -> datetime:
        page = first_page
        while True:
            self._check_cancelled()
            identifiers = {
                record.metadata.arxiv_id
                for record in page.records
                if isinstance(record, OaiArticle)
            }
            previous = {
                snapshot.metadata.arxiv_id: snapshot
                for snapshot in self.store.article_snapshots(
                    config.category, identifiers
                )
            }
            candidates: list[EventCandidate] = []
            for record in page.records:
                self._check_cancelled()
                if not isinstance(record, OaiArticle):
                    continue
                inferred = diff_oai_article(
                    previous.get(record.metadata.arxiv_id),
                    record,
                    config.category,
                    coverage_start,
                    set_spec=config.oai_set_spec,
                    raw_sha256=page.raw_sha256,
                    observed_at=page.response_date,
                )
                candidates.extend(
                    candidate
                    for candidate in inferred
                    if coverage_end is None
                    or candidate.effective_date <= coverage_end
                )
            self._check_cancelled()
            self.store.apply_oai_page(
                run_id,
                config.category,
                page.records,
                tuple(candidates),
                page.raw_sha256,
                page.response_date,
            )
            if page.resumption_token is None:
                return page.response_date
            self._check_cancelled()
            page = self.oai_source.next_page(
                page.resumption_token,
                cancelled=self._cancelled,
            )

    @staticmethod
    def _atom_candidate(batch: AtomBatch, entry: AtomEntry) -> EventCandidate:
        evidence = EventEvidence(
            source_key=(
                f"atom:{batch.category}:{entry.mailing_date}:"
                f"{entry.metadata.arxiv_id}:v{entry.version.number}:"
                f"{entry.announce_type.value}"
            ),
            source=EvidenceSource.ATOM,
            confidence=Confidence.CURRENT,
            category=batch.category,
            announce_type=entry.announce_type,
            mailing_date=entry.mailing_date,
            announced_version=entry.version.number,
            list_position=entry.position,
            oai_datestamp=None,
            raw_sha256=batch.raw_sha256,
            observed_at=batch.fetched_at,
        )
        return EventCandidate(
            arxiv_id=entry.metadata.arxiv_id,
            announced_version=entry.version.number,
            effective_date=entry.mailing_date,
            date_basis=DateBasis.FEED_MAILING,
            evidence=evidence,
        )

    def _sync_atom(self, config: CategoryConfig) -> bool:
        try:
            self._check_cancelled()
            batch = self.atom_source.fetch(
                config.category,
                cancelled=self._cancelled,
            )
            self._check_cancelled()
            if batch.category != config.category:
                raise ValueError("Atom category does not match the request")
            for entry in batch.entries:
                self._check_cancelled()
                candidate = self._atom_candidate(batch, entry)
                self.store.apply_event_batch(
                    entry.metadata, (entry.version,), (candidate,)
                )
            self.store.record_enrichment_day(
                EnrichmentDayRecord(
                    category=config.category,
                    mailing_date=batch.mailing_date,
                    source="atom",
                    status=(
                        EnrichmentStatus.COMPLETE
                        if batch.entries
                        else EnrichmentStatus.EMPTY
                    ),
                    fetched_at=batch.fetched_at,
                    raw_sha256=batch.raw_sha256,
                )
            )
            return True
        except Exception as error:
            self._raise_cancelled(error)
            code, message = self._error(error, "Atom")
            self.store.record_enrichment_day(
                EnrichmentDayRecord(
                    category=config.category,
                    mailing_date=self._today(),
                    source="atom",
                    status=EnrichmentStatus.FAILED,
                    fetched_at=self._now(),
                    error_code=code,
                    error_message=message,
                )
            )
            return False

    def extend_coverage(self, category: str, new_start: date) -> CategorySyncRecord:
        state = self.store.category_sync_state(category)
        if new_start >= state.coverage_start:
            if new_start == state.coverage_start:
                return state
            raise ValueError("coverage can only be extended earlier")
        if (
            state.pending_backfill_start is not None
            and new_start >= state.pending_backfill_start
        ):
            return state
        identify = self.oai_source.identify(cancelled=self._cancelled)
        if new_start < identify.earliest_datestamp:
            raise ValueError("requested coverage predates the OAI repository")
        self.store.set_pending_backfill(
            category, new_start, state.coverage_start
        )
        return self.store.category_sync_state(category)

    def _sync_backfill(
        self, config: CategoryConfig, state: CategorySyncRecord
    ) -> bool:
        pending_start = state.pending_backfill_start
        pending_until = state.pending_backfill_until
        if pending_start is None or pending_until is None:
            raise ValueError("pending backfill interval is incomplete")

        # Re-evaluate durable histories first, retaining original provenance.
        try:
            for snapshot in self.store.article_snapshots(config.category):
                self._check_cancelled()
                reconstructed = OaiArticle(
                    oai_identifier=f"oai:arXiv.org:{snapshot.metadata.arxiv_id}",
                    oai_datestamp=snapshot.last_oai_datestamp,
                    set_specs=(config.oai_set_spec,),
                    metadata=snapshot.metadata,
                    versions=snapshot.versions,
                )
                candidates = tuple(
                    candidate
                    for candidate in diff_oai_article(
                        None,
                        reconstructed,
                        config.category,
                        pending_start,
                        set_spec=config.oai_set_spec,
                        raw_sha256=snapshot.last_raw_sha256,
                        observed_at=snapshot.last_seen_at,
                    )
                    if candidate.effective_date <= pending_until
                )
                if candidates:
                    self.store.apply_event_batch(
                        snapshot.metadata, snapshot.versions, candidates
                    )
        except Exception as error:
            self._raise_cancelled(error)
            run_id = self.store.begin_sync_run(
                config.category,
                "coverage_backfill",
                pending_start,
                pending_until,
                self._now(),
            )
            code, message = self._error(error, "historical backfill")
            self.store.fail_sync_run(run_id, code, message, self._now())
            return False

        run_id = self.store.begin_sync_run(
            config.category,
            "coverage_backfill",
            pending_start,
            pending_until,
            self._now(),
        )
        try:
            self._check_cancelled()
            page = self.oai_source.backfill_first_page(
                config.oai_set_spec,
                pending_start,
                pending_until,
                cancelled=self._cancelled,
            )
            final_response_at = self._apply_oai_chain(
                run_id,
                config,
                page,
                coverage_start=pending_start,
                coverage_end=pending_until,
            )
            self.store.complete_backfill_run(
                run_id, pending_start, final_response_at, self._now()
            )
            return True
        except Exception as error:
            code, message = self._error(error, "historical backfill")
            self.store.fail_sync_run(run_id, code, message, self._now())
            self._raise_cancelled(error)
            return False

    def _eligible_catchup_dates(
        self,
        config: CategoryConfig,
        supplied: Mapping[str, Iterable[date]] | None,
    ) -> tuple[date, ...]:
        today = self._today()
        earliest = today - timedelta(days=self.catchup_window_days - 1)
        if supplied is None:
            start = max(config.coverage_start, earliest)
            requested = (
                start + timedelta(days=offset)
                for offset in range((today - start).days + 1)
            )
        else:
            requested = supplied.get(config.category, ())
        successful = {
            record.mailing_date
            for record in self.store.enrichment_records(config.category)
            if record.status in {EnrichmentStatus.COMPLETE, EnrichmentStatus.EMPTY}
        }
        return tuple(
            day
            for day in sorted(set(requested))
            if earliest <= day <= today and day not in successful
        )

    @staticmethod
    def _catchup_hash(day: CatchupDay) -> str | None:
        if not day.pages:
            return None
        if len(day.pages) == 1:
            return day.pages[0].raw_sha256
        digest = hashlib.sha256()
        for page in sorted(day.pages, key=lambda value: value.page):
            digest.update(page.raw_sha256.encode("ascii"))
        return digest.hexdigest()

    def _sync_catchup_day(
        self, config: CategoryConfig, mailing_date: date
    ) -> bool:
        try:
            self._check_cancelled()
            result = self.catchup_source.fetch_day(
                config.category,
                mailing_date,
                cancelled=self._cancelled,
            )
            self._check_cancelled()
            if (
                result.category != config.category
                or result.mailing_date != mailing_date
            ):
                raise ValueError("catch-up response does not match the request")
            for page in sorted(result.pages, key=lambda value: value.page):
                self._check_cancelled()
                for entry in page.entries:
                    self._check_cancelled()
                    evidence = EventEvidence(
                        source_key=(
                            f"catchup:{config.category}:{mailing_date}:"
                            f"{entry.metadata.arxiv_id}:{entry.section.value}"
                        ),
                        source=EvidenceSource.CATCHUP,
                        confidence=Confidence.RECOVERED,
                        category=config.category,
                        announce_type=entry.section,
                        mailing_date=mailing_date,
                        announced_version=None,
                        list_position=entry.position,
                        oai_datestamp=None,
                        raw_sha256=page.raw_sha256,
                        observed_at=self._now(),
                    )
                    candidate = EventCandidate(
                        arxiv_id=entry.metadata.arxiv_id,
                        announced_version=None,
                        effective_date=mailing_date,
                        date_basis=DateBasis.CATCHUP_MAILING,
                        evidence=evidence,
                    )
                    self.store.apply_event_batch(
                        entry.metadata, (), (candidate,)
                    )
            self.store.record_enrichment_day(
                EnrichmentDayRecord(
                    category=config.category,
                    mailing_date=mailing_date,
                    source="catchup",
                    status=result.status,
                    fetched_at=self._now(),
                    raw_sha256=self._catchup_hash(result),
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
            )
            return result.status is not EnrichmentStatus.FAILED
        except Exception as error:
            self._raise_cancelled(error)
            code, message = self._error(error, "catch-up")
            self.store.record_enrichment_day(
                EnrichmentDayRecord(
                    category=config.category,
                    mailing_date=mailing_date,
                    source="catchup",
                    status=EnrichmentStatus.FAILED,
                    fetched_at=self._now(),
                    error_code=code,
                    error_message=message,
                )
            )
            return False

    def progress(
        self, configs: tuple[CategoryConfig, ...], *, offline: bool = False
    ) -> SyncReport:
        values: list[CategoryProgress] = []
        exact_sets: list[set[date]] = []
        for config in configs:
            state = self.store.category_sync_state(config.category)
            backfill = self.store.latest_sync_run(
                config.category, "coverage_backfill"
            )
            records = self.store.enrichment_records(config.category)
            observed = {record.mailing_date for record in records}
            exact = {
                record.mailing_date
                for record in records
                if record.status
                in {EnrichmentStatus.COMPLETE, EnrichmentStatus.EMPTY}
            }
            if exact:
                exact_start = min(exact)
                exact_end = max(exact)
                failed = tuple(
                    exact_start + timedelta(days=offset)
                    for offset in range((exact_end - exact_start).days + 1)
                    if exact_start + timedelta(days=offset) not in exact
                )
            else:
                exact_start = None
                exact_end = None
                failed = tuple(sorted(observed))
            if state.pending_backfill_start is None:
                backfill_status = "idle"
                backfill_error_code = None
                backfill_error_message = None
            elif backfill is None:
                backfill_status = "pending"
                backfill_error_code = None
                backfill_error_message = None
            else:
                backfill_status = backfill.status
                backfill_error_code = backfill.error_code
                backfill_error_message = backfill.error_message
            values.append(
                CategoryProgress(
                    category=config.category,
                    metadata_sync=MetadataSyncProgress(
                        status=state.status,
                        completed_through_utc=state.completed_through_utc,
                        last_error_code=state.last_error_code,
                        last_error_message=state.last_error_message,
                    ),
                    historical_backfill=HistoricalBackfillProgress(
                        status=backfill_status,
                        pending_start=state.pending_backfill_start,
                        pending_until=state.pending_backfill_until,
                        last_error_code=backfill_error_code,
                        last_error_message=backfill_error_message,
                    ),
                    exact_start=exact_start,
                    exact_end=exact_end,
                    missing_exact_dates=failed,
                )
            )
            exact_sets.append(exact)

        global_exact = set.intersection(*exact_sets) if exact_sets else set()
        if global_exact:
            global_start = min(global_exact)
            global_end = max(global_exact)
            missing = tuple(
                global_start + timedelta(days=offset)
                for offset in range((global_end - global_start).days + 1)
                if not all(
                    global_start + timedelta(days=offset) in values
                    for values in exact_sets
                )
            )
        else:
            global_start = None
            global_end = None
            missing = ()
        return SyncReport(
            categories=tuple(values),
            offline=offline,
            metadata_complete=all(
                value.metadata_sync.status == "idle"
                and value.metadata_sync.completed_through_utc is not None
                for value in values
            ),
            exact_start=global_start,
            exact_end=global_end,
            missing_exact_dates=missing,
        )
