from __future__ import annotations

import os
from pathlib import Path

import pytest

from arxiv_digest.update_snapshot import (
    SnapshotError, capture_core_installation_token, create_snapshot,
    scan_environment, verify_installation_token,
)


@pytest.fixture
def layout(tmp_path: Path):
    env = tmp_path / "pipx/venvs/arxiv-digest"
    env.mkdir(parents=True, mode=0o700)
    (env / "bin").mkdir(mode=0o755)
    (env / "empty").mkdir(mode=0o750)
    (env / "bin/arxiv-digest").write_bytes(b"old application\n")
    (env / "bin/arxiv-digest").chmod(0o755)
    (env / "pipx_metadata.json").write_bytes(b'{"main_package":{"package_version":"0.3.0"}}')
    (env / "alias").symlink_to("bin/arxiv-digest")
    interpreter = tmp_path / "base-python"
    interpreter.write_bytes(b"synthetic interpreter")
    interpreter.chmod(0o700)
    exposed = tmp_path / "exposed/arxiv-digest"
    exposed.parent.mkdir(mode=0o700)
    exposed.symlink_to(env / "bin/arxiv-digest")
    snapshots = tmp_path / "pipx/arxiv-digest-update-snapshots"
    snapshots.mkdir(mode=0o700)
    return env, exposed, interpreter, snapshots / ("a" * 64)


def test_inventory_preserves_empty_directories_modes_and_raw_links(layout):
    env, _, _, _ = layout
    (env / "__pycache__").mkdir()
    (env / "__pycache__/cache.pyc").write_bytes(b"ignored")
    (env / "module.pyc").write_bytes(b"ignored")
    actual = scan_environment(env)
    entries = {item["path"]: item for item in actual["entries"]}
    assert entries["empty"] == {"kind": "directory", "path": "empty", "mode": 0o750}
    assert entries["alias"] == {"kind": "symlink", "path": "alias", "mode": 0o755, "target": "bin/arxiv-digest"} or entries["alias"] == {"kind": "symlink", "path": "alias", "mode": 0o777, "target": "bin/arxiv-digest"}
    assert entries["bin/arxiv-digest"]["size"] == 16
    assert not any("pyc" in path for path in entries)


@pytest.mark.parametrize("hostile", ["hardlink", "writable", "escape", "fifo", "case", "cache_link"])
def test_inventory_rejects_unsafe_tree_without_following_links(layout, hostile):
    env, _, _, _ = layout
    if hostile == "hardlink":
        os.link(env / "bin/arxiv-digest", env / "second")
    elif hostile == "writable":
        (env / "bin/arxiv-digest").chmod(0o666)
    elif hostile == "escape":
        (env / "outside").symlink_to("../../outside")
    elif hostile == "fifo":
        os.mkfifo(env / "fifo")
    elif hostile == "case":
        if (env / "EMPTY").exists():
            pytest.skip("native filesystem folds case before the scanner")
        (env / "EMPTY").write_bytes(b"collision")
    else:
        (env / "__pycache__").symlink_to("../../outside")
    with pytest.raises(SnapshotError):
        scan_environment(env)


def test_snapshot_rereads_complete_copy_and_detects_later_change(layout):
    env, exposed, interpreter, snapshot = layout
    token = create_snapshot(env, snapshot, exposed, interpreter)
    assert scan_environment(snapshot) == token["core"]["inventory"]
    verify_installation_token(token)
    (env / "empty/new-file").write_bytes(b"external mutation")
    with pytest.raises(SnapshotError, match="changed"):
        verify_installation_token(token)


def test_snapshot_refuses_conflict_and_nested_destination(layout):
    env, exposed, interpreter, snapshot = layout
    snapshot.mkdir()
    with pytest.raises(SnapshotError):
        create_snapshot(env, snapshot, exposed, interpreter)
    assert snapshot.is_dir()
    with pytest.raises(SnapshotError):
        create_snapshot(env, env / "nested", exposed, interpreter)


def test_external_symlink_requires_exact_explicit_authorization(layout):
    env, exposed, interpreter, _ = layout
    (env / "bin/python").symlink_to(interpreter)
    with pytest.raises(SnapshotError):
        capture_core_installation_token(env, exposed, interpreter)
    token = capture_core_installation_token(
        env, exposed, interpreter,
        allowed_external_symlinks={"bin/python": str(interpreter)},
    )
    assert token["interpreter"]["path"] == str(interpreter)


def test_retained_provenance_verifies_manifest_wheel_and_complete_installation(layout, tmp_path):
    import hashlib
    import json
    from email.parser import BytesParser
    from types import SimpleNamespace

    from arxiv_digest.update_manifest import build_update_manifest, serialize_update_manifest
    from arxiv_digest.update_runtime import protocol, recovery
    from arxiv_digest.update_snapshot import validate_protected_provenance
    from tests.update_wheel_factory import default_metadata, write_valid_wheel

    env, exposed, interpreter, _ = layout
    root = tmp_path / "recovery"
    attempt = root / ("a" * 64)
    attempt.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    wheel_path = write_valid_wheel(attempt / "arxiv_digest-0.3.0-py3-none-any.whl")
    wheel_path.chmod(0o600)
    manifest = build_update_manifest(wheel_path, version="0.3.0", channel="stable", automatic_update=False, automatic_update_from=None)
    manifest_bytes = serialize_update_manifest(manifest)
    (attempt / "UPDATE_MANIFEST.json").write_bytes(manifest_bytes)
    (attempt / "UPDATE_MANIFEST.json").chmod(0o600)
    direct = json.dumps({"url": wheel_path.as_uri(), "archive_info": {"hash": "sha256=" + manifest.wheel.sha256, "hashes": {"sha256": manifest.wheel.sha256}}}).encode()
    (env / "direct_url.json").write_bytes(direct)
    core = capture_core_installation_token(env, exposed, interpreter)
    wheel = {"path": str(wheel_path), "size": manifest.wheel.size, "sha256": manifest.wheel.sha256,
             "identity": recovery._file_identity(wheel_path.lstat()), "version": "0.3.0",
             "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
             "source_url": "https://github.com/yuzhangmath/arxiv-digest/releases/download/v0.3.0/arxiv_digest-0.3.0-py3-none-any.whl"}
    record = {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1,
              "application_data_generation": 2, "version": "0.3.0", "attempt_id": "a" * 64,
              "launch_id": "b" * 64, "wheel": wheel, "core_token": core,
              "direct_url_sha256": hashlib.sha256(direct).hexdigest(),
              "runtime_requirements_sha256": manifest.runtime_requirements_sha256}
    protocol.ProtectedProvenanceStore(root).compare_and_swap(None, record)
    metadata = BytesParser().parsebytes(default_metadata("0.3.0"))
    kwargs = {"paths": SimpleNamespace(update_recovery_dir=root),
              "distribution": SimpleNamespace(version="0.3.0", requirements=metadata.get_all("Requires-Dist")),
              "direct_url": direct, "package_or_url": str(wheel_path)}
    assert validate_protected_provenance(**kwargs)
    (env / "empty/external").write_bytes(b"new external file")
    assert not validate_protected_provenance(**kwargs)
    (env / "empty/external").unlink()
    (attempt / "UPDATE_MANIFEST.json").write_bytes(b"tampered")
    assert not validate_protected_provenance(**kwargs)
