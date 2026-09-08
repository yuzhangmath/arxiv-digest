#!/usr/bin/env python3
"""Stdlib verifier embedded literally in the write-enabled release workflow.

The workflow executes this trusted inline source, never a downloaded script or
an import from the downloaded wheel. Keep the embedded source synchronized.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

MANIFEST_BYTE_LIMIT = 1024 * 1024
RELEASE_PAGE_BYTE_LIMIT = 4 * 1024 * 1024
REPOSITORY = "https://github.com/yuzhangmath/arxiv-digest"


def canonical_version(value):
    if type(value) is not str or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("noncanonical version")
    return tuple(map(int, value.split(".")))


class BundleError(ValueError):
    """A local or published release bundle violates the closed contract."""


@dataclass(frozen=True, slots=True)
class BundleAsset:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    version: str
    commit: str
    channel: str
    assets: tuple[BundleAsset, ...]
    release_notes_sha256: str


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise BundleError("published release JSON contains duplicate keys")
        value[key] = member
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise BundleError("published release JSON contains a nonfinite number")


def _asset(path: Path) -> BundleAsset:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise BundleError("local release asset is not a regular file")
            digest = hashlib.sha256()
            size = 0
            remaining = before.st_size + 1
            while remaining and (chunk := source.read(min(64 * 1024, remaining))):
                digest.update(chunk)
                size += len(chunk)
                remaining -= len(chunk)
            after = os.fstat(source.fileno())
    except BundleError:
        raise
    except OSError as error:
        raise BundleError("local release asset could not be read") from error
    if _file_identity(before) != _file_identity(after) or size != before.st_size:
        raise BundleError("local release asset changed during verification")
    return BundleAsset(path.name, size, digest.hexdigest())


def _asset_from_payload(name: str, payload: bytes) -> BundleAsset:
    return BundleAsset(name, len(payload), hashlib.sha256(payload).hexdigest())


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular_bytes(path: Path, *, byte_limit: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit:
                raise BundleError("local release file is invalid")
            payload = source.read(byte_limit + 1)
            after = os.fstat(source.fileno())
    except BundleError:
        raise
    except OSError as error:
        raise BundleError("local release file could not be read") from error
    if (
        len(payload) != before.st_size
        or len(payload) > byte_limit
        or _file_identity(before) != _file_identity(after)
    ):
        raise BundleError("local release file changed during verification")
    return payload


def verify_github_release(
    release_payload: bytes,
    *,
    bundle: VerifiedBundle,
    tag: str,
    remote_tag_commit: str,
) -> None:
    """Verify a GitHub release response against an already verified bundle."""

    if type(bundle) is not VerifiedBundle:
        raise BundleError("verified bundle record is invalid")
    try:
        canonical_version(bundle.version)
    except ValueError as error:
        raise BundleError("verified bundle record is invalid") from error
    expected_asset_names = (
        f"arxiv_digest-{bundle.version}-py3-none-any.whl",
        f"arxiv_digest-{bundle.version}.tar.gz",
        "UPDATE_MANIFEST.json",
        "SHA256SUMS",
    )
    if (
        type(bundle.commit) is not str
        or re.fullmatch(r"[0-9a-f]{40}", bundle.commit) is None
        or type(bundle.channel) is not str
        or bundle.channel not in {"prerelease", "stable"}
        or type(bundle.assets) is not tuple
        or len(bundle.assets) != len(expected_asset_names)
        or any(type(asset) is not BundleAsset for asset in bundle.assets)
        or any(
            type(asset.name) is not str
            or type(asset.size) is not int
            or asset.size <= 0
            or type(asset.sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", asset.sha256) is None
            for asset in bundle.assets
        )
        or tuple(asset.name for asset in bundle.assets) != expected_asset_names
        or type(bundle.release_notes_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", bundle.release_notes_sha256) is None
    ):
        raise BundleError("verified bundle record is invalid")
    if type(release_payload) is not bytes:
        raise BundleError("published release response is invalid")
    if len(release_payload) > RELEASE_PAGE_BYTE_LIMIT:
        raise BundleError("published release response exceeds the byte limit")
    try:
        release = json.loads(
            release_payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except BundleError:
        raise
    except (
        RecursionError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise BundleError("published release response is invalid") from error

    expected_tag = f"v{bundle.version}"
    if type(tag) is not str or tag != expected_tag:
        raise BundleError("published release tag is invalid")
    if type(remote_tag_commit) is not str or remote_tag_commit != bundle.commit:
        raise BundleError("published release tag commit is invalid")
    if type(release) is not dict:
        raise BundleError("published release record is invalid")
    expected_prerelease = bundle.channel == "prerelease"
    if (
        release.get("tag_name") != expected_tag
        or release.get("draft") is not False
        or release.get("prerelease") is not expected_prerelease
    ):
        raise BundleError("published release identity is invalid")
    body = release.get("body")
    if type(body) is not str:
        raise BundleError("published release notes are invalid")
    try:
        body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as error:
        raise BundleError("published release notes are invalid") from error
    if body_digest != bundle.release_notes_sha256:
        raise BundleError("published release notes do not match")

    assets = release.get("assets")
    if type(assets) is not list or len(assets) != len(bundle.assets):
        raise BundleError("published release asset inventory is invalid")
    records: dict[str, dict[str, object]] = {}
    for record in assets:
        if type(record) is not dict or type(record.get("name")) is not str:
            raise BundleError("published release asset record is invalid")
        name = record["name"]
        if name in records:
            raise BundleError("published release asset names are duplicated")
        records[name] = record
    if set(records) != {asset.name for asset in bundle.assets}:
        raise BundleError("published release asset inventory is invalid")

    base_url = f"{REPOSITORY}/releases/download/{expected_tag}/"
    for asset in bundle.assets:
        record = records[asset.name]
        if (
            record.get("state") != "uploaded"
            or type(record.get("size")) is not int
            or record.get("size") != asset.size
            or record.get("digest") != f"sha256:{asset.sha256}"
            or record.get("browser_download_url") != f"{base_url}{asset.name}"
        ):
            raise BundleError("published release asset does not match")


def verify_downloaded_bundle(root: Path, *, version: str, commit: str) -> VerifiedBundle:
    """Verify authenticated build output without executing its contents."""
    canonical_version(version)
    if type(commit) is not str or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BundleError("invalid build commit")
    if root.is_symlink() or not root.is_dir():
        raise BundleError("invalid bundle directory")
    wheel = f"arxiv_digest-{version}-py3-none-any.whl"
    sdist = f"arxiv_digest-{version}.tar.gz"
    names = {wheel, sdist, "UPDATE_MANIFEST.json", "SHA256SUMS", "RELEASE_NOTES.md", "COMMIT_SHA"}
    if {path.name for path in root.iterdir()} != names:
        raise BundleError("unexpected bundle inventory")
    before = {name: (root / name).lstat() for name in names}
    if any(not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0 for info in before.values()):
        raise BundleError("unsafe bundle entry")
    if before[wheel].st_size > 128 * 1024 * 1024 or before[sdist].st_size > 512 * 1024 * 1024:
        raise BundleError("oversize bundle asset")
    if _read_regular_bytes(root / "COMMIT_SHA", byte_limit=41) != (commit + "\n").encode("ascii"):
        raise BundleError("bundle commit mismatch")
    payload = _read_regular_bytes(root / "UPDATE_MANIFEST.json", byte_limit=MANIFEST_BYTE_LIMIT)
    manifest = json.loads(payload, object_pairs_hook=_strict_object, parse_constant=_reject_json_constant)
    expected_keys = {"application_data_generation", "automatic_update", "automatic_update_from", "channel", "platforms", "product",
                     "python", "runtime_requirements_sha256", "schema_version", "updater_protocol", "version", "wheel"}
    if type(manifest) is not dict or set(manifest) != expected_keys:
        raise BundleError("invalid bundle manifest")
    if (manifest["version"] != version or manifest["product"] != "arxiv-digest"
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or type(manifest["updater_protocol"]) is not int or manifest["updater_protocol"] != 1
            or type(manifest["application_data_generation"]) is not int or manifest["application_data_generation"] != 2
            or manifest["channel"] not in {"prerelease", "stable"}
            or manifest["platforms"] != ["darwin", "linux"]
            or type(manifest["automatic_update"]) is not bool
            or type(manifest["runtime_requirements_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", manifest["runtime_requirements_sha256"]) is None):
        raise BundleError("bundle manifest identity mismatch")
    assets = (_asset(root / wheel), _asset(root / sdist), _asset_from_payload("UPDATE_MANIFEST.json", payload))
    if (type(manifest["wheel"]) is not dict or set(manifest["wheel"]) != {"name", "sha256", "size"}
            or type(manifest["wheel"]["size"]) is not int
            or manifest["wheel"] != {"name": assets[0].name, "size": assets[0].size, "sha256": assets[0].sha256}):
        raise BundleError("bundle wheel identity mismatch")
    expected = b"".join(f"{asset.sha256}  {asset.name}\n".encode("ascii") for asset in assets)
    checksums = _read_regular_bytes(root / "SHA256SUMS", byte_limit=len(expected))
    if checksums != expected:
        raise BundleError("bundle checksum mismatch")
    notes = _read_regular_bytes(root / "RELEASE_NOTES.md", byte_limit=RELEASE_PAGE_BYTE_LIMIT)
    notes.decode("utf-8")
    if {path.name for path in root.iterdir()} != names or any(
            _file_identity(before[name]) != _file_identity((root / name).lstat()) for name in names):
        raise BundleError("bundle changed during verification")
    return VerifiedBundle(version, commit, manifest["channel"], (*assets, _asset_from_payload("SHA256SUMS", checksums)), hashlib.sha256(notes).hexdigest())


def main():
    try:
        bundle = verify_downloaded_bundle(Path(os.environ["BUNDLE"]), version=os.environ["VERSION"], commit=os.environ["EXPECTED_COMMIT"])
        if os.environ.get("VERIFY_REMOTE") == "1":
            payload = _read_regular_bytes(Path(os.environ["RELEASE_JSON"]), byte_limit=RELEASE_PAGE_BYTE_LIMIT)
            verify_github_release(payload, bundle=bundle, tag=os.environ["RELEASE_TAG"], remote_tag_commit=os.environ["REMOTE_TAG_COMMIT"])
        print(bundle.channel)
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("release verification failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
