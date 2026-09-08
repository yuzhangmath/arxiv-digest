from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isclose
from pathlib import Path

import pytest

from arxiv_digest.models import (
    AnnounceType,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    EvidenceSource,
    PaperMetadata,
    PaperVersion,
    SourceObservation,
)
from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory
from arxiv_digest.review import ReviewService
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


@dataclass
class Profiles:
    profile: Profile

    def load(self) -> Profile:
        return self.profile


def _profile(
    tmp_path: Path,
    revision: int = 1,
    *,
    keywords: tuple[str, ...] = (),
    seed_papers: tuple[str, ...] = (),
) -> Profile:
    return Profile(
        schema_version=2,
        revision=revision,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=keywords,
        phrases=(),
        authors=(),
        seed_papers=seed_papers,
        pdf_destination=PdfDestination("downloads", tmp_path / "pdfs"),
    )


def _add(
    store: Store,
    number: int,
    day: date,
    *,
    source: EvidenceSource = EvidenceSource.CATCHUP,
    version: int | None = 1,
    title: str | None = None,
) -> int:
    arxiv_id = f"2608.{number:05d}"
    metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title=title or f"Synthetic review paper {number}",
        authors=(f"Author {number}",),
        abstract="A neutral deterministic review fixture.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    if source is not EvidenceSource.CATCHUP:
        raise ValueError("Review fixtures require catch-up support")
    existing = tuple(
        observation
        for observation in store.source_observations()
        if observation.source is EvidenceSource.CATCHUP
        and observation.category == "cs.SE"
        and observation.daily_list_date == day
    )
    observation = SourceObservation(
        source_key=f"catchup:cs.SE:{day}:{arxiv_id}:{number}",
        arxiv_id=arxiv_id,
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=day,
        announced_version=None,
        list_position=number,
        oai_datestamp=None,
        response_sha256=f"{number % 16:x}" * 64,
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    observations = tuple(
        sorted((*existing, observation), key=lambda item: item.list_position or 0)
    )
    entries = tuple(
        CatchupEntry(
            metadata=(
                metadata
                if item.arxiv_id == arxiv_id
                else store.article_metadata(item.arxiv_id)
            ),
            section=item.announce_type or AnnounceType.NEW,
            mailing_date=day,
            position=item.list_position or 0,
        )
        for item in observations
    )
    result = CatchupDay(
        category="cs.SE",
        mailing_date=day,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=day,
                page=1,
                total_pages=1,
                entries=entries,
                raw_sha256="f" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    return next(
        event.event_id
        for event in store.apply_catchup_day(
            result,
            observations,
            datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        if event.arxiv_id == arxiv_id
    )


@pytest.fixture
def review(tmp_path: Path) -> tuple[Store, Profiles, ReviewService]:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 7, 1))
    profiles = Profiles(_profile(tmp_path))
    return store, profiles, ReviewService(store, profiles)


def test_oldest_first_finish_and_later_discovery(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    first_day = date(2026, 7, 31)
    second_day = date(2026, 8, 3)
    first_id = _add(store, 1, first_day)
    _add(store, 2, second_day)

    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (2, 2)
    assert summary.profile_revision == profiles.profile.revision
    assert summary.projection_revision == 0
    assert service.start().day == first_day
    page = service.open_date(first_day)
    service.finish_date(
        first_day,
        through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    assert service.summary().oldest_unreviewed_date == second_day
    assert store.review_event(first_id).reviewed_at is not None
    completed = service.open_date(first_day)
    assert [card.event.event_id for card in completed.cards] == [first_id]

    late_id = _add(store, 3, first_day)

    reopened = service.summary()
    assert reopened.oldest_unreviewed_date == first_day
    assert reopened.newly_discovered == 1
    active = service.open_date(first_day)
    assert [card.event.event_id for card in active.cards] == [late_id]
    assert active.total_cards == 1
    events = store.events_for_date(first_day)
    assert {event.event_id for event in events if event.reviewed_at is None} == {
        late_id
    }


def test_next_later_unreviewed_date_skips_finished_dates_without_wrapping(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    older_day = date(2026, 7, 31)
    current_day = date(2026, 8, 3)
    finished_day = date(2026, 8, 5)
    target_day = date(2026, 8, 7)
    for number, day in enumerate(
        (older_day, current_day, finished_day, target_day),
        start=1,
    ):
        _add(store, number, day)
    finished_page = service.open_date(finished_day)
    service.finish_date(
        finished_day,
        through_revision=finished_page.snapshot_revision,
        profile_revision=finished_page.profile_revision,
        projection_revision=finished_page.projection_revision,
        finished_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert service.next_later_unreviewed_date(current_day) == target_day
    assert service.next_later_unreviewed_date(target_day) is None


def test_finish_all_reviews_current_backlog_and_leaves_later_discoveries(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    first_day = date(2026, 7, 31)
    second_day = date(2026, 8, 3)
    _add(store, 1, first_day)
    _add(store, 2, second_day)
    confirmed = service.summary()
    late_id = _add(store, 3, first_day)

    finished = service.finish_all(
        through_revision=confirmed.snapshot_revision,
        profile_revision=profiles.profile.revision,
        projection_revision=0,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc)
    )

    assert finished.reviewed_count == 2
    reopened = service.summary()
    assert reopened.unreviewed_papers == 1
    assert reopened.oldest_unreviewed_date == first_day
    assert reopened.newly_discovered == 1
    assert store.review_event(late_id).reviewed_at is None


def test_reopened_date_tiers_new_cards_against_the_complete_date(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    from arxiv_digest.ranking import RankingTier

    store, profiles, service = review
    seed_id = "2501.00001"
    seed = PaperMetadata(
        arxiv_id=seed_id,
        title="Quantum widget alignment",
        authors=("Seed Author",),
        abstract="A neutral deterministic review fixture.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    store.apply_article_snapshot(
        seed,
        (PaperVersion(1, datetime(2025, 1, 1, tzinfo=timezone.utc)),),
    )
    profiles.profile = _profile(tmp_path=Path("/tmp"), seed_papers=(seed_id,))
    day = date(2026, 8, 3)
    for number in range(1, 11):
        title = (
            "Quantum widget alignment"
            if number <= 2
            else f"Neutral queue topic {number}"
        )
        _add(store, number, day, title=title)
    opened = service.open_date(day)
    service.finish_date(
        day,
        through_revision=opened.snapshot_revision,
        profile_revision=opened.profile_revision,
        projection_revision=opened.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    late_id = _add(store, 11, day, title="Quantum widget")
    reopened = service.open_date(day)

    assert [card.event.event_id for card in reopened.cards] == [late_id]
    assert reopened.cards[0].tier is RankingTier.POSSIBLE


def test_pagination_resume_and_navigation_do_not_finish(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    day = date(2026, 8, 3)
    for number in range(1, 26):
        _add(store, number, day)
    _add(store, 30, date(2026, 8, 31))
    _add(store, 31, date(2026, 9, 1))

    first = service.open_date(day)
    assert len(first.cards) == 20
    assert first.page_count == 2
    assert first.next_anchor_event_id is not None
    second = service.open_date(day, anchor_event_id=first.next_anchor_event_id)
    assert len(second.cards) == 5
    assert second.previous_anchor_event_id == first.anchor_event_id
    service.record_position(
        day,
        snapshot_revision=second.snapshot_revision,
        anchor_event_id=second.anchor_event_id,
        profile_revision=second.profile_revision,
        projection_revision=second.projection_revision,
    )
    profiles.profile = _profile(Path("/tmp"), revision=2)
    resumed = service.open_date(day)
    assert resumed.page_number == 1
    assert service.summary().unreviewed_papers == 27
    assert service.previous_date(date(2026, 9, 1)) == date(2026, 8, 31)
    assert service.next_date(date(2026, 8, 31)) == date(2026, 9, 1)


def test_opening_from_start_ignores_the_saved_page(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    for number in range(1, 26):
        _add(store, number, day)

    first = service.open_date(day)
    second = service.open_date(day, anchor_event_id=first.next_anchor_event_id)
    service.record_position(
        day,
        snapshot_revision=second.snapshot_revision,
        anchor_event_id=second.anchor_event_id,
        profile_revision=second.profile_revision,
        projection_revision=second.projection_revision,
    )

    reopened = service.open_date(day, from_start=True)

    assert reopened.page_number == 1
    assert reopened.anchor_event_id == first.anchor_event_id


def test_opening_from_start_takes_precedence_over_an_explicit_anchor(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    for number in range(1, 26):
        _add(store, number, day)

    first = service.open_date(day)
    reopened = service.open_date(
        day,
        anchor_event_id=first.next_anchor_event_id,
        from_start=True,
    )

    assert reopened.page_number == 1
    assert reopened.anchor_event_id == first.anchor_event_id


def test_calendar_jump_and_missing_anchor_use_confirmed_dates(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    _add(store, 1, date(2026, 8, 3))
    _add(store, 2, date(2026, 8, 5))
    page = service.open_date(date(2026, 8, 5), anchor_event_id=-1)

    assert page.day == date(2026, 8, 5)
    assert page.previous_date == date(2026, 8, 3)
    assert service.calendar(date(2026, 8, 4), date(2026, 8, 31))[0].day == date(
        2026, 8, 5
    )
    assert page.cards[0].event.daily_list_date == date(2026, 8, 5)
    assert all(
        item.source is EvidenceSource.CATCHUP
        for item in page.cards[0].event.observations
    )
    with pytest.raises(KeyError):
        service.open_date(date(2026, 8, 4))
    with pytest.raises(ValueError):
        service.calendar(date(2026, 8, 5), date(2026, 8, 4))


def test_finish_snapshot_does_not_review_event_recovered_into_date_later(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    open_day = date(2026, 8, 3)
    _add(store, 1, open_day)
    snapshot = service.open_date(open_day)

    recovered_id = _add(store, 2, open_day, version=None)
    assert (
        store.review_event(recovered_id).queue_revision
        > snapshot.snapshot_revision
    )
    service.finish_date(
        open_day,
        through_revision=snapshot.snapshot_revision,
        profile_revision=snapshot.profile_revision,
        projection_revision=snapshot.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert store.review_event(recovered_id).reviewed_at is None
    assert service.summary().oldest_unreviewed_date == open_day


def test_profile_change_reranks_without_resetting_review_state(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    day = date(2026, 8, 3)
    _add(store, 1, day)
    second_id = _add(store, 2, day, title="Graph methods")

    neutral = service.open_date(day)
    assert neutral.cards[0].event.event_id != second_id
    profiles.profile = _profile(Path("/tmp"), revision=2, keywords=("graph",))
    reranked = service.open_date(day)
    assert reranked.cards[0].event.event_id == second_id

    service.finish_date(
        day,
        through_revision=reranked.snapshot_revision,
        profile_revision=reranked.profile_revision,
        projection_revision=reranked.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    profiles.profile = _profile(Path("/tmp"), revision=3, keywords=("other",))
    assert service.summary().unreviewed_papers == 0


def test_review_builds_seed_vectors_from_durable_metadata(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    seed_id = "2608.00999"
    seed = PaperMetadata(
        arxiv_id=seed_id,
        title="Quantum widget alignment",
        authors=("Seed Author",),
        abstract="Quantum widgets align under a synthetic objective.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    store.apply_article_snapshot(
        seed,
        (PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
    )
    day = date(2026, 8, 3)
    related_id = _add(store, 1, day, title="Quantum widget methods")
    _add(store, 2, day, title="Unrelated queue bookkeeping")
    profiles.profile = _profile(
        Path("/tmp"), revision=2, seed_papers=(seed_id,)
    )

    page = service.open_date(day)

    assert page.cards[0].event.event_id == related_id
    seed_reason = next(
        reason
        for reason in page.cards[0].reasons
        if reason.kind == "seed_similarity"
    )
    assert seed_reason.label == "Related to selected seed paper"
    assert seed_reason.reference.arxiv_id == seed_id
    assert seed_reason.reference.title == "Quantum widget alignment"

    from arxiv_digest.ranking import (
        CATEGORY_BASELINE_WEIGHT,
        SEED_SIMILARITY_WEIGHT,
    )
    from arxiv_digest.text import build_tfidf_vectors

    opened_papers = tuple(
        store.article_metadata(card.paper.arxiv_id) for card in page.cards
    )
    vectors = build_tfidf_vectors((*opened_papers, seed), ngram_range=(1, 3))
    related_vector = vectors["2608.00001"]
    seed_vector = vectors[seed_id]
    expected_similarity = sum(
        weight * seed_vector.get(term, 0.0)
        for term, weight in related_vector.items()
    )
    expected_score = (
        CATEGORY_BASELINE_WEIGHT
        + SEED_SIMILARITY_WEIGHT * expected_similarity
    )
    related = next(card for card in page.cards if card.event.event_id == related_id)
    assert isclose(related.score, expected_score, rel_tol=1e-12)


def test_review_names_the_saved_paper_behind_a_similarity_reason(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    saved_id = "2608.00998"
    saved = PaperMetadata(
        arxiv_id=saved_id,
        title="Persistent widgets in topology",
        authors=("Saved Author",),
        abstract="A distinct synthetic saved-paper abstract.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    store.apply_article_snapshot(
        saved,
        (PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
    )
    store.save_paper(saved_id, 1)
    day = date(2026, 8, 3)
    related_id = _add(
        store,
        1,
        day,
        title="Persistent widgets for deterministic groups",
    )
    _add(store, 2, day, title="Unrelated queue bookkeeping")

    page = service.open_date(day)

    related = next(card for card in page.cards if card.event.event_id == related_id)
    saved_reason = next(
        reason
        for reason in related.reasons
        if reason.kind == "saved_similarity"
    )
    assert saved_reason.label == "Similar to saved paper"
    assert saved_reason.reference.arxiv_id == saved_id
    assert saved_reason.reference.title == "Persistent widgets in topology"
