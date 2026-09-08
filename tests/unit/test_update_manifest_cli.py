from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.update_wheel_factory import write_valid_wheel


@pytest.fixture
def synthetic_030_wheel(tmp_path: Path) -> Path:
    return write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        version="0.3.0",
    )


def test_release_policy_exactly_matches_the_immutable_bootstrap_policy() -> None:
    from arxiv_digest.update_contract import BOOTSTRAP_POLICY

    expected = dict(BOOTSTRAP_POLICY)
    expected["platforms"] = list(expected["platforms"])

    assert json.loads(Path("release/update-policy.json").read_bytes()) == expected


def test_cli_generates_and_rereads_bootstrap_manifest(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_manifest import parse_update_manifest
    from scripts.update_manifest import main

    output = tmp_path / "UPDATE_MANIFEST.json"
    assert main(
        [
            "--wheel",
            str(synthetic_030_wheel),
            "--policy",
            "release/update-policy.json",
            "--output",
            str(output),
        ]
    ) == 0
    manifest = parse_update_manifest(output.read_bytes())
    assert manifest.version == "0.3.0"
    assert manifest.automatic_update is False
    assert manifest.automatic_update_from is None


def test_cli_never_overwrites_an_existing_regular_file(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    output = tmp_path / "UPDATE_MANIFEST.json"
    output.write_bytes(b"existing manifest\n")

    with pytest.raises(FileExistsError):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                "release/update-policy.json",
                "--output",
                str(output),
            ]
        )

    assert output.read_bytes() == b"existing manifest\n"


def test_cli_never_follows_or_replaces_an_existing_symlink(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    victim = tmp_path / "victim.json"
    victim.write_bytes(b"private existing bytes\n")
    output = tmp_path / "UPDATE_MANIFEST.json"
    output.symlink_to(victim)

    with pytest.raises(FileExistsError):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                "release/update-policy.json",
                "--output",
                str(output),
            ]
        )

    assert output.is_symlink()
    assert victim.read_bytes() == b"private existing bytes\n"


