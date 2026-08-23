from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    CategoryConfig,
    CatchupDay,
    EnrichmentStatus,
    OaiArticle,
    PaperMetadata,
    PaperVersion,
)
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
        datetime(2026, 7 if day < 32 else 8, day if day < 32 else day - 31, 9, tzinfo=timezone.utc),
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


class ScriptedAtom:
    def __init__(self) -> None:
        self.values: dict[str, list[object]] = defaultdict(list)

    def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
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


def _service(tmp_path: Path) -> tuple[Store, ScriptedOai, ScriptedAtom, ScriptedCatchup, SyncService]:
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
    events = store.events_for_date(date(2026, 8, 5))
    assert len(events) == 1
    assert events[0].evidence[0].oai_datestamp == date(2026, 8, 20)


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
    assert len(store.events_for_date(date(2026, 8, 5))) == 1


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
        version=version,
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
    assert store.article_version("2608.04002", 1).submitted_at == version.submitted_at
    event = store.events_for_date(date(2026, 8, 20))[0]
    assert event.announced_version == 1


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


def test_extending_coverage_requeues_stored_history_before_bounded_fetch(
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
    event = store.events_for_date(date(2026, 7, 15))[0]
    assert event.evidence[0].raw_sha256 == "9" * 64
    assert oai.backfill_calls == [
        ("cs:SE", date(2026, 7, 1), date(2026, 7, 31))
    ]


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
            error_code="catchup_fixture_failure" if status is EnrichmentStatus.FAILED else None,
            error_message="Synthetic catch-up failure." if status is EnrichmentStatus.FAILED else None,
        )

    report = service.sync(
        (config,),
        catchup_dates={"cs.SE": (date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20))},
    )

    progress = report.categories[0]
    assert progress.exact_start == date(2026, 8, 18)
    assert progress.exact_end == date(2026, 8, 20)
    assert progress.missing_exact_dates == (date(2026, 8, 19),)


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

    assert catchup.calls == []
