from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from arxiv_digest.application import _DefaultRuntime
from arxiv_digest.models import EnrichmentStatus
from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import EnrichmentDayRecord, Store
from arxiv_digest.web.api import ApiRequest, ApiRouter


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
TOKEN = "A" * 43
HOST = "127.0.0.1:43123"


def test_settings_separates_redacted_persisted_status_aggregates(
    tmp_path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.ensure_category_state("cs.SE", "cs:SE", date(2026, 8, 1))
    incremental_run = store.begin_sync_run(
        "cs.SE", "incremental", date(2026, 8, 1), None, NOW
    )
    store.complete_incremental_run(
        incremental_run,
        date(2026, 8, 20),
        datetime(2026, 8, 20, 2, tzinfo=timezone.utc),
        NOW,
    )
    for mailing_date, status in (
        (date(2026, 5, 20), EnrichmentStatus.FAILED),
        (date(2026, 8, 18), EnrichmentStatus.EMPTY),
        (date(2026, 8, 19), EnrichmentStatus.FAILED),
        (date(2026, 8, 21), EnrichmentStatus.COMPLETE),
    ):
        failed = status is EnrichmentStatus.FAILED
        store.record_enrichment_day(
            EnrichmentDayRecord(
                category="cs.SE",
                mailing_date=mailing_date,
                source="catchup",
                status=status,
                fetched_at=NOW,
                error_code="catchup_layout_changed" if failed else None,
                error_message=(
                    "The catch-up fixture did not complete." if failed else None
                ),
            )
        )
    store.ensure_catchup_targets("cs.SE", (date(2026, 8, 20),))
    connection = store._connect()
    connection.execute(
        """INSERT INTO articles(
               arxiv_id, title, abstract, primary_category, metadata_hash
           ) VALUES ('2608.00001', 'secret title', 'secret abstract',
                     'cs.SE', ?)""",
        ("a" * 64,),
    )
    connection.execute(
        """INSERT INTO article_versions(arxiv_id, version, submitted_at)
           VALUES ('2608.00001', 1, '2026-08-01T00:00:00+00:00')"""
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
               ) VALUES ('2608.00001', ?, NULL, ?, ?)""",
            (daily_list_date, resolution, queue_revision),
        )
    connection.execute(
        "INSERT INTO saved_papers(arxiv_id, saved_version) VALUES ('2608.00001', 1)"
    )
    connection.execute(
        """INSERT INTO download_files(
               arxiv_id, version, filename, byte_count, sha256, last_verified_at
           ) VALUES ('2608.00001', 1, 'private.pdf', 42, ?, ?)""",
        ("b" * 64, NOW.isoformat()),
    )
    connection.commit()
    connection.close()

    destination = tmp_path / "Research PDFs"
    profile = Profile(
        schema_version=2,
        revision=7,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 5, 20)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("documents", destination),
    )
    profiles = SimpleNamespace(load=lambda: profile)
    runtime = _DefaultRuntime(
        SimpleNamespace(),
        profiles,
        SimpleNamespace(),
        SimpleNamespace(),
        output=lambda _message: None,
    )
    runtime.store = store
    runtime.sync = SimpleNamespace(catchup_window_days=90)
    runtime._active_sync_job = "sync_status_fixture"
    runtime._jobs["sync_status_fixture"] = {
        "job_id": "sync_status_fixture",
        "status": "running",
        "complete": False,
        "failed": False,
        "daily_list_retry": {
            "status": "running",
            "completed": 0,
            "total": 1,
        },
    }
    runtime.setup = SimpleNamespace(
        clock=lambda: NOW,
        launcher_settings=lambda: SimpleNamespace(
            operation="none", error_code=None, retry_available=False
        )
    )
    cache_root = tmp_path / "candidate-cache"
    cache_root.mkdir()
    (cache_root / "manifest.json").write_text("{}", encoding="utf-8")
    runtime.candidates = SimpleNamespace(cache=SimpleNamespace(root=cache_root))
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"settings_get": runtime._settings_get},
    )

    response = router.dispatch(
        ApiRequest(
            method="GET",
            target="/api/v1/settings",
            headers={"Host": HOST, "Authorization": f"Bearer {TOKEN}"},
        )
    )

    assert response.status == 200
    data = json.loads(response.body)["data"]
    assert data["synchronizing"] is True
    assert data["daily_list_retry"] == {
        "status": "running",
        "completed": 0,
        "total": 1,
    }
    assert data["coverage_min"] == "2026-05-25"
    assert data["coverage_max"] == "2026-08-21"
    assert data["metadata_sync"] == {
        "checkpoint_count": 1,
        "categories": [{
            "category": "cs.SE",
            "status": "idle",
            "synchronized_through": "2026-08-20",
            "error_codes": [],
        }],
    }
    assert data["daily_list_coverage"] == {
        "target": 5,
        "checked": 4,
        "with_papers": 1,
        "empty": 1,
        "failed": 2,
        "pending": 1,
        "unavailable": 1,
        "gaps": 3,
        "categories": [{
            "category": "cs.SE",
            "coverage_start": "2026-05-20",
            "target": 5,
            "checked": 4,
            "with_papers": 1,
            "empty": 1,
            "failed": 2,
            "pending": 1,
            "unavailable": 1,
            "gaps": 3,
            "error_codes": ["catchup_layout_changed"],
            "retryable_failed_dates": ["2026-08-19"],
        }],
    }
    assert data["version_resolution"] == {
        "canonical_event_count": 3,
        "atom_confirmed": 1,
        "chronology_matched": 1,
        "unconfirmed": 1,
    }
    assert data["candidate_cache"] == {"status": "ready", "file_count": 1}
    assert data["library"] == {"saved_paper_count": 1}
    assert data["pdf_presence"] == {"downloaded_pdf_count": 1}
    serialized = json.dumps(data)
    for forbidden in (
        "historical_backfill",
        "error_message",
        "secret",
        "2608.00001",
        "private.pdf",
        "paper_date_sources",
    ):
        assert forbidden not in serialized


def test_settings_reads_terminal_sync_status_before_storage_snapshot(
    tmp_path,
) -> None:
    calls: list[str] = []
    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    store = Store(database_path)
    store.ensure_category_state("math.AT", "math:AT", date(2026, 8, 1))
    original_state = store.category_sync_state
    store.category_sync_state = lambda category: (
        calls.append("state") or original_state(category)
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(
        load=lambda: Profile(
            schema_version=2,
            revision=1,
            category_coverage=(
                ProfileCategory("math.AT", date(2026, 8, 1)),
            ),
            keywords=(),
            phrases=(),
            authors=(),
            seed_papers=(),
            pdf_destination=PdfDestination("documents", tmp_path),
        )
    )
    runtime.status = lambda _payload: calls.append("status") or {
        "sync": None,
        "offline": False,
    }
    runtime.sync = SimpleNamespace(catchup_window_days=90)
    runtime.store = store
    runtime.setup = SimpleNamespace(
        clock=lambda: NOW,
        launcher_settings=lambda: SimpleNamespace(
            operation="none", error_code=None, retry_available=False
        )
    )
    runtime.launcher = None
    runtime.candidates = SimpleNamespace(
        cache=SimpleNamespace(root=tmp_path / "missing-cache")
    )

    runtime._settings_get({})

    assert calls[:2] == ["status", "state"]


def test_settings_coverage_publishes_revised_profile_and_starts_sync(
    tmp_path,
) -> None:
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    current = Profile(
        schema_version=2,
        revision=7,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 8, 1)),
            ProfileCategory("cs.LG", date(2026, 7, 15)),
        ),
        keywords=("verification",),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("documents", tmp_path),
    )
    states = {
        "cs.SE": SimpleNamespace(set_spec="cs:SE"),
        "cs.LG": SimpleNamespace(set_spec="cs:LG"),
    }
    publications = []
    sync_starts = []
    calls = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: current)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: states[category]
    )
    runtime.setup = SimpleNamespace(
        clock=lambda: NOW,
        publish_profile=lambda profile, configs, **options: (
            calls.append("publish"),
            publications.append((profile, configs, options)),
        )[-1],
    )
    runtime.sync = SimpleNamespace(
        catchup_window_days=90,
        extend_coverage=lambda category, new_start: calls.append(
            ("extend", category, new_start)
        ),
    )
    runtime._start_sync_job = lambda payload: (
        calls.append("sync"),
        sync_starts.append(payload),
        {"job_id": "sync_existing"},
    )[-1]

    with __import__("pytest").raises(ValueError, match="recovery window"):
        runtime._settings_coverage(
            {
                "category": "cs.SE",
                "new_start": "2026-05-24",
                "expected_revision": 7,
            }
        )
    assert publications == []
    assert sync_starts == []

    result = runtime._settings_coverage(
        {
            "category": "cs.SE",
            "new_start": "2026-07-01",
            "expected_revision": 7,
        }
    )

    assert result == {
        "revision": 8,
        "category": "cs.SE",
        "coverage_start": "2026-07-01",
        "sync_job_id": "sync_existing",
    }
    assert calls == [
        ("extend", "cs.SE", date(2026, 7, 1)),
        "publish",
        "sync",
    ]
    assert sync_starts == [{"follow_up": True}]
    profile, configs, options = publications[0]
    assert profile == Profile(
        schema_version=2,
        revision=8,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
            ProfileCategory("cs.LG", date(2026, 7, 15)),
        ),
        keywords=current.keywords,
        phrases=current.phrases,
        authors=current.authors,
        seed_papers=current.seed_papers,
        pdf_destination=current.pdf_destination,
    )
    assert tuple(
        (item.category, item.oai_set_spec, item.coverage_start)
        for item in configs
    ) == (
        ("cs.SE", "cs:SE", date(2026, 7, 1)),
        ("cs.LG", "cs:LG", date(2026, 7, 15)),
    )
    assert options == {"expected_revision": 7}
