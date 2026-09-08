from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest


def _coordination_paths(root: Path):
    from arxiv_digest.paths import resolve_paths

    return resolve_paths(
        platform="linux",
        home=root,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(root / "coordination"),
        },
    )


def _operation_guard(root: Path):
    from arxiv_digest.desktop_launcher import launcher_operation_guard

    return partial(launcher_operation_guard, _coordination_paths(root))


def test_launcher_operation_guard_holds_both_locks_and_releases_on_error(
    tmp_path: Path,
) -> None:
    from arxiv_digest.desktop_launcher import launcher_operation_guard
    from arxiv_digest.update_locks import acquire_exclusive, acquire_shared

    paths = _coordination_paths(tmp_path)
    with pytest.raises(RuntimeError, match="operation failed"):
        with launcher_operation_guard(paths, timeout=0.1):
            for path in (
                paths.update_transition_lock_path,
                paths.launcher_operation_lock_path,
            ):
                with pytest.raises(TimeoutError):
                    acquire_exclusive(path, timeout=0)
            # A CLI invocation already holding SH can enter the same boundary.
            shared = acquire_shared(paths.update_transition_lock_path, timeout=0)
            shared.release()
            raise RuntimeError("operation failed")

    for path in (
        paths.update_transition_lock_path,
        paths.launcher_operation_lock_path,
    ):
        exclusive = acquire_exclusive(path, timeout=0)
        exclusive.release()


def test_launcher_guard_acquires_transition_before_launcher_and_releases_in_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arxiv_digest.desktop_launcher as launchers

    paths = _coordination_paths(tmp_path)
    events: list[str] = []
    original_shared = launchers.acquire_shared
    original_exclusive = launchers.acquire_exclusive

    class RecordedLock:
        def __init__(self, lock, name):
            self.lock = lock
            self.name = name

        def release(self):
            events.append(f"release:{self.name}")
            self.lock.release()

    def shared(path, **kwargs):
        assert path == paths.update_transition_lock_path
        lock = original_shared(path, **kwargs)
        events.append("acquire:transition-shared")
        return RecordedLock(lock, "transition-shared")

    def exclusive(path, **kwargs):
        assert path == paths.launcher_operation_lock_path
        lock = original_exclusive(path, **kwargs)
        events.append("acquire:launcher-exclusive")
        return RecordedLock(lock, "launcher-exclusive")

    monkeypatch.setattr(launchers, "acquire_shared", shared)
    monkeypatch.setattr(launchers, "acquire_exclusive", exclusive)

    with launchers.launcher_operation_guard(paths, timeout=0.1):
        events.append("operation")

    assert events == [
        "acquire:transition-shared",
        "acquire:launcher-exclusive",
        "operation",
        "release:launcher-exclusive",
        "release:transition-shared",
    ]


@pytest.mark.parametrize("held_lock", ["transition", "launcher"])
def test_launcher_lock_timeout_precedes_status_and_leaves_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_lock: str
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        launcher_operation_guard,
    )
    from arxiv_digest.update_locks import acquire_exclusive

    paths = _coordination_paths(tmp_path)
    paths.ensure_update_coordination()
    path = (
        paths.update_transition_lock_path
        if held_lock == "transition"
        else paths.launcher_operation_lock_path
    )
    owner = acquire_exclusive(path, timeout=0)
    executable = tmp_path / "arxiv-digest"
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="linux",
        home=tmp_path,
        executable=executable,
        operation_guard=partial(launcher_operation_guard, paths, timeout=0.01),
    )
    monkeypatch.setattr(
        manager, "status", lambda: pytest.fail("status read before lock acquisition")
    )
    try:
        with pytest.raises(TimeoutError):
            manager.install()
        assert not manager.target.exists()
    finally:
        owner.release()
    # A failed second acquisition must not strand the first shared lock.
    replacement = acquire_exclusive(paths.update_transition_lock_path, timeout=0)
    replacement.release()


