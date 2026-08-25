from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    CategoryConfig,
    EnrichmentStatus,
    EvidenceSource,
    OaiArticle,
    PaperMetadata,
    PaperVersion,
    SourceObservation,
    VersionResolution,
)
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import ReviewSnapshotConflict, Store


DAY = date(2026, 8, 24)
OBSERVED_AT = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)


def _metadata(arxiv_id: str = "2608.24001") -> PaperMetadata:
    return PaperMetadata(
        arxiv_id=arxiv_id,
        title="Confirmed daily-list storage",
        authors=("A. Example",),
        abstract="A deterministic storage fixture.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )


def _observation(
    source: EvidenceSource,
    *,
    source_key: str,
    daily_list_date: date | None,
    announced_version: int | None,
) -> SourceObservation:
    return SourceObservation(
        source_key=source_key,
        arxiv_id="2608.24001",
        source=source,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        announced_version=announced_version,
        list_position=0 if source is not EvidenceSource.OAI else None,
        oai_datestamp=None,
        response_sha256="a" * 64,
        observed_at=OBSERVED_AT,
    )


def _store(tmp_path: Path) -> Store:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    return Store(database_path)


def _catchup_day(
    *,
    entries: tuple[CatchupEntry, ...],
    status: EnrichmentStatus = EnrichmentStatus.COMPLETE,
    raw_sha256: str = "b" * 64,
) -> CatchupDay:
    return CatchupDay(
        category="cs.SE",
        mailing_date=DAY,
        status=status,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=DAY,
                page=1,
                total_pages=1,
                entries=entries,
                raw_sha256=raw_sha256,
            ),
        ),
        error_code=None,
        error_message=None,
    )


def test_hidden_observations_do_not_create_or_revise_review_queue(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    batch = AtomBatch(
        category="cs.SE",
        mailing_date=DAY,
        entries=(
            AtomEntry(
                metadata=_metadata(),
                announced_version=1,
                published_at=OBSERVED_AT,
                announce_type=AnnounceType.NEW,
                mailing_date=DAY,
                position=0,
            ),
        ),
        raw_sha256="a" * 64,
        fetched_at=OBSERVED_AT,
    )
    observation = _observation(
        EvidenceSource.ATOM,
        source_key="atom:cs.SE:2026-08-24:0:2608.24001v1",
        daily_list_date=DAY,
        announced_version=1,
    )

    assert store.apply_atom_batch(batch, (observation,)) == ()
    assert store.canonical_event_count() == 0
    assert store.review_queue_revision() == 0
    assert store.source_observations("2608.24001") == (observation,)
    # An Atom publication timestamp is not an arXiv submission timestamp.
    assert store.article_versions("2608.24001") == ()


def test_catchup_observation_creates_the_only_visible_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    result = _catchup_day(entries=(entry,))
    observation = SourceObservation(
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        arxiv_id="2608.24001",
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=DAY,
        announced_version=None,
        list_position=0,
        oai_datestamp=None,
        response_sha256="b" * 64,
        observed_at=OBSERVED_AT,
    )

    events = store.apply_catchup_day(result, (observation,), OBSERVED_AT)

    assert len(events) == 1
    assert events[0].daily_list_date == DAY
    assert events[0].announced_version is None
    assert events[0].version_resolution is VersionResolution.UNCONFIRMED
    assert tuple(item.source_key for item in events[0].observations) == (
        observation.source_key,
    )
    assert store.canonical_event_count() == 1
    assert store.review_queue_revision() == 1


def test_replaying_a_catchup_day_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    result = _catchup_day(entries=(entry,))
    observation = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )

    first = store.apply_catchup_day(result, (observation,), OBSERVED_AT)
    second = store.apply_catchup_day(result, (observation,), OBSERVED_AT)

    assert second == first
    assert store.review_queue_revision() == 1


