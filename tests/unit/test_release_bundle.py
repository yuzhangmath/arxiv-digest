from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.update_wheel_factory import write_valid_wheel


VERSION = "0.3.0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_checksums(root: Path) -> None:
    checksum_assets = (
        root / f"arxiv_digest-{VERSION}-py3-none-any.whl",
        root / f"arxiv_digest-{VERSION}.tar.gz",
        root / "UPDATE_MANIFEST.json",
    )
    (root / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in checksum_assets),
        encoding="ascii",
    )


def _write_valid_bundle(root: Path) -> Path:
    from arxiv_digest.update_manifest import (
        build_update_manifest,
        serialize_update_manifest,
    )

    root.mkdir()
    wheel = write_valid_wheel(
        root / f"arxiv_digest-{VERSION}-py3-none-any.whl",
        version=VERSION,
    )
    sdist = root / f"arxiv_digest-{VERSION}.tar.gz"
    sdist.write_bytes(b"deterministic source distribution\n")
    manifest = root / "UPDATE_MANIFEST.json"
    manifest.write_bytes(
        serialize_update_manifest(
            build_update_manifest(
                wheel,
                version=VERSION,
                channel="prerelease",
                automatic_update=False,
                automatic_update_from=None,
            )
        )
    )
    _rewrite_checksums(root)
    (root / "RELEASE_NOTES.md").write_bytes(b"# arXiv Digest 0.3.0\n")
    (root / "COMMIT_SHA").write_text(f"{COMMIT}\n", encoding="ascii")
    return root


