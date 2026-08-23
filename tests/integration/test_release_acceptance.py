from __future__ import annotations

import json
import http.client
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from arxiv_digest.candidates import (
    CandidateCorpus,
    CandidateCorpusBuild,
    CandidateCorpusDiagnostics,
    CandidateDocument,
    build_suggestions,
    candidate_corpus_hash,
    search_candidate_papers,
)
from arxiv_digest.desktop_launcher import DesktopLauncherManager, LauncherState
from arxiv_digest.downloads import DownloadManager, safe_pdf_filename
from arxiv_digest.folders import FolderService
from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    CategoryConfig,
    CatchupDay,
    Confidence,
    DateBasis,
    EnrichmentStatus,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    OaiArticle,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.paths import AppPaths, resolve_paths
from arxiv_digest.profile import PdfDestination, Profile, ProfileRepository
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.review import ReviewService
from arxiv_digest.setup import CategorySelection, SetupService, SetupStateError
from arxiv_digest.sources.oai import OaiIdentify, OaiPage
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import DownloadFileRecord, Store
from arxiv_digest.sync import SyncService
from arxiv_digest.web.lifecycle import INACTIVITY_SECONDS, LifecycleController
from arxiv_digest.web.server import LoopbackServer


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "acceptance"
NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
FIXTURE_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


def _fixture() -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "release.json").read_text(encoding="utf-8"))


def _candidate_corpus(spec: dict[str, object]) -> CandidateCorpus:
    raw = spec["candidate_corpus"]
    assert isinstance(raw, dict)
    raw_categories = raw["categories"]
    assert isinstance(raw_categories, list)
    categories = tuple(str(value["category"]) for value in raw_categories)
    window_start = date.fromisoformat(str(raw["window_start"]))
    window_end = date.fromisoformat(str(raw["window_end"]))
    paper_count = int(raw["paper_count"])
    documents: list[CandidateDocument] = []
    for index in range(paper_count):
        category = categories[index % len(categories)]
        submitted = window_start + timedelta(days=index % 90)
        authors = (
            ("Riley Seed", "Jordan Seed")
            if index == 0
            else (
                f"Fixture Author {index:02d}",
                str(raw["recurring_author"]),
            )
        )
        paper = PaperMetadata(
            arxiv_id=f"2608.{index + 1:05d}",
            title=f"Candidate constellation {index + 1:02d}",
            authors=authors,
            abstract=(
                "A spectral garden links orchard invariants across the "
                f"{category} fixture stratum."
            ),
            primary_category=category,
            categories=(category,),
        )
        documents.append(
            CandidateDocument(
                paper=paper,
                versions=(
                    PaperVersion(
                        1,
                        datetime.combine(submitted, time(), tzinfo=timezone.utc),
                    ),
                ),
                eligible_categories=(category,),
                evidence_dates=(),
            )
        )
    return CandidateCorpus(
        schema_version=1,
        categories=categories,
        window_start=window_start,
        window_end=window_end,
        created_at=NOW,
        source_hashes=(sha256(b"release-acceptance-corpus").hexdigest(),),
        documents=tuple(documents),
    )


