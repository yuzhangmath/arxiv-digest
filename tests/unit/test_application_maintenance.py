from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest


def _runtime():
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.web.lifecycle import LifecycleController

    runtime = object.__new__(_DefaultRuntime)
    runtime.maintenance = MaintenanceBarrier()
    runtime.lifecycle = LifecycleController()
    runtime._jobs = {}
    runtime._jobs_lock = threading.RLock()
    runtime._candidate_state_lock = threading.RLock()
    runtime._sync_start_lock = threading.Lock()
    runtime._sync_follow_up_requested = False
    return runtime


def test_runtime_background_job_blocks_exclusive_maintenance_until_terminal() -> None:
    from arxiv_digest.maintenance import WorkActiveError

    runtime = _runtime()
    operation_started = threading.Event()
    release_operation = threading.Event()
    operation_finished = threading.Event()

    def operation() -> str:
        operation_started.set()
        release_operation.wait(2)
        operation_finished.set()
        return "finished"

    job_id = runtime._new_job("download", "download", operation)
    assert operation_started.wait(2)

    try:
        with pytest.raises(WorkActiveError):
            with runtime.maintenance.exclusive(cancel_active=False):
                pass
    finally:
        release_operation.set()

    assert operation_finished.wait(2)
    with runtime.maintenance.exclusive(timeout=2):
        pass
    assert runtime._job_status({"job_id": job_id})["status"] == "completed"


def test_background_job_exposes_initial_fields_when_first_observable() -> None:
    runtime = _runtime()
    operation_started = threading.Event()
    release_operation = threading.Event()

    def operation() -> str:
        operation_started.set()
        assert release_operation.wait(2)
        return "finished"

    job_id = runtime._new_job(
        "sync",
        "sync",
        operation,
        initial_fields={"phase": "enrichment"},
    )
    assert operation_started.wait(2)
    try:
        status = runtime._job_status({"job_id": job_id})
        assert status["status"] == "running"
        assert status["phase"] == "enrichment"
    finally:
        release_operation.set()


def test_candidate_job_keeps_worker_completion_separate_from_corpus_readiness() -> None:
    runtime = _runtime()
    diagnostics = SimpleNamespace(
        complete=False,
        reduced_breadth=False,
        setup_ready=False,
        minimum_met=False,
        pages_fetched=60,
        can_resume=True,
        corpus_hash="a" * 64,
        progress=(),
    )

    job_id = runtime._new_job(
        "setup",
        "sync",
        lambda: SimpleNamespace(diagnostics=diagnostics),
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = runtime._job_status({"job_id": job_id})
        if status["status"] == "completed":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("candidate job did not reach a terminal state")

    assert status["complete"] is True
    assert status["corpus_complete"] is False
    assert status["setup_ready"] is False
    assert status["minimum_met"] is False


def test_repeated_candidate_start_reuses_the_running_job() -> None:
    runtime = _runtime()
    runtime._candidate_job_id = None
    runtime._candidate_job_revision = None
    runtime._candidate_build = None
    runtime._suggestions = {}
    runtime._suggestion_ids = {}
    draft = SimpleNamespace(revision=2)
    runtime.setup = SimpleNamespace(load_draft=lambda: draft)
    runtime._category_configs = lambda _draft: ("synthetic-config",)
    operation_started = threading.Event()
    release_operation = threading.Event()
    calls = []

    class Candidates:
        def retry(self, configs, *, cancelled):
            calls.append(configs)
            operation_started.set()
            assert release_operation.wait(2)
            return "candidate-build"

    runtime.candidates = Candidates()

    first = runtime._start_candidate_corpus(
        {"draft_revision": 2, "mode": "restart"}
    )
    assert operation_started.wait(2)
    second = runtime._start_candidate_corpus(
        {"draft_revision": 2, "mode": "restart"}
    )

    try:
        assert second == first
        assert calls == [("synthetic-config",)]
    finally:
        release_operation.set()


def test_candidate_start_releases_job_map_lock_before_waiting_for_worker() -> None:
    runtime = _runtime()
    runtime._candidate_job_id = None
    runtime._candidate_job_revision = None
    runtime._candidate_build = None
    runtime._suggestions = {}
    runtime._suggestion_ids = {}
    draft = SimpleNamespace(revision=2)
    runtime.setup = SimpleNamespace(load_draft=lambda: draft)
    runtime._category_configs = lambda _draft: ("synthetic-config",)
    runtime.candidates = SimpleNamespace(
        retry=lambda configs, *, cancelled: "candidate-build"
    )
    original_new_job = runtime._new_job

    def checked_new_job(*args, **kwargs):
        assert not runtime._jobs_lock._is_owned()
        return original_new_job(*args, **kwargs)

    runtime._new_job = checked_new_job

    runtime._start_candidate_corpus(
        {"draft_revision": 2, "mode": "restart"}
    )


def test_candidate_accept_rejects_a_running_job() -> None:
    runtime = _runtime()
    job_id = "setup_running1234"
    runtime._candidate_job_id = job_id
    runtime._candidate_job_revision = 2
    runtime._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "complete": False,
        "failed": False,
    }
    runtime.setup = SimpleNamespace(
        load_draft=lambda: SimpleNamespace(revision=2)
    )
    runtime._candidate_build = SimpleNamespace(corpus_hash="a" * 64)

    with pytest.raises(ValueError, match="still running"):
        runtime._accept_candidate_corpus(
            {"draft_revision": 2, "corpus_hash": "a" * 64}
        )


