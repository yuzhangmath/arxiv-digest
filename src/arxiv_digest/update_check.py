"""A bounded, passive check for newer public releases."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from urllib.request import Request, urlopen

from arxiv_digest import __version__


RELEASES_URL = "https://github.com/yuzhangmath/arxiv-digest/releases"
_API_URL = "https://api.github.com/repos/yuzhangmath/arxiv-digest/releases?per_page=100"
_PAGE_BYTE_LIMIT = 4 * 1024 * 1024
_PAGE_LIMIT = 10
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError("expected a major.minor.patch release version")
    return tuple(map(int, value.split(".")))


def _latest_release(*, open_url: Callable, deadline_at: float, monotonic: Callable) -> str | None:
    latest = None
    for page in range(1, _PAGE_LIMIT + 1):
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            raise TimeoutError("release check expired")
        request = Request(
            f"{_API_URL}&page={page}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"arxiv-digest/{__version__}"},
        )
        with open_url(request, timeout=min(3.0, remaining)) as response:
            payload = response.read(_PAGE_BYTE_LIMIT + 1)
            if len(payload) > _PAGE_BYTE_LIMIT:
                raise ValueError("release page is too large")
            releases = json.loads(payload)
            if not isinstance(releases, list) or len(releases) > 100:
                raise ValueError("invalid release list")
            for release in releases:
                if (
                    not isinstance(release, dict)
                    or type(release.get("draft")) is not bool
                    or not isinstance(release.get("tag_name"), str)
                ):
                    raise ValueError("invalid release record")
                if release["draft"]:
                    continue
                tag = release["tag_name"]
                if not tag.startswith("v"):
                    continue
                try:
                    version = tag[1:]
                    parsed = _version(version)
                except ValueError:
                    continue
                if latest is None or parsed > _version(latest):
                    latest = version
            # Only use the presence of a next page; never follow a supplied URL.
            if 'rel="next"' not in response.headers.get("Link", ""):
                return latest
    raise ValueError("release list exceeded the page limit")


class UpdateChecker:
    """Look up releases once, without delaying startup or installing anything."""

    def __init__(
        self,
        *,
        current_version: str = __version__,
        open_url: Callable = urlopen,
        deadline_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        _version(current_version)
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("release check deadline must be positive and finite")
        self._current_version = current_version
        self._open_url = open_url
        self._deadline_seconds = deadline_seconds
        self._monotonic = monotonic
        self._deadline_at = 0.0
        self._lock = threading.Lock()
        self._state = {"status": "idle"}

    def _fallback(self) -> dict[str, str]:
        return {
            "status": "manual_fallback",
            "installed_version": self._current_version,
            "release_notes_url": RELEASES_URL,
        }

    def start(self) -> None:
        with self._lock:
            if self._state["status"] != "idle":
                return
            self._deadline_at = self._monotonic() + self._deadline_seconds
            self._state = {"status": "checking"}
        try:
            threading.Thread(target=self._run, name="arxiv-digest-update-check", daemon=True).start()
        except Exception:
            with self._lock:
                self._state = self._fallback()

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            if self._state["status"] == "checking" and self._monotonic() >= self._deadline_at:
                self._state = self._fallback()
            return dict(self._state)

    def _run(self) -> None:
        try:
            latest = _latest_release(
                open_url=self._open_url, deadline_at=self._deadline_at, monotonic=self._monotonic,
            )
            state = {"status": "current", "installed_version": self._current_version}
            if latest is not None and _version(latest) > _version(self._current_version):
                state.update(
                    status="available_manual", available_version=latest,
                    release_notes_url=f"{RELEASES_URL}/tag/v{latest}",
                )
        except Exception:
            state = self._fallback()
        with self._lock:
            if self._state["status"] == "checking":
                self._state = self._fallback() if self._monotonic() >= self._deadline_at else state
