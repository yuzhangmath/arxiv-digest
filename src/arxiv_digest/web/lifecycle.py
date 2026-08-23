"""Deterministic tab, worker, and graceful-shutdown lifecycle state."""

from __future__ import annotations

import fcntl
import base64
import binascii
import http.client
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from arxiv_digest.atomic import atomic_write


INACTIVITY_SECONDS = 30 * 60
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
        self._lock_fd: int | None = None
        self._published: RuntimeDescriptor | None = None

    def _open_lock(self) -> int:
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                self.lock_path,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                descriptor = os.open(self.lock_path, flags)
            except OSError as error:
                raise InstanceSecurityError("process lock is not private") from error
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600
            ):
                os.close(descriptor)
                raise InstanceSecurityError("process lock is not private")
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _read_existing(self) -> RuntimeDescriptor:
        try:
            status = self.descriptor_path.lstat()
        except FileNotFoundError as error:
            raise InstanceSecurityError("running process has no descriptor") from error
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise InstanceSecurityError("runtime descriptor is not private")
        descriptor = _decode_descriptor(self.descriptor_path.read_bytes())
        if not self._pid_alive(descriptor.pid):
            raise InstanceSecurityError("runtime descriptor process is not alive")
        if not self._health_probe(descriptor):
            raise InstanceSecurityError("runtime health verification failed")
        return descriptor

    def acquire(self) -> OwnedInstance | ExistingInstance:
        if self._lock_fd is not None:
            raise RuntimeError("single-instance lock is already owned")
        descriptor = self._open_lock()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return ExistingInstance(self._read_existing())
        self._lock_fd = descriptor
        return OwnedInstance(self)

    def _publish(
        self,
        *,
        port: int,
        startup_nonce: str,
        token: str,
    ) -> RuntimeDescriptor:
        if self._lock_fd is None:
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
        self._published = descriptor
        return descriptor

    def release(self) -> None:
        if self._lock_fd is None:
            return
        if self._published is not None:
            try:
                current = _decode_descriptor(self.descriptor_path.read_bytes())
            except (FileNotFoundError, InstanceSecurityError):
                current = None
            if current == self._published:
                self.descriptor_path.unlink(missing_ok=True)
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None
        self._published = None


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
        self._quit_requested = False
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

    def request_quit(self) -> None:
        with self._state_lock:
            self._quit_requested = True

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
            if self._quit_requested:
                return self._transactions == 0
            self._expire_leases()
            return (
                not self._has_workers()
                and not self._leases
                and self._idle_since is not None
                and self._clock() - self._idle_since >= self._inactivity_seconds
            )
