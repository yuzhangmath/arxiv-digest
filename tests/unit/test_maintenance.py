from __future__ import annotations

import threading
from contextlib import ExitStack

import pytest


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
