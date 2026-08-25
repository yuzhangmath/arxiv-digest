from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


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


def test_macos_launcher_includes_custom_app_icon(tmp_path: Path) -> None:
    from arxiv_digest.desktop_launcher import DesktopLauncherManager

    executable = tmp_path / "bin/arxiv-digest"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    manager = DesktopLauncherManager(
        platform="darwin", home=tmp_path, executable=executable
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
        platform="darwin", home=tmp_path, executable=executable
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
        platform="darwin", home=tmp_path, executable=executable
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
        platform="darwin", home=tmp_path, executable=executable
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
