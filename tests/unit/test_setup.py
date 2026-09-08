from __future__ import annotations

import json
import sqlite3
import threading
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
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
from arxiv_digest.setup import (
    CREATE_DESKTOP_LAUNCHER_LABEL,
    CategorySelection,
    DESKTOP_LAUNCHER_PROMPT,
    NOT_NOW_LABEL,
    SetupRevisionError,
    SetupService,
    SetupStateError,
    SetupStep,
)
from arxiv_digest.storage.database import open_database


def test_setup_connections_hold_maintenance_lease_until_close(
    tmp_path: Path,
) -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    barrier = MaintenanceBarrier()
    repository = ProfileRepository(
        tmp_path / "profile.json",
        tmp_path / "profile.lock",
        maintenance=barrier,
    )
    service = SetupService(
        database_path,
        repository,
        maintenance=barrier,
    )
    connection = service._connect()
    exclusive_entered = threading.Event()

    def restore() -> None:
        with barrier.exclusive(timeout=2):
            exclusive_entered.set()

    thread = threading.Thread(target=restore)
    thread.start()
    try:
        assert not exclusive_entered.wait(0.05)
    finally:
        connection.close()
    thread.join(2)

    assert exclusive_entered.is_set()
    assert not thread.is_alive()


def test_setup_migration_creates_singleton_state_tables(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "state.sqlite3")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        publication = connection.execute(
            "SELECT pending_revision, pending_sha256, status "
            "FROM profile_publication WHERE singleton = 1"
        ).fetchone()
        settings = connection.execute(
            "SELECT launcher_operation, launcher_last_error_code "
            "FROM application_settings WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()

    assert {"setup_draft", "profile_publication", "application_settings"} <= tables
    assert publication == (None, None, "none")
    assert settings == ("none", None)


def test_desktop_launcher_prompt_and_actions_are_exact_and_unselected() -> None:
    assert DESKTOP_LAUNCHER_PROMPT == (
        "Would you like a desktop launcher so you can open arXiv Digest "
        "without using the terminal?"
    )
    assert CREATE_DESKTOP_LAUNCHER_LABEL == "Create desktop launcher"
    assert NOT_NOW_LABEL == "Not now"


def test_pending_setup_launcher_obeys_the_same_transition_guard_and_can_retry(
    tmp_path: Path,
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherState,
        launcher_operation_guard,
    )
    from arxiv_digest.paths import resolve_paths
    from arxiv_digest.update_locks import acquire_exclusive

    paths = resolve_paths(
        platform="linux",
        home=tmp_path,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "application"),
        },
    )
    paths.ensure()
    paths.ensure_update_coordination()
    connection = open_database(paths.database_path)
    connection.execute(
        "UPDATE application_settings SET launcher_operation = 'create_failed', "
        "launcher_last_error_code = 'launcher_io_error' WHERE singleton = 1"
    )
    connection.commit()
    connection.close()
    executable = tmp_path / "arxiv-digest"
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="linux",
        home=tmp_path,
        executable=executable,
        operation_guard=lambda: launcher_operation_guard(paths, timeout=0.01),
    )
    service = SetupService(
        paths.database_path,
        ProfileRepository(paths.profile_path, paths.profile_lock_path),
        launcher_manager=manager,
    )
    owner = acquire_exclusive(paths.update_transition_lock_path, timeout=0)
    try:
        blocked = service.retry_launcher()
        assert blocked.operation == "create_failed"
        assert blocked.error_code == "launcher_io_error"
        assert not manager.target.exists()
    finally:
        owner.release()

    retried = service.retry_launcher()
    assert retried.operation == "none"
    assert retried.error_code is None
    assert manager.status().state is LauncherState.INSTALLED


def test_draft_decoder_rejects_invalid_payload_types(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(database_path, repository)
    service.start()
    connection = sqlite3.connect(database_path)
    try:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM setup_draft WHERE singleton = 1"
            ).fetchone()[0]
        )
        payload["corpus_complete"] = 1
        connection.execute(
            "UPDATE setup_draft SET payload_json = ? WHERE singleton = 1",
            (json.dumps(payload),),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="corpus_complete must be boolean"):
        service.load_draft()


