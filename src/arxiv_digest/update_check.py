"""Background discovery of newer published arXiv Digest releases."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from arxiv_digest import __version__
from arxiv_digest.update_contract import (
    DISCOVERY_DEADLINE_SECONDS,
    REPOSITORY,
    canonical_version,
)
from arxiv_digest.update_discovery import (
    DiscoveryResult,
    UpdateDescriptor,
    discover_updates,
)
from arxiv_digest.update_http import _safe_open_url

if TYPE_CHECKING:
    from arxiv_digest.update_installation import InstallationDetection


class UpdateChecker:
    """Run one release lookup without delaying dashboard startup."""

    def __init__(
        self,
        *,
        current_version: str = __version__,
        discover: Callable[..., DiscoveryResult] | None = None,
        detect_installation: Callable[..., InstallationDetection] | None = None,
        open_url: Any = _safe_open_url,
        deadline_seconds: float = DISCOVERY_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        canonical_version(current_version)
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("update deadline must be positive and finite")
        self._current_version = current_version
        self._discover = discover_updates if discover is None else discover
        self._detect_installation = detect_installation
        self._open_url = open_url
        self._deadline_seconds = deadline_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._state: dict[str, bool | str] = {"status": "idle"}
        self._descriptor: UpdateDescriptor | None = None
        self._deadline_at: float | None = None
        self._deadline_timer: threading.Timer | None = None

    def start(self) -> None:
        """Start the lookup once and return immediately."""

        deadline_timer: threading.Timer | None = None
        try:
            with self._lock:
                if self._state["status"] != "idle":
                    return
                self._state = {
                    "automatic_update": False,
                    "status": "checking",
                }
                self._deadline_at = self._monotonic() + self._deadline_seconds
                deadline_timer = threading.Timer(
                    max(0.0, self._deadline_at - self._monotonic()),
                    self._expire,
                )
                deadline_timer.daemon = True
                self._deadline_timer = deadline_timer
            worker = threading.Thread(
                target=self._run,
                name="arxiv-digest-update-check",
                daemon=True,
            )
            deadline_timer.start()
            worker.start()
        except Exception:
            if deadline_timer is not None:
                deadline_timer.cancel()
            with self._lock:
                if self._state["status"] == "checking":
                    self._state = self._fallback_state()
                if self._deadline_timer is deadline_timer:
                    self._deadline_timer = None

    def snapshot(self) -> dict[str, bool | str]:
        with self._lock:
            return dict(self._state)

    def descriptor_for(self, target_version: str) -> UpdateDescriptor | None:
        canonical_version(target_version)
        with self._lock:
            if (
                self._state["status"] == "available_automatic"
                and self._descriptor is not None
                and self._descriptor.target.version == target_version
            ):
                return self._descriptor
            return None

    def _fallback_state(self) -> dict[str, bool | str]:
        return {
            "automatic_update": False,
            "installed_version": self._current_version,
            "release_notes_url": f"{REPOSITORY}/releases",
            "status": "manual_fallback",
        }

    def _expire(self) -> None:
        with self._lock:
            if self._state["status"] == "checking":
                self._state = self._fallback_state()
                self._descriptor = None
            self._deadline_timer = None

    def _run(self) -> None:
        result: DiscoveryResult | None = None
        try:
            with self._lock:
                if self._state["status"] != "checking":
                    return
                deadline_at = self._deadline_at
            assert deadline_at is not None
            if self._monotonic() >= deadline_at:
                raise TimeoutError("update discovery deadline expired")
            installation = None
            if self._detect_installation is not None:
                try:
                    installation = self._detect_installation(
                        deadline_at=deadline_at, monotonic=self._monotonic,
                    ).installation
                except Exception:
                    # Installation ineligibility preserves ordinary release discovery.
                    installation = None
            if self._monotonic() >= deadline_at:
                raise TimeoutError("update discovery deadline expired")
            result = self._discover(
                current_version=self._current_version,
                installation=installation,
                open_url=self._open_url,
                deadline_at=self._deadline_at,
                monotonic=self._monotonic,
            )
            state = dict(result.public_snapshot)
        except Exception:
            state = self._fallback_state()
        deadline_timer: threading.Timer | None = None
        with self._lock:
            if self._state["status"] == "checking":
                if self._monotonic() >= self._deadline_at:
                    state = self._fallback_state()
                    result = None
                self._state = state
                self._descriptor = (
                    result.descriptor
                    if result is not None
                    and state["status"] == "available_automatic"
                    and state.get("automatic_update") is True
                    and result.descriptor is not None
                    and state.get("available_version") == result.descriptor.target.version
                    and state.get("installed_version") == self._current_version
                    else None
                )
                deadline_timer = self._deadline_timer
                self._deadline_timer = None
        if deadline_timer is not None:
            deadline_timer.cancel()
