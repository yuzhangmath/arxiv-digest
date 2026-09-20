from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import threading

import pytest

from arxiv_digest.application import _DefaultRuntime


def runtime():
    value = object.__new__(_DefaultRuntime)
    value._jobs_lock = threading.RLock()
    value._sync_start_lock = threading.Lock()
    value._sync_cancel = threading.Event()
    value._active_sync_job = "sync_fixture"
    value._sync_follow_up_requested = False
    value._jobs = {"sync_fixture": {"status": "running"}}
    value._last_sync_report = None
    configs = (SimpleNamespace(category="cs.CL"), SimpleNamespace(category="cs.SE"))
    value._sync_configs = lambda: configs
    value._retryable_sync_dates = lambda _configs=None: {
        "cs.CL": (date(2026, 8, 20), date(2026, 8, 21)),
        "cs.SE": (date(2026, 8, 20),),
    }
    return value


def test_scoped_retry_calls_only_the_selected_category_and_date():
    value = runtime()
    observed = []

    def retry(configs, dates, *, attempted):
        observed.append((tuple(c.category for c in configs), dates))
        attempted("cs.CL", date(2026, 8, 20))
        return "report"

    value.sync = SimpleNamespace(retry_failed_dates=retry)
    result = value._run_sync_pass(
        "sync_fixture", retry_failed_dates=True,
        retry_target=("cs.CL", date(2026, 8, 20)),
    )
    assert result == "report"
    assert observed == [(('cs.CL',), {"cs.CL": (date(2026, 8, 20),)})]
    assert value._jobs["sync_fixture"]["daily_list_retry"] == {
        "status": "running", "completed": 1, "total": 1,
    }


@pytest.mark.parametrize("payload", [
    {"retry_date": "2026-08-20"},
    {"retry_category": "cs.CL"},
    {"retry_category": "cs.CL", "retry_date": "2026-08-20"},
    {"retry_failed_dates": True, "retry_category": "cs.CL", "retry_date": "2026-08-19"},
    {"retry_failed_dates": True, "retry_category": "cs.AI", "retry_date": "2026-08-20"},
    {"retry_failed_dates": True, "retry_missing_abstracts": True,
     "retry_category": "cs.CL", "retry_date": "2026-08-20"},
])
def test_scoped_retry_rejects_invalid_or_unavailable_targets(payload):
    value = runtime()
    with pytest.raises(ValueError):
        value._start_sync_job(payload)


def test_scoped_retry_does_not_expand_into_a_follow_up_sync():
    value = runtime()
    value._sync_follow_up_requested = True
    calls = []
    value._run_sync_pass = lambda *args, **kwargs: calls.append(kwargs) or "report"
    result = value._run_sync(
        "sync_fixture", retry_failed_dates=True,
        retry_target=("cs.CL", date(2026, 8, 20)),
    )
    assert result == "report"
    assert len(calls) == 1


def test_date_abstract_retry_retains_recovery_outcome_after_job_finishes():
    value = runtime()
    day = date(2026, 8, 20)
    value.profiles = SimpleNamespace(load=lambda: None)
    value.store = SimpleNamespace(
        unreviewed_papers_missing_abstracts=lambda **kwargs: (
            "2608.00001", "2608.00002", "2608.00003",
        ),
        events_for_date=lambda selected, **kwargs: (
            SimpleNamespace(arxiv_id="2608.00001"),
            SimpleNamespace(arxiv_id="2608.00002"),
        ),
    )
    outcome = SimpleNamespace(
        attempted=2, total=2, recovered=1, remaining=1,
        error_codes=("arxiv_http_406",),
    )
    report = SimpleNamespace(offline=False, abstract_retry=outcome)

    def retry(configs, *, day, attempted):
        assert day == date(2026, 8, 20)
        attempted("2608.00001")
        progress = value.status({})["abstract_retry"]
        assert progress["completed"] == 1
        assert progress["total"] == 2
        assert progress["retry_date"] == day.isoformat()
        attempted("2608.00002")
        return report

    value.sync = SimpleNamespace(retry_missing_abstracts=retry)
    assert value._run_abstract_retry("sync_fixture", day=day) is report
    value._active_sync_job = None
    expected = {
        "status": "completed", "completed": 2, "attempted": 2, "total": 2,
        "recovered": 1, "remaining": 1, "error_codes": ["arxiv_http_406"],
        "retry_date": "2026-08-20",
    }
    assert value.status({})["abstract_retry"] == expected
    # Polling must not erase the result after the worker is gone.
    assert value.status({})["abstract_retry"] == expected


def test_scoped_abstract_retry_accepts_confirmed_date_without_category():
    value = runtime()
    value._active_sync_job = None
    value.store = SimpleNamespace(
        events_for_date=lambda selected, **kwargs: (SimpleNamespace(arxiv_id="2608.00001"),),
    )
    captured = []
    value._run_sync = lambda *args, **kwargs: captured.append(kwargs)

    def run_inline(_prefix, _kind, operation, *, job_id, **kwargs):
        operation()
        return job_id

    value._new_job = run_inline
    value._start_sync_job({"retry_missing_abstracts": True, "retry_date": "2026-08-20"})
    assert captured == [{
        "retry_failed_dates": False, "retry_missing_abstracts": True,
        "abstract_retry_date": date(2026, 8, 20),
    }]


