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



@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_explicit_launcher_repair_removes_historical_recovery_wrapper(
    tmp_path: Path, platform: str,
) -> None:
    import shlex
    from arxiv_digest.desktop_launcher import DesktopLauncherManager, LauncherState

    executable = tmp_path / "arxiv-digest"
    executable.write_bytes(b"synthetic executable")
    recovery = tmp_path / "data/update-recovery/recover-arxiv-digest"
    manager = DesktopLauncherManager(
        platform=platform, home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path), legacy_recovery_wrapper=recovery,
    )
    manager.install()
    wrapper = shlex.quote(str(recovery))
    if platform == "darwin":
        payload = (
            f"#!/bin/sh\nif [ -e {wrapper} ] || [ -L {wrapper} ]; then\n"
            f"  {wrapper} || exit $?\nfi\nexec '{executable}'\n"
        ).encode()
        launcher = manager.target / "Contents/MacOS/arxiv-digest"
        launcher.write_bytes(payload)
        manifest = manager.target / "Contents/Resources/ownership.json"
        ownership = json.loads(manifest.read_text())
        ownership["executable_sha256"] = hashlib.sha256(payload).hexdigest()
        manifest.write_text(json.dumps(ownership, sort_keys=True, separators=(",", ":")) + "\n", newline="\n")
    else:
        script = (
            f"if [ -e {wrapper} ] || [ -L {wrapper} ]; then {wrapper} || exit $?; "
            f"fi; exec {shlex.quote(str(executable))}"
        ).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
        launcher = manager.target
        launcher.write_text("".join(
            f'Exec=/bin/sh -c "{script}"\n' if line.startswith("Exec=") else line
            for line in launcher.read_text().splitlines(keepends=True)
        ), newline="\n")
    assert manager.status().state is LauncherState.OUTDATED
    assert manager.install().state is LauncherState.INSTALLED
    assert b"recover-arxiv-digest" not in launcher.read_bytes()
    assert not recovery.exists()



def test_launcher_guard_releases_ownership_after_error(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import launcher_operation_guard
    from arxiv_digest.atomic import acquire_exclusive

    paths = _coordination_paths(tmp_path)
    with pytest.raises(RuntimeError, match="operation failed"):
        with launcher_operation_guard(paths, timeout=0.1):
            with pytest.raises(TimeoutError):
                acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
            raise RuntimeError("operation failed")
    lock = acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
    lock.release()



def test_launcher_lock_timeout_precedes_status_and_leaves_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        launcher_operation_guard,
    )
    from arxiv_digest.atomic import acquire_exclusive

    paths = _coordination_paths(tmp_path)
    owner = acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
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
    replacement = acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
    replacement.release()



def test_concurrent_launcher_remove_waits_for_install_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager,
        LauncherState,
        launcher_operation_guard,
    )
    from arxiv_digest.atomic import acquire_exclusive

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
            acquire_exclusive(paths.launcher_operation_lock_path, timeout=0)
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
    if os.name != "nt":
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
    escaped_executable = str(executable.resolve()).replace("\\", "\\\\")
    assert f'Exec="{escaped_executable}"' in payload
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


def test_windows_launcher_is_owned_foreground_and_preserves_special_paths(
    tmp_path: Path,
) -> None:
    import base64
    from arxiv_digest.desktop_launcher import DesktopLauncherManager, LauncherState

    executable = tmp_path / "bin space ' & % ! 论文" / "arxiv-digest.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="win32", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )

    assert manager.status().state is LauncherState.ABSENT
    installed = manager.install()
    assert installed.state is LauncherState.INSTALLED
    assert manager.target == tmp_path / "Desktop/arXiv Digest.cmd"
    payload = manager.target.read_bytes()
    assert b"org.arxiv.digest" in payload
    command = payload.decode("ascii").splitlines()[-1]
    assert command.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand ")
    script = base64.b64decode(command.split()[-1], validate=True).decode("utf-16-le")
    quoted_path = str(executable.resolve()).replace("'", "''")
    assert script == f"$ErrorActionPreference = 'Stop'; & '{quoted_path}'; exit $LASTEXITCODE"
    assert manager.install().state is LauncherState.INSTALLED
    assert manager.target.read_bytes() == payload
    assert manager.remove().state is LauncherState.ABSENT
    assert not manager.target.exists()


def test_windows_launcher_uses_native_redirected_desktop_for_the_current_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import SimpleNamespace
    import arxiv_digest.desktop_launcher as launchers

    executable = tmp_path / "arxiv-digest.exe"
    executable.write_bytes(b"synthetic executable")
    desktop = tmp_path / "Redirected Desktop"
    calls = []

    def folder_path(*arguments):
        calls.append(arguments)
        return str(desktop)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setitem(sys.modules, "win32com.shell", SimpleNamespace(
        shell=SimpleNamespace(SHGetFolderPath=folder_path),
        shellcon=SimpleNamespace(CSIDL_DESKTOPDIRECTORY=16),
    ))
    manager = launchers.DesktopLauncherManager(
        platform="win32", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )
    assert manager.target == desktop / "arXiv Digest.cmd"
    assert calls == [(0, 16, None, 0)]
    assert not desktop.exists()


@pytest.mark.parametrize("install_first", [False, True])
def test_windows_launcher_preserves_unowned_and_tampered_files(
    tmp_path: Path, install_first: bool,
) -> None:
    from arxiv_digest.desktop_launcher import (
        DesktopLauncherManager, LauncherCollisionError, LauncherState,
    )

    executable = tmp_path / "arxiv-digest.exe"
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="win32", home=tmp_path, executable=executable,
        operation_guard=_operation_guard(tmp_path),
    )
    if install_first:
        manager.install()
    else:
        manager.target.parent.mkdir(parents=True)
    with manager.target.open("ab") as handle:
        handle.write(b"user content\n")
    expected = manager.target.read_bytes()
    assert manager.status().state is LauncherState.COLLISION
    with pytest.raises(LauncherCollisionError):
        manager.install()
    with pytest.raises(LauncherCollisionError):
        manager.remove()
    assert manager.target.read_bytes() == expected


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows cmd and PowerShell")
def test_windows_launcher_executes_a_unicode_executable_path_and_waits_for_exit(
    tmp_path: Path,
) -> None:
    import subprocess
    import venv
    from arxiv_digest.desktop_launcher import DesktopLauncherManager

    environment = tmp_path / "space ' & % ! 论文"
    venv.EnvBuilder(with_pip=False).create(environment)
    manager = DesktopLauncherManager(
        platform="win32", home=tmp_path,
        executable=environment / "Scripts/python.exe",
        operation_guard=_operation_guard(tmp_path),
    )
    manager.install()
    result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(manager.target)],
        input="print('launcher-finished'); raise SystemExit(19)\n",
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 19, result.stderr
    assert "launcher-finished" in result.stdout
