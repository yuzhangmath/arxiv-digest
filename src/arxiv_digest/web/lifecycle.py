"""Deterministic tab, worker, and graceful-shutdown lifecycle state."""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from arxiv_digest.atomic import (
    ExclusiveLock, acquire_exclusive, atomic_write, open_private_read_file,
    validate_private_file,
)


INACTIVITY_SECONDS = 3 * 60
TAB_LEASE_SECONDS = 90



class InstanceSecurityError(RuntimeError):
    """Runtime ownership metadata could not be trusted."""



@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    pid: int
    port: int
    startup_nonce: str
    token: str
    started_at: str



@dataclass(frozen=True, slots=True)
class ExistingInstance:
    descriptor: RuntimeDescriptor



class OwnedInstance:
    def __init__(self, coordinator: "SingleInstance") -> None:
        self._coordinator = coordinator

    def publish(
        self,
        *,
        port: int,
        startup_nonce: str,
        token: str,
    ) -> RuntimeDescriptor:
        return self._coordinator._publish(
            port=port,
            startup_nonce=startup_nonce,
            token=token,
        )



def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import win32api
        import win32con
        import win32event

        # os.kill(pid, 0) calls TerminateProcess on Windows.
        try:
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        except win32api.error as error:
            if error.winerror == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return False
            if error.winerror == 5:  # ERROR_ACCESS_DENIED: process exists.
                return True
            raise OSError("process liveness could not be checked") from error
        try:
            return win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT
        finally:
            handle.Close()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True



