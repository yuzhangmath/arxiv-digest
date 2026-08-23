from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST = PROJECT_ROOT / "dist"
SDIST = DIST / "arxiv_digest-0.1.0.tar.gz"
WHEEL = DIST / "arxiv_digest-0.1.0-py3-none-any.whl"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return b"\0" in data


def _source_resources() -> dict[str, bytes]:
    package = PROJECT_ROOT / "src/arxiv_digest"
    paths = [
        *sorted((package / "storage/migrations").glob("*.sql")),
        *sorted(path for path in (package / "web/static").rglob("*") if path.is_file()),
    ]
    resources = {
        f"package/{path.relative_to(package).as_posix()}": path.read_bytes()
        for path in paths
    }
    resources["project/LICENSE"] = (PROJECT_ROOT / "LICENSE").read_bytes()
    resources["share/THIRD_PARTY_NOTICES.md"] = (
        PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"
    ).read_bytes()
    return resources


def _sdist_resources() -> dict[str, bytes]:
    assert SDIST.is_file(), f"release sdist is missing: {SDIST.name}; run python -m build"
    result: dict[str, bytes] = {}
    with tarfile.open(SDIST, "r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            assert len(parts) >= 2, f"sdist member has no root prefix: {member.name}"
            relative = PurePosixPath(*parts[1:]).as_posix()
            key: str | None = None
            if relative.startswith(
                "src/arxiv_digest/storage/migrations/"
            ) and relative.endswith(".sql"):
                key = "package/" + relative.removeprefix("src/arxiv_digest/")
            elif relative.startswith("src/arxiv_digest/web/static/"):
                key = "package/" + relative.removeprefix("src/arxiv_digest/")
            elif relative == "LICENSE":
                key = "project/LICENSE"
            elif relative == "THIRD_PARTY_NOTICES.md":
                key = "share/THIRD_PARTY_NOTICES.md"
            if key is not None:
                extracted = archive.extractfile(member)
                assert extracted is not None
                result[key] = extracted.read()
    return result


def _wheel_resources() -> tuple[dict[str, bytes], list[str]]:
    assert WHEEL.is_file(), f"release wheel is missing: {WHEEL.name}; run python -m build"
    result: dict[str, bytes] = {}
    unexpected_binary: list[str] = []
    expected_source = _source_resources()
    with zipfile.ZipFile(WHEEL) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            data = archive.read(info)
            key: str | None = None
            if name.startswith(
                "arxiv_digest/storage/migrations/"
            ) and name.endswith(".sql"):
                key = "package/" + name.removeprefix("arxiv_digest/")
            elif name.startswith("arxiv_digest/web/static/"):
                key = "package/" + name.removeprefix("arxiv_digest/")
            elif name.endswith(".dist-info/licenses/LICENSE"):
                key = "project/LICENSE"
            elif name.endswith(
                ".data/data/share/arxiv-digest/THIRD_PARTY_NOTICES.md"
            ):
                key = "share/THIRD_PARTY_NOTICES.md"
            if key is not None:
                result[key] = data
            if name.startswith("arxiv_digest/"):
                normalized = "package/" + name.removeprefix("arxiv_digest/")
                if normalized not in expected_source and _is_binary(data):
                    unexpected_binary.append(name)
    return result, unexpected_binary


def _assert_katex_manifest(resources: dict[str, bytes]) -> None:
    prefix = "package/web/static/vendor/katex/"
    manifest_key = prefix + "katex-manifest.json"
    manifest = json.loads(resources[manifest_key].decode("utf-8"))
    entries = manifest["files"]
    assert [entry["path"] for entry in entries] == sorted(
        entry["path"] for entry in entries
    )
    declared = {entry["path"]: entry["sha256"] for entry in entries}
    actual = {
        key.removeprefix(prefix): _sha256(data)
        for key, data in resources.items()
        if key.startswith(prefix) and key != manifest_key
    }
    assert actual == declared

    binary_resources = {
        key.removeprefix(prefix)
        for key, data in resources.items()
        if key.startswith("package/") and _is_binary(data)
    }
    assert binary_resources == {
        path for path in declared if path.startswith("fonts/")
    }


def test_sdist_and_wheel_contain_exact_release_resources() -> None:
    source = _source_resources()
    sdist = _sdist_resources()
    wheel, unexpected_binary = _wheel_resources()

    assert set(sdist) == set(source)
    assert set(wheel) == set(source)
    assert {key: _sha256(value) for key, value in sdist.items()} == {
        key: _sha256(value) for key, value in source.items()
    }
    assert {key: _sha256(value) for key, value in wheel.items()} == {
        key: _sha256(value) for key, value in source.items()
    }
    assert unexpected_binary == []
    _assert_katex_manifest(source)
    _assert_katex_manifest(sdist)
    _assert_katex_manifest(wheel)


def test_migration_and_application_asset_inventories_are_exact() -> None:
    source = _source_resources()
    sdist = _sdist_resources()
    wheel, _ = _wheel_resources()

    for prefix in ("package/storage/migrations/", "package/web/static/"):
        expected = {key for key in source if key.startswith(prefix)}
        assert {key for key in sdist if key.startswith(prefix)} == expected
        assert {key for key in wheel if key.startswith(prefix)} == expected
