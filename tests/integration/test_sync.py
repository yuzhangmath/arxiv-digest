from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from urllib.error import HTTPError

import pytest

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    CategoryConfig,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
    VersionResolution,
)
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
from arxiv_digest.setup import SetupService
from arxiv_digest.sources.catchup import catchup_observations
from arxiv_digest.sources.oai import OaiIdentify, OaiPage, OaiProtocolError
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store
from arxiv_digest.sync import SyncCancelled, SyncService


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def _paper(arxiv_id: str = "2608.04001", category: str = "cs.SE") -> PaperMetadata:
    return PaperMetadata(
        arxiv_id=arxiv_id,
        title="Synthetic Durable Synchronization",
        authors=("A. Fixture",),
        abstract="A deterministic integration-test record.",
        primary_category=category,
        categories=(category,),
    )


def _version(number: int, day: int) -> PaperVersion:
    return PaperVersion(
        number,
        datetime(
            2026,
            7 if day < 32 else 8,
            day if day < 32 else day - 31,
            9,
            tzinfo=timezone.utc,
        ),
    )


def _article(
    *,
    arxiv_id: str = "2608.04001",
    category: str = "cs.SE",
    versions: tuple[PaperVersion, ...] = (),
    datestamp: date = date(2026, 8, 20),
) -> OaiArticle:
    return OaiArticle(
        oai_identifier=f"oai:arXiv.org:{arxiv_id}",
        oai_datestamp=datestamp,
        set_specs=(category.replace(".", ":"),),
        metadata=_paper(arxiv_id, category),
        versions=versions,
    )


def _page(
    response_day: int,
    records: tuple[OaiArticle, ...] = (),
    token: str | None = None,
    marker: str = "a",
) -> OaiPage:
    return OaiPage(
        response_date=datetime(2026, 8, response_day, 2, tzinfo=timezone.utc),
        records=records,
        resumption_token=token,
        raw_sha256=marker * 64,
    )


class ScriptedOai:
    def __init__(self) -> None:
        self.first: dict[str, list[object]] = defaultdict(list)
        self.next: dict[str, list[object]] = defaultdict(list)
        self.backfill: dict[str, list[object]] = defaultdict(list)
        self.first_calls: list[tuple[str, date]] = []
        self.backfill_calls: list[tuple[str, date, date]] = []
        self.records: dict[str, list[object]] = defaultdict(list)
        self.record_calls: list[str] = []
        self.identify_result = OaiIdentify(
            response_date=NOW,
            earliest_datestamp=date(2007, 1, 1),
            granularity="YYYY-MM-DD",
        )

    @staticmethod
    def _take(values: list[object]) -> object:
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def first_page(
        self, set_spec: str, from_date: date, *, cancelled=None
    ) -> OaiPage:
        self.first_calls.append((set_spec, from_date))
        return self._take(self.first[set_spec])  # type: ignore[return-value]

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        return self._take(self.next[token])  # type: ignore[return-value]

    def backfill_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        self.backfill_calls.append((set_spec, from_date, until_date))
        return self._take(self.backfill[set_spec])  # type: ignore[return-value]

    def identify(self, *, cancelled=None) -> OaiIdentify:
        return self.identify_result

    def get_record(self, arxiv_id: str, *, cancelled=None):
        self.record_calls.append(arxiv_id)
        assert callable(cancelled)
        return self._take(self.records[arxiv_id])


class ScriptedAtom:
    def __init__(self) -> None:
        self.values: dict[str, list[object]] = defaultdict(list)
        self.calls: list[str] = []

    def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
        self.calls.append(category)
        return ScriptedOai._take(self.values[category])  # type: ignore[return-value]


class ScriptedCatchup:
    def __init__(self) -> None:
        self.values: dict[tuple[str, date], object] = {}
        self.calls: list[tuple[str, date]] = []

    def fetch_day(
        self, category: str, mailing_date: date, *, cancelled=None
    ) -> CatchupDay:
        self.calls.append((category, mailing_date))
        value = self.values[(category, mailing_date)]
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


def _empty_atom(category: str, day: int) -> AtomBatch:
    return AtomBatch(
        category=category,
        mailing_date=date(2026, 8, day),
        entries=(),
        raw_sha256="e" * 64,
        fetched_at=datetime(2026, 8, day, 3, tzinfo=timezone.utc),
    )


def _service(
    tmp_path: Path,
) -> tuple[
    Store,
    ScriptedOai,
    ScriptedAtom,
    ScriptedCatchup,
    SyncService,
]:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    oai = ScriptedOai()
    atom = ScriptedAtom()
    catchup = ScriptedCatchup()
    service = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
    )
    return store, oai, atom, catchup, service


def test_catchup_is_applied_before_hidden_sources_and_survives_their_failures(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    calls: list[str] = []
    mailing_date = date(2026, 8, 20)
    metadata = _paper("2608.04011")

    class FailingOai:
        def first_page(
            self, set_spec: str, from_date: date, *, cancelled=None
        ) -> OaiPage:
            calls.append("oai")
            raise RuntimeError("synthetic OAI outage")

    class FailingAtom:
        def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
            calls.append("atom")
            raise RuntimeError("synthetic Atom outage")

    class RecordingCatchup:
        def fetch_day(
            self, category: str, requested_date: date, *, cancelled=None
        ) -> CatchupDay:
            calls.append("catchup")
            return CatchupDay(
                category=category,
                mailing_date=requested_date,
                status=EnrichmentStatus.COMPLETE,
                pages=(
                    CatchupPage(
                        category=category,
                        mailing_date=requested_date,
                        page=1,
                        total_pages=1,
                        entries=(
                            CatchupEntry(
                                metadata=metadata,
                                section=AnnounceType.NEW,
                                mailing_date=requested_date,
                                position=0,
                            ),
                        ),
                        raw_sha256="c" * 64,
                    ),
                ),
                error_code=None,
                error_message=None,
            )

    config = CategoryConfig("cs.SE", "cs:SE", mailing_date)
    service = SyncService(
        store,
        FailingOai(),
        FailingAtom(),
        RecordingCatchup(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
    )
    visible_after_attempt: list[int] = []

    service.sync(
        (config,),
        catchup_dates={config.category: (mailing_date,)},
        attempted=lambda _category, day: visible_after_attempt.append(
            len(store.events_for_date(day))
        ),
        daily_list_complete=lambda: calls.append("daily_list_complete"),
    )

    assert calls == ["catchup", "daily_list_complete", "atom", "oai"]
    assert visible_after_attempt == [1]
    assert len(store.events_for_date(mailing_date)) == 1


def test_cached_confirmed_review_survives_when_all_network_phases_fail(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    mailing_date = date(2026, 8, 20)
    config = CategoryConfig("cs.SE", "cs:SE", mailing_date)
    catchup.values[(config.category, mailing_date)] = CatchupDay(
        category=config.category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category=config.category,
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=_paper("2608.04015"),
                        section=AnnounceType.NEW,
                        mailing_date=mailing_date,
                        position=0,
                    ),
                ),
                raw_sha256="4" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))
    first = service.sync(
        (config,),
        catchup_dates={config.category: (mailing_date,)},
    )
    event_id = store.events_for_date(mailing_date)[0].event_id
    assert first.dates_with_papers == 1

    atom.values[config.category].append(RuntimeError("synthetic Atom outage"))
    oai.first[config.oai_set_spec].append(RuntimeError("synthetic OAI outage"))
    offline = service.sync(
        (config,),
        catchup_dates={config.category: (mailing_date,)},
    )

    assert offline.offline is True
    assert offline.dates_with_papers == 1
    assert store.events_for_date(mailing_date)[0].event_id == event_id


