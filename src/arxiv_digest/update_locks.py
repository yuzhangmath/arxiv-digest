"""Private flock handles with explicit owned and borrowed semantics."""

from __future__ import annotations

import fcntl
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from arxiv_digest.atomic import (
    atomic_rename_noreplace,
    ensure_private_directory_strict,
    open_private_lock_file,
)


@dataclass(frozen=True, slots=True)
class LockIdentity:
    device: int
    inode: int
    uid: int
    mode: int


class LockMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class LockTimeoutError(TimeoutError):
    pass


class LockCancelledError(RuntimeError):
    pass


def _identity(metadata: os.stat_result) -> LockIdentity:
    return LockIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _is_private_lock(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    )


class BorrowedLock:
    def __init__(
        self,
        descriptor: int,
        *,
        identity: LockIdentity,
        mode: LockMode,
    ) -> None:
        self._descriptor = descriptor
        self.identity = identity
        self.mode = mode

    def fileno(self) -> int:
        if self._descriptor is None:
            raise ValueError("lock handle is closed")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        os.close(descriptor)

    def adopt_sole_ownership(self) -> "OwnedLock":
        if self._descriptor is None:
            raise ValueError("lock handle is closed")
        descriptor = self._descriptor
        self._descriptor = None
        return OwnedLock(
            descriptor,
            identity=self.identity,
            mode=self.mode,
        )


class OwnedLock:
    def __init__(
        self,
        descriptor: int,
        *,
        identity: LockIdentity,
        mode: LockMode,
    ) -> None:
        self._descriptor = descriptor
        self.identity = identity
        self.mode = mode

    def fileno(self) -> int:
        if self._descriptor is None:
            raise ValueError("lock handle is closed")
        return self._descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def transfer_close_only(self) -> BorrowedLock:
        if self._descriptor is None:
            raise ValueError("lock handle is closed")
        descriptor = self._descriptor
        self._descriptor = None
        return BorrowedLock(
            descriptor,
            identity=self.identity,
            mode=self.mode,
        )


def _open_private_lock(path: Path) -> tuple[int, LockIdentity]:
    ensure_private_directory_strict(path.parent)
    descriptor = open_private_lock_file(path)
    try:
        metadata = os.fstat(descriptor)
        if not _is_private_lock(metadata):
            raise PermissionError("lock file is not private")
        return descriptor, _identity(metadata)
    except BaseException:
        os.close(descriptor)
        raise


def _validate_lock_path(
    path: Path, descriptor: int, identity: LockIdentity,
) -> None:
    descriptor_metadata = os.fstat(descriptor)
    path_metadata = path.lstat()
    if (
        not _is_private_lock(descriptor_metadata)
        or not _is_private_lock(path_metadata)
        or _identity(descriptor_metadata) != identity
        or _identity(path_metadata) != identity
        or os.get_inheritable(descriptor)
    ):
        raise PermissionError("lock path identity changed or is not private")


def _acquire(
    path: Path,
    *,
    timeout: float,
    mode: LockMode,
    cancelled: Callable[[], bool],
) -> OwnedLock:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout
    if cancelled():
        raise LockCancelledError("lock wait was canceled")
    path = Path(path)
    descriptor, identity = _open_private_lock(path)
    operation = fcntl.LOCK_EX if mode is LockMode.EXCLUSIVE else fcntl.LOCK_SH
    attempted = False
    try:
        while True:
            if cancelled():
                raise LockCancelledError("lock wait was canceled")
            # A zero timeout permits one immediate attempt, but never a retry.
            if (attempted or timeout > 0) and time.monotonic() >= deadline:
                raise LockTimeoutError("timed out waiting for lock")
            _validate_lock_path(path, descriptor, identity)
            if cancelled():
                raise LockCancelledError("lock wait was canceled")
            if (attempted or timeout > 0) and time.monotonic() >= deadline:
                raise LockTimeoutError("timed out waiting for lock")
            attempted = True
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                _validate_lock_path(path, descriptor, identity)
                return OwnedLock(
                    descriptor,
                    identity=identity,
                    mode=mode,
                )
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError("timed out waiting for lock")
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
            except InterruptedError:
                continue
    except BaseException:
        os.close(descriptor)
        raise


def acquire_exclusive(
    path: Path,
    *,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> OwnedLock:
    return _acquire(
        path,
        timeout=timeout,
        mode=LockMode.EXCLUSIVE,
        cancelled=cancelled,
    )


def acquire_shared(
    path: Path,
    *,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> OwnedLock:
    return _acquire(
        path,
        timeout=timeout,
        mode=LockMode.SHARED,
        cancelled=cancelled,
    )


def adopt_borrowed(
    descriptor: int,
    *,
    expected_identity: LockIdentity,
    mode: LockMode,
) -> BorrowedLock:
    try:
        metadata = os.fstat(descriptor)
        if (
            not _is_private_lock(metadata)
            or _identity(metadata) != expected_identity
            or not fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        ):
            raise PermissionError("borrowed lock is not private")
        return BorrowedLock(
            descriptor,
            identity=expected_identity,
            mode=mode,
        )
    except BaseException:
        os.close(descriptor)
        raise