def test_oai_version_refines_unconfirmed_event_without_revising_queue(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    catchup = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )
    original = store.apply_catchup_day(
        _catchup_day(entries=(entry,)), (catchup,), OBSERVED_AT
    )[0]
    store.finish_date(
        DAY,
        through_revision=original.queue_revision,
        finished_at=OBSERVED_AT,
    )
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 8, 1))
    run_id = store.begin_sync_run(
        "cs.SE", "incremental", date(2026, 8, 1), None, OBSERVED_AT
    )
    version = PaperVersion(
        number=1,
        submitted_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    oai = SourceObservation(
        source_key="oai:2608.24001:v1",
        arxiv_id="2608.24001",
        source=EvidenceSource.OAI,
        category=None,
        announce_type=None,
        daily_list_date=None,
        announced_version=1,
        list_position=None,
        oai_datestamp=DAY,
        response_sha256="c" * 64,
        observed_at=OBSERVED_AT,
    )

    refined = store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiArticle(
                oai_identifier="oai:arXiv.org:2608.24001",
                oai_datestamp=DAY,
                set_specs=("cs:SE",),
                metadata=_metadata(),
                versions=(version,),
            ),
        ),
        (oai,),
        "c" * 64,
        OBSERVED_AT,
    )[0]

    assert refined.event_id == original.event_id
    assert refined.queue_revision == original.queue_revision
    assert refined.reviewed_at == OBSERVED_AT
    assert refined.announced_version == 1
    assert refined.version_resolution is VersionResolution.CHRONOLOGY_MATCHED
    assert {item.source_key for item in refined.observations} == {
        catchup.source_key,
        oai.source_key,
    }


def test_concrete_version_replacement_reopens_the_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    catchup = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )
    store.apply_catchup_day(
        _catchup_day(entries=(entry,)), (catchup,), OBSERVED_AT
    )
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 8, 1))
    run_id = store.begin_sync_run(
        "cs.SE", "incremental", date(2026, 8, 1), None, OBSERVED_AT
    )
    version_one = PaperVersion(
        1, datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    )
    first_oai = SourceObservation(
        source_key="oai:2608.24001:v1",
        arxiv_id="2608.24001",
        source=EvidenceSource.OAI,
        category=None,
        announce_type=None,
        daily_list_date=None,
        announced_version=1,
        list_position=None,
        oai_datestamp=DAY,
        response_sha256="1" * 64,
        observed_at=OBSERVED_AT,
    )
    record_one = OaiArticle(
        oai_identifier="oai:arXiv.org:2608.24001",
        oai_datestamp=DAY,
        set_specs=("cs:SE",),
        metadata=_metadata(),
        versions=(version_one,),
    )
    resolved = store.apply_oai_page(
        run_id,
        "cs.SE",
        (record_one,),
        (first_oai,),
        "1" * 64,
        OBSERVED_AT,
    )[0]
    store.finish_date(
        DAY,
        through_revision=resolved.queue_revision,
        finished_at=OBSERVED_AT,
    )
    version_two = PaperVersion(
        2, datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    )
    second_oai = SourceObservation(
        source_key="oai:2608.24001:v2",
        arxiv_id="2608.24001",
        source=EvidenceSource.OAI,
        category=None,
        announce_type=None,
        daily_list_date=None,
        announced_version=2,
        list_position=None,
        oai_datestamp=DAY,
        response_sha256="2" * 64,
        observed_at=OBSERVED_AT,
    )
    record_two = OaiArticle(
        oai_identifier="oai:arXiv.org:2608.24001",
        oai_datestamp=DAY,
        set_specs=("cs:SE",),
        metadata=_metadata(),
        versions=(version_one, version_two),
    )

    replaced = store.apply_oai_page(
        run_id,
        "cs.SE",
        (record_two,),
        (second_oai,),
        "2" * 64,
        OBSERVED_AT,
    )[0]

    assert replaced.event_id == resolved.event_id
    assert replaced.announced_version == 2
    assert replaced.queue_revision > resolved.queue_revision
    assert replaced.reviewed_at is None


