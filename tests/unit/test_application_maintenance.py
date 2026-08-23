from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
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
        def sync(self, configs):
            assert configs == ("synthetic-config",)
            assert not runtime._sync_cancel.is_set()
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


def test_settings_destination_change_recomputes_local_pdf_presence_before_return(
    tmp_path,
) -> None:
    from arxiv_digest.profile import PdfDestination, Profile

    runtime = _runtime()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    profile = Profile(
        schema_version=1,
        revision=3,
        categories=("cs.SE",),
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
    runtime._sync_configs = lambda: ("config",)
    runtime.setup = SimpleNamespace(
        publish_profile=lambda revised, configs, expected_revision: events.append(
            ("published", revised.pdf_destination.path, configs, expected_revision)
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
        ("published", second, ("config",), 3),
        ("recomputed", second),
        ("projected", second),
    ]


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
                format_version=1,
                application_version="0.1.0",
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
