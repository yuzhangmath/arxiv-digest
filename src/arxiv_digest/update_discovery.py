"""Bounded GitHub release discovery with closed public projections."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.request import Request

from arxiv_digest import __version__
from arxiv_digest.update_contract import (
    DISCOVERY_DEADLINE_SECONDS,
    MANIFEST_BYTE_LIMIT,
    MANIFEST_MEDIA_TYPES,
    PRODUCT,
    RELEASE_LIST_REQUEST_LIMIT,
    RELEASE_PAGE_BYTE_LIMIT,
    RELEASE_RECORD_LIMIT,
    REPOSITORY,
    SUPPORTED_PLATFORMS,
    UPDATE_MANIFEST_FILENAME,
    canonical_version,
    release_urls,
)
from arxiv_digest.update_http import _safe_open_url, open_release_asset
from arxiv_digest.update_manifest import (
    UpdateManifest,
    parse_update_manifest,
    runtime_requirements_sha256,
    serialize_update_manifest,
)

if TYPE_CHECKING:
    from arxiv_digest.update_installation import PipxInstallation


_RELEASES_API_URL = (
    "https://api.github.com/repos/yuzhangmath/arxiv-digest/"
    "releases?per_page=100"
)
_LINK_ENTRY = re.compile(r'<([^<>]+)>; rel="([a-z]+)"')


class _Response(Protocol):
    status: int
    headers: object

    def geturl(self) -> str: ...

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> "_Response": ...

    def __exit__(self, *args: object) -> object: ...


OpenUrl = Callable[..., _Response]


@dataclass(frozen=True, slots=True)
class GitHubAsset:
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True, slots=True)
class ListedRelease:
    version: str
    channel: Literal["prerelease", "stable"]
    assets: tuple[GitHubAsset, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    release_notes_url: str
    manifest: UpdateManifest
    manifest_asset: GitHubAsset
    wheel_asset: GitHubAsset

    @property
    def version(self) -> str:
        """Return the authoritative manifest version without duplicating it."""

        return self.manifest.version

    @property
    def channel(self) -> Literal["prerelease", "stable"]:
        """Return the authoritative manifest channel without duplicating it."""

        return self.manifest.channel


@dataclass(frozen=True, slots=True)
class UpdateDescriptor:
    """Backend-only identities for an eligible update and its rollback source."""

    installed: VerifiedRelease
    target: VerifiedRelease
    installation: PipxInstallation


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    public_snapshot: Mapping[str, bool | str]
    descriptor: UpdateDescriptor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "public_snapshot",
            MappingProxyType(dict(self.public_snapshot)),
        )


def _verified_release_matches(release: VerifiedRelease) -> bool:
    payload = serialize_update_manifest(release.manifest)
    urls = release_urls(release.version)
    wheel = release.manifest.wheel
    return (
        release.release_notes_url == urls["notes"]
        and release.manifest_asset == GitHubAsset(
            UPDATE_MANIFEST_FILENAME,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            f"{urls['wheel_prefix']}{UPDATE_MANIFEST_FILENAME}",
        )
        and release.wheel_asset == GitHubAsset(
            wheel.name, wheel.size, wheel.sha256,
            f"{urls['wheel_prefix']}{wheel.name}",
        )
    )


def _source_version_allowed(installed_version: str, target: UpdateManifest) -> bool:
    source = target.automatic_update_from
    return (
        target.automatic_update is True
        and source is not None
        and canonical_version(source.minimum)
        <= canonical_version(installed_version)
        < canonical_version(source.maximum_exclusive)
        and installed_version not in source.excluded
    )


def automatic_update_descriptor(
    *,
    installed: VerifiedRelease,
    target: VerifiedRelease,
    installation: PipxInstallation,
    local_requirements_sha256: str,
    running_python: tuple[int, int],
    platform: str,
) -> UpdateDescriptor | None:
    """Add automatic capability only after both releases match local metadata."""

    from arxiv_digest.update_manifest import python_policy_from_requires_python

    try:
        if (
            not _verified_release_matches(installed)
            or not _verified_release_matches(target)
            or not canonical_version(installed.version) < canonical_version(target.version)
            or not _source_version_allowed(installed.version, target.manifest)
            or installation.version != installed.version
            or installation.distribution.name != PRODUCT
            or installation.distribution.version != installed.version
            or platform not in SUPPORTED_PLATFORMS
            or installation.platform != platform
            or type(running_python) is not tuple
            or len(running_python) != 2
            or any(type(part) is not int or part < 0 for part in running_python)
            or installation.running_python != running_python
            or python_policy_from_requires_python(
                installation.distribution.requires_python
            ) != installed.manifest.python
        ):
            return None
        digest = runtime_requirements_sha256(installation.distribution.requirements)
        if not (
            digest == local_requirements_sha256
            == installation.runtime_requirements_sha256
            == installed.manifest.runtime_requirements_sha256
            == target.manifest.runtime_requirements_sha256
        ):
            return None
        for release in (installed, target):
            policy = release.manifest.python
            minimum = tuple(int(part) for part in policy.minimum.split("."))
            maximum = (
                None if policy.maximum_exclusive is None
                else tuple(int(part) for part in policy.maximum_exclusive.split("."))
            )
            if (
                platform not in release.manifest.platforms
                or running_python < minimum
                or maximum is not None and running_python >= maximum
            ):
                return None
    except (AttributeError, TypeError, ValueError):
        return None
    return UpdateDescriptor(installed, target, installation)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("GitHub release response has duplicate keys")
        value[key] = member
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("GitHub release response contains a nonfinite value")


def _page_url(number: int) -> str:
    return _RELEASES_API_URL if number == 1 else f"{_RELEASES_API_URL}&page={number}"


def _response_content_type(response: _Response) -> str:
    get_all = getattr(response.headers, "get_all", None)
    get_content_type = getattr(response.headers, "get_content_type", None)
    if (
        not callable(get_all)
        or len(get_all("Content-Type", [])) != 1
        or not callable(get_content_type)
    ):
        raise ValueError("GitHub release response headers are invalid")
    return get_content_type()


def _next_page(response: _Response, expected: int) -> int | None:
    get_all = getattr(response.headers, "get_all", None)
    if not callable(get_all):
        raise ValueError("GitHub release pagination headers are invalid")
    links = get_all("Link", [])
    if not links:
        return None
    if len(links) != 1 or type(links[0]) is not str:
        raise ValueError("GitHub release pagination is ambiguous")
    link = links[0]
    next_urls: list[str] = []
    for entry in link.split(","):
        match = _LINK_ENTRY.fullmatch(entry.strip())
        if match is None:
            raise ValueError("GitHub release pagination is invalid")
        if match.group(2) == "next":
            next_urls.append(match.group(1))
    if not next_urls:
        return None
    if next_urls != [f"{_RELEASES_API_URL}&page={expected}"]:
        raise ValueError("GitHub release pagination is invalid")
    return expected


def _read_page(
    response: _Response,
    *,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if monotonic() >= deadline_at:
            raise TimeoutError("GitHub release discovery deadline expired")
        chunk = response.read(min(64 * 1024, RELEASE_PAGE_BYTE_LIMIT - total + 1))
        if type(chunk) is not bytes:
            raise ValueError("GitHub release response body is invalid")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > RELEASE_PAGE_BYTE_LIMIT:
            raise ValueError("GitHub release response exceeded byte limit")


def _listed_assets(value: object, version: str) -> tuple[GitHubAsset, ...]:
    if type(value) is not list:
        return ()
    base = release_urls(version)["wheel_prefix"]
    assets: list[GitHubAsset] = []
    for raw in value:
        if type(raw) is not dict:
            return ()
        name = raw.get("name")
        size = raw.get("size")
        digest = raw.get("digest")
        url = raw.get("browser_download_url")
        if (
            type(name) is not str
            or type(size) is not int
            or size <= 0
            or type(digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or type(url) is not str
            or url != f"{base}{name}"
            or raw.get("state") != "uploaded"
        ):
            return ()
        assets.append(GitHubAsset(name, size, digest.removeprefix("sha256:"), url))
    return tuple(sorted(assets, key=lambda asset: asset.name))


def _fetch_complete_releases(
    open_url: OpenUrl,
    *,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> list[ListedRelease]:
    releases: list[ListedRelease] = []
    release_by_version: dict[str, ListedRelease] = {}
    page = 1
    record_count = 0
    while True:
        url = _page_url(page)
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"arxiv-digest/{__version__}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            raise TimeoutError("GitHub release discovery deadline expired")
        with open_url(request, timeout=min(3.0, remaining)) as response:
            if (
                response.status != 200
                or response.geturl() != url
                or _response_content_type(response) != "application/json"
                or response.headers.get("Content-Encoding") is not None
            ):
                raise ValueError("GitHub release response is invalid")
            payload = json.loads(
                _read_page(
                    response,
                    deadline_at=deadline_at,
                    monotonic=monotonic,
                ),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
            next_page = _next_page(response, page + 1)
        if type(payload) is not list:
            raise ValueError("GitHub release response is invalid")
        record_count += len(payload)
        if record_count > RELEASE_RECORD_LIMIT:
            raise ValueError("GitHub release record limit exceeded")
        for value in payload:
            if type(value) is not dict:
                raise ValueError("GitHub release record is invalid")
            draft = value.get("draft")
            if type(draft) is not bool:
                raise ValueError("GitHub release draft flag is invalid")
            if draft:
                continue
            tag = value.get("tag_name")
            if type(tag) is not str or not tag.startswith("v"):
                continue
            try:
                canonical_version(tag[1:])
            except ValueError:
                continue
            prerelease = value.get("prerelease")
            if type(prerelease) is not bool:
                raise ValueError("GitHub release prerelease flag is invalid")
            listed_release = ListedRelease(
                version=tag[1:],
                channel="prerelease" if prerelease else "stable",
                assets=_listed_assets(value.get("assets"), tag[1:]),
            )
            existing = release_by_version.get(listed_release.version)
            if existing is not None:
                if existing != listed_release:
                    raise ValueError("GitHub release records conflict")
                continue
            release_by_version[listed_release.version] = listed_release
            releases.append(listed_release)
        if next_page is None:
            return releases
        if record_count == RELEASE_RECORD_LIMIT:
            raise ValueError("GitHub release pagination is incomplete")
        if page >= RELEASE_LIST_REQUEST_LIMIT:
            raise ValueError("GitHub release pagination is incomplete")
        page = next_page


def discover_updates(
    *,
    current_version: str,
    installation: PipxInstallation | None,
    open_url: OpenUrl = _safe_open_url,
    deadline_at: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> DiscoveryResult:
    """Discover the highest complete published release newer than installed."""

    current = canonical_version(current_version)
    if deadline_at is None:
        deadline_at = monotonic() + DISCOVERY_DEADLINE_SECONDS
    if type(deadline_at) not in {int, float} or not math.isfinite(deadline_at):
        raise ValueError("GitHub release discovery deadline is invalid")
    try:
        published = _fetch_complete_releases(
            open_url,
            deadline_at=deadline_at,
            monotonic=monotonic,
        )
    except Exception:
        return DiscoveryResult(
            MappingProxyType(
                {
                    "automatic_update": False,
                    "installed_version": current_version,
                    "release_notes_url": f"{REPOSITORY}/releases",
                    "status": "manual_fallback",
                }
            )
        )
    candidates = [
        release
        for release in published
        if canonical_version(release.version) > current
    ]
    descriptor: UpdateDescriptor | None = None
    if not candidates:
        snapshot: dict[str, bool | str] = {
            "automatic_update": False,
            "installed_version": current_version,
            "status": "current",
        }
    else:
        target = max(candidates, key=lambda release: canonical_version(release.version))
        snapshot = {
            "automatic_update": False,
            "available_version": target.version,
            "installed_version": current_version,
            "release_notes_url": release_urls(target.version)["notes"],
            "status": "available_manual",
        }
        if installation is not None and installation.version == current_version:
            installed_record = next(
                (release for release in published if release.version == current_version),
                None,
            )
            if installed_record is not None:
                try:
                    verified_target = verify_release_record(
                        target, open_url=open_url,
                        deadline_at=deadline_at, monotonic=monotonic,
                    )
                    if _source_version_allowed(current_version, verified_target.manifest):
                        verified_installed = verify_release_record(
                            installed_record, open_url=open_url,
                            deadline_at=deadline_at, monotonic=monotonic,
                        )
                        descriptor = automatic_update_descriptor(
                            installed=verified_installed,
                            target=verified_target,
                            installation=installation,
                            local_requirements_sha256=(
                                installation.runtime_requirements_sha256
                            ),
                            running_python=installation.running_python,
                            platform=installation.platform,
                        )
                except Exception:
                    descriptor = None
            if descriptor is not None and monotonic() >= deadline_at:
                descriptor = None
            if descriptor is not None:
                snapshot["automatic_update"] = True
                snapshot["status"] = "available_automatic"
    return DiscoveryResult(MappingProxyType(snapshot), descriptor)


def _manifest_content_length(response: _Response) -> int:
    get_all = getattr(response.headers, "get_all", None)
    if not callable(get_all):
        raise ValueError("release manifest headers are invalid")
    values = get_all("Content-Length", [])
    if len(values) != 1:
        raise ValueError("release manifest Content-Length is invalid")
    value = values[0]
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdigit()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ValueError("release manifest Content-Length is invalid")
    return int(value)


def _read_manifest(
    response: _Response,
    *,
    expected_size: int,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if monotonic() >= deadline_at:
            raise TimeoutError("release manifest deadline expired")
        chunk = response.read(min(64 * 1024, expected_size - total + 1))
        if type(chunk) is not bytes:
            raise ValueError("release manifest response body is invalid")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > expected_size:
            raise ValueError("release manifest exceeds declared size")
    if total != expected_size:
        raise ValueError("release manifest is truncated")
    return b"".join(chunks)


def verify_release_record(
    release: ListedRelease,
    *,
    open_url: OpenUrl = _safe_open_url,
    deadline_at: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> VerifiedRelease:
    """Verify exact release metadata and its downloaded canonical manifest."""

    canonical_version(release.version)
    base = release_urls(release.version)["wheel_prefix"]
    expected_names = {
        UPDATE_MANIFEST_FILENAME,
        f"arxiv_digest-{release.version}-py3-none-any.whl",
        f"arxiv_digest-{release.version}.tar.gz",
        "SHA256SUMS",
    }
    if (
        release.assets != tuple(sorted(release.assets, key=lambda asset: asset.name))
        or {asset.name for asset in release.assets} != expected_names
        or len(release.assets) != len(expected_names)
    ):
        raise ValueError("release asset inventory is invalid")
    for asset in release.assets:
        if (
            type(asset.name) is not str
            or type(asset.size) is not int
            or not 1 <= asset.size
            or type(asset.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", asset.sha256) is None
            or asset.url != f"{base}{asset.name}"
        ):
            raise ValueError("release asset metadata is invalid")
    by_name = {asset.name: asset for asset in release.assets}
    manifest_asset = by_name[UPDATE_MANIFEST_FILENAME]
    if manifest_asset.size > MANIFEST_BYTE_LIMIT:
        raise ValueError("release manifest exceeds byte limit")
    if deadline_at is None:
        deadline_at = monotonic() + DISCOVERY_DEADLINE_SECONDS
    response = open_release_asset(
        manifest_asset.url,
        open_url=open_url,
        deadline_at=deadline_at,
        monotonic=monotonic,
    )
    try:
        if (
            response.status != 200
            or _response_content_type(response) not in MANIFEST_MEDIA_TYPES
            or response.headers.get("Content-Encoding") is not None
            or _manifest_content_length(response) != manifest_asset.size
        ):
            raise ValueError("release manifest response is invalid")
        payload = _read_manifest(
            response,
            expected_size=manifest_asset.size,
            deadline_at=deadline_at,
            monotonic=monotonic,
        )
    finally:
        response.close()
    if hashlib.sha256(payload).hexdigest() != manifest_asset.sha256:
        raise ValueError("release manifest digest is invalid")
    manifest = parse_update_manifest(payload)
    wheel = by_name[f"arxiv_digest-{release.version}-py3-none-any.whl"]
    if (
        manifest.version != release.version
        or manifest.channel != release.channel
        or manifest.wheel.name != wheel.name
        or manifest.wheel.size != wheel.size
        or manifest.wheel.sha256 != wheel.sha256
    ):
        raise ValueError("release manifest does not match release metadata")
    return VerifiedRelease(
        release_notes_url=release_urls(release.version)["notes"],
        manifest=manifest,
        manifest_asset=manifest_asset,
        wheel_asset=wheel,
    )