def test_corrected_daily_list_date_preserves_event_and_date_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old_day = DAY
    new_day = date(2026, 8, 25)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=old_day,
        position=0,
    )
    source_key = "catchup:cs.SE:corrected:0:2608.24001"
    original_observation = _observation(
        EvidenceSource.CATCHUP,
        source_key=source_key,
        daily_list_date=old_day,
        announced_version=None,
    )
    original = store.apply_catchup_day(
        _catchup_day(entries=(entry,)),
        (original_observation,),
        OBSERVED_AT,
    )[0]
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 8, 1))
    run_id = store.begin_sync_run(
        "cs.SE", "incremental", date(2026, 8, 1), None, OBSERVED_AT
    )
    version = PaperVersion(
        1, datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    )
    store.apply_oai_page(
        run_id,
        "cs.SE",
        (
            OaiArticle(
                oai_identifier="oai:arXiv.org:2608.24001",
                oai_datestamp=old_day,
                set_specs=("cs:SE",),
                metadata=_metadata(),
                versions=(version,),
            ),
        ),
        (
            SourceObservation(
                source_key="oai:2608.24001:v1",
                arxiv_id="2608.24001",
                source=EvidenceSource.OAI,
                category=None,
                announce_type=None,
                daily_list_date=None,
                announced_version=1,
                list_position=None,
                oai_datestamp=old_day,
                response_sha256="3" * 64,
                observed_at=OBSERVED_AT,
            ),
        ),
        "3" * 64,
        OBSERVED_AT,
    )
    store.finish_date(
        old_day,
        through_revision=store.review_queue_revision(),
        finished_at=OBSERVED_AT,
    )
    corrected_entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=new_day,
        position=0,
    )
    corrected_day = CatchupDay(
        category="cs.SE",
        mailing_date=new_day,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=new_day,
                page=1,
                total_pages=1,
                entries=(corrected_entry,),
                raw_sha256="4" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    corrected_observation = SourceObservation(
        source_key=source_key,
        arxiv_id="2608.24001",
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=new_day,
        announced_version=None,
        list_position=0,
        oai_datestamp=None,
        response_sha256="4" * 64,
        observed_at=OBSERVED_AT,
    )

    corrected = store.apply_catchup_day(
        corrected_day, (corrected_observation,), OBSERVED_AT
    )[0]

    assert corrected.event_id == original.event_id
    assert corrected.daily_list_date == new_day
    assert corrected.reviewed_at == OBSERVED_AT
    connection = sqlite3.connect(store.database_path)
    assert connection.execute(
        "SELECT daily_list_date FROM review_date_state"
    ).fetchall() == [(new_day.isoformat(),)]
    connection.close()


def test_changed_successful_catchup_content_removes_stale_membership(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    catchup = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )
    store.apply_catchup_day(
        _catchup_day(entries=(entry,)), (catchup,), OBSERVED_AT
    )

    store.apply_catchup_day(
        _catchup_day(entries=(), status=EnrichmentStatus.EMPTY),
        (),
        OBSERVED_AT,
    )

    assert store.canonical_event_count() == 0
    assert store.source_observations("2608.24001") == ()
    assert store.review_queue_revision() == 2


def test_active_projection_requires_in_coverage_catchup_support(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    metadata = PaperMetadata(
        arxiv_id="2608.24001",
        title="Confirmed daily-list storage",
        authors=("A. Example",),
        abstract="A deterministic storage fixture.",
        primary_category="cs.SE",
        categories=("cs.SE", "math.AT"),
    )
    for category, digest in (("cs.SE", "d"), ("math.AT", "e")):
        entry = CatchupEntry(
            metadata=metadata,
            section=AnnounceType.NEW,
            mailing_date=DAY,
            position=0,
        )
        result = CatchupDay(
            category=category,
            mailing_date=DAY,
            status=EnrichmentStatus.COMPLETE,
            pages=(
                CatchupPage(
                    category=category,
                    mailing_date=DAY,
                    page=1,
                    total_pages=1,
                    entries=(entry,),
                    raw_sha256=digest * 64,
                ),
            ),
            error_code=None,
            error_message=None,
        )
        observation = SourceObservation(
            source_key=f"catchup:{category}:2026-08-24:0:2608.24001",
            arxiv_id="2608.24001",
            source=EvidenceSource.CATCHUP,
            category=category,
            announce_type=AnnounceType.NEW,
            daily_list_date=DAY,
            announced_version=None,
            list_position=0,
            oai_datestamp=None,
            response_sha256=digest * 64,
            observed_at=OBSERVED_AT,
        )
        store.apply_catchup_day(result, (observation,), OBSERVED_AT)

    cs_only = store.events_for_date(
        DAY,
        active_configs=(CategoryConfig("cs.SE", "cs:SE", DAY),),
    )
    math_only = store.events_for_date(
        DAY,
        active_configs=(CategoryConfig("math.AT", "math:AT", DAY),),
    )

    assert len(cs_only) == len(math_only) == 1
    assert tuple(item.category for item in cs_only[0].observations) == ("cs.SE",)
    assert tuple(item.category for item in math_only[0].observations) == (
        "math.AT",
    )
    assert store.events_for_date(
        DAY,
        active_configs=(
            CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 25)),
        ),
    ) == ()
    assert store.events_for_date(DAY, active_configs=()) == ()


