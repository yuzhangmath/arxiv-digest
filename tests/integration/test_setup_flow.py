from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from arxiv_digest.candidates import (
    CandidateCorpus,
    CandidateCorpusBuild,
    CandidateCorpusDiagnostics,
    CandidateDocument,
    candidate_corpus_hash,
)
from arxiv_digest.models import CategoryConfig, PaperMetadata, PaperVersion
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileRepository,
    ProfileRevisionError,
)
from arxiv_digest.setup import (
    CategorySelection,
    SetupRecoveryError,
    SetupRoute,
    SetupService,
    SetupStateError,
)
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


class RecordingLauncher:
    def __init__(self) -> None:
        self.install_calls = 0

    def install(self) -> None:
        self.install_calls += 1


class FlakyLauncher(RecordingLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def install(self) -> None:
        super().install()
        if self.fail:
            raise RuntimeError("SECRET filesystem detail must not be persisted")


def _seed() -> CandidateDocument:
    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id="2608.01234",
            title="Selected geometry",
            authors=("Ada Example",),
            abstract="Derived geometry.",
            primary_category="math.AG",
            categories=("math.AG",),
        ),
        versions=(PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
        eligible_categories=("math.AG",),
        evidence_dates=(date(2026, 8, 1),),
    )


def _ready_draft(service: SetupService, destination: Path):
    draft = service.start()
    draft = service.select_categories(
        draft.revision,
        (CategorySelection("math.AG", "arXiv:math.AG"),),
    )
    draft = service.set_initial_coverage(
        draft.revision,
        date(2026, 7, 23),
        earliest_datestamp=date(2007, 1, 1),
    )
    corpus = CandidateCorpus(
        schema_version=1,
        categories=("math.AG",),
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=NOW,
        source_hashes=("a" * 64,),
        documents=(_seed(),),
    )
    digest = candidate_corpus_hash(corpus)
    build = CandidateCorpusBuild(
        corpus,
        CandidateCorpusDiagnostics(
            complete=True,
            reduced_breadth=False,
            setup_ready=True,
            minimum_met=False,
            pages_fetched=18,
            progress=(),
            corpus_hash=digest,
            can_resume=False,
        ),
    )
    draft = service.accept_candidate_corpus(
        draft.revision, build, corpus_hash=digest
    )
    draft = service.select_seed_papers(draft.revision, (_seed(),))
    draft = service.select_terms(
        draft.revision,
        keywords=("derived geometry",),
        phrases=("mirror symmetry",),
    )
    draft = service.select_authors(draft.revision, ("Ada Example",))
    draft = service.set_pdf_destination(
        draft.revision,
        PdfDestination("custom", destination.resolve()),
        tested=True,
    )
    return service.confirm_review(draft.revision)


