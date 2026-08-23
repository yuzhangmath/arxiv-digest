from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from arxiv_digest.models import (
    AnnounceType,
    Confidence,
    DateBasis,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.profile import PdfDestination, Profile
from arxiv_digest.review import ReviewService
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


@dataclass
class Profiles:
    value: Profile

    def load(self) -> Profile:
        return self.value


def _populate(store: Store, number: int, day: date) -> int:
    arxiv_id = f"2608.{number:05d}"
    metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title=f"Backlog fixture {number}",
        authors=(f"Synthetic Author {number % 11}",),
        abstract="A deterministic paper in the two-hundred-card backlog.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    version = PaperVersion(
        1, datetime(2026, 8, (number % 28) + 1, tzinfo=timezone.utc)
    )
    evidence = EventEvidence(
        source_key=f"atom:cs.SE:{day}:{arxiv_id}:v1:new",
        source=EvidenceSource.ATOM,
        confidence=Confidence.CURRENT,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        mailing_date=day,
        announced_version=1,
        list_position=number,
        oai_datestamp=None,
        raw_sha256=f"{number % 16:x}" * 64,
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    candidate = EventCandidate(
        arxiv_id,
        1,
        day,
        DateBasis.FEED_MAILING,
        evidence,
    )
    return store.apply_event_batch(metadata, (version,), (candidate,))[0].event_id


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
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
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
        expected[day] = {_populate(store, number, day) for number in numbers}

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
        finished_at=datetime(2026, 8, 22, 14, tzinfo=timezone.utc),
    )
    reached_last, last_revision = _walk_date(
        service, date(2026, 8, 7), min(expected[date(2026, 8, 7)])
    )
    assert reached_last == expected[date(2026, 8, 7)]
    service.finish_date(
        date(2026, 8, 7),
        through_revision=last_revision,
        finished_at=datetime(2026, 8, 22, 15, tzinfo=timezone.utc),
    )

    assert service.summary().unreviewed_papers == 0
    assert service.next_unreviewed() is None