def _github_release_payload(bundle: object) -> bytes:
    assets = [
        {
            "name": asset.name,
            "state": "uploaded",
            "size": asset.size,
            "digest": f"sha256:{asset.sha256}",
            "browser_download_url": (
                "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
                f"v{bundle.version}/{asset.name}"
            ),
        }
        for asset in bundle.assets
    ]
    return json.dumps(
        {
            "tag_name": f"v{bundle.version}",
            "draft": False,
            "prerelease": bundle.channel == "prerelease",
            "body": "# arXiv Digest 0.3.0\n",
            "assets": assets,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@pytest.fixture
def valid_bundle(tmp_path: Path) -> Path:
    return _write_valid_bundle(tmp_path / "bundle")


def test_local_bundle_verification_returns_the_exact_public_record(
    valid_bundle: Path,
) -> None:
    from scripts.release_bundle import BundleAsset, VerifiedBundle, verify_local_bundle

    wheel = valid_bundle / f"arxiv_digest-{VERSION}-py3-none-any.whl"
    sdist = valid_bundle / f"arxiv_digest-{VERSION}.tar.gz"
    manifest = valid_bundle / "UPDATE_MANIFEST.json"
    checksums = valid_bundle / "SHA256SUMS"
    notes = valid_bundle / "RELEASE_NOTES.md"

    assert verify_local_bundle(
        valid_bundle,
        version=VERSION,
        commit=COMMIT,
    ) == VerifiedBundle(
        version=VERSION,
        commit=COMMIT,
        channel="prerelease",
        assets=tuple(
            BundleAsset(path.name, path.stat().st_size, _sha256(path))
            for path in (wheel, sdist, manifest, checksums)
        ),
        release_notes_sha256=_sha256(notes),
    )


def test_local_bundle_rejects_an_empty_source_distribution(
    valid_bundle: Path,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    (valid_bundle / f"arxiv_digest-{VERSION}.tar.gz").write_bytes(b"")
    _rewrite_checksums(valid_bundle)

    with pytest.raises(BundleError):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


def test_local_bundle_hashes_assets_without_unbounded_path_reads(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release_bundle import verify_local_bundle

    def reject_read_bytes(_path: Path) -> bytes:
        raise AssertionError("release assets must be read through stable descriptors")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    verified = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)

    assert verified.version == VERSION


def test_local_bundle_opens_assets_without_blocking_on_replaced_special_files(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from scripts import release_bundle

    original_open = os.open
    observed: set[str] = set()

    def require_nonblocking_asset_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        candidate = Path(path)
        if candidate.parent == valid_bundle:
            observed.add(candidate.name)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(release_bundle.os, "open", require_nonblocking_asset_open)

    verified = release_bundle.verify_local_bundle(
        valid_bundle,
        version=VERSION,
        commit=COMMIT,
    )

    assert verified.version == VERSION
    assert observed == {path.name for path in valid_bundle.iterdir()}


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_local_bundle_requires_the_exact_six_file_inventory(
    valid_bundle: Path,
    mutation: str,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    if mutation == "extra":
        (valid_bundle / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    else:
        (valid_bundle / "COMMIT_SHA").unlink()

    with pytest.raises(BundleError, match="inventory"):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_local_bundle_rejects_nonregular_assets(
    valid_bundle: Path,
    replacement: str,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    notes = valid_bundle / "RELEASE_NOTES.md"
    notes.unlink()
    if replacement == "symlink":
        notes.symlink_to(valid_bundle / "COMMIT_SHA")
    else:
        notes.mkdir()

    with pytest.raises(BundleError, match="regular file"):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


def test_local_bundle_normalizes_invalid_wheel_errors(valid_bundle: Path) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    wheel = valid_bundle / f"arxiv_digest-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"not a wheel")
    _rewrite_checksums(valid_bundle)

    with pytest.raises(BundleError):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize(
    "mutation",
    ["star", "path", "crlf", "extra", "missing-lf", "order", "uppercase"],
)
def test_local_bundle_rejects_noncanonical_checksum_bytes(
    valid_bundle: Path,
    mutation: str,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    checksums = valid_bundle / "SHA256SUMS"
    payload = checksums.read_bytes()
    lines = payload.splitlines(keepends=True)
    if mutation == "star":
        payload = payload.replace(b"  ", b" *", 1)
    elif mutation == "path":
        payload = payload.replace(b"  arxiv_digest-", b"  dist/arxiv_digest-", 1)
    elif mutation == "crlf":
        payload = payload.replace(b"\n", b"\r\n")
    elif mutation == "extra":
        payload += lines[0]
    elif mutation == "missing-lf":
        payload = payload[:-1]
    elif mutation == "order":
        payload = b"".join((lines[1], lines[0], lines[2]))
    else:
        payload = payload[:64].upper() + payload[64:]
    checksums.write_bytes(payload)

    with pytest.raises(BundleError):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize(
    "field",
    ["version", "wheel-size", "wheel-digest", "python", "requirements"],
)
def test_local_bundle_compares_every_manifest_identity_to_the_real_wheel(
    valid_bundle: Path,
    field: str,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    manifest_path = valid_bundle / "UPDATE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_bytes())
    if field == "version":
        manifest["version"] = "0.3.1"
        manifest["wheel"]["name"] = "arxiv_digest-0.3.1-py3-none-any.whl"
    elif field == "wheel-size":
        manifest["wheel"]["size"] += 1
    elif field == "wheel-digest":
        manifest["wheel"]["sha256"] = "0" * 64
    elif field == "python":
        manifest["python"]["minimum"] = "3.10"
    else:
        manifest["runtime_requirements_sha256"] = "0" * 64
    manifest_path.write_bytes(
        (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )
    _rewrite_checksums(valid_bundle)

    with pytest.raises(BundleError, match="does not match"):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize(
    ("version", "commit"),
    [
        pytest.param("03.0.0", COMMIT, id="noncanonical-version"),
        pytest.param(VERSION, COMMIT.upper(), id="uppercase-commit"),
        pytest.param(VERSION, COMMIT[:-1], id="short-commit"),
    ],
)
def test_local_bundle_rejects_noncanonical_inputs(
    valid_bundle: Path,
    version: str,
    commit: str,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    with pytest.raises(BundleError):
        verify_local_bundle(valid_bundle, version=version, commit=commit)


def test_local_bundle_requires_the_exact_commit_file_bytes(valid_bundle: Path) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    (valid_bundle / "COMMIT_SHA").write_text(COMMIT, encoding="ascii")

    with pytest.raises(BundleError, match="commit"):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize("payload", [b"", b"\xff"])
def test_local_bundle_requires_positive_utf8_release_notes(
    valid_bundle: Path,
    payload: bytes,
) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    (valid_bundle / "RELEASE_NOTES.md").write_bytes(payload)

    with pytest.raises(BundleError):
        verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)


def test_local_bundle_normalizes_missing_directory_errors(tmp_path: Path) -> None:
    from scripts.release_bundle import BundleError, verify_local_bundle

    with pytest.raises(BundleError):
        verify_local_bundle(
            tmp_path / "missing",
            version=VERSION,
            commit=COMMIT,
        )


def test_local_bundle_reuses_validated_payloads_for_published_asset_digests(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import release_bundle

    original = release_bundle._asset
    calls: list[str] = []

    def trace(path: Path) -> object:
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(release_bundle, "_asset", trace)

    release_bundle.verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)

    assert calls == [f"arxiv_digest-{VERSION}.tar.gz"]


def test_local_bundle_rejects_checksum_substitution_after_validation(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import release_bundle

    original = release_bundle._read_regular_bytes
    substituted = False

    def substitute_after_read(path: Path, *, byte_limit: int) -> bytes:
        nonlocal substituted
        payload = original(path, byte_limit=byte_limit)
        if path.name == "SHA256SUMS" and not substituted:
            substituted = True
            path.write_bytes(b"substituted checksums\n")
        return payload

    monkeypatch.setattr(
        release_bundle,
        "_read_regular_bytes",
        substitute_after_read,
    )

    with pytest.raises(release_bundle.BundleError):
        release_bundle.verify_local_bundle(
            valid_bundle,
            version=VERSION,
            commit=COMMIT,
        )


@pytest.mark.parametrize("mutation", ["manifest", "late-extra-entry"])
def test_local_bundle_rechecks_inventory_after_cross_phase_mutation(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from scripts import release_bundle

    original = release_bundle._read_regular_bytes
    mutated = False

    def mutate_after_read(path: Path, *, byte_limit: int) -> bytes:
        nonlocal mutated
        payload = original(path, byte_limit=byte_limit)
        if mutation == "manifest" and path.name == "UPDATE_MANIFEST.json" and not mutated:
            changed = json.loads(payload)
            changed["channel"] = "stable"
            path.write_bytes(
                (
                    json.dumps(changed, separators=(",", ":"), sort_keys=True)
                    + "\n"
                ).encode()
            )
            _rewrite_checksums(valid_bundle)
            mutated = True
        elif (
            mutation == "late-extra-entry"
            and path.name == "RELEASE_NOTES.md"
            and not mutated
        ):
            (valid_bundle / "late-entry.txt").write_text("late", encoding="utf-8")
            mutated = True
        return payload

    monkeypatch.setattr(release_bundle, "_read_regular_bytes", mutate_after_read)

    with pytest.raises(release_bundle.BundleError):
        release_bundle.verify_local_bundle(
            valid_bundle,
            version=VERSION,
            commit=COMMIT,
        )


def test_github_release_verification_accepts_the_exact_published_record(
    valid_bundle: Path,
) -> None:
    from scripts.release_bundle import verify_github_release, verify_local_bundle

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)

    assert (
        verify_github_release(
            _github_release_payload(bundle),
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )
        is None
    )


def test_github_release_bounds_payload_before_json_decode(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import release_bundle

    bundle = release_bundle.verify_local_bundle(
        valid_bundle,
        version=VERSION,
        commit=COMMIT,
    )
    monkeypatch.setattr(release_bundle, "RELEASE_PAGE_BYTE_LIMIT", 8)

    def reject_json_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversize payload reached JSON decoding")

    monkeypatch.setattr(release_bundle.json, "loads", reject_json_decode)

    with pytest.raises(release_bundle.BundleError, match="byte limit"):
        release_bundle.verify_github_release(
            b"{}" + b" " * 7,
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


def test_github_release_normalizes_recursive_json_failure(
    valid_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import release_bundle

    bundle = release_bundle.verify_local_bundle(
        valid_bundle,
        version=VERSION,
        commit=COMMIT,
    )

    def fail_recursively(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("adversarial nesting")

    monkeypatch.setattr(release_bundle.json, "loads", fail_recursively)

    with pytest.raises(release_bundle.BundleError):
        release_bundle.verify_github_release(
            b"{}",
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{", id="truncated"),
        pytest.param(b"NaN", id="nonfinite"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"[]", id="wrong-top-type"),
        pytest.param(
            b'{"tag_name":"v0.3.0","tag_name":"v0.3.0"}',
            id="duplicate-key",
        ),
        pytest.param(bytearray(b"{}"), id="non-bytes"),
    ],
)
def test_github_release_normalizes_invalid_json(
    valid_bundle: Path,
    payload: object,
) -> None:
    from scripts.release_bundle import (
        BundleError,
        verify_github_release,
        verify_local_bundle,
    )

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)

    with pytest.raises(BundleError):
        verify_github_release(
            payload,  # type: ignore[arg-type]
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


def test_github_release_derives_stable_status_only_from_the_bundle_channel(
    valid_bundle: Path,
) -> None:
    from scripts.release_bundle import verify_github_release, verify_local_bundle

    prerelease_bundle = verify_local_bundle(
        valid_bundle,
        version=VERSION,
        commit=COMMIT,
    )
    stable_bundle = replace(prerelease_bundle, channel="stable")
    payload = json.loads(_github_release_payload(stable_bundle))
    assert payload["prerelease"] is False

    assert (
        verify_github_release(
            json.dumps(payload).encode(),
            bundle=stable_bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )
        is None
    )


def test_github_release_rejects_an_invalid_bundle_channel(valid_bundle: Path) -> None:
    from scripts.release_bundle import (
        BundleError,
        verify_github_release,
        verify_local_bundle,
    )

    verified = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    invalid = replace(verified, channel="beta")

    with pytest.raises(BundleError):
        verify_github_release(
            _github_release_payload(invalid),
            bundle=invalid,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "caller-tag",
        "remote-commit",
        "tag-name",
        "draft-true",
        "draft-zero",
        "prerelease-false",
        "prerelease-one",
        "body-changed",
        "body-non-string",
    ],
)
def test_github_release_requires_exact_release_identity(
    valid_bundle: Path,
    mutation: str,
) -> None:
    from scripts.release_bundle import (
        BundleError,
        verify_github_release,
        verify_local_bundle,
    )

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    release = json.loads(_github_release_payload(bundle))
    tag = f"v{VERSION}"
    remote_commit = COMMIT
    if mutation == "caller-tag":
        tag += "?mutable=true"
    elif mutation == "remote-commit":
        remote_commit = "f" * 40
    elif mutation == "tag-name":
        release["tag_name"] = f"v{VERSION}-other"
    elif mutation == "draft-true":
        release["draft"] = True
    elif mutation == "draft-zero":
        release["draft"] = 0
    elif mutation == "prerelease-false":
        release["prerelease"] = False
    elif mutation == "prerelease-one":
        release["prerelease"] = 1
    elif mutation == "body-changed":
        release["body"] += "changed"
    else:
        release["body"] = ["notes"]

    with pytest.raises(BundleError):
        verify_github_release(
            json.dumps(release).encode(),
            bundle=bundle,
            tag=tag,
            remote_tag_commit=remote_commit,
        )


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "renamed"])
def test_github_release_requires_the_exact_four_asset_inventory(
    valid_bundle: Path,
    mutation: str,
) -> None:
    from scripts.release_bundle import (
        BundleError,
        verify_github_release,
        verify_local_bundle,
    )

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    release = json.loads(_github_release_payload(bundle))
    if mutation == "missing":
        release["assets"].pop()
    elif mutation == "extra":
        extra = dict(release["assets"][0])
        extra["name"] = "unexpected.txt"
        release["assets"].append(extra)
    elif mutation == "duplicate":
        release["assets"][-1] = dict(release["assets"][0])
    else:
        release["assets"][0]["name"] = "renamed.whl"

    with pytest.raises(BundleError, match="asset"):
        verify_github_release(
            json.dumps(release).encode(),
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "state",
        "size-bool",
        "size-zero",
        "size-changed",
        "digest-bare",
        "digest-uppercase",
        "url-query",
        "url-host",
        "record-type",
        "name-type",
    ],
)
def test_github_release_requires_exact_asset_records(
    valid_bundle: Path,
    mutation: str,
) -> None:
    from scripts.release_bundle import (
        BundleError,
        verify_github_release,
        verify_local_bundle,
    )

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    release = json.loads(_github_release_payload(bundle))
    asset = release["assets"][0]
    if mutation == "state":
        asset["state"] = "new"
    elif mutation == "size-bool":
        asset["size"] = True
    elif mutation == "size-zero":
        asset["size"] = 0
    elif mutation == "size-changed":
        asset["size"] += 1
    elif mutation == "digest-bare":
        asset["digest"] = asset["digest"].removeprefix("sha256:")
    elif mutation == "digest-uppercase":
        asset["digest"] = asset["digest"].upper()
    elif mutation == "url-query":
        asset["browser_download_url"] += "?download=1"
    elif mutation == "url-host":
        asset["browser_download_url"] = asset["browser_download_url"].replace(
            "github.com",
            "example.com",
        )
    elif mutation == "record-type":
        release["assets"][0] = [asset]
    else:
        asset["name"] = True

    with pytest.raises(BundleError, match="asset"):
        verify_github_release(
            json.dumps(release).encode(),
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )


def test_github_release_ignores_unrelated_github_fields(valid_bundle: Path) -> None:
    from scripts.release_bundle import verify_github_release, verify_local_bundle

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    release = json.loads(_github_release_payload(bundle))
    release["immutable"] = True
    release["assets"][0]["content_type"] = "application/octet-stream"

    assert (
        verify_github_release(
            json.dumps(release).encode(),
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )
        is None
    )


def test_canonical_github_release_fixture_matches_the_bundle(
    valid_bundle: Path,
) -> None:
    from scripts.release_bundle import verify_github_release, verify_local_bundle

    bundle = verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)

    assert (
        verify_github_release(
            Path("tests/fixtures/update/github-release.json").read_bytes(),
            bundle=bundle,
            tag=f"v{VERSION}",
            remote_tag_commit=COMMIT,
        )
        is None
    )


def test_local_cli_is_quiet_after_exact_bundle_verification(
    valid_bundle: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.release_bundle import main

    assert (
        main(
            [
                "local",
                "--bundle",
                str(valid_bundle),
                "--version",
                VERSION,
                "--commit",
                COMMIT,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_local_cli_returns_a_bounded_generic_validation_error(
    valid_bundle: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.release_bundle import main

    secret = "PRIVATE-BUNDLE-CONTENT"
    (valid_bundle / "SHA256SUMS").write_text(secret, encoding="utf-8")

    assert (
        main(
            [
                "local",
                "--bundle",
                str(valid_bundle),
                "--version",
                VERSION,
                "--commit",
                COMMIT,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "release bundle verification failed\n"
    assert secret not in captured.err
    assert str(valid_bundle) not in captured.err


def test_local_cli_rejects_abbreviated_options(valid_bundle: Path) -> None:
    from scripts.release_bundle import main

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "local",
                "--bund",
                str(valid_bundle),
                "--version",
                VERSION,
                "--commit",
                COMMIT,
            ]
        )

    assert raised.value.code == 2
