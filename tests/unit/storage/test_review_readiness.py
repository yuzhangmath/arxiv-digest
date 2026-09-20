from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from arxiv_digest.models import (
    AnnounceType, CatchupDay, CatchupEntry, CatchupPage, CategoryConfig,
    EnrichmentStatus, EvidenceSource, PaperMetadata, SourceObservation,
)
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import ReviewSnapshotConflict, Store


DAY = date(2026, 8, 24)
NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
ACTIVE = (CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1)),)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    path = tmp_path / "state.sqlite3"
    open_database(path).close()
    return Store(path)


def paper(number: int, abstract: str = "A complete synthetic abstract.") -> PaperMetadata:
    return PaperMetadata(
        arxiv_id=f"2608.{number:05}", title=f"Synthetic paper {number}",
        authors=("A. Example",), abstract=abstract,
        primary_category="cs.SE", categories=("cs.SE",),
    )


def recover(store: Store, day: date, papers: tuple[PaperMetadata, ...], *, category: str = "cs.SE"):
    entries = tuple(
        CatchupEntry(metadata=item, section=AnnounceType.NEW, mailing_date=day, position=index)
        for index, item in enumerate(papers)
    )
    observations = tuple(
        SourceObservation(
            source_key=f"catchup:{category}:{day.isoformat()}:{index}:{item.arxiv_id}",
            arxiv_id=item.arxiv_id, source=EvidenceSource.CATCHUP, category=category,
            announce_type=AnnounceType.NEW, daily_list_date=day,
            announced_version=None, list_position=index, oai_datestamp=None,
            response_sha256="a" * 64, observed_at=NOW,
        )
        for index, item in enumerate(papers)
    )
    return store.apply_catchup_day(
        CatchupDay(
            category=category, mailing_date=day, status=EnrichmentStatus.COMPLETE,
            pages=(CatchupPage(category=category, mailing_date=day, page=1, total_pages=1, entries=entries, raw_sha256="a" * 64),),
            error_code=None, error_message=None,
        ), observations, NOW,
    )


def test_abstract_counts_are_informational_and_leave_confirmed_events_intact(store: Store) -> None:
    pending_events = recover(store, DAY, (paper(1), paper(2, " \n\t")))
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(3),))
    states = store.review_date_readiness(active_configs=ACTIVE)
    assert [(item.day, item.total_papers, item.abstracts_ready, item.missing_abstracts) for item in states] == [
        (DAY, 2, 1, 1), (later, 1, 1, 0),
    ]
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY, later)
    assert store.events_for_date(DAY, active_configs=ACTIVE) == pending_events
    snapshot = store.review_snapshot(DAY, active_configs=ACTIVE)
    assert (snapshot.abstracts_ready, snapshot.missing_abstracts) == (1, 1)
    assert store.review_date_links(later, active_configs=ACTIVE).previous_date == DAY
    assert store.unreviewed_papers_missing_abstracts(active_configs=ACTIVE) == (paper(2).arxiv_id,)


def test_missing_abstracts_allow_reading_position_and_finish(store: Store) -> None:
    events = recover(store, DAY, (paper(1), paper(2, "")))
    snapshot = store.review_snapshot(DAY, active_configs=ACTIVE)
    assert snapshot.events == events
    assert (snapshot.abstracts_ready, snapshot.missing_abstracts) == (1, 1)
    assert [bool(value.abstract) for value in snapshot.papers] == [True, False]
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    store.record_position(
        DAY, snapshot.snapshot_revision, events[0].event_id,
        snapshot.profile_revision, snapshot.projection_revision,
        active_configs=ACTIVE,
    )
    result = store.finish_date(
        DAY, through_revision=snapshot.snapshot_revision,
        finished_at=NOW, active_configs=ACTIVE,
    )
    assert result.reviewed_count == 2
    assert all(event.reviewed_at == NOW for event in store.events_for_date(DAY))


def test_partial_snapshot_readiness_includes_newer_papers_and_changes_on_recovery(store: Store) -> None:
    original = recover(store, DAY, (paper(1),))[0]
    revision = store.review_queue_revision()
    recover(store, DAY, (paper(1), paper(2, "")))
    snapshot = store.review_snapshot(
        DAY, through_revision=revision, active_configs=ACTIVE,
    )
    assert snapshot.events == (original,)
    assert (snapshot.abstracts_ready, snapshot.missing_abstracts) == (1, 1)
    store.apply_article_snapshot(paper(2), ())
    recovered = store.review_snapshot(DAY, active_configs=ACTIVE)
    assert (recovered.abstracts_ready, recovered.missing_abstracts) == (2, 0)
    assert recovered.projection_revision > snapshot.projection_revision
    with pytest.raises(ReviewSnapshotConflict):
        store.record_position(
            DAY, snapshot.snapshot_revision, original.event_id,
            snapshot.profile_revision, snapshot.projection_revision,
            active_configs=ACTIVE,
        )


