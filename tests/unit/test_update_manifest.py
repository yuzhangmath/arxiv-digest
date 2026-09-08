from __future__ import annotations

import hashlib
import json
import stat
import struct
import warnings
import zipfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("value,expected", [
    (">=3.11", ("3.11", None)),
    (">=3.11,<4.0", ("3.11", "4.0")),
    ("<4.0,>=3.11", ("3.11", "4.0")),
])
def test_installed_python_policy_uses_the_wheel_contract(value, expected) -> None:
    from arxiv_digest.update_manifest import python_policy_from_requires_python

    policy = python_policy_from_requires_python(value)
    assert (policy.minimum, policy.maximum_exclusive) == expected

from tests.update_wheel_factory import (
    default_metadata as _default_metadata,
    default_wheel_metadata as _default_wheel_metadata,
    record_digest as _record_digest,
    regular_zip_info as _regular_zip_info,
    write_valid_wheel as _write_valid_wheel,
)


def _set_zip_flag(path: Path, mask: int) -> None:
    payload = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = 0
        while True:
            position = payload.find(signature, position)
            if position < 0:
                break
            flags = struct.unpack_from("<H", payload, position + flag_offset)[0]
            struct.pack_into("<H", payload, position + flag_offset, flags | mask)
            position += 4
    path.write_bytes(payload)


@pytest.fixture
def valid_wheel(tmp_path: Path) -> Path:
    return _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl"
    )


def _valid_manifest_bytes(wheel: Path) -> bytes:
    from arxiv_digest.update_manifest import (
        build_update_manifest,
        serialize_update_manifest,
    )

    return serialize_update_manifest(
        build_update_manifest(
            wheel,
            version="0.3.0",
            channel="prerelease",
            automatic_update=False,
            automatic_update_from=None,
        )
    )


