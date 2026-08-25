from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from arxiv_digest.models import (
    AnnounceType,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    EvidenceSource,
    PaperMetadata,
    SourceObservation,
)
from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory
from arxiv_digest.review import ReviewService
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


@dataclass
class Profiles:
    value: Profile

    def load(self) -> Profile:
        return self.value


def _populate_day(store: Store, numbers: range, day: date) -> set[int]:
    metadata = tuple(
        PaperMetadata(
            arxiv_id=f"2608.{number:05d}",
            title=f"Backlog fixture {number}",
            authors=(f"Synthetic Author {number % 11}",),
            abstract="A deterministic paper in the two-hundred-card backlog.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        )
        for number in numbers
    )
    entries = tuple(
        CatchupEntry(
            metadata=paper,
            section=AnnounceType.NEW,
            mailing_date=day,
            position=index,
        )
        for index, paper in enumerate(metadata)
    )
    observations = tuple(
        SourceObservation(
            source_key=f"catchup:cs.SE:{day}:{paper.arxiv_id}:{index}",
            arxiv_id=paper.arxiv_id,
            source=EvidenceSource.CATCHUP,
            category="cs.SE",
            announce_type=AnnounceType.NEW,
            daily_list_date=day,
            announced_version=None,
            list_position=index,
            oai_datestamp=None,
            response_sha256="a" * 64,
            observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        for index, paper in enumerate(metadata)
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
                raw_sha256="a" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    return {
        event.event_id
        for event in store.apply_catchup_day(
            result,
            observations,
            datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
    }


def _walk_date(
    service: ReviewService, day: date, first_event_id: int
) -> tuple[set[int], int]:
    page = service.open_date(day, anchor_event_id=first_event_id)
    reached: set[int] = set()
    snapshot_revision = page.snapshot_revision
    while True:
        assert 1 <= len(page.cards) <= 20
        reached.update(card.event.event_id for card in page.cards)
        if page.next_anchor_event_id is None:
            return reached, snapshot_revision
        page = service.open_date(
            day, anchor_event_id=page.next_anchor_event_id
        )


def test_two_hundred_papers_remain_reachable_across_review_navigation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 7, 1))
    profile = Profile(
        schema_version=2,
        revision=1,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", tmp_path / "pdfs"),
    )
    profiles = Profiles(profile)
    service = ReviewService(store, profiles)
    allocation = (
        (date(2026, 7, 30), range(1, 46)),
        (date(2026, 8, 3), range(46, 156)),
        (date(2026, 8, 7), range(156, 201)),
    )
    expected: dict[date, set[int]] = {}
    for day, numbers in allocation:
        expected[day] = _populate_day(store, numbers, day)

    summary = service.summary()
    assert summary.unreviewed_dates == 3
    assert summary.unreviewed_papers == 200
    assert service.start().day == date(2026, 7, 30)

    first = service.open_date(date(2026, 7, 30))
    next_anchor = first.next_anchor_event_id
    assert next_anchor is not None
    service.record_position(
        first.day,
        snapshot_revision=first.snapshot_revision,
        anchor_event_id=next_anchor,
        profile_revision=first.profile_revision,
        projection_revision=first.projection_revision,
    )
    reopened = ReviewService(store, profiles).open_date(first.day)
    assert reopened.anchor_event_id == next_anchor

    reached_first, first_revision = _walk_date(
        service, date(2026, 7, 30), min(expected[date(2026, 7, 30)])
    )
    assert reached_first == expected[date(2026, 7, 30)]
    service.finish_date(
        date(2026, 7, 30),
        through_revision=first_revision,
        profile_revision=first.profile_revision,
        projection_revision=first.projection_revision,
        finished_at=datetime(2026, 8, 22, 13, tzinfo=timezone.utc),
    )
    assert service.next_unreviewed().day == date(2026, 8, 3)

    jumped = service.open_date(date(2026, 8, 7))
    assert jumped.previous_date == date(2026, 8, 3)
    assert jumped.next_unreviewed_date == date(2026, 8, 3)
    reached_middle, middle_revision = _walk_date(
        service, date(2026, 8, 3), min(expected[date(2026, 8, 3)])
    )
    assert reached_middle == expected[date(2026, 8, 3)]
    service.finish_date(
        date(2026, 8, 3),
        through_revision=middle_revision,
        profile_revision=jumped.profile_revision,
        projection_revision=jumped.projection_revision,
        finished_at=datetime(2026, 8, 22, 14, tzinfo=timezone.utc),
    )
    reached_last, last_revision = _walk_date(
        service, date(2026, 8, 7), min(expected[date(2026, 8, 7)])
    )
    assert reached_last == expected[date(2026, 8, 7)]
    service.finish_date(
        date(2026, 8, 7),
        through_revision=last_revision,
        profile_revision=jumped.profile_revision,
        projection_revision=jumped.projection_revision,
        finished_at=datetime(2026, 8, 22, 15, tzinfo=timezone.utc),
    )

    assert service.summary().unreviewed_papers == 0
    assert service.next_unreviewed() is None