def test_setup_draft_exposes_the_current_candidate_job_for_reload() -> None:
    runtime = _runtime()
    job_id = "setup_reload1234"
    runtime._candidate_job_id = job_id
    runtime._candidate_job_revision = 2
    runtime._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "complete": False,
        "failed": False,
    }
    runtime._candidate_cache_can_resume = lambda _draft: False
    draft = SimpleNamespace(revision=2)

    payload = runtime._setup_payload(draft)

    assert payload.corpus_job == {
        "job_id": job_id,
        "status": "running",
        "complete": False,
        "failed": False,
    }


def test_setup_payload_fallback_issues_finalized_eastern_catchup_bounds() -> None:
    from datetime import date, datetime, timezone

    runtime = _runtime()
    runtime._candidate_cache_can_resume = lambda _draft: False
    runtime.setup = SimpleNamespace(
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    )
    runtime.sync = SimpleNamespace(catchup_window_days=60)

    payload = runtime._setup_payload(SimpleNamespace(revision=2))

    assert payload.coverage_min == date(2026, 6, 24)
    assert payload.coverage_max == date(2026, 8, 21)


def test_setup_payload_uses_sync_services_finalized_coverage_bounds() -> None:
    runtime = _runtime()
    runtime._candidate_cache_can_resume = lambda _draft: False
    runtime.setup = SimpleNamespace(
        clock=lambda: (_ for _ in ()).throw(
            AssertionError("setup must not issue an independent UTC bound")
        )
    )
    runtime.sync = SimpleNamespace(
        coverage_bounds=lambda: (
            date(2026, 5, 25),
            date(2026, 8, 21),
        )
    )

    payload = runtime._setup_payload(SimpleNamespace(revision=2))

    assert payload.coverage_min == date(2026, 5, 25)
    assert payload.coverage_max == date(2026, 8, 21)


def test_setup_coverage_validation_receives_the_server_issued_bounds() -> None:
    runtime = _runtime()
    observed = []
    revised = SimpleNamespace(revision=3)
    runtime._oai = SimpleNamespace(
        identify=lambda: SimpleNamespace(earliest_datestamp=date(2007, 1, 1))
    )
    runtime._supported_coverage_bounds = lambda: (
        date(2026, 5, 25),
        date(2026, 8, 21),
    )
    runtime.setup = SimpleNamespace(
        set_initial_coverage=lambda *args, **kwargs: observed.append(
            (args, kwargs)
        )
        or revised
    )
    runtime._setup_payload = lambda draft: draft

    result = runtime._setup_draft_put(
        {
            "revision": 2,
            "step": "coverage",
            "coverage_start": "2026-08-21",
        }
    )

    assert result is revised
    assert observed == [
        (
            (2, date(2026, 8, 21)),
            {
                "earliest_datestamp": date(2007, 1, 1),
                "coverage_bounds": (
                    date(2026, 5, 25),
                    date(2026, 8, 21),
                ),
            },
        )
    ]


def test_setup_draft_snapshots_candidate_identity_and_job_status_atomically() -> None:
    runtime = _runtime()
    original_job_id = "setup_original1234"
    replacement_job_id = "setup_replacement1234"
    runtime._candidate_job_id = original_job_id
    runtime._candidate_job_revision = 2
    runtime._jobs[original_job_id] = {
        "job_id": original_job_id,
        "status": "completed",
        "complete": True,
        "failed": False,
    }
    runtime._candidate_cache_can_resume = lambda _draft: False
    original_job_status = runtime._job_status
    replacement_acquired_lock: list[bool] = []

    def observed_job_status(payload):
        def replace_candidate() -> None:
            acquired = runtime._candidate_state_lock.acquire(blocking=False)
            replacement_acquired_lock.append(acquired)
            if not acquired:
                return
            try:
                runtime._candidate_job_id = replacement_job_id
            finally:
                runtime._candidate_state_lock.release()

        replacement = threading.Thread(target=replace_candidate)
        replacement.start()
        replacement.join(2)
        assert not replacement.is_alive()
        return original_job_status(payload)

    runtime._job_status = observed_job_status

    payload = runtime._setup_payload(SimpleNamespace(revision=2))

    assert replacement_acquired_lock == [False]
    assert payload.corpus_job == {
        "job_id": original_job_id,
        "status": "completed",
        "complete": True,
        "failed": False,
    }


