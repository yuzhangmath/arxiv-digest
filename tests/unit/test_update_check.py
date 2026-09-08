from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_update_checker_is_non_blocking_and_publishes_the_result() -> None:
    from types import MappingProxyType

    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult

    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_lock = threading.Lock()
    fetch_calls = 0

    expected = {
        "automatic_update": False,
        "available_version": "0.3.1",
        "installed_version": "0.3.0",
        "release_notes_url": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.1"
        ),
        "status": "available_manual",
    }

    def discover(**_arguments: object) -> DiscoveryResult:
        nonlocal fetch_calls
        with fetch_lock:
            fetch_calls += 1
        fetch_started.set()
        assert release_fetch.wait(timeout=1)
        return DiscoveryResult(MappingProxyType(expected))

    checker = UpdateChecker(
        current_version="0.3.0",
        discover=discover,
    )
    starters = [threading.Thread(target=checker.start) for _ in range(8)]
    for starter in starters:
        starter.start()
    for starter in starters:
        starter.join(timeout=1)

    assert fetch_started.wait(timeout=1)
    assert checker.snapshot() == {
        "automatic_update": False,
        "status": "checking",
    }
    assert fetch_calls == 1
    release_fetch.set()
    deadline = time.monotonic() + 1
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert checker.snapshot() == expected
    checker.start()
    assert fetch_calls == 1


def test_update_checker_deadline_is_terminal_when_fetch_never_returns() -> None:
    from types import MappingProxyType

    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult

    release_fetch = threading.Event()
    fetch_finished = threading.Event()

    def discover(**_arguments: object) -> DiscoveryResult:
        release_fetch.wait()
        fetch_finished.set()
        return DiscoveryResult(
            MappingProxyType(
                {
                    "automatic_update": False,
                    "available_version": "0.3.1",
                    "installed_version": "0.3.0",
                    "release_notes_url": (
                        "https://github.com/yuzhangmath/arxiv-digest/"
                        "releases/tag/v0.3.1"
                    ),
                    "status": "available_manual",
                }
            )
        )

    checker = UpdateChecker(
        current_version="0.3.0",
        discover=discover,
        deadline_seconds=0.02,
    )
    try:
        checker.start()
        deadline = time.monotonic() + 1
        while (
            checker.snapshot()["status"] == "checking"
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        expected_fallback = {
            "automatic_update": False,
            "installed_version": "0.3.0",
            "release_notes_url": (
                "https://github.com/yuzhangmath/arxiv-digest/releases"
            ),
            "status": "manual_fallback",
        }
        assert checker.snapshot() == expected_fallback

        release_fetch.set()
        assert fetch_finished.wait(timeout=1)
        assert checker.snapshot() == expected_fallback
    finally:
        release_fetch.set()


def test_update_checker_fails_closed_when_worker_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_check

    class BrokenThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    class InertTimer:
        daemon = False

        def __init__(self, *_args: object) -> None:
            pass

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(update_check.threading, "Timer", InertTimer)
    monkeypatch.setattr(update_check.threading, "Thread", BrokenThread)
    checker = update_check.UpdateChecker(
        current_version="0.3.0",
        discover=lambda **arguments: (_ for _ in ()).throw(
            AssertionError("worker never started")
        ),
    )

    checker.start()

    assert checker.snapshot() == {
        "automatic_update": False,
        "installed_version": "0.3.0",
        "release_notes_url": (
            "https://github.com/yuzhangmath/arxiv-digest/releases"
        ),
        "status": "manual_fallback",
    }


def test_runtime_exposes_only_the_cached_update_snapshot() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier

    expected = {
        "automatic_update": False,
        "available_version": "0.3.1",
        "installed_version": "0.3.0",
        "release_notes_url": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.1"
        ),
        "status": "available_manual",
    }
    runtime = object.__new__(_DefaultRuntime)
    runtime.store = object()
    runtime.maintenance = MaintenanceBarrier()
    runtime.update_coordinator = SimpleNamespace(status=lambda: None)
    calls: list[str] = []
    runtime.update_checker = SimpleNamespace(
        start=lambda: calls.append("start"),
        snapshot=lambda: expected,
    )

    assert runtime.handlers()["update"]({}) == expected
    assert calls == ["start"]


def test_checker_publishes_only_the_closed_discovery_projection() -> None:
    from types import MappingProxyType

    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult

    expected = MappingProxyType(
        {
            "automatic_update": False,
            "available_version": "0.3.1",
            "installed_version": "0.3.0",
            "release_notes_url": (
                "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.1"
            ),
            "status": "available_manual",
        }
    )
    called = threading.Event()

    def discover(**arguments: object) -> DiscoveryResult:
        assert arguments["current_version"] == "0.3.0"
        assert arguments["installation"] is None
        assert type(arguments["deadline_at"]) is float
        called.set()
        return DiscoveryResult(expected)

    checker = UpdateChecker(current_version="0.3.0", discover=discover)
    checker.start()
    assert called.wait(timeout=1)
    deadline = time.monotonic() + 1
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.005)

    assert checker.snapshot() == expected
    assert checker.descriptor_for("0.3.1") is None


