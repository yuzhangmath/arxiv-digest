#!/usr/bin/env python3
"""Generate a canonical update manifest from one already-built wheel."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path

from arxiv_digest.update_contract import (
    APPLICATION_DATA_GENERATION,
    MANIFEST_BYTE_LIMIT,
    PRODUCT,
    SCHEMA_VERSION,
    SUPPORTED_PLATFORMS,
    UPDATER_PROTOCOL,
    canonical_version,
)
from arxiv_digest.update_manifest import (
    AutomaticUpdateFrom,
    build_update_manifest,
    parse_update_manifest,
    serialize_update_manifest,
)


_POLICY_KEYS = frozenset(
    {
        "application_data_generation",
        "automatic_update",
        "automatic_update_from",
        "channel",
        "platforms",
        "product",
        "schema_version",
        "updater_protocol",
        "version",
    }
)
_SOURCE_KEYS = frozenset({"excluded", "maximum_exclusive", "minimum"})
POLICY_BYTE_LIMIT = MANIFEST_BYTE_LIMIT


def _policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("release policy is invalid")
        value[key] = member
    return value


def _reject_policy_constant(_value: str) -> object:
    raise ValueError("release policy is invalid")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_policy_file(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > POLICY_BYTE_LIMIT:
            raise ValueError("release policy is invalid")
        payload = source.read(POLICY_BYTE_LIMIT + 1)
        after = os.fstat(source.fileno())
    if (
        len(payload) != before.st_size
        or len(payload) > POLICY_BYTE_LIMIT
        or _file_identity(before) != _file_identity(after)
    ):
        raise ValueError("release policy is invalid")
    return payload


def _automatic_source(
    value: object,
    *,
    target_version: str,
) -> AutomaticUpdateFrom | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or value.keys() != _SOURCE_KEYS
        or type(value["minimum"]) is not str
        or type(value["maximum_exclusive"]) is not str
        or type(value["excluded"]) is not list
        or any(type(version) is not str for version in value["excluded"])
    ):
        raise ValueError("release policy is invalid")
    try:
        minimum = canonical_version(value["minimum"])
        maximum = canonical_version(value["maximum_exclusive"])
        target = canonical_version(target_version)
        exclusions = tuple(
            canonical_version(version) for version in value["excluded"]
        )
    except ValueError as error:
        raise ValueError("release policy is invalid") from error
    if (
        not minimum < maximum <= target
        or exclusions != tuple(sorted(set(exclusions)))
        or any(not minimum <= excluded < maximum for excluded in exclusions)
    ):
        raise ValueError("release policy is invalid")
    return AutomaticUpdateFrom(
        value["minimum"],
        value["maximum_exclusive"],
        tuple(value["excluded"]),
    )


def _reread_regular_file(path: Path, expected_size: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ValueError("written update manifest identity changed")
        payload = source.read(expected_size + 1)
        after = os.fstat(source.fileno())
    if (
        len(payload) != expected_size
        or _file_identity(before) != _file_identity(after)
    ):
        raise ValueError("written update manifest identity changed")
    return payload


def _write_exclusive_atomic(path: Path, payload: bytes) -> bytes:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        parse_update_manifest(_reread_regular_file(temporary, len(payload)))
        os.link(temporary, path, follow_symlinks=False)
        published = True
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _reread_regular_file(path, len(payload))
    finally:
        if not published:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="update_manifest.py",
        allow_abbrev=False,
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        policy_payload = _read_policy_file(arguments.policy)
        policy = json.loads(
            policy_payload.decode("utf-8"),
            object_pairs_hook=_policy_object,
            parse_constant=_reject_policy_constant,
        )
    except (OSError, RecursionError, TypeError, ValueError) as error:
        raise ValueError("release policy is invalid") from error
    if type(policy) is not dict or policy.keys() != _POLICY_KEYS:
        raise ValueError("release policy is invalid")
    if (
        type(policy["schema_version"]) is not int
        or policy["schema_version"] != SCHEMA_VERSION
        or type(policy["product"]) is not str
        or policy["product"] != PRODUCT
        or type(policy["updater_protocol"]) is not int
        or policy["updater_protocol"] != UPDATER_PROTOCOL
        or type(policy["application_data_generation"]) is not int
        or policy["application_data_generation"] != APPLICATION_DATA_GENERATION
        or type(policy["platforms"]) is not list
        or tuple(policy["platforms"]) != SUPPORTED_PLATFORMS
        or type(policy["version"]) is not str
        or type(policy["channel"]) is not str
        or policy["channel"] not in {"prerelease", "stable"}
        or type(policy["automatic_update"]) is not bool
        or policy["automatic_update"]
        != (policy["automatic_update_from"] is not None)
    ):
        raise ValueError("release policy is invalid")
    try:
        canonical_version(policy["version"])
    except ValueError as error:
        raise ValueError("release policy is invalid") from error
    manifest = build_update_manifest(
        arguments.wheel,
        version=policy["version"],
        channel=policy["channel"],
        automatic_update=policy["automatic_update"],
        automatic_update_from=_automatic_source(
            policy["automatic_update_from"],
            target_version=policy["version"],
        ),
    )
    payload = serialize_update_manifest(manifest)
    written = _write_exclusive_atomic(arguments.output, payload)
    if parse_update_manifest(written) != manifest:
        raise ValueError("written update manifest did not reread exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