def test_background_job_admission_bounds_live_daemon_workers() -> None:
    from arxiv_digest.application import _MAX_ACTIVE_BACKGROUND_JOBS

    runtime = _runtime()
    release = threading.Event()
    started = []

    def operation() -> str:
        started.append(threading.current_thread().name)
        assert release.wait(2)
        return "finished"

    job_ids = [
        runtime._new_job("download", "download", operation)
        for _ in range(_MAX_ACTIVE_BACKGROUND_JOBS)
    ]
    try:
        with pytest.raises(ValueError, match="too many active background jobs"):
            runtime._new_job("download", "download", operation)
        assert len(started) == _MAX_ACTIVE_BACKGROUND_JOBS
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(
            runtime._job_status({"job_id": job_id})["status"] == "completed"
            for job_id in job_ids
        ):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("admitted background jobs did not finish")


def test_terminal_job_history_is_pruned_to_a_fixed_retention_bound(
    monkeypatch,
) -> None:
    import arxiv_digest.application as application

    monkeypatch.setattr(application, "_MAX_RETAINED_TERMINAL_JOBS", 3)
    runtime = _runtime()
    job_ids = []
    for value in range(5):
        job_id = runtime._new_job(
            "download", "download", lambda value=value: value
        )
        job_ids.append(job_id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with runtime._jobs_lock:
                status = runtime._jobs.get(job_id, {}).get("status")
            if status == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("background job did not finish")

    with runtime._jobs_lock:
        assert tuple(runtime._jobs) == tuple(job_ids[-3:])


def test_restore_cancellation_stops_download_before_post_fetch_mutation() -> None:
    runtime = _runtime()
    fetch_completed = threading.Event()
    release_without_cancellation = threading.Event()
    job_finished = threading.Event()
    events: list[str] = []

    class Downloads:
        def download(
            self,
            arxiv_id: str,
            version: int,
            *,
            save_first: bool,
            save_version: int | None,
            cancelled=None,
        ) -> str:
            events.append("network-fetched")
            fetch_completed.set()
            while cancelled is None or not cancelled():
                if release_without_cancellation.wait(0.01):
                    events.append("stale-store-mutation")
                    job_finished.set()
                    return "published"
            events.append("cancelled-before-store-mutation")
            job_finished.set()
            return "cancelled"

    runtime.downloads = Downloads()
    started = runtime._start_download(
        {
            "arxiv_id": "2608.00001",
            "version": 1,
            "save_first": False,
        }
    )
    assert fetch_completed.wait(2)

    try:
        with runtime.maintenance.exclusive(cancel_active=True, timeout=0.5):
            assert runtime._job_status(started)["status"] == "completed"
            events.append("restore-published")
    finally:
        release_without_cancellation.set()

    assert job_finished.wait(2)
    assert events == [
        "network-fetched",
        "cancelled-before-store-mutation",
        "restore-published",
    ]


def test_review_date_forwards_the_from_start_override() -> None:
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    calls = []
    page = SimpleNamespace(cards=(), last_finished_revision=None)

    class Review:
        def open_date(self, day, *, anchor_event_id, from_start):
            calls.append((day, anchor_event_id, from_start))
            return page

    runtime.review = Review()
    runtime.store = SimpleNamespace(article_versions=lambda _arxiv_id: ())

    result = runtime._review_date(
        {
            "date": "2026-08-22",
            "anchor_event_id": 19,
            "from_start": True,
        }
    )

    assert result.page is page
    assert calls == [(date(2026, 8, 22), 19, True)]


def test_review_finish_uses_opened_projection_and_returns_next_later_date() -> None:
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    calls = []

    class Review:
        def finish_date(self, day, **values):
            calls.append((day, values))
            return SimpleNamespace(reviewed_count=4, through_revision=19)

        def next_later_unreviewed_date(self, day):
            assert day == date(2026, 8, 22)
            return date(2026, 8, 23)

    runtime.review = Review()
    result = runtime._review_finish(
        {
            "date": "2026-08-22",
            "snapshot_revision": 19,
            "profile_revision": 4,
            "projection_revision": 7,
        }
    )

    assert result == {
        "reviewed_count": 4,
        "through_revision": 19,
        "next_later_unreviewed_date": "2026-08-23",
    }
    assert calls[0][0] == date(2026, 8, 22)
    assert calls[0][1]["through_revision"] == 19
    assert calls[0][1]["profile_revision"] == 4
    assert calls[0][1]["projection_revision"] == 7


def test_restore_cancellation_reaches_active_sync_without_being_cleared() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._sync_cancel.set()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime._sync_configs = lambda: ("synthetic-config",)
    sync_started = threading.Event()
    sync_cancelled = threading.Event()

    class Sync:
        def sync(self, configs, *, daily_list_complete):
            assert configs == ("synthetic-config",)
            assert not runtime._sync_cancel.is_set()
            daily_list_complete()
            sync_started.set()
            assert runtime._sync_cancel.wait(2)
            sync_cancelled.set()
            return "cancelled-report"

    runtime.sync = Sync()
    runtime._start_sync_job({})
    assert sync_started.wait(2)

    with runtime.maintenance.exclusive(cancel_active=True, timeout=0.5):
        assert sync_cancelled.is_set()

    assert runtime._last_sync_report == "cancelled-report"


def test_failed_date_retry_job_reports_completed_dates_while_running() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime.profiles = SimpleNamespace(load=lambda: SimpleNamespace(categories=("cs.SE",)))
    config = SimpleNamespace(category="cs.SE")
    runtime._sync_configs = lambda: (config,)
    retry_dates = ("2026-08-20", "2026-08-21")
    progress = SimpleNamespace(
        categories=(
            SimpleNamespace(
                category="cs.SE",
                retryable_failed_exact_dates=retry_dates,
            ),
        ),
        offline=False,
        target_dates=2,
        checked_dates=0,
        dates_with_papers=0,
        empty_dates=0,
        failed_dates=2,
        pending_dates=0,
        unavailable_dates=0,
        daily_list_status="incomplete",
    )
    observed = []

    class Sync:
        def progress(self, configs, *, offline=False):
            assert configs == (config,)
            assert offline is False
            return progress

        def retry_failed_dates(self, configs, dates, *, attempted):
            assert configs == (config,)
            assert dates == {"cs.SE": retry_dates}
            for mailing_date in retry_dates:
                attempted("cs.SE", mailing_date)
                observed.append(runtime.status({})["daily_list_retry"])
            return progress

    runtime.sync = Sync()
    assert runtime.status({})["daily_list_progress"] == {
        "target_dates": 2,
        "checked_dates": 0,
        "dates_with_papers": 0,
        "empty_dates": 0,
        "failed_dates": 2,
        "pending_dates": 0,
        "unavailable_dates": 0,
        "status": "incomplete",
    }

    def run_inline(_prefix, _kind, operation, *, job_id, **_options):
        runtime._jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "complete": False,
            "failed": False,
        }
        operation()
        return job_id

    runtime._new_job = run_inline

    runtime._start_sync_job({"retry_failed_dates": True})

    assert observed == [
        {"status": "running", "completed": 1, "total": 2},
        {"status": "running", "completed": 2, "total": 2},
    ]


