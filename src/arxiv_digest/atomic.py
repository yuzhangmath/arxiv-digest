from __future__ import annotations

import errno
import fcntl
import math
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path



def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("private path must be a directory")
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                "private directory must be owned by the current user"
            )
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)



def ensure_private_directory_strict(path: Path) -> None:
    """Create or validate an exact private directory without repairing it."""

    flags = (
        os.O_RDONLY
        | _required_open_flag("O_DIRECTORY")
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = path.lstat()
    _validate_private_directory(before)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        after = path.lstat()
        for entry in (metadata, after):
            _validate_private_directory(entry)
            if _private_identity(entry) != _private_identity(before):
                raise PermissionError("private directory path identity changed")
        if os.get_inheritable(descriptor):
            raise PermissionError("private directory descriptor must be close-on-exec")
    finally:
        os.close(descriptor)



def _required_open_flag(name: str) -> int:
    flag = getattr(os, name, 0)
    if not flag:
        raise OSError(errno.ENOTSUP, f"private no-follow open is unsupported: {name}")
    return flag



def _private_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode



def _validate_private_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PermissionError("private directory must be owned mode 0700")



def _validate_private_lock_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise PermissionError("private lock file must be owned mode 0600")



def open_private_lock_file(path: Path) -> int:
    """Return a stable no-follow coordination descriptor, owned by the caller."""

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_NONBLOCK
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None:
        _validate_private_lock_file(before)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        after = path.lstat()
        _validate_private_lock_file(metadata)
        _validate_private_lock_file(after)
        if (
            _private_identity(metadata) != _private_identity(after)
            or (before is not None and _private_identity(before) != _private_identity(metadata))
        ):
            raise PermissionError("private lock path identity changed")
        if os.get_inheritable(descriptor):
            raise PermissionError("private lock descriptor must be close-on-exec")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise



def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)



@contextmanager
def exclusive_flock(path: Path) -> Iterator[None]:
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("lock target must be a regular file")
        if metadata.st_uid != os.getuid():
            raise PermissionError("lock file must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("lock file permissions must be 0600")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)



class ExclusiveLock:
    """A private process lock released by its original owner."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)



def acquire_exclusive(path: Path, *, timeout: float = 0) -> ExclusiveLock:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be finite and nonnegative")
    ensure_private_directory_strict(path.parent)
    descriptor = open_private_lock_file(path)
    identity = _private_identity(os.fstat(descriptor))
    deadline = time.monotonic() + timeout

    def validate() -> None:
        for metadata in (os.fstat(descriptor), path.lstat()):
            _validate_private_lock_file(metadata)
            if _private_identity(metadata) != identity:
                raise PermissionError("lock path identity changed")

    try:
        while True:
            validate()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                validate()
                return ExclusiveLock(descriptor)
            except (BlockingIOError, InterruptedError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for lock")
                time.sleep(min(0.005, remaining))
    except BaseException:
        os.close(descriptor)
        raise