def test_concurrent_launcher_remove_waits_for_install_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherState,
        launcher_operation_guard,
    )
    from arxiv_digest.update_locks import acquire_exclusive

    executable = tmp_path / "arxiv-digest"
    executable.write_bytes(b"synthetic executable")
    paths = _coordination_paths(tmp_path)
    at_verification = threading.Event()
    finish_verification = threading.Event()
    remove_entering_guard = threading.Event()
    remove_status = threading.Event()
    errors: list[BaseException] = []

    @contextmanager
    def remove_guard():
        remove_entering_guard.set()
        with launcher_operation_guard(paths, timeout=2):
            yield

    installer = DesktopLauncherManager(
        platform="linux", home=tmp_path, executable=executable,
        operation_guard=partial(launcher_operation_guard, paths, timeout=2),
    )
    remover = DesktopLauncherManager(
        platform="linux", home=tmp_path, executable=executable,
        operation_guard=remove_guard,
    )
    installer_status = installer.status
    remover_status = remover.status

    def verify_install():
        status = installer_status()
        if status.state is LauncherState.INSTALLED:
            at_verification.set()
            assert finish_verification.wait(2)
        return status

    def inspect_remove():
        remove_status.set()
        return remover_status()

    monkeypatch.setattr(installer, "status", verify_install)
    monkeypatch.setattr(remover, "status", inspect_remove)

    def run(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    install_thread = threading.Thread(target=run, args=(installer.install,))
    remove_thread = threading.Thread(target=run, args=(remover.remove,))
    install_thread.start()
    try:
        assert at_verification.wait(2)
        remove_thread.start()
        assert remove_entering_guard.wait(2)
        assert not remove_status.wait(0.03)
        assert installer.target.exists()
        with pytest.raises(TimeoutError):
            acquire_exclusive(paths.update_transition_lock_path, timeout=0)
    finally:
        finish_verification.set()
        install_thread.join(2)
        if remove_thread.ident is not None:
            remove_thread.join(2)
    assert not errors
    assert not install_thread.is_alive()
    assert not remove_thread.is_alive()
    assert remove_status.is_set()
    assert remover_status().state is LauncherState.ABSENT


@pytest.mark.parametrize("operation", ["install", "remove"])
def test_launcher_guard_covers_initial_status_mutation_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import arxiv_digest.desktop_launcher as launchers

    executable = tmp_path / "arxiv-digest"
    executable.write_bytes(b"synthetic executable")
    events: list[str] = []

    @contextmanager
    def guard():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    manager = launchers.DesktopLauncherManager(
        platform="linux",
        home=tmp_path,
        executable=executable,
        operation_guard=guard,
    )
    if operation == "remove":
        manager.install()
    events.clear()
    original_status = manager.status

    def status():
        result = original_status()
        events.append(f"status:{result.state.value}")
        return result

    monkeypatch.setattr(manager, "status", status)
    if operation == "install":
        original_write = launchers.atomic_write

        def write(*args, **kwargs):
            events.append("mutate")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(launchers, "atomic_write", write)
        initial, final = "absent", "installed"
    else:
        original_fsync = launchers._fsync_directory

        def fsync(path):
            assert not manager.target.exists()
            events.append("mutate")
            return original_fsync(path)

        monkeypatch.setattr(launchers, "_fsync_directory", fsync)
        initial, final = "installed", "absent"

    result = getattr(manager, operation)()

    assert result.state.value == final
    assert events == [
        "enter", f"status:{initial}", "mutate", f"status:{final}", "exit"
    ]


def _write_v1_macos_launcher(home: Path, executable: Path) -> Path:
    app = home / "Applications/arXiv Digest.app"
    macos = app / "Contents/MacOS"
    resources = app / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    quoted = str(executable.resolve()).replace("'", "'\"'\"'")
    launcher = f"#!/bin/sh\nexec '{quoted}'\n".encode()
    plist = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>\n"
        "<key>CFBundleIdentifier</key><string>org.arxiv.digest</string>\n"
        "<key>CFBundleName</key><string>arXiv Digest</string>\n"
        "<key>CFBundleExecutable</key><string>arxiv-digest</string>\n"
        "<key>CFBundlePackageType</key><string>APPL</string>\n"
        "</dict></plist>\n"
    ).encode()
    manifest = (
        json.dumps(
            {
                "executable_sha256": hashlib.sha256(launcher).hexdigest(),
                "product": "org.arxiv.digest",
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    (app / "Contents/Info.plist").write_bytes(plist)
    (macos / "arxiv-digest").write_bytes(launcher)
    (resources / "ownership.json").write_bytes(manifest)
    return app


def test_macos_launcher_is_owned_atomic_idempotent_and_foreground(
    tmp_path: Path,
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherState,
    )

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    installed = manager.install()
    app = tmp_path / "Applications/arXiv Digest.app"
    launcher = app / "Contents/MacOS/arxiv-digest"
    first_bytes = launcher.read_bytes()

    assert installed.state is LauncherState.INSTALLED
    assert manager.status().state is LauncherState.INSTALLED
    assert "org.arxiv.digest" in (app / "Contents/Info.plist").read_text()
    assert str(executable.resolve()) in first_bytes.decode()
    assert "daemon" not in first_bytes.decode().casefold()
    assert "dashboard" not in first_bytes.decode().casefold()
    assert os.stat(launcher).st_mode & 0o111
    assert manager.install().state is LauncherState.INSTALLED
    assert launcher.read_bytes() == first_bytes


def test_macos_launcher_includes_custom_app_icon(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import DesktopLauncherManager

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    manager.install()

    app = tmp_path / "Applications/arXiv Digest.app"
    plist = (app / "Contents/Info.plist").read_text()
    icon = app / "Contents/Resources/arxiv-digest.icns"
    assert (
        "<key>CFBundleIconFile</key><string>arxiv-digest.icns</string>" in plist
    )
    assert icon.read_bytes().startswith(b"icns")


def test_macos_launcher_refuses_a_tampered_app_icon(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherCollisionError,
        LauncherState,
    )

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )
    manager.install()

    icon = (
        tmp_path
        / "Applications/arXiv Digest.app/Contents/Resources/arxiv-digest.icns"
    )
    icon.write_bytes(b"tampered")

    assert manager.status().state is LauncherState.COLLISION
    with pytest.raises(LauncherCollisionError):
        manager.install()


def test_macos_launcher_upgrades_an_exact_owned_v1_bundle(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import DesktopLauncherManager, LauncherState

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    app = _write_v1_macos_launcher(tmp_path, executable)
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    assert manager.status().state.value == "outdated"
    assert manager.install().state is LauncherState.INSTALLED
    assert (app / "Contents/Resources/arxiv-digest.icns").is_file()


def test_macos_launcher_preserves_extra_files_in_an_owned_bundle(
    tmp_path: Path,
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherCollisionError,
        LauncherState,
    )

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    app = _write_v1_macos_launcher(tmp_path, executable)
    sentinel = app / "user-file"
    sentinel.write_text("preserve me")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    assert manager.status().state is LauncherState.COLLISION
    with pytest.raises(LauncherCollisionError):
        manager.install()
    with pytest.raises(LauncherCollisionError):
        manager.remove()
    assert sentinel.read_text() == "preserve me"


def test_macos_launcher_refuses_unowned_or_tampered_targets(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherCollisionError,
        LauncherState,
    )

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    app = tmp_path / "Applications/arXiv Digest.app"
    app.mkdir(parents=True)
    sentinel = app / "user-file"
    sentinel.write_text("preserve me")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    assert manager.status().state is LauncherState.COLLISION
    with pytest.raises(LauncherCollisionError):
        manager.install()
    with pytest.raises(LauncherCollisionError):
        manager.remove()
    assert sentinel.read_text() == "preserve me"


def test_linux_launcher_has_absolute_exec_marker_and_safe_remove(
    tmp_path: Path,
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherState,
    )

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="linux", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    result = manager.install()
    desktop = tmp_path / ".local/share/applications/arxiv-digest.desktop"
    payload = desktop.read_text()

    assert result.state is LauncherState.INSTALLED
    assert f'Exec="{executable.resolve()}"' in payload
    assert "X-arXiv-Digest-Managed=true" in payload
    assert "Terminal=false" in payload
    assert manager.remove().state is LauncherState.ABSENT
    assert not desktop.exists()


def test_launcher_requires_an_absolute_installed_console_entry(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import DesktopLauncherManager

    with pytest.raises(ValueError, match="absolute"):
        DesktopLauncherManager(
            platform="linux",
            home=tmp_path,
            executable=Path("relative/arxiv-digest"),
            operation_guard=_operation_guard(tmp_path),
        )