def test_sync_job_publishes_enrichment_phase_when_daily_lists_are_complete() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime._sync_configs = lambda: ("synthetic-config",)
    runtime.sync = SimpleNamespace(
        has_pending_daily_list_work=lambda configs: False,
    )
    captured = {}

    def capture_job(
        _prefix,
        _kind,
        _operation,
        *,
        job_id,
        request_cancel,
        initial_fields,
    ):
        captured.update(
            job_id=job_id,
            request_cancel=request_cancel,
            initial_fields=initial_fields,
        )
        return job_id

    runtime._new_job = capture_job

    result = runtime._start_sync_job({})

    assert result == {"job_id": captured["job_id"]}
    assert captured["initial_fields"] == {"phase": "enrichment"}


def test_sync_pass_moves_from_daily_list_to_enrichment_at_the_boundary() -> None:
    runtime = _runtime()
    job_id = "sync_phase_fixture"
    runtime._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "complete": False,
        "failed": False,
        "phase": "enrichment",
        "daily_list_retry": {"status": "running", "completed": 1, "total": 1},
    }
    runtime._active_sync_job = job_id
    runtime._last_sync_report = None
    runtime._sync_configs = lambda: ("synthetic-config",)
    runtime._retryable_sync_dates = lambda _configs: {}
    observed = []

    class Sync:
        def has_pending_daily_list_work(self, configs):
            assert configs == ("synthetic-config",)
            return True

        def sync(self, configs, *, daily_list_complete):
            assert configs == ("synthetic-config",)
            observed.append(runtime._jobs[job_id]["phase"])
            daily_list_complete()
            observed.append(runtime._jobs[job_id]["phase"])
            assert "daily_list_retry" not in runtime._jobs[job_id]
            return "sync-report"

    runtime.sync = Sync()

    result = runtime._run_sync_pass(job_id, retry_failed_dates=False)

    assert result == "sync-report"
    assert observed == ["daily_list", "enrichment"]