def test_cancellation_aborts_all_later_phases_without_advancing_checkpoint(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    cancellation = Event()
    page = _page(20, token="must-not-fetch")

    class CancellingOai:
        def first_page(
            self,
            set_spec: str,
            from_date: date,
            *,
            cancelled=None,
        ) -> OaiPage:
            assert cancelled is not None
            assert cancelled() is False
            cancellation.set()
            return page

        def next_page(self, token: str, *, cancelled=None) -> OaiPage:
            raise AssertionError("cancelled sync requested another OAI page")

    class UnreachableSource:
        def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
            raise AssertionError("cancelled sync continued into Atom")

        def fetch_day(
            self, category: str, mailing_date: date, *, cancelled=None
        ) -> CatchupDay:
            raise AssertionError("cancelled sync continued into catch-up")

    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    service = SyncService(
        store,
        CancellingOai(),
        UnreachableSource(),
        UnreachableSource(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
        cancelled=cancellation.is_set,
    )

    with pytest.raises(SyncCancelled):
        service.sync((config,), catchup_dates={})

    state = store.category_sync_state("cs.SE")
    assert state.completed_through_utc is None
    assert state.last_error_code == "cancelled"
    assert store.enrichment_records("cs.SE") == ()


def test_transport_cancellation_is_persisted_as_cancelled_not_network_failure(
    tmp_path: Path,
) -> None:
    from arxiv_digest.rate_limit import ArxivRequestCancelled

    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)

    class CancelledOai:
        def first_page(
            self, set_spec: str, from_date: date, *, cancelled=None
        ) -> OaiPage:
            assert cancelled is not None
            raise ArxivRequestCancelled("fixture transport cancellation")

    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    service = SyncService(
        store,
        CancelledOai(),
        object(),
        object(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
    )

    with pytest.raises(SyncCancelled):
        service.sync((config,), catchup_dates={})

    state = store.category_sync_state("cs.SE")
    assert state.completed_through_utc is None
    assert state.last_error_code == "cancelled"


def test_cancellation_leaves_all_prepared_daily_list_targets_pending(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    cancellation = Event()
    dates = (date(2026, 8, 20), date(2026, 8, 21))
    config = CategoryConfig("cs.SE", "cs:SE", dates[0])

    class CancellingCatchup:
        def fetch_day(
            self, category: str, mailing_date: date, *, cancelled=None
        ) -> CatchupDay:
            cancellation.set()
            return CatchupDay(
                category=category,
                mailing_date=mailing_date,
                status=EnrichmentStatus.EMPTY,
                pages=(),
                error_code=None,
                error_message=None,
            )

    class UnreachableHiddenSource:
        def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
            raise AssertionError("cancelled sync reached Atom")

        def first_page(
            self, set_spec: str, from_date: date, *, cancelled=None
        ) -> OaiPage:
            raise AssertionError("cancelled sync reached OAI")

    service = SyncService(
        store,
        UnreachableHiddenSource(),
        UnreachableHiddenSource(),
        CancellingCatchup(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
        cancelled=cancellation.is_set,
    )

    with pytest.raises(SyncCancelled):
        service.sync(
            (config,),
            catchup_dates={config.category: dates},
        )

    records = store.catchup_day_records(config.category)
    assert tuple(record.daily_list_date for record in records) == dates
    assert all(record.status.value == "pending" for record in records)
    assert all(record.attempted_at is None for record in records)

    oai = ScriptedOai()
    atom = ScriptedAtom()
    catchup = ScriptedCatchup()
    for mailing_date in dates:
        catchup.values[(config.category, mailing_date)] = CatchupDay(
            category=config.category,
            mailing_date=mailing_date,
            status=EnrichmentStatus.EMPTY,
            pages=(),
            error_code=None,
            error_message=None,
        )
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))
    resumed = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
    ).sync(
        (config,),
        catchup_dates={config.category: dates},
    )

    assert resumed.checked_dates == 2
    assert resumed.empty_dates == 2
    assert resumed.pending_dates == 0


def test_two_page_chain_advances_only_at_end_and_next_run_overlaps_one_day(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    article = _article(versions=(_version(1, 36),))
    oai.first["cs:SE"].extend(
        [_page(20, (article,), "page-two"), _page(22, marker="c")]
    )
    oai.next["page-two"].append(_page(21, marker="b"))
    atom.values["cs.SE"].extend([_empty_atom("cs.SE", 20), _empty_atom("cs.SE", 21)])

    first = service.sync((config,), catchup_dates={})
    second = service.sync((config,), catchup_dates={})

    assert oai.first_calls == [
        ("cs:SE", date(2026, 8, 1)),
        ("cs:SE", date(2026, 8, 20)),
    ]
    assert first.categories[0].metadata_sync.completed_through_utc == date(2026, 8, 21)
    assert second.categories[0].metadata_sync.completed_through_utc == date(2026, 8, 22)
    assert store.latest_sync_run("cs.SE", "incremental").pages_applied == 1
    assert store.events_for_date(date(2026, 8, 5)) == ()
    observations = store.source_observations(article.metadata.arxiv_id)
    assert len(observations) == 1
    assert observations[0].category is None
    assert observations[0].oai_datestamp == date(2026, 8, 20)


def test_interrupted_chain_restarts_from_old_checkpoint_and_replay_is_harmless(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    article = _article(versions=(_version(1, 36),))
    first_page = _page(20, (article,), "continue")
    oai.first["cs:SE"].extend([first_page, first_page])
    oai.next["continue"].extend(
        [RuntimeError("synthetic interruption"), _page(21, marker="b")]
    )
    atom.values["cs.SE"].extend([_empty_atom("cs.SE", 20), _empty_atom("cs.SE", 21)])

    failed = service.sync((config,), catchup_dates={})
    recovered = service.sync((config,), catchup_dates={})

    assert failed.categories[0].metadata_sync.completed_through_utc is None
    assert oai.first_calls == [
        ("cs:SE", date(2026, 8, 1)),
        ("cs:SE", date(2026, 8, 1)),
    ]
    assert recovered.metadata_complete is True
    assert store.events_for_date(date(2026, 8, 5)) == ()
    assert len(store.source_observations(article.metadata.arxiv_id)) == 1


def test_expired_token_gets_one_bounded_restart(tmp_path: Path) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    oai.first["cs:SE"].extend(
        [_page(20, token="expired"), _page(21, marker="b")]
    )
    oai.next["expired"].append(
        OaiProtocolError("badResumptionToken", "synthetic expiry")
    )
    atom.values["cs.SE"].append(_empty_atom("cs.SE", 21))

    report = service.sync((config,), catchup_dates={})

    assert report.metadata_complete is True
    assert len(oai.first_calls) == 2
    assert store.latest_sync_run("cs.SE", "incremental").status == "completed"


def test_one_oai_failure_does_not_block_other_category_or_current_atom(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    configs = (
        CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1)),
        CategoryConfig("math.LO", "math:LO", date(2026, 8, 1)),
    )
    oai.first["cs:SE"].append(RuntimeError("synthetic OAI failure"))
    oai.first["math:LO"].append(_page(21))
    version = PaperVersion(1, datetime(2026, 8, 20, 7, tzinfo=timezone.utc))
    entry = AtomEntry(
        metadata=_paper("2608.04002", "cs.SE"),
        announced_version=version.number,
        published_at=version.submitted_at,
        announce_type=AnnounceType.NEW,
        mailing_date=date(2026, 8, 20),
        position=0,
    )
    atom.values["cs.SE"].append(
        AtomBatch("cs.SE", date(2026, 8, 20), (entry,), "f" * 64, NOW)
    )
    atom.values["math.LO"].append(_empty_atom("math.LO", 20))

    report = service.sync(configs, catchup_dates={})

    assert report.metadata_complete is False
    progress = {item.category: item for item in report.categories}
    assert progress["cs.SE"].metadata_sync.status == "failed"
    assert progress["math.LO"].metadata_sync.status == "idle"
    assert store.article_metadata("2608.04002") == entry.metadata
    with pytest.raises(KeyError):
        store.article_version("2608.04002", 1)
    assert store.events_for_date(date(2026, 8, 20)) == ()
    observation = store.source_observations("2608.04002")[0]
    assert observation.announced_version == 1
    assert observation.daily_list_date == entry.mailing_date


def test_atom_does_not_overwrite_oai_metadata_or_version_timestamp(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    arxiv_id = "2608.04008"
    oai_metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title="Complete OAI metadata",
        authors=("Ada Fixture", "Ben Example"),
        abstract="The complete authoritative abstract.",
        primary_category="cs.SE",
        categories=("cs.SE", "stat.ML"),
        comments="Fourteen pages",
        journal_ref="Fixture Journal 2 (2026)",
        doi="10.0000/fixture.4008",
    )
    precise_version = PaperVersion(
        2, datetime(2026, 8, 19, 9, 17, 23, tzinfo=timezone.utc)
    )
    article = OaiArticle(
        oai_identifier=f"oai:arXiv.org:{arxiv_id}",
        oai_datestamp=date(2026, 8, 20),
        set_specs=("cs:SE",),
        metadata=oai_metadata,
        versions=(precise_version,),
    )
    oai.first["cs:SE"].append(_page(22, (article,)))
    feed_metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title="Incomplete feed metadata",
        authors=("Ada Fixture", "Ben Example"),
        abstract="A shorter feed abstract.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    feed_midnight = datetime(2026, 8, 22, 4, tzinfo=timezone.utc)
    entry = AtomEntry(
        metadata=feed_metadata,
        announced_version=2,
        published_at=feed_midnight,
        announce_type=AnnounceType.REPLACE,
        mailing_date=date(2026, 8, 22),
        position=0,
    )
    atom.values["cs.SE"].append(
        AtomBatch(
            "cs.SE",
            date(2026, 8, 22),
            (entry,),
            "f" * 64,
            NOW,
        )
    )

    service.sync((config,), catchup_dates={})

    assert store.article_metadata(arxiv_id) == oai_metadata
    assert store.article_version(arxiv_id, 2) == precise_version
    assert store.events_for_date(date(2026, 8, 19)) == ()
    assert {item.source.value for item in store.source_observations(arxiv_id)} == {
        "oai",
        "atom",
    }


def test_untrusted_oai_protocol_code_is_redacted_before_durable_state(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    private_code = "Private/" + "x" * 256
    oai.first["cs:SE"].append(
        OaiProtocolError(private_code, "private remote description")
    )
    atom.values["cs.SE"].append(_empty_atom("cs.SE", 20))

    service.sync((config,), catchup_dates={})

    state = store.category_sync_state("cs.SE")
    run = store.latest_sync_run("cs.SE", "incremental")
    assert state.last_error_code == "oai_protocol_error"
    assert run is not None
    assert run.error_code == "oai_protocol_error"
    assert private_code not in state.last_error_code
    assert len(state.last_error_code) <= 64


def test_extending_coverage_runs_bounded_metadata_fetch_without_inference(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    historical = _article(
        versions=(PaperVersion(1, datetime(2026, 7, 15, 9, tzinfo=timezone.utc)),)
    )
    oai.first["cs:SE"].extend(
        [_page(20, (historical,), marker="9"), _page(22, marker="8")]
    )
    atom.values["cs.SE"].extend([_empty_atom("cs.SE", 20), _empty_atom("cs.SE", 21)])
    service.sync((config,), catchup_dates={})
    assert store.events_for_date(date(2026, 7, 15)) == ()

    pending = service.extend_coverage("cs.SE", date(2026, 7, 1))
    assert pending.coverage_start == date(2026, 8, 1)
    assert pending.pending_backfill_until == date(2026, 7, 31)
    oai.backfill["cs:SE"].append(_page(22, marker="7"))

    service.sync((config,), catchup_dates={})

    state = store.category_sync_state("cs.SE")
    assert state.coverage_start == date(2026, 7, 1)
    assert state.completed_through_utc == date(2026, 8, 22)
    assert state.pending_backfill_start is None
    assert store.events_for_date(date(2026, 7, 15)) == ()
    observation = store.source_observations(
        historical.metadata.arxiv_id
    )[0]
    assert observation.response_sha256 == "9" * 64
    assert oai.backfill_calls == [
        ("cs:SE", date(2026, 7, 1), date(2026, 7, 31))
    ]


def test_settings_extension_schedules_old_coverage_interval_before_publication(
    tmp_path: Path,
) -> None:
    from arxiv_digest.application import _DefaultRuntime

    store, oai, atom, _catchup, service = _service(tmp_path)
    old_start = date(2026, 8, 1)
    new_start = date(2026, 7, 1)
    config = CategoryConfig("cs.SE", "cs:SE", old_start)
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    profiles = ProfileRepository(
        tmp_path / "profile.json",
        tmp_path / "profile.lock",
    )
    profiles.save_atomic(
        Profile(
            schema_version=2,
            revision=1,
            category_coverage=(ProfileCategory(config.category, old_start),),
            keywords=(),
            phrases=(),
            authors=(),
            seed_papers=(),
            pdf_destination=PdfDestination("documents", tmp_path),
        ),
        expected_revision=None,
    )
    setup = SetupService(
        store,
        profiles,
        clock=lambda: NOW,
    )
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22, marker="8"))
    oai.backfill[config.oai_set_spec].append(_page(22, marker="7"))

    runtime = object.__new__(_DefaultRuntime)
    runtime.store = store
    runtime.profiles = profiles
    runtime.setup = setup
    runtime.sync = service

    def run_sync(_payload: dict[str, object]) -> dict[str, str]:
        service.sync(runtime._sync_configs(), catchup_dates={})
        return {"job_id": "sync_extension"}

    runtime._start_sync_job = run_sync

    result = runtime._settings_coverage(
        {
            "category": config.category,
            "new_start": new_start.isoformat(),
            "expected_revision": 1,
        }
    )

    assert result["sync_job_id"] == "sync_extension"
    state = store.category_sync_state(config.category)
    assert state.coverage_start == new_start
    assert state.pending_backfill_start is None
    assert state.pending_backfill_until is None
    assert oai.backfill_calls == [
        (config.oai_set_spec, new_start, old_start - timedelta(days=1))
    ]


def test_extend_coverage_rejects_dates_outside_the_catchup_window(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    oai = ScriptedOai()
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 20))
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    service = SyncService(
        store,
        oai,
        ScriptedAtom(),
        ScriptedCatchup(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
        catchup_window_days=3,
    )

    with pytest.raises(ValueError, match="catch-up recovery window"):
        service.extend_coverage(config.category, date(2026, 8, 19))


def test_failed_backfill_stays_pending_while_later_incremental_advances(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    oai.first["cs:SE"].extend([_page(20), _page(21), _page(22)])
    atom.values["cs.SE"].extend(
        [
            _empty_atom("cs.SE", 20),
            _empty_atom("cs.SE", 21),
            _empty_atom("cs.SE", 22),
        ]
    )
    service.sync((config,), catchup_dates={})
    service.extend_coverage("cs.SE", date(2026, 7, 1))
    oai.backfill["cs:SE"].append(RuntimeError("synthetic backfill failure"))

    failed = service.sync((config,), catchup_dates={})

    state_after_failure = store.category_sync_state("cs.SE")
    assert state_after_failure.completed_through_utc == date(2026, 8, 21)
    assert state_after_failure.pending_backfill_start == date(2026, 7, 1)
    assert failed.categories[0].metadata_sync.status == "idle"
    assert failed.categories[0].historical_backfill.status == "failed"

    oai.backfill["cs:SE"].append(RuntimeError("synthetic retry failure"))
    later = service.sync((config,), catchup_dates={})

    assert later.categories[0].metadata_sync.completed_through_utc == date(2026, 8, 22)
    assert later.categories[0].historical_backfill.pending_start == date(2026, 7, 1)


def test_exact_enrichment_retains_an_interior_failed_date(tmp_path: Path) -> None:
    _store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    oai.first["cs:SE"].append(_page(22))
    atom.values["cs.SE"].append(RuntimeError("synthetic feed outage"))
    for day, status in (
        (18, EnrichmentStatus.EMPTY),
        (19, EnrichmentStatus.FAILED),
        (20, EnrichmentStatus.EMPTY),
    ):
        catchup.values[("cs.SE", date(2026, 8, day))] = CatchupDay(
            category="cs.SE",
            mailing_date=date(2026, 8, day),
            status=status,
            pages=(),
            error_code=(
                "catchup_fixture_failure"
                if status is EnrichmentStatus.FAILED
                else None
            ),
            error_message=(
                "Synthetic catch-up failure."
                if status is EnrichmentStatus.FAILED
                else None
            ),
        )

    report = service.sync(
        (config,),
        catchup_dates={
            "cs.SE": (
                date(2026, 8, 18),
                date(2026, 8, 19),
                date(2026, 8, 20),
            )
        },
    )

    progress = report.categories[0]
    assert progress.exact_start == date(2026, 8, 18)
    assert progress.exact_end == date(2026, 8, 20)
    assert progress.missing_exact_dates == (date(2026, 8, 19),)


def test_exact_enrichment_retains_failed_dates_outside_the_success_range(
    tmp_path: Path,
) -> None:
    _store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    oai.first["cs:SE"].append(_page(22))
    atom.values["cs.SE"].append(RuntimeError("synthetic feed outage"))
    for day, status in (
        (17, EnrichmentStatus.FAILED),
        (18, EnrichmentStatus.EMPTY),
        (19, EnrichmentStatus.FAILED),
    ):
        catchup.values[("cs.SE", date(2026, 8, day))] = CatchupDay(
            category="cs.SE",
            mailing_date=date(2026, 8, day),
            status=status,
            pages=(),
            error_code=(
                "catchup_fixture_failure"
                if status is EnrichmentStatus.FAILED
                else None
            ),
            error_message=(
                "Synthetic catch-up failure."
                if status is EnrichmentStatus.FAILED
                else None
            ),
        )

    progress = service.sync(
        (config,),
        catchup_dates={
            "cs.SE": (
                date(2026, 8, 17),
                date(2026, 8, 18),
                date(2026, 8, 19),
            )
        },
    ).categories[0]

    assert progress.exact_start == date(2026, 8, 18)
    assert progress.exact_end == date(2026, 8, 18)
    assert progress.missing_exact_dates == (
        date(2026, 8, 17),
        date(2026, 8, 19),
    )


def test_daily_list_progress_has_disjoint_category_lists_and_global_counts(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    with_papers = date(2026, 8, 18)
    empty = date(2026, 8, 19)
    failed = date(2026, 8, 20)
    pending = date(2026, 8, 21)
    catchup.values[(config.category, with_papers)] = CatchupDay(
        category=config.category,
        mailing_date=with_papers,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category=config.category,
                mailing_date=with_papers,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=_paper("2608.04012"),
                        section=AnnounceType.NEW,
                        mailing_date=with_papers,
                        position=0,
                    ),
                ),
                raw_sha256="1" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    catchup.values[(config.category, empty)] = CatchupDay(
        category=config.category,
        mailing_date=empty,
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )
    catchup.values[(config.category, failed)] = CatchupDay(
        category=config.category,
        mailing_date=failed,
        status=EnrichmentStatus.FAILED,
        pages=(),
        error_code="catchup_layout_changed",
        error_message="The catch-up layout was not recognized.",
    )
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))

    service.sync(
        (config,),
        catchup_dates={
            config.category: (with_papers, empty, failed),
        },
    )
    store.ensure_catchup_targets(config.category, (pending,))

    report = service.progress((config,))
    category = report.categories[0]

    assert category.target_dates == (with_papers, empty, failed, pending)
    assert category.checked_dates == (with_papers, empty, failed)
    assert category.dates_with_papers == (with_papers,)
    assert category.empty_dates == (empty,)
    assert category.failed_dates == (failed,)
    assert category.pending_dates == (pending,)
    assert category.unavailable_dates == ()
    assert (
        report.target_dates,
        report.checked_dates,
        report.dates_with_papers,
        report.empty_dates,
        report.failed_dates,
        report.pending_dates,
        report.unavailable_dates,
    ) == (4, 3, 1, 1, 1, 1, 0)


def test_global_date_is_failed_when_one_applicable_category_fails(
    tmp_path: Path,
) -> None:
    _store, oai, atom, catchup, service = _service(tmp_path)
    mailing_date = date(2026, 8, 20)
    configs = (
        CategoryConfig("cs.SE", "cs:SE", mailing_date),
        CategoryConfig("math.LO", "math:LO", mailing_date),
    )
    catchup.values[("cs.SE", mailing_date)] = CatchupDay(
        category="cs.SE",
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=_paper("2608.04013"),
                        section=AnnounceType.NEW,
                        mailing_date=mailing_date,
                        position=0,
                    ),
                ),
                raw_sha256="2" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    catchup.values[("math.LO", mailing_date)] = CatchupDay(
        category="math.LO",
        mailing_date=mailing_date,
        status=EnrichmentStatus.FAILED,
        pages=(),
        error_code="catchup_fetch_failed",
        error_message="The catch-up page could not be retrieved.",
    )
    for config in configs:
        atom.values[config.category].append(
            _empty_atom(config.category, 22)
        )
        oai.first[config.oai_set_spec].append(_page(22))

    attempts: list[tuple[int, int, int]] = []
    report = service.sync(
        configs,
        catchup_dates={
            config.category: (mailing_date,) for config in configs
        },
        attempted=lambda _category, _day: attempts.append(
            (
                service.progress(configs).checked_dates,
                service.progress(configs).failed_dates,
                service.progress(configs).pending_dates,
            )
        ),
    )

    by_category = {item.category: item for item in report.categories}
    assert by_category["cs.SE"].dates_with_papers == (mailing_date,)
    assert by_category["math.LO"].failed_dates == (mailing_date,)
    assert by_category["math.LO"].daily_list_errors == (
        (mailing_date, "catchup_fetch_failed"),
    )
    assert report.target_dates == 1
    assert report.checked_dates == 1
    assert report.failed_dates == 1
    assert report.dates_with_papers == 0
    assert report.empty_dates == 0
    assert report.pending_dates == 0
    assert attempts == [(0, 0, 1), (1, 1, 0)]