def test_stale_projection_finish_is_rejected_without_reviewing_events(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    observation = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )
    event = store.apply_catchup_day(
        _catchup_day(entries=(entry,)), (observation,), OBSERVED_AT
    )[0]
    connection = sqlite3.connect(store.database_path)
    connection.execute(
        """UPDATE profile_publication
           SET pending_revision = 1, pending_sha256 = ?, status = 'published'
           WHERE singleton = 1""",
        ("f" * 64,),
    )
    connection.execute(
        "UPDATE state_meta SET projection_revision = 1 WHERE singleton = 1"
    )
    connection.commit()
    connection.close()
    active = (CategoryConfig("cs.SE", "cs:SE", DAY),)
    snapshot = store.review_snapshot(DAY, active_configs=active)

    connection = sqlite3.connect(store.database_path)
    connection.execute(
        "UPDATE state_meta SET projection_revision = 2 WHERE singleton = 1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ReviewSnapshotConflict):
        store.finish_date(
            DAY,
            through_revision=snapshot.snapshot_revision,
            finished_at=OBSERVED_AT,
            profile_revision=snapshot.profile_revision,
            projection_revision=snapshot.projection_revision,
            active_configs=active,
        )

    assert store.review_event(event.event_id).reviewed_at is None


def test_new_event_on_finished_date_is_marked_recovered_after_finish(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_entry = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    first_observation = _observation(
        EvidenceSource.CATCHUP,
        source_key="catchup:cs.SE:2026-08-24:0:2608.24001",
        daily_list_date=DAY,
        announced_version=None,
    )
    first = store.apply_catchup_day(
        _catchup_day(entries=(first_entry,)),
        (first_observation,),
        OBSERVED_AT,
    )[0]
    store.finish_date(
        DAY,
        through_revision=first.queue_revision,
        finished_at=OBSERVED_AT,
    )
    second_metadata = _metadata("2608.24002")
    second_entry = CatchupEntry(
        metadata=second_metadata,
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=1,
    )
    second_observation = SourceObservation(
        source_key="catchup:cs.SE:2026-08-24:1:2608.24002",
        arxiv_id="2608.24002",
        source=EvidenceSource.CATCHUP,
        category="cs.SE",
        announce_type=AnnounceType.NEW,
        daily_list_date=DAY,
        announced_version=None,
        list_position=1,
        oai_datestamp=None,
        response_sha256="5" * 64,
        observed_at=OBSERVED_AT,
    )
    expanded_day = _catchup_day(
        entries=(first_entry, second_entry), raw_sha256="5" * 64
    )

    events = store.apply_catchup_day(
        expanded_day,
        (first_observation, second_observation),
        OBSERVED_AT,
    )
    by_id = {event.arxiv_id: event for event in events}

    assert by_id["2608.24001"].reviewed_at == OBSERVED_AT
    assert by_id["2608.24001"].recovered_after_finish is False
    assert by_id["2608.24002"].reviewed_at is None
    assert by_id["2608.24002"].recovered_after_finish is True


def test_removing_one_support_link_preserves_event_until_last_support_is_gone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    metadata = PaperMetadata(
        arxiv_id="2608.24001",
        title="Confirmed daily-list storage",
        authors=("A. Example",),
        abstract="A deterministic storage fixture.",
        primary_category="cs.SE",
        categories=("cs.SE", "math.AT"),
    )
    for category, digest in (("cs.SE", "6"), ("math.AT", "7")):
        entry = CatchupEntry(
            metadata=metadata,
            section=AnnounceType.NEW,
            mailing_date=DAY,
            position=0,
        )
        result = CatchupDay(
            category=category,
            mailing_date=DAY,
            status=EnrichmentStatus.COMPLETE,
            pages=(
                CatchupPage(
                    category=category,
                    mailing_date=DAY,
                    page=1,
                    total_pages=1,
                    entries=(entry,),
                    raw_sha256=digest * 64,
                ),
            ),
            error_code=None,
            error_message=None,
        )
        observation = SourceObservation(
            source_key=f"catchup:{category}:2026-08-24:0:2608.24001",
            arxiv_id="2608.24001",
            source=EvidenceSource.CATCHUP,
            category=category,
            announce_type=AnnounceType.NEW,
            daily_list_date=DAY,
            announced_version=None,
            list_position=0,
            oai_datestamp=None,
            response_sha256=digest * 64,
            observed_at=OBSERVED_AT,
        )
        store.apply_catchup_day(result, (observation,), OBSERVED_AT)
    original_revision = store.review_queue_revision()

    for index, category in enumerate(("cs.SE", "math.AT"), start=8):
        empty = CatchupDay(
            category=category,
            mailing_date=DAY,
            status=EnrichmentStatus.EMPTY,
            pages=(
                CatchupPage(
                    category=category,
                    mailing_date=DAY,
                    page=1,
                    total_pages=1,
                    entries=(),
                    raw_sha256=f"{index}" * 64,
                ),
            ),
            error_code=None,
            error_message=None,
        )
        store.apply_catchup_day(empty, (), OBSERVED_AT)
        if category == "cs.SE":
            assert store.canonical_event_count() == 1
            assert store.review_queue_revision() == original_revision

    assert store.canonical_event_count() == 0
    assert store.review_queue_revision() == original_revision + 1


def test_failed_multi_page_day_persists_no_partial_papers_or_observations(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    partial = CatchupEntry(
        metadata=_metadata(),
        section=AnnounceType.NEW,
        mailing_date=DAY,
        position=0,
    )
    failed = CatchupDay(
        category="cs.SE",
        mailing_date=DAY,
        status=EnrichmentStatus.FAILED,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=DAY,
                page=1,
                total_pages=2,
                entries=(partial,),
                raw_sha256="a" * 64,
            ),
        ),
        error_code="catchup_layout_changed",
        error_message="The arXiv catch-up page layout was not recognized.",
    )

    assert store.apply_catchup_day(failed, (), OBSERVED_AT) == ()
    assert store.source_observations() == ()
    assert store.canonical_event_count() == 0
    with pytest.raises(KeyError):
        store.article_metadata("2608.24001")
    assert store.catchup_day_records("cs.SE")[0].status.value == "failed"
