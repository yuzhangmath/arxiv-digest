from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from arxiv_digest.models import (
    AnnounceType,
    Confidence,
    DateBasis,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    EnrichmentStatus,
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store
from arxiv_digest.storage.store import EnrichmentDayRecord


def _metadata(arxiv_id: str = "2608.01001") -> PaperMetadata:
    return PaperMetadata(
        arxiv_id=arxiv_id,
        title="Synthetic Queue Systems",
        authors=("A. Example",),
        abstract="A fictional abstract about deterministic queues.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )


def _version(number: int = 1) -> PaperVersion:
    return PaperVersion(
        number=number,
        submitted_at=datetime(2026, 8, number, 12, tzinfo=timezone.utc),
    )


def _candidate(
    *,
    arxiv_id: str = "2608.01001",
    version: int | None = 1,
    day: date = date(2026, 8, 3),
    source_key: str = "atom:cs.SE:2026-08-03:0:2608.01001v1",
    category: str = "cs.SE",
    list_position: int = 0,
) -> EventCandidate:
    evidence = EventEvidence(
        source_key=source_key,
        source=EvidenceSource.ATOM,
        confidence=Confidence.CURRENT,
        category=category,
        announce_type=AnnounceType.NEW,
        mailing_date=day,
        announced_version=version,
        list_position=list_position,
        oai_datestamp=None,
        raw_sha256="0" * 64,
        observed_at=datetime(2026, 8, 3, 13, tzinfo=timezone.utc),
    )
    return EventCandidate(
        arxiv_id=arxiv_id,
        announced_version=version,
        effective_date=day,
        date_basis=DateBasis.FEED_MAILING,
        evidence=evidence,
    )


def _source_candidate(
    *,
    source: EvidenceSource,
    version: int | None,
    day: date,
    source_key: str,
    category: str = "cs.SE",
) -> EventCandidate:
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
        source_key=source_key,
        source=source,
        confidence=confidence,
        category=category,
        announce_type=(
            AnnounceType.NEW if version == 1 else AnnounceType.CROSS
        ),
        mailing_date=(None if source is EvidenceSource.OAI else day),
        announced_version=version,
        list_position=(None if source is EvidenceSource.OAI else 0),
        oai_datestamp=(
            date(2026, 8, 20) if source is EvidenceSource.OAI else None
        ),
        raw_sha256="d" * 64,
        observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    return EventCandidate(
        arxiv_id="2608.01001",
        announced_version=version,
        effective_date=day,
        date_basis=date_basis,
        evidence=evidence,
    )


def test_apply_event_batch_creates_a_review_event_atomically(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)

    result = store.apply_event_batch(
        _metadata(), (_version(),), (_candidate(),)
    )

    assert len(result) == 1
    event = result[0]
    assert event.arxiv_id == "2608.01001"
    assert event.announced_version == 1
    assert event.queue_revision == 1
    assert event.evidence[0].source_key.startswith("atom:")
    assert store.events_for_date(date(2026, 8, 3)) == result


def test_candidate_mailing_evidence_is_category_and_window_bounded(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.apply_event_batch(
        _metadata(),
        (_version(),),
        (
            _candidate(day=date(2026, 8, 3)),
            _candidate(
                category="math.LO",
                day=date(2026, 8, 4),
                source_key="atom:math.LO:2026-08-04:0:2608.01001v1",
            ),
        ),
    )
    store.apply_event_batch(
        _metadata("2608.01002"),
        (_version(),),
        (
            _candidate(
                arxiv_id="2608.01002",
                day=date(2026, 5, 1),
                source_key="atom:cs.SE:2026-05-01:0:2608.01002v1",
            ),
        ),
    )

    assert store.candidate_mailing_evidence(
        "cs.SE", date(2026, 8, 1), date(2026, 8, 31)
    ) == (("2608.01001", date(2026, 8, 3)),)


def test_source_key_replay_is_idempotent_and_does_not_consume_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    first_candidate = _candidate()

    original = store.apply_event_batch(
        _metadata(), (_version(),), (first_candidate,)
    )
    replay = store.apply_event_batch(
        _metadata(), (_version(),), (first_candidate,)
    )
    next_event = store.apply_event_batch(
        _metadata("2608.01002"),
        (_version(),),
        (
            _candidate(
                arxiv_id="2608.01002",
                source_key="atom:cs.SE:2026-08-03:1:2608.01002v1",
            ),
        ),
    )

    assert replay == original
    assert len(store.events_for_date(date(2026, 8, 3))) == 2
    assert next_event[0].queue_revision == 2


def test_overlapping_categories_merge_into_one_versioned_daily_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    metadata = PaperMetadata(
        arxiv_id="2608.01001",
        title="Synthetic Queue Systems",
        authors=("A. Example",),
        abstract="A fictional abstract about deterministic queues.",
        primary_category="cs.SE",
        categories=("cs.SE", "math.LO"),
    )

    result = store.apply_event_batch(
        metadata,
        (_version(),),
        (
            _candidate(),
            _candidate(
                category="math.LO",
                list_position=4,
                source_key="atom:math.LO:2026-08-03:4:2608.01001v1",
            ),
        ),
    )

    assert len(result) == 1
    assert result[0].queue_revision == 1
    assert {item.category for item in result[0].evidence} == {
        "cs.SE",
        "math.LO",
    }


def test_finish_date_does_not_review_later_discovery(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    first = store.apply_event_batch(
        _metadata(), (_version(),), (_candidate(),)
    )[0]
    second = store.apply_event_batch(
        _metadata("2608.01002"),
        (_version(),),
        (
            _candidate(
                arxiv_id="2608.01002",
                source_key="atom:cs.SE:2026-08-03:1:2608.01002v1",
            ),
        ),
    )[0]

    result = store.finish_date(
        date(2026, 8, 3),
        through_revision=first.queue_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert result.reviewed_count == 1
    assert store.review_event(first.event_id).reviewed_at is not None
    assert store.review_event(second.event_id).reviewed_at is None


def test_beginning_incremental_sync_updates_only_incremental_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, pending_backfill_start,
               pending_backfill_until
           ) VALUES (?, ?, ?, ?, ?)""",
        ("cs.SE", "cs:SE", "2026-07-01", "2026-06-01", "2026-06-30"),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)

    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 7, 1),
        None,
        datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    check = sqlite3.connect(database_path)
    assert check.execute(
        "SELECT run_kind, status FROM sync_runs WHERE run_id = ?", (run_id,)
    ).fetchone() == ("incremental", "running")
    assert check.execute(
        """SELECT status, last_attempt_at, pending_backfill_start,
                  pending_backfill_until
           FROM category_sync_state WHERE category = 'cs.SE'"""
    ).fetchone() == (
        "syncing",
        "2026-08-03T00:00:00Z",
        "2026-06-01",
        "2026-06-30",
    )
    check.close()


def test_completing_incremental_sync_advances_checkpoint_from_response_day(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(category, set_spec, coverage_start)
           VALUES (?, ?, ?)""",
        ("cs.SE", "cs:SE", "2026-07-01"),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)
    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 7, 1),
        None,
        datetime(2026, 8, 3, tzinfo=timezone.utc),
    )

    store.complete_incremental_run(
        run_id,
        date(2026, 8, 4),
        datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 4, 2, tzinfo=timezone.utc),
    )

    check = sqlite3.connect(database_path)
    assert check.execute(
        """SELECT status, final_response_at, completed_at
           FROM sync_runs WHERE run_id = ?""",
        (run_id,),
    ).fetchone() == (
        "completed",
        "2026-08-04T01:00:00Z",
        "2026-08-04T02:00:00Z",
    )
    assert check.execute(
        """SELECT completed_through_utc, status, last_success_at,
                  last_error_code
           FROM category_sync_state WHERE category = 'cs.SE'"""
    ).fetchone() == (
        "2026-08-04",
        "idle",
        "2026-08-04T02:00:00Z",
        None,
    )
    check.close()