def test_enrichment_phase_does_not_report_a_daily_list_retry_as_running() -> None:
    runtime = _runtime()
    job_id = "sync_enrichment_fixture"
    runtime._jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "complete": False,
        "failed": False,
        "phase": "enrichment",
    }
    runtime._active_sync_job = job_id
    runtime._last_sync_report = None
    runtime.profiles = SimpleNamespace(load=lambda: None)
    runtime.sync = SimpleNamespace()
    retry_day = date(2026, 8, 22)
    runtime._retryable_sync_dates = lambda: {"cs.SE": (retry_day,)}

    result = runtime.status({})

    assert result["sync"]["phase"] == "enrichment"
    assert result["daily_list_retry"] == {
        "status": "idle",
        "completed": 0,
        "total": 1,
    }


def test_sync_start_does_not_block_restore_from_draining_a_cancelled_worker() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime._sync_configs = lambda: ("synthetic-config",)
    runtime.sync = SimpleNamespace(sync=lambda _configs: "sync-report")

    cancel_old_worker = threading.Event()
    old_worker_started = threading.Event()
    old_worker_saw_cancel = threading.Event()
    release_old_worker = threading.Event()

    def old_operation() -> str:
        old_worker_started.set()
        assert cancel_old_worker.wait(2)
        old_worker_saw_cancel.set()
        assert release_old_worker.wait(2)
        return "cancelled"

    runtime._new_job(
        "download",
        "download",
        old_operation,
        request_cancel=cancel_old_worker.set,
    )
    assert old_worker_started.wait(2)

    sync_registration_attempted = threading.Event()
    original_worker = runtime.maintenance.worker

    @contextmanager
    def observed_worker(worker_id, request_cancel):
        if worker_id.startswith("sync_"):
            sync_registration_attempted.set()
        with original_worker(worker_id, request_cancel):
            yield

    runtime.maintenance.worker = observed_worker
    restore_entered = threading.Event()
    release_restore = threading.Event()
    restore_errors: list[Exception] = []

    def restore() -> None:
        try:
            with runtime.maintenance.exclusive(cancel_active=True, timeout=0.5):
                restore_entered.set()
                assert release_restore.wait(2)
        except Exception as error:
            restore_errors.append(error)

    restore_thread = threading.Thread(target=restore)
    restore_thread.start()
    assert old_worker_saw_cancel.wait(2)

    sync_results: list[dict[str, str]] = []
    sync_errors: list[Exception] = []

    def start_sync() -> None:
        try:
            sync_results.append(runtime._start_sync_job({}))
        except Exception as error:
            sync_errors.append(error)

    sync_thread = threading.Thread(target=start_sync)
    sync_thread.start()
    assert sync_registration_attempted.wait(2)

    release_old_worker.set()
    release_restore.set()
    restore_thread.join(2)
    sync_thread.join(2)

    assert not restore_thread.is_alive()
    assert not sync_thread.is_alive()
    assert restore_errors == []
    assert restore_entered.is_set()
    assert sync_errors == []
    assert len(sync_results) == 1


def test_sync_start_rolls_back_its_reservation_when_job_admission_fails() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None

    def reject_job(*_args, **_kwargs):
        raise ValueError("too many active background jobs")

    runtime._new_job = reject_job

    with pytest.raises(ValueError, match="too many active background jobs"):
        runtime._start_sync_job({})

    assert runtime._active_sync_job is None


