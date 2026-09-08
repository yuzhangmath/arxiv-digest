"""Shared no-auto-redirect transport for canonical GitHub release assets."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from arxiv_digest import __version__
from arxiv_digest.update_contract import (
    DISCOVERY_DEADLINE_SECONDS,
    RELEASE_ASSET_REDIRECT_LIMIT,
    RELEASE_ASSET_HOSTS,
    canonical_version,
    release_urls,
)


_REQUEST_TIMEOUT_SECONDS = 3.0
_INITIAL_PATH = re.compile(
    r"/yuzhangmath/arxiv-digest/releases/download/v([^/]+)/([^/]+)"
)
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ReleaseAssetTransportError(ValueError):
    """A release-asset request or redirect is not safely canonical."""


class HttpResponse(Protocol):
    status: int
    headers: object

    def geturl(self) -> str: ...

    def close(self) -> None: ...


OpenUrl = Callable[..., HttpResponse]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: object,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _safe_open_url(request: Request, *, timeout: float) -> HttpResponse:
    """Open one HTTP hop while leaving redirects visible to the caller."""

    opener = build_opener(_NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as error:
        if error.code in _REDIRECT_STATUSES:
            return error
        raise


def _split_url(url: str):
    if (
        type(url) is not str
        or "\\" in url
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url)
    ):
        raise ReleaseAssetTransportError("release asset URL is invalid")
    try:
        split = urlsplit(url)
        port = split.port
    except ValueError as error:
        raise ReleaseAssetTransportError("release asset URL is invalid") from error
    if (
        split.scheme != "https"
        or split.hostname not in RELEASE_ASSET_HOSTS
        or split.netloc not in RELEASE_ASSET_HOSTS
        or split.username is not None
        or split.password is not None
        or port is not None
        or split.fragment
    ):
        raise ReleaseAssetTransportError("release asset URL is invalid")
    return split


def _validate_initial_url(url: str) -> None:
    split = _split_url(url)
    match = _INITIAL_PATH.fullmatch(split.path)
    if split.hostname != "github.com" or split.query or match is None:
        raise ReleaseAssetTransportError("release asset URL is not canonical")
    version, asset_name = match.groups()
    try:
        canonical_version(version)
    except ValueError as error:
        raise ReleaseAssetTransportError(
            "release asset URL is not canonical"
        ) from error
    if _ASSET_NAME.fullmatch(asset_name) is None:
        raise ReleaseAssetTransportError("release asset URL is not canonical")
    if url != f"{release_urls(version)['wheel_prefix']}{asset_name}":
        raise ReleaseAssetTransportError("release asset URL is not canonical")


def _validate_redirect_url(url: str, initial_url: str) -> None:
    split = _split_url(url)
    if split.hostname == "github.com":
        if url != initial_url:
            raise ReleaseAssetTransportError("release asset redirect is invalid")
        return
    if not split.path.startswith("/") or split.path == "/":
        raise ReleaseAssetTransportError("release asset redirect is invalid")


def _single_header(headers: object, name: str) -> str | None:
    get_all = getattr(headers, "get_all", None)
    if not callable(get_all):
        raise ReleaseAssetTransportError("release asset headers are invalid")
    values = get_all(name, [])
    if len(values) > 1:
        raise ReleaseAssetTransportError("release asset headers are ambiguous")
    return None if not values else values[0]


def open_release_asset(
    initial_url: str,
    *,
    open_url: OpenUrl = _safe_open_url,
    deadline_at: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> HttpResponse:
    """Open a canonical asset by validating and issuing every redirect hop."""

    _validate_initial_url(initial_url)
    if deadline_at is None:
        deadline_at = monotonic() + DISCOVERY_DEADLINE_SECONDS
    if type(deadline_at) not in {int, float} or not math.isfinite(deadline_at):
        raise ValueError("release asset deadline is invalid")
    requested_url = initial_url
    redirects = 0
    while True:
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            raise TimeoutError("release asset deadline expired")
        request = Request(
            requested_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": f"arxiv-digest/{__version__}",
            },
        )
        response = open_url(
            request,
            timeout=min(_REQUEST_TIMEOUT_SECONDS, remaining),
        )
        try:
            if monotonic() >= deadline_at:
                raise TimeoutError("release asset deadline expired")
            response_url = response.geturl()
            response_status = response.status
        except BaseException:
            response.close()
            raise
        if response_url != requested_url:
            response.close()
            raise ReleaseAssetTransportError(
                "release asset opener followed a hidden redirect"
            )
        if response_status not in _REDIRECT_STATUSES:
            return response
        try:
            if redirects >= RELEASE_ASSET_REDIRECT_LIMIT:
                raise ReleaseAssetTransportError(
                    "release asset redirect limit exceeded"
                )
            location = _single_header(response.headers, "Location")
            if location is None:
                raise ReleaseAssetTransportError(
                    "release asset redirect is missing a location"
                )
            _validate_redirect_url(location, initial_url)
        finally:
            response.close()
        redirects += 1
        requested_url = location