def test_not_now_publishes_exact_profile_category_and_seed_metadata(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    launcher = RecordingLauncher()
    service = SetupService(
        database_path,
        repository,
        launcher_manager=launcher,
        clock=lambda: NOW,
    )
    draft = _ready_draft(service, tmp_path / "pdfs")

    with pytest.raises(SetupStateError) as missing:
        service.complete(draft.revision, launcher_choice=None)
    assert missing.value.code == "launcher_choice_required"
    assert repository.load() is None

    profile = service.complete(draft.revision, launcher_choice="not_now")
    assert profile.revision == 1
    assert profile.categories == ("math.AG",)
    assert profile.seed_papers == ("2608.01234",)
    assert profile.keywords == ("derived geometry",)
    assert launcher.install_calls == 0
    assert service.load_draft() is None
    assert service.route() is SetupRoute.INTERESTS

    connection = sqlite3.connect(database_path)
    try:
        category = connection.execute(
            "SELECT set_spec, coverage_start FROM category_sync_state "
            "WHERE category = 'math.AG'"
        ).fetchone()
        article = connection.execute(
            "SELECT title FROM articles WHERE arxiv_id = '2608.01234'"
        ).fetchone()
        settings = connection.execute(
            "SELECT launcher_operation, launcher_last_error_code "
            "FROM application_settings WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    assert category == ("arXiv:math.AG", "2026-07-23")
    assert article == ("Selected geometry",)
    assert settings == ("none", None)


def test_launcher_failure_is_redacted_retryable_and_never_reopens_setup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    launcher = FlakyLauncher()
    service = SetupService(
        database_path,
        repository,
        launcher_manager=launcher,
        clock=lambda: NOW,
    )
    draft = _ready_draft(service, tmp_path / "pdfs")

    profile = service.complete(draft.revision, launcher_choice="create")
    assert repository.load() == profile
    assert service.launcher_settings().operation == "create_failed"
    assert service.launcher_settings().error_code == "launcher_install_failed"
    assert b"SECRET" not in database_path.read_bytes()

    restarted = SetupService(
        database_path,
        repository,
        launcher_manager=launcher,
        clock=lambda: NOW,
    )
    assert restarted.route() is SetupRoute.INTERESTS
    assert launcher.install_calls == 1

    launcher.fail = False
    assert restarted.retry_launcher().operation == "none"
    assert launcher.install_calls == 2
    assert restarted.launcher_settings().error_code is None


@pytest.mark.parametrize(
    "crash_phase, expected_revision, expected_coverage",
    (
        ("pending_file_fsynced", 1, "2026-07-23"),
        ("sqlite_pending_committed", 2, "2026-06-01"),
        ("profile_replaced", 2, "2026-06-01"),
        ("publication_marked", 2, "2026-06-01"),
    ),
)
def test_each_publication_phase_recovers_an_exact_old_or_new_revision(
    tmp_path: Path,
    crash_phase: str,
    expected_revision: int,
    expected_coverage: str,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    initial = SetupService(database_path, repository, clock=lambda: NOW)
    draft = _ready_draft(initial, tmp_path / "pdfs")
    old_profile = initial.complete(draft.revision, launcher_choice="not_now")

    def crash_after(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"injected crash after {phase}")

    publisher = SetupService(
        database_path,
        repository,
        clock=lambda: NOW,
        crash_injector=crash_after,
    )
    new_profile = Profile(
        schema_version=1,
        revision=2,
        categories=("math.AG",),
        keywords=("new preference",),
        phrases=old_profile.phrases,
        authors=old_profile.authors,
        seed_papers=old_profile.seed_papers,
        pdf_destination=old_profile.pdf_destination,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        publisher.publish_profile(
            new_profile,
            (CategoryConfig("math.AG", "arXiv:math.AG", date(2026, 6, 1)),),
            expected_revision=1,
        )

    recovered = SetupService(database_path, repository, clock=lambda: NOW)
    recovered.recover()
    active = repository.load()
    assert active is not None
    assert active.revision == expected_revision
    assert active.keywords == (
        ("new preference",) if expected_revision == 2 else ("derived geometry",)
    )
    connection = sqlite3.connect(database_path)
    try:
        coverage = connection.execute(
            "SELECT coverage_start FROM category_sync_state "
            "WHERE category = 'math.AG'"
        ).fetchone()[0]
        marker = connection.execute(
            "SELECT pending_revision, status FROM profile_publication "
            "WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    assert coverage == expected_coverage
    assert marker == (expected_revision, "published")
    assert not (tmp_path / "profile.json.pending").exists()


def test_maintenance_operation_spans_every_publication_phase(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    barrier = MaintenanceBarrier()
    store = Store(database_path, maintenance=barrier)
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    observed: list[tuple[str, int]] = []
    service = SetupService(
        store,
        repository,
        clock=lambda: NOW,
        crash_injector=lambda phase: observed.append(
            (phase, barrier.active_operations)
        ),
    )
    draft = _ready_draft(service, tmp_path / "pdfs")

    service.complete(draft.revision, launcher_choice="not_now")

    assert observed == [
        ("pending_file_fsynced", 1),
        ("sqlite_pending_committed", 1),
        ("profile_replaced", 1),
        ("publication_marked", 1),
    ]
    assert barrier.active_operations == 0


def test_interests_publication_requires_revision_and_retains_removed_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(database_path, repository, clock=lambda: NOW)
    draft = _ready_draft(service, tmp_path / "pdfs")
    old = service.complete(draft.revision, launcher_choice="not_now")
    replacement = Profile(
        schema_version=1,
        revision=2,
        categories=("math.NT",),
        keywords=("automorphic forms",),
        phrases=(),
        authors=(),
        seed_papers=old.seed_papers,
        pdf_destination=old.pdf_destination,
    )

    service.publish_profile(
        replacement,
        (CategoryConfig("math.NT", "arXiv:math.NT", date(2026, 8, 2)),),
        expected_revision=1,
    )

    assert repository.load() == replacement
    connection = sqlite3.connect(database_path)
    try:
        categories = connection.execute(
            "SELECT category, set_spec, coverage_start "
            "FROM category_sync_state ORDER BY category"
        ).fetchall()
    finally:
        connection.close()
    assert categories == [
        ("math.AG", "arXiv:math.AG", "2026-07-23"),
        ("math.NT", "arXiv:math.NT", "2026-08-02"),
    ]

    with pytest.raises(ProfileRevisionError):
        service.publish_profile(
            replacement,
            (CategoryConfig("math.NT", "arXiv:math.NT", date(2026, 8, 2)),),
            expected_revision=1,
        )
    assert repository.load() == replacement


def test_recovery_stops_when_neither_profile_file_matches_marker(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    crashing = SetupService(
        database_path,
        repository,
        clock=lambda: NOW,
        crash_injector=lambda phase: (
            (_ for _ in ()).throw(RuntimeError("injected crash"))
            if phase == "sqlite_pending_committed"
            else None
        ),
    )
    draft = _ready_draft(crashing, tmp_path / "pdfs")
    with pytest.raises(RuntimeError, match="injected crash"):
        crashing.complete(draft.revision, launcher_choice="not_now")
    (tmp_path / "profile.json.pending").write_bytes(b"tampered")

    recovered = SetupService(database_path, repository, clock=lambda: NOW)
    with pytest.raises(SetupRecoveryError, match="known-good backup"):
        recovered.recover()
    assert repository.load() is None
    assert recovered.load_draft() is not None
