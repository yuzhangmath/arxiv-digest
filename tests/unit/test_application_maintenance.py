from __future__ import annotations

import os
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
    runtime._sync_start_lock = threading.Lock()
    runtime._sync_follow_up_requested = False
    return runtime


def test_runtime_permission_failure_removes_temporary_file(tmp_path, monkeypatch):
    import arxiv_digest.application as application

    runtime = _runtime()
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)
    opened = []

    def fail(descriptor, mode):
        opened.append(descriptor)
        raise PermissionError("private permissions unavailable")

    monkeypatch.setattr(application, "set_private_file_permissions", fail)
    with pytest.raises(PermissionError, match="unavailable"):
        runtime._private_temp(".zip")

    assert list(tmp_path.iterdir()) == []
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


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
                "coverage_bounds": (
                    date(2026, 5, 25),
                    date(2026, 8, 21),
                ),
            },
        )
    ]


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
    runtime._sync_configs = lambda _profile=None: (config,)
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


def test_missing_abstract_retry_job_reports_progress_without_daily_list_retry() -> None:
    runtime = _runtime()
    runtime._sync_cancel = threading.Event()
    runtime._active_sync_job = None
    runtime._last_sync_report = None
    runtime.profiles = SimpleNamespace(load=lambda: None)
    configs = ("synthetic-config",)
    runtime._sync_configs = lambda: configs
    identifiers = ("2608.00001", "2608.00002")
    runtime.store = SimpleNamespace(
        unreviewed_papers_missing_abstracts=lambda *, active_configs: identifiers,
    )
    observed = []
    report = SimpleNamespace(offline=False)

    class Sync:
        def has_pending_daily_list_work(self, configs):
            return True

        def retry_missing_abstracts(self, selected, *, attempted):
            assert selected == configs
            for arxiv_id in identifiers:
                attempted(arxiv_id)
                status = runtime.status({})
                assert status["sync"]["phase"] == "enrichment"
                assert status["daily_list_retry"]["status"] == "idle"
                observed.append(status["abstract_retry"])
            return report

    runtime.sync = Sync()

    def run_inline(_prefix, _kind, operation, *, job_id, initial_fields, **_options):
        assert initial_fields["phase"] == "enrichment"
        runtime._jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            **initial_fields,
        }
        operation()
        return job_id

    runtime._new_job = run_inline
    runtime._start_sync_job({"retry_missing_abstracts": True})

    assert observed == [
        {"status": "running", "completed": 1, "total": 2},
        {"status": "running", "completed": 2, "total": 2},
    ]
    assert runtime._last_sync_report is report
    assert runtime.status({})["abstract_retry"]["status"] == "idle"


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
    original_worker = runtime.maintenance.reserve_worker

    def observed_worker(worker_id, request_cancel):
        if worker_id.startswith("sync_"):
            sync_registration_attempted.set()
        return original_worker(worker_id, request_cancel)

    runtime.maintenance.reserve_worker = observed_worker
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


def test_worker_registration_precedes_thread_start_and_unwinds_start_failure(monkeypatch) -> None:
    import arxiv_digest.application as application

    runtime = _runtime()
    def fail_start(_thread):
        assert runtime.maintenance.work_active
        raise KeyboardInterrupt
    monkeypatch.setattr(application.threading.Thread, "start", fail_start)
    with pytest.raises(KeyboardInterrupt):
        runtime._new_job("download", "download", lambda: None)
    assert not runtime.maintenance.work_active
    assert not runtime.lifecycle._has_workers()
    assert all(job["status"] != "running" for job in runtime._jobs.values())
