from __future__ import annotations


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
