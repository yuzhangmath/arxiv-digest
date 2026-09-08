"""Bounded target-only transfers into an attempt's already-private directory."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arxiv_digest.update_contract import DISCOVERY_DEADLINE_SECONDS, WHEEL_BYTE_LIMIT
from arxiv_digest.update_discovery import UpdateDescriptor, automatic_update_descriptor
from arxiv_digest.update_http import _safe_open_url, _single_header, open_release_asset
from arxiv_digest.update_manifest import WheelInspection, inspect_update_wheel_descriptor, parse_update_manifest, serialize_update_manifest
from arxiv_digest.update_runtime.protocol import _directory, file_identity


class DownloadError(ValueError):
    """Fixed safe failure; underlying details remain private to the caller."""


@dataclass(frozen=True, slots=True)
class DownloadedWheel:
    path: Path
    inspection: WheelInspection
    identity: dict[str, int]


def retain_target_manifest(descriptor: UpdateDescriptor, attempt_dir: Path):
    """Retain the exact authenticated canonical manifest beside its wheel."""
    payload = serialize_update_manifest(descriptor.target.manifest)
    expected = descriptor.target.manifest_asset
    if len(payload) != expected.size or hashlib.sha256(payload).hexdigest() != expected.sha256:
        raise DownloadError("retained target manifest differs")
    with _directory(attempt_dir) as parent:
        snapshot = parent.publish("UPDATE_MANIFEST.json", payload, parse_update_manifest, None)
    return {"path": str(Path(attempt_dir) / "UPDATE_MANIFEST.json"), "size": len(payload),
            "sha256": snapshot.sha256, "identity": snapshot.identity}


def _private_file(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise DownloadError("download identity is unsafe")


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DownloadError("download directory is unsafe")
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _unlink_owned(parent: int, name: str, created: os.stat_result) -> None:
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (named.st_dev, named.st_ino) == (created.st_dev, created.st_ino):
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)


def download_target_wheel(
    descriptor: UpdateDescriptor,
    attempt_dir: Path,
    *,
    open_url=_safe_open_url,
    deadline_at: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
) -> DownloadedWheel:
    """Stream, fsync and inspect the exact retained target through its O_RDWR FD."""

    if not isinstance(descriptor, UpdateDescriptor):
        raise DownloadError("update eligibility changed")
    installation = descriptor.installation
    if automatic_update_descriptor(
        installed=descriptor.installed, target=descriptor.target,
        installation=installation,
        local_requirements_sha256=installation.runtime_requirements_sha256,
        running_python=installation.running_python, platform=installation.platform,
    ) != descriptor:
        raise DownloadError("update eligibility changed")
    if deadline_at is None:
        deadline_at = monotonic() + DISCOVERY_DEADLINE_SECONDS
    if type(deadline_at) not in (int, float) or not math.isfinite(deadline_at):
        raise DownloadError("download deadline is invalid")

    def checkpoint():
        if cancelled() or monotonic() >= deadline_at:
            raise DownloadError("download canceled or expired")

    parent = fd = -1
    response = None
    created = None
    target = descriptor.target
    name = target.wheel_asset.name
    try:
        checkpoint()
        response = open_release_asset(
            target.wheel_asset.url, open_url=open_url,
            deadline_at=deadline_at, monotonic=monotonic,
        )
        length = _single_header(response.headers, "Content-Length")
        media = _single_header(response.headers, "Content-Type")
        if (response.status != 200 or type(length) is not str
                or not length.isascii() or not length.isdecimal()
                or len(length) > 10 or length.startswith("0")
                or int(length) != target.wheel_asset.size
                or not 0 < int(length) <= WHEEL_BYTE_LIMIT
                or media not in {"application/octet-stream", "application/zip"}):
            raise DownloadError("download response is invalid")
        checkpoint()
        parent = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parent_identity = _directory_identity(os.fstat(parent))
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        created = os.fstat(fd)
        _private_file(created)
        digest = hashlib.sha256()
        total = 0
        while True:
            checkpoint()
            chunk = response.read(min(64 * 1024, target.wheel_asset.size - total + 1))
            if type(chunk) is not bytes:
                raise DownloadError("download response is invalid")
            if not chunk:
                break
            total += len(chunk)
            if total > target.wheel_asset.size:
                raise DownloadError("download size does not match")
            digest.update(chunk)
            pending = memoryview(chunk)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise DownloadError("download write failed")
                pending = pending[written:]
        if total != target.wheel_asset.size or digest.hexdigest() != target.wheel_asset.sha256:
            raise DownloadError("download integrity does not match")
        os.fsync(fd)
        checkpoint()
        os.lseek(fd, 0, os.SEEK_SET)
        inspected = inspect_update_wheel_descriptor(fd, filename=name, expected_version=target.version)
        if (inspected.wheel != target.manifest.wheel
                or inspected.python != target.manifest.python
                or inspected.runtime_requirements_sha256 != installation.runtime_requirements_sha256):
            raise DownloadError("downloaded wheel is incompatible")
        info = os.fstat(fd)
        _private_file(info)
        identity = file_identity(info)
        if file_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
            raise DownloadError("download identity changed")
        if _directory_identity(os.stat(attempt_dir, follow_symlinks=False)) != parent_identity:
            raise DownloadError("download directory changed")
        checkpoint()
        os.fsync(parent)
        retain_target_manifest(descriptor, Path(attempt_dir))
        return DownloadedWheel(Path(attempt_dir) / name, inspected, identity)
    except Exception as error:
        if parent >= 0 and created is not None:
            _unlink_owned(parent, name, created)
        if isinstance(error, DownloadError):
            raise
        raise DownloadError("update download could not be verified") from error
    finally:
        if fd >= 0:
            os.close(fd)
        if parent >= 0:
            os.close(parent)
        if response is not None:
            response.close()