def test_aged_pending_gap_is_reported_unavailable_without_inference(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    unavailable = date(2026, 8, 19)
    config = CategoryConfig("cs.SE", "cs:SE", unavailable)
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    store.ensure_catchup_targets(config.category, (unavailable,))
    service = SyncService(
        store,
        ScriptedOai(),
        ScriptedAtom(),
        ScriptedCatchup(),
        clock=lambda: NOW,
        today=lambda: date(2026, 8, 22),
        catchup_window_days=3,
    )

    report = service.progress((config,))

    category = report.categories[0]
    assert category.pending_dates == (unavailable,)
    assert category.unavailable_dates == (unavailable,)
    assert report.target_dates == 1
    assert report.checked_dates == 0
    assert report.pending_dates == 1
    assert report.unavailable_dates == 1
    assert store.events_for_date(unavailable) == ()


def test_default_catchup_ends_on_new_york_mailing_date_before_utc_midnight(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    oai = ScriptedOai()
    atom = ScriptedAtom()
    catchup = ScriptedCatchup()
    service = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: datetime(2026, 8, 22, 2, tzinfo=timezone.utc),
    )
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 21))
    oai.first["cs:SE"].append(_page(22))
    atom.values["cs.SE"].append(_empty_atom("cs.SE", 21))
    catchup.values[("cs.SE", date(2026, 8, 21))] = CatchupDay(
        category="cs.SE",
        mailing_date=date(2026, 8, 21),
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )

    service.sync((config,))

    assert catchup.calls == [("cs.SE", date(2026, 8, 21))]


