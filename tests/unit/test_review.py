from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
    abstract: str = "A neutral deterministic review fixture.",
) -> int:
    arxiv_id = f"2608.{number:05d}"
    metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title=title or f"Synthetic review paper {number}",
        authors=(f"Author {number}",),
        abstract=abstract,
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


def test_date_can_be_read_and_finished_while_abstracts_remain_recoverable(review) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    _add(store, 1, day, abstract="")
    _add(store, 2, day, abstract=" \n\t")
    _add(store, 3, day)
    summary = service.summary()
    assert summary.missing_abstracts == 2
    assert summary.waiting_abstract_dates == 1
    assert summary.waiting_abstract_papers == 3
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (1, 3)
    assert service.start().day == day
    entry = service.calendar(day, day)[0]
    assert entry.abstracts_pending is True
    assert entry.total_papers == 3
    assert entry.abstracts_ready == 1
    assert entry.missing_abstracts == 2
    assert entry.unreviewed_papers == 3
    assert entry.newly_discovered == 0
    assert entry.finished is False
    page = service.open_date(day)
    assert page.total_cards == 3
    assert (page.abstracts_ready, page.missing_abstracts) == (1, 2)
    store.save_paper(page.cards[0].paper.arxiv_id, None)
    assert len(store.saved_paper_metadata()) == 1
    result = service.finish_date(
        day, through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    assert result.reviewed_count == 3
    assert all(event.reviewed_at is not None for event in store.events_for_date(day))
    assert service.calendar(day, day)[0].finished is True
    assert service.summary().missing_abstracts == 0
    assert service.summary().waiting_abstract_dates == 0
    store.apply_article_snapshot(
        replace(store.article_metadata("2608.00001"), abstract="Recovered abstract."),
        store.article_versions("2608.00001"),
    )
    assert service.summary().missing_abstracts == 0
    assert service.summary().unreviewed_dates == 0
    assert service.calendar(day, day)[0].abstracts_ready == 2
    store.apply_article_snapshot(
        replace(store.article_metadata("2608.00002"), abstract="Last abstract."),
        store.article_versions("2608.00002"),
    )
    assert service.summary().waiting_abstract_dates == 0
    assert service.summary().unreviewed_papers == 0
    assert service.calendar(day, day)[0].abstracts_pending is False
    page = service.open_date(day)
    assert (page.abstracts_ready, page.missing_abstracts) == (3, 0)
    service.finish_date(
        day,
        through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    assert service.summary().missing_abstracts == 0


def test_navigation_and_finish_all_include_dates_missing_abstracts(review) -> None:
    store, _profiles, service = review
    first, waiting, last = (date(2026, 8, number) for number in (3, 4, 5))
    _add(store, 1, first)
    missing_id = _add(store, 2, waiting, abstract="")
    complete_id = _add(store, 3, waiting)
    _add(store, 4, last)
    assert service.open_date(first).next_date == waiting
    assert service.open_date(last).previous_date == waiting
    assert service.previous_date(last) == waiting
    assert service.next_date(first) == waiting
    assert service.open_date(waiting).previous_date == first
    assert service.open_date(waiting).next_date == last
    assert service.next_later_unreviewed_date(first) == waiting
    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (3, 4)
    result = service.finish_all(
        through_revision=summary.snapshot_revision,
        profile_revision=summary.profile_revision,
        projection_revision=summary.projection_revision,
        finished_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert result.reviewed_count == 4
    assert store.review_event(missing_id).reviewed_at is not None
    assert store.review_event(complete_id).reviewed_at is not None
    assert service.summary().missing_abstracts == 0
    assert service.summary().waiting_abstract_dates == 0


def test_incomplete_date_paging_resumes_without_marking_papers_reviewed(review) -> None:
    store, profiles, service = review
    day = date(2026, 8, 3)
    for number in range(1, 23):
        _add(store, number, day, abstract="" if number == 1 else "Synthetic abstract.")
    first = service.open_date(day)
    assert first.page_count == 2
    second = service.open_date(day, anchor_event_id=first.next_anchor_event_id)
    assert second.page_number == 2
    assert second.missing_abstracts == 1
    service.record_position(
        day, snapshot_revision=second.snapshot_revision,
        anchor_event_id=second.anchor_event_id,
        profile_revision=second.profile_revision,
        projection_revision=second.projection_revision,
    )
    reopened = ReviewService(Store(store.database_path), profiles).open_date(day)
    assert reopened.anchor_event_id == second.anchor_event_id
    assert reopened.page_number == 2
    assert all(event.reviewed_at is None for event in store.events_for_date(day))


def test_previously_reviewed_papers_missing_abstracts_remain_readable(review) -> None:
    store, _profiles, service = review
    first, later = date(2026, 8, 3), date(2026, 8, 4)
    _add(store, 1, first, abstract="")
    _add(store, 2, later, abstract="")
    with open_database(store.database_path) as connection:
        connection.execute(
            "UPDATE canonical_events SET reviewed_at = ? WHERE daily_list_date = ?",
            ("2026-08-05T00:00:00Z", first.isoformat()),
        )
    assert service.summary().oldest_unreviewed_date == later
    assert service.summary().missing_abstracts == 1
    assert service.summary().waiting_abstract_dates == 1
    assert service.open_date(first).total_cards == 1
    assert service.open_date(first).missing_abstracts == 1


def test_summary_excludes_missing_abstracts_on_finished_dates(review) -> None:
    store, _profiles, service = review
    first, second, pending = (date(2026, 8, day) for day in (3, 4, 5))
    for number in range(1, 10):
        _add(store, number, first if number < 5 else second, abstract="")
    _add(store, 10, pending)
    for day in (first, second):
        page = service.open_date(day)
        service.finish_date(
            day, through_revision=page.snapshot_revision,
            profile_revision=page.profile_revision,
            projection_revision=page.projection_revision,
            finished_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        )

    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (1, 1)
    assert summary.missing_abstracts == 0
    assert (summary.waiting_abstract_dates, summary.waiting_abstract_papers) == (0, 0)
    entries = service.calendar(first, pending)
    assert [(entry.finished, entry.missing_abstracts) for entry in entries] == [
        (True, 4), (True, 5), (False, 0),
    ]
    assert service.open_date(first).missing_abstracts == 4
    assert service.open_date(second).missing_abstracts == 5


def test_summary_excludes_reviewed_missing_papers_when_date_reopens(review) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    _add(store, 1, day, abstract="")
    page = service.open_date(day)
    service.finish_date(
        day, through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    _add(store, 2, day)
    summary = service.summary()
    assert (summary.unreviewed_papers, summary.newly_discovered) == (1, 1)
    assert summary.missing_abstracts == 0
    assert (summary.waiting_abstract_dates, summary.waiting_abstract_papers) == (0, 0)

    _add(store, 3, day, abstract=" \n\t")
    summary = service.summary()
    assert (summary.unreviewed_papers, summary.newly_discovered) == (2, 2)
    assert summary.missing_abstracts == 1
    assert (summary.waiting_abstract_dates, summary.waiting_abstract_papers) == (1, 2)
    assert service.open_date(day).missing_abstracts == 2


def test_summary_counts_unique_missing_papers_across_unreviewed_dates(review) -> None:
    store, _profiles, service = review
    first, later = date(2026, 8, 3), date(2026, 8, 4)
    _add(store, 1, first, abstract="")
    _add(store, 1, later, abstract="")
    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (2, 2)
    assert summary.missing_abstracts == 1
    assert (summary.waiting_abstract_dates, summary.waiting_abstract_papers) == (2, 2)


@pytest.mark.parametrize("view", ("summary", "calendar"))
def test_read_models_handle_a_date_becoming_pending_during_the_read(
    review, monkeypatch, view: str,
) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    _add(store, 1, day)
    original_snapshot = store.review_snapshot

    def recover_during_read(*args, **kwargs):
        _add(store, 2, day, abstract="")
        monkeypatch.setattr(store, "review_snapshot", original_snapshot)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(store, "review_snapshot", recover_during_read)
    if view == "summary":
        summary = service.summary()
        assert summary.unreviewed_dates == 1
        assert summary.unreviewed_papers == 1
        assert summary.missing_abstracts == 0
        assert summary.waiting_abstract_dates == 0
        assert summary.waiting_abstract_papers == 0
        fresh = service.summary()
        assert fresh.missing_abstracts == 1
        assert fresh.waiting_abstract_dates == 1
        assert fresh.waiting_abstract_papers == 2
    else:
        entry = service.calendar(day, day)[0]
        assert entry.abstracts_pending is True
        assert entry.abstracts_ready == entry.missing_abstracts == 1
        assert entry.total_papers == 2
        assert entry.finished is False
        assert entry.unreviewed_papers == 2


def test_calendar_ready_counts_use_the_same_snapshot(review, monkeypatch) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    _add(store, 1, day)
    original_snapshot = store.review_snapshot

    def recover_during_read(*args, **kwargs):
        _add(store, 2, day)
        monkeypatch.setattr(store, "review_snapshot", original_snapshot)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(store, "review_snapshot", recover_during_read)
    entry = service.calendar(day, day)[0]
    assert entry.abstracts_pending is False
    assert entry.total_papers == entry.abstracts_ready == entry.unreviewed_papers == 2
    assert entry.missing_abstracts == 0


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


def _record_empty_or_failed_day(
    store: Store,
    day: date,
    *,
    category: str = "cs.SE",
    status: EnrichmentStatus = EnrichmentStatus.FAILED,
) -> None:
    failed = status is EnrichmentStatus.FAILED
    store.apply_catchup_day(
        CatchupDay(
            category=category,
            mailing_date=day,
            status=status,
            pages=(),
            error_code="catchup_fetch_failed" if failed else None,
            error_message="Daily-list retrieval failed." if failed else None,
        ),
        (),
        datetime(2026, 8, 22, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "day,before,after",
    (
        (date(2026, 9, 20), "2026-09-20T23:59:59+00:00", "2026-09-21T00:00:00+00:00"),
        (date(2027, 1, 20), "2027-01-21T00:59:59+00:00", "2027-01-21T01:00:00+00:00"),
    ),
)
def test_calendar_hides_unfinalized_failure_until_eastern_cutoff(
    review, monkeypatch, day: date, before: str, after: str,
) -> None:
    store, _profiles, service = review
    observed_at = [datetime.fromisoformat(before)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed_at[0].astimezone(tz)

    monkeypatch.setattr("arxiv_digest.review.datetime", Clock)
    _record_empty_or_failed_day(store, day)
    before_records = store.catchup_day_records("cs.SE")

    assert service.calendar(day, day) == ()
    assert store.catchup_day_records("cs.SE") == before_records

    observed_at[0] = datetime.fromisoformat(after)
    entries = service.calendar(day, day)
    assert [entry.day for entry in entries] == [day]
    assert entries[0].retrieval_failed is True
    assert store.catchup_day_records("cs.SE") == before_records


def test_unfinalized_confirmed_date_is_hidden_and_cannot_be_finished(
    review,
) -> None:
    store, profiles, _service = review
    earlier, visible, hidden = (date(2026, 9, day) for day in (18, 19, 20))
    observed_at = [datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc)]
    latest_finalized = [visible]
    service = ReviewService(
        store, profiles, latest_finalized_date=lambda: latest_finalized[0],
    )
    _add(store, 1, earlier, abstract="")
    visible_id = _add(store, 2, visible, abstract="")
    hidden_id = _add(store, 3, hidden, abstract="")
    page = service.open_date(earlier)
    service.finish_date(
        earlier,
        through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=observed_at[0],
    )

    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (1, 1)
    assert summary.missing_abstracts == 1
    assert (summary.waiting_abstract_dates, summary.waiting_abstract_papers) == (1, 1)
    assert [entry.day for entry in service.calendar(earlier, hidden)] == [earlier, visible]
    assert service.start().day == visible
    assert service.open_date(visible).next_date is None
    assert service.next_date(visible) is None
    assert service.previous_date(date(2026, 9, 21)) == visible
    assert service.next_later_unreviewed_date(visible) is None
    with pytest.raises(KeyError):
        service.open_date(hidden)
    with pytest.raises(KeyError):
        service.record_position(
            hidden, snapshot_revision=summary.snapshot_revision,
            anchor_event_id=hidden_id, profile_revision=summary.profile_revision,
            projection_revision=summary.projection_revision,
        )
    with pytest.raises(KeyError):
        service.finish_date(
            hidden, through_revision=summary.snapshot_revision,
            profile_revision=summary.profile_revision,
            projection_revision=summary.projection_revision,
            finished_at=observed_at[0],
        )
    result = service.finish_all(
        through_revision=summary.snapshot_revision,
        profile_revision=summary.profile_revision,
        projection_revision=summary.projection_revision,
        finished_at=observed_at[0],
    )
    assert result.reviewed_count == 1
    assert store.review_event(visible_id).reviewed_at is not None
    assert store.review_event(hidden_id).reviewed_at is None
    assert store.review_snapshot(hidden).last_finished_revision is None
    assert service.start() is None
    assert service.summary().missing_abstracts == 0

    observed_at[0] = datetime(2026, 9, 21, tzinfo=timezone.utc)
    latest_finalized[0] = hidden
    assert service.start().day == hidden
    assert service.open_date(visible).next_date == hidden
    assert service.next_later_unreviewed_date(visible) == hidden
    assert service.summary().missing_abstracts == 1


def test_finish_all_keeps_confirmation_date_bound_when_cutoff_advances(review) -> None:
    store, profiles, _service = review
    visible, hidden = date(2026, 9, 19), date(2026, 9, 20)
    latest_finalized = [visible]
    service = ReviewService(
        store, profiles, latest_finalized_date=lambda: latest_finalized[0],
    )
    visible_id = _add(store, 1, visible)
    hidden_id = _add(store, 2, hidden)
    summary = service.summary()
    assert summary.unreviewed_papers == 1
    assert summary.through_date == visible

    latest_finalized[0] = hidden
    result = service.finish_all(
        through_revision=summary.snapshot_revision,
        profile_revision=summary.profile_revision,
        projection_revision=summary.projection_revision,
        through_date=summary.through_date,
        finished_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )

    assert result.reviewed_count == 1
    assert store.review_event(visible_id).reviewed_at is not None
    assert store.review_event(hidden_id).reviewed_at is None
    assert service.summary().oldest_unreviewed_date == hidden


def test_finish_all_caps_requested_date_at_current_finalized_cutoff(review) -> None:
    store, profiles, _service = review
    visible, hidden = date(2026, 9, 19), date(2026, 9, 20)
    service = ReviewService(store, profiles, latest_finalized_date=lambda: visible)
    _add(store, 1, visible)
    hidden_id = _add(store, 2, hidden)
    summary = service.summary()

    result = service.finish_all(
        through_revision=summary.snapshot_revision,
        profile_revision=summary.profile_revision,
        projection_revision=summary.projection_revision,
        through_date=hidden,
        finished_at=datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc),
    )

    assert result.reviewed_count == 1
    assert store.review_event(hidden_id).reviewed_at is None


def test_calendar_failed_date_has_unknown_counts_and_does_not_enter_review(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    failed_day = date(2026, 8, 3)
    _record_empty_or_failed_day(store, failed_day)
    summary_before = service.summary()
    connection = open_database(store.database_path)
    before = tuple(connection.iterdump())
    connection.close()

    entries = service.calendar(failed_day, failed_day)

    assert len(entries) == 1
    assert asdict(entries[0]) == {
        "day": failed_day,
        "total_papers": None,
        "unreviewed_papers": None,
        "newly_discovered": None,
        "finished": None,
        "retrieval_failed": True,
        "abstracts_pending": False,
        "abstracts_ready": None,
        "missing_abstracts": None,
    }
    assert service.summary() == summary_before
    assert summary_before.unreviewed_dates == 0
    assert summary_before.unreviewed_papers == 0
    assert service.start() is None
    with pytest.raises(KeyError):
        service.open_date(failed_day)
    connection = open_database(store.database_path)
    assert tuple(connection.iterdump()) == before
    connection.close()


def test_calendar_merges_failed_dates_with_confirmed_review_dates(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    store.ensure_category_state("cs.AI", "cs:AI", date(2026, 7, 1))
    profiles.profile = replace(
        profiles.profile,
        category_coverage=(
            *profiles.profile.category_coverage,
            ProfileCategory("cs.AI", date(2026, 7, 1)),
        ),
    )
    first, failed, mixed, last = (date(2026, 8, day) for day in (3, 4, 5, 6))
    _add(store, 1, first)
    _add(store, 2, mixed)
    _add(store, 3, mixed)
    _add(store, 4, last)
    page = service.open_date(mixed)
    service.finish_date(
        mixed,
        through_revision=page.snapshot_revision,
        profile_revision=page.profile_revision,
        projection_revision=page.projection_revision,
        finished_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    _record_empty_or_failed_day(store, mixed, category="cs.AI")
    _record_empty_or_failed_day(store, failed)
    _record_empty_or_failed_day(store, failed, category="cs.AI")

    entries = service.calendar(first, last)

    assert [entry.day for entry in entries] == [first, failed, mixed, last]
    assert [entry.retrieval_failed for entry in entries] == [
        False, True, True, False
    ]
    assert asdict(entries[2]) == {
        "day": mixed,
        "total_papers": 2,
        "unreviewed_papers": 0,
        "newly_discovered": 0,
        "finished": True,
        "retrieval_failed": True,
        "abstracts_pending": False,
        "abstracts_ready": 2,
        "missing_abstracts": 0,
    }
    _add(store, 5, mixed)
    recovered = service.calendar(mixed, mixed)[0]
    assert (recovered.total_papers, recovered.unreviewed_papers) == (3, 1)
    assert recovered.newly_discovered == 1
    assert recovered.finished is False
    assert recovered.retrieval_failed is True
    assert service.next_date(first) == mixed
    assert service.previous_date(mixed) == first
    assert service.next_later_unreviewed_date(first) == mixed


def test_calendar_failures_respect_active_categories_coverage_and_date_range(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, profiles, service = review
    profiles.profile = replace(
        profiles.profile,
        category_coverage=(ProfileCategory("cs.SE", date(2026, 8, 3)),),
    )
    for day in (2, 3, 5, 8, 9):
        _record_empty_or_failed_day(store, date(2026, 8, day))
    _record_empty_or_failed_day(store, date(2026, 8, 4), category="cs.AI")
    store.ensure_catchup_targets("cs.SE", (date(2026, 8, 6),))
    _record_empty_or_failed_day(
        store, date(2026, 8, 7), status=EnrichmentStatus.EMPTY
    )

    full_range = service.calendar(date(2026, 8, 1), date(2026, 8, 8))
    assert [entry.day for entry in full_range] == [
        date(2026, 8, 3),
        date(2026, 8, 5),
        date(2026, 8, 8),
    ]
    later_range = service.calendar(date(2026, 8, 5), date(2026, 8, 8))
    assert [entry.day for entry in later_range] == [
        date(2026, 8, 5),
        date(2026, 8, 8),
    ]


@pytest.mark.parametrize("recovers_papers", [False, True])
def test_calendar_failed_placeholder_clears_after_recovery(
    review: tuple[Store, Profiles, ReviewService],
    recovers_papers: bool,
) -> None:
    store, _profiles, service = review
    day = date(2026, 8, 3)
    _record_empty_or_failed_day(store, day)
    assert service.calendar(day, day)[0].retrieval_failed is True

    if recovers_papers:
        _add(store, 1, day)
    else:
        _record_empty_or_failed_day(store, day, status=EnrichmentStatus.EMPTY)

    entries = service.calendar(day, day)
    if recovers_papers:
        assert len(entries) == 1
        assert entries[0].retrieval_failed is False
        assert entries[0].total_papers == 1
        assert entries[0].unreviewed_papers == 1
        assert entries[0].finished is False
    else:
        assert entries == ()


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