def test_backfill_completion_moves_only_coverage_and_clears_pending_bounds(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               status, last_error_code, last_error_message
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "cs:SE",
            "2026-07-01",
            "2026-08-04",
            "failed",
            "incremental_failure",
            "safe diagnostic",
        ),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)

    store.set_pending_backfill(
        "cs.SE", date(2026, 6, 1), date(2026, 7, 1)
    )
    run_id = store.begin_sync_run(
        "cs.SE",
        "coverage_backfill",
        date(2026, 6, 1),
        date(2026, 6, 30),
        datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    store.complete_backfill_run(
        run_id,
        date(2026, 6, 1),
        datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 5, 2, tzinfo=timezone.utc),
    )

    check = sqlite3.connect(database_path)
    assert check.execute(
        """SELECT coverage_start, completed_through_utc,
                  pending_backfill_start, pending_backfill_until,
                  status, last_error_code
           FROM category_sync_state WHERE category = 'cs.SE'"""
    ).fetchone() == (
        "2026-06-01",
        "2026-08-04",
        None,
        None,
        "failed",
        "incremental_failure",
    )
    assert check.execute(
        "SELECT status FROM sync_runs WHERE run_id = ?", (run_id,)
    ).fetchone() == ("completed",)
    check.close()


def test_failing_backfill_does_not_overwrite_incremental_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               pending_backfill_start, pending_backfill_until, status,
               last_error_code
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "cs:SE",
            "2026-07-01",
            "2026-08-04",
            "2026-06-01",
            "2026-06-30",
            "idle",
            "old_incremental_code",
        ),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)
    run_id = store.begin_sync_run(
        "cs.SE",
        "coverage_backfill",
        date(2026, 6, 1),
        date(2026, 6, 30),
        datetime(2026, 8, 5, tzinfo=timezone.utc),
    )

    store.fail_sync_run(
        run_id,
        "fixture_failure",
        "safe synthetic failure",
        datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
    )

    check = sqlite3.connect(database_path)
    assert check.execute(
        """SELECT status, failed_at, error_code, error_message
           FROM sync_runs WHERE run_id = ?""",
        (run_id,),
    ).fetchone() == (
        "failed",
        "2026-08-05T01:00:00Z",
        "fixture_failure",
        "safe synthetic failure",
    )
    assert check.execute(
        """SELECT completed_through_utc, pending_backfill_start,
                  pending_backfill_until, status, last_error_code
           FROM category_sync_state WHERE category = 'cs.SE'"""
    ).fetchone() == (
        "2026-08-04",
        "2026-06-01",
        "2026-06-30",
        "idle",
        "old_incremental_code",
    )
    check.close()


