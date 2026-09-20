"""Oldest-first review navigation over the durable event ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import ceil
from typing import Callable, Protocol

from arxiv_digest.models import CatchupDayStatus, CategoryConfig, PaperMetadata
from arxiv_digest.profile import Profile
from arxiv_digest.ranking import RankedPaper, rank_date
from arxiv_digest.storage.store import FinishResult, Store, StoreReviewSnapshot
from arxiv_digest.sync import daily_list_coverage_bounds
from arxiv_digest.text import build_tfidf_vectors


PAGE_SIZE = 20

class ProfileLoader(Protocol):
    def load(self) -> Profile | None: ...


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    unreviewed_dates: int
    unreviewed_papers: int
    newly_discovered: int
    oldest_unreviewed_date: date | None
    snapshot_revision: int
    profile_revision: int
    projection_revision: int
    missing_abstracts: int = 0
    waiting_abstract_dates: int = 0
    waiting_abstract_papers: int = 0
    through_date: date | None = None


@dataclass(frozen=True, slots=True)
class ReviewDateSummary:
    day: date
    total_papers: int
    unreviewed_papers: int
    newly_discovered: int
    finished: bool
    abstracts_ready: int = 0
    missing_abstracts: int = 0


@dataclass(frozen=True, slots=True)
class CalendarDateSummary:
    day: date
    total_papers: int | None
    unreviewed_papers: int | None
    newly_discovered: int | None
    finished: bool | None
    retrieval_failed: bool
    abstracts_pending: bool = False
    abstracts_ready: int | None = None
    missing_abstracts: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewPosition:
    day: date
    snapshot_revision: int
    anchor_event_id: int
    profile_revision: int
    projection_revision: int


@dataclass(frozen=True, slots=True)
class ReviewPage:
    day: date
    cards: tuple[RankedPaper, ...]
    snapshot_revision: int
    profile_revision: int
    projection_revision: int
    anchor_event_id: int | None
    previous_anchor_event_id: int | None
    next_anchor_event_id: int | None
    previous_date: date | None
    next_date: date | None
    page_number: int
    page_count: int
    total_cards: int
    last_finished_revision: int | None = None
    abstracts_ready: int = 0
    missing_abstracts: int = 0


def page_from_anchor(
    day: date,
    ranked: tuple[RankedPaper, ...],
    *,
    anchor: int | None,
    page_size: int,
    snapshot_revision: int,
    profile_revision: int,
    projection_revision: int,
    previous_date: date | None,
    next_date: date | None,
    last_finished_revision: int | None = None,
    abstracts_ready: int = 0,
    missing_abstracts: int = 0,
) -> ReviewPage:
    if page_size < 1:
        raise ValueError("review page size must be positive")
    if not ranked:
        raise KeyError(day)
    event_ids = [card.event.event_id for card in ranked]
    if anchor is None:
        start = 0
    else:
        try:
            anchor_index = event_ids.index(anchor)
        except ValueError:
            later = tuple(
                (event_id, index)
                for index, event_id in enumerate(event_ids)
                if event_id > anchor
            )
            anchor_index = (
                min(later)[1] if later else len(ranked) - 1
            )
        start = (anchor_index // page_size) * page_size
    cards = ranked[start : start + page_size]
    page_count = ceil(len(ranked) / page_size)
    page_number = start // page_size + 1
    previous_start = start - page_size
    next_start = start + page_size
    return ReviewPage(
        day=day,
        cards=cards,
        snapshot_revision=snapshot_revision,
        profile_revision=profile_revision,
        projection_revision=projection_revision,
        anchor_event_id=cards[0].event.event_id,
        previous_anchor_event_id=(
            None
            if previous_start < 0
            else ranked[previous_start].event.event_id
        ),
        next_anchor_event_id=(
            None
            if next_start >= len(ranked)
            else ranked[next_start].event.event_id
        ),
        previous_date=previous_date,
        next_date=next_date,
        page_number=page_number,
        page_count=page_count,
        total_cards=len(ranked),
        last_finished_revision=last_finished_revision,
        abstracts_ready=abstracts_ready,
        missing_abstracts=missing_abstracts,
    )


class ReviewService:
    def __init__(
        self,
        store: Store,
        profiles: ProfileLoader,
        *,
        latest_finalized_date: Callable[[], date] | None = None,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self._latest_finalized_date = latest_finalized_date or (
            lambda: daily_list_coverage_bounds(datetime.now(timezone.utc))[1]
        )

    def _require_finalized_date(self, day: date) -> date:
        latest = self._latest_finalized_date()
        if day > latest:
            raise KeyError(day)
        return latest

    def _profile(self) -> Profile:
        profile = self.profiles.load()
        if profile is None:
            raise RuntimeError("an active profile is required")
        return profile

    def _active_configs(
        self, profile: Profile
    ) -> tuple[CategoryConfig, ...]:
        return tuple(
            CategoryConfig(
                item.category,
                self.store.category_sync_state(item.category).set_spec,
                item.coverage_start,
            )
            for item in profile.category_coverage
        )

    def _date_summary(
        self,
        day: date,
        *,
        active_configs: tuple[CategoryConfig, ...],
        through_revision: int | None = None,
    ) -> ReviewDateSummary:
        snapshot = self.store.review_snapshot(
            day,
            through_revision=through_revision,
            active_configs=active_configs,
        )
        return self._summary_from_snapshot(snapshot)

    @staticmethod
    def _summary_from_snapshot(snapshot: StoreReviewSnapshot) -> ReviewDateSummary:
        unreviewed = tuple(
            event for event in snapshot.events if event.reviewed_at is None
        )
        newly_discovered = sum(
            1
            for event in unreviewed
            if snapshot.last_finished_revision is not None
            and event.queue_revision > snapshot.last_finished_revision
        )
        return ReviewDateSummary(
            day=snapshot.day,
            total_papers=len(snapshot.events),
            unreviewed_papers=len(unreviewed),
            newly_discovered=newly_discovered,
            finished=bool(snapshot.events) and not unreviewed,
            abstracts_ready=snapshot.abstracts_ready,
            missing_abstracts=snapshot.missing_abstracts,
        )

    def summary(self) -> ReviewSummary:
        profile = self._profile()
        latest = self._latest_finalized_date()
        active_configs = self._active_configs(profile)
        snapshot_revision, projection_revision = self.store.review_revisions()
        readiness = self.store.review_date_readiness(
            through_revision=snapshot_revision,
            active_configs=active_configs,
            through_date=latest,
        )
        summaries = []
        waiting = []
        missing_abstracts: set[str] = set()
        for value in readiness:
            snapshot = self.store.review_snapshot(
                value.day,
                active_configs=active_configs,
                through_revision=snapshot_revision,
            )
            summary = self._summary_from_snapshot(snapshot)
            summaries.append(summary)
            missing_ids = {
                paper.arxiv_id for paper in snapshot.papers
                if not paper.abstract.strip()
            }
            # The dashboard describes pending review, while date pages and
            # abstract retries also include already-reviewed papers.
            pending_missing_ids = {
                event.arxiv_id for event in snapshot.events
                if event.reviewed_at is None and event.arxiv_id in missing_ids
            }
            if pending_missing_ids:
                waiting.append(summary)
                missing_abstracts.update(pending_missing_ids)
        pending = tuple(
            value for value in summaries if value.unreviewed_papers > 0
        )
        return ReviewSummary(
            unreviewed_dates=len(pending),
            unreviewed_papers=sum(value.unreviewed_papers for value in pending),
            newly_discovered=sum(value.newly_discovered for value in pending),
            oldest_unreviewed_date=(pending[0].day if pending else None),
            snapshot_revision=snapshot_revision,
            profile_revision=profile.revision,
            projection_revision=projection_revision,
            missing_abstracts=len(missing_abstracts),
            waiting_abstract_dates=len(waiting),
            waiting_abstract_papers=sum(
                value.unreviewed_papers for value in waiting
            ),
            through_date=latest,
        )

    def start(self) -> ReviewPage | None:
        oldest = self.summary().oldest_unreviewed_date
        return None if oldest is None else self.open_date(oldest)

    def _ranking_inputs(
        self,
        papers: tuple[PaperMetadata, ...],
        seed_papers: tuple[PaperMetadata, ...],
        saved_papers: tuple[PaperMetadata, ...],
        profile: Profile,
    ) -> tuple[
        dict[str, dict[str, float]],
        dict[str, dict[str, float]],
        dict[str, dict[str, float]],
    ]:
        by_id = {paper.arxiv_id: paper for paper in papers}
        for paper in seed_papers:
            by_id.setdefault(paper.arxiv_id, paper)
        for paper in saved_papers:
            by_id.setdefault(paper.arxiv_id, paper)
        vectors = build_tfidf_vectors(by_id.values(), ngram_range=(1, 3))
        return (
            {
                paper.arxiv_id: vectors[paper.arxiv_id]
                for paper in papers
            },
            {
                arxiv_id: vectors[arxiv_id]
                for arxiv_id in profile.seed_papers
                if arxiv_id in vectors
            },
            {
                paper.arxiv_id: vectors[paper.arxiv_id]
                for paper in saved_papers
                if paper.arxiv_id in vectors
            },
        )

    def open_date(
        self,
        day: date,
        *,
        anchor_event_id: int | None = None,
        from_start: bool = False,
    ) -> ReviewPage:
        profile = self._profile()
        latest = self._require_finalized_date(day)
        active_configs = self._active_configs(profile)
        snapshot = self.store.review_snapshot(
            day,
            seed_ids=profile.seed_papers,
            active_configs=active_configs,
            profile_revision=profile.revision,
        )
        unreviewed_events = tuple(
            event for event in snapshot.events if event.reviewed_at is None
        )
        date_vectors, seed_vectors, saved_vectors = self._ranking_inputs(
            snapshot.papers,
            snapshot.seed_papers,
            snapshot.saved_papers,
            profile,
        )
        ranked_date = rank_date(
            snapshot.events,
            snapshot.papers,
            profile,
            seed_vectors,
            saved_vectors,
            date_vectors=date_vectors,
            reference_titles={
                paper.arxiv_id: paper.title
                for paper in (*snapshot.seed_papers, *snapshot.saved_papers)
            },
        )
        ranked = (
            tuple(
                card for card in ranked_date if card.event.reviewed_at is None
            )
            if unreviewed_events
            else ranked_date
        )
        anchor = (
            None
            if from_start
            else (
                anchor_event_id
                if anchor_event_id is not None
                else snapshot.anchor_event_id
            )
        )
        links = self.store.review_date_links(
            day, active_configs=active_configs, through_date=latest,
        )
        return page_from_anchor(
            day,
            ranked,
            anchor=anchor,
            page_size=PAGE_SIZE,
            snapshot_revision=snapshot.snapshot_revision,
            profile_revision=profile.revision,
            projection_revision=snapshot.projection_revision,
            previous_date=links.previous_date,
            next_date=links.next_date,
            last_finished_revision=snapshot.last_finished_revision,
            abstracts_ready=snapshot.abstracts_ready,
            missing_abstracts=snapshot.missing_abstracts,
        )

    def record_position(
        self,
        day: date,
        *,
        snapshot_revision: int,
        anchor_event_id: int,
        profile_revision: int,
        projection_revision: int,
    ) -> ReviewPosition:
        profile = self._profile()
        self._require_finalized_date(day)
        if profile.revision != profile_revision:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(profile_revision, profile.revision)
        active_configs = self._active_configs(profile)
        position = self.store.record_position(
            day,
            snapshot_revision,
            anchor_event_id,
            profile_revision,
            projection_revision,
            active_configs=active_configs,
        )
        return ReviewPosition(
            day=position.day,
            snapshot_revision=position.snapshot_revision,
            anchor_event_id=position.anchor_event_id,
            profile_revision=position.profile_revision,
            projection_revision=position.projection_revision,
        )

    def previous_date(self, day: date) -> date | None:
        profile = self._profile()
        return self.store.review_date_links(
            day, active_configs=self._active_configs(profile),
            through_date=self._latest_finalized_date(),
        ).previous_date

    def next_date(self, day: date) -> date | None:
        profile = self._profile()
        return self.store.review_date_links(
            day, active_configs=self._active_configs(profile),
            through_date=self._latest_finalized_date(),
        ).next_date

    def next_later_unreviewed_date(self, day: date) -> date | None:
        profile = self._profile()
        active_configs = self._active_configs(profile)
        snapshot_revision, _projection_revision = self.store.review_revisions()
        for candidate in self.store.list_review_dates(
            through_revision=snapshot_revision,
            active_configs=active_configs,
            through_date=self._latest_finalized_date(),
        ):
            if candidate <= day:
                continue
            summary = self._date_summary(
                candidate,
                active_configs=active_configs,
                through_revision=snapshot_revision,
            )
            if summary.unreviewed_papers > 0:
                return candidate
        return None

    def calendar(self, start: date, end: date) -> tuple[CalendarDateSummary, ...]:
        if start > end:
            raise ValueError("calendar start must not follow end")
        profile = self._profile()
        active_configs = self._active_configs(profile)
        end = min(end, self._latest_finalized_date())
        readiness = {
            value.day: value
            for value in self.store.review_date_readiness(
                active_configs=active_configs, through_date=end,
            )
            if start <= value.day <= end
        }
        failed_dates = {
            record.daily_list_date
            for config in active_configs
            for record in self.store.catchup_day_records(config.category)
            if record.status is CatchupDayStatus.FAILED
            and max(start, config.coverage_start) <= record.daily_list_date <= end
        }
        entries = []
        for day in sorted(readiness.keys() | failed_dates):
            summary = (
                self._date_summary(day, active_configs=active_configs)
                if day in readiness else None
            )
            entries.append(
                CalendarDateSummary(
                    day=day,
                    total_papers=None if summary is None else summary.total_papers,
                    unreviewed_papers=(
                        None if summary is None else summary.unreviewed_papers
                    ),
                    newly_discovered=(
                        None if summary is None else summary.newly_discovered
                    ),
                    finished=None if summary is None else summary.finished,
                    retrieval_failed=day in failed_dates,
                    abstracts_pending=summary is not None and summary.missing_abstracts > 0,
                    abstracts_ready=(
                        None if summary is None else summary.abstracts_ready
                    ),
                    missing_abstracts=None if summary is None else summary.missing_abstracts,
                )
            )
        return tuple(entries)

    def finish_date(
        self,
        day: date,
        *,
        through_revision: int,
        profile_revision: int,
        projection_revision: int,
        finished_at: datetime,
    ) -> FinishResult:
        profile = self._profile()
        self._require_finalized_date(day)
        if profile.revision != profile_revision:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(profile_revision, profile.revision)
        return self.store.finish_date(
            day,
            through_revision=through_revision,
            finished_at=finished_at,
            profile_revision=profile_revision,
            projection_revision=projection_revision,
            active_configs=self._active_configs(profile),
        )

    def finish_all(
        self,
        *,
        through_revision: int,
        profile_revision: int,
        projection_revision: int,
        finished_at: datetime,
        through_date: date | None = None,
    ) -> FinishResult:
        profile = self._profile()
        if profile.revision != profile_revision:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(profile_revision, profile.revision)
        latest = self._latest_finalized_date()
        return self.store.finish_all(
            through_revision=through_revision,
            finished_at=finished_at,
            profile_revision=profile_revision,
            projection_revision=projection_revision,
            active_configs=self._active_configs(profile),
            through_date=latest if through_date is None else min(through_date, latest),
        )
