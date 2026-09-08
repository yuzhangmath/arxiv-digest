from __future__ import annotations

import builtins
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from arxiv_digest.cli import PreflightDisposition, main
from arxiv_digest.paths import AppPaths, resolve_paths
from arxiv_digest.update_locks import LockTimeoutError, acquire_exclusive, acquire_shared


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "app"),
        },
    )


def _assert_transition_held(paths: AppPaths) -> None:
    with pytest.raises(LockTimeoutError):
        acquire_exclusive(paths.update_transition_lock_path, timeout=0)


def _assert_transition_free(paths: AppPaths) -> None:
    lock = acquire_exclusive(paths.update_transition_lock_path, timeout=0)
    lock.release()


def test_default_doctor_keeps_transition_held_through_lazy_imports(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__
    observed: set[str] = set()

    def guarded_import(name, *args, **kwargs):
        if name in {
            "arxiv_digest.application",
            "arxiv_digest.backup",
            "arxiv_digest.doctor",
        }:
            _assert_transition_held(paths)
            observed.add(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    output: list[str] = []

    assert main(["doctor"], paths_factory=lambda: paths, output=output.append) == 0
    assert observed == {
        "arxiv_digest.application",
        "arxiv_digest.backup",
        "arxiv_digest.doctor",
    }
    _assert_transition_free(paths)
    assert not paths.profile_path.exists()
    assert not paths.database_path.exists()
    assert not paths.restore_journal_path.exists()
    assert not paths.runtime_descriptor_path.exists()


@pytest.mark.parametrize("command", ["doctor", "export", "import", "install-launcher"])
@pytest.mark.parametrize("failure", [None, "factory", "action"])
def test_non_dashboard_command_retains_transition_until_return_or_error(
    paths: AppPaths, command: str, failure: str | None
) -> None:
    def action(*args) -> int:
        _assert_transition_held(paths)
        if failure == "action":
            raise RuntimeError("action interrupted")
        return 17

    def factory():
        _assert_transition_held(paths)
        if failure == "factory":
            raise RuntimeError("factory interrupted")
        return SimpleNamespace(
            doctor=action,
            export_backup=action,
            import_backup=action,
            install_launcher=action,
        )

    argv = [command]
    if command in {"export", "import"}:
        argv.append(str(paths.data_dir.parent / "synthetic-backup.zip"))
    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} interrupted"):
            main(argv, application_factory=factory, paths_factory=lambda: paths)
    else:
        assert main(argv, application_factory=factory, paths_factory=lambda: paths) == 17
    _assert_transition_free(paths)


@pytest.mark.parametrize("existing", [False, True])
def test_dashboard_transition_handoff_waits_for_ordinary_instance_resolution(
    paths: AppPaths, existing: bool
) -> None:
    from arxiv_digest.application import Application
    from arxiv_digest.web.lifecycle import SingleInstance

    paths.ensure()
    paths.ensure_update_coordination()
    before_acquire, continue_acquire = Event(), Event()
    events: list[str] = []
    owner = SingleInstance(paths.process_lock_path, paths.runtime_descriptor_path)
    if existing:
        owner.acquire().publish(
            port=43123, startup_nonce="nonce_abcd12345678", token="A" * 43
        )

    class PausedInstance(SingleInstance):
        def acquire(self):
            _assert_transition_held(paths)
            before_acquire.set()
            assert continue_acquire.wait(3)
            return super().acquire()

    def after_handoff(event: str):
        _assert_transition_free(paths)
        with pytest.raises(LockTimeoutError):
            acquire_exclusive(paths.process_lock_path, timeout=0)
        events.append(event)

    def profiles() -> bool:
        after_handoff("profile")
        return False

    server = SimpleNamespace(
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="A" * 43,
        start=lambda: after_handoff("server"),
        stop=lambda: None,
        launch_url=lambda view: f"http://127.0.0.1:43123/#{view}",
    )
    application = Application(
        paths=paths,
        profile_exists=profiles,
        instance_factory=lambda: PausedInstance(
            paths.process_lock_path,
            paths.runtime_descriptor_path,
            health_probe=lambda descriptor: True,
        ),
        server_factory=lambda handlers: server,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: after_handoff("restore"),
        open_database=lambda: after_handoff("database"),
        start_sync=lambda: pytest.fail("uninitialized launch started sync"),
        browser_open=lambda url: after_handoff("browser") or True,
        wait_for_server=lambda running: None,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                main,
                [],
                paths_factory=lambda: paths,
                application_factory=lambda: application,
            )
            try:
                assert before_acquire.wait(3)
                _assert_transition_held(paths)
                assert events == []
            finally:
                continue_acquire.set()
            assert future.result(timeout=3) == 0
        assert events == (
            ["profile", "browser"] if existing
            else ["restore", "profile", "database", "server", "browser"]
        )
        _assert_transition_free(paths)
    finally:
        owner.release()


@pytest.mark.parametrize("command", ["doctor", "library"])
@pytest.mark.parametrize("kind", ["regular", "symlink", "directory", "fifo"])
def test_any_present_journal_blocks_before_application_or_data_access(
    paths: AppPaths, kind: str, command: str
) -> None:
    paths.ensure_update_coordination()
    if kind == "regular":
        paths.update_journal_path.write_bytes(b'{"command":"untrusted"}')
    elif kind == "symlink":
        paths.update_journal_path.symlink_to(paths.data_dir / "absent")
    elif kind == "directory":
        paths.update_journal_path.mkdir()
    else:
        os.mkfifo(paths.update_journal_path)
    output: list[str] = []

    assert main(
        [command],
        paths_factory=lambda: paths,
        application_factory=lambda: pytest.fail("blocked preflight constructed app"),
        output=output.append,
    ) == 3
    assert len(output) == 1
    if command == "doctor":
        assert str(paths.data_dir.parent) not in output[0]
        assert "Update recovery: blocked" in output[0]
    else:
        assert str(paths.recovery_wrapper_path) in output[0]
    assert "untrusted" not in output[0]
    assert not paths.profile_path.exists()
    assert not paths.database_path.exists()
    _assert_transition_free(paths)


@pytest.mark.parametrize("blocked", [False, True])
def test_doctor_never_recovers_or_changes_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: bool
) -> None:
    import hashlib
    import socket
    import subprocess

    from arxiv_digest import __version__
    from arxiv_digest.cli import _default_journal_classifier
    from tests.unit.test_backup import initialized_paths
    from tests.update_runtime_factory import prepared_runtime

    paths = initialized_paths(tmp_path / "state")
    _, _, _, locks = prepared_runtime(paths.update_recovery_dir)
    for lock in locks.values():
        lock.close()
    paths.ensure_update_coordination()
    paths.restore_journal_path.write_bytes(b'{"private":"synthetic restore state"}')
    if blocked:
        paths.recovery_wrapper_path.write_bytes(b"unverified recovery wrapper")
    expected = PreflightDisposition.BLOCK if blocked else PreflightDisposition.RECOVER
    assert _default_journal_classifier(paths.update_journal_path) is expected

    def snapshot():
        result = {}
        for path in tmp_path.rglob("*"):
            info = path.lstat()
            payload = (
                os.readlink(path) if path.is_symlink()
                else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file()
                else None
            )
            result[path.relative_to(tmp_path)] = (
                info.st_ino, info.st_mode, info.st_mtime_ns, payload,
            )
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("doctor invoked recovery, network, or application startup")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    before = snapshot()
    output: list[str] = []
    assert main(
        ["doctor"], paths_factory=lambda: paths,
        application_factory=forbidden, output=output.append,
    ) == 3
    assert snapshot() == before
    state = "blocked" if blocked else "pending"
    assert output == [
        f"arXiv Digest {__version__}\n"
        f"Update recovery: {state}\n"
        "No recovery was attempted. Follow the update recovery instructions "
        "in the troubleshooting guide."
    ]
    _assert_transition_free(paths)


