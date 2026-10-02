from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from arxiv_digest.update_check import RELEASES_URL, UpdateChecker


class Response(io.BytesIO):
    def __init__(self, releases, *, next_page=False):
        super().__init__(json.dumps(releases).encode())
        self.headers = {"Link": '<https://untrusted.invalid/>; rel="next"'} if next_page else {}


def release(version, **extra):
    return {"tag_name": f"v{version}", "draft": False, **extra}


def completed(checker):
    checker.start()
    deadline = time.monotonic() + 2
    while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
        time.sleep(0.001)
    result = checker.snapshot()
    assert result["status"] != "checking"
    return result


@pytest.mark.parametrize("releases, expected", [
    ([], "current"),
    ([release("0.2.0"), release("0.3.1")], "current"),
    ([release("0.3.2"), release("0.10.0", prerelease=True)], "available_manual"),
    ([release("9.0.0", draft=True), release("99.0.0rc1"), release("01.0.0")], "current"),
])
def test_release_notice_uses_newest_public_version(releases, expected):
    checker = UpdateChecker(current_version="0.3.1", open_url=lambda *a, **kw: Response(releases))
    state = completed(checker)
    assert state == ({
        "status": expected,
        "installed_version": "0.3.1",
        **({"available_version": "0.10.0", "release_notes_url": f"{RELEASES_URL}/tag/v0.10.0"}
           if expected == "available_manual" else {}),
    })
    state["status"] = "changed"
    assert checker.snapshot()["status"] == expected


def test_pagination_uses_only_canonical_requests_and_bounds_each_read():
    requests = []
    responses = [Response([release("0.3.2")], next_page=True), Response([release("0.4.0")])]

    def open_url(request, *, timeout):
        requests.append(request)
        assert 0 < timeout <= 3
        return responses.pop(0)

    state = completed(UpdateChecker(current_version="0.3.1", open_url=open_url))
    assert state["available_version"] == "0.4.0"
    assert [request.full_url for request in requests] == [
        f"https://api.github.com/repos/yuzhangmath/arxiv-digest/releases?per_page=100&page={page}"
        for page in (1, 2)
    ]
    assert all(request.get_header("User-agent").startswith("arxiv-digest/") for request in requests)


@pytest.mark.parametrize("failure", ["network", "malformed", "record", "draft", "oversize", "pagination"])
def test_failed_or_unbounded_checks_offer_manual_link(failure):
    calls = 0

    def open_url(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "network":
            raise OSError("private failure detail")
        if failure == "malformed":
            return Response({"message": "unavailable"})
        if failure == "record":
            return Response([None])
        if failure == "draft":
            return Response([{"tag_name": "v0.4.0"}])
        if failure == "oversize":
            return Response("x" * (4 * 1024 * 1024))
        return Response([release("0.4.0")], next_page=True)

    assert completed(UpdateChecker(current_version="0.3.1", open_url=open_url)) == {
        "status": "manual_fallback", "installed_version": "0.3.1", "release_notes_url": RELEASES_URL,
    }
    assert calls == (10 if failure == "pagination" else 1)


def test_check_starts_once_without_blocking_and_never_replaces_expired_result(monkeypatch):
    now = [0.0]
    entered, unblock, finished = threading.Event(), threading.Event(), threading.Event()
    calls = 0

    def open_url(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert unblock.wait(2)
        return Response([release("0.4.0")])

    checker = UpdateChecker(current_version="0.3.1", open_url=open_url, monotonic=lambda: now[0])
    run = checker._run

    def observed_run():
        try:
            run()
        finally:
            finished.set()

    monkeypatch.setattr(checker, "_run", observed_run)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: checker.start(), range(8)))
        assert entered.wait(1)
        assert checker.snapshot() == {"status": "checking"}
        assert calls == 1
        now[0] = 60.0
        expired = checker.snapshot()
        assert expired["status"] == "manual_fallback"
        unblock.set()
        assert finished.wait(1)
        assert checker.snapshot() == expired
        checker.start()
        assert calls == 1
    finally:
        unblock.set()


def test_unavailable_thread_offers_manual_link(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail)
    checker = UpdateChecker()
    assert completed(checker)["status"] == "manual_fallback"


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan")])
def test_deadline_must_be_positive_and_finite(deadline):
    with pytest.raises(ValueError):
        UpdateChecker(deadline_seconds=deadline)