def _complete_setup(
    root: Path,
    corpus: CandidateCorpus,
    *,
    launcher_choice: str,
) -> tuple[SetupService, ProfileRepository, DesktopLauncherManager, Path]:
    root.mkdir(parents=True)
    database_path = root / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(root / "profile.json", root / "profile.lock")
    executable = root / "bin" / "arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic installed console entry\n")
    launcher = DesktopLauncherManager(
        platform="linux",
        home=root / "home",
        executable=executable,
    )
    service = SetupService(
        database_path,
        repository,
        launcher_manager=launcher,
        clock=lambda: NOW,
    )
    raw_categories = _fixture()["candidate_corpus"]["categories"]
    assert isinstance(raw_categories, list)

    draft = service.start()
    assert draft.revision == 0
    assert repository.load() is None
    draft = service.select_categories(
        draft.revision,
        tuple(
            CategorySelection(str(item["category"]), str(item["set_spec"]))
            for item in raw_categories
        ),
    )
    coverage_start = date.fromisoformat(
        str(_fixture()["synchronization"]["initial_coverage_start"])
    )
    draft = service.set_initial_coverage(
        draft.revision,
        coverage_start,
        earliest_datestamp=date(2007, 1, 1),
    )
    corpus_hash = candidate_corpus_hash(corpus)
    build = CandidateCorpusBuild(
        corpus,
        CandidateCorpusDiagnostics(
            complete=True,
            reduced_breadth=False,
            setup_ready=True,
            minimum_met=True,
            pages_fetched=54,
            progress=(),
            corpus_hash=corpus_hash,
            can_resume=False,
        ),
    )
    draft = service.accept_candidate_corpus(
        draft.revision,
        build,
        corpus_hash=corpus_hash,
    )

    suggestions = build_suggestions(
        corpus,
        (corpus.documents[0].paper.arxiv_id,),
        (),
        (),
    )
    recurring_keyword = str(_fixture()["candidate_corpus"]["recurring_keyword"])
    recurring_phrase = str(_fixture()["candidate_corpus"]["recurring_phrase"])
    recurring_author = str(_fixture()["candidate_corpus"]["recurring_author"])
    assert any(value.value == recurring_keyword for value in suggestions.terms)
    assert any(value.value == recurring_phrase for value in suggestions.terms)
    assert any(value.name == recurring_author for value in suggestions.authors)

    explicit = _fixture()["explicit_profile"]
    assert isinstance(explicit, dict)
    draft = service.select_seed_papers(draft.revision, (corpus.documents[0],))
    draft = service.select_terms(
        draft.revision,
        keywords=(recurring_keyword, str(explicit["custom_keyword"])),
        phrases=(recurring_phrase, str(explicit["custom_phrase"])),
    )
    draft = service.select_authors(
        draft.revision,
        (recurring_author, str(explicit["custom_author"])),
    )

    folders = FolderService(platform="linux", home=root / "home")
    downloads_choice = folders.standard_choices()[0]
    destination = folders.validate(downloads_choice)
    draft = service.set_pdf_destination(
        draft.revision,
        destination,
        tested=True,
    )
    draft = service.confirm_review(draft.revision)
    with pytest.raises(SetupStateError) as missing_choice:
        service.complete(draft.revision, launcher_choice=None)
    assert missing_choice.value.code == "launcher_choice_required"
    assert repository.load() is None
    profile = service.complete(
        draft.revision,
        launcher_choice=launcher_choice,  # type: ignore[arg-type]
    )
    assert profile.pdf_destination == destination
    return service, repository, launcher, database_path


