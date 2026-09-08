#!/usr/bin/env python3
"""Verify exact local and published release bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arxiv_digest.update_contract import (
    MANIFEST_BYTE_LIMIT,
    RELEASE_PAGE_BYTE_LIMIT,
    REPOSITORY,
    canonical_version,
)
from arxiv_digest.update_manifest import (
    ManifestError,
    inspect_update_wheel,
    parse_update_manifest,
)


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


def verify_local_bundle(
    bundle: Path,
    *,
    version: str,
    commit: str,
) -> VerifiedBundle:
    try:
        canonical_version(version)
    except ValueError as error:
        raise BundleError("release version is invalid") from error
    if type(commit) is not str or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BundleError("release commit is invalid")

    root = Path(bundle)
    wheel_name = f"arxiv_digest-{version}-py3-none-any.whl"
    sdist_name = f"arxiv_digest-{version}.tar.gz"
    expected_names = {
        wheel_name,
        sdist_name,
        "UPDATE_MANIFEST.json",
        "SHA256SUMS",
        "RELEASE_NOTES.md",
        "COMMIT_SHA",
    }
    try:
        actual_names = {path.name for path in root.iterdir()}
    except OSError as error:
        raise BundleError("local release inventory is invalid") from error
    if actual_names != expected_names:
        raise BundleError("local release inventory is invalid")
    paths = {name: root / name for name in expected_names}
    try:
        file_stats = {name: path.lstat() for name, path in paths.items()}
    except OSError as error:
        raise BundleError("local release inventory is invalid") from error
    if any(not stat.S_ISREG(value.st_mode) for value in file_stats.values()):
        raise BundleError("local release asset is not a regular file")
    positive_names = {
        wheel_name,
        sdist_name,
        "UPDATE_MANIFEST.json",
        "RELEASE_NOTES.md",
    }
    if any(file_stats[name].st_size <= 0 for name in positive_names):
        raise BundleError("local release asset is empty")
    if _read_regular_bytes(paths["COMMIT_SHA"], byte_limit=41) != (
        f"{commit}\n".encode("ascii")
    ):
        raise BundleError("release commit does not match")

    wheel = paths[wheel_name]
    try:
        manifest_payload = _read_regular_bytes(
            paths["UPDATE_MANIFEST.json"],
            byte_limit=MANIFEST_BYTE_LIMIT,
        )
        manifest = parse_update_manifest(manifest_payload)
        inspection = inspect_update_wheel(wheel, expected_version=version)
    except BundleError:
        raise
    except (ManifestError, OSError) as error:
        raise BundleError("release manifest or wheel is invalid") from error
    if (
        manifest.version != version
        or manifest.wheel != inspection.wheel
        or manifest.python != inspection.python
        or manifest.runtime_requirements_sha256
        != inspection.runtime_requirements_sha256
    ):
        raise BundleError("release manifest does not match the wheel")

    checksum_assets = (
        BundleAsset(
            inspection.wheel.name,
            inspection.wheel.size,
            inspection.wheel.sha256,
        ),
        _asset(paths[sdist_name]),
        _asset_from_payload("UPDATE_MANIFEST.json", manifest_payload),
    )
    checksum_bytes = b"".join(
        f"{asset.sha256}  {asset.name}\n".encode("ascii")
        for asset in checksum_assets
    )
    checksum_payload = _read_regular_bytes(
        paths["SHA256SUMS"],
        byte_limit=len(checksum_bytes),
    )
    if checksum_payload != checksum_bytes:
        raise BundleError("release checksums are invalid")

    ordered_assets = (
        *checksum_assets,
        _asset_from_payload("SHA256SUMS", checksum_payload),
    )
    release_notes = _read_regular_bytes(
        paths["RELEASE_NOTES.md"],
        byte_limit=RELEASE_PAGE_BYTE_LIMIT,
    )
    try:
        release_notes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BundleError("release notes are not valid UTF-8") from error
    try:
        final_names = {path.name for path in root.iterdir()}
        final_stats = {name: path.lstat() for name, path in paths.items()}
    except OSError as error:
        raise BundleError("local release inventory changed") from error
    if final_names != expected_names or any(
        _file_identity(file_stats[name]) != _file_identity(final_stats[name])
        for name in expected_names
    ):
        raise BundleError("local release inventory changed")
    return VerifiedBundle(
        version=version,
        commit=commit,
        channel=manifest.channel,
        assets=ordered_assets,
        release_notes_sha256=hashlib.sha256(release_notes).hexdigest(),
    )


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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the closed local release-bundle verifier."""

    parser = argparse.ArgumentParser(
        prog="release_bundle.py",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local", allow_abbrev=False)
    local.add_argument("--bundle", type=Path, required=True)
    local.add_argument("--version", required=True)
    local.add_argument("--commit", required=True)
    arguments = parser.parse_args(argv)

    if arguments.command != "local":
        raise AssertionError("argparse admitted an unknown command")
    try:
        verify_local_bundle(
            arguments.bundle,
            version=arguments.version,
            commit=arguments.commit,
        )
    except BundleError:
        print("release bundle verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