def _health_probe(descriptor: RuntimeDescriptor) -> bool:
    connection = http.client.HTTPConnection(
        "127.0.0.1", descriptor.port, timeout=2
    )
    try:
        connection.request(
            "GET",
            "/api/v1/status",
            headers={
                "Host": f"127.0.0.1:{descriptor.port}",
                "Authorization": f"Bearer {descriptor.token}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read(64 * 1024 + 1))
    except Exception:
        return False
    finally:
        connection.close()
    return (
        response.status == 200
        and payload.get("ok") is True
        and payload.get("data", {}).get("startup_nonce")
        == descriptor.startup_nonce
    )



def _valid_token(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(
        r"[A-Za-z0-9_-]{43}", value
    ) is None:
        return False
    try:
        decoded = base64.b64decode(
            value + "=", altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 32



def _decode_descriptor(payload: bytes) -> RuntimeDescriptor:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise InstanceSecurityError("runtime descriptor has duplicate keys")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstanceSecurityError("runtime descriptor is invalid") from error
    expected = {"pid", "port", "startup_nonce", "token", "started_at"}
    if not isinstance(value, dict) or set(value) != expected:
        raise InstanceSecurityError("runtime descriptor schema is invalid")
    if (
        type(value["pid"]) is not int
        or value["pid"] < 1
        or type(value["port"]) is not int
        or not 1 <= value["port"] <= 65535
        or not isinstance(value["startup_nonce"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value["startup_nonce"])
        is None
        or not _valid_token(value["token"])
        or not isinstance(value["started_at"], str)
    ):
        raise InstanceSecurityError("runtime descriptor values are invalid")
    return RuntimeDescriptor(**value)



def _is_private_file(metadata: os.stat_result) -> bool:
    if os.name == "nt":
        # Ownership and DACL are checked on the no-follow handle after opening.
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and not metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    )



def _file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )



def _read_private_descriptor(
    path: Path, *, expected_identity: tuple[int, int] | None = None,
) -> RuntimeDescriptor:
    if os.name != "nt" and (
        not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_CLOEXEC", 0)
    ):
        raise InstanceSecurityError("private runtime descriptor reads are unsupported")
    try:
        initial = path.lstat()
        if not _is_private_file(initial):
            raise InstanceSecurityError("runtime descriptor is not private")
        if expected_identity is not None and (
            initial.st_dev, initial.st_ino
        ) != expected_identity:
            raise InstanceSecurityError("runtime descriptor path changed")
        descriptor = open_private_read_file(path) if os.name == "nt" else os.open(
            path,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
        )
    except FileNotFoundError as error:
        raise InstanceSecurityError("running process has no descriptor") from error
    except InstanceSecurityError:
        raise
    except OSError as error:
        raise InstanceSecurityError("runtime descriptor is not private") from error
    try:
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(initial):
            raise InstanceSecurityError("runtime descriptor path changed")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024 - len(payload) + 1)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 64 * 1024:
                raise InstanceSecurityError("runtime descriptor is too large")
        final = os.fstat(descriptor)
        validate_private_file(descriptor)
        current = path.lstat()
        if (
            _file_snapshot(final) != _file_snapshot(opened)
            or _file_snapshot(current) != _file_snapshot(opened)
        ):
            raise InstanceSecurityError("runtime descriptor path changed")
        return _decode_descriptor(bytes(payload))
    except InstanceSecurityError:
        raise
    except OSError as error:
        raise InstanceSecurityError("runtime descriptor path changed") from error
    finally:
        os.close(descriptor)



class SingleInstance:
    def __init__(
        self,
        lock_path: Path,
        descriptor_path: Path,
        *,
        pid_alive: Callable[[int], bool] = _pid_alive,
        health_probe: Callable[[RuntimeDescriptor], bool] = _health_probe,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.lock_path = lock_path
        self.descriptor_path = descriptor_path
        self._pid_alive = pid_alive
        self._health_probe = health_probe
        self._wall_clock = wall_clock
        self._lock: ExclusiveLock | None = None
        self._published: RuntimeDescriptor | None = None
        self._published_identity: tuple[int, int] | None = None

    def _read_existing(self) -> RuntimeDescriptor:
        descriptor = _read_private_descriptor(self.descriptor_path)
        if not self._pid_alive(descriptor.pid):
            raise InstanceSecurityError("runtime descriptor process is not alive")
        if not self._health_probe(descriptor):
            raise InstanceSecurityError("runtime health verification failed")
        return descriptor

    def acquire(self) -> OwnedInstance | ExistingInstance:
        if self._lock is not None:
            raise RuntimeError("single-instance lock is already owned")
        try:
            lock = acquire_exclusive(self.lock_path, timeout=0.0)
        except TimeoutError:
            return ExistingInstance(self._read_existing())
        except (OSError, PermissionError) as error:
            raise InstanceSecurityError("process lock is not private") from error
        self._lock = lock
        return OwnedInstance(self)

    def _publish(
        self,
        *,
        port: int,
        startup_nonce: str,
        token: str,
    ) -> RuntimeDescriptor:
        if self._lock is None:
            raise RuntimeError("cannot publish without process ownership")
        if (
            not 1 <= port <= 65535
            or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", startup_nonce) is None
            or not _valid_token(token)
        ):
            raise ValueError("runtime descriptor values are invalid")
        started_at = self._wall_clock().astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        descriptor = RuntimeDescriptor(
            pid=os.getpid(),
            port=port,
            startup_nonce=startup_nonce,
            token=token,
            started_at=started_at,
        )
        payload = (
            json.dumps(
                {
                    "pid": descriptor.pid,
                    "port": descriptor.port,
                    "startup_nonce": descriptor.startup_nonce,
                    "token": descriptor.token,
                    "started_at": descriptor.started_at,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        atomic_write(self.descriptor_path, payload, mode=0o600)
        metadata = self.descriptor_path.lstat()
        self._published_identity = (metadata.st_dev, metadata.st_ino)
        self._published = descriptor
        return descriptor

    def release(self) -> None:
        if self._lock is None:
            return
        try:
            if self._published is not None:
                try:
                    current = _read_private_descriptor(
                        self.descriptor_path,
                        expected_identity=self._published_identity,
                    )
                except InstanceSecurityError:
                    current = None
                if current == self._published:
                    self.descriptor_path.unlink(missing_ok=True)
        finally:
            try:
                self._lock.release()
            finally:
                self._lock = None
                self._published = None
                self._published_identity = None



class LifecycleController:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        inactivity_seconds: float = INACTIVITY_SECONDS,
        lease_seconds: float = TAB_LEASE_SECONDS,
    ) -> None:
        self._clock = clock
        self._inactivity_seconds = inactivity_seconds
        self._lease_seconds = lease_seconds
        self._leases: dict[str, float] = {}
        self._workers: dict[str, set[str]] = {
            "sync": set(),
            "download": set(),
        }
        self._transactions = 0
        self._closing = False
        self._idle_since: float | None = clock()
        self._state_lock = threading.RLock()

    def connect(self, tab_id: str) -> None:
        with self._state_lock:
            self._leases[tab_id] = self._clock()
            self._idle_since = None

    def heartbeat(self, tab_id: str) -> None:
        with self._state_lock:
            if tab_id not in self._leases:
                raise KeyError("unknown tab lease")
            self._leases[tab_id] = self._clock()

    def disconnect(self, tab_id: str) -> None:
        with self._state_lock:
            self._leases.pop(tab_id, None)
            if not self._leases and not self._has_workers():
                self._idle_since = self._clock()

    def _has_workers(self) -> bool:
        return any(self._workers.values())

    def worker_started(self, kind: str, job_id: str) -> None:
        with self._state_lock:
            if kind not in self._workers:
                raise ValueError("worker kind must be sync or download")
            if self._closing:
                raise RuntimeError("application is closing")
            self._workers[kind].add(job_id)
            self._idle_since = None

    def worker_finished(self, kind: str, job_id: str) -> None:
        with self._state_lock:
            if kind not in self._workers:
                raise ValueError("worker kind must be sync or download")
            # Expired browser leases cannot make a completed worker inherit an
            # already-elapsed idle deadline. The worker owns liveness until
            # this instant, so a fresh deadline begins here.
            self._expire_leases()
            self._workers[kind].discard(job_id)
            if not self._has_workers() and not self._leases:
                self._idle_since = self._clock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._state_lock:
            self._transactions += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._transactions -= 1

    @property
    def is_closing(self) -> bool:
        with self._state_lock:
            return self._closing

    def request_quit(self) -> bool:
        with self._state_lock:
            if self._closing:
                return False
            self._closing = True
            return True

    def _expire_leases(self) -> None:
        now = self._clock()
        expired = {
            tab_id: heartbeat + self._lease_seconds
            for tab_id, heartbeat in self._leases.items()
            if now >= heartbeat + self._lease_seconds
        }
        for tab_id in expired:
            del self._leases[tab_id]
        if (
            expired
            and not self._leases
            and not self._has_workers()
            and self._idle_since is None
        ):
            self._idle_since = max(expired.values())

    def should_stop(self) -> bool:
        with self._state_lock:
            if self._closing:
                return self._transactions == 0
            self._expire_leases()
            return (
                not self._has_workers()
                and not self._leases
                and self._idle_since is not None
                and self._clock() - self._idle_since >= self._inactivity_seconds
            )
