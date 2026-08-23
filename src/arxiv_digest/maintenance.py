"""In-process barrier for atomic maintenance and cancellable workers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class MaintenanceError(RuntimeError):
    pass


class WorkActiveError(MaintenanceError):
    pass


class MaintenanceTimeoutError(MaintenanceError):
    pass


class MaintenanceBarrier:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_operations = 0
        self._workers: dict[str, Callable[[], None]] = {}
        self._exclusive_pending = False
        self._exclusive_owner: int | None = None
        self._local = threading.local()

    @property
    def work_active(self) -> bool:
        with self._condition:
            return bool(self._workers)

    @property
    def active_operations(self) -> int:
        with self._condition:
            return self._active_operations

    @contextmanager
    def operation(self) -> Iterator[None]:
        owner = threading.get_ident()
        depth = int(getattr(self._local, "operation_depth", 0))
        worker_depth = int(getattr(self._local, "worker_depth", 0))
        counted = False
        if depth == 0:
            with self._condition:
                while (
                    self._exclusive_pending and worker_depth == 0
                    or self._exclusive_owner is not None
                    and self._exclusive_owner != owner
                ):
                    self._condition.wait()
                if self._exclusive_owner != owner:
                    self._active_operations += 1
                    counted = True
        self._local.operation_depth = depth + 1
        try:
            yield
        finally:
            self._local.operation_depth = depth
            if counted:
                with self._condition:
                    self._active_operations -= 1
                    self._condition.notify_all()

    @contextmanager
    def worker(
        self, worker_id: str, request_cancel: Callable[[], None]
    ) -> Iterator[None]:
        if not worker_id.strip():
            raise ValueError("worker ID must not be blank")
        if not callable(request_cancel):
            raise TypeError("worker cancellation callback must be callable")
        depth = int(getattr(self._local, "worker_depth", 0))
        with self._condition:
            while self._exclusive_pending or self._exclusive_owner is not None:
                self._condition.wait()
            if worker_id in self._workers:
                raise ValueError("worker ID is already registered")
            self._workers[worker_id] = request_cancel
        self._local.worker_depth = depth + 1
        try:
            yield
        finally:
            self._local.worker_depth = depth
            with self._condition:
                self._workers.pop(worker_id, None)
                self._condition.notify_all()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    @contextmanager
    def exclusive(
        self,
        *,
        cancel_active: bool = False,
        timeout: float | None = None,
    ) -> Iterator[None]:
        if timeout is not None and timeout < 0:
            raise ValueError("maintenance timeout must be nonnegative")
        owner = threading.get_ident()
        deadline = None if timeout is None else time.monotonic() + timeout
        callbacks: tuple[Callable[[], None], ...] = ()
        with self._condition:
            if self._exclusive_owner == owner:
                raise MaintenanceError("exclusive maintenance is not reentrant")
            while self._exclusive_pending or self._exclusive_owner is not None:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise MaintenanceTimeoutError(
                        "timed out waiting for maintenance ownership"
                    )
                self._condition.wait(remaining)
            if self._workers and not cancel_active:
                raise WorkActiveError("cancellable work is active")
            self._exclusive_pending = True
            callbacks = tuple(self._workers.values())

        try:
            for callback in callbacks:
                callback()
            with self._condition:
                while self._workers or self._active_operations:
                    remaining = self._remaining(deadline)
                    if remaining is not None and remaining <= 0:
                        raise MaintenanceTimeoutError(
                            "timed out waiting for active work to stop"
                        )
                    self._condition.wait(remaining)
                self._exclusive_owner = owner
                self._exclusive_pending = False
                self._condition.notify_all()
            try:
                yield
            finally:
                with self._condition:
                    self._exclusive_owner = None
                    self._condition.notify_all()
        except Exception:
            with self._condition:
                if self._exclusive_owner == owner:
                    self._exclusive_owner = None
                self._exclusive_pending = False
                self._condition.notify_all()
            raise