@pytest.mark.parametrize("obstruction", ["runtime", "lock", "lock-open"])
def test_doctor_redacts_unsafe_recovery_coordination(
    paths: AppPaths, obstruction: str
) -> None:
    paths.ensure_update_coordination()
    paths.update_journal_path.write_bytes(b'{"private":"pending recovery"}')
    options = {}
    if obstruction == "runtime":
        paths.update_runtime_dir.rmdir()
        paths.update_runtime_dir.write_bytes(b"unexpected runtime file")
    elif obstruction == "lock":
        paths.update_transition_lock_path.chmod(0o644)
    else:
        def denied(path):
            raise PermissionError(13, "synthetic lock failure", str(path))
        options["transition_acquire"] = denied
    before = {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in paths.data_dir.rglob("*") if path.is_file()
    }
    output: list[str] = []
    assert main(
        ["doctor"], paths_factory=lambda: paths, output=output.append,
        application_factory=lambda: pytest.fail("unsafe preflight constructed app"),
        **options,
    ) == 3
    assert len(output) == 1
    assert "Update recovery: blocked" in output[0]
    assert str(paths.data_dir.parent) not in output[0]
    assert {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in paths.data_dir.rglob("*") if path.is_file()
    } == before
    assert not paths.profile_path.exists()
    assert not paths.database_path.exists()


