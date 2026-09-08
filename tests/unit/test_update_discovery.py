from __future__ import annotations

import hashlib
import io
import json
from dataclasses import FrozenInstanceError, replace
from email.message import Message
from pathlib import Path

import pytest

from tests.update_wheel_factory import write_valid_wheel


_API_URL = (
    "https://api.github.com/repos/yuzhangmath/arxiv-digest/"
    "releases?per_page=100"
)
_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "update"


class _Response(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        link: str | None = None,
        content_type: str = "application/json",
        content_length: int | None = None,
    ) -> None:
        super().__init__(payload)
        self.status = 200
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if link is not None:
            self.headers["Link"] = link

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _two_page_release_server(opened: list[str]):
    pages = {
        _API_URL: (
            _FIXTURE_ROOT / "github-releases-page-1.json"
        ).read_bytes(),
        f"{_API_URL}&page=2": (
            _FIXTURE_ROOT / "github-releases-page-2.json"
        ).read_bytes(),
    }

    def open_url(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        opened.append(url)
        link = (
            f'<{_API_URL}&page=2>; rel="next"'
            if url == _API_URL
            else None
        )
        return _Response(pages[url], url=url, link=link)

    return open_url


def _valid_release_record(tmp_path: Path, *, version: str = "0.3.1"):
    from arxiv_digest.update_contract import release_urls
    from arxiv_digest.update_discovery import GitHubAsset, ListedRelease
    from arxiv_digest.update_manifest import (
        build_update_manifest,
        serialize_update_manifest,
    )

    wheel_path = write_valid_wheel(
        tmp_path / f"arxiv_digest-{version}-py3-none-any.whl",
        version=version,
    )
    manifest = build_update_manifest(
        wheel_path,
        version=version,
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    manifest_bytes = serialize_update_manifest(manifest)
    base = release_urls(version)["wheel_prefix"]
    assets = tuple(
        sorted(
            (
                GitHubAsset(
                    "UPDATE_MANIFEST.json",
                    len(manifest_bytes),
                    hashlib.sha256(manifest_bytes).hexdigest(),
                    f"{base}UPDATE_MANIFEST.json",
                ),
                GitHubAsset(
                    manifest.wheel.name,
                    manifest.wheel.size,
                    manifest.wheel.sha256,
                    f"{base}{manifest.wheel.name}",
                ),
                GitHubAsset(
                    f"arxiv_digest-{version}.tar.gz",
                    1,
                    "a" * 64,
                    f"{base}arxiv_digest-{version}.tar.gz",
                ),
                GitHubAsset(
                    "SHA256SUMS",
                    1,
                    "b" * 64,
                    f"{base}SHA256SUMS",
                ),
            ),
            key=lambda asset: asset.name,
        )
    )
    return ListedRelease(version, "prerelease", assets), manifest, manifest_bytes


def test_automatic_update_descriptor_retains_verified_releases_and_installation(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_discovery import automatic_update_descriptor

    installed, target, installation = _eligible_bundle(tmp_path)
    descriptor = automatic_update_descriptor(
        installed=installed,
        target=target,
        installation=installation,
        local_requirements_sha256=installation.runtime_requirements_sha256,
        running_python=installation.running_python,
        platform=installation.platform,
    )

    assert descriptor is not None
    assert descriptor.installed is installed
    assert descriptor.target is target
    assert descriptor.installation is installation
    with pytest.raises(FrozenInstanceError):
        descriptor.target = installed


def _bound_release(manifest):
    from arxiv_digest.update_contract import UPDATE_MANIFEST_FILENAME, release_urls
    from arxiv_digest.update_discovery import GitHubAsset, VerifiedRelease
    from arxiv_digest.update_manifest import serialize_update_manifest

    payload = serialize_update_manifest(manifest)
    urls = release_urls(manifest.version)
    return VerifiedRelease(
        urls["notes"], manifest,
        GitHubAsset(
            UPDATE_MANIFEST_FILENAME, len(payload), hashlib.sha256(payload).hexdigest(),
            f"{urls['wheel_prefix']}{UPDATE_MANIFEST_FILENAME}",
        ),
        GitHubAsset(
            manifest.wheel.name, manifest.wheel.size, manifest.wheel.sha256,
            f"{urls['wheel_prefix']}{manifest.wheel.name}",
        ),
    )


def _eligible_bundle(tmp_path: Path):
    from arxiv_digest.update_manifest import AutomaticUpdateFrom
    from tests.update_installation_factory import eligible_installation

    _, installed, _ = _valid_release_record(tmp_path, version="0.3.0")
    _, target, _ = _valid_release_record(tmp_path, version="0.3.2")
    target = replace(
        target, automatic_update=True,
        automatic_update_from=AutomaticUpdateFrom("0.3.0", "0.3.2", ()),
    )
    return _bound_release(installed), _bound_release(target), eligible_installation(tmp_path)


def _automatic(installed, target, installation, **overrides):
    from arxiv_digest.update_discovery import automatic_update_descriptor

    arguments = {
        "installed": installed,
        "target": target,
        "installation": installation,
        "local_requirements_sha256": installation.runtime_requirements_sha256,
        "running_python": installation.running_python,
        "platform": installation.platform,
    }
    arguments.update(overrides)
    return automatic_update_descriptor(**arguments)


@pytest.mark.parametrize("boundary", ["opt-out", "below-minimum", "exclusive-maximum", "excluded"])
def test_publisher_must_explicitly_allow_the_installed_source_version(
    tmp_path: Path, boundary: str,
) -> None:
    from arxiv_digest.update_manifest import AutomaticUpdateFrom

    installed, target, installation = _eligible_bundle(tmp_path)
    source = {
        "opt-out": None,
        "below-minimum": AutomaticUpdateFrom("0.3.1", "0.3.2", ()),
        "exclusive-maximum": AutomaticUpdateFrom("0.2.0", "0.3.0", ()),
        "excluded": AutomaticUpdateFrom("0.3.0", "0.3.2", ("0.3.0",)),
    }[boundary]
    target = _bound_release(replace(
        target.manifest, automatic_update=source is not None, automatic_update_from=source,
    ))
    assert _automatic(installed, target, installation) is None


@pytest.mark.parametrize("release_side", ["installed", "target"])
@pytest.mark.parametrize("mismatch", [
    "protocol", "generation", "schema", "product", "noncanonical-version",
    "wheel-name", "wheel-size", "wheel-digest", "wheel-url", "manifest-digest",
    "manifest-size", "manifest-url", "notes-url", "manifest-channel",
])
def test_both_verified_release_records_must_keep_their_exact_manifest_and_assets(
    tmp_path: Path, release_side: str, mismatch: str,
) -> None:
    installed, target, installation = _eligible_bundle(tmp_path)
    release = installed if release_side == "installed" else target
    bad_manifest = {
        "protocol": {"updater_protocol": 2},
        "generation": {"application_data_generation": 3},
        "schema": {"schema_version": 2},
        "product": {"product": "different-product"},
        "noncanonical-version": {"version": "00.3.0"},
        "manifest-channel": {"channel": "stable"},
    }
    if mismatch in bad_manifest:
        release = replace(release, manifest=replace(release.manifest, **bad_manifest[mismatch]))
    elif mismatch == "notes-url":
        release = replace(release, release_notes_url="https://example.invalid/release")
    else:
        kind, field = mismatch.split("-")
        asset = release.wheel_asset if kind == "wheel" else release.manifest_asset
        fields = {
            "name": {"name": "arxiv_digest-0.3.2-cp311-linux.whl"},
            "size": {"size": asset.size + 1},
            "digest": {"sha256": "f" * 64},
            "url": {"url": "https://example.invalid/asset"},
        }
        release = replace(release, **{f"{kind}_asset": replace(asset, **fields[field])})
    if release_side == "installed":
        installed = release
    else:
        target = release
    assert _automatic(installed, target, installation) is None


@pytest.mark.parametrize("release_side", ["installed", "target"])
@pytest.mark.parametrize("boundary", ["platform", "python-minimum", "python-maximum", "requirements"])
def test_both_manifests_must_support_running_platform_python_and_same_requirements(
    tmp_path: Path, release_side: str, boundary: str,
) -> None:
    from arxiv_digest.update_manifest import PythonPolicy

    installed, target, installation = _eligible_bundle(tmp_path)
    release = installed if release_side == "installed" else target
    excluded_platform = "darwin" if installation.platform == "linux" else "linux"
    major, minor = installation.running_python
    changes = {
        "platform": {"platforms": (excluded_platform,)},
        "python-minimum": {"python": PythonPolicy(f"{major}.{minor + 1}", None)},
        "python-maximum": {"python": PythonPolicy("3.0", f"{major}.{minor}")},
        "requirements": {"runtime_requirements_sha256": "c" * 64},
    }
    release = _bound_release(replace(release.manifest, **changes[boundary]))
    if release_side == "installed":
        installed = release
    else:
        target = release
    assert _automatic(installed, target, installation) is None


@pytest.mark.parametrize("mismatch", [
    "installation-version", "distribution-version", "distribution-name",
    "local-requirements", "captured-requirements", "distribution-requirements",
    "local-python", "unrepresentable-python", "unsupported-platform",
    "different-platform", "different-interpreter", "invalid-interpreter",
])
def test_live_distribution_metadata_must_agree_with_the_installed_manifest(
    tmp_path: Path, mismatch: str,
) -> None:
    installed, target, installation = _eligible_bundle(tmp_path)
    arguments = {}
    if mismatch == "installation-version":
        installation = replace(installation, version="0.2.9")
    elif mismatch == "captured-requirements":
        installation = replace(installation, runtime_requirements_sha256="d" * 64)
    elif mismatch == "local-requirements":
        arguments["local_requirements_sha256"] = "d" * 64
    elif mismatch == "unsupported-platform":
        arguments["platform"] = "win32"
    elif mismatch == "different-platform":
        arguments["platform"] = "darwin" if installation.platform == "linux" else "linux"
    elif mismatch == "different-interpreter":
        arguments["running_python"] = (3, 98)
    elif mismatch == "invalid-interpreter":
        arguments["running_python"] = (True, 11)
    else:
        changes = {
            "distribution-version": {"version": "0.2.9"},
            "distribution-name": {"name": "different-product"},
            "distribution-requirements": {"requirements": ("new-dependency==1",)},
            "local-python": {"requires_python": ">=3.10"},
            "unrepresentable-python": {"requires_python": ">=3.11,!=3.12"},
        }
        installation = replace(
            installation, distribution=replace(installation.distribution, **changes[mismatch]),
        )
    assert _automatic(installed, target, installation, **arguments) is None


def test_equal_or_older_releases_never_get_an_automatic_descriptor(tmp_path: Path) -> None:
    installed, target, installation = _eligible_bundle(tmp_path)
    assert _automatic(installed, installed, installation) is None
    assert _automatic(target, installed, replace(installation, version=target.version)) is None


def _release_server(releases, opened, *, corrupt_version=None):
    from arxiv_digest.update_contract import release_urls
    from arxiv_digest.update_manifest import serialize_update_manifest

    records = []
    manifests = {}
    for release in releases:
        base = release_urls(release.version)["wheel_prefix"]
        assets = [
            {"name": asset.name, "size": asset.size, "digest": f"sha256:{asset.sha256}",
             "browser_download_url": asset.url, "state": "uploaded"}
            for asset in (release.manifest_asset, release.wheel_asset)
        ]
        for name in (f"arxiv_digest-{release.version}.tar.gz", "SHA256SUMS"):
            assets.append({
                "name": name, "size": 1, "digest": f"sha256:{'a' * 64}",
                "browser_download_url": f"{base}{name}", "state": "uploaded",
            })
        records.append({
            "tag_name": f"v{release.version}", "draft": False,
            "prerelease": release.channel == "prerelease", "assets": assets,
        })
        manifests[release.manifest_asset.url] = (
            b"private invalid manifest" if release.version == corrupt_version
            else serialize_update_manifest(release.manifest)
        )

    def open_url(request, *, timeout):
        assert 0 < timeout <= 3
        url = request.full_url
        opened.append(url)
        payload = json.dumps(records).encode() if url == _API_URL else manifests[url]
        return _Response(payload, url=url, content_length=len(payload))

    return open_url


def test_discovery_verifies_both_releases_and_keeps_descriptor_out_of_public_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_discovery import discover_updates

    installed, target, installation = _eligible_bundle(tmp_path)
    opened = []
    result = discover_updates(
        current_version=installed.version, installation=installation,
        open_url=_release_server((installed, target), opened),
    )

    assert opened == [_API_URL, target.manifest_asset.url, installed.manifest_asset.url]
    assert result.descriptor is not None
    assert result.descriptor.installation is installation
    assert result.descriptor.installed == installed
    assert result.descriptor.target == target
    assert dict(result.public_snapshot) == {
        "status": "available_automatic", "automatic_update": True,
        "installed_version": installed.version, "available_version": target.version,
        "release_notes_url": target.release_notes_url,
    }


@pytest.mark.parametrize("failure", ["ineligible-installation", "missing-installed", "missing-target", "opt-out"])
def test_manual_updates_skip_manifest_requests_that_cannot_establish_eligibility(
    tmp_path: Path, failure: str,
) -> None:
    from arxiv_digest.update_discovery import discover_updates

    installed, target, installation = _eligible_bundle(tmp_path)
    releases = (installed, target)
    if failure == "ineligible-installation":
        installation = None
    elif failure == "missing-installed":
        releases = (target,)
    elif failure == "missing-target":
        releases = (installed,)
    else:
        target = _bound_release(replace(
            target.manifest, automatic_update=False, automatic_update_from=None,
        ))
        releases = (installed, target)
    opened = []
    result = discover_updates(
        current_version=installed.version, installation=installation,
        open_url=_release_server(releases, opened),
    )

    assert result.descriptor is None
    assert result.public_snapshot["automatic_update"] is False
    assert result.public_snapshot["status"] == (
        "current" if failure == "missing-target" else "available_manual"
    )
    assert opened == (
        [_API_URL, target.manifest_asset.url] if failure == "opt-out" else [_API_URL]
    )


@pytest.mark.parametrize("corrupt_side", ["installed", "target"])
def test_manifest_failures_preserve_the_manual_link_without_private_details(
    tmp_path: Path, corrupt_side: str,
) -> None:
    from arxiv_digest.update_discovery import discover_updates

    installed, target, installation = _eligible_bundle(tmp_path)
    opened = []
    corrupted = installed if corrupt_side == "installed" else target
    result = discover_updates(
        current_version=installed.version, installation=installation,
        open_url=_release_server((installed, target), opened, corrupt_version=corrupted.version),
    )
    assert result.descriptor is None
    assert dict(result.public_snapshot) == {
        "status": "available_manual", "automatic_update": False,
        "installed_version": installed.version, "available_version": target.version,
        "release_notes_url": target.release_notes_url,
    }


def test_ineligible_highest_release_does_not_fall_back_to_an_older_automatic_target(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_discovery import discover_updates

    installed, target, installation = _eligible_bundle(tmp_path)
    _, newer, _ = _valid_release_record(tmp_path, version="0.3.3")
    latest = _bound_release(newer)
    opened = []
    result = discover_updates(
        current_version=installed.version, installation=installation,
        open_url=_release_server((installed, target, latest), opened),
    )
    assert result.descriptor is None
    assert result.public_snapshot["status"] == "available_manual"
    assert result.public_snapshot["available_version"] == "0.3.3"
    assert opened == [_API_URL, latest.manifest_asset.url]


def test_discovery_rejects_automatic_capability_if_final_manifest_read_finishes_late(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_discovery import discover_updates

    installed, target, installation = _eligible_bundle(tmp_path)
    now = [10.0]
    server = _release_server((installed, target), [])

    def open_url(request, **kwargs):
        response = server(request, **kwargs)
        if request.full_url == installed.manifest_asset.url:
            original_read = response.read

            def late_eof(size=-1):
                chunk = original_read(size)
                if not chunk:
                    now[0] = 20.0
                return chunk

            response.read = late_eof
        return response

    result = discover_updates(
        current_version=installed.version, installation=installation,
        open_url=open_url, deadline_at=20.0, monotonic=lambda: now[0],
    )
    assert result.descriptor is None
    assert result.public_snapshot["status"] == "available_manual"


@pytest.mark.parametrize("entrypoint", ["discovery", "checker"])
def test_production_transport_keeps_manifest_redirects_visible_for_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    import time
    import urllib.request

    from arxiv_digest import update_http
    from arxiv_digest.update_check import UpdateChecker
    from arxiv_digest.update_discovery import discover_updates
    from arxiv_digest.update_installation import InstallationDetection
    from arxiv_digest.update_manifest import serialize_update_manifest

    installed, target, installation = _eligible_bundle(tmp_path)
    requests = []
    server = _release_server((installed, target), [])
    redirects = {
        release.manifest_asset.url: (
            f"https://release-assets.githubusercontent.com/assets/{release.version}/manifest",
            serialize_update_manifest(release.manifest),
        )
        for release in (installed, target)
    }
    redirected_payloads = {url: payload for url, payload in redirects.values()}

    class Opener:
        def __init__(self, *, automatic_redirects):
            self.automatic_redirects = automatic_redirects

        def open(self, request, data=None, timeout=None):
            del data
            url = request.full_url
            requests.append(url)
            if url == _API_URL:
                return server(request, timeout=timeout)
            if url in redirects:
                redirected, payload = redirects[url]
                if self.automatic_redirects:
                    return _Response(payload, url=redirected, content_length=len(payload))
                response = _Response(b"", url=url)
                response.status = 302
                response.headers["Location"] = redirected
                return response
            payload = redirected_payloads[url]
            return _Response(payload, url=url, content_length=len(payload))

    def build_opener(*handlers):
        assert any(isinstance(handler, update_http._NoRedirect) for handler in handlers)
        return Opener(automatic_redirects=False)

    monkeypatch.setattr(urllib.request, "_opener", Opener(automatic_redirects=True))
    monkeypatch.setattr(update_http, "build_opener", build_opener)
    if entrypoint == "discovery":
        snapshot = discover_updates(
            current_version=installed.version, installation=installation,
        ).public_snapshot
    else:
        checker = UpdateChecker(
            current_version=installed.version,
            detect_installation=lambda **kwargs: InstallationDetection(installation),
        )
        checker.start()
        deadline = time.monotonic() + 1
        while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
            time.sleep(0.005)
        snapshot = checker.snapshot()

    assert snapshot["status"] == "available_automatic"
    assert requests == [
        _API_URL,
        target.manifest_asset.url, redirects[target.manifest_asset.url][0],
        installed.manifest_asset.url, redirects[installed.manifest_asset.url][0],
    ]


def test_complete_pagination_selects_the_highest_strict_release() -> None:
    from arxiv_digest.update_discovery import discover_updates

    opened: list[str] = []
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=_two_page_release_server(opened),
    )

    assert opened == [_API_URL, f"{_API_URL}&page=2"]
    assert result.public_snapshot == {
        "automatic_update": False,
        "available_version": "0.3.2",
        "installed_version": "0.3.0",
        "release_notes_url": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.2"
        ),
        "status": "available_manual",
    }
    assert result.descriptor is None


def test_transport_failure_projects_only_generic_manual_fallback() -> None:
    from arxiv_digest.update_discovery import discover_updates

    def unavailable(request: object, *, timeout: float) -> _Response:
        del request, timeout
        raise OSError("private transport detail")

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=unavailable,
    )

    assert result.public_snapshot == {
        "automatic_update": False,
        "installed_version": "0.3.0",
        "release_notes_url": (
            "https://github.com/yuzhangmath/arxiv-digest/releases"
        ),
        "status": "manual_fallback",
    }
    assert "private transport detail" not in repr(result.public_snapshot)


def test_discovery_result_copies_and_freezes_its_public_snapshot() -> None:
    from arxiv_digest.update_discovery import DiscoveryResult

    source: dict[str, bool | str] = {
        "automatic_update": False,
        "installed_version": "0.3.0",
        "status": "current",
    }
    result = DiscoveryResult(source)
    source["status"] = "manual_fallback"

    assert result.public_snapshot["status"] == "current"
    with pytest.raises(TypeError):
        result.public_snapshot["status"] = "manual_fallback"  # type: ignore[index]


def test_complete_list_without_a_newer_release_projects_current() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = json.dumps(
        [
            {"tag_name": "v0.3.0", "draft": False, "prerelease": True},
            {"tag_name": "v0.2.9", "draft": False, "prerelease": False},
            {"tag_name": "v9.0.0", "draft": True, "prerelease": False},
            {"tag_name": "release-next", "draft": False},
        ]
    ).encode("utf-8")

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot == {
        "automatic_update": False,
        "installed_version": "0.3.0",
        "status": "current",
    }


def test_duplicate_github_keys_fail_closed_to_manual_fallback() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = (
        b'[{"draft":false,"prerelease":false,'
        b'"tag_name":"v0.3.1","tag_name":"v9.0.0"}]'
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"
    assert "available_version" not in result.public_snapshot


def test_non_boolean_draft_flag_fails_closed_instead_of_claiming_current() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = (
        b'[{"draft":"false","prerelease":false,'
        b'"tag_name":"v0.3.1"}]'
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_non_boolean_prerelease_flag_fails_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = (
        b'[{"assets":[],"draft":false,"prerelease":"false",'
        b'"tag_name":"v0.3.1"}]'
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_non_object_release_record_fails_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            b"[null]",
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_conflicting_duplicate_published_versions_fail_closed_in_any_order() -> None:
    from arxiv_digest.update_discovery import discover_updates

    records = (
        {
            "assets": [],
            "draft": False,
            "prerelease": False,
            "tag_name": "v0.3.1",
        },
        {
            "assets": [],
            "draft": False,
            "prerelease": True,
            "tag_name": "v0.3.1",
        },
    )
    for ordered_records in (records, tuple(reversed(records))):
        payload = json.dumps(ordered_records).encode("utf-8")
        result = discover_updates(
            current_version="0.3.0",
            installation=None,
            open_url=lambda request, *, timeout: _Response(
                payload,
                url=request.full_url,
            ),
        )

        assert result.public_snapshot["status"] == "manual_fallback"


def test_release_pages_are_streamed_to_eof_under_the_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_discovery

    response = _Response(b"[]", url=_API_URL)
    reads: list[int] = []
    original_read = response.read

    def tracked_read(size: int = -1) -> bytes:
        reads.append(size)
        return original_read(size)

    response.read = tracked_read  # type: ignore[method-assign]
    monkeypatch.setattr(update_discovery, "RELEASE_PAGE_BYTE_LIMIT", 8, raising=False)

    result = update_discovery.discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: response,
    )

    assert result.public_snapshot["status"] == "current"
    assert reads == [9, 7]


def test_nonfinite_github_values_fail_closed_before_selection() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = (
        b'[{"assets":[],"draft":false,"prerelease":NaN,'
        b'"tag_name":"v0.3.1"}]'
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_encoded_release_pages_fail_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    response = _Response(b"[]", url=_API_URL)
    response.headers["Content-Encoding"] = "gzip"

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: response,
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_remaining_next_link_at_request_cap_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_discovery

    monkeypatch.setattr(
        update_discovery,
        "RELEASE_LIST_REQUEST_LIMIT",
        1,
        raising=False,
    )
    opened: list[str] = []

    result = update_discovery.discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=_two_page_release_server(opened),
    )

    assert opened == [_API_URL]
    assert result.public_snapshot["status"] == "manual_fallback"


def test_record_cap_counts_records_that_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_discovery

    monkeypatch.setattr(update_discovery, "RELEASE_RECORD_LIMIT", 1, raising=False)
    payload = json.dumps(
        [
            {"draft": True, "tag_name": "v9.0.0"},
            {"draft": False, "prerelease": True, "tag_name": "v0.3.1"},
        ]
    ).encode("utf-8")

    result = update_discovery.discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_discovery_starts_no_request_after_its_absolute_deadline() -> None:
    from arxiv_digest.update_discovery import discover_updates

    opened: list[object] = []
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: opened.append(request),
        deadline_at=10.0,
        monotonic=lambda: 10.0,
    )

    assert opened == []
    assert result.public_snapshot["status"] == "manual_fallback"


def test_verified_release_binds_manifest_to_the_exact_release_assets(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_contract import release_urls
    from arxiv_digest.update_discovery import verify_release_record

    release, manifest, manifest_bytes = _valid_release_record(tmp_path)

    verified = verify_release_record(
        release,
        open_url=lambda request, *, timeout: _Response(
            manifest_bytes,
            url=request.full_url,
            content_type="application/octet-stream",
            content_length=len(manifest_bytes),
        ),
    )

    manifest_asset = next(
        asset
        for asset in release.assets
        if asset.name == "UPDATE_MANIFEST.json"
    )
    wheel_asset = next(
        asset for asset in release.assets if asset.name == manifest.wheel.name
    )

    assert verified.release_notes_url == release_urls(release.version)["notes"]
    assert verified.manifest == manifest
    assert verified.manifest_asset == manifest_asset
    assert verified.wheel_asset == wheel_asset
    assert verified.version == manifest.version
    assert verified.channel == manifest.channel


def test_verified_release_rejects_ambiguous_manifest_content_type(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_discovery import verify_release_record

    release, _manifest, manifest_bytes = _valid_release_record(tmp_path)
    response = _Response(
        manifest_bytes,
        url=next(
            asset.url
            for asset in release.assets
            if asset.name == "UPDATE_MANIFEST.json"
        ),
        content_type="application/json",
        content_length=len(manifest_bytes),
    )
    response.headers["Content-Type"] = "application/octet-stream"

    with pytest.raises(ValueError, match="response"):
        verify_release_record(
            release,
            open_url=lambda request, *, timeout: response,
        )


def test_duplicate_pagination_headers_fail_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    response = _Response(
        b"[]",
        url=_API_URL,
        link=f'<{_API_URL}&page=2>; rel="next"',
    )
    response.headers["Link"] = f'<{_API_URL}&page=2>; rel="next"'
    final = _Response(b"[]", url=f"{_API_URL}&page=2")

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: (
            response if request.full_url == _API_URL else final
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_duplicate_next_relations_in_one_header_fail_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    response = _Response(
        b"[]",
        url=_API_URL,
        link=(
            f'<{_API_URL}&page=2>; rel="next", '
            f'<{_API_URL}&page=2>; rel="next"'
        ),
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: response,
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_listed_release_normalizes_exact_uploaded_assets_by_name() -> None:
    from arxiv_digest import update_discovery
    from arxiv_digest.update_contract import release_urls

    version = "0.3.1"
    base = release_urls(version)["wheel_prefix"]
    raw_assets = [
        {
            "browser_download_url": f"{base}{name}",
            "digest": f"sha256:{digest}",
            "name": name,
            "size": size,
            "state": "uploaded",
        }
        for name, size, digest in (
            ("SHA256SUMS", 17, "d" * 64),
            (f"arxiv_digest-{version}.tar.gz", 15, "c" * 64),
            (f"arxiv_digest-{version}-py3-none-any.whl", 13, "b" * 64),
            ("UPDATE_MANIFEST.json", 11, "a" * 64),
        )
    ]
    payload = json.dumps(
        [
            {
                "assets": raw_assets,
                "draft": False,
                "prerelease": True,
                "tag_name": f"v{version}",
            }
        ]
    ).encode("utf-8")

    releases = update_discovery._fetch_complete_releases(
        lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
        deadline_at=20.0,
        monotonic=lambda: 10.0,
    )

    assert [asset.name for asset in releases[0].assets] == sorted(
        asset["name"] for asset in raw_assets
    )
    manifest_asset = next(
        asset
        for asset in releases[0].assets
        if asset.name == "UPDATE_MANIFEST.json"
    )
    assert manifest_asset.sha256 == "a" * 64


def test_release_page_streaming_obeys_the_same_absolute_deadline() -> None:
    from arxiv_digest.update_discovery import discover_updates

    now = [0.0]
    response = _Response(b"[]", url=_API_URL)
    original_read = response.read

    def delayed_read(size: int = -1) -> bytes:
        chunk = original_read(size)
        now[0] = 2.0
        return chunk

    response.read = delayed_read  # type: ignore[method-assign]
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: response,
        deadline_at=1.0,
        monotonic=lambda: now[0],
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_missing_release_channel_fails_closed() -> None:
    from arxiv_digest.update_discovery import discover_updates

    payload = json.dumps(
        [
            {
                "assets": [],
                "draft": False,
                "tag_name": "v0.3.1",
            }
        ]
    ).encode("utf-8")
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: _Response(
            payload,
            url=request.full_url,
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_next_link_rejects_extra_relation_parameters() -> None:
    from arxiv_digest.update_discovery import discover_updates

    first = _Response(
        b"[]",
        url=_API_URL,
        link=(
            f'<{_API_URL}&page=2>; rel="next"; title="not-canonical"'
        ),
    )
    final = _Response(b"[]", url=f"{_API_URL}&page=2")

    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: (
            first if request.full_url == _API_URL else final
        ),
    )

    assert result.public_snapshot["status"] == "manual_fallback"


def test_final_page_accepts_well_formed_non_next_link_relations() -> None:
    from arxiv_digest.update_discovery import discover_updates

    response = _Response(
        b"[]",
        url=_API_URL,
        link=(
            f'<{_API_URL}>; rel="first", '
            f'<{_API_URL}>; rel="prev"'
        ),
    )
    result = discover_updates(
        current_version="0.3.0",
        installation=None,
        open_url=lambda request, *, timeout: response,
    )

    assert result.public_snapshot["status"] == "current"
