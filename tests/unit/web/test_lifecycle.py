from __future__ import annotations

import json
import stat
import threading
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def windows_descriptor_read(monkeypatch):
    from arxiv_digest.web import lifecycle

    payload = json.dumps({
        "pid": 1234, "port": 43123, "startup_nonce": "nonce_abcd12345678",
        "token": "A" * 43, "started_at": "2026-01-01T00:00:00Z",
    }).encode()
    metadata = {
        "st_dev": 1, "st_ino": 2, "st_uid": 0,
        "st_mode": stat.S_IFREG | 0o600, "st_nlink": 1,
        "st_size": len(payload), "st_mtime_ns": 150,
        "st_ctime_ns": 100, "st_file_attributes": 0,
    }
    initial = SimpleNamespace(**metadata)
    current = SimpleNamespace(**metadata)
    # Windows Python 3.14 reports creation time through lstat(), but change
    # time through fstat(), even when both refer to the same unchanged file.
    metadata["st_ctime_ns"] = 200
    opened = SimpleNamespace(**metadata)
    final = SimpleNamespace(**metadata)
    path_stats = iter([initial, current])
    handle_stats = iter([opened, final])
    chunks = iter([payload, b""])
    closed = []
    validated = []
    path = SimpleNamespace(lstat=lambda: next(path_stats))
    monkeypatch.setattr(lifecycle, "os", SimpleNamespace(
        name="nt", fstat=lambda fd: next(handle_stats),
        read=lambda fd, count: next(chunks), close=closed.append,
    ))
    monkeypatch.setattr(lifecycle, "open_private_read_file", lambda actual: 42)
    monkeypatch.setattr(lifecycle, "validate_private_file", validated.append)
    return SimpleNamespace(
        path=path, initial=initial, opened=opened, final=final, current=current,
        closed=closed, validated=validated,
    )


def test_windows_runtime_descriptor_accepts_different_stat_ctime_semantics(
    windows_descriptor_read,
):
    from arxiv_digest.web import lifecycle

    state = windows_descriptor_read
    descriptor = lifecycle._read_private_descriptor(
        state.path, expected_identity=(1, 2),
    )

    assert descriptor.port == 43123
    assert descriptor.startup_nonce == "nonce_abcd12345678"
    assert state.validated == [42]
    assert state.closed == [42]


@pytest.mark.parametrize("snapshot, field", [
    ("opened", "st_ino"),
    ("final", "st_ctime_ns"),
    ("final", "st_mtime_ns"),
    ("current", "st_ctime_ns"),
    ("current", "st_ino"),
])
def test_windows_runtime_descriptor_rejects_changed_file_metadata(
    windows_descriptor_read, snapshot, field,
):
    from arxiv_digest.web import lifecycle

    state = windows_descriptor_read
    metadata = getattr(state, snapshot)
    setattr(metadata, field, getattr(metadata, field) + 1)

    with pytest.raises(lifecycle.InstanceSecurityError, match="path changed"):
        lifecycle._read_private_descriptor(state.path, expected_identity=(1, 2))

    if snapshot != "opened":
        assert state.validated == [42]
    assert state.closed == [42]


@pytest.mark.parametrize("wait_result, expected", [(258, True), (0, False)])
def test_windows_pid_probe_uses_a_non_destructive_process_handle(monkeypatch, wait_result, expected):
    from arxiv_digest.web import lifecycle

    closed = []
    opened = []
    handle = SimpleNamespace(Close=lambda: closed.append(True))
    monkeypatch.setattr(lifecycle, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        OpenProcess=lambda rights, inherit, pid: opened.append((rights, inherit, pid)) or handle,
        error=OSError,
    ))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(SYNCHRONIZE=0x100000))
    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace(
        WaitForSingleObject=lambda actual, timeout: wait_result, WAIT_TIMEOUT=258,
    ))
    monkeypatch.setattr(lifecycle.os, "kill", lambda *args: pytest.fail("Windows kill(pid, 0) terminates the process"))
    assert lifecycle._pid_alive(1234) is expected
    assert opened == [(0x100000, False, 1234)]
    assert closed == [True]



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



def test_concurrent_quit_requests_accept_exactly_once() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    ready = threading.Barrier(2)
    def request(_):
        ready.wait(timeout=5)
        return lifecycle.request_quit()
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(request, range(2))) == [False, True]
    assert lifecycle.is_closing
    assert lifecycle.request_quit() is False



def test_quit_waits_until_all_inflight_transactions_drain() -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    with lifecycle.transaction():
        with lifecycle.transaction():
            assert lifecycle.request_quit() is True
            assert lifecycle.should_stop() is False
        assert lifecycle.should_stop() is False
    assert lifecycle.should_stop() is True



@pytest.mark.parametrize("kind", ["sync", "download"])
def test_closing_lifecycle_rejects_new_workers(kind: str) -> None:
    from arxiv_digest.web.lifecycle import LifecycleController

    lifecycle = LifecycleController(clock=FakeClock())
    lifecycle.request_quit()
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