def test_manifest_round_trip_is_canonical_and_closed(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import (
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    encoded = serialize_update_manifest(manifest)

    assert encoded.endswith(b"\n")
    assert parse_update_manifest(encoded) == manifest
    assert serialize_update_manifest(parse_update_manifest(encoded)) == encoded


def test_manifest_size_is_rejected_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest
    from arxiv_digest.update_contract import MANIFEST_BYTE_LIMIT

    def unexpected_json_parse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("JSON parsing began before the manifest size check")

    monkeypatch.setattr(update_manifest.json, "loads", unexpected_json_parse)

    with pytest.raises(
        update_manifest.ManifestError,
        match="manifest exceeds byte limit",
    ):
        update_manifest.parse_update_manifest(b" " * (MANIFEST_BYTE_LIMIT + 1))


def test_manifest_serializer_enforces_the_byte_limit(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    manifest = update_manifest.build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    monkeypatch.setattr(update_manifest, "MANIFEST_BYTE_LIMIT", 1)

    with pytest.raises(
        update_manifest.ManifestError,
        match="manifest exceeds byte limit",
    ):
        update_manifest.serialize_update_manifest(manifest)


def test_manifest_parser_rejects_duplicate_object_keys(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    payload = _valid_manifest_bytes(valid_wheel).replace(
        b'"product":"arxiv-digest"',
        b'"product":"arxiv-digest","product":"arxiv-digest"',
    )

    with pytest.raises(ManifestError, match="manifest has duplicate keys"):
        parse_update_manifest(payload)


def test_manifest_parser_rejects_unknown_top_level_keys(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["unexpected"] = "value"
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()

    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize("container", ["python", "wheel"])
def test_manifest_parser_rejects_unknown_nested_keys(
    valid_wheel: Path,
    container: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value[container]["unexpected"] = "value"
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()

    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("container", "member"),
    [
        pytest.param(None, "product", id="top-level"),
        pytest.param("python", "minimum", id="python"),
        pytest.param("wheel", "name", id="wheel"),
    ],
)
def test_manifest_parser_rejects_missing_schema_keys(
    valid_wheel: Path,
    container: str | None,
    member: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    target = value if container is None else value[container]
    del target[member]
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()

    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_manifest_parser_closes_automatic_source_keys(
    valid_wheel: Path,
    mutation: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["automatic_update"] = True
    source = {
        "minimum": "0.2.0",
        "maximum_exclusive": "0.3.0",
        "excluded": [],
    }
    if mutation == "unknown":
        source["unexpected"] = "value"
    else:
        del source["minimum"]
    value["automatic_update_from"] = source
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()

    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


def test_manifest_parser_rejects_duplicate_nested_keys(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    payload = _valid_manifest_bytes(valid_wheel).replace(
        b'"minimum":"3.11"',
        b'"minimum":"3.11","minimum":"3.11"',
    )

    with pytest.raises(ManifestError, match="manifest has duplicate keys"):
        parse_update_manifest(payload)


def test_manifest_validation_rejects_bool_for_integer_on_both_paths(
    valid_wheel: Path,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(replace(manifest, schema_version=True))

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["schema_version"] = True
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        pytest.param("product", "other-product", id="product-value"),
        pytest.param("product", 7, id="product-type"),
        pytest.param("updater_protocol", 2, id="protocol-value"),
        pytest.param("updater_protocol", True, id="protocol-bool"),
        pytest.param("application_data_generation", 1, id="generation-value"),
        pytest.param("application_data_generation", False, id="generation-bool"),
        pytest.param("channel", "beta", id="channel-value"),
        pytest.param("channel", 7, id="channel-type"),
        pytest.param("automatic_update", 0, id="automatic-update-type"),
        pytest.param("version", "00.3.0", id="version-canonical"),
        pytest.param("version", 3, id="version-type"),
    ],
)
def test_manifest_scalar_contract_is_validated_on_both_paths(
    valid_wheel: Path,
    field: str,
    invalid: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(replace(manifest, **{field: invalid}))

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value[field] = invalid
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param((), id="empty"),
        pytest.param(("linux", "darwin"), id="unsorted"),
        pytest.param(("darwin", "darwin"), id="duplicate"),
        pytest.param(("windows",), id="unsupported"),
        pytest.param("darwin", id="not-tuple"),
        pytest.param(None, id="null"),
        pytest.param((7,), id="non-string"),
    ],
)
def test_manifest_platforms_are_closed_on_both_paths(
    valid_wheel: Path,
    invalid: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(
            replace(manifest, platforms=invalid)  # type: ignore[arg-type]
        )

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["platforms"] = list(invalid) if type(invalid) is tuple else invalid
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        pytest.param("03.11", None, id="minimum-leading-zero"),
        pytest.param("3.11.0", None, id="minimum-triplet"),
        pytest.param(3, None, id="minimum-type"),
        pytest.param("9" * 5_000 + ".1", None, id="minimum-int-limit"),
        pytest.param("3.11", "3.012", id="maximum-leading-zero"),
        pytest.param("3.11", "3.11", id="empty-range"),
        pytest.param("3.11", "3.10", id="inverted-range"),
        pytest.param("3.11", 4, id="maximum-type"),
    ],
)
def test_manifest_python_policy_is_closed_on_both_paths(
    valid_wheel: Path,
    minimum: object,
    maximum: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        PythonPolicy,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    invalid_policy = PythonPolicy(minimum, maximum)  # type: ignore[arg-type]
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(replace(manifest, python=invalid_policy))

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["python"] = {"minimum": minimum, "maximum_exclusive": maximum}
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="short"),
        pytest.param("g" * 64, id="non-hex"),
        pytest.param(7, id="non-string"),
    ],
)
def test_manifest_runtime_digest_is_closed_on_both_paths(
    valid_wheel: Path,
    invalid: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(
            replace(
                manifest,
                runtime_requirements_sha256=invalid,  # type: ignore[arg-type]
            )
        )

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["runtime_requirements_sha256"] = invalid
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        pytest.param("name", "other.whl", id="name-value"),
        pytest.param("name", 7, id="name-type"),
        pytest.param("size", 0, id="size-zero"),
        pytest.param("size", True, id="size-bool"),
        pytest.param("size", 128 * 1024 * 1024 + 1, id="size-limit"),
        pytest.param("sha256", "A" * 64, id="digest-uppercase"),
        pytest.param("sha256", "a" * 63, id="digest-short"),
        pytest.param("sha256", 7, id="digest-type"),
    ],
)
def test_manifest_wheel_identity_is_closed_on_both_paths(
    valid_wheel: Path,
    field: str,
    invalid: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        ManifestError,
        WheelIdentity,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    wheel_values = {
        "name": manifest.wheel.name,
        "size": manifest.wheel.size,
        "sha256": manifest.wheel.sha256,
    }
    wheel_values[field] = invalid
    invalid_wheel = WheelIdentity(**wheel_values)  # type: ignore[arg-type]
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(replace(manifest, wheel=invalid_wheel))

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["wheel"][field] = invalid
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("automatic_update", "has_source"),
    [
        pytest.param(False, True, id="manual-with-source"),
        pytest.param(True, False, id="automatic-without-source"),
    ],
)
def test_manifest_automatic_update_source_is_coupled_on_both_paths(
    valid_wheel: Path,
    automatic_update: bool,
    has_source: bool,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        AutomaticUpdateFrom,
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    source = AutomaticUpdateFrom("0.2.0", "0.3.0", ()) if has_source else None
    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    invalid_manifest = replace(
        manifest,
        automatic_update=automatic_update,
        automatic_update_from=source,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(invalid_manifest)

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["automatic_update"] = automatic_update
    value["automatic_update_from"] = (
        None
        if source is None
        else {
            "minimum": source.minimum,
            "maximum_exclusive": source.maximum_exclusive,
            "excluded": list(source.excluded),
        }
    )
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    ("minimum", "maximum", "excluded"),
    [
        pytest.param("00.2.0", "0.3.0", (), id="minimum-canonical"),
        pytest.param("0.2.0", "0.3", (), id="maximum-canonical"),
        pytest.param("0.2.0", 3, (), id="maximum-type"),
        pytest.param("0.3.0", "0.3.0", (), id="empty-range"),
        pytest.param("0.3.1", "0.3.0", (), id="inverted-range"),
        pytest.param("0.2.0", "0.4.0", (), id="past-target"),
        pytest.param(
            "0.2.0",
            "0.3.0",
            ("0.2.2", "0.2.1"),
            id="exclusions-unsorted",
        ),
        pytest.param(
            "0.2.0",
            "0.3.0",
            ("0.2.1", "0.2.1"),
            id="exclusions-duplicate",
        ),
        pytest.param("0.2.0", "0.3.0", ("0.1.9",), id="excluded-below"),
        pytest.param("0.2.0", "0.3.0", ("0.3.0",), id="excluded-at-maximum"),
        pytest.param("0.2.0", "0.3.0", ("00.2.1",), id="excluded-canonical"),
        pytest.param("0.2.0", "0.3.0", (7,), id="excluded-type"),
        pytest.param("0.2.0", "0.3.0", "0.2.1", id="excluded-not-tuple"),
        pytest.param("0.2.0", "0.3.0", None, id="excluded-null"),
    ],
)
def test_manifest_automatic_update_range_is_closed_on_both_paths(
    valid_wheel: Path,
    minimum: object,
    maximum: object,
    excluded: object,
) -> None:
    from dataclasses import replace

    from arxiv_digest.update_manifest import (
        AutomaticUpdateFrom,
        ManifestError,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    source = AutomaticUpdateFrom(minimum, maximum, excluded)  # type: ignore[arg-type]
    invalid_manifest = replace(
        manifest,
        automatic_update=True,
        automatic_update_from=source,
    )
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        serialize_update_manifest(invalid_manifest)

    value = json.loads(_valid_manifest_bytes(valid_wheel))
    value["automatic_update"] = True
    value["automatic_update_from"] = {
        "minimum": minimum,
        "maximum_exclusive": maximum,
        "excluded": list(excluded) if type(excluded) is tuple else excluded,
    }
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with pytest.raises(ManifestError, match="manifest schema is invalid"):
        parse_update_manifest(payload)


def test_manifest_exclusions_use_numeric_version_order(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import (
        AutomaticUpdateFrom,
        build_update_manifest,
        parse_update_manifest,
        serialize_update_manifest,
    )

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.12.0-py3-none-any.whl",
        version="0.12.0",
    )
    manifest = build_update_manifest(
        wheel,
        version="0.12.0",
        channel="stable",
        automatic_update=True,
        automatic_update_from=AutomaticUpdateFrom(
            "0.1.0",
            "0.12.0",
            ("0.2.0", "0.10.0"),
        ),
    )

    payload = serialize_update_manifest(manifest)
    assert parse_update_manifest(payload) == manifest


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda payload: payload.rstrip(b"\n"), id="missing-line-feed"),
        pytest.param(lambda payload: payload + b"\n", id="extra-line-feed"),
        pytest.param(
            lambda payload: payload.replace(b'"channel":', b'"channel": ', 1),
            id="insignificant-space",
        ),
    ],
)
def test_manifest_parser_requires_exact_canonical_bytes(
    valid_wheel: Path,
    mutate: object,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    payload = mutate(_valid_manifest_bytes(valid_wheel))  # type: ignore[operator]

    with pytest.raises(ManifestError, match="manifest encoding is not canonical"):
        parse_update_manifest(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"{", id="truncated"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"NaN", id="non-finite"),
    ],
)
def test_manifest_parser_normalizes_invalid_json(payload: bytes) -> None:
    from arxiv_digest.update_manifest import ManifestError, parse_update_manifest

    with pytest.raises(ManifestError, match="manifest JSON is invalid"):
        parse_update_manifest(payload)