def test_cli_rejects_unknown_release_policy_keys(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    policy_value = json.loads(Path("release/update-policy.json").read_bytes())
    policy_value["unexpected"] = "value"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_rejects_duplicate_release_policy_keys(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    policy_payload = Path("release/update-policy.json").read_bytes().replace(
        b'"product": "arxiv-digest"',
        b'"product": "arxiv-digest",\n  "product": "arxiv-digest"',
    )
    policy = tmp_path / "policy.json"
    policy.write_bytes(policy_payload)

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        pytest.param("product", "other-product", id="product"),
        pytest.param("schema_version", True, id="schema-bool"),
        pytest.param("updater_protocol", 2, id="protocol"),
        pytest.param("application_data_generation", 1, id="generation"),
        pytest.param("platforms", ["linux", "darwin"], id="platform-order"),
        pytest.param("platforms", "darwin", id="platform-type"),
    ],
)
def test_cli_rejects_policy_fixed_contract_mismatch(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    from scripts.update_manifest import main

    policy_value = json.loads(Path("release/update-policy.json").read_bytes())
    policy_value[field] = invalid
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


@pytest.mark.parametrize(
    "policy_payload",
    [
        pytest.param(b"{", id="truncated"),
        pytest.param(b"NaN", id="non-finite"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
def test_cli_normalizes_invalid_policy_json(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    policy_payload: bytes,
) -> None:
    from scripts.update_manifest import main

    policy = tmp_path / "policy.json"
    policy.write_bytes(policy_payload)

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_rejects_non_utf8_policy_encoding(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    value = json.loads(Path("release/update-policy.json").read_bytes())
    policy = tmp_path / "policy.json"
    policy.write_bytes(json.dumps(value).encode("utf-16"))

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_rejects_wrongly_typed_policy_values(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    policy_value = json.loads(Path("release/update-policy.json").read_bytes())
    policy_value["automatic_update"] = 0
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_rejects_malformed_automatic_update_source(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from scripts.update_manifest import main

    policy_value = json.loads(Path("release/update-policy.json").read_bytes())
    policy_value["automatic_update"] = True
    policy_value["automatic_update_from"] = "all versions"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_accepts_a_closed_automatic_update_source(
    synthetic_030_wheel: Path,
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_manifest import (
        AutomaticUpdateFrom,
        parse_update_manifest,
    )
    from scripts.update_manifest import main

    policy_value = json.loads(Path("release/update-policy.json").read_bytes())
    policy_value["automatic_update"] = True
    policy_value["automatic_update_from"] = {
        "excluded": ["0.2.0", "0.2.10"],
        "maximum_exclusive": "0.3.0",
        "minimum": "0.1.0",
    }
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_value), encoding="utf-8")
    output = tmp_path / "UPDATE_MANIFEST.json"

    assert (
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert parse_update_manifest(output.read_bytes()).automatic_update_from == (
        AutomaticUpdateFrom("0.1.0", "0.3.0", ("0.2.0", "0.2.10"))
    )


def test_cli_bounds_policy_bytes_before_json_decode(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import update_manifest

    policy = tmp_path / "policy.json"
    policy.write_bytes(b"{}" + b" " * 7)
    monkeypatch.setattr(update_manifest, "POLICY_BYTE_LIMIT", 8, raising=False)

    def reject_json_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversize policy reached JSON decoding")

    monkeypatch.setattr(update_manifest.json, "loads", reject_json_decode)

    with pytest.raises(ValueError, match="release policy is invalid"):
        update_manifest.main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_cli_requires_a_nonsymlink_regular_policy_file(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    kind: str,
) -> None:
    from scripts.update_manifest import main

    policy = tmp_path / "policy.json"
    if kind == "symlink":
        policy.symlink_to(Path("release/update-policy.json").resolve())
    else:
        policy.mkdir()

    with pytest.raises(ValueError, match="release policy is invalid"):
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )


def test_cli_reads_policy_through_a_stable_descriptor(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.update_manifest import main

    def reject_path_read(_path: Path) -> bytes:
        raise AssertionError("policy must not use a path-based read")

    monkeypatch.setattr(Path, "read_bytes", reject_path_read)

    assert (
        main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                "release/update-policy.json",
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )
        == 0
    )


def test_cli_opens_the_policy_without_blocking_on_a_replaced_special_file(
    synthetic_030_wheel: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from scripts import update_manifest

    policy = Path("release/update-policy.json")
    original_open = os.open
    observed = False

    def require_nonblocking_policy_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal observed
        if Path(path) == policy:
            observed = True
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(update_manifest.os, "open", require_nonblocking_policy_open)

    assert (
        update_manifest.main(
            [
                "--wheel",
                str(synthetic_030_wheel),
                "--policy",
                str(policy),
                "--output",
                str(tmp_path / "UPDATE_MANIFEST.json"),
            ]
        )
        == 0
    )
    assert observed is True


def test_exclusive_writer_unlinks_temporary_before_final_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import stat

    from arxiv_digest.update_manifest import (
        build_update_manifest,
        serialize_update_manifest,
    )
    from scripts import update_manifest

    wheel = write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        version="0.3.0",
    )
    payload = serialize_update_manifest(
        build_update_manifest(
            wheel,
            version="0.3.0",
            channel="prerelease",
            automatic_update=False,
            automatic_update_from=None,
        )
    )
    output = tmp_path / "UPDATE_MANIFEST.json"
    events: list[str] = []
    original_link = os.link
    original_fsync = os.fsync
    original_unlink = Path.unlink
    original_reread = update_manifest._reread_regular_file

    def trace_link(*args: object, **kwargs: object) -> None:
        events.append("link")
        original_link(*args, **kwargs)  # type: ignore[arg-type]

    def trace_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("directory-fsync")
        original_fsync(descriptor)

    def trace_unlink(path: Path, *args: object, **kwargs: object) -> None:
        events.append("unlink-temporary")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    def trace_reread(path: Path, expected_size: int) -> bytes:
        if path == output:
            events.append("reread-output")
        return original_reread(path, expected_size)

    monkeypatch.setattr(os, "link", trace_link)
    monkeypatch.setattr(os, "fsync", trace_fsync)
    monkeypatch.setattr(Path, "unlink", trace_unlink)
    monkeypatch.setattr(update_manifest, "_reread_regular_file", trace_reread)

    assert update_manifest._write_exclusive_atomic(output, payload) == payload
    assert events == [
        "link",
        "unlink-temporary",
        "directory-fsync",
        "reread-output",
    ]


def test_exclusive_writer_fails_closed_after_uncertain_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import stat

    from arxiv_digest.update_manifest import (
        build_update_manifest,
        serialize_update_manifest,
    )
    from scripts import update_manifest

    wheel = write_valid_wheel(
        tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl",
        version="0.3.0",
    )
    payload = serialize_update_manifest(
        build_update_manifest(
            wheel,
            version="0.3.0",
            channel="prerelease",
            automatic_update=False,
            automatic_update_from=None,
        )
    )
    output = tmp_path / "UPDATE_MANIFEST.json"
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated uncertain directory fsync")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="uncertain"):
        update_manifest._write_exclusive_atomic(output, payload)

    assert output.read_bytes() == payload
    assert list(tmp_path.glob(".UPDATE_MANIFEST.json.*.tmp")) == []


def test_written_manifest_reread_rejects_in_place_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from scripts import update_manifest

    path = tmp_path / "UPDATE_MANIFEST.json"
    payload = b"stable manifest bytes"
    path.write_bytes(payload)
    original_fdopen = os.fdopen

    class MutatingReader:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> "MutatingReader":
            self._wrapped.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return self._wrapped.fileno()  # type: ignore[attr-defined,no-any-return]

        def read(self, size: int = -1) -> bytes:
            value = self._wrapped.read(size)  # type: ignore[attr-defined]
            path.write_bytes(b"changed manifest byte")
            return value  # type: ignore[no-any-return]

    def mutating_fdopen(descriptor: int, mode: str) -> MutatingReader:
        return MutatingReader(original_fdopen(descriptor, mode))

    monkeypatch.setattr(os, "fdopen", mutating_fdopen)

    with pytest.raises(ValueError, match="identity changed"):
        update_manifest._reread_regular_file(path, len(payload))
