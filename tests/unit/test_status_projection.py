from __future__ import annotations

import json
import threading
from datetime import date
from types import SimpleNamespace

from arxiv_digest.application import _DefaultRuntime
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.web.lifecycle import LifecycleController


def _category(
    category: str,
    *,
    metadata_status: str = "idle",
    metadata_error: str | None = None,
    daily_error: str | None = None,
):
    day = date(2026, 8, 25)
    failed = (day,) if daily_error else ()
    complete = () if daily_error else (day,)
    return SimpleNamespace(
        category=category,
        metadata_sync=SimpleNamespace(
            status=metadata_status,
            completed_through_utc=None,
            last_error_code=metadata_error,
            last_error_message="private upstream detail",
        ),
        target_dates=(day,),
        checked_dates=(day,),
        dates_with_papers=complete,
        empty_dates=(),
        failed_dates=failed,
        pending_dates=(),
        unavailable_dates=(),
        daily_list_status="failed" if failed else "complete",
        daily_list_errors=(
            ((day, daily_error),) if daily_error else ()
        ),
        retryable_failed_exact_dates=failed,
    )


def test_status_keeps_metadata_and_category_daily_list_progress_distinct_and_redacted() -> None:
    runtime = object.__new__(_DefaultRuntime)
    runtime.maintenance = MaintenanceBarrier()
    runtime.lifecycle = LifecycleController()
    runtime._jobs = {}
    runtime._jobs_lock = threading.RLock()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime.profiles = SimpleNamespace(
        load=lambda: SimpleNamespace(categories=("cs.SE", "math.LO"))
    )
    configs = (SimpleNamespace(category="cs.SE"), SimpleNamespace(category="math.LO"))
    runtime._sync_configs = lambda: configs
    categories = (
        _category(
            "cs.SE",
            metadata_status="failed",
            metadata_error="private_metadata_failure",
        ),
        _category("math.LO", daily_error="catchup_layout_changed"),
    )
    report = SimpleNamespace(
        categories=categories,
        offline=False,
        metadata_complete=False,
        target_dates=1,
        checked_dates=1,
        dates_with_papers=1,
        empty_dates=0,
        failed_dates=1,
        pending_dates=0,
        unavailable_dates=0,
        daily_list_status="failed",
    )
    runtime.sync = SimpleNamespace(
        progress=lambda supplied, *, offline=False: report
    )

    result = runtime.status({})

    assert result["metadata_sync"] == {
        "status": "incomplete",
        "categories": [
            {
                "category": "cs.SE",
                "status": "failed",
                "synchronized_through": None,
                "error_codes": ["sync_error"],
            },
            {
                "category": "math.LO",
                "status": "idle",
                "synchronized_through": None,
                "error_codes": [],
            },
        ],
    }
    assert result["daily_list_progress"]["categories"] == [
        {
            "category": "cs.SE",
            "target_dates": 1,
            "checked_dates": 1,
            "dates_with_papers": 1,
            "empty_dates": 0,
            "failed_dates": 0,
            "pending_dates": 0,
            "unavailable_dates": 0,
            "status": "complete",
            "error_codes": [],
        },
        {
            "category": "math.LO",
            "target_dates": 1,
            "checked_dates": 1,
            "dates_with_papers": 0,
            "empty_dates": 0,
            "failed_dates": 1,
            "pending_dates": 0,
            "unavailable_dates": 0,
            "status": "failed",
            "error_codes": ["catchup_layout_changed"],
        },
    ]
    serialized = json.dumps(result)
    assert "private upstream detail" not in serialized
    assert "private_metadata_failure" not in serialized