class _FixturePdfClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Interface]] = []

    def get(
        self,
        url: str,
        *,
        interface: Interface,
        accept: str,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        assert accept == "application/pdf"
        assert max_bytes is not None and len(self.payload) <= max_bytes
        self.calls.append((url, interface))
        return HttpResponse(
            status=200,
            final_url=url,
            headers={"content-type": "application/pdf"},
            body=self.payload,
            observed_at=NOW,
        )


def test_empty_first_run_composes_explicit_setup_pdf_and_launcher_choices(
    tmp_path: Path,
) -> None:
    spec = _fixture()
    corpus = _candidate_corpus(spec)
    raw_corpus = spec["candidate_corpus"]
    assert isinstance(raw_corpus, dict)

    assert len(corpus.documents) == int(raw_corpus["paper_count"]) == 60
    visible = search_candidate_papers(
        corpus,
        "",
        limit=int(raw_corpus["visible_count"]),
    )
    assert len(visible) == 30
    visible_by_category = Counter(
        category
        for document in visible
        for category in document.eligible_categories
    )
    assert all(visible_by_category[category] >= 5 for category in corpus.categories)

    seed = corpus.documents[0]
    suggestions = build_suggestions(
        corpus,
        (seed.paper.arxiv_id,),
        (),
        (),
    )
    seed_title = seed.paper.title.casefold()
    assert any(
        term.kind == "keyword" and term.value not in seed_title
        for term in suggestions.terms
    )
    assert any(
        term.kind == "phrase"
        and " " in term.value
        and term.value not in seed_title
        for term in suggestions.terms
    )
    seed_authors = set(seed.paper.authors)
    assert any(author.name not in seed_authors for author in suggestions.authors)

    _service, repository, launcher, database_path = _complete_setup(
        tmp_path / "not-now",
        corpus,
        launcher_choice="not_now",
    )
    profile = repository.load()
    assert profile is not None
    explicit = spec["explicit_profile"]
    assert isinstance(explicit, dict)
    assert profile.categories == corpus.categories
    assert profile.seed_papers == (seed.paper.arxiv_id,)
    assert profile.keywords == (
        str(raw_corpus["recurring_keyword"]),
        str(explicit["custom_keyword"]),
    )
    assert profile.phrases == (
        str(raw_corpus["recurring_phrase"]),
        str(explicit["custom_phrase"]),
    )
    assert profile.authors == (
        str(raw_corpus["recurring_author"]),
        str(explicit["custom_author"]),
    )
    assert launcher.status().state is LauncherState.ABSENT
    assert not launcher.target.exists()
    state = Store(database_path).category_sync_state(corpus.categories[0])
    assert state.coverage_start == date(2026, 7, 23)

    pdf_payload = FIXTURE_PDF_BYTES
    client = _FixturePdfClient(pdf_payload)
    result = DownloadManager(
        Store(database_path),
        repository,
        client,
        max_pdf_bytes=1024,
        clock=lambda: NOW,
    ).download(seed.paper.arxiv_id, 1, save_first=True)
    written = profile.pdf_destination.path / result.filename
    assert written.read_bytes() == pdf_payload
    assert written.parent == tmp_path / "not-now" / "home/Downloads/Arxiv Digest"
    assert client.calls == [
        (
            f"https://arxiv.org/pdf/{seed.paper.arxiv_id}v1",
            Interface.PDF,
        )
    ]

    _created, _created_repository, created_launcher, _database = _complete_setup(
        tmp_path / "create",
        corpus,
        launcher_choice="create",
    )
    assert created_launcher.status().state is LauncherState.INSTALLED
    assert created_launcher.target.is_file()
    assert created_launcher.remove().state is LauncherState.ABSENT


class _ScriptedOai:
    def __init__(self) -> None:
        self.first: dict[str, list[object]] = defaultdict(list)
        self.next: dict[str, list[object]] = defaultdict(list)
        self.first_calls: list[tuple[str, date]] = []

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

    def identify(self, *, cancelled=None) -> OaiIdentify:
        return OaiIdentify(NOW, date(2007, 1, 1), "YYYY-MM-DD")


class _ScriptedAtom:
    def __init__(self) -> None:
        self.values: dict[str, list[object]] = defaultdict(list)

    def fetch(self, category: str, *, cancelled=None) -> AtomBatch:
        return _ScriptedOai._take(self.values[category])  # type: ignore[return-value]


class _ScriptedCatchup:
    def __init__(self) -> None:
        self.values: dict[tuple[str, date], object] = {}

    def fetch_day(
        self, category: str, mailing_date: date, *, cancelled=None
    ) -> CatchupDay:
        value = self.values[(category, mailing_date)]
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


def _oai_page(
    response_day: date,
    *,
    records: tuple[OaiArticle, ...] = (),
    token: str | None = None,
    marker: str = "a",
) -> OaiPage:
    return OaiPage(
        datetime.combine(response_day, time(2), tzinfo=timezone.utc),
        records,
        token,
        marker * 64,
    )


def _empty_atom(category: str, mailing_date: date) -> AtomBatch:
    return AtomBatch(
        category,
        mailing_date,
        (),
        sha256(f"atom:{category}:{mailing_date}".encode()).hexdigest(),
        datetime.combine(mailing_date, time(3), tzinfo=timezone.utc),
    )


def _sync_service(
    root: Path,
) -> tuple[Store, _ScriptedOai, _ScriptedAtom, _ScriptedCatchup, SyncService]:
    database_path = root / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    oai = _ScriptedOai()
    atom = _ScriptedAtom()
    catchup = _ScriptedCatchup()
    return (
        store,
        oai,
        atom,
        catchup,
        SyncService(
            store,
            oai,
            atom,
            catchup,
            clock=lambda: NOW,
            today=lambda: date(2026, 8, 22),
        ),
    )


def test_initial_thirty_day_sync_and_short_gap_gain_exact_coverage(
    tmp_path: Path,
) -> None:
    spec = _fixture()
    raw_categories = spec["candidate_corpus"]["categories"]
    assert isinstance(raw_categories, list)
    coverage_start = date.fromisoformat(
        str(spec["synchronization"]["initial_coverage_start"])
    )
    configs = tuple(
        CategoryConfig(
            str(value["category"]),
            str(value["set_spec"]),
            coverage_start,
        )
        for value in raw_categories
    )
    store, oai, atom, catchup, service = _sync_service(tmp_path)
    initial_completed = date.fromisoformat(
        str(spec["synchronization"]["initial_completed_through"])
    )
    for index, config in enumerate(configs):
        oai.first[config.oai_set_spec].append(
            _oai_page(initial_completed, marker=f"{index + 1:x}")
        )
        atom.values[config.category].append(
            _empty_atom(config.category, initial_completed)
        )

    initial = service.sync(configs, catchup_dates={})

    assert initial.metadata_complete is True
    assert (date(2026, 8, 22) - coverage_start).days == 30
    assert all(
        store.category_sync_state(config.category).coverage_start
        == coverage_start
        for config in configs
    )
    assert all(
        item.metadata_sync.completed_through_utc == initial_completed
        for item in initial.categories
    )

    short_gap_dates = tuple(
        date.fromisoformat(value)
        for value in spec["synchronization"]["short_gap_dates"]
    )
    for index, config in enumerate(configs):
        oai.first[config.oai_set_spec].append(
            _oai_page(short_gap_dates[-1], marker=f"{index + 4:x}")
        )
        atom.values[config.category].append(
            _empty_atom(config.category, short_gap_dates[-1])
        )
        for mailing_date in short_gap_dates:
            catchup.values[(config.category, mailing_date)] = CatchupDay(
                category=config.category,
                mailing_date=mailing_date,
                status=EnrichmentStatus.EMPTY,
                pages=(),
                error_code=None,
                error_message=None,
            )

    enriched = service.sync(
        configs,
        catchup_dates={config.category: short_gap_dates for config in configs},
    )

    assert enriched.metadata_complete is True
    assert enriched.missing_exact_dates == ()
    for config in configs:
        successful_days = {
            record.mailing_date
            for record in store.enrichment_records(config.category)
            if record.status in {EnrichmentStatus.COMPLETE, EnrichmentStatus.EMPTY}
        }
        assert set(short_gap_dates) <= successful_days


def _long_gap_article(
    arxiv_id: str,
    *,
    title: str,
    categories: tuple[str, ...],
    versions: tuple[PaperVersion, ...],
    datestamp: date,
    journal_ref: str = "",
) -> OaiArticle:
    return OaiArticle(
        oai_identifier=f"oai:arXiv.org:{arxiv_id}",
        oai_datestamp=datestamp,
        set_specs=tuple(value.replace(".", ":") for value in categories),
        metadata=PaperMetadata(
            arxiv_id=arxiv_id,
            title=title,
            authors=("Taylor Fixture",),
            abstract="A deterministic long-absence recovery fixture.",
            primary_category=categories[0],
            categories=categories,
            journal_ref=journal_ref,
        ),
        versions=versions,
    )


def test_long_absence_recovers_versions_merges_categories_and_ignores_admin_edits(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _sync_service(tmp_path)
    coverage_start = date(2026, 4, 1)
    configs = (
        CategoryConfig("synthetic.alpha", "synthetic:alpha", coverage_start),
        CategoryConfig("synthetic.beta", "synthetic:beta", coverage_start),
        CategoryConfig("synthetic.gamma", "synthetic:gamma", coverage_start),
    )
    administrative_version = (
        PaperVersion(1, datetime(2026, 4, 15, 9, tzinfo=timezone.utc)),
    )
    original_admin = _long_gap_article(
        "2604.00001",
        title="Original administrative fixture",
        categories=("synthetic.gamma",),
        versions=administrative_version,
        datestamp=date(2026, 4, 29),
    )
    for index, config in enumerate(configs):
        records = (original_admin,) if config.category == "synthetic.gamma" else ()
        oai.first[config.oai_set_spec].append(
            _oai_page(date(2026, 4, 30), records=records, marker=f"{index + 1:x}")
        )
        atom.values[config.category].append(
            _empty_atom(config.category, date(2026, 4, 30))
        )
    first = service.sync(configs, catchup_dates={})
    assert first.metadata_complete is True
    original_admin_event_ids = {
        event.event_id for event in store.events_for_date(date(2026, 4, 15))
    }
    assert len(original_admin_event_ids) == 1

    shared_versions = (
        PaperVersion(1, datetime(2026, 5, 1, 9, tzinfo=timezone.utc)),
        PaperVersion(2, datetime(2026, 6, 15, 9, tzinfo=timezone.utc)),
        PaperVersion(3, datetime(2026, 8, 10, 9, tzinfo=timezone.utc)),
    )
    shared = _long_gap_article(
        "2605.00002",
        title="Recovered multi-version fixture",
        categories=("synthetic.alpha", "synthetic.beta"),
        versions=shared_versions,
        datestamp=date(2026, 8, 21),
    )
    changed_admin = _long_gap_article(
        "2604.00001",
        title="Updated administrative fixture",
        categories=("synthetic.gamma",),
        versions=administrative_version,
        datestamp=date(2026, 8, 21),
        journal_ref="Synthetic Journal 1 (2026)",
    )
    for index, config in enumerate(configs):
        records = (
            (shared,)
            if config.category in {"synthetic.alpha", "synthetic.beta"}
            else (changed_admin,)
        )
        oai.first[config.oai_set_spec].append(
            _oai_page(date(2026, 8, 22), records=records, marker=f"{index + 4:x}")
        )
        atom.values[config.category].append(
            _empty_atom(config.category, date(2026, 8, 22))
        )

    recovered = service.sync(configs, catchup_dates={})

    assert recovered.metadata_complete is True
    assert (date(2026, 8, 22) - date(2026, 4, 30)).days > 90
    assert all(
        call[1] == date(2026, 4, 29)
        for call in oai.first_calls[len(configs) : 2 * len(configs)]
    )
    recovered_events = [
        event
        for day in (date(2026, 5, 1), date(2026, 6, 15), date(2026, 8, 10))
        for event in store.events_for_date(day)
        if event.arxiv_id == "2605.00002"
    ]
    assert [event.announced_version for event in recovered_events] == [1, 2, 3]
    assert len({event.event_id for event in recovered_events}) == 3
    assert all(
        {item.category for item in event.evidence}
        == {"synthetic.alpha", "synthetic.beta"}
        for event in recovered_events
    )
    identities = [
        (event.arxiv_id, event.announced_version, event.effective_date)
        for day in store.list_review_dates()
        for event in store.events_for_date(day)
    ]
    assert len(identities) == len(set(identities))
    assert {
        event.event_id for event in store.events_for_date(date(2026, 4, 15))
    } == original_admin_event_ids
    admin_metadata = store.article_metadata("2604.00001")
    assert admin_metadata.title == "Updated administrative fixture"
    assert admin_metadata.journal_ref == "Synthetic Journal 1 (2026)"

    oai.first["synthetic:alpha"].append(RuntimeError("synthetic category outage"))
    for index, config in enumerate(configs[1:], start=8):
        oai.first[config.oai_set_spec].append(
            _oai_page(date(2026, 8, 22), marker=f"{index:x}")
        )
    for config in configs:
        atom.values[config.category].append(
            _empty_atom(config.category, date(2026, 8, 22))
        )
    partial = service.sync(configs, catchup_dates={})
    progress = {item.category: item for item in partial.categories}
    assert partial.metadata_complete is False
    assert progress["synthetic.alpha"].metadata_sync.status == "failed"
    assert progress["synthetic.beta"].metadata_sync.completed_through_utc == date(
        2026, 8, 22
    )


def test_interrupted_page_chain_keeps_checkpoint_and_replay_has_no_duplicates(
    tmp_path: Path,
) -> None:
    store, oai, atom, _catchup, service = _sync_service(tmp_path)
    config = CategoryConfig(
        "synthetic.alpha", "synthetic:alpha", date(2026, 7, 23)
    )
    article = _long_gap_article(
        "2608.00003",
        title="Interrupted chain fixture",
        categories=("synthetic.alpha",),
        versions=(
            PaperVersion(1, datetime(2026, 8, 5, 9, tzinfo=timezone.utc)),
        ),
        datestamp=date(2026, 8, 20),
    )
    first_page = _oai_page(
        date(2026, 8, 20),
        records=(article,),
        token="acceptance-page-two",
    )
    oai.first[config.oai_set_spec].extend((first_page, first_page))
    oai.next["acceptance-page-two"].extend(
        (
            RuntimeError("synthetic interruption"),
            _oai_page(date(2026, 8, 21), marker="b"),
        )
    )
    atom.values[config.category].extend(
        (
            _empty_atom(config.category, date(2026, 8, 20)),
            _empty_atom(config.category, date(2026, 8, 21)),
        )
    )

    interrupted = service.sync((config,), catchup_dates={})
    assert interrupted.categories[0].metadata_sync.completed_through_utc is None
    assert store.category_sync_state(config.category).completed_through_utc is None

    resumed = service.sync((config,), catchup_dates={})
    assert resumed.metadata_complete is True
    assert oai.first_calls == [
        (config.oai_set_spec, config.coverage_start),
        (config.oai_set_spec, config.coverage_start),
    ]
    events = store.events_for_date(date(2026, 8, 5))
    assert len(events) == 1
    assert len({event.event_id for event in events}) == 1


def _isolated_paths(root: Path) -> AppPaths:
    paths = resolve_paths(
        platform="linux",
        home=root,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str((root / "app-state").resolve()),
        },
    )
    paths.ensure()
    return paths


def _publish_acceptance_profile(paths: AppPaths, destination: Path) -> ProfileRepository:
    destination.mkdir(parents=True)
    open_database(paths.database_path).close()
    repository = ProfileRepository(paths.profile_path, paths.profile_lock_path)
    SetupService(paths.database_path, repository, clock=lambda: NOW).publish_profile(
        Profile(
            schema_version=1,
            revision=1,
            categories=("synthetic.alpha",),
            keywords=("orchard",),
            phrases=("spectral garden",),
            authors=("Taylor Fixture",),
            seed_papers=(),
            pdf_destination=PdfDestination("custom", destination.resolve()),
        ),
        (CategoryConfig("synthetic.alpha", "synthetic:alpha", date(2026, 5, 1)),),
        expected_revision=None,
    )
    return repository


def _insert_backlog_event(store: Store, serial: int, day: date) -> int:
    arxiv_id = f"2608.{serial:05d}"
    metadata = PaperMetadata(
        arxiv_id=arxiv_id,
        title=f"Release backlog fixture {serial}",
        authors=(f"Backlog Author {serial % 13}",),
        abstract="A synthetic durable acceptance backlog record.",
        primary_category="synthetic.alpha",
        categories=("synthetic.alpha",),
    )
    version = PaperVersion(
        1,
        datetime.combine(day, time(9), tzinfo=timezone.utc),
    )
    source_key = f"acceptance:backlog:{day}:{arxiv_id}"
    candidate = EventCandidate(
        arxiv_id=arxiv_id,
        announced_version=1,
        effective_date=day,
        date_basis=DateBasis.FEED_MAILING,
        evidence=EventEvidence(
            source_key=source_key,
            source=EvidenceSource.ATOM,
            confidence=Confidence.CURRENT,
            category="synthetic.alpha",
            announce_type=AnnounceType.NEW,
            mailing_date=day,
            announced_version=1,
            list_position=serial,
            oai_datestamp=None,
            raw_sha256=sha256(source_key.encode()).hexdigest(),
            observed_at=NOW,
        ),
    )
    return store.apply_event_batch(metadata, (version,), (candidate,))[0].event_id


def _walk_review_date(
    service: ReviewService, day: date, first_event_id: int
) -> set[int]:
    page = service.open_date(day, anchor_event_id=first_event_id)
    reached: set[int] = set()
    while True:
        assert 1 <= len(page.cards) <= 20
        reached.update(card.event.event_id for card in page.cards)
        if page.next_anchor_event_id is None:
            return reached
        page = service.open_date(day, anchor_event_id=page.next_anchor_event_id)


def test_two_hundred_card_review_resumes_finishes_reopens_and_survives_cache_clear(
    tmp_path: Path,
) -> None:
    from arxiv_digest.application import create_application

    spec = _fixture()
    raw_backlog = spec["backlog"]
    assert isinstance(raw_backlog, dict)
    raw_dates = raw_backlog["dates"]
    assert isinstance(raw_dates, list)
    paths = _isolated_paths(tmp_path)
    repository = _publish_acceptance_profile(paths, tmp_path / "PDF destination")
    store = Store(paths.database_path)
    allocation: dict[date, set[int]] = {}
    serial = 1
    for item in raw_dates:
        day = date.fromisoformat(str(item["date"]))
        allocation[day] = set()
        for _ in range(int(item["count"])):
            allocation[day].add(_insert_backlog_event(store, serial, day))
            serial += 1
    assert serial - 1 == int(raw_backlog["total"]) == 200

    first_day, middle_day, last_day = tuple(sorted(allocation))
    service = ReviewService(store, repository)
    summary = service.summary()
    assert summary.unreviewed_papers == 200
    assert summary.oldest_unreviewed_date == first_day
    first = service.start()
    assert first is not None
    assert first.day == first_day
    assert len(first.cards) == 20
    assert first.page_count == 3

    next_anchor = first.next_anchor_event_id
    assert next_anchor is not None
    service.record_position(
        first_day,
        snapshot_revision=first.snapshot_revision,
        anchor_event_id=next_anchor,
    )
    resumed = ReviewService(store, repository).start()
    assert resumed is not None
    assert resumed.anchor_event_id == next_anchor
    assert resumed.page_number == 2

    reached = {
        day: _walk_review_date(service, day, min(allocation[day]))
        for day in (first_day, middle_day, last_day)
    }
    assert reached == allocation
    middle = service.open_date(middle_day)
    assert middle.previous_date == first_day
    assert middle.next_date == last_day
    assert middle.page_count == 6

    finish_snapshot = service.open_date(first_day)
    profile_before_finish = paths.profile_path.read_bytes()
    finished = service.finish_date(
        first_day,
        through_revision=finish_snapshot.snapshot_revision,
        finished_at=NOW,
    )
    assert finished.reviewed_count == 45
    assert paths.profile_path.read_bytes() == profile_before_finish
    assert service.summary().oldest_unreviewed_date == middle_day

    new_event_id = _insert_backlog_event(store, 201, first_day)
    reopened = service.summary()
    assert reopened.oldest_unreviewed_date == first_day
    assert reopened.newly_discovered == 1
    first_events = store.events_for_date(first_day)
    assert {event.event_id for event in first_events if event.reviewed_at is None} == {
        new_event_id
    }
    assert all(
        event.reviewed_at is not None
        for event in first_events
        if event.event_id != new_event_id
    )

    store.save_paper("2608.00001", 1)
    run_id = store.begin_sync_run(
        "synthetic.alpha",
        "incremental",
        date(2026, 5, 1),
        None,
        NOW,
    )
    checkpoint_at = datetime(2026, 8, 20, 2, tzinfo=timezone.utc)
    store.complete_incremental_run(
        run_id,
        checkpoint_at.date(),
        checkpoint_at,
        NOW,
    )
    profile_before_clear = paths.profile_path.read_bytes()
    checkpoint_before_clear = store.category_sync_state("synthetic.alpha")
    review_before_clear = tuple(store.events_for_date(first_day))
    library_before_clear = tuple(
        item.metadata.arxiv_id
        for item in store.search_library("", limit=20, offset=0)
    )
    candidate_cache = paths.cache_dir / "candidate-corpus" / "shards"
    candidate_cache.mkdir(parents=True)
    (candidate_cache / "disposable.json").write_text(
        "synthetic cache bytes", encoding="utf-8"
    )

    application = create_application(
        paths=paths,
        browser_open=lambda _url: True,
    )
    connection = application.open_database_action()
    try:
        result = application.handlers_factory()["settings_cache_clear"]({})
    finally:
        connection.close()

    assert result == {"cleared": True}
    assert not (paths.cache_dir / "candidate-corpus").exists()
    assert paths.profile_path.read_bytes() == profile_before_clear
    after_store = Store(paths.database_path)
    assert after_store.category_sync_state("synthetic.alpha") == checkpoint_before_clear
    assert tuple(after_store.events_for_date(first_day)) == review_before_clear
    assert tuple(
        item.metadata.arxiv_id
        for item in after_store.search_library("", limit=20, offset=0)
    ) == library_before_clear


def test_portable_backup_restore_round_trip_excludes_machine_local_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup, inspect_backup, restore_backup

    source = _isolated_paths(tmp_path / "source")
    source_destination = tmp_path / "source-machine-pdfs"
    repository = _publish_acceptance_profile(source, source_destination)
    profile = repository.load()
    assert profile is not None
    store = Store(source.database_path)
    day = date(2026, 6, 15)
    event_id = _insert_backlog_event(store, 301, day)
    store.save_paper("2608.00301", 1)
    snapshot_revision = store.review_snapshot(day).snapshot_revision
    store.finish_date(day, through_revision=snapshot_revision, finished_at=NOW)
    run_id = store.begin_sync_run(
        "synthetic.alpha", "incremental", date(2026, 5, 1), None, NOW
    )
    checkpoint_at = datetime(2026, 8, 20, 2, tzinfo=timezone.utc)
    store.complete_incremental_run(
        run_id, checkpoint_at.date(), checkpoint_at, NOW
    )

    pdf_payload = FIXTURE_PDF_BYTES
    pdf_name = safe_pdf_filename(
        "2608.00301", 1, "Release backlog fixture 301"
    )
    local_pdf = source_destination / pdf_name
    local_pdf.write_bytes(pdf_payload)
    store.record_download_file(
        DownloadFileRecord(
            arxiv_id="2608.00301",
            version=1,
            filename=pdf_name,
            byte_count=len(pdf_payload),
            sha256=sha256(pdf_payload).hexdigest(),
            last_verified_at=NOW,
        )
    )
    (source.cache_dir / "candidate-corpus").mkdir(parents=True)
    (source.cache_dir / "candidate-corpus/cache.json").write_text(
        "acceptance-cache-marker", encoding="utf-8"
    )
    source.runtime_descriptor_path.write_text(
        '{"token":"acceptance-runtime-token"}', encoding="utf-8"
    )

    archive = tmp_path / "portable.arxiv-digest-backup.zip"
    export_backup(source, archive, clock=lambda: NOW)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == [
            "manifest.json",
            "profile.json",
            "state.jsonl",
        ]
        profile_payload = bundle.read("profile.json")
        state_payload = bundle.read("state.jsonl")
    archive_bytes = archive.read_bytes()
    assert b"pdf_destination" not in profile_payload
    for excluded in (
        str(source_destination).encode(),
        pdf_payload,
        b"acceptance-cache-marker",
        b"acceptance-runtime-token",
        b"download_file",
    ):
        assert excluded not in archive_bytes

    inspection = inspect_backup(archive)
    assert inspection.profile.categories == ("synthetic.alpha",)
    assert {record.record_type for record in inspection.records} >= {
        "article",
        "version",
        "category_sync",
        "review_event",
        "review_date_state",
        "saved_paper",
    }
    assert b"acceptance-cache-marker" not in state_payload

    target = _isolated_paths(tmp_path / "target")
    target_destination = tmp_path / "target-confirmed-pdfs"
    target_destination.mkdir()
    restored = restore_backup(
        target,
        inspection,
        PdfDestination("custom", target_destination.resolve()),
        clock=lambda: NOW,
    )

    assert restored.pre_restore_path is None
    restored_profile = ProfileRepository(
        target.profile_path, target.profile_lock_path
    ).load()
    assert restored_profile is not None
    assert restored_profile.categories == profile.categories
    assert restored_profile.keywords == profile.keywords
    assert restored_profile.pdf_destination.path == target_destination.resolve()
    restored_store = Store(target.database_path)
    assert [
        item.metadata.arxiv_id
        for item in restored_store.search_library("", limit=20, offset=0)
    ] == ["2608.00301"]
    restored_event = next(
        event
        for event in restored_store.events_for_date(day)
        if event.event_id == event_id
    )
    assert restored_event.reviewed_at == NOW
    assert restored_store.category_sync_state(
        "synthetic.alpha"
    ).completed_through_utc == date(2026, 8, 20)
    assert restored_store.download_file("2608.00301", 1) is None
    assert not (target.cache_dir / "candidate-corpus").exists()


class _FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_server_quit_requires_authentication_and_idle_policy_is_thirty_minutes() -> None:
    clock = _FakeMonotonic()
    idle = LifecycleController(clock=clock)
    clock.advance(INACTIVITY_SECONDS - 1)
    assert idle.should_stop() is False
    clock.advance(1)
    assert idle.should_stop() is True

    lifecycle = LifecycleController(clock=_FakeMonotonic())
    server = LoopbackServer(handlers={}, lifecycle=lifecycle)
    server.start()
    try:
        def quit_request(*, authenticated: bool) -> tuple[int, dict[str, object]]:
            headers = {
                "Host": f"127.0.0.1:{server.port}",
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            }
            if authenticated:
                headers["Authorization"] = f"Bearer {server.token}"
            connection = http.client.HTTPConnection(
                server.host, server.port, timeout=2
            )
            connection.request(
                "POST", "/api/v1/application/quit", body=b"{}", headers=headers
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload

        denied_status, denied = quit_request(authenticated=False)
        assert denied_status == 401
        assert denied["error"]["code"] == "authentication_required"
        assert lifecycle.should_stop() is False

        accepted_status, accepted = quit_request(authenticated=True)
        assert accepted_status == 200
        assert accepted["data"] == {"quitting": True}
        assert lifecycle.should_stop() is True
    finally:
        server.stop()