def test_active_sync_runs_follow_up_with_newly_published_configs() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime._sync_follow_up_requested = False
    selected = {"configs": ("old-config",)}
    runtime._sync_configs = lambda: selected["configs"]
    runtime._retryable_sync_dates = lambda _configs: {}
    first_pass_started = threading.Event()
    release_first_pass = threading.Event()
    calls = []
    phases = []

    class Sync:
        def sync(self, configs, *, daily_list_complete):
            calls.append(configs)
            phases.append(runtime._jobs[runtime._active_sync_job]["phase"])
            daily_list_complete()
            phases.append(runtime._jobs[runtime._active_sync_job]["phase"])
            if len(calls) == 1:
                first_pass_started.set()
                assert release_first_pass.wait(2)
            return f"report-{len(calls)}"

    runtime.sync = Sync()

    first = runtime._start_sync_job({})
    assert first_pass_started.wait(2)
    selected["configs"] = ("new-config",)
    second = runtime._start_sync_job({"follow_up": True})
    release_first_pass.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = runtime._job_status({"job_id": first["job_id"]})
        if status["status"] != "running":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("synchronization follow-up did not finish")

    assert second == first
    assert status["status"] == "completed"
    assert calls == [("old-config",), ("new-config",)]
    assert phases == [
        "daily_list",
        "enrichment",
        "daily_list",
        "enrichment",
    ]
    assert runtime._last_sync_report == "report-2"


def test_finished_sync_cannot_clear_a_new_jobs_follow_up_request() -> None:
    runtime = _runtime()
    old_job_id = "sync_old"
    new_job_id = "sync_new"
    runtime._active_sync_job = old_job_id
    runtime._sync_follow_up_requested = False
    runtime._run_sync_pass = lambda *_args, **_kwargs: "old-report"

    class InterleavingLock:
        def __init__(self) -> None:
            self.interleaved = False

        def __enter__(self):
            return self

        def __exit__(self, *_error) -> None:
            if not self.interleaved and runtime._active_sync_job is None:
                self.interleaved = True
                runtime._active_sync_job = new_job_id
                runtime._sync_follow_up_requested = True

    runtime._jobs_lock = InterleavingLock()

    assert runtime._run_sync(old_job_id, retry_failed_dates=False) == "old-report"
    assert runtime._active_sync_job == new_job_id
    assert runtime._sync_follow_up_requested is True