def test_supported_coverage_bounds_follow_eastern_finalization_across_utc_midnight(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    observed_at = {
        "value": datetime(2026, 8, 22, 23, 59, tzinfo=timezone.utc)
    }
    service = SyncService(
        Store(database_path),
        ScriptedOai(),
        ScriptedAtom(),
        ScriptedCatchup(),
        clock=lambda: observed_at["value"],
        catchup_finalization_hour=20,
    )

    assert service.coverage_bounds() == (
        date(2026, 5, 25),
        date(2026, 8, 21),
    )

    observed_at["value"] = datetime(
        2026, 8, 23, 0, 0, tzinfo=timezone.utc
    )
    assert service.coverage_bounds() == (
        date(2026, 5, 25),
        date(2026, 8, 22),
    )


def test_supported_coverage_bounds_use_one_clock_read_at_eastern_midnight(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    observations = iter(
        (
            datetime(2026, 8, 23, 3, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 8, 23, 4, 0, 0, tzinfo=timezone.utc),
        )
    )
    service = SyncService(
        Store(database_path),
        ScriptedOai(),
        ScriptedAtom(),
        ScriptedCatchup(),
        clock=lambda: next(observations),
        catchup_finalization_hour=20,
    )

    assert service.coverage_bounds() == (
        date(2026, 5, 25),
        date(2026, 8, 22),
    )


@pytest.mark.parametrize("explicit", (False, True))
def test_unfinalized_daily_list_is_not_created_or_fetched(
    tmp_path: Path, explicit: bool,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    current_day = date(2026, 8, 22)
    config = CategoryConfig("cs.SE", "cs:SE", current_day)
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))

    report = service.sync(
        (config,),
        catchup_dates={config.category: (current_day,)} if explicit else None,
    )

    assert catchup.calls == []
    assert store.catchup_day_records(config.category) == ()
    assert service.has_pending_daily_list_work((config,)) is False
    assert report.target_dates == report.checked_dates == report.failed_dates == 0
    assert report.daily_list_status == "idle"


@pytest.mark.parametrize(
    "mailing_date,cutoff",
    (
        (date(2026, 9, 20), datetime(2026, 9, 21, 0, tzinfo=timezone.utc)),
        (date(2026, 11, 20), datetime(2026, 11, 21, 1, tzinfo=timezone.utc)),
    ),
)
def test_saved_unfinalized_failure_is_hidden_and_retry_waits_for_cutoff(
    tmp_path: Path, mailing_date: date, cutoff: datetime,
) -> None:
    store, oai, atom, catchup, _ = _service(tmp_path)
    observed_at = {"value": cutoff - timedelta(seconds=1)}
    service = SyncService(
        store, oai, atom, catchup, clock=lambda: observed_at["value"],
    )
    config = CategoryConfig("cs.SE", "cs:SE", mailing_date)
    store.ensure_category_state(config.category, config.oai_set_spec, mailing_date)
    store.apply_catchup_day(
        CatchupDay(
            category=config.category,
            mailing_date=mailing_date,
            status=EnrichmentStatus.FAILED,
            pages=(),
            error_code="catchup_http_406",
            error_message="Synthetic HTTP failure before finalization.",
        ),
        (),
        observed_at["value"],
    )
    records_before = store.catchup_day_records(config.category)
    attempts: list[tuple[str, date]] = []

    report = service.retry_failed_dates(
        (config,), {config.category: (mailing_date,)},
        attempted=lambda category, day: attempts.append((category, day)),
    )

    assert catchup.calls == attempts == []
    assert store.catchup_day_records(config.category) == records_before
    assert report.target_dates == report.checked_dates == report.failed_dates == 0
    assert report.missing_exact_dates == ()
    assert report.categories[0].retryable_failed_exact_dates == ()
    assert report.categories[0].daily_list_errors == ()
    assert report.daily_list_status == "idle"
    assert service.has_pending_daily_list_work((config,)) is False

    observed_at["value"] = cutoff
    report = service.progress((config,))
    assert report.failed_dates == 1
    assert report.categories[0].retryable_failed_exact_dates == (mailing_date,)
    assert service.has_pending_daily_list_work((config,)) is True
    catchup.values[(config.category, mailing_date)] = CatchupDay(
        category=config.category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )

    report = service.retry_failed_dates(
        (config,), {config.category: (mailing_date,)},
        attempted=lambda category, day: attempts.append((category, day)),
    )

    assert catchup.calls == attempts == [(config.category, mailing_date)]
    assert report.empty_dates == 1
    assert report.failed_dates == 0
    assert service.has_pending_daily_list_work((config,)) is False


def test_saved_unfinalized_empty_day_is_hidden_without_changing_its_record(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    current_day = date(2026, 8, 22)
    config = CategoryConfig("cs.SE", "cs:SE", current_day)
    result = CatchupDay(
        category=config.category,
        mailing_date=current_day,
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )
    store.ensure_category_state(config.category, config.oai_set_spec, current_day)
    store.apply_catchup_day(result, (), NOW)
    records_before = store.catchup_day_records(config.category)
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))

    report = service.sync(
        (config,),
        catchup_dates={config.category: (current_day,)},
    )

    assert catchup.calls == []
    assert store.catchup_day_records(config.category) == records_before
    assert report.target_dates == 0
    assert report.checked_dates == 0
    assert report.pending_dates == 0
    assert report.empty_dates == 0


