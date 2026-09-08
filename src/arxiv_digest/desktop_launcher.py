"""Opt-in, foreground-only desktop launchers for macOS and Linux."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import os
import shutil
import shlex
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from arxiv_digest.atomic import atomic_write
from arxiv_digest.paths import AppPaths
from arxiv_digest.update_contract import LOCK_WAIT_TIMEOUT_SECONDS
from arxiv_digest.update_locks import acquire_exclusive, acquire_shared


_MAC_SCHEMA_VERSION = 3
_LINUX_SCHEMA_VERSION = 2
_BUNDLE_ID = "org.arxiv.digest"
_MAC_ICON_FILENAME = "arxiv-digest.icns"


class LauncherState(StrEnum):
    ABSENT = "absent"
    INSTALLED = "installed"
    OUTDATED = "outdated"
    COLLISION = "collision"


@dataclass(frozen=True, slots=True)
class LauncherStatus:
    state: LauncherState
    path: Path


class LauncherCollisionError(RuntimeError):
    pass


@contextmanager
def launcher_operation_guard(
    paths: AppPaths,
    *,
    timeout: float = LOCK_WAIT_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize launcher changes while excluding an update transition."""

    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("launcher lock timeout must be finite and nonnegative")
    paths.ensure_update_coordination()
    deadline = time.monotonic() + timeout
    transition = acquire_shared(paths.update_transition_lock_path, timeout=timeout)
    try:
        launcher = acquire_exclusive(
            paths.launcher_operation_lock_path,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        try:
            yield
        finally:
            launcher.release()
    finally:
        transition.release()


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
        operation_guard: Callable[[], AbstractContextManager[None]],
        recovery_wrapper: Path | None = None,
    ) -> None:
        if platform != "darwin" and not platform.startswith("linux"):
            raise RuntimeError("desktop launchers support macOS and Linux")
        if not executable.is_absolute():
            raise ValueError("launcher executable must be an absolute path")
        self.platform = platform
        self.home = Path(home)
        self.executable = executable.resolve()
        self._operation_guard = operation_guard
        self.recovery_wrapper = recovery_wrapper
        if recovery_wrapper is not None and (not recovery_wrapper.is_absolute() or "\n" in str(recovery_wrapper) or "\r" in str(recovery_wrapper)):
            raise ValueError("recovery wrapper must be an absolute fixed path")
        if not self.executable.is_file():
            raise ValueError("launcher executable must be an installed file")

    @property
    def target(self) -> Path:
        if self.platform == "darwin":
            return self.home / "Applications/arXiv Digest.app"
        return self.home / ".local/share/applications/arxiv-digest.desktop"

    def _mac_launcher(self, *, legacy: bool = False) -> bytes:
        quoted = str(self.executable).replace("'", "'\"'\"'")
        recovery = ""
        if self.recovery_wrapper is not None and not legacy:
            wrapper = shlex.quote(str(self.recovery_wrapper))
            recovery = f"if [ -e {wrapper} ] || [ -L {wrapper} ]; then\n  {wrapper} || exit $?\nfi\n"
        return f"#!/bin/sh\n{recovery}exec '{quoted}'\n".encode("utf-8")

    @staticmethod
    def _mac_plist(*, schema: int = _MAC_SCHEMA_VERSION) -> bytes:
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\"><dict>\n"
            "<key>CFBundleIdentifier</key><string>org.arxiv.digest</string>\n"
            "<key>CFBundleName</key><string>arXiv Digest</string>\n"
            "<key>CFBundleExecutable</key><string>arxiv-digest</string>\n"
            f"<key>CFBundleIconFile</key><string>{_MAC_ICON_FILENAME}</string>\n"
            f"<key>CFBundleVersion</key><string>{schema}</string>\n"
            "<key>CFBundlePackageType</key><string>APPL</string>\n"
            "</dict></plist>\n"
        ).encode("utf-8")

    @staticmethod
    def _legacy_mac_plist() -> bytes:
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

    @staticmethod
    def _mac_icon() -> bytes:
        return (
            importlib.resources.files("arxiv_digest")
            .joinpath("assets", _MAC_ICON_FILENAME)
            .read_bytes()
        )

    def _mac_manifest(self, launcher: bytes, icon: bytes, *, schema: int = _MAC_SCHEMA_VERSION) -> bytes:
        return (
            json.dumps(
                {
                    "executable_sha256": hashlib.sha256(launcher).hexdigest(),
                    "icon_sha256": hashlib.sha256(icon).hexdigest(),
                    "product": _BUNDLE_ID,
                    "schema_version": schema,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _legacy_mac_manifest(self, launcher: bytes) -> bytes:
        return (
            json.dumps(
                {
                    "executable_sha256": hashlib.sha256(launcher).hexdigest(),
                    "product": _BUNDLE_ID,
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _mac_inventory_matches(target: Path, *, includes_icon: bool) -> bool:
        expected = {
            ("Contents", "directory"),
            ("Contents/Info.plist", "file"),
            ("Contents/MacOS", "directory"),
            ("Contents/MacOS/arxiv-digest", "file"),
            ("Contents/Resources", "directory"),
            ("Contents/Resources/ownership.json", "file"),
        }
        if includes_icon:
            expected.add((f"Contents/Resources/{_MAC_ICON_FILENAME}", "file"))
        actual: set[tuple[str, str]] = set()
        for path in target.rglob("*"):
            if path.is_symlink():
                return False
            if path.is_dir():
                kind = "directory"
            elif path.is_file():
                kind = "file"
            else:
                return False
            actual.add((path.relative_to(target).as_posix(), kind))
        return actual == expected

    def _linux_payload(self, *, legacy: bool = False) -> bytes:
        escaped = (
            str(self.executable)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        command = f'"{escaped}"'
        if self.recovery_wrapper is not None and not legacy:
            wrapper = shlex.quote(str(self.recovery_wrapper))
            script = f"if [ -e {wrapper} ] || [ -L {wrapper} ]; then {wrapper} || exit $?; fi; exec {shlex.quote(str(self.executable))}"
            escaped_script = script.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
            command = f'/bin/sh -c "{escaped_script}"'
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=arXiv Digest\n"
            f"Exec={command}\n"
            "Terminal=false\n"
            "X-arXiv-Digest-Managed=true\n"
            f"X-arXiv-Digest-Launcher-Schema={1 if legacy else _LINUX_SCHEMA_VERSION}\n"
        ).encode("utf-8")

    def status(self) -> LauncherStatus:
        target = self.target
        if not target.exists() and not target.is_symlink():
            return LauncherStatus(LauncherState.ABSENT, target)
        if self.platform == "darwin":
            launcher = target / "Contents/MacOS/arxiv-digest"
            plist = target / "Contents/Info.plist"
            icon = target / "Contents/Resources" / _MAC_ICON_FILENAME
            manifest = target / "Contents/Resources/ownership.json"
            try:
                launcher_bytes = self._mac_launcher()
                icon_bytes = self._mac_icon()
                current = (
                    target.is_dir()
                    and not target.is_symlink()
                    and self._mac_inventory_matches(target, includes_icon=True)
                    and launcher.is_file()
                    and not launcher.is_symlink()
                    and plist.read_bytes() == self._mac_plist()
                    and launcher.read_bytes() == launcher_bytes
                    and icon.is_file()
                    and not icon.is_symlink()
                    and icon.read_bytes() == icon_bytes
                    and manifest.read_bytes()
                    == self._mac_manifest(launcher_bytes, icon_bytes)
                )
                previous = (
                    not current
                    and target.is_dir()
                    and not target.is_symlink()
                    and self._mac_inventory_matches(target, includes_icon=True)
                    and launcher.read_bytes() == self._mac_launcher(legacy=True)
                    and plist.read_bytes() == self._mac_plist(schema=2)
                    and icon.read_bytes() == icon_bytes
                    and manifest.read_bytes() == self._mac_manifest(self._mac_launcher(legacy=True), icon_bytes, schema=2)
                )
                legacy = (
                    not current
                    and target.is_dir()
                    and not target.is_symlink()
                    and self._mac_inventory_matches(target, includes_icon=False)
                    and launcher.is_file()
                    and not launcher.is_symlink()
                    and plist.read_bytes() == self._legacy_mac_plist()
                    and launcher.read_bytes() == self._mac_launcher(legacy=True)
                    and manifest.read_bytes()
                    == self._legacy_mac_manifest(self._mac_launcher(legacy=True))
                )
            except OSError:
                current = False
                legacy = False
                previous = False
            if current:
                return LauncherStatus(LauncherState.INSTALLED, target)
            if legacy or previous:
                return LauncherStatus(LauncherState.OUTDATED, target)
            return LauncherStatus(LauncherState.COLLISION, target)
        else:
            try:
                valid = (
                    target.is_file()
                    and not target.is_symlink()
                    and target.read_bytes() == self._linux_payload()
                )
                outdated = (not valid and target.is_file() and not target.is_symlink()
                            and target.read_bytes() == self._linux_payload(legacy=True))
            except OSError:
                valid = False
                outdated = False
        return LauncherStatus(
            LauncherState.INSTALLED if valid else LauncherState.OUTDATED if outdated else LauncherState.COLLISION,
            target,
        )

    def install(self) -> LauncherStatus:
        with self._operation_guard():
            return self._install()

    def update_states(self) -> dict:
        """Describe an owned launcher's exact refresh without mutating it.

        The coordinator captures this under launcher ownership, then repeats
        the prior-state comparison under both exclusive update locks.
        """
        from arxiv_digest.update_runtime.recovery import capture_launcher_state

        status = self.status()
        if status.state is LauncherState.COLLISION:
            raise LauncherCollisionError("launcher ownership cannot be proven")
        prior = capture_launcher_state(self.target)
        if status.state is LauncherState.ABSENT:
            return {"prior": prior, "intended": prior}
        if self.platform != "darwin":
            payload = self._linux_payload()
            intended = {"kind": "file", "path": str(self.target), "mode": 0o700,
                        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                        "content_hex": payload.hex()}
        else:
            launcher, icon = self._mac_launcher(), self._mac_icon()
            files = {
                "Contents/Info.plist": (self._mac_plist(), 0o600),
                "Contents/MacOS/arxiv-digest": (launcher, 0o700),
                f"Contents/Resources/{_MAC_ICON_FILENAME}": (icon, 0o600),
                "Contents/Resources/ownership.json": (self._mac_manifest(launcher, icon), 0o600),
            }
            entries = [{"kind": "directory", "path": path, "mode": 0o700}
                       for path in ("Contents", "Contents/MacOS", "Contents/Resources")]
            entries.extend({"kind": "file", "path": path, "mode": mode, "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest()}
                           for path, (payload, mode) in files.items())
            intended = {"kind": "directory", "path": str(self.target),
                        "inventory": {"root_mode": 0o700, "entries": sorted(entries, key=lambda item: item["path"])},
                        "files": [{"path": path, "bytes_hex": payload.hex()} for path, (payload, _) in sorted(files.items())]}
        return {"prior": prior, "intended": intended}

    def _install(self) -> LauncherStatus:
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
        backup: Path | None = None
        try:
            macos = temporary / "Contents/MacOS"
            resources = temporary / "Contents/Resources"
            macos.mkdir(mode=0o700, parents=True)
            resources.mkdir(mode=0o700, parents=True)
            launcher = self._mac_launcher()
            icon = self._mac_icon()
            atomic_write(
                temporary / "Contents/Info.plist", self._mac_plist(), mode=0o600
            )
            atomic_write(macos / "arxiv-digest", launcher, mode=0o700)
            atomic_write(
                resources / _MAC_ICON_FILENAME,
                icon,
                mode=0o600,
            )
            atomic_write(
                resources / "ownership.json",
                self._mac_manifest(launcher, icon),
                mode=0o600,
            )
            if target.exists():
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=".arxiv-digest-launcher-backup.",
                        dir=target.parent,
                    )
                )
                backup.rmdir()
                os.replace(target, backup)
            try:
                os.replace(temporary, target)
            except BaseException:
                if backup is not None:
                    os.replace(backup, target)
                    backup = None
                raise
            _fsync_directory(target.parent)
            installed = self.status()
            if installed.state is not LauncherState.INSTALLED:
                raise RuntimeError("desktop launcher verification failed")
            if backup is not None:
                shutil.rmtree(backup)
                backup = None
                _fsync_directory(target.parent)
            return installed
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def remove(self) -> LauncherStatus:
        with self._operation_guard():
            return self._remove()

    def _remove(self) -> LauncherStatus:
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
