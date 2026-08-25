"""Oldest-first review navigation over the durable event ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import ceil
from typing import Protocol

from arxiv_digest.models import CategoryConfig, PaperMetadata
from arxiv_digest.profile import Profile
from arxiv_digest.ranking import RankedPaper, rank_date
from arxiv_digest.storage.store import FinishResult, Store
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


@dataclass(frozen=True, slots=True)
class ReviewDateSummary:
    day: date
    total_papers: int
    unreviewed_papers: int
    newly_discovered: int
    finished: bool


@dataclass(frozen=True, slots=True)
class ReviewDateLinks:
    previous_date: date | None
    next_date: date | None
    next_unreviewed_date: date | None


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
    next_unreviewed_date: date | None
    page_number: int
    page_count: int
    total_cards: int
    last_finished_revision: int | None = None


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
    next_unreviewed_date: date | None,
    last_finished_revision: int | None = None,
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
        next_unreviewed_date=next_unreviewed_date,
        page_number=page_number,
        page_count=page_count,
        total_cards=len(ranked),
        last_finished_revision=last_finished_revision,
    )


class ReviewService:
    def __init__(self, store: Store, profiles: ProfileLoader) -> None:
        self.store = store
        self.profiles = profiles

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
            day=day,
            total_papers=len(snapshot.events),
            unreviewed_papers=len(unreviewed),
            newly_discovered=newly_discovered,
            finished=bool(snapshot.events) and not unreviewed,
        )

    def summary(self) -> ReviewSummary:
        profile = self._profile()
        active_configs = self._active_configs(profile)
        snapshot_revision, projection_revision = self.store.review_revisions()
        summaries = tuple(
            self._date_summary(
                day,
                active_configs=active_configs,
                through_revision=snapshot_revision,
            )
            for day in self.store.list_review_dates(
                through_revision=snapshot_revision,
                active_configs=active_configs,
            )
        )
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
    ) -> ReviewPage:
        profile = self._profile()
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
        )
        ranked = (
            tuple(
                card for card in ranked_date if card.event.reviewed_at is None
            )
            if unreviewed_events
            else ranked_date
        )
        anchor = (
            anchor_event_id
            if anchor_event_id is not None
            else snapshot.anchor_event_id
        )
        links = self.store.review_date_links(
            day, active_configs=active_configs
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
            next_unreviewed_date=self.summary().oldest_unreviewed_date,
            last_finished_revision=snapshot.last_finished_revision,
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
            day, active_configs=self._active_configs(profile)
        ).previous_date

    def next_date(self, day: date) -> date | None:
        profile = self._profile()
        return self.store.review_date_links(
            day, active_configs=self._active_configs(profile)
        ).next_date

    def next_unreviewed(self) -> ReviewPage | None:
        day = self.summary().oldest_unreviewed_date
        return None if day is None else self.open_date(day)

    def calendar(self, start: date, end: date) -> tuple[ReviewDateSummary, ...]:
        if start > end:
            raise ValueError("calendar start must not follow end")
        profile = self._profile()
        active_configs = self._active_configs(profile)
        return tuple(
            self._date_summary(day, active_configs=active_configs)
            for day in self.store.list_review_dates(
                active_configs=active_configs
            )
            if start <= day <= end
        )

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
    ) -> FinishResult:
        profile = self._profile()
        if profile.revision != profile_revision:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(profile_revision, profile.revision)
        return self.store.finish_all(
            through_revision=through_revision,
            finished_at=finished_at,
            profile_revision=profile_revision,
            projection_revision=projection_revision,
            active_configs=self._active_configs(profile),
        )