def test_saved_unfinalized_complete_day_is_hidden_until_cutoff(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    current_day = date(2026, 8, 22)
    config = CategoryConfig("cs.SE", "cs:SE", current_day)
    result = CatchupDay(
        category=config.category,
        mailing_date=current_day,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category=config.category,
                mailing_date=current_day,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=_paper("2608.04014"),
                        section=AnnounceType.NEW,
                        mailing_date=current_day,
                        position=0,
                    ),
                ),
                raw_sha256="3" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    store.ensure_category_state(config.category, config.oai_set_spec, current_day)
    store.apply_catchup_day(result, catchup_observations(result, NOW), NOW)
    records_before = store.catchup_day_records(config.category)
    events_before = store.events_for_date(current_day)
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))

    report = service.sync(
        (config,),
        catchup_dates={config.category: (current_day,)},
    )

    assert catchup.calls == []
    assert store.catchup_day_records(config.category) == records_before
    assert store.events_for_date(current_day) == events_before
    assert report.target_dates == 0
    assert report.checked_dates == 0
    assert report.dates_with_papers == 0
    assert report.pending_dates == 0

    finalized_service = SyncService(
        store, oai, atom, catchup,
        clock=lambda: datetime(2026, 8, 23, 0, tzinfo=timezone.utc),
    )
    report = finalized_service.progress((config,))

    assert report.target_dates == report.checked_dates == report.dates_with_papers == 1
    assert store.catchup_day_records(config.category) == records_before
    assert store.events_for_date(current_day) == events_before


def test_finalized_current_day_empty_is_terminal_without_next_day_refetch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    oai = ScriptedOai()
    atom = ScriptedAtom()
    catchup = ScriptedCatchup()
    current_day = date(2026, 8, 22)
    service = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: datetime(2026, 8, 23, 1, tzinfo=timezone.utc),
        today=lambda: current_day,
    )
    config = CategoryConfig("cs.SE", "cs:SE", current_day)
    catchup.values[(config.category, current_day)] = CatchupDay(
        category=config.category,
        mailing_date=current_day,
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )
    atom.values[config.category].append(_empty_atom(config.category, 22))
    oai.first[config.oai_set_spec].append(_page(22))

    service.sync(
        (config,),
        catchup_dates={config.category: (current_day,)},
    )

    assert store.catchup_day_records(config.category)[0].status.value == "empty"
    assert service._eligible_catchup_dates(
        config,
        {config.category: (current_day,)},
    ) == ()


def test_pending_current_day_is_resumed_and_finalized_after_the_day_closes(
    tmp_path: Path,
) -> None:
    store, _oai, _atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 21))
    pending = date(2026, 8, 21)
    store.ensure_catchup_targets(config.category, (pending,))

    assert service._eligible_catchup_dates(config, None) == (pending,)

    catchup.values[(config.category, pending)] = CatchupDay(
        category=config.category,
        mailing_date=pending,
        status=EnrichmentStatus.EMPTY,
        pages=(),
        error_code=None,
        error_message=None,
    )
    assert service._sync_catchup_day(config, pending) is True

    assert service._eligible_catchup_dates(config, None) == ()


def test_atom_enrichment_alone_does_not_claim_exact_mailing_coverage(
    tmp_path: Path,
) -> None:
    _store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    oai.first["cs:SE"].append(_page(22))
    atom.values["cs.SE"].append(_empty_atom("cs.SE", 22))

    report = service.sync((config,), catchup_dates={})

    assert catchup.calls == []
    progress = report.categories[0]
    assert progress.exact_start is None
    assert progress.exact_end is None
    assert progress.missing_exact_dates == ()


def test_daily_list_preflight_detects_a_missing_required_date(
    tmp_path: Path,
) -> None:
    _store, _oai, _atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 21))

    assert service.has_pending_daily_list_work((config,)) is True


def test_daily_list_preflight_accepts_complete_finalized_coverage(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, _service_before_finalization = _service(tmp_path)
    service = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: datetime(2026, 8, 23, 1, tzinfo=timezone.utc),
        today=lambda: date(2026, 8, 22),
    )
    mailing_date = date(2026, 8, 22)
    config = CategoryConfig("cs.SE", "cs:SE", mailing_date)
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    store.ensure_catchup_targets(config.category, (mailing_date,))
    result = CatchupDay(
        category=config.category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category=config.category,
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=_paper("2608.04221"),
                        section=AnnounceType.NEW,
                        mailing_date=mailing_date,
                        position=0,
                    ),
                ),
                raw_sha256="2" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    store.apply_catchup_day(
        result,
        catchup_observations(result, NOW),
        NOW,
    )

    assert service.has_pending_daily_list_work((config,)) is False


def test_daily_list_preflight_detects_a_failed_retryable_date(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, _service_before_finalization = _service(tmp_path)
    service = SyncService(
        store,
        oai,
        atom,
        catchup,
        clock=lambda: datetime(2026, 8, 23, 1, tzinfo=timezone.utc),
        today=lambda: date(2026, 8, 22),
    )
    mailing_date = date(2026, 8, 22)
    config = CategoryConfig("cs.SE", "cs:SE", mailing_date)
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    store.ensure_catchup_targets(config.category, (mailing_date,))
    store.apply_catchup_day(
        CatchupDay(
            category=config.category,
            mailing_date=mailing_date,
            status=EnrichmentStatus.FAILED,
            pages=(),
            error_code="catchup_fixture_failed",
            error_message="The catch-up fixture did not complete.",
        ),
        (),
        NOW,
    )

    assert service.has_pending_daily_list_work((config,)) is True


def test_catchup_evidence_does_not_erase_authoritative_oai_metadata(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    arxiv_id = "2608.04009"
    oai_metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title="Authoritative OAI title",
        authors=("Ada Fixture", "Ben Example"),
        abstract="The complete OAI abstract.",
        primary_category="cs.SE",
        categories=("cs.SE", "stat.ML"),
        comments="Twelve pages",
        journal_ref="Fixture Journal 1 (2026)",
        doi="10.0000/fixture.4009",
    )
    version = PaperVersion(
        1, datetime(2026, 8, 19, 9, 17, tzinfo=timezone.utc)
    )
    article = OaiArticle(
        oai_identifier=f"oai:arXiv.org:{arxiv_id}",
        oai_datestamp=date(2026, 8, 20),
        set_specs=("cs:SE",),
        metadata=oai_metadata,
        versions=(version,),
    )
    oai.first["cs:SE"].append(_page(22, (article,)))
    atom.values["cs.SE"].append(_empty_atom("cs.SE", 22))
    mailing_date = date(2026, 8, 20)
    catchup_metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title="Catch-up fallback title",
        authors=("Ada Fixture", "Ben Example"),
        abstract="",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    catchup.values[("cs.SE", mailing_date)] = CatchupDay(
        category="cs.SE",
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=catchup_metadata,
                        section=AnnounceType.NEW,
                        mailing_date=mailing_date,
                        position=0,
                    ),
                ),
                raw_sha256="c" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )

    service.sync(
        (config,), catchup_dates={"cs.SE": (mailing_date,)}
    )

    assert store.article_metadata(arxiv_id) == oai_metadata
    assert store.events_for_date(date(2026, 8, 19)) == ()
    event = store.events_for_date(mailing_date)[0]
    assert event.daily_list_date == mailing_date
    assert event.announced_version == 1
    assert event.version_resolution is VersionResolution.CHRONOLOGY_MATCHED
    assert {item.source.value for item in event.observations} == {
        "oai",
        "catchup",
    }


