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
from arxiv_digest.models import PaperMetadata, PaperVersion
from arxiv_digest.profile import PdfDestination, ProfileRepository
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


def test_initial_coverage_honors_oai_boundary_and_warns_after_90_days(
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
            date(2006, 1, 1),
            earliest_datestamp=date(2007, 1, 1),
        )
    assert error.value.code == "coverage_before_earliest"
    assert service.load_draft() == draft

    revised = service.set_initial_coverage(
        draft.revision,
        date(2026, 5, 1),
        earliest_datestamp=date(2007, 1, 1),
    )
    assert revised.current_step is SetupStep.CANDIDATE_CORPUS
    assert revised.coverage_start == date(2026, 5, 1)
    assert "inferred metadata/version events" in revised.coverage_warning
    assert "bulk-update" in revised.coverage_warning


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