def test_snapshot_counts_and_papers_share_one_database_read(store: Store, monkeypatch) -> None:
    recover(store, DAY, (paper(1), paper(2, "")))
    original = store._metadata_from_connection

    def recover_during_read(connection, arxiv_id):
        monkeypatch.setattr(store, "_metadata_from_connection", original)
        Store(store.database_path).apply_article_snapshot(paper(2), ())
        return original(connection, arxiv_id)

    monkeypatch.setattr(store, "_metadata_from_connection", recover_during_read)
    snapshot = store.review_snapshot(DAY, active_configs=ACTIVE)
    assert (snapshot.abstracts_ready, snapshot.missing_abstracts) == (1, 1)
    assert snapshot.papers[1].abstract == ""
    fresh = store.review_snapshot(DAY, active_configs=ACTIVE)
    assert (fresh.abstracts_ready, fresh.missing_abstracts) == (2, 0)
    assert fresh.papers[1].abstract == paper(2).abstract


def test_active_projection_and_coverage_determine_abstract_counts(store: Store) -> None:
    recover(store, DAY, (paper(1),))
    recover(store, DAY, (paper(2, ""),), category="math.AT")
    other = CategoryConfig("math.AT", "math:AT", date(2026, 8, 1))
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    assert store.list_review_dates(active_configs=(*ACTIVE, other)) == (DAY,)
    assert store.review_date_readiness(active_configs=(*ACTIVE, other))[0].missing_abstracts == 1
    assert store.list_review_dates(active_configs=(*ACTIVE, replace(other, coverage_start=DAY + timedelta(days=1)))) == (DAY,)
    assert store.review_date_readiness(active_configs=()) == ()


def test_finish_snapshot_does_not_mark_newer_missing_papers_reviewed(store: Store) -> None:
    original = recover(store, DAY, (paper(1),))[0]
    snapshot = store.review_snapshot(DAY, active_configs=ACTIVE)
    recover(store, DAY, (paper(1), paper(2, "")))
    assert store.list_review_dates(through_revision=snapshot.snapshot_revision, active_configs=ACTIVE) == (DAY,)
    current = store.review_snapshot(DAY, through_revision=snapshot.snapshot_revision, active_configs=ACTIVE)
    assert current.events == (original,)
    store.record_position(DAY, snapshot.snapshot_revision, original.event_id, snapshot.profile_revision, snapshot.projection_revision, active_configs=ACTIVE)
    result = store.finish_date(DAY, through_revision=snapshot.snapshot_revision, finished_at=NOW, active_configs=ACTIVE)
    assert result.reviewed_count == 1
    assert [item.reviewed_at for item in store.events_for_date(DAY)] == [NOW, None]


def test_finish_all_includes_dates_missing_abstracts(store: Store) -> None:
    recover(store, DAY, (paper(1), paper(2, "")))
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(3),))
    revision = store.review_queue_revision()
    result = store.finish_all(through_revision=revision, finished_at=NOW, active_configs=ACTIVE)
    assert result.reviewed_count == 3
    assert all(item.reviewed_at == NOW for item in store.events_for_date(DAY))
    assert store.events_for_date(later)[0].reviewed_at == NOW
    assert store.review_snapshot(later).last_finished_revision == revision


def test_metadata_recovery_invalidates_stale_finish_all_without_rewriting_events(store: Store) -> None:
    first = recover(store, DAY, (paper(1, ""),))[0]
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(2),))
    snapshot = store.review_snapshot(later, active_configs=ACTIVE)
    store.apply_article_snapshot(paper(1), ())
    queue, projection = store.review_revisions()
    assert queue == snapshot.snapshot_revision
    assert projection == snapshot.projection_revision + 1
    assert store.events_for_date(DAY)[0] == first
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY, later)
    with pytest.raises(ReviewSnapshotConflict):
        store.finish_all(
            through_revision=snapshot.snapshot_revision, finished_at=NOW,
            profile_revision=snapshot.profile_revision,
            projection_revision=snapshot.projection_revision, active_configs=ACTIVE,
        )
    assert all(item.reviewed_at is None for day in (DAY, later) for item in store.events_for_date(day))