def test_oai_page_builds_stable_complete_article_snapshots(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(category, set_spec, coverage_start)
           VALUES (?, ?, ?)""",
        ("cs.SE", "cs:SE", "2026-07-01"),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)
    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 7, 1),
        None,
        datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    later = OaiArticle(
        oai_identifier="oai:arXiv.org:2608.01002",
        oai_datestamp=date(2026, 8, 3),
        set_specs=("cs:SE",),
        metadata=_metadata("2608.01002"),
        versions=(_version(1),),
    )
    earlier_metadata = PaperMetadata(
        arxiv_id="2608.01001",
        title="Synthetic Queue Systems",
        authors=("A. Example", "B. Example"),
        abstract="A fictional abstract about deterministic queues.",
        primary_category="cs.SE",
        categories=("cs.SE", "math.LO"),
    )
    earlier = OaiArticle(
        oai_identifier="oai:arXiv.org:2608.01001",
        oai_datestamp=date(2026, 8, 2),
        set_specs=("cs:SE", "math:LO"),
        metadata=earlier_metadata,
        versions=(_version(1), _version(2)),
    )

    store.apply_oai_page(
        run_id,
        "cs.SE",
        (later, earlier),
        (),
        "a" * 64,
        datetime(2026, 8, 3, 4, tzinfo=timezone.utc),
    )

    snapshots = store.article_snapshots("cs.SE")
    assert [item.metadata.arxiv_id for item in snapshots] == [
        "2608.01001",
        "2608.01002",
    ]
    first = snapshots[0]
    assert first.category == "cs.SE"
    assert first.metadata == earlier_metadata
    assert first.versions == (_version(1), _version(2))
    assert first.observed_categories == ("cs.SE", "math.LO")
    assert first.last_oai_datestamp == date(2026, 8, 2)
    assert first.last_raw_sha256 == "a" * 64
    assert first.last_seen_at == datetime(
        2026, 8, 3, 4, tzinfo=timezone.utc
    )
    assert store.article_snapshots(
        "cs.SE", {"2608.01002"}
    ) == (snapshots[1],)


def test_distinct_non_null_versions_never_merge(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    second = _candidate(
        version=2,
        source_key="atom:cs.SE:2026-08-03:1:2608.01001v2",
        list_position=1,
    )

    events = store.apply_event_batch(
        _metadata(), (_version(1), _version(2)), (_candidate(), second)
    )

    assert [event.announced_version for event in events] == [1, 2]
    assert [event.queue_revision for event in events] == [1, 2]


def test_invalid_event_rolls_back_article_versions_fts_and_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    invalid = _candidate(
        arxiv_id="2608.09999",
        source_key="atom:cs.SE:2026-08-03:1:2608.09999v1",
    )

    with pytest.raises(ValueError, match="metadata"):
        store.apply_event_batch(
            _metadata(), (_version(),), (_candidate(), invalid)
        )

    connection = sqlite3.connect(database_path)
    for table in (
        "articles",
        "article_versions",
        "review_events",
        "event_evidence",
        "papers_fts",
    ):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (
            0,
        )
    assert connection.execute(
        "SELECT queue_revision FROM state_meta WHERE singleton = 1"
    ).fetchone() == (0,)
    connection.close()


def test_library_search_covers_id_title_author_abstract_and_safe_punctuation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    title_match = PaperMetadata(
        arxiv_id="2608.02001",
        title="Widget Methods",
        authors=("A. Example",),
        abstract="A neutral fictional abstract.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    abstract_match = PaperMetadata(
        arxiv_id="2608.02002",
        title="Neutral Methods",
        authors=("B. Searcher",),
        abstract="A fictional abstract containing widget evidence.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    for position, metadata in enumerate((title_match, abstract_match)):
        store.apply_event_batch(
            metadata,
            (_version(),),
            (
                _candidate(
                    arxiv_id=metadata.arxiv_id,
                    source_key=(
                        f"atom:cs.SE:2026-08-03:{position}:"
                        f"{metadata.arxiv_id}v1"
                    ),
                    list_position=position,
                ),
            ),
        )
        store.save_paper(metadata.arxiv_id, 1)

    widget = store.search_library("widget", limit=10, offset=0)
    assert [item.metadata.arxiv_id for item in widget] == [
        "2608.02001",
        "2608.02002",
    ]
    assert store.search_library("B. Searcher", limit=10, offset=0)[
        0
    ].metadata.arxiv_id == "2608.02002"
    assert store.search_library("2608.02001", limit=10, offset=0)[
        0
    ].metadata.title == "Widget Methods"
    assert store.search_library("!!!", limit=10, offset=0) == ()


def test_saved_tombstone_stays_searchable_but_unavailable(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(category, set_spec, coverage_start)
           VALUES (?, ?, ?)""",
        ("cs.SE", "cs:SE", "2026-07-01"),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)
    store.apply_event_batch(_metadata(), (_version(),), (_candidate(),))
    store.save_paper("2608.01001", 1)
    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 7, 1),
        None,
        datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiTombstone(
                oai_identifier="oai:arXiv.org:2608.01001",
                oai_datestamp=date(2026, 8, 4),
                set_specs=("cs:SE",),
            ),
        ),
        (),
        "b" * 64,
        datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
    )

    result = store.search_library("Queue", limit=10, offset=0)
    assert len(result) == 1
    assert result[0].metadata.arxiv_id == "2608.01001"
    assert result[0].paper_available is False
    assert result[0].local_pdf_versions == ()