def test_checker_timer_uses_the_remaining_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_check

    intervals: list[float] = []

    class InertTimer:
        daemon = False

        def __init__(self, interval: float, function: object) -> None:
            del function
            intervals.append(interval)

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    class InertThread:
        def __init__(self, **_arguments: object) -> None:
            pass

        def start(self) -> None:
            pass

    now = iter((10.0, 11.25))
    monkeypatch.setattr(update_check.threading, "Timer", InertTimer)
    monkeypatch.setattr(update_check.threading, "Thread", InertThread)
    checker = update_check.UpdateChecker(
        current_version="0.3.0",
        deadline_seconds=60.0,
        monotonic=lambda: next(now),
    )

    checker.start()

    assert intervals == [58.75]


def test_checker_detects_installation_in_background_under_the_discovery_deadline() -> None:
    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult
    from arxiv_digest.update_installation import (
        InstallationDetection, InstallationUnavailableReason,
    )

    detecting = threading.Event()
    finish_detection = threading.Event()
    finished = threading.Event()
    deadlines: list[float] = []

    def detect_installation(*, deadline_at, monotonic):
        deadlines.append(deadline_at)
        assert callable(monotonic)
        detecting.set()
        assert finish_detection.wait(timeout=1)
        return InstallationDetection(reason=InstallationUnavailableReason.UNSUPPORTED_SOURCE)

    def discover(**arguments):
        assert arguments["installation"] is None
        assert arguments["deadline_at"] == deadlines[0]
        finished.set()
        return DiscoveryResult({"status": "current", "automatic_update": False})

    checker = UpdateChecker(
        current_version="0.3.0", detect_installation=detect_installation,
        discover=discover,
    )
    try:
        checker.start()
        assert detecting.wait(timeout=1)
        assert checker.snapshot()["status"] == "checking"
        finish_detection.set()
        assert finished.wait(timeout=1)
        deadline = time.monotonic() + 1
        while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert checker.snapshot() == {"status": "current", "automatic_update": False}
    finally:
        finish_detection.set()


def test_checker_does_not_start_discovery_after_detection_uses_up_deadline() -> None:
    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_installation import (
        InstallationDetection, InstallationUnavailableReason,
    )

    now = [10.0]
    detected = threading.Event()

    def detect_installation(**arguments):
        now[0] = arguments["deadline_at"]
        detected.set()
        return InstallationDetection(reason=InstallationUnavailableReason.DEADLINE_EXPIRED)

    checker = UpdateChecker(
        current_version="0.3.0", detect_installation=detect_installation,
        discover=lambda **arguments: pytest.fail("discovery started after deadline"),
        deadline_seconds=60,
        monotonic=lambda: now[0],
    )
    checker.start()
    assert detected.wait(timeout=1)
    deadline = time.monotonic() + 1
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert checker.snapshot()["status"] == "manual_fallback"
    assert checker.descriptor_for("0.3.1") is None


@pytest.mark.parametrize("late_result", [False, True])
def test_checker_retains_only_the_exact_on_time_automatic_descriptor(
    tmp_path: Path, late_result: bool,
) -> None:
    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult
    from arxiv_digest.update_installation import InstallationDetection
    from tests.unit.test_update_discovery import _automatic, _eligible_bundle

    installed, target, installation = _eligible_bundle(tmp_path)
    descriptor = _automatic(installed, target, installation)
    assert descriptor is not None
    expected = {
        "status": "available_automatic", "automatic_update": True,
        "installed_version": installed.version, "available_version": target.version,
        "release_notes_url": target.release_notes_url,
    }
    now = [10.0]
    published = threading.Event()

    def discover(**arguments):
        assert arguments["installation"] is installation
        if late_result:
            now[0] = arguments["deadline_at"]
        published.set()
        return DiscoveryResult(expected, descriptor)

    checker = UpdateChecker(
        current_version=installed.version,
        detect_installation=lambda **arguments: InstallationDetection(installation),
        discover=discover, deadline_seconds=60, monotonic=lambda: now[0],
    )
    assert checker.descriptor_for(target.version) is None
    checker.start()
    assert published.wait(timeout=1)
    deadline = time.monotonic() + 1
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.005)
    if late_result:
        assert checker.snapshot()["status"] == "manual_fallback"
        assert checker.descriptor_for(target.version) is None
    else:
        assert checker.snapshot() == expected
        assert checker.descriptor_for(target.version) is descriptor
        assert checker.descriptor_for("0.3.1") is None
        with pytest.raises(ValueError):
            checker.descriptor_for("v0.3.2")


def test_detection_error_preserves_discovered_manual_release_without_private_details() -> None:
    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import DiscoveryResult

    expected = {
        "status": "available_manual", "automatic_update": False,
        "installed_version": "0.3.0", "available_version": "0.3.2",
        "release_notes_url": "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.2",
    }
    discovered = threading.Event()

    def detection(**arguments):
        raise OSError("sensitive installation path and subprocess detail")

    def discover(**arguments):
        assert arguments["installation"] is None
        discovered.set()
        return DiscoveryResult(expected)

    checker = UpdateChecker(
        current_version="0.3.0", detect_installation=detection, discover=discover,
    )
    checker.start()
    assert discovered.wait(timeout=1)
    deadline = time.monotonic() + 1
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert checker.snapshot() == expected
