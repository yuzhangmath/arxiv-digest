"""Opt-in, foreground-only desktop launchers for macOS and Linux."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from arxiv_digest.atomic import atomic_write


_SCHEMA_VERSION = 1
_BUNDLE_ID = "org.arxiv.digest"


class LauncherState(StrEnum):
    ABSENT = "absent"
    INSTALLED = "installed"
    COLLISION = "collision"


@dataclass(frozen=True, slots=True)
class LauncherStatus:
    state: LauncherState
    path: Path


class LauncherCollisionError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DesktopLauncherManager:
    def __init__(
        self,
        *,
        platform: str,
        home: Path,
        executable: Path,
    ) -> None:
        if platform != "darwin" and not platform.startswith("linux"):
            raise RuntimeError("desktop launchers support macOS and Linux")
        if not executable.is_absolute():
            raise ValueError("launcher executable must be an absolute path")
        self.platform = platform
        self.home = Path(home)
        self.executable = executable.resolve()
        if not self.executable.is_file():
            raise ValueError("launcher executable must be an installed file")

    @property
    def target(self) -> Path:
        if self.platform == "darwin":
            return self.home / "Applications/arXiv Digest.app"
        return self.home / ".local/share/applications/arxiv-digest.desktop"

    def _mac_launcher(self) -> bytes:
        quoted = str(self.executable).replace("'", "'\"'\"'")
        return f"#!/bin/sh\nexec '{quoted}'\n".encode("utf-8")

    @staticmethod
    def _mac_plist() -> bytes:
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict>\n"
            "<key>CFBundleIdentifier</key><string>org.arxiv.digest</string>\n"
            "<key>CFBundleName</key><string>arXiv Digest</string>\n"
            "<key>CFBundleExecutable</key><string>arxiv-digest</string>\n"
            "<key>CFBundlePackageType</key><string>APPL</string>\n"
            "</dict></plist>\n"
        ).encode("utf-8")

    def _mac_manifest(self, launcher: bytes) -> bytes:
        return (
            json.dumps(
                {
                    "executable_sha256": hashlib.sha256(launcher).hexdigest(),
                    "product": _BUNDLE_ID,
                    "schema_version": _SCHEMA_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _linux_payload(self) -> bytes:
        escaped = (
            str(self.executable)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=arXiv Digest\n"
            f'Exec="{escaped}"\n'
            "Terminal=false\n"
            "X-arXiv-Digest-Managed=true\n"
            f"X-arXiv-Digest-Launcher-Schema={_SCHEMA_VERSION}\n"
        ).encode("utf-8")

    def status(self) -> LauncherStatus:
        target = self.target
        if not target.exists() and not target.is_symlink():
            return LauncherStatus(LauncherState.ABSENT, target)
        if self.platform == "darwin":
            launcher = target / "Contents/MacOS/arxiv-digest"
            plist = target / "Contents/Info.plist"
            manifest = target / "Contents/Resources/ownership.json"
            try:
                valid = (
                    target.is_dir()
                    and not target.is_symlink()
                    and launcher.is_file()
                    and not launcher.is_symlink()
                    and plist.read_bytes() == self._mac_plist()
                    and launcher.read_bytes() == self._mac_launcher()
                    and manifest.read_bytes()
                    == self._mac_manifest(self._mac_launcher())
                )
            except OSError:
                valid = False
        else:
            try:
                valid = (
                    target.is_file()
                    and not target.is_symlink()
                    and target.read_bytes() == self._linux_payload()
                )
            except OSError:
                valid = False
        return LauncherStatus(
            LauncherState.INSTALLED if valid else LauncherState.COLLISION,
            target,
        )

    def install(self) -> LauncherStatus:
        current = self.status()
        if current.state is LauncherState.INSTALLED:
            return current
        if current.state is LauncherState.COLLISION:
            raise LauncherCollisionError(
                "launcher target exists without a valid ownership marker"
            )
        if self.platform == "darwin":
            return self._install_macos()
        target = self.target
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(target, self._linux_payload(), mode=0o700)
        return self.status()

    def _install_macos(self) -> LauncherStatus:
        target = self.target
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".arxiv-digest-launcher.", dir=target.parent)
        )
        try:
            macos = temporary / "Contents/MacOS"
            resources = temporary / "Contents/Resources"
            macos.mkdir(mode=0o700, parents=True)
            resources.mkdir(mode=0o700, parents=True)
            launcher = self._mac_launcher()
            atomic_write(
                temporary / "Contents/Info.plist", self._mac_plist(), mode=0o600
            )
            atomic_write(macos / "arxiv-digest", launcher, mode=0o700)
            atomic_write(
                resources / "ownership.json",
                self._mac_manifest(launcher),
                mode=0o600,
            )
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        installed = self.status()
        if installed.state is not LauncherState.INSTALLED:
            raise RuntimeError("desktop launcher verification failed")
        return installed

    def remove(self) -> LauncherStatus:
        current = self.status()
        if current.state is LauncherState.ABSENT:
            return current
        if current.state is LauncherState.COLLISION:
            raise LauncherCollisionError(
                "launcher target is not owned by arXiv Digest"
            )
        if self.platform == "darwin":
            shutil.rmtree(self.target)
        else:
            self.target.unlink()
        _fsync_directory(self.target.parent)
        return self.status()
