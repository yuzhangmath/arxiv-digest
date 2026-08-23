from __future__ import annotations

import os
from pathlib import Path

import pytest


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
        platform="darwin", home=tmp_path, executable=executable
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
        platform="darwin", home=tmp_path, executable=executable
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
        platform="linux", home=tmp_path, executable=executable
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
        )
