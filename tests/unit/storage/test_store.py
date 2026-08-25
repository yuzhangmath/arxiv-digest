from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    EvidenceSource,
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
    SourceObservation,
    VersionResolution,
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


def test_article_snapshot_stores_metadata_and_versions_without_creating_an_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)

    events = store.apply_article_snapshot(_metadata(), (_version(),))

    assert events == ()
    assert store.article_metadata("2608.01001") == _metadata()
    assert store.article_versions("2608.01001") == (_version(),)
    assert store.canonical_event_count() == 0
    assert store.review_queue_revision() == 0


def test_article_snapshot_reconciles_an_existing_catchup_backed_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    daily_list_date = date(2026, 8, 3)
    observed_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=daily_list_date,
        position=0,
    )
    observation = SourceObservation(
        source_key="catchup:cs.SE:2026-08-03:0:2608.01001",
        arxiv_id="2608.01001",
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        announced_version=None,
        list_position=0,
        oai_datestamp=None,
        response_sha256="c" * 64,
        observed_at=observed_at,
    )
    original = store.apply_catchup_day(
        CatchupDay(
            category="cs.SE",
            mailing_date=daily_list_date,
            status=EnrichmentStatus.COMPLETE,
            pages=(
                CatchupPage(
                    category="cs.SE",
                    mailing_date=daily_list_date,
                    page=1,
                    total_pages=1,
                    entries=(entry,),
                    raw_sha256="c" * 64,
                ),
            ),
            error_code=None,
            error_message=None,
        ),
        (observation,),
        observed_at,
    )[0]

    refined = store.apply_article_snapshot(_metadata(), (_version(),))

    assert len(refined) == 1
    assert refined[0].event_id == original.event_id
    assert refined[0].queue_revision == original.queue_revision
    assert refined[0].announced_version == 1
    assert refined[0].version_resolution is VersionResolution.CHRONOLOGY_MATCHED


def test_candidate_mailing_evidence_uses_durable_atom_and_catchup_observations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    observed_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    atom_day = date(2026, 8, 2)
    atom_metadata = _metadata("2608.01002")
    atom_observation = SourceObservation(
        source_key="atom:cs.SE:2026-08-02:0:2608.01002v1",
        arxiv_id=atom_metadata.arxiv_id,
        source=EvidenceSource.ATOM,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=atom_day,
        announced_version=1,
        list_position=0,
        oai_datestamp=None,
        response_sha256="a" * 64,
        observed_at=observed_at,
    )
    store.apply_atom_batch(
        AtomBatch(
            category="cs.SE",
            mailing_date=atom_day,
            entries=(
                AtomEntry(
                    metadata=atom_metadata,
                    announced_version=1,
                    published_at=observed_at,
                    announce_type=AnnounceType.NEW,
                    mailing_date=atom_day,
                    position=0,
                ),
            ),
            raw_sha256="a" * 64,
            fetched_at=observed_at,
        ),
        (atom_observation,),
    )
    catchup_day = date(2026, 8, 3)
    catchup_metadata = _metadata("2608.01003")
    catchup_entry = CatchupEntry(
        metadata=catchup_metadata,
        section=AnnounceType.NEW,
        mailing_date=catchup_day,
        position=0,
    )
    catchup_observation = SourceObservation(
        source_key="catchup:cs.SE:2026-08-03:0:2608.01003",
        arxiv_id=catchup_metadata.arxiv_id,
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=catchup_day,
        announced_version=None,
        list_position=0,
        oai_datestamp=None,
        response_sha256="b" * 64,
        observed_at=observed_at,
    )
    store.apply_catchup_day(
        CatchupDay(
            category="cs.SE",
            mailing_date=catchup_day,
            status=EnrichmentStatus.COMPLETE,
            pages=(
                CatchupPage(
                    category="cs.SE",
                    mailing_date=catchup_day,
                    page=1,
                    total_pages=1,
                    entries=(catchup_entry,),
                    raw_sha256="b" * 64,
                ),
            ),
            error_code=None,
            error_message=None,
        ),
        (catchup_observation,),
        observed_at,
    )

    assert store.candidate_mailing_evidence(
        "cs.SE", atom_day, catchup_day
    ) == (
        ("2608.01002", atom_day),
        ("2608.01003", catchup_day),
    )
    assert store.candidate_mailing_evidence(
        "math.LO", atom_day, catchup_day
    ) == ()


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
    for metadata in (title_match, abstract_match):
        store.apply_article_snapshot(metadata, (_version(),))
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
    store.apply_article_snapshot(_metadata(), (_version(),))
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


def test_tombstone_excludes_oai_snapshots_and_article_snapshots_do_not_revive_it(
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
    result = store.apply_article_snapshot(
        _metadata(), (_version(1), _version(2))
    )

    assert result == ()
    assert store.article_snapshots("cs.SE") == ()
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "SELECT is_deleted FROM articles WHERE arxiv_id = '2608.01001'"
    ).fetchone() == (1,)
    assert store.canonical_event_count() == 0
    assert connection.execute(
        "SELECT queue_revision FROM state_meta WHERE singleton = 1"
    ).fetchone() == (0,)
    connection.close()


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
    store.record_enrichment_day(
        EnrichmentDayRecord(
            category="cs.SE",
            mailing_date=date(2026, 8, 4),
            source="catchup",
            status=EnrichmentStatus.FAILED,
            fetched_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            error_code="catchup_layout_changed",
            error_message="Safe synthetic parser diagnostic.",
        )
    )

    records = store.enrichment_records("cs.SE")
    assert [(record.mailing_date, record.status) for record in records] == [
        (date(2026, 8, 3), EnrichmentStatus.EMPTY),
        (date(2026, 8, 4), EnrichmentStatus.FAILED),
    ]
    assert records[0].raw_sha256 == "c" * 64
    assert records[0].error_code is None
    assert records[1].raw_sha256 is None
    assert records[1].error_code == "catchup_layout_changed"
