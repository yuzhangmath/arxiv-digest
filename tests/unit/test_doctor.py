from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from arxiv_digest.models import PaperMetadata, PaperVersion
from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import PdfDestination, Profile, ProfileRepository
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import Store


def _paths(tmp_path: Path):
    return resolve_paths(
        platform="linux",
        home=tmp_path,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "isolated"),
        },
    )


def test_fresh_doctor_is_redacted_read_only_and_creates_nothing(
    tmp_path: Path,
) -> None:
    from arxiv_digest.doctor import inspect_doctor, render_doctor

    paths = _paths(tmp_path)
    root = tmp_path / "isolated"

    report = inspect_doctor(paths, platform="linux")
    rendered = render_doctor(report)

    assert report.initialized is False
    assert "not initialized" in rendered.casefold()
    assert str(root) not in rendered
    assert not root.exists()


def test_initialized_doctor_reports_only_allowlisted_aggregate_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.doctor import inspect_doctor, render_doctor

    paths = _paths(tmp_path)
    paths.ensure()
    secret_destination = tmp_path / "Private Papers"
    secret_destination.mkdir()
    private_error_code = "private_secret_code"
    repository = ProfileRepository(paths.profile_path, paths.profile_lock_path)
    repository.save_atomic(
        Profile(
            schema_version=1,
            revision=7,
            categories=("cs.SE",),
            keywords=("secret topic",),
            phrases=("private phrase",),
            authors=("Sensitive Author",),
            seed_papers=("2608.09999",),
            pdf_destination=PdfDestination("custom", secret_destination),
        ),
        expected_revision=None,
    )
    connection = open_database(paths.database_path)
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               status, last_error_code, last_error_message
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "cs:SE",
            date(2026, 7, 1).isoformat(),
            date(2026, 8, 20).isoformat(),
            "failed",
            private_error_code,
            "secret transport detail /private/example",
        ),
    )
    connection.commit()
    connection.close()
    store = Store(paths.database_path)
    store.apply_event_batch(
        PaperMetadata(
            arxiv_id="2608.09999",
            title="Sensitive Paper Title",
            authors=("Sensitive Author",),
            abstract="Secret abstract text.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        (PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
        (),
    )
    store.save_paper("2608.09999", 1)

    report = inspect_doctor(paths, platform="linux")
    rendered = render_doctor(report)

    assert report.initialized is True
    assert report.schema_version == 3
    assert report.category_count == 1
    assert report.checkpoint_count == 1
    assert report.saved_paper_count == 1
    assert report.destination_kind == "custom"
    assert report.preference_counts == (1, 1, 1, 1)
    assert report.sync_error_codes == ("sync_error",)
    assert "sync_error" in rendered
    for secret in (
        str(tmp_path),
        str(secret_destination),
        "Sensitive Paper Title",
        "Sensitive Author",
        "secret topic",
        "private phrase",
        "2608.09999",
        "secret transport detail",
        private_error_code,
    ):
        assert secret not in rendered