def test_failed_catchup_day_creates_no_event_and_retry_confirms_v1(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    arxiv_id = "2608.04010"
    submission_date = date(2026, 8, 19)
    mailing_date = date(2026, 8, 20)
    metadata = _paper(arxiv_id)
    version = PaperVersion(
        1, datetime(2026, 8, 19, 9, 17, tzinfo=timezone.utc)
    )
    article = _article(arxiv_id=arxiv_id, versions=(version,))
    oai.first["cs:SE"].extend(
        (
            _page(22, (article,), marker="a"),
            _page(22, marker="b"),
        )
    )
    atom.values["cs.SE"].extend(
        (_empty_atom("cs.SE", 22), _empty_atom("cs.SE", 22))
    )
    catchup.values[("cs.SE", mailing_date)] = CatchupDay(
        category="cs.SE",
        mailing_date=mailing_date,
        status=EnrichmentStatus.FAILED,
        pages=(),
        error_code="catchup_layout_changed",
        error_message="The arXiv catch-up page layout was not recognized.",
    )

    service.sync(
        (config,), catchup_dates={"cs.SE": (mailing_date,)}
    )

    failed_records = store.catchup_day_records("cs.SE")
    assert len(failed_records) == 1
    failed_record = failed_records[0]
    assert failed_record.status.value == "failed"
    assert store.events_for_date(submission_date) == ()
    assert store.events_for_date(mailing_date) == ()

    catchup.values[("cs.SE", mailing_date)] = CatchupDay(
        category="cs.SE",
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category="cs.SE",
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=(
                    CatchupEntry(
                        metadata=metadata,
                        section=AnnounceType.NEW,
                        mailing_date=mailing_date,
                        position=0,
                    ),
                ),
                raw_sha256="c" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )

    service.sync(
        (config,), catchup_dates={"cs.SE": (mailing_date,)}
    )

    records = store.catchup_day_records("cs.SE")
    assert len(records) == 1
    assert records[0].status.value == "complete"
    assert store.events_for_date(submission_date) == ()
    recovered = store.events_for_date(mailing_date)[0]
    assert recovered.announced_version == 1
    assert recovered.daily_list_date == mailing_date
    assert recovered.version_resolution is VersionResolution.CHRONOLOGY_MATCHED
    assert {item.source.value for item in recovered.observations} == {
        "oai",
        "catchup",
    }


def test_failed_date_retry_reports_each_attempt_without_running_other_sync_phases(
    tmp_path: Path,
) -> None:
    store, oai, _atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    store.ensure_category_state(
        config.category,
        config.oai_set_spec,
        config.coverage_start,
    )
    retry_dates = (date(2026, 8, 20), date(2026, 8, 21))
    store.ensure_catchup_targets(config.category, retry_dates)
    for mailing_date in retry_dates:
        store.apply_catchup_day(
            CatchupDay(
                category=config.category,
                mailing_date=mailing_date,
                status=EnrichmentStatus.FAILED,
                error_code="catchup_fixture_failed",
                error_message="The catch-up fixture did not complete.",
                pages=(),
            ),
            (),
            NOW,
        )
        catchup.values[(config.category, mailing_date)] = CatchupDay(
            category=config.category,
            mailing_date=mailing_date,
            status=EnrichmentStatus.EMPTY,
            pages=(),
            error_code=None,
            error_message=None,
        )
    attempts = []

    report = service.retry_failed_dates(
        (config,),
        {config.category: retry_dates},
        attempted=lambda category, mailing_date: attempts.append(
            (category, mailing_date)
        ),
    )

    assert attempts == [(config.category, day) for day in retry_dates]
    assert catchup.calls == [(config.category, day) for day in retry_dates]
    assert oai.first_calls == []
    assert report.checked_dates == 2
    assert report.empty_dates == 2
    assert report.failed_dates == 0


def _recover_retry_papers(
    store: Store,
    config: CategoryConfig,
    mailing_date: date,
    *papers: PaperMetadata,
) -> None:
    store.ensure_category_state(
        config.category, config.oai_set_spec, config.coverage_start
    )
    day = CatchupDay(
        category=config.category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.COMPLETE,
        pages=(
            CatchupPage(
                category=config.category,
                mailing_date=mailing_date,
                page=1,
                total_pages=1,
                entries=tuple(
                    CatchupEntry(paper, AnnounceType.NEW, mailing_date, index)
                    for index, paper in enumerate(papers)
                ),
                raw_sha256="c" * 64,
            ),
        ),
        error_code=None,
        error_message=None,
    )
    store.apply_catchup_day(day, catchup_observations(day, NOW), NOW)


@pytest.mark.parametrize("retry_only", (False, True))
def test_throttled_daily_list_stops_remaining_dates_categories_and_metadata(
    tmp_path: Path, retry_only: bool,
) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    other = CategoryConfig("cs.AI", "cs:AI", date(2026, 8, 18))
    first, blocked, later = (date(2026, 8, day) for day in (18, 19, 20))
    catchup.values[(config.category, first)] = CatchupDay(
        config.category, first, EnrichmentStatus.COMPLETE,
        (CatchupPage(
            category=config.category, mailing_date=first, page=1, total_pages=1,
            entries=(CatchupEntry(_paper(), AnnounceType.NEW, first, 0),),
            raw_sha256="c" * 64,
        ),),
        None, None,
    )
    catchup.values[(config.category, blocked)] = ArxivRateLimited(
        retry_at=NOW + timedelta(hours=1), http_status=429,
    )
    requested = {config.category: (first, blocked, later), other.category: (later,)}
    attempts: list[tuple[str, date]] = []
    daily_list_completed: list[bool] = []
    callback = lambda category, day: attempts.append((category, day))

    if retry_only:
        report = service.retry_failed_dates((config, other), requested, attempted=callback)
    else:
        report = service.sync(
            (config, other), catchup_dates=requested, attempted=callback,
            daily_list_complete=lambda: daily_list_completed.append(True),
        )

    assert catchup.calls == attempts == [(config.category, first), (config.category, blocked)]
    assert atom.calls == oai.first_calls == oai.backfill_calls == []
    assert daily_list_completed == []
    assert report.offline is False
    assert report.categories[0].dates_with_papers == (first,)
    assert report.categories[0].failed_dates == (blocked,)
    records = {item.daily_list_date: item for item in store.catchup_day_records(config.category)}
    assert records[first].status.value == "complete"
    assert store.events_for_date(first)[0].arxiv_id == _paper().arxiv_id
    assert records[blocked].error_code == "arxiv_rate_limited"
    assert records[later].status.value == "pending"
    assert records[later].attempted_at is None
    assert store.category_sync_state(other.category).completed_through_utc is None


@pytest.mark.parametrize("retry_only", (False, True))
@pytest.mark.parametrize("blocked_kind", ("cooldown", "unavailable"))
def test_blocked_access_preserves_existing_daily_list_errors_without_attempt_callback(
    tmp_path: Path, retry_only: bool, blocked_kind: str,
) -> None:
    from arxiv_digest.arxiv_access import ArxivCooldownUnavailable, ArxivRateLimited

    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    requested = (date(2026, 8, 19), date(2026, 8, 20))
    store.ensure_category_state(config.category, config.oai_set_spec, config.coverage_start)
    for day in requested:
        store.apply_catchup_day(
            CatchupDay(config.category, day, EnrichmentStatus.FAILED, (),
                       "catchup_http_406", "The arXiv catch-up request returned HTTP 406."),
            (), NOW - timedelta(days=1),
        )
    before = store.catchup_day_records(config.category)
    error = (
        ArxivRateLimited(retry_at=NOW + timedelta(hours=1), http_status=429, attempted=False)
        if blocked_kind == "cooldown" else ArxivCooldownUnavailable()
    )
    catchup.values[(config.category, requested[0])] = error
    attempts: list[tuple[str, date]] = []
    callback = lambda category, day: attempts.append((category, day))

    if retry_only:
        service.retry_failed_dates((config,), {config.category: requested}, attempted=callback)
    else:
        service.sync((config,), catchup_dates={config.category: requested}, attempted=callback)

    assert attempts == []
    assert len(catchup.calls) == 1
    assert atom.calls == oai.first_calls == oai.backfill_calls == []
    assert store.catchup_day_records(config.category) == before


@pytest.mark.parametrize("phase", ("atom", "incremental", "backfill", "next_page"))
def test_throttled_metadata_stops_later_source_requests_and_keeps_checkpoint(
    tmp_path: Path, phase: str,
) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    store, oai, atom, _catchup, service = _service(tmp_path)
    configs = (
        CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18)),
        CategoryConfig("cs.AI", "cs:AI", date(2026, 8, 18)),
    )
    error = ArxivRateLimited(retry_at=NOW + timedelta(hours=1), http_status=429)
    for config in configs:
        atom.values[config.category] = [_empty_atom(config.category, 21)]
        oai.first[config.oai_set_spec] = [_page(21)]
        store.ensure_category_state(config.category, config.oai_set_spec, config.coverage_start)
        store.set_pending_backfill(config.category, date(2026, 8, 17), config.coverage_start)
        oai.backfill[config.oai_set_spec] = [_page(21)]
    if phase == "atom":
        atom.values[configs[0].category] = [error]
    elif phase == "incremental":
        oai.first[configs[0].oai_set_spec] = [error]
    elif phase == "backfill":
        oai.backfill[configs[0].oai_set_spec] = [error]
    else:
        oai.first[configs[0].oai_set_spec] = [_page(21, token="fixture-next")]
        oai.next["fixture-next"] = [error]

    report = service.sync(configs, catchup_dates={})

    if phase == "atom":
        assert atom.calls == [configs[0].category]
        assert oai.first_calls == oai.backfill_calls == []
    elif phase == "backfill":
        assert len(oai.first_calls) == 2
        assert len(oai.backfill_calls) == 1
        assert report.categories[0].historical_backfill.last_error_code == "arxiv_rate_limited"
        assert store.category_sync_state(configs[1].category).pending_backfill_start is not None
    else:
        assert len(oai.first_calls) == 1
        assert oai.backfill_calls == []
        assert report.categories[0].metadata_sync.last_error_code == "arxiv_rate_limited"
        assert store.category_sync_state(configs[0].category).completed_through_utc is None
        assert store.category_sync_state(configs[1].category).completed_through_utc is None


