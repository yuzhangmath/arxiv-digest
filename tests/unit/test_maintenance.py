from __future__ import annotations

import threading
from contextlib import ExitStack

import pytest


def test_shutdown_closes_admission_and_waits_for_worker_cleanup_after_timeout():
    from arxiv_digest.maintenance import MaintenanceBarrier, MaintenanceError, MaintenanceTimeoutError
    barrier = MaintenanceBarrier()
    started, canceled, finish = threading.Event(), threading.Event(), threading.Event()
    cleaned = []
    def run():
        with barrier.worker("retained-worker", canceled.set):
            started.set()
            assert finish.wait(2)
            with barrier.operation():
                cleaned.append(True)
    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(2)
    try:
        with pytest.raises(MaintenanceTimeoutError):
            barrier.drain_for_shutdown(timeout=0.01)
        assert canceled.is_set() and barrier.work_active
        with pytest.raises(MaintenanceError):
            barrier.reserve_worker("new-worker", lambda: None)
        finish.set()
        barrier.drain_for_shutdown(timeout=2)
        assert cleaned == [True]
    finally:
        finish.set()
        worker.join(2)


def test_shutdown_can_observe_an_update_lease_owned_by_the_coordinator():
    from arxiv_digest.maintenance import MaintenanceBarrier
    barrier = MaintenanceBarrier()
    owned, release = threading.Event(), threading.Event()
    def coordinator():
        with barrier.update_latch(), barrier.exclusive(cancel_active=True, timeout=1):
            owned.set()
            assert release.wait(2)
    thread = threading.Thread(target=coordinator)
    thread.start()
    assert owned.wait(2)
    try:
        barrier.drain_for_shutdown(timeout=0.01)
        assert barrier.update_active
    finally:
        release.set()
        thread.join(2)


def test_restore_reports_active_worker_without_requesting_cancellation() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier, WorkActiveError

    barrier = MaintenanceBarrier()
    cancelled: list[str] = []
    with barrier.worker("sync-1", lambda: cancelled.append("sync-1")):
        with pytest.raises(WorkActiveError):
            with barrier.exclusive(cancel_active=False):
                raise AssertionError("exclusive lease must not be entered")

    assert cancelled == []
    with barrier.operation():
        pass


def test_explicit_cancel_waits_for_worker_terminal_unregister() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    started = threading.Event()
    cancel_requested = threading.Event()
    unregistered = threading.Event()

    def worker() -> None:
        with barrier.worker("download-1", cancel_requested.set):
            started.set()
            cancel_requested.wait(2)
        unregistered.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(2)

    with barrier.exclusive(cancel_active=True, timeout=2):
        assert cancel_requested.is_set()
        assert unregistered.is_set()

    thread.join(2)
    assert not thread.is_alive()


def test_cancelled_worker_can_finish_a_store_lease_without_deadlocking_restore() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    started = threading.Event()
    cancel_requested = threading.Event()
    cleanup_finished = threading.Event()

    def worker() -> None:
        with barrier.worker("download-1", cancel_requested.set):
            started.set()
            cancel_requested.wait(2)
            # A worker can reach a Store call just as exclusive maintenance
            # becomes pending. It must finish before restore ownership rather
            # than deadlock with each side waiting for the other.
            with barrier.operation():
                cleanup_finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(2)

    with barrier.exclusive(cancel_active=True, timeout=2):
        assert cleanup_finished.is_set()

    thread.join(2)
    assert not thread.is_alive()


def test_exclusive_waits_for_store_operation_and_blocks_new_ones() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    first_started = threading.Event()
    enter_nested = threading.Event()
    nested_entered = threading.Event()
    release_first = threading.Event()
    exclusive_entered = threading.Event()
    release_exclusive = threading.Event()
    second_entered = threading.Event()

    def first_operation() -> None:
        with barrier.operation():
            first_started.set()
            enter_nested.wait(2)
            with barrier.operation():
                nested_entered.set()
            release_first.wait(2)

    def restore() -> None:
        with barrier.exclusive(timeout=2):
            exclusive_entered.set()
            release_exclusive.wait(2)

    def second_operation() -> None:
        with barrier.operation():
            second_entered.set()

    first = threading.Thread(target=first_operation)
    first.start()
    assert first_started.wait(2)
    restore_thread = threading.Thread(target=restore)
    restore_thread.start()
    second = threading.Thread(target=second_operation)
    second.start()
    assert not exclusive_entered.wait(0.05)
    assert not second_entered.is_set()
    enter_nested.set()
    assert nested_entered.wait(2)

    release_first.set()
    assert exclusive_entered.wait(2)
    assert not second_entered.is_set()
    release_exclusive.set()

    for thread in (first, restore_thread, second):
        thread.join(2)
        assert not thread.is_alive()
    assert second_entered.is_set()


def test_duplicate_worker_ids_and_exclusive_timeout_are_safe() -> None:
    from arxiv_digest.maintenance import (
        MaintenanceBarrier,
        MaintenanceTimeoutError,
    )

    barrier = MaintenanceBarrier()
    with ExitStack() as stack:
        stack.enter_context(barrier.worker("sync-1", lambda: None))
        with pytest.raises(ValueError, match="already registered"):
            stack.enter_context(barrier.worker("sync-1", lambda: None))
        with pytest.raises(MaintenanceTimeoutError):
            with barrier.exclusive(cancel_active=True, timeout=0):
                pass


