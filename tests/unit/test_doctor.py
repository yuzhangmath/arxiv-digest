from __future__ import annotations

from datetime import date
from pathlib import Path

from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
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
            schema_version=2,
            revision=7,
            category_coverage=(
                ProfileCategory("cs.SE", date(2026, 5, 20)),
            ),
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
    connection.execute(
        """INSERT INTO category_sync_state(
               category, set_spec, coverage_start, completed_through_utc,
               status
           ) VALUES ('cs.LG', 'cs:LG', '2026-07-01', '2026-08-20', 'idle')"""
    )
    connection.executemany(
        """INSERT INTO catchup_days(
               category, daily_list_date, status, attempted_at, error_code
           ) VALUES ('cs.SE', ?, ?, '2026-08-22T12:00:00+00:00', ?)""",
        (
            ("2026-05-20", "failed", "private_catchup_code"),
            ("2026-08-18", "empty", None),
            ("2026-08-19", "failed", "catchup_layout_changed"),
            ("2026-08-21", "complete", None),
        ),
    )
    connection.execute(
        """INSERT INTO catchup_days(category, daily_list_date, status)
           VALUES ('cs.SE', '2026-08-20', 'pending')"""
    )
    connection.execute(
        "UPDATE state_meta SET projection_revision = 3 WHERE singleton = 1"
    )
    connection.commit()
    connection.close()
    store = Store(paths.database_path)
    connection = store._connect()
    connection.execute(
        """INSERT INTO articles(
               arxiv_id, title, abstract, primary_category, metadata_hash
           ) VALUES ('2608.09999', 'Sensitive Paper Title',
                     'Secret abstract text.', 'cs.SE', ?)""",
        ("a" * 64,),
    )
    connection.execute(
        """INSERT INTO article_versions(arxiv_id, version, submitted_at)
           VALUES ('2608.09999', 1, '2026-08-01T00:00:00+00:00')"""
    )
    connection.execute(
        """INSERT INTO saved_papers(arxiv_id, saved_version)
           VALUES ('2608.09999', 1)"""
    )
    for queue_revision, (daily_list_date, resolution) in enumerate(
        (
            ("2026-08-18", "atom_confirmed"),
            ("2026-08-19", "chronology_matched"),
            ("2026-08-21", "unconfirmed"),
        ),
        start=1,
    ):
        connection.execute(
            """INSERT INTO canonical_events(
                   arxiv_id, daily_list_date, announced_version,
                   version_resolution, queue_revision
               ) VALUES ('2608.09999', ?, NULL, ?, ?)""",
            (daily_list_date, resolution, queue_revision),
        )
    connection.execute(
        """INSERT INTO download_files(
               arxiv_id, version, filename, byte_count, sha256, last_verified_at
           ) VALUES ('2608.09999', 1, 'secret.pdf', 42, ?, ?)""",
        ("d" * 64, "2026-08-22T12:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    candidate_root = paths.cache_dir / "candidate-corpus"
    candidate_root.mkdir(parents=True)
    (candidate_root / "private-manifest.json").write_text(
        '{"arxiv_id":"2608.09999"}', encoding="utf-8"
    )
    paths.restore_journal_path.write_text(
        '{"private_path":"/private/example"}', encoding="utf-8"
    )

    report = inspect_doctor(
        paths,
        platform="linux",
        today=date(2026, 8, 22),
    )
    rendered = render_doctor(report)

    assert report.initialized is True
    assert report.application_generation == 2
    assert report.schema_version == 4
    assert report.profile_revision == 7
    assert report.projection_revision == 3
    assert report.active_category_count == 1
    assert report.metadata_checkpoint_count == 1
    assert report.daily_list_target_count == 5
    assert report.daily_list_checked_count == 4
    assert report.daily_list_with_papers_count == 1
    assert report.daily_list_empty_count == 1
    assert report.daily_list_failed_count == 2
    assert report.daily_list_pending_count == 1
    assert report.daily_list_unavailable_count == 1
    assert report.daily_list_gap_count == 3
    assert report.canonical_event_count == 3
    assert report.atom_confirmed_count == 1
    assert report.chronology_matched_count == 1
    assert report.unconfirmed_count == 1
    assert report.saved_paper_count == 1
    assert report.downloaded_pdf_count == 1
    assert report.candidate_cache_status == "ready"
    assert report.candidate_cache_file_count == 1
    assert report.maintenance_state == "recovery_pending"
    assert report.sync_error_codes == (
        "catchup_layout_changed",
        "sync_error",
    )
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
        "secret.pdf",
        "private-manifest.json",
        private_error_code,
    ):
        assert secret not in rendered
    assert "Platform:" not in rendered
    assert "destination" not in rendered.casefold()
    assert "preferences" not in rendered.casefold()