@pytest.mark.parametrize("already_blocked", (False, True))
def test_missing_abstract_retry_stops_on_throttling_and_preserves_prior_success(
    tmp_path: Path, already_blocked: bool,
) -> None:
    from arxiv_digest.arxiv_access import ArxivRateLimited

    store, oai, _atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    papers = [replace(_paper(f"2608.0400{number}"), abstract="") for number in (1, 2, 3)]
    _recover_retry_papers(store, config, date(2026, 8, 20), *papers)
    oai.records[papers[0].arxiv_id] = [_article(arxiv_id=papers[0].arxiv_id)]
    oai.records[papers[1].arxiv_id] = [ArxivRateLimited(
        retry_at=NOW + timedelta(hours=1), http_status=429, attempted=not already_blocked,
    )]
    attempts: list[str] = []

    report = service.retry_missing_abstracts((config,), attempted=attempts.append)

    assert report.offline is False
    assert oai.record_calls == [paper.arxiv_id for paper in papers[:2]]
    assert attempts == [paper.arxiv_id for paper in papers[:1 if already_blocked else 2]]
    assert store.article_metadata(papers[0].arxiv_id).abstract == _paper().abstract
    assert store.article_metadata(papers[1].arxiv_id).abstract == ""
    assert store.article_metadata(papers[2].arxiv_id).abstract == ""
    assert report.abstract_retry is not None
    assert report.abstract_retry.attempted == (1 if already_blocked else 2)
    assert report.abstract_retry.total == 3
    assert report.abstract_retry.recovered == 1
    assert report.abstract_retry.remaining == 2
    assert report.abstract_retry.error_codes == ("arxiv_rate_limited",)


def test_date_abstract_retry_reports_partial_recovery_and_retries_only_missing_ids(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    inactive = CategoryConfig("cs.AI", "cs:AI", config.coverage_start)
    day = date(2026, 8, 20)
    first, second, other_day, other_category = (
        replace(_paper(f"2608.0400{number}"), abstract="") for number in (1, 2, 3, 4)
    )
    _recover_retry_papers(store, config, day, first, second, _paper("2608.04005"))
    _recover_retry_papers(store, config, date(2026, 8, 21), other_day)
    _recover_retry_papers(store, inactive, day, other_category)
    oai.records[first.arxiv_id] = [_article(arxiv_id=first.arxiv_id)]
    oai.records[second.arxiv_id] = [
        HTTPError("https://example.invalid/private", 406, "private transport detail", {}, None),
        _article(arxiv_id=second.arxiv_id),
    ]
    committed: list[tuple[str, bool]] = []

    def observe_attempt(arxiv_id: str) -> None:
        committed.append((arxiv_id, bool(store.article_metadata(arxiv_id).abstract)))

    report = service.retry_missing_abstracts((config,), day=day, attempted=observe_attempt)

    assert committed == [(first.arxiv_id, True), (second.arxiv_id, False)]
    assert oai.record_calls == [first.arxiv_id, second.arxiv_id]
    outcome = report.abstract_retry
    assert outcome is not None
    assert (outcome.attempted, outcome.total, outcome.recovered, outcome.remaining) == (2, 2, 1, 1)
    assert outcome.error_codes == ("arxiv_http_406",)
    assert "private" not in repr(report)
    assert not report.offline
    assert atom.calls == catchup.calls == oai.first_calls == oai.backfill_calls == []

    retried = service.retry_missing_abstracts((config,), day=day)

    assert oai.record_calls == [first.arxiv_id, second.arxiv_id, second.arxiv_id]
    assert retried.abstract_retry is not None
    assert (retried.abstract_retry.total, retried.abstract_retry.recovered, retried.abstract_retry.remaining) == (1, 1, 0)
    assert retried.abstract_retry.error_codes == ()
    assert store.article_metadata(other_day.arxiv_id).abstract == ""
    assert store.article_metadata(other_category.arxiv_id).abstract == ""
    assert service.retry_missing_abstracts((config,), day=day).abstract_retry.total == 0


def test_abstract_retry_with_no_targets_reports_zero_requests(tmp_path: Path) -> None:
    _store, oai, _atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))

    report = service.retry_missing_abstracts((config,), day=date(2026, 8, 20))

    assert report.abstract_retry is not None
    outcome = report.abstract_retry
    assert (outcome.attempted, outcome.total, outcome.recovered, outcome.remaining) == (0, 0, 0, 0)
    assert outcome.error_codes == ()
    assert oai.record_calls == []


def test_metadata_http_failure_preserves_only_the_status_code(tmp_path: Path) -> None:
    store, oai, atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    atom.values[config.category] = [_empty_atom(config.category, 21)]
    oai.first[config.oai_set_spec] = [
        HTTPError("https://example.invalid/private", 406, "private transport detail", {}, None),
    ]

    report = service.sync((config,), catchup_dates={})

    progress = report.categories[0].metadata_sync
    assert progress.last_error_code == "arxiv_http_406"
    assert progress.last_error_message == "The arXiv request returned HTTP 406."
    assert "private" not in repr(store.category_sync_state(config.category))


def _seed_historical_finished_date(tmp_path: Path, store: Store, day: date) -> None:
    """Represent review progress saved before abstract readiness was required."""
    reviewed_at = NOW.isoformat().replace("+00:00", "Z")
    with open_database(tmp_path / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE canonical_events SET reviewed_at = ? WHERE daily_list_date = ?",
            (reviewed_at, day.isoformat()),
        )
        connection.execute(
            """INSERT INTO review_date_state(
                   daily_list_date, last_finished_at, last_finished_revision
               ) VALUES (?, ?, ?)""",
            (day.isoformat(), reviewed_at, store.review_queue_revision()),
        )