def test_browser_restore_waits_for_a_paused_candidate_start_and_clears_it(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.backup
    import arxiv_digest.storage.database

    runtime = _runtime()
    runtime.paths = SimpleNamespace(database_path=tmp_path / "state.sqlite3")
    runtime._candidate_job_id = None
    runtime._candidate_job_revision = None
    runtime._candidate_build = None
    runtime._suggestions = {}
    runtime._suggestion_ids = {}

    original_draft = SimpleNamespace(revision=2)
    restored_draft = SimpleNamespace(revision=9)
    draft_state = {"value": original_draft}
    runtime.setup = SimpleNamespace(load_draft=lambda: draft_state["value"])

    configs_entered = threading.Event()
    release_configs = threading.Event()

    def category_configs(draft):
        configs_entered.set()
        assert release_configs.wait(2)
        return (f"revision-{draft.revision}",)

    runtime._category_configs = category_configs
    candidate_started = threading.Event()
    force_candidate_finish = threading.Event()
    events: list[str] = []

    class Candidates:
        def retry(self, configs, *, cancelled):
            assert configs == ("revision-2",)
            events.append("candidate-started")
            candidate_started.set()
            while not cancelled():
                if force_candidate_finish.wait(0.01):
                    events.append("candidate-finished-without-cancel")
                    return "stale-candidate-build"
            events.append("candidate-cancelled")
            return "cancelled-candidate-build"

    runtime.candidates = Candidates()

    archive = tmp_path / "pending.zip"
    archive.write_bytes(b"validated")
    runtime.folder = SimpleNamespace(validate=lambda choice: choice)
    runtime._resolve_folder_choice = lambda _identifier: "destination"
    runtime._picker_choices = {}
    runtime._pending_restores_lock = threading.RLock()
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {
        "restore_abcd1234": (
            time.monotonic(),
            SimpleNamespace(path=archive),
        )
    }
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._expire_pending_restores = lambda: None
    runtime._schedule_pending_restore_expiration_locked = lambda: None

    restore_published = threading.Event()

    def publish_restore(*args, **kwargs):
        assert kwargs["maintenance"] is None
        draft_state["value"] = restored_draft
        events.append("restore-published")
        restore_published.set()
        return SimpleNamespace(profile_revision=3, pre_restore_path=None)

    monkeypatch.setattr(arxiv_digest.backup, "restore_backup", publish_restore)
    monkeypatch.setattr(
        arxiv_digest.storage.database,
        "open_database",
        lambda _path: SimpleNamespace(close=lambda: None),
    )

    start_results: list[dict[str, str]] = []
    start_errors: list[Exception] = []
    restore_errors: list[Exception] = []

    def start_candidate() -> None:
        try:
            start_results.append(
                runtime._start_candidate_corpus(
                    {"draft_revision": 2, "mode": "restart"}
                )
            )
        except Exception as error:
            start_errors.append(error)

    def restore() -> None:
        try:
            runtime._backup_restore(
                {
                    "pending_restore_id": "restore_abcd1234",
                    "destination_choice": "downloads",
                    "cancel_active": True,
                }
            )
        except Exception as error:
            restore_errors.append(error)

    start_thread = threading.Thread(target=start_candidate)
    restore_thread = threading.Thread(target=restore)
    start_thread.start()
    assert configs_entered.wait(2)
    restore_thread.start()

    published_while_start_was_paused = restore_published.wait(0.2)
    release_configs.set()
    assert candidate_started.wait(2)
    start_thread.join(2)
    restore_thread.join(2)
    force_candidate_finish.set()
    deadline = time.monotonic() + 2
    while start_results and time.monotonic() < deadline:
        status = runtime._job_status(start_results[0])["status"]
        if status != "running":
            break
        time.sleep(0.01)

    try:
        assert not start_thread.is_alive()
        assert not restore_thread.is_alive()
        assert start_errors == []
        assert restore_errors == []
        assert published_while_start_was_paused is False
        assert events == [
            "candidate-started",
            "candidate-cancelled",
            "restore-published",
        ]
        assert runtime._candidate_job_id is None
        assert runtime._candidate_job_revision is None
        assert runtime._candidate_build is None
    finally:
        release_configs.set()
        force_candidate_finish.set()


def test_settings_destination_change_recomputes_local_pdf_presence_before_return(
    tmp_path,
) -> None:
    from datetime import date

    from arxiv_digest.models import CategoryConfig
    from arxiv_digest.profile import (
        PdfDestination,
        Profile,
        ProfileCategory,
    )

    runtime = _runtime()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    coverage = ProfileCategory("cs.SE", date(2026, 7, 1))
    profile = Profile(
        schema_version=2,
        revision=3,
        category_coverage=(coverage,),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", first),
    )
    destination = PdfDestination("custom", second)
    events = []
    runtime.profiles = SimpleNamespace(load=lambda: profile)
    runtime._tested_destinations = {
        "destination_abcd1234": (None, destination)
    }
    config = CategoryConfig("cs.SE", "cs:SE", coverage.coverage_start)
    runtime._sync_configs = lambda: (config,)
    runtime.setup = SimpleNamespace(
        publish_profile=lambda revised, configs, expected_revision: events.append(
            (
                "published",
                revised.pdf_destination.path,
                revised.category_coverage,
                configs,
                expected_revision,
            )
        )
    )
    runtime.downloads = SimpleNamespace(
        recompute_presence=lambda path: events.append(("recomputed", path))
    )
    runtime.store = object()
    runtime._profile_value = lambda revised, store: events.append(
        ("projected", revised.pdf_destination.path)
    ) or {"revision": revised.revision}

    result = runtime._settings_folder(
        {
            "expected_revision": 3,
            "tested_destination_token": "destination_abcd1234",
        }
    )

    assert result == {"revision": 4}
    assert events == [
        ("published", second, (coverage,), (config,), 3),
        ("recomputed", second),
        ("projected", second),
    ]


def test_sync_configs_use_active_profile_coverage_not_retained_row_coverage(
    tmp_path,
) -> None:
    from datetime import date

    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    runtime = _runtime()
    profile = Profile(
        schema_version=2,
        revision=3,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", tmp_path),
    )
    runtime.profiles = SimpleNamespace(load=lambda: profile)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda _category: SimpleNamespace(
            set_spec="cs:SE",
            coverage_start=date(2025, 1, 1),
        )
    )

    configs = runtime._sync_configs()

    assert tuple(
        (config.category, config.oai_set_spec, config.coverage_start)
        for config in configs
    ) == (("cs.SE", "cs:SE", date(2026, 7, 1)),)