def test_sparse_metadata_cannot_erase_a_known_abstract_or_change_readiness(store: Store) -> None:
    recover(store, DAY, (paper(1),))
    old_revisions = store.review_revisions()
    incoming = replace(paper(1, " \n"), title="Updated synthetic title")
    recover(store, DAY, (incoming,))
    expected = replace(incoming, abstract=paper(1).abstract)
    assert store.article_metadata(paper(1).arxiv_id) == expected
    assert store.review_revisions() == old_revisions
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    payload = {
        "abstract": expected.abstract, "arxiv_id": expected.arxiv_id,
        "authors": expected.authors, "categories": expected.categories,
        "comments": expected.comments, "doi": expected.doi,
        "journal_ref": expected.journal_ref,
        "primary_category": expected.primary_category, "title": expected.title,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    connection = sqlite3.connect(store.database_path)
    try:
        assert connection.execute("SELECT metadata_hash FROM articles WHERE arxiv_id = ?", (expected.arxiv_id,)).fetchone()[0] == digest
    finally:
        connection.close()


def test_previously_reviewed_missing_papers_remain_recoverable(store: Store) -> None:
    recover(store, DAY, (paper(1, ""),))
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("UPDATE canonical_events SET reviewed_at = ?", (NOW.isoformat().replace("+00:00", "Z"),))
        connection.commit()
    finally:
        connection.close()
    assert store.unreviewed_papers_missing_abstracts(active_configs=ACTIVE) == (paper(1).arxiv_id,)
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    store.apply_article_snapshot(paper(1), ())
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    assert store.events_for_date(DAY)[0].reviewed_at == NOW


def test_deleted_missing_papers_keep_their_confirmed_dates(store: Store) -> None:
    recover(store, DAY, (paper(1, ""),))
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("UPDATE articles SET is_deleted = 1")
        connection.commit()
    finally:
        connection.close()
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY,)
    assert store.review_date_readiness(active_configs=ACTIVE)[0].missing_abstracts == 1


def test_finish_all_older_snapshot_leaves_new_missing_papers_unreviewed(store: Store) -> None:
    recover(store, DAY, (paper(1),))
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(2),))
    through_revision = store.review_queue_revision()
    recover(store, DAY, (paper(1), paper(3, "")))
    result = store.finish_all(through_revision=through_revision, finished_at=NOW, active_configs=ACTIVE)
    assert result.reviewed_count == 2
    assert [event.reviewed_at for event in store.events_for_date(DAY)] == [NOW, None]
    assert store.events_for_date(later)[0].reviewed_at == NOW


def test_abstract_count_and_projection_only_track_event_backed_recovery(store: Store) -> None:
    store.apply_article_snapshot(paper(1, ""), ())
    initial = store.review_revisions()
    store.apply_article_snapshot(paper(1), ())
    assert store.review_revisions() == initial
    recover(store, DAY, (paper(1), paper(2)))
    state = store.review_date_readiness(active_configs=ACTIVE)[0]
    assert state.total_papers == state.abstracts_ready == 2
    assert state.missing_abstracts == 0
    initial = store.review_revisions()
    store.apply_article_snapshot(replace(paper(1), abstract="Updated complete abstract."), ())
    assert store.review_revisions() == initial


@pytest.mark.parametrize("retain_other_category", [False, True])
def test_correcting_an_announcement_invalidates_old_finish_all(
    store: Store, retain_other_category: bool,
) -> None:
    recover(store, DAY, (paper(1), paper(2, "")))
    if retain_other_category:
        recover(store, DAY, (paper(2, ""),), category="math.AT")
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(3),))
    assert store.list_review_dates(active_configs=ACTIVE) == (DAY, later)
    snapshot = store.review_snapshot(later, active_configs=ACTIVE)
    old_ready_event = store.events_for_date(DAY, active_configs=ACTIVE)[0]

    recover(store, DAY, (paper(1),))

    assert store.list_review_dates(active_configs=ACTIVE) == (DAY, later)
    assert store.events_for_date(DAY, active_configs=ACTIVE) == (old_ready_event,)
    assert store.review_revisions()[1] > snapshot.projection_revision
    if retain_other_category:
        assert len(store.events_for_date(DAY)) == 2
    with pytest.raises(ReviewSnapshotConflict):
        store.finish_all(
            through_revision=snapshot.snapshot_revision, finished_at=NOW,
            profile_revision=snapshot.profile_revision,
            projection_revision=snapshot.projection_revision, active_configs=ACTIVE,
        )
    assert all(event.reviewed_at is None for day in (DAY, later) for event in store.events_for_date(day))


def test_new_active_support_for_an_existing_event_invalidates_old_finish_all(store: Store) -> None:
    recover(store, DAY, (paper(1),), category="math.AT")
    later = DAY + timedelta(days=1)
    recover(store, later, (paper(2),))
    snapshot = store.review_snapshot(later, active_configs=ACTIVE)
    assert store.list_review_dates(active_configs=ACTIVE) == (later,)
    original = store.events_for_date(DAY)[0]

    recover(store, DAY, (paper(1),))

    assert store.list_review_dates(active_configs=ACTIVE) == (DAY, later)
    assert store.events_for_date(DAY)[0].event_id == original.event_id
    assert store.review_queue_revision() == snapshot.snapshot_revision
    with pytest.raises(ReviewSnapshotConflict):
        store.finish_all(
            through_revision=snapshot.snapshot_revision, finished_at=NOW,
            profile_revision=snapshot.profile_revision,
            projection_revision=snapshot.projection_revision, active_configs=ACTIVE,
        )
