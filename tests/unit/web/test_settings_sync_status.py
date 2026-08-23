from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from arxiv_digest.application import _DefaultRuntime
from arxiv_digest.models import EnrichmentStatus
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import EnrichmentDayRecord, Store
from arxiv_digest.sync import SyncService
from arxiv_digest.web.api import ApiRequest, ApiRouter


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
TOKEN = "A" * 43
HOST = "127.0.0.1:43123"


def test_settings_reports_persisted_metadata_backfill_and_exact_status(
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
    store.set_pending_backfill(
        "cs.SE", date(2026, 7, 1), date(2026, 8, 1)
    )
    backfill_run = store.begin_sync_run(
        "cs.SE",
        "coverage_backfill",
        date(2026, 7, 1),
        date(2026, 7, 31),
        NOW,
    )
    store.fail_sync_run(
        backfill_run,
        "historical_fixture_failed",
        "The historical fixture did not complete.",
        NOW,
    )
    for mailing_date, status in (
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
                error_code="catchup_fixture_failed" if failed else None,
                error_message=(
                    "The catch-up fixture did not complete." if failed else None
                ),
            )
        )

    profile = SimpleNamespace(
        revision=7,
        categories=("cs.SE",),
        pdf_destination=SimpleNamespace(kind="documents"),
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
    runtime.sync = SyncService(store, object(), object(), object())
    runtime.setup = SimpleNamespace(
        launcher_settings=lambda: SimpleNamespace(
            operation="none", error_code=None, retry_available=False
        )
    )
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
    category = json.loads(response.body)["data"]["categories"][0]
    assert category["metadata_synchronized_through"] == "2026-08-20"
    assert category["current_sync"] == {
        "status": "idle",
        "error_code": None,
        "error_message": None,
    }
    assert category["historical_backfill"] == {
        "status": "failed",
        "start": "2026-07-01",
        "until": "2026-07-31",
        "error_code": "historical_fixture_failed",
        "error_message": "The historical fixture did not complete.",
    }
    assert category["exact_enrichment"] == {
        "start": "2026-08-18",
        "end": "2026-08-21",
        "holes": ["2026-08-19", "2026-08-20"],
    }