def test_missing_abstract_selection_includes_unreviewed_and_reviewed_active_daily_lists(
    tmp_path: Path,
) -> None:
    store, _oai, _atom, _catchup, _service_instance = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 20))
    inactive = CategoryConfig("cs.AI", "cs:AI", date(2026, 8, 1))
    blank = replace(_paper(), abstract="")
    whitespace = replace(_paper("2608.04002"), abstract=" \t\n\u00a0")
    _recover_retry_papers(
        store, config, date(2026, 8, 20), blank, whitespace, _paper("2608.04003")
    )
    _recover_retry_papers(
        store, inactive, date(2026, 8, 20), blank,
        replace(_paper("2608.04004", "cs.AI"), abstract=""),
    )
    _recover_retry_papers(
        store, config, date(2026, 8, 19),
        replace(_paper("2608.04005"), abstract=""),
    )
    _recover_retry_papers(
        store, config, date(2026, 8, 21), blank,
        replace(_paper("2608.04006"), abstract=""),
    )
    _seed_historical_finished_date(tmp_path, store, date(2026, 8, 21))
    deleted = replace(_paper("2608.04007"), abstract="")
    _recover_retry_papers(store, config, date(2026, 8, 22), blank, deleted)
    with open_database(tmp_path / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE articles SET is_deleted = 1 WHERE arxiv_id = ?",
            (deleted.arxiv_id,),
        )
    metadata_only = replace(_paper("2608.04008"), abstract="")
    store.apply_article_snapshot(metadata_only, ())
    store.save_paper(metadata_only.arxiv_id, None)

    assert store.unreviewed_papers_missing_abstracts(
        active_configs=(config,)
    ) == (blank.arxiv_id, whitespace.arxiv_id, "2608.04006")
    assert store.list_review_dates(active_configs=(config,)) == tuple(
        date(2026, 8, day) for day in (20, 21, 22)
    )
    assert store.unreviewed_papers_missing_abstracts(active_configs=()) == ()
    assert store.unreviewed_papers_missing_abstracts(
        active_configs=(replace(config, coverage_start=date(2026, 8, 21)),)
    ) == (blank.arxiv_id, "2608.04006")


def test_missing_abstract_retry_hydrates_once_without_changing_review_or_sync(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    blank = replace(_paper(), abstract="")
    _recover_retry_papers(store, config, date(2026, 8, 19), blank)
    _seed_historical_finished_date(tmp_path, store, date(2026, 8, 19))
    _recover_retry_papers(store, config, date(2026, 8, 20), blank)
    _recover_retry_papers(store, config, date(2026, 8, 21), blank)
    store.save_paper(blank.arxiv_id, None)
    confirmed_dates = tuple(date(2026, 8, day) for day in (19, 20, 21))
    events_before = tuple(store.events_for_date(day)[0] for day in confirmed_dates)
    assert store.list_review_dates(active_configs=(config,)) == confirmed_dates
    state_before = store.category_sync_state(config.category)
    coverage_before = store.catchup_day_records(config.category)
    revision_before = store.review_queue_revision()
    projection_before = store.review_revisions()[1]
    article = _article(versions=(_version(1, 49), _version(2, 50)))
    oai.records[blank.arxiv_id] = [article]
    attempts: list[str] = []

    report = service.retry_missing_abstracts((config,), attempted=attempts.append)

    assert report.offline is False
    assert attempts == oai.record_calls == [blank.arxiv_id]
    assert store.article_metadata(blank.arxiv_id) == article.metadata
    assert store.article_versions(blank.arxiv_id) == article.versions
    assert store.saved_paper_metadata() == (article.metadata,)
    assert store.unreviewed_papers_missing_abstracts(active_configs=(config,)) == ()
    assert store.review_queue_revision() == revision_before
    assert store.review_revisions()[1] > projection_before
    assert store.list_review_dates(active_configs=(config,)) == confirmed_dates
    assert store.category_sync_state(config.category) == state_before
    assert store.catchup_day_records(config.category) == coverage_before
    assert oai.first_calls == oai.backfill_calls == atom.calls == catchup.calls == []
    assert store.latest_sync_run(config.category, "incremental") is None
    events_after = tuple(
        store.events_for_date(day)[0] for day in store.list_review_dates()
    )
    assert [
        (event.event_id, event.daily_list_date, event.reviewed_at)
        for event in events_after
    ] == [
        (event.event_id, event.daily_list_date, event.reviewed_at)
        for event in events_before
    ]
    assert service.retry_missing_abstracts((config,)).offline is False
    assert oai.record_calls == [blank.arxiv_id]


def test_date_abstract_retry_includes_previously_reviewed_papers(
    tmp_path: Path,
) -> None:
    store, oai, atom, catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 18))
    day = date(2026, 8, 19)
    paper = replace(_paper(), abstract="")
    _recover_retry_papers(store, config, day, paper)
    _seed_historical_finished_date(tmp_path, store, day)
    oai.records[paper.arxiv_id] = [_article()]
    assert store.list_review_dates(active_configs=(config,)) == (day,)

    service.retry_missing_abstracts((config,), day=day)

    assert oai.record_calls == [paper.arxiv_id]
    assert store.list_review_dates(active_configs=(config,)) == (day,)
    assert store.events_for_date(day)[0].reviewed_at == NOW
    assert oai.first_calls == oai.backfill_calls == atom.calls == catchup.calls == []


@pytest.mark.parametrize(
    "failed_result",
    (
        RuntimeError("synthetic metadata outage"),
        replace(_article(), metadata=replace(_paper(), abstract=" \t\n")),
        _article(arxiv_id="2608.04999"),
        replace(_article(), oai_identifier="oai:arXiv.org:2608.04999"),
        OaiTombstone("oai:arXiv.org:2608.04001", date(2026, 8, 20), ("cs:SE",)),
    ),
    ids=("outage", "blank", "wrong-paper", "wrong-identifier", "tombstone"),
)
def test_missing_abstract_retry_keeps_failures_retryable_and_continues(
    tmp_path: Path, failed_result: object,
) -> None:
    store, oai, _atom, _catchup, service = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    first = replace(_paper(), abstract="")
    second = replace(_paper("2608.04002"), abstract="")
    _recover_retry_papers(store, config, date(2026, 8, 20), first, second)
    oai.records[first.arxiv_id] = [failed_result, failed_result]
    oai.records[second.arxiv_id] = [_article(arxiv_id=second.arxiv_id)]
    attempts: list[str] = []

    report = service.retry_missing_abstracts((config,), attempted=attempts.append)

    assert report.offline is False
    assert attempts == [first.arxiv_id, second.arxiv_id]
    assert store.article_metadata(first.arxiv_id).abstract == ""
    assert store.article_metadata(second.arxiv_id).abstract == _paper().abstract
    assert store.unreviewed_papers_missing_abstracts(
        active_configs=(config,)
    ) == (first.arxiv_id,)
    assert store.canonical_event_count() == 2
    assert service.retry_missing_abstracts((config,)).offline is True
    assert oai.record_calls == [first.arxiv_id, second.arxiv_id, first.arxiv_id]


@pytest.mark.parametrize("cancel_at", ("before", "transport", "after-response"))
def test_missing_abstract_retry_propagates_cancellation_without_applying_result(
    tmp_path: Path, cancel_at: str,
) -> None:
    from arxiv_digest.rate_limit import ArxivRequestCancelled

    store, _oai, atom, catchup, _service_instance = _service(tmp_path)
    config = CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    first = replace(_paper(), abstract="")
    second = replace(_paper("2608.04002"), abstract="")
    _recover_retry_papers(store, config, date(2026, 8, 20), first, second)
    cancellation = Event()
    calls: list[str] = []
    attempts: list[str] = []

    class CancellingOai:
        def get_record(self, arxiv_id: str, *, cancelled=None):
            calls.append(arxiv_id)
            assert callable(cancelled) and not cancelled()
            if cancel_at == "transport":
                raise ArxivRequestCancelled("synthetic cancellation")
            cancellation.set()
            return _article(arxiv_id=arxiv_id)

    if cancel_at == "before":
        cancellation.set()
    service = SyncService(
        store, CancellingOai(), atom, catchup,
        clock=lambda: NOW, cancelled=cancellation.is_set,
    )
    state_before = store.category_sync_state(config.category)

    with pytest.raises(SyncCancelled):
        service.retry_missing_abstracts((config,), attempted=attempts.append)

    expected_attempts = [] if cancel_at == "before" else [first.arxiv_id]
    assert attempts == calls == expected_attempts
    assert store.article_metadata(first.arxiv_id).abstract == ""
    assert store.article_metadata(second.arxiv_id).abstract == ""
    assert store.category_sync_state(config.category) == state_before
