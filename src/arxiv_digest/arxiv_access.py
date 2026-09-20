"""Machine-local arXiv throttling state, separate from portable user data."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from math import isfinite
import os
from pathlib import Path
import stat
from threading import Lock

from arxiv_digest.atomic import atomic_write, exclusive_flock


_DEFAULT_COOLDOWN_SECONDS = 60 * 60
_MAX_STATE_BYTES = 4096


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cooldown field")
        result[key] = value
    return result


def retry_after_seconds(value: str | None, now: datetime) -> float:
    """Return a usable positive Retry-After delay, or zero for invalid input."""
    if value is None:
        return 0.0
    try:
        delay = float(value)
    except (ValueError, OverflowError):
        try:
            retry_at = parsedate_to_datetime(value)
            delay = (_as_utc(retry_at) - _as_utc(now)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    if not isfinite(delay) or delay <= 0:
        return 0.0
    try:
        _as_utc(now) + timedelta(seconds=delay)
    except OverflowError:
        return 0.0
    return delay


class ArxivRateLimited(RuntimeError):
    code = "arxiv_rate_limited"

    def __init__(
        self,
        *,
        retry_at: datetime,
        http_status: int | None = None,
        attempted: bool = True,
    ) -> None:
        self.retry_at = _as_utc(retry_at)
        self.http_status = http_status
        self.attempted = attempted
        status = f" (HTTP {http_status})" if http_status is not None else ""
        self.safe_message = (
            f"arXiv rate limit reported{status}. Requests are paused until "
            f"{self.retry_at.isoformat()}."
        )
        super().__init__(self.safe_message)


class ArxivCooldownUnavailable(RuntimeError):
    code = "arxiv_cooldown_unavailable"
    safe_message = (
        "The saved arXiv cooldown could not be read or written safely. "
        "Requests are paused until the local cooldown state is available."
    )

    def __init__(self, *, attempted: bool = False) -> None:
        self.attempted = attempted
        super().__init__(self.safe_message)


class ArxivCooldown:
    def __init__(
        self,
        path: Path | None = None,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = path
        self._wall_clock = wall_clock
        self._gate = Lock()
        self._state: tuple[datetime, int | None] | None = None

    def _read(self) -> tuple[datetime, int | None] | None:
        if self._path is None:
            return None
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > _MAX_STATE_BYTES
            ):
                raise ValueError("invalid cooldown file")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                payload = json.loads(
                    handle.read(_MAX_STATE_BYTES + 1),
                    object_pairs_hook=_unique_json_fields,
                )
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "retry_at", "http_status"}
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != 1
                or not isinstance(payload["retry_at"], str)
            ):
                raise ValueError("invalid cooldown record")
            status = payload["http_status"]
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise ValueError("invalid HTTP status")
            deadline = datetime.fromisoformat(payload["retry_at"])
            if deadline.tzinfo is None:
                raise ValueError("cooldown deadline must include a timezone")
            return (_as_utc(deadline), status)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, OverflowError):
            raise ArxivCooldownUnavailable() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _merge(self, other: tuple[datetime, int | None] | None) -> None:
        if other is not None and (self._state is None or other[0] > self._state[0]):
            self._state = other

    def active(self) -> ArxivRateLimited | None:
        with self._gate:
            self._merge(self._read())
            if self._state is None or self._state[0] <= _as_utc(self._wall_clock()):
                return None
            return ArxivRateLimited(
                retry_at=self._state[0], http_status=self._state[1], attempted=False,
            )

    def raise_if_active(self) -> None:
        if (error := self.active()) is not None:
            raise error

    def record(
        self,
        *,
        retry_after: str | None,
        http_status: int | None,
    ) -> ArxivRateLimited:
        now = _as_utc(self._wall_clock())
        seconds = retry_after_seconds(retry_after, now) or _DEFAULT_COOLDOWN_SECONDS
        with self._gate:
            # Keep the deadline even if durable persistence fails.
            self._merge((now + timedelta(seconds=seconds), http_status))
            if self._path is not None:
                try:
                    # Independent clients/processes cannot replace a longer cooldown
                    # with a shorter observation while sharing this operational state.
                    with exclusive_flock(self._path.with_suffix(".lock")):
                        self._merge(self._read())
                        assert self._state is not None
                        atomic_write(self._path, json.dumps({
                            "schema_version": 1,
                            "retry_at": self._state[0].isoformat(),
                            "http_status": self._state[1],
                        }, sort_keys=True).encode("utf-8"))
                except (OSError, ArxivCooldownUnavailable):
                    raise ArxivCooldownUnavailable(attempted=True) from None
            assert self._state is not None
            return ArxivRateLimited(
                retry_at=self._state[0], http_status=self._state[1], attempted=True,
            )