def test_manifest_builder_reuses_the_authoritative_wheel_inspection(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import (
        build_update_manifest,
        inspect_update_wheel,
    )

    inspection = inspect_update_wheel(valid_wheel, expected_version="0.3.0")
    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )

    assert manifest.python == inspection.python
    assert (
        manifest.runtime_requirements_sha256
        == inspection.runtime_requirements_sha256
    )
    assert manifest.wheel == inspection.wheel


def test_runtime_requirement_digest_matches_frozen_schema_one_vectors() -> None:
    from arxiv_digest.update_manifest import runtime_requirements_sha256

    fixture_path = (
        Path(__file__).parents[1] / "fixtures/update/schema1-requirements.json"
    )
    fixture = json.loads(fixture_path.read_bytes())

    for vector in fixture["valid"]:
        expected = hashlib.sha256(vector["normalized"].encode("utf-8")).hexdigest()
        assert runtime_requirements_sha256(vector["values"]) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="blank"),
        pytest.param(" \t ", id="whitespace-only"),
        pytest.param("name\nInjected: value", id="line-break"),
        pytest.param("name\rInjected: value", id="carriage-return"),
        pytest.param("name\0", id="nul"),
        pytest.param("bad requirement ???", id="pep508"),
        pytest.param(7, id="non-string"),
    ],
)
def test_runtime_requirement_digest_rejects_invalid_values(value: object) -> None:
    from arxiv_digest.update_manifest import ManifestError
    from arxiv_digest.update_manifest import runtime_requirements_sha256

    with pytest.raises(ManifestError, match="runtime requirement is invalid"):
        runtime_requirements_sha256([value])  # type: ignore[list-item]


