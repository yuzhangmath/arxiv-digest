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



class WorkerReservation:
    """A worker registered before starting its thread, with close-only cleanup."""

    def __init__(
        self, barrier: MaintenanceBarrier, worker_id: str,
        request_cancel: Callable[[], None],
    ) -> None:
        self._barrier = barrier
        self.worker_id = worker_id
        self.request_cancel = request_cancel
        self._active = False
        self._closed = False
        self._thread: threading.Thread | None = None

    @contextmanager
    def activate(self) -> Iterator[None]:
        barrier = self._barrier
        with barrier._condition:
            if self._closed or self._active:
                raise MaintenanceError("worker reservation is no longer available")
            self._active = True
            self._thread = threading.current_thread()
            if barrier._closing:
                barrier._shutdown_threads.add(self._thread)
        depth = int(getattr(barrier._local, "worker_depth", 0))
        barrier._local.worker_depth = depth + 1
        try:
            yield
        finally:
            barrier._local.worker_depth = depth
            with barrier._condition:
                self._active = False
                self.close()

    def close(self) -> None:
        with self._barrier._condition:
            if self._active:
                raise MaintenanceError("cannot close an active worker reservation")
            if not self._closed:
                self._barrier._workers.pop(self.worker_id, None)
                self._closed = True
                self._barrier._condition.notify_all()



class MaintenanceBarrier:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_operations = 0
        self._workers: dict[str, WorkerReservation] = {}
        self._exclusive_pending = False
        self._exclusive_pending_owner: int | None = None
        self._exclusive_owner: int | None = None
        self._local = threading.local()
        self._closing = False
        self._shutdown_threads: set[threading.Thread] = set()

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
                while True:
                    if self._closing and not worker_depth:
                        raise MaintenanceError("application is closing")
                    if not (
                        self._exclusive_pending and worker_depth == 0
                        or self._exclusive_owner is not None
                        and self._exclusive_owner != owner
                    ):
                        break
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

    def reserve_worker(
        self, worker_id: str, request_cancel: Callable[[], None],
    ) -> WorkerReservation:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker ID must not be blank")
        if not callable(request_cancel):
            raise TypeError("worker cancellation callback must be callable")
        with self._condition:
            while True:
                if self._closing:
                    raise MaintenanceError("application is closing")
                if not self._exclusive_pending and self._exclusive_owner is None:
                    break
                self._condition.wait()
            if worker_id in self._workers:
                raise ValueError("worker ID is already registered")
            reservation = WorkerReservation(self, worker_id, request_cancel)
            self._workers[worker_id] = reservation
            return reservation

    @contextmanager
    def worker(
        self, worker_id: str, request_cancel: Callable[[], None]
    ) -> Iterator[None]:
        reservation = self.reserve_worker(worker_id, request_cancel)
        with reservation.activate():
            yield

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def drain_for_shutdown(self, *, timeout: float) -> None:
        """Close admissions, cancel workers, and join their actual threads.

        A timeout keeps admissions closed and retains unfinished
        threads so the owner can retry while still holding its process lock.
        """
        if timeout < 0 or getattr(self._local, "worker_depth", 0):
            raise MaintenanceError("shutdown requires a non-worker owner")
        deadline = time.monotonic() + timeout
        with self._condition:
            self._closing = True
            reservations = tuple(self._workers.values())
            self._shutdown_threads.update(item._thread for item in reservations if item._thread is not None)
            self._condition.notify_all()
        for reservation in reservations:
            try:
                reservation.request_cancel()
            except Exception:
                # A failed callback cannot authorize early lock release. The
                # registration remains authoritative until its worker exits.
                pass
        with self._condition:
            while self._workers or self._active_operations:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    raise MaintenanceTimeoutError("waiting for shutdown work to finish")
                self._condition.wait(remaining)
            threads = tuple(self._shutdown_threads)
        for thread in threads:
            if thread is threading.current_thread():
                raise MaintenanceError("shutdown cannot join its caller")
            thread.join(self._remaining(deadline))
            if thread.is_alive():
                raise MaintenanceTimeoutError("waiting for shutdown thread to finish")
            with self._condition:
                self._shutdown_threads.discard(thread)

    @contextmanager
    def exclusive(
        self,
        *,
        cancel_active: bool = False,
        timeout: float | None = None,
    ) -> Iterator[tuple[str, ...]]:
        if timeout is not None and timeout < 0:
            raise ValueError("maintenance timeout must be nonnegative")
        owner = threading.get_ident()
        deadline = None if timeout is None else time.monotonic() + timeout
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
            self._exclusive_pending_owner = owner
            reservations = tuple(self._workers.values())

        try:
            for reservation in reservations:
                reservation.request_cancel()
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
                self._exclusive_pending_owner = None
                self._condition.notify_all()
            try:
                yield tuple(item.worker_id for item in reservations)
            finally:
                with self._condition:
                    self._exclusive_owner = None
                    self._condition.notify_all()
        except BaseException:
            with self._condition:
                if self._exclusive_owner == owner:
                    self._exclusive_owner = None
                if self._exclusive_pending_owner == owner:
                    self._exclusive_pending = False
                    self._exclusive_pending_owner = None
                self._condition.notify_all()
            raise