def test_date_abstract_retry_cannot_target_unconfirmed_date():
    value = runtime()
    value.store = SimpleNamespace(events_for_date=lambda *args, **kwargs: ())
    with pytest.raises(ValueError, match="confirmed"):
        value._start_sync_job({"retry_missing_abstracts": True, "retry_date": "2026-08-20"})


def test_cancelled_date_abstract_retry_keeps_partial_recovery_visible():
    from arxiv_digest.sync import SyncCancelled

    value = runtime()
    value.profiles = SimpleNamespace(load=lambda: None)
    remaining = {"2608.00001", "2608.00002"}
    value.store = SimpleNamespace(
        unreviewed_papers_missing_abstracts=lambda **kwargs: tuple(sorted(remaining)),
        events_for_date=lambda *args, **kwargs: (
            SimpleNamespace(arxiv_id="2608.00001"),
            SimpleNamespace(arxiv_id="2608.00002"),
        ),
    )

    def retry(configs, *, day, attempted):
        remaining.remove("2608.00001")
        attempted("2608.00001")
        raise SyncCancelled("synthetic cancellation")

    value.sync = SimpleNamespace(retry_missing_abstracts=retry)
    with pytest.raises(SyncCancelled):
        value._run_sync(
            "sync_fixture", retry_failed_dates=False, retry_missing_abstracts=True,
            abstract_retry_date=date(2026, 8, 20),
        )
    assert value.status({})["abstract_retry"] == {
        "status": "interrupted", "completed": 1, "attempted": 1, "total": 2,
        "recovered": 1, "remaining": 1, "error_codes": ["cancelled"],
        "retry_date": "2026-08-20",
    }


def test_cooldown_status_is_safe_and_does_not_start_a_request():
    value = runtime()
    retry_at = datetime(2026, 8, 22, 13, tzinfo=timezone.utc)
    value.arxiv_cooldown = SimpleNamespace(active=lambda: SimpleNamespace(
        retry_at=retry_at, http_status=429,
        safe_message="arXiv requests are paused after rate limiting.",
    ))
    assert value._arxiv_access_status() == {
        "paused": True, "retry_at": "2026-08-22T13:00:00+00:00",
        "http_status": 429,
        "message": "arXiv requests are paused after rate limiting.",
    }


def test_startup_skips_sync_while_cooldown_is_active():
    value = runtime()
    value.sync = object()
    value.profiles = SimpleNamespace(load=lambda: object())
    value.arxiv_cooldown = SimpleNamespace(active=lambda: RuntimeError("paused"))
    calls = []
    value._start_sync_job = lambda payload: calls.append(payload)
    value.start_sync()
    assert calls == []


def test_cooldown_between_admission_and_worker_does_not_run_sync():
    value = runtime()
    value.arxiv_cooldown = SimpleNamespace(active=lambda: RuntimeError("paused"))
    value.sync = SimpleNamespace(progress=lambda configs: "unchanged-report")
    value._run_sync_pass = lambda *args, **kwargs: pytest.fail("sync started while paused")
    assert value._run_sync("sync_fixture", retry_failed_dates=False) == "unchanged-report"


def test_clearing_cache_does_not_cancel_saved_cooldown(tmp_path):
    from arxiv_digest.arxiv_access import ArxivCooldown

    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    path = tmp_path / "data" / "arxiv-cooldown.json"
    cooldown = ArxivCooldown(path, wall_clock=lambda: now)
    cooldown.record(retry_after=None, http_status=429)
    value = runtime()
    value.paths = SimpleNamespace(cache_dir=tmp_path / "cache")
    cache = value.paths.cache_dir / "candidates"
    cache.mkdir(parents=True)
    (cache / "manifest.json").write_text("{}")
    value.candidates = SimpleNamespace(cache=SimpleNamespace(root=cache))
    value._suggestions = {}
    value._suggestion_ids = {}
    assert value._settings_cache_clear({}) == {"cleared": True}
    restarted = ArxivCooldown(path, wall_clock=lambda: now)
    assert restarted.active().http_status == 429
    assert restarted.active().retry_at == datetime(2026, 8, 22, 13, tzinfo=timezone.utc)


def test_unreadable_cooldown_remains_visible_without_breaking_local_status(tmp_path):
    from arxiv_digest.arxiv_access import ArxivCooldown

    path = tmp_path / "arxiv-cooldown.json"
    path.write_text("invalid")
    path.chmod(0o600)
    value = runtime()
    value.arxiv_cooldown = ArxivCooldown(path)
    status = value._arxiv_access_status()
    assert status["paused"] is True
    assert status["retry_at"] is None
    assert status["http_status"] is None
    assert "could not be read or written safely" in status["message"]