def test_valid_universal_wheel_has_an_exact_public_inspection(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import (
        PythonPolicy,
        WheelIdentity,
        WheelInspection,
        inspect_update_wheel,
        runtime_requirements_sha256,
    )

    payload = valid_wheel.read_bytes()

    assert inspect_update_wheel(valid_wheel, expected_version="0.3.0") == (
        WheelInspection(
            distribution="arxiv-digest",
            version="0.3.0",
            python=PythonPolicy("3.11", None),
            runtime_requirements_sha256=runtime_requirements_sha256(
                ["beautifulsoup4<5,>=4.12", "packaging<27,>=24"]
            ),
            wheel=WheelIdentity(
                name=valid_wheel.name,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )
    )


def test_public_manifest_records_are_frozen_and_slotted(valid_wheel: Path) -> None:
    from dataclasses import FrozenInstanceError

    from arxiv_digest.update_manifest import (
        AutomaticUpdateFrom,
        PythonPolicy,
        WheelIdentity,
        build_update_manifest,
        inspect_update_wheel,
    )

    inspection = inspect_update_wheel(valid_wheel)
    manifest = build_update_manifest(
        valid_wheel,
        version="0.3.0",
        channel="prerelease",
        automatic_update=False,
        automatic_update_from=None,
    )
    records = (
        PythonPolicy("3.11", None),
        WheelIdentity("wheel.whl", 1, "0" * 64),
        AutomaticUpdateFrom("0.2.0", "0.3.0", ()),
        inspection,
        manifest,
    )

    for record in records:
        assert not hasattr(record, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(record, next(iter(record.__slots__)), None)


def test_wheel_inspection_rejects_an_unexpected_version(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with pytest.raises(ManifestError, match="wheel version does not match"):
        inspect_update_wheel(valid_wheel, expected_version="0.3.1")


def test_wheel_inspection_rejects_a_noncanonical_filename(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(tmp_path / "renamed.whl")

    with pytest.raises(ManifestError, match="wheel filename is invalid"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_multiple_dist_info_trees(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with zipfile.ZipFile(valid_wheel, "a") as archive:
        archive.writestr(
            _regular_zip_info("other-1.0.0.dist-info/METADATA"),
            b"Name: other\n",
        )

    with pytest.raises(ManifestError, match="wheel dist-info tree is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_duplicate_member_names(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(valid_wheel, "a") as archive:
            archive.writestr(
                _regular_zip_info("arxiv_digest/__init__.py"),
                b"replacement\n",
            )

    with pytest.raises(ManifestError, match="wheel has duplicate members"):
        inspect_update_wheel(valid_wheel)


@pytest.mark.parametrize(
    "member_name",
    [
        pytest.param("../escape.py", id="parent"),
        pytest.param("/absolute.py", id="absolute"),
        pytest.param(r"dir\escape.py", id="backslash"),
        pytest.param("dir//escape.py", id="empty-component"),
        pytest.param("dir/./escape.py", id="dot-component"),
    ],
)
def test_wheel_inspection_rejects_unsafe_member_paths(
    valid_wheel: Path,
    member_name: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with zipfile.ZipFile(valid_wheel, "a") as archive:
        archive.writestr(_regular_zip_info(member_name), b"pass\n")

    with pytest.raises(ManifestError, match="wheel member path is unsafe"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_symlink_members(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    symlink = zipfile.ZipInfo("arxiv_digest/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(valid_wheel, "a") as archive:
        archive.writestr(symlink, b"target")

    with pytest.raises(ManifestError, match="wheel member type is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_directory_members(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    directory = zipfile.ZipInfo("extra/")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(valid_wheel, "a") as archive:
        archive.writestr(directory, b"")

    with pytest.raises(ManifestError, match="wheel member type is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_encrypted_members(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    _set_zip_flag(valid_wheel, 0x1)

    with pytest.raises(ManifestError, match="wheel member is encrypted"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_local_header_encryption(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    payload = bytearray(valid_wheel.read_bytes())
    position = 0
    while True:
        position = payload.find(b"PK\x03\x04", position)
        if position < 0:
            break
        flags = struct.unpack_from("<H", payload, position + 6)[0]
        struct.pack_into("<H", payload, position + 6, flags | 0x1)
        position += 4
    valid_wheel.write_bytes(payload)

    with pytest.raises(ManifestError, match="wheel member is encrypted"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_local_central_flag_disagreement(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    payload = bytearray(valid_wheel.read_bytes())
    local_header = payload.find(b"PK\x03\x04")
    assert local_header >= 0
    flags = struct.unpack_from("<H", payload, local_header + 6)[0]
    struct.pack_into("<H", payload, local_header + 6, flags | 0x8)
    valid_wheel.write_bytes(payload)

    with pytest.raises(ManifestError, match="wheel ZIP structure is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_local_central_compression_disagreement(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    payload = bytearray(valid_wheel.read_bytes())
    local_header = payload.find(b"PK\x03\x04")
    assert local_header >= 0
    struct.pack_into("<H", payload, local_header + 8, zipfile.ZIP_DEFLATED)
    valid_wheel.write_bytes(payload)

    with pytest.raises(ManifestError, match="wheel ZIP structure is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_unsupported_compression(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    payload = bytearray(valid_wheel.read_bytes())
    local_header = payload.find(b"PK\x03\x04")
    central_header = payload.find(b"PK\x01\x02")
    assert local_header >= 0 and central_header >= 0
    struct.pack_into("<H", payload, local_header + 8, 99)
    struct.pack_into("<H", payload, central_header + 10, 99)
    valid_wheel.write_bytes(payload)

    with pytest.raises(ManifestError, match="wheel compression is unsupported"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_a_symlink_path(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    link_directory = valid_wheel.parent / "link"
    link_directory.mkdir()
    link = link_directory / valid_wheel.name
    link.symlink_to(valid_wheel)

    with pytest.raises(ManifestError, match="wheel must be a regular file"):
        inspect_update_wheel(link)


def test_wheel_size_is_rejected_before_archive_parsing(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    monkeypatch.setattr(
        update_manifest,
        "WHEEL_BYTE_LIMIT",
        valid_wheel.stat().st_size - 1,
        raising=False,
    )

    def unexpected_archive_parse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("archive parsing began before the wheel size check")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_archive_parse)

    with pytest.raises(update_manifest.ManifestError, match="wheel exceeds byte limit"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_hashing_uses_only_bounded_reads(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    real_fdopen = update_manifest.os.fdopen
    read_sizes: list[int] = []

    class ObservedFile:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> "ObservedFile":
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)  # type: ignore[attr-defined]

        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 1024 * 1024
            read_sizes.append(size)
            return self._wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    def observed_fdopen(descriptor: int, mode: str, **options: object) -> ObservedFile:
        return ObservedFile(real_fdopen(descriptor, mode, **options))

    monkeypatch.setattr(update_manifest.os, "fdopen", observed_fdopen)

    assert update_manifest.inspect_update_wheel(valid_wheel).version == "0.3.0"
    assert read_sizes


def test_wheel_inspection_rejects_in_place_change_after_hashing(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    real_preflight = update_manifest._preflight_central_directory

    def mutate_after_preflight(wheel_file: object, wheel_size: int) -> None:
        real_preflight(wheel_file, wheel_size)
        with valid_wheel.open("r+b") as mutable:
            first_byte = mutable.read(1)
            mutable.seek(0)
            mutable.write(first_byte)

    monkeypatch.setattr(
        update_manifest,
        "_preflight_central_directory",
        mutate_after_preflight,
    )

    with pytest.raises(update_manifest.ManifestError, match="wheel changed"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_central_directory_limit_is_checked_before_zipfile(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    payload = valid_wheel.read_bytes()
    end_position = payload.rfind(b"PK\x05\x06")
    central_size = struct.unpack_from("<I", payload, end_position + 12)[0]
    monkeypatch.setattr(
        update_manifest,
        "CENTRAL_DIRECTORY_METADATA_BYTE_LIMIT",
        central_size - 1,
        raising=False,
    )

    def unexpected_zipfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed before central-directory preflight")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_zipfile)

    with pytest.raises(
        update_manifest.ManifestError,
        match="wheel central directory exceeds byte limit",
    ):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_zip64_before_zipfile(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    payload = bytearray(valid_wheel.read_bytes())
    end_position = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<H", payload, end_position + 10, 0xFFFF)
    valid_wheel.write_bytes(payload)

    def unexpected_zipfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed for a ZIP64 wheel")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_zipfile)

    with pytest.raises(update_manifest.ManifestError, match="ZIP64 is unsupported"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_member_zip64_before_zipfile(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    payload = bytearray(valid_wheel.read_bytes())
    central_header = payload.find(b"PK\x01\x02")
    assert central_header >= 0
    struct.pack_into("<I", payload, central_header + 24, 0xFFFFFFFF)
    valid_wheel.write_bytes(payload)

    def unexpected_zipfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed for a member-ZIP64 wheel")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_zipfile)

    with pytest.raises(update_manifest.ManifestError, match="ZIP64 is unsupported"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_multidisk_before_zipfile(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    payload = bytearray(valid_wheel.read_bytes())
    end_position = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<H", payload, end_position + 4, 1)
    valid_wheel.write_bytes(payload)

    def unexpected_zipfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed for a multidisk wheel")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_zipfile)

    with pytest.raises(update_manifest.ManifestError, match="multidisk is unsupported"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_inspection_preflights_central_directory_bounds(
    valid_wheel: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest import update_manifest

    payload = bytearray(valid_wheel.read_bytes())
    end_position = payload.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<I", payload, end_position + 16)[0]
    struct.pack_into("<I", payload, end_position + 16, central_offset + 1)
    valid_wheel.write_bytes(payload)

    def unexpected_zipfile(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed for malformed central bounds")

    monkeypatch.setattr(update_manifest.zipfile, "ZipFile", unexpected_zipfile)

    with pytest.raises(update_manifest.ManifestError, match="ZIP structure is invalid"):
        update_manifest.inspect_update_wheel(valid_wheel)


def test_wheel_inspection_normalizes_bad_zipfile_errors(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    payload = bytearray(valid_wheel.read_bytes())
    central_header = payload.find(b"PK\x01\x02")
    assert central_header >= 0
    payload[central_header : central_header + 4] = b"NOPE"
    valid_wheel.write_bytes(payload)

    with pytest.raises(ManifestError, match="wheel ZIP structure is invalid"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_excessive_member_compression(
    valid_wheel: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with zipfile.ZipFile(valid_wheel, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _regular_zip_info(
                "arxiv_digest/compressed.bin",
                compression=zipfile.ZIP_DEFLATED,
            ),
            b"0" * 200_000,
        )

    with pytest.raises(ManifestError, match="wheel member compression is excessive"):
        inspect_update_wheel(valid_wheel)


def test_wheel_inspection_rejects_oversized_metadata(tmp_path: Path) -> None:
    from arxiv_digest.update_contract import METADATA_MEMBER_BYTE_LIMIT
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_padding=METADATA_MEMBER_BYTE_LIMIT,
    )

    with pytest.raises(ManifestError, match="wheel metadata member is too large"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_a_nonuniversal_wheel_tag(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        wheel_payload=(
            b"Wheel-Version: 1.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: cp314-cp314-macosx_15_0_arm64\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel tag is unsupported"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_non_purelib_wheels(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        wheel_payload=(
            b"Wheel-Version: 1.0\n"
            b"Root-Is-Purelib: false\n"
            b"Tag: py3-none-any\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel is not purelib"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_an_unsupported_wheel_version(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        wheel_payload=(
            b"Wheel-Version: 2.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel version is unsupported"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_malformed_wheel_headers(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        wheel_payload=(
            b"not-a-header\n"
            b"Wheel-Version: 1.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel metadata is malformed"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_bounds_the_wheel_metadata_member(tmp_path: Path) -> None:
    from arxiv_digest.update_contract import METADATA_MEMBER_BYTE_LIMIT
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        wheel_payload=(
            b"Wheel-Version: 1.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
            + b"x" * METADATA_MEMBER_BYTE_LIMIT
        ),
    )

    with pytest.raises(ManifestError, match="wheel metadata member is too large"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_malformed_core_metadata(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"not-a-header\n"
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=3.11\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="core metadata is malformed"):
        inspect_update_wheel(wheel)


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    [
        pytest.param("Name", "arxiv-digest", id="name"),
        pytest.param("Version", "0.3.0", id="version"),
        pytest.param("Requires-Python", ">=3.11", id="requires-python"),
        pytest.param("Metadata-Version", "2.4", id="metadata-version"),
    ],
)
def test_wheel_inspection_rejects_ambiguous_core_singletons(
    tmp_path: Path,
    header_name: str,
    header_value: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=3.11\n"
            + f"{header_name}: {header_value}\n\n".encode()
        ),
    )

    with pytest.raises(ManifestError, match="core metadata is ambiguous"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_the_wrong_distribution(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: other-project\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=3.11\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel distribution does not match"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_a_mismatched_dist_info_name(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        dist_info_name="other-0.3.0.dist-info",
    )

    with pytest.raises(ManifestError, match="wheel dist-info name does not match"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_rejects_a_noncanonical_version(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-01.2.3-py3-none-any.whl",
        version="01.2.3",
    )

    with pytest.raises(ManifestError, match="wheel version is not canonical"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_maps_exact_python_bounds(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import PythonPolicy, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=3.11,<3.15\n\n"
        ),
    )

    assert inspect_update_wheel(wheel).python == PythonPolicy("3.11", "3.15")


def test_wheel_inspection_maps_reversed_exact_python_bounds(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import PythonPolicy, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: <3.15,>=3.11\n\n"
        ),
    )

    assert inspect_update_wheel(wheel).python == PythonPolicy("3.11", "3.15")


def test_wheel_inspection_rejects_inverted_python_bounds(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=3.15,<3.11\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel Python policy is unsupported"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_normalizes_python_integer_limits(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    huge_major = b"9" * 5_000
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        metadata_payload=(
            b"Metadata-Version: 2.4\n"
            b"Name: arxiv-digest\n"
            b"Version: 0.3.0\n"
            b"Requires-Python: >=" + huge_major + b".1\n\n"
        ),
    )

    with pytest.raises(ManifestError, match="wheel Python policy is unsupported"):
        inspect_update_wheel(wheel)


def test_wheel_record_covers_every_archive_member(valid_wheel: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    with zipfile.ZipFile(valid_wheel, "a") as archive:
        archive.writestr(_regular_zip_info("arxiv_digest/extra.py"), b"pass\n")

    with pytest.raises(ManifestError, match="wheel RECORD is incomplete"):
        inspect_update_wheel(valid_wheel)


def test_wheel_record_rejects_duplicate_paths(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        extra_record_rows=(("arxiv_digest/__init__.py", "", ""),),
    )

    with pytest.raises(ManifestError, match="wheel RECORD has duplicate paths"):
        inspect_update_wheel(wheel)


def test_wheel_record_verifies_bounded_member_hashes(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    metadata_name = "arxiv_digest-0.3.0.dist-info/METADATA"
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            metadata_name: (
                _record_digest(b"wrong"),
                str(len(_default_metadata("0.3.0"))),
            )
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD hash does not match"):
        inspect_update_wheel(wheel)


def test_wheel_record_verifies_bounded_member_sizes(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    metadata_name = "arxiv_digest-0.3.0.dist-info/METADATA"
    metadata_payload = _default_metadata("0.3.0")
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            metadata_name: (
                _record_digest(metadata_payload),
                str(len(metadata_payload) + 1),
            )
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD size does not match"):
        inspect_update_wheel(wheel)


def test_wheel_record_verifies_the_wheel_metadata_hash(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel_name = "arxiv_digest-0.3.0.dist-info/WHEEL"
    wheel_payload = _default_wheel_metadata()
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            wheel_name: (_record_digest(b"wrong"), str(len(wheel_payload)))
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD hash does not match"):
        inspect_update_wheel(wheel)


def test_wheel_record_verifies_the_wheel_metadata_size(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel_name = "arxiv_digest-0.3.0.dist-info/WHEEL"
    wheel_payload = _default_wheel_metadata()
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            wheel_name: (
                _record_digest(wheel_payload),
                str(len(wheel_payload) + 1),
            )
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD size does not match"):
        inspect_update_wheel(wheel)


def test_wheel_record_requires_an_empty_self_entry(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_self=(_record_digest(b"record"), "6"),
    )

    with pytest.raises(ManifestError, match="wheel RECORD self entry is invalid"):
        inspect_update_wheel(wheel)


def test_wheel_record_rejects_non_sha256_member_entries(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    package_name = "arxiv_digest/__init__.py"
    package_payload = b'__version__ = "0.3.0"\n'
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={package_name: ("md5=deadbeef", str(len(package_payload)))},
    )

    with pytest.raises(ManifestError, match="wheel RECORD entry is invalid"):
        inspect_update_wheel(wheel)


def test_wheel_record_rejects_noncanonical_sha256_base64(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    package_name = "arxiv_digest/__init__.py"
    package_payload = b'__version__ = "0.3.0"\n'
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            package_name: ("sha256=" + "A" * 42 + "B", str(len(package_payload)))
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD entry is invalid"):
        inspect_update_wheel(wheel)


def test_wheel_record_sizes_match_every_archive_member(tmp_path: Path) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    package_name = "arxiv_digest/__init__.py"
    package_payload = b'__version__ = "0.3.0"\n'
    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_overrides={
            package_name: (
                _record_digest(package_payload),
                str(len(package_payload) + 1),
            )
        },
    )

    with pytest.raises(ManifestError, match="wheel RECORD size does not match"):
        inspect_update_wheel(wheel)


def test_wheel_inspection_bounds_the_record_member(tmp_path: Path) -> None:
    from arxiv_digest.update_contract import METADATA_MEMBER_BYTE_LIMIT
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        record_padding=METADATA_MEMBER_BYTE_LIMIT,
    )

    with pytest.raises(ManifestError, match="wheel metadata member is too large"):
        inspect_update_wheel(wheel)


@pytest.mark.parametrize("member", ["METADATA", "WHEEL", "RECORD"])
def test_wheel_inspection_normalizes_a_missing_metadata_member(
    tmp_path: Path,
    member: str,
) -> None:
    from arxiv_digest.update_manifest import ManifestError, inspect_update_wheel

    wheel = _write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        omit_members=frozenset({f"arxiv_digest-0.3.0.dist-info/{member}"}),
    )

    with pytest.raises(ManifestError, match="wheel metadata members are incomplete"):
        inspect_update_wheel(wheel)
