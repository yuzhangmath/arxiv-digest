from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isclose
from pathlib import Path

import pytest

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
from arxiv_digest.review import (
    ReviewService,
    confidence_label,
    review_date_label,
)
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
        schema_version=1,
        revision=revision,
        categories=("cs.SE",),
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
    source: EvidenceSource = EvidenceSource.ATOM,
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
    confidence = {
        EvidenceSource.ATOM: Confidence.CURRENT,
        EvidenceSource.CATCHUP: Confidence.RECOVERED,
        EvidenceSource.OAI: Confidence.INFERRED,
    }[source]
    date_basis = {
        EvidenceSource.ATOM: DateBasis.FEED_MAILING,
        EvidenceSource.CATCHUP: DateBasis.CATCHUP_MAILING,
        EvidenceSource.OAI: DateBasis.VERSION_HISTORY_UTC,
    }[source]
    evidence = EventEvidence(
        source_key=f"{source.value}:cs.SE:{day}:{arxiv_id}:{version}",
        source=source,
        confidence=confidence,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        mailing_date=None if source is EvidenceSource.OAI else day,
        announced_version=version,
        list_position=(None if source is EvidenceSource.OAI else number),
        oai_datestamp=(day if source is EvidenceSource.OAI else None),
        raw_sha256=f"{number % 16:x}" * 64,
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    candidate = EventCandidate(
        arxiv_id=arxiv_id,
        announced_version=version,
        effective_date=day,
        date_basis=date_basis,
        evidence=evidence,
    )
    versions = (
        ()
        if version is None
        else (
            PaperVersion(
                version,
                datetime(2026, 8, min(number, 28), tzinfo=timezone.utc),
            ),
        )
    )
    return store.apply_event_batch(metadata, versions, (candidate,))[0].event_id


@pytest.fixture
def review(tmp_path: Path) -> tuple[Store, Profiles, ReviewService]:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    profiles = Profiles(_profile(tmp_path))
    return store, profiles, ReviewService(store, profiles)


def test_oldest_first_finish_and_later_discovery(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    first_day = date(2026, 7, 31)
    second_day = date(2026, 8, 3)
    first_id = _add(store, 1, first_day)
    _add(store, 2, second_day)

    summary = service.summary()
    assert (summary.unreviewed_dates, summary.unreviewed_papers) == (2, 2)
    assert service.start().day == first_day
    page = service.open_date(first_day)
    service.finish_date(
        first_day,
        through_revision=page.snapshot_revision,
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
    store.apply_event_batch(
        seed,
        (PaperVersion(1, datetime(2025, 1, 1, tzinfo=timezone.utc)),),
        (),
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
    )
    profiles.profile = _profile(Path("/tmp"), revision=2)
    resumed = service.open_date(day)
    assert resumed.page_number == 2
    assert service.summary().unreviewed_papers == 27
    assert service.previous_date(date(2026, 9, 1)) == date(2026, 8, 31)
    assert service.next_date(date(2026, 8, 31)) == date(2026, 9, 1)


def test_calendar_jump_missing_anchor_and_labels(
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
    assert confidence_label(Confidence.CURRENT) == "Current announcement"
    assert confidence_label(Confidence.RECOVERED) == "Recovered announcement"
    assert confidence_label(Confidence.INFERRED) == "Inferred update"
    assert review_date_label(DateBasis.VERSION_HISTORY_UTC) == "Inferred catch-up date"
    with pytest.raises(KeyError):
        service.open_date(date(2026, 8, 4))
    with pytest.raises(ValueError):
        service.calendar(date(2026, 8, 5), date(2026, 8, 4))


def test_finish_snapshot_does_not_review_event_relocated_into_date_later(
    review: tuple[Store, Profiles, ReviewService],
) -> None:
    store, _profiles, service = review
    open_day = date(2026, 8, 3)
    _add(store, 1, open_day)
    provisional_id = _add(
        store,
        2,
        date(2026, 8, 1),
        source=EvidenceSource.OAI,
    )
    snapshot = service.open_date(open_day)

    relocated_id = _add(store, 2, open_day, source=EvidenceSource.ATOM)
    assert relocated_id == provisional_id
    assert store.review_event(relocated_id).queue_revision > snapshot.snapshot_revision
    service.finish_date(
        open_day,
        through_revision=snapshot.snapshot_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert store.review_event(relocated_id).reviewed_at is None
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
    store.apply_event_batch(
        seed,
        (PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
        (),
    )
    day = date(2026, 8, 3)
    related_id = _add(store, 1, day, title="Quantum widget methods")
    _add(store, 2, day, title="Unrelated queue bookkeeping")
    profiles.profile = _profile(
        Path("/tmp"), revision=2, seed_papers=(seed_id,)
    )

    page = service.open_date(day)

    assert page.cards[0].event.event_id == related_id
    assert any(
        reason.kind == "seed_similarity" for reason in page.cards[0].reasons
    )

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