def test_tombstone_excludes_snapshots_and_future_event_batches(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    connection = open_database(database_path)
    connection.execute(
        """INSERT INTO category_sync_state(category, set_spec, coverage_start)
           VALUES (?, ?, ?)""",
        ("cs.SE", "cs:SE", "2026-07-01"),
    )
    connection.commit()
    connection.close()
    store = Store(database_path)
    run_id = store.begin_sync_run(
        "cs.SE",
        "incremental",
        date(2026, 7, 1),
        None,
        datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiArticle(
                oai_identifier="oai:arXiv.org:2608.01001",
                oai_datestamp=date(2026, 8, 3),
                set_specs=("cs:SE",),
                metadata=_metadata(),
                versions=(_version(),),
            ),
        ),
        (),
        "a" * 64,
        datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
    )
    assert len(store.article_snapshots("cs.SE")) == 1
    store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiTombstone(
                oai_identifier="oai:arXiv.org:2608.01001",
                oai_datestamp=date(2026, 8, 4),
                set_specs=("cs:SE",),
            ),
        ),
        (),
        "b" * 64,
        datetime(2026, 8, 4, 2, tzinfo=timezone.utc),
    )

    assert store.article_snapshots("cs.SE") == ()
    result = store.apply_event_batch(
        _metadata(),
        (_version(1), _version(2)),
        (
            _candidate(
                version=2,
                day=date(2026, 8, 5),
                source_key="atom:cs.SE:2026-08-05:0:2608.01001v2",
            ),
        ),
    )

    assert result == ()
    assert store.article_snapshots("cs.SE") == ()
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "SELECT is_deleted FROM articles WHERE arxiv_id = '2608.01001'"
    ).fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (
        0,
    )
    assert connection.execute(
        "SELECT queue_revision FROM state_meta WHERE singleton = 1"
    ).fetchone() == (0,)
    connection.close()