def test_draft_survives_restart_and_rejects_stale_edits(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    clock = lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    first = SetupService(database_path, repository, clock=clock)
    draft = first.start()
    assert draft.revision == 0
    assert draft.current_step is SetupStep.CATEGORIES

    revised = first.select_categories(
        draft.revision,
        (CategorySelection("math.AG", "math.AG"),),
    )
    assert revised.revision == 1
    assert revised.current_step is SetupStep.INITIAL_COVERAGE
    assert repository.load() is None

    restarted = SetupService(database_path, repository, clock=clock)
    assert restarted.load_draft() == revised
    with pytest.raises(SetupRevisionError) as error:
        restarted.select_categories(
            draft.revision,
            (CategorySelection("math.NT", "math.NT"),),
        )
    assert (error.value.expected, error.value.actual) == (0, 1)


def test_initial_coverage_rejects_dates_outside_supported_catchup_window(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(
        database_path,
        repository,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    draft = service.start()
    draft = service.select_categories(
        draft.revision, (CategorySelection("math.AG", "math.AG"),)
    )

    with pytest.raises(SetupStateError) as error:
        service.set_initial_coverage(
            draft.revision,
            date(2026, 5, 24),
            earliest_datestamp=date(2007, 1, 1),
        )
    assert error.value.code == "coverage_outside_recovery_window"
    assert service.load_draft() == draft

    revised = service.set_initial_coverage(
        draft.revision,
        date(2026, 5, 25),
        earliest_datestamp=date(2007, 1, 1),
    )
    assert revised.current_step is SetupStep.CANDIDATE_CORPUS
    assert revised.coverage_start == date(2026, 5, 25)
    assert revised.coverage_warning is None


def test_initial_coverage_rejects_current_eastern_date_until_finalization(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    observed_at = {
        "value": datetime(2026, 8, 22, 23, 59, tzinfo=timezone.utc)
    }
    service = SetupService(
        database_path,
        repository,
        clock=lambda: observed_at["value"],
    )
    draft = service.start()
    draft = service.select_categories(
        draft.revision, (CategorySelection("math.AG", "math.AG"),)
    )

    with pytest.raises(SetupStateError) as error:
        service.set_initial_coverage(
            draft.revision,
            date(2026, 8, 22),
            earliest_datestamp=date(2007, 1, 1),
        )
    assert error.value.code == "coverage_outside_recovery_window"

    observed_at["value"] = datetime(
        2026, 8, 23, 0, 0, tzinfo=timezone.utc
    )
    revised = service.set_initial_coverage(
        draft.revision,
        date(2026, 8, 22),
        earliest_datestamp=date(2007, 1, 1),
    )
    assert revised.coverage_start == date(2026, 8, 22)


def test_initial_coverage_validation_uses_server_issued_bounds(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    service = SetupService(
        database_path,
        ProfileRepository(
            tmp_path / "profile.json", tmp_path / "profile.lock"
        ),
        clock=lambda: datetime(2026, 8, 23, 0, tzinfo=timezone.utc),
    )
    draft = service.select_categories(
        service.start().revision,
        (CategorySelection("math.AG", "math.AG"),),
    )

    with pytest.raises(SetupStateError) as error:
        service.set_initial_coverage(
            draft.revision,
            date(2026, 8, 22),
            earliest_datestamp=date(2007, 1, 1),
            coverage_bounds=(date(2026, 5, 24), date(2026, 8, 21)),
        )

    assert error.value.code == "coverage_outside_recovery_window"


def _corpus_build(
    category: str,
    *,
    complete: bool = False,
    reduced: bool = True,
    minimum: bool = True,
) -> CandidateCorpusBuild:
    corpus = CandidateCorpus(
        schema_version=1,
        categories=(category,),
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        source_hashes=("1" * 64,),
        documents=(),
    )
    digest = candidate_corpus_hash(corpus)
    return CandidateCorpusBuild(
        corpus=corpus,
        diagnostics=CandidateCorpusDiagnostics(
            complete=complete,
            reduced_breadth=reduced,
            setup_ready=complete or (reduced and minimum),
            minimum_met=minimum,
            pages_fetched=1,
            progress=(),
            corpus_hash=digest,
            can_resume=not complete,
        ),
    )


def test_corpus_acceptance_is_hash_and_category_bound_and_resumable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(
        database_path,
        repository,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    draft = service.start()
    draft = service.select_categories(
        draft.revision, (CategorySelection("math.AG", "math.AG"),)
    )
    draft = service.set_initial_coverage(
        draft.revision,
        date(2026, 8, 1),
        earliest_datestamp=date(2007, 1, 1),
    )

    with pytest.raises(SetupStateError) as stale:
        service.accept_candidate_corpus(
            draft.revision, _corpus_build("math.AG"), corpus_hash="0" * 64
        )
    assert stale.value.code == "stale_corpus"
    with pytest.raises(SetupStateError) as mismatch:
        service.accept_candidate_corpus(
            draft.revision,
            _corpus_build("math.NT"),
            corpus_hash=_corpus_build("math.NT").corpus_hash,
        )
    assert mismatch.value.code == "corpus_category_mismatch"

    visible = _corpus_build("math.AG")
    accepted = service.accept_candidate_corpus(
        draft.revision, visible, corpus_hash=visible.corpus_hash
    )
    assert accepted.current_step is SetupStep.SEED_PAPERS
    assert accepted.corpus_reduced_breadth is True
    assert accepted.corpus_hash == visible.corpus_hash
    assert SetupService(database_path, repository).load_draft() == accepted


def _seed(arxiv_id: str = "2608.01234") -> CandidateDocument:
    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id=arxiv_id,
            title="A custom-selected paper",
            authors=("Ada Example",),
            abstract="An abstract.",
            primary_category="math.AG",
            categories=("math.AG",),
        ),
        versions=(
            PaperVersion(
                1, datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
            ),
        ),
        eligible_categories=("math.AG",),
        evidence_dates=(date(2026, 8, 1),),
    )


def _draft_through_corpus(service: SetupService):
    draft = service.start()
    draft = service.select_categories(
        draft.revision, (CategorySelection("math.AG", "math.AG"),)
    )
    draft = service.set_initial_coverage(
        draft.revision,
        date(2026, 8, 1),
        earliest_datestamp=date(2007, 1, 1),
    )
    visible = _corpus_build("math.AG")
    return service.accept_candidate_corpus(
        draft.revision, visible, corpus_hash=visible.corpus_hash
    )


def test_explicit_custom_selections_survive_through_review(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(
        database_path,
        repository,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    draft = _draft_through_corpus(service)
    custom_paper = _seed()
    draft = service.select_seed_papers(draft.revision, (custom_paper,))
    draft = service.select_terms(
        draft.revision,
        keywords=(" derived category ",),
        phrases=("homological mirror symmetry",),
    )
    draft = service.select_authors(draft.revision, ("Ada Example",))
    destination = PdfDestination("custom", (tmp_path / "pdfs").resolve())
    draft = service.set_pdf_destination(
        draft.revision, destination, tested=True
    )

    summary = service.review_summary(draft)
    assert summary.seed_papers == ("2608.01234",)
    assert summary.keywords == ("derived category",)
    assert summary.phrases == ("homological mirror symmetry",)
    assert summary.authors == ("Ada Example",)
    assert summary.categories == ("math.AG",)
    assert repository.load() is None

    reviewed = service.confirm_review(draft.revision)
    assert reviewed.current_step is SetupStep.DESKTOP_LAUNCHER
    assert reviewed.review_confirmed is True


def test_empty_optional_interests_advance_and_publish(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(
        database_path,
        repository,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    draft = _draft_through_corpus(service)

    draft = service.select_seed_papers(draft.revision, ())
    draft = service.select_terms(draft.revision, keywords=(), phrases=())
    draft = service.select_authors(draft.revision, ())
    destination = PdfDestination("custom", (tmp_path / "pdfs").resolve())
    draft = service.set_pdf_destination(
        draft.revision, destination, tested=True
    )
    reviewed = service.confirm_review(draft.revision)
    profile = service.complete(reviewed.revision, launcher_choice="not_now")

    assert profile.schema_version == 2
    assert profile.category_coverage == (
        ProfileCategory("math.AG", date(2026, 8, 1)),
    )
    assert profile.seed_papers == ()
    assert profile.keywords == ()
    assert profile.phrases == ()
    assert profile.authors == ()
    assert repository.load() == profile
    connection = sqlite3.connect(database_path)
    try:
        projection_revision = connection.execute(
            "SELECT projection_revision FROM state_meta WHERE singleton = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert projection_revision == 1


def test_profile_publication_requires_exact_category_coverage_pairs(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(
        database_path,
        repository,
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    draft = _draft_through_corpus(service)
    draft = service.select_seed_papers(draft.revision, ())
    draft = service.select_terms(draft.revision, keywords=(), phrases=())
    draft = service.select_authors(draft.revision, ())
    draft = service.set_pdf_destination(
        draft.revision,
        PdfDestination("custom", (tmp_path / "pdfs").resolve()),
        tested=True,
    )
    reviewed = service.confirm_review(draft.revision)
    original = service.complete(reviewed.revision, launcher_choice="not_now")
    revised = Profile(
        schema_version=2,
        revision=2,
        category_coverage=(
            ProfileCategory("math.AG", date(2026, 7, 15)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=original.pdf_destination,
    )

    with pytest.raises(ValueError, match="category coverage"):
        service.publish_profile(
            revised,
            (CategoryConfig("math.AG", "math.AG", date(2026, 8, 1)),),
            expected_revision=1,
        )

    assert repository.load() == original


def test_preference_only_profile_publication_preserves_projection_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    repository = ProfileRepository(
        tmp_path / "profile.json", tmp_path / "profile.lock"
    )
    service = SetupService(database_path, repository)
    config = CategoryConfig("math.AG", "math.AG", date(2026, 8, 1))
    initial = Profile(
        schema_version=2,
        revision=1,
        category_coverage=(
            ProfileCategory(config.category, config.coverage_start),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", (tmp_path / "pdfs").resolve()),
    )
    service.publish_profile(initial, (config,), expected_revision=None)
    revised = Profile(
        schema_version=2,
        revision=2,
        category_coverage=initial.category_coverage,
        keywords=("derived categories",),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=initial.pdf_destination,
    )

    service.publish_profile(revised, (config,), expected_revision=1)

    connection = sqlite3.connect(database_path)
    try:
        projection_revision = connection.execute(
            "SELECT projection_revision FROM state_meta WHERE singleton = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert projection_revision == 1
