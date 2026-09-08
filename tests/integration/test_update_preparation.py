from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path

from arxiv_digest.application import _DefaultRuntime
from arxiv_digest.desktop_launcher import DesktopLauncherManager, launcher_operation_guard
from arxiv_digest.profile import ProfileRepository
from arxiv_digest.update_coordinator import UpdateCoordinator
from arxiv_digest.update_discovery import UpdateDescriptor
from arxiv_digest.update_locks import acquire_exclusive
from arxiv_digest.update_runtime import protocol
from arxiv_digest.web.lifecycle import LifecycleController
from tests.unit.test_backup import initialized_paths
from tests.unit.test_update_discovery import _eligible_bundle
from tests.unit.test_update_download import Response
from tests.unit.test_update_installation import _detect
from tests.update_installation_factory import synthetic_pipx_installation
from tests.update_runtime_factory import private_test_interpreter
from tests.update_wheel_factory import write_valid_wheel


def test_production_first_preparation_and_copied_helper_cancel_preserve_data(tmp_path, monkeypatch):
    import arxiv_digest.update_download as download
    fixture = synthetic_pipx_installation(tmp_path / "installation")
    fixture.base_interpreter.unlink()
    fixture.base_interpreter.symlink_to(private_test_interpreter(tmp_path / "fixture-interpreter"))
    fixture.paths = initialized_paths(tmp_path / "application")
    detected = _detect(fixture)
    assert detected.installation is not None, detected.reason
    (tmp_path / "release").mkdir()
    installed, target, _ = _eligible_bundle(tmp_path / "release")
    descriptor = UpdateDescriptor(installed, target, detected.installation)
    wheel = write_valid_wheel(tmp_path / target.wheel_asset.name, version=target.version).read_bytes()
    real_download = download.download_target_wheel
    monkeypatch.setattr(download, "download_target_wheel", lambda descriptor, directory, **kwargs:
        real_download(descriptor, directory, **kwargs, open_url=lambda request, **kw: Response(wheel, request.full_url)))
    runtime = _DefaultRuntime(fixture.paths,
        ProfileRepository(fixture.paths.profile_path, fixture.paths.profile_lock_path, maintenance=fixture.maintenance),
        fixture.maintenance, LifecycleController(), output=lambda message: None)
    manager = DesktopLauncherManager(platform=sys.platform, home=Path(fixture.environ["HOME"]),
        executable=fixture.exposed_command, recovery_wrapper=fixture.paths.recovery_wrapper_path,
        operation_guard=lambda: launcher_operation_guard(fixture.paths))
    monkeypatch.setattr(runtime, "_launcher_manager", lambda: manager)
    fixture.paths.ensure_update_coordination()
    ordinary = acquire_exclusive(fixture.paths.process_lock_path, timeout=0)
    before_profile = fixture.paths.profile_path.read_bytes()
    before_database = fixture.paths.database_path.read_bytes()
    coordinator = UpdateCoordinator(runtime=runtime, paths=fixture.paths, checker=runtime.update_checker)
    phases = []
    try:
        with coordinator._prepare_production("d" * 64, descriptor, phases.append) as prepared:
            prepared.helper.ready(time.monotonic() + 10)
            assert prepared.store.read_snapshot().record["state"] == "prepared"
            assert prepared.helper.command("CANCEL", time.monotonic() + 10) == "CANCELED"
            assert prepared.helper.process.wait(timeout=10) == 0
            prepared.abort()
            prepared.helper.close()
        assert protocol.JournalStore(fixture.paths.update_recovery_dir).read_snapshot().record["state"] == "aborted_no_mutation"
        assert phases == ["verifying", "snapshotting_environment", "stopping_work", "backing_up", "preparing_recovery"]
        assert fixture.paths.profile_path.read_bytes() == before_profile
        assert fixture.paths.database_path.read_bytes() == before_database
        assert fixture.maintenance.update_active is False
        assert not manager.target.exists()
    finally:
        runtime._close_runtime()
        ordinary.release()


def test_normal_quit_retains_ordinary_lock_through_worker_drain_and_join(tmp_path, monkeypatch):
    import pytest
    from arxiv_digest.application import Application
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.update_locks import LockTimeoutError
    from arxiv_digest.web.lifecycle import SingleInstance
    paths = initialized_paths(tmp_path / "application")
    barrier, lifecycle = MaintenanceBarrier(), LifecycleController()
    runtime = _DefaultRuntime(paths, ProfileRepository(paths.profile_path, paths.profile_lock_path),
                              barrier, lifecycle, output=lambda message: None)
    started, canceled, finish, stopped = (threading.Event() for _ in range(4))
    events = []
    monkeypatch.setattr("arxiv_digest.update_contract.LOCK_WAIT_TIMEOUT_SECONDS", 0.01)
    def work():
        started.set()
        assert finish.wait(3)
        with barrier.operation():
            events.append("worker_cleanup")
    def sync():
        runtime._new_job("sync", "sync", work, request_cancel=canceled.set)
        assert started.wait(2)
    class Server:
        port, startup_nonce, token = 31234, "s" * 32, "A" * 43
        def start(self): pass
        def stop(self): events.append("server_closed")
        def launch_url(self, view): return "http://127.0.0.1:31234/"
    def closed():
        events.append("runtime_closed")
        assert not any(thread.name.startswith("arxiv-digest-sync") and thread.is_alive() for thread in threading.enumerate())
    application = Application(paths=paths, profile_exists=lambda: True,
        instance_factory=lambda: SingleInstance(paths.process_lock_path, paths.runtime_descriptor_path),
        server_factory=lambda handlers: Server(), handlers_factory=lambda: {}, resolve_restore_journal=lambda: None,
        open_database=lambda: SimpleNamespace(close=runtime._close_runtime), start_sync=sync,
        browser_open=lambda url: True, wait_for_server=lambda server: lifecycle.request_quit(),
        application_stopping=closed)
    errors = []
    def run():
        try:
            assert application.open_dashboard("default") == 0
        except BaseException as error:
            errors.append(error)
        finally:
            stopped.set()
    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert canceled.wait(2), errors
        time.sleep(0.04)  # Several finite drain waits must not release ownership.
        assert not stopped.is_set()
        with pytest.raises(LockTimeoutError):
            acquire_exclusive(paths.process_lock_path, timeout=0)
        finish.set()
        assert stopped.wait(3)
        assert errors == []
        assert events == ["server_closed", "worker_cleanup", "runtime_closed"]
        lock = acquire_exclusive(paths.process_lock_path, timeout=0)
        lock.release()
    finally:
        finish.set()
        owner.join(3)