def test_browser_restore_closes_bootstrap_sqlite_and_reopens_before_release(
    tmp_path,
    monkeypatch,
) -> None:
    import arxiv_digest.backup
    import arxiv_digest.storage.database
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier, MaintenanceError
    from arxiv_digest.paths import resolve_paths
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.web.lifecycle import LifecycleController

    paths = resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "state"),
        },
    )
    paths.ensure()
    maintenance = MaintenanceBarrier()
    maintenance_requests = []
    original_exclusive = maintenance.exclusive

    @contextmanager
    def tracking_exclusive(**options):
        maintenance_requests.append(options)
        with original_exclusive(**options):
            yield

    monkeypatch.setattr(maintenance, "exclusive", tracking_exclusive)
    profiles = ProfileRepository(
        paths.profile_path,
        paths.profile_lock_path,
        maintenance=maintenance,
    )
    runtime = _DefaultRuntime(
        paths,
        profiles,
        maintenance,
        LifecycleController(),
        output=lambda _message: None,
    )
    real_open_database = arxiv_digest.storage.database.open_database
    opened_connections = []

    def tracking_open_database(path):
        if opened_connections:
            with pytest.raises(MaintenanceError, match="not reentrant"):
                with maintenance.exclusive():
                    pass
        connection = real_open_database(path)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(
        arxiv_digest.storage.database,
        "open_database",
        tracking_open_database,
    )
    database_handle = runtime.open_database()
    held_connection = opened_connections[0]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        held_connection.execute("SELECT 1")
    archive = tmp_path / "pending.zip"
    archive.write_bytes(b"validated")
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {
        "restore_abcd1234": (
            time.monotonic(),
            SimpleNamespace(path=archive),
        )
    }
    runtime._resolve_folder_choice = lambda _identifier: "destination"
    runtime.folder = SimpleNamespace(validate=lambda choice: choice)

    def restore(*args, **kwargs):
        assert kwargs["maintenance"] is None
        with pytest.raises(MaintenanceError, match="not reentrant"):
            with maintenance.exclusive():
                pass
        return SimpleNamespace(profile_revision=2, pre_restore_path=None)

    monkeypatch.setattr(arxiv_digest.backup, "restore_backup", restore)

    result = runtime._backup_restore(
        {
            "pending_restore_id": "restore_abcd1234",
            "destination_choice": "downloads",
            "cancel_active": False,
        }
    )

    assert result == {
        "profile_revision": 2,
        "pre_restore_backup_created": False,
    }
    assert maintenance_requests[0] == {
        "cancel_active": False,
        "timeout": 45.0,
    }
    assert len(opened_connections) == 2
    validated_connection = opened_connections[1]
    assert validated_connection is not held_connection
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        validated_connection.execute("SELECT 1")

    database_handle.close()


def test_failed_restore_keeps_quota_reserved_against_concurrent_inspection(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.application as application
    import arxiv_digest.backup
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier

    runtime = object.__new__(_DefaultRuntime)
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)
    runtime.maintenance = MaintenanceBarrier()
    runtime._candidate_state_lock = threading.RLock()
    runtime.folder = SimpleNamespace(validate=lambda choice: choice)
    runtime._resolve_folder_choice = lambda _identifier: "destination"
    runtime._picker_choices = {}
    runtime._pending_restores_lock = threading.RLock()
    runtime._pending_restore_reservations = {}
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    runtime._pending_restores = {
        "restore_first": (
            time.monotonic(),
            SimpleNamespace(path=first),
        ),
        "restore_second": (
            time.monotonic(),
            SimpleNamespace(path=second),
        ),
    }
    restore_started = threading.Event()
    release_restore = threading.Event()
    restore_errors: list[Exception] = []
    inspect_calls: list[object] = []

    def fail_restore(*args, **kwargs):
        restore_started.set()
        assert release_restore.wait(2)
        raise ValueError("restore failed safely")

    def inspect(path):
        inspect_calls.append(path)
        return SimpleNamespace(
            path=path,
            manifest=SimpleNamespace(
                format_version=2,
                application_version="0.2.0",
                created_at=SimpleNamespace(
                    isoformat=lambda: "2026-08-22T12:00:00+00:00"
                ),
            ),
            profile=SimpleNamespace(revision=1, categories=()),
            records=(),
        )

    monkeypatch.setattr(arxiv_digest.backup, "restore_backup", fail_restore)
    monkeypatch.setattr(arxiv_digest.backup, "inspect_backup", inspect)
    monkeypatch.setattr(application, "_MAX_PENDING_RESTORES", 2)

    def restore() -> None:
        try:
            runtime._backup_restore(
                {
                    "pending_restore_id": "restore_first",
                    "destination_choice": "downloads",
                    "cancel_active": False,
                }
            )
        except Exception as error:
            restore_errors.append(error)

    thread = threading.Thread(target=restore)
    thread.start()
    assert restore_started.wait(2)
    try:
        with pytest.raises(
            ValueError, match="too many pending restore inspections"
        ):
            runtime._backup_inspect({"archive": b"third"})
    finally:
        release_restore.set()
        thread.join(2)

    assert not thread.is_alive()
    assert len(restore_errors) == 1
    assert str(restore_errors[0]) == "restore failed safely"
    assert inspect_calls == []
    assert set(runtime._pending_restores) == {
        "restore_first",
        "restore_second",
    }
    runtime._close_runtime()
