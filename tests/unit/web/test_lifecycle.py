from __future__ import annotations

import threading

import pytest

from arxiv_digest.update_contract import ShutdownIntent


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_connected_tab_suspends_timer_until_the_last_disconnects() -> None:
    from arxiv_digest.web.lifecycle import INACTIVITY_SECONDS, LifecycleController

    clock = FakeClock()
    lifecycle = LifecycleController(clock=clock)
    lifecycle.connect("tab_abcd1234")

    for _ in range(40):
        clock.advance(60)
        lifecycle.heartbeat("tab_abcd1234")
    assert lifecycle.should_stop() is False

    lifecycle.disconnect("tab_abcd1234")
    clock.advance(INACTIVITY_SECONDS - 1)
    assert lifecycle.should_stop() is False
    clock.advance(1)
    assert lifecycle.should_stop() is True


def test_expired_heartbeat_starts_timer_at_the_lease_expiry_instant() -> None:
    from arxiv_digest.web.lifecycle import (
        INACTIVITY_SECONDS,
        TAB_LEASE_SECONDS,
        LifecycleController,
    )

    clock = FakeClock()
    lifecycle = LifecycleController(clock=clock)
    lifecycle.connect("tab_abcd1234")

    clock.advance(TAB_LEASE_SECONDS + INACTIVITY_SECONDS - 1)
    assert lifecycle.should_stop() is False
    clock.advance(1)
    assert lifecycle.should_stop() is True


def test_sync_and_download_workers_postpone_idle_shutdown() -> None:
    from arxiv_digest.web.lifecycle import INACTIVITY_SECONDS, LifecycleController

    for kind in ("sync", "download"):
        clock = FakeClock()
        lifecycle = LifecycleController(clock=clock)
        lifecycle.worker_started(kind, f"{kind}_abcd1234")

        clock.advance(INACTIVITY_SECONDS * 2)
        assert lifecycle.should_stop() is False

        lifecycle.worker_finished(kind, f"{kind}_abcd1234")
        clock.advance(INACTIVITY_SECONDS)
        assert lifecycle.should_stop() is True


def test_explicit_quit_waits_only_for_inflight_transaction_boundaries() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    lifecycle.worker_started("sync", "sync_abcd1234")

    with lifecycle.transaction():
        lifecycle.request_quit()
        assert lifecycle.should_stop() is False

    assert lifecycle.should_stop() is True


@pytest.mark.parametrize("first", list(ShutdownIntent))
def test_shutdown_intent_is_first_writer_wins(first: ShutdownIntent) -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())

    assert lifecycle.request_shutdown(first) is True
    for repeated in ShutdownIntent:
        assert lifecycle.request_shutdown(repeated) is False
    assert lifecycle.request_quit() is False
    assert lifecycle.shutdown_intent is first


def test_concurrent_shutdown_requests_accept_exactly_one_intent() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    ready = threading.Barrier(2)

    def request(intent: ShutdownIntent) -> tuple[ShutdownIntent, bool]:
        ready.wait(timeout=5)
        return intent, lifecycle.request_shutdown(intent)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, ShutdownIntent))

    accepted = [intent for intent, success in results if success]
    assert accepted == [lifecycle.shutdown_intent]
    assert lifecycle.is_closing is True


@pytest.mark.parametrize("intent", list(ShutdownIntent))
def test_shutdown_waits_until_all_inflight_transactions_drain(
    intent: ShutdownIntent,
) -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    with lifecycle.transaction():
        with lifecycle.transaction():
            assert lifecycle.request_shutdown(intent) is True
            assert lifecycle.should_stop() is False
        assert lifecycle.should_stop() is False
    assert lifecycle.should_stop() is True


@pytest.mark.parametrize("intent", list(ShutdownIntent))
@pytest.mark.parametrize("kind", ["sync", "download"])
def test_closing_lifecycle_rejects_new_workers(
    intent: ShutdownIntent, kind: str,
) -> None:

    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    lifecycle.request_shutdown(intent)

    assert lifecycle.is_closing is True
    with pytest.raises(RuntimeError, match="closing"):
        lifecycle.worker_started(kind, f"{kind}_abcd1234")


def test_worker_completion_starts_a_fresh_timer_after_stale_tab_lease() -> None:
    from arxiv_digest.web.lifecycle import (
        INACTIVITY_SECONDS,
        TAB_LEASE_SECONDS,
        LifecycleController,
    )

    clock = FakeClock()
    lifecycle = LifecycleController(clock=clock)
    lifecycle.connect("tab_abcd1234")
    lifecycle.worker_started("sync", "sync_abcd1234")

    clock.advance(TAB_LEASE_SECONDS + INACTIVITY_SECONDS)
    lifecycle.worker_finished("sync", "sync_abcd1234")

    assert lifecycle.should_stop() is False
    clock.advance(INACTIVITY_SECONDS)
    assert lifecycle.should_stop() is True


def test_dedicated_update_owner_keeps_expired_tabs_alive_and_excludes_quit() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    clock = FakeClock()
    lifecycle = LifecycleController(clock=clock, inactivity_seconds=1, lease_seconds=1)
    lifecycle.connect("update-tab")
    with lifecycle.update_owner("update-job"):
        clock.advance(1000)
        lifecycle.disconnect("update-tab")
        assert lifecycle.should_stop() is False
        assert lifecycle.request_quit() is False
        assert lifecycle.shutdown_intent is None
        assert not lifecycle._has_workers()
        assert lifecycle.request_shutdown(ShutdownIntent.UPDATE_RESTART) is True
        assert lifecycle.should_stop() is True
    assert lifecycle.shutdown_intent is ShutdownIntent.UPDATE_RESTART


def test_update_failure_can_enable_quit_without_releasing_update_owner() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    with lifecycle.update_owner("update-job"):
        with pytest.raises(RuntimeError, match="owner"):
            lifecycle.allow_update_failure_quit("wrong-job")
        lifecycle.allow_update_failure_quit("update-job")
        assert lifecycle.update_failure_quit_allowed
        assert lifecycle.request_quit() is True
        assert lifecycle.shutdown_intent is ShutdownIntent.QUIT
        assert lifecycle.request_shutdown(ShutdownIntent.UPDATE_RESTART) is False


def test_releasing_update_owner_after_baseexception_starts_fresh_idle_deadline() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    clock = FakeClock()
    lifecycle = LifecycleController(clock=clock, inactivity_seconds=1, lease_seconds=1)
    with pytest.raises(KeyboardInterrupt):
        with lifecycle.update_owner("update-job"):
            lifecycle.connect("stale-tab")
            clock.advance(1000)
            raise KeyboardInterrupt
    assert lifecycle.should_stop() is False
    clock.advance(1)
    assert lifecycle.should_stop() is True


def test_quit_winning_before_update_owner_refuses_preparation() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    assert lifecycle.request_quit()
    with pytest.raises(RuntimeError, match="closing"):
        with lifecycle.update_owner("late-update"):
            pytest.fail("a closing app must not prepare an update")