def test_recovery_dispatch_releases_shared_lock_and_uses_only_fixed_wrapper(
    paths: AppPaths,
) -> None:
    def classify(path: Path) -> PreflightDisposition:
        assert path == paths.update_journal_path
        _assert_transition_held(paths)
        return PreflightDisposition.RECOVER

    def recover(wrapper: Path) -> int:
        assert wrapper == paths.recovery_wrapper_path
        _assert_transition_free(paths)
        return 23

    assert main(
        [],
        paths_factory=lambda: paths,
        journal_classifier=classify,
        recovery_executor=recover,
        application_factory=lambda: pytest.fail("recovery constructed app"),
    ) == 23


def test_transition_timeout_prevents_classification_and_app_creation(paths: AppPaths) -> None:
    paths.ensure_update_coordination()
    held = acquire_exclusive(paths.update_transition_lock_path, timeout=0)
    output: list[str] = []
    try:
        assert main(
            ["doctor"],
            paths_factory=lambda: paths,
            transition_acquire=lambda path: acquire_shared(path, timeout=0.01),
            journal_classifier=lambda path: pytest.fail("classified during transition"),
            application_factory=lambda: pytest.fail("constructed app during transition"),
            output=output.append,
        ) == 3
    finally:
        held.release()
    assert output == ["An arXiv Digest update is in progress. Try again shortly."]
    assert not paths.update_plan_path.exists()
    assert not paths.update_journal_path.exists()
    _assert_transition_free(paths)


def test_production_launcher_is_guarded_before_it_inspects_or_changes_files(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.desktop_launcher import LauncherState, LauncherStatus
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.web.lifecycle import LifecycleController

    home = paths.data_dir.parent.parent / "home"
    executable = home / "bin" / "arxiv-digest"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setattr("arxiv_digest.application.shutil.which", lambda name: str(executable))
    monkeypatch.setattr(Path, "home", lambda: home)
    runtime = _DefaultRuntime(
        paths,
        ProfileRepository(paths.profile_path, paths.profile_lock_path),
        MaintenanceBarrier(),
        LifecycleController(),
        output=lambda message: None,
    )
    manager = runtime._launcher_manager()
    assert manager is not None
    assert not paths.update_recovery_dir.exists()
    inspections: list[str] = []

    def absent():
        _assert_transition_held(paths)
        with pytest.raises(LockTimeoutError):
            acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
        inspections.append("status")
        return LauncherStatus(LauncherState.ABSENT, manager.target)

    monkeypatch.setattr(manager, "status", absent)
    manager.remove()
    assert inspections == ["status"]
    _assert_transition_free(paths)
    launcher = acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
    launcher.release()


@pytest.mark.parametrize("argv", [["--help"], ["doctor", "--help"], ["--arxiv-digest-internal-relaunch"]])
def test_help_and_unauthenticated_internal_modes_create_no_state(
    paths: AppPaths, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            argv,
            paths_factory=lambda: pytest.fail("parsed command resolved paths"),
            application_factory=lambda: pytest.fail("parsed command constructed app"),
        )
    if "--help" in argv:
        assert raised.value.code == 0
        assert "--arxiv-digest-internal-" not in capsys.readouterr().out
    else:
        assert "authenticated invocation" in str(raised.value)
    assert not paths.data_dir.exists()


@pytest.mark.parametrize("terminal", ["complete", "aborted_no_mutation"])
def test_authoritative_nonblocking_terminal_allows_before_receipt_ack(paths, terminal):
    from arxiv_digest.update_runtime import protocol
    from tests.update_protocol_factory import admit_journal, completed_journal, next_record, proposal
    paths.ensure_update_coordination()
    if terminal == "complete":
        store, _ = completed_journal(paths.update_recovery_dir)
    else:
        store, current = admit_journal(paths.update_recovery_dir)
        current = store.transition(current, next_record(current, "canceling_no_install"))
        store.transition(current, next_record(current, terminal, receipt={**proposal(outcome="handoff_failed"), "unacknowledged": True}))
    called = []
    assert main(["doctor"], paths_factory=lambda: paths,
                application_factory=lambda: SimpleNamespace(doctor=lambda: called.append("doctor") or 0)) == 0
    assert called == ["doctor"]
    assert "receipt" in store.read_snapshot().record


def test_recoverable_record_does_not_execute_unverified_or_missing_helper(paths):
    from arxiv_digest.update_runtime import protocol
    from tests.update_protocol_factory import admit_journal
    paths.ensure_update_coordination()
    admit_journal(paths.update_recovery_dir)
    assert protocol.classify_journal(paths.update_recovery_dir) == "recover"
    assert main(["doctor"], paths_factory=lambda: paths,
                recovery_executor=lambda path: pytest.fail("unverified recovery runtime executed"),
                application_factory=lambda: pytest.fail("recovery opened data"), output=lambda message: None) == 3