def test_store_connections_hold_operation_leases_until_close(tmp_path) -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.storage.database import open_database
    from arxiv_digest.storage.store import Store

    database_path = tmp_path / "state.sqlite3"
    open_database(database_path).close()
    barrier = MaintenanceBarrier()
    store = Store(database_path, maintenance=barrier)
    reader_entered = threading.Event()

    def reader() -> None:
        store.list_review_dates()
        reader_entered.set()

    with barrier.exclusive(timeout=2):
        assert store.list_review_dates() == ()
        thread = threading.Thread(target=reader)
        thread.start()
        assert not reader_entered.wait(0.05)
    thread.join(2)

    assert reader_entered.is_set()
    assert not thread.is_alive()


def test_update_latch_refuses_unadmitted_work_and_drains_a_complete_handler() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier, UpdateInProgressError

    barrier = MaintenanceBarrier()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []

    def admitted_handler() -> None:
        try:
            with barrier.handler():
                entered.set()
                assert release.wait(2)
                # Admission covers the whole handler, including work after the
                # latch is set and before its first storage call.
                with barrier.operation():
                    finished.set()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=admitted_handler)
    thread.start()
    assert entered.wait(2)
    with barrier.update_latch():
        with pytest.raises(UpdateInProgressError):
            with barrier.handler():
                pass
        with pytest.raises(UpdateInProgressError):
            barrier.reserve_worker("late-worker", lambda: None)
        release.set()
        with barrier.exclusive(cancel_active=True, timeout=2) as canceled:
            assert finished.is_set()
            assert canceled == ()
        assert barrier.update_active
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    assert not barrier.update_active


def test_reserved_worker_is_canceled_before_its_thread_starts() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    canceled = threading.Event()
    reservation = barrier.reserve_worker("pdf_reserved", canceled.set)
    with barrier.update_latch():
        def run() -> None:
            assert canceled.wait(2)
            with reservation.activate():
                with barrier.operation():
                    pass
        thread = threading.Thread(target=run)
        thread.start()
        with barrier.exclusive(cancel_active=True, timeout=2) as identifiers:
            assert identifiers == ("pdf_reserved",)
            assert barrier.canceled_worker_ids == identifiers
    thread.join(2)
    assert not thread.is_alive()
    assert not barrier.work_active


def test_baseexception_during_cancel_unwinds_pending_and_update_latch() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    def interrupt() -> None:
        raise KeyboardInterrupt
    reservation = barrier.reserve_worker("worker-interrupted", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            with barrier.update_latch():
                with barrier.exclusive(cancel_active=True, timeout=0):
                    pass
        assert not barrier.update_active
    finally:
        reservation.close()
    with barrier.exclusive(timeout=0):
        pass


def test_update_latch_cannot_clear_while_its_exclusive_lease_remains() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier, MaintenanceError

    barrier = MaintenanceBarrier()
    latch = barrier.update_latch()
    latch.__enter__()
    exclusive = barrier.exclusive(timeout=0)
    exclusive.__enter__()
    try:
        with pytest.raises(MaintenanceError, match="exclusive"):
            latch.__exit__(None, None, None)
        assert barrier.update_active
    finally:
        exclusive.__exit__(None, None, None)


def test_update_timeout_does_not_keep_latch_for_another_pending_exclusive_owner() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier, MaintenanceTimeoutError

    barrier = MaintenanceBarrier()
    cancel = threading.Event()
    release = threading.Event()
    errors = []
    reservation = barrier.reserve_worker("existing-worker", cancel.set)
    def ordinary_restore():
        try:
            with barrier.exclusive(cancel_active=True, timeout=2):
                pass
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=ordinary_restore)
    thread.start()
    assert cancel.wait(2)
    with pytest.raises(MaintenanceTimeoutError):
        with barrier.update_latch():
            with barrier.exclusive(cancel_active=True, timeout=0):
                pass
    assert not barrier.update_active
    reservation.close()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []


def test_exception_cleanup_cannot_clear_a_later_threads_pending_lease() -> None:
    from arxiv_digest.maintenance import MaintenanceBarrier

    barrier = MaintenanceBarrier()
    release_operation = threading.Event()
    begin_operation = threading.Event()
    operation_entered = threading.Event()
    begin_second = threading.Event()
    second_pending = threading.Event()
    errors = []
    main_owner = threading.get_ident()

    class ReleaseBoundary(threading.Condition):
        after_release = None

        def __exit__(self, *args):
            result = super().__exit__(*args)
            if self.after_release is not None and threading.get_ident() == main_owner:
                callback, self.after_release = self.after_release, None
                callback()
            return result

        def wait(self, timeout=None):
            if barrier._exclusive_pending_owner == threading.get_ident():
                second_pending.set()
            return super().wait(timeout)

    condition = ReleaseBoundary()
    barrier._condition = condition

    def operation():
        assert begin_operation.wait(2)
        with barrier.operation():
            operation_entered.set()
            assert release_operation.wait(2)

    def second_exclusive():
        assert begin_second.wait(2)
        try:
            with barrier.exclusive(timeout=2):
                pass
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=operation)
    second = threading.Thread(target=second_exclusive)
    reader.start()
    second.start()

    def after_first_release():
        begin_operation.set()
        assert operation_entered.wait(2)
        begin_second.set()
        assert second_pending.wait(2)

    try:
        with pytest.raises(RuntimeError, match="first operation failed"):
            with barrier.exclusive(timeout=0):
                condition.after_release = after_first_release
                raise RuntimeError("first operation failed")
        with condition:
            assert barrier._exclusive_pending
            assert barrier._exclusive_pending_owner == second.ident
    finally:
        release_operation.set()
        begin_operation.set()
        begin_second.set()
        reader.join(2)
        second.join(2)
    assert not reader.is_alive()
    assert not second.is_alive()
    assert errors == []