def test_review_snapshot_dates_links_and_position_are_durable(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    first = store.apply_event_batch(
        _metadata(), (_version(),), (_candidate(),)
    )[0]
    store.apply_event_batch(
        _metadata("2608.01002"),
        (_version(),),
        (
            _candidate(
                arxiv_id="2608.01002",
                day=date(2026, 8, 5),
                source_key="atom:cs.SE:2026-08-05:0:2608.01002v1",
            ),
        ),
    )

    snapshot = store.review_snapshot(date(2026, 8, 3))
    assert snapshot.snapshot_revision == 2
    assert snapshot.events == (first,)
    assert snapshot.anchor_event_id is None
    assert store.list_review_dates() == (
        date(2026, 8, 3),
        date(2026, 8, 5),
    )
    assert store.review_date_links(date(2026, 8, 3)).next_date == date(
        2026, 8, 5
    )

    position = store.record_position(
        date(2026, 8, 3), snapshot.snapshot_revision, first.event_id, 7
    )
    assert position.anchor_event_id == first.event_id
    resumed = store.review_snapshot(date(2026, 8, 3))
    assert resumed.anchor_event_id == first.event_id
    assert resumed.profile_revision == 7


def test_enrichment_day_records_exact_empty_and_failed_states(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)

    store.record_enrichment_day(
        EnrichmentDayRecord(
            category="cs.SE",
            mailing_date=date(2026, 8, 3),
            source="catchup",
            status=EnrichmentStatus.EMPTY,
            fetched_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            raw_sha256="c" * 64,
        )
    )

    connection = sqlite3.connect(database_path)
    assert connection.execute(
        """SELECT status, fetched_at, raw_sha256, error_code
           FROM enrichment_days
           WHERE category = 'cs.SE' AND mailing_date = '2026-08-03'
             AND source = 'catchup'"""
    ).fetchone() == (
        "empty",
        "2026-08-04T00:00:00Z",
        "c" * 64,
        None,
    )
    connection.close()


def test_stronger_evidence_relocates_unreviewed_event_without_changing_id(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    inferred = _source_candidate(
        source=EvidenceSource.OAI,
        version=1,
        day=date(2026, 8, 1),
        source_key="oai:cs:SE:2608.01001:v1",
    )
    original = store.apply_event_batch(
        _metadata(), (_version(),), (inferred,)
    )[0]
    store.record_position(
        date(2026, 8, 1), original.queue_revision, original.event_id, 4
    )
    current = _source_candidate(
        source=EvidenceSource.ATOM,
        version=1,
        day=date(2026, 8, 3),
        source_key="atom:cs.SE:2026-08-03:2608.01001:v1:new",
    )

    upgraded = store.apply_event_batch(
        _metadata(), (_version(),), (current,)
    )[0]

    assert upgraded.event_id == original.event_id
    assert upgraded.effective_date == date(2026, 8, 3)
    assert upgraded.date_basis is DateBasis.FEED_MAILING
    assert upgraded.confidence is Confidence.CURRENT
    assert upgraded.queue_revision > original.queue_revision
    assert len(upgraded.evidence) == 2
    assert store.review_snapshot(date(2026, 8, 1)).anchor_event_id == original.event_id


def test_strength_only_upgrade_never_reopens_reviewed_event(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    inferred = _source_candidate(
        source=EvidenceSource.OAI,
        version=1,
        day=date(2026, 8, 1),
        source_key="oai:cs:SE:2608.01001:v1",
    )
    original = store.apply_event_batch(
        _metadata(), (_version(),), (inferred,)
    )[0]
    store.finish_date(
        date(2026, 8, 1),
        through_revision=original.queue_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    current = _source_candidate(
        source=EvidenceSource.ATOM,
        version=1,
        day=date(2026, 8, 3),
        source_key="atom:cs.SE:2026-08-03:2608.01001:v1:new",
    )

    upgraded = store.apply_event_batch(
        _metadata(), (_version(),), (current,)
    )[0]

    assert upgraded.event_id == original.event_id
    assert upgraded.reviewed_at == datetime(2026, 8, 4, tzinfo=timezone.utc)
    assert upgraded.queue_revision == original.queue_revision


@pytest.mark.parametrize("atom_first", [True, False])
def test_same_day_atom_and_versionless_catchup_merge_order_independently(
    tmp_path: Path,
    atom_first: bool,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    atom = _source_candidate(
        source=EvidenceSource.ATOM,
        version=1,
        day=date(2026, 8, 3),
        source_key="atom:cs.SE:2026-08-03:2608.01001:v1:new",
    )
    catchup = _source_candidate(
        source=EvidenceSource.CATCHUP,
        version=None,
        day=date(2026, 8, 3),
        source_key="catchup:cs.SE:2026-08-03:2608.01001:new",
    )
    first, second = (atom, catchup) if atom_first else (catchup, atom)

    initial = store.apply_event_batch(_metadata(), (_version(),), (first,))[0]
    merged = store.apply_event_batch(_metadata(), (_version(),), (second,))[0]

    assert merged.event_id == initial.event_id
    assert merged.announced_version == 1
    assert merged.effective_date == date(2026, 8, 3)
    assert {item.source for item in merged.evidence} == {
        EvidenceSource.ATOM,
        EvidenceSource.CATCHUP,
    }
    assert store.events_for_date(date(2026, 8, 3)) == (merged,)


def test_relocation_collision_uses_lower_id_and_repoints_every_anchor(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    catchup = _source_candidate(
        source=EvidenceSource.CATCHUP,
        version=None,
        day=date(2026, 8, 3),
        source_key="catchup:cs.SE:2026-08-03:2608.01001:new",
    )
    inferred = _source_candidate(
        source=EvidenceSource.OAI,
        version=1,
        day=date(2026, 8, 1),
        source_key="oai:cs:SE:2608.01001:v1",
    )
    first = store.apply_event_batch(_metadata(), (_version(),), (catchup,))[0]
    second = store.apply_event_batch(_metadata(), (_version(),), (inferred,))[0]
    store.record_position(date(2026, 8, 3), second.queue_revision, first.event_id, 1)
    store.record_position(date(2026, 8, 1), second.queue_revision, second.event_id, 2)
    store.finish_date(
        date(2026, 8, 1),
        through_revision=second.queue_revision,
        finished_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    current = _source_candidate(
        source=EvidenceSource.ATOM,
        version=1,
        day=date(2026, 8, 3),
        source_key="atom:cs.SE:2026-08-03:2608.01001:v1:new",
    )

    survivor = store.apply_event_batch(
        _metadata(), (_version(),), (current,)
    )[0]

    assert survivor.event_id == min(first.event_id, second.event_id)
    assert survivor.announced_version == 1
    assert survivor.effective_date == date(2026, 8, 3)
    assert survivor.reviewed_at is not None
    assert {item.source for item in survivor.evidence} == {
        EvidenceSource.ATOM,
        EvidenceSource.CATCHUP,
        EvidenceSource.OAI,
    }
    assert store.review_snapshot(date(2026, 8, 3)).anchor_event_id == survivor.event_id
    assert store.review_snapshot(date(2026, 8, 1)).anchor_event_id == survivor.event_id
    with pytest.raises(KeyError):
        store.review_event(max(first.event_id, second.event_id))
