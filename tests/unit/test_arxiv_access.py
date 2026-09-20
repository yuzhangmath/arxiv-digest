from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import stat

import pytest

from arxiv_digest.arxiv_access import (
    ArxivCooldown,
    ArxivCooldownUnavailable,
    ArxivRateLimited,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def test_cooldown_persists_private_minimal_state_and_expires_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state" / "arxiv-cooldown.json"
    clock = [NOW]
    first = ArxivCooldown(path, wall_clock=lambda: clock[0])
    assert first.active() is None
    assert not path.exists()

    observed = first.record(retry_after="120", http_status=429)

    assert isinstance(observed, ArxivRateLimited)
    assert observed.attempted is True
    assert observed.http_status == 429
    assert observed.retry_at == NOW + timedelta(seconds=120)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "retry_at": "2026-08-22T12:02:00+00:00",
        "http_status": 429,
    }
    restarted = ArxivCooldown(path, wall_clock=lambda: clock[0])
    blocked = restarted.active()
    assert blocked is not None
    assert blocked.attempted is False
    assert blocked.retry_at == observed.retry_at
    with pytest.raises(ArxivRateLimited):
        restarted.raise_if_active()
    clock[0] += timedelta(seconds=120)
    assert restarted.active() is None
    assert path.exists()


@pytest.mark.parametrize("retry_after", [None, "", "garbage", "NaN", "inf", "-5", "0", "1e999", "9" * 1000, "Thu, 01 Jan 1970 00:00:00 GMT"])
def test_invalid_or_elapsed_retry_after_uses_one_hour(retry_after: str | None) -> None:
    cooldown = ArxivCooldown(wall_clock=lambda: NOW)
    result = cooldown.record(retry_after=retry_after, http_status=429)
    assert result.retry_at == NOW + timedelta(hours=1)


def test_http_date_retry_after_honors_long_server_cooldown() -> None:
    until = NOW + timedelta(days=2)
    result = ArxivCooldown(wall_clock=lambda: NOW).record(
        retry_after=format_datetime(until), http_status=406,
    )
    assert result.retry_at == until


def test_second_response_cannot_shorten_an_active_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "arxiv-cooldown.json"
    first = ArxivCooldown(path, wall_clock=lambda: NOW)
    first.record(retry_after="7200", http_status=429)
    second = ArxivCooldown(path, wall_clock=lambda: NOW)
    result = second.record(retry_after="120", http_status=406)
    assert result.retry_at == NOW + timedelta(hours=2)
    assert result.http_status == 429


@pytest.mark.parametrize("payload", [b"not-json", b"{}", b'{"schema_version":2,"retry_at":"2026-08-22T13:00:00+00:00","http_status":429}', b'{"schema_version":1,"retry_at":"2026-08-22T13:00:00","http_status":429}', b"x" * 5000])
def test_invalid_persistence_blocks_requests_without_echoing_contents(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "private-cooldown.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    cooldown = ArxivCooldown(path, wall_clock=lambda: NOW)
    with pytest.raises(ArxivCooldownUnavailable) as caught:
        cooldown.raise_if_active()
    assert caught.value.code == "arxiv_cooldown_unavailable"
    assert str(path) not in caught.value.safe_message
    assert payload.decode() not in caught.value.safe_message


def test_failed_atomic_persistence_keeps_memory_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arxiv_digest.arxiv_access as access
    path = tmp_path / "arxiv-cooldown.json"
    cooldown = ArxivCooldown(path, wall_clock=lambda: NOW)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("private path and system error")

    monkeypatch.setattr(access, "atomic_write", fail)
    with pytest.raises(ArxivCooldownUnavailable) as caught:
        cooldown.record(retry_after=None, http_status=429)
    assert "private path" not in caught.value.safe_message
    assert caught.value.attempted is True
    blocked = cooldown.active()
    assert blocked is not None
    assert blocked.retry_at == NOW + timedelta(hours=1)


def test_duplicate_json_fields_are_not_used_to_hide_a_deadline(tmp_path: Path) -> None:
    path = tmp_path / "arxiv-cooldown.json"
    path.write_text('{"schema_version":1,"retry_at":"2026-08-22T13:00:00+00:00","retry_at":"1970-01-01T00:00:00+00:00","http_status":429}')
    path.chmod(0o600)
    with pytest.raises(ArxivCooldownUnavailable):
        ArxivCooldown(path, wall_clock=lambda: NOW).active()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "public"])
def test_cooldown_rejects_nonprivate_or_nonregular_files_without_waiting(tmp_path: Path, kind: str) -> None:
    import os
    path = tmp_path / "arxiv-cooldown.json"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing.json")
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text('{}')
        path.chmod(0o644)
    with pytest.raises(ArxivCooldownUnavailable):
        ArxivCooldown(path, wall_clock=lambda: NOW).active()
