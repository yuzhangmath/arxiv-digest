"""Application adapter for the copied runtime's sole snapshot implementation."""
from arxiv_digest.update_runtime.recovery import (
    SnapshotError, capture_core_installation_token, capture_exposed_link,
    capture_installation_token, create_snapshot, fsync_directory,
    scan_environment, verify_installation_token,
)


def validate_protected_provenance(*, paths, distribution, direct_url, package_or_url):
    """Prove an updater-installed source against its retained wheel and scanner."""
    import hashlib
    import json
    import os
    from pathlib import Path

    from arxiv_digest.update_manifest import (
        inspect_update_wheel_descriptor, parse_update_manifest,
        runtime_requirements_sha256,
    )
    from arxiv_digest.update_runtime import protocol, recovery

    try:
        store = protocol.ProtectedProvenanceStore(paths.update_recovery_dir)
        saved = store.read_snapshot()
        if saved is None:
            return False
        record = saved.record
        wheel = record["wheel"]
        wheel_path = Path(wheel["path"])
        if (record["version"] != distribution.version or str(wheel_path) != package_or_url
                or record["direct_url_sha256"] != hashlib.sha256(direct_url).hexdigest()
                or record["runtime_requirements_sha256"] != runtime_requirements_sha256(distribution.requirements)):
            return False
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate archive source field")
                value[key] = item
            return value
        direct = json.loads(direct_url, object_pairs_hook=pairs)
        if direct != {"url": wheel_path.as_uri(), "archive_info": {
            "hash": "sha256=" + wheel["sha256"], "hashes": {"sha256": wheel["sha256"]},
        }}:
            return False
        manifest_bytes, manifest_identity = recovery.read_owned_bytes(wheel_path.parent / "UPDATE_MANIFEST.json")
        if manifest_identity["mode"] != 0o600 or hashlib.sha256(manifest_bytes).hexdigest() != wheel["manifest_sha256"]:
            return False
        manifest = parse_update_manifest(manifest_bytes)
        if (manifest.version != distribution.version or manifest.wheel.name != wheel_path.name
                or manifest.wheel.size != wheel["size"] or manifest.wheel.sha256 != wheel["sha256"]
                or manifest.runtime_requirements_sha256 != record["runtime_requirements_sha256"]):
            return False
        parent_fd = recovery._open_directory(wheel_path.parent, private=True)
        try:
            descriptor = os.open(wheel_path.name, recovery._flags(), dir_fd=parent_fd)
            try:
                if recovery._file_identity(os.fstat(descriptor)) != wheel["identity"]:
                    return False
                inspection = inspect_update_wheel_descriptor(descriptor, filename=wheel_path.name, expected_version=distribution.version)
                if recovery._file_identity(os.fstat(descriptor)) != wheel["identity"] or inspection.wheel.sha256 != wheel["sha256"]:
                    return False
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
        old = record["core_token"]
        token = capture_core_installation_token(old["environment_path"], old["exposed_link"]["path"],
                    old["interpreter"]["path"], allowed_external_symlinks=recovery._external_links(old))
        # Snapshot consumption replaces only the environment root inode. Every
        # durable member, owner/mode/device, interpreter, metadata and link remain
        # exact; accepting that root replacement preserves restored eligibility.
        expected = {**old, "environment_identity": {**old["environment_identity"],
                    "inode": token["environment_identity"]["inode"]}}
        if token != expected:
            return False
        final = store.read_snapshot()
        return final is not None and final.sha256 == saved.sha256 and final.identity == saved.identity
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False

__all__ = [
    "SnapshotError", "capture_core_installation_token", "capture_exposed_link",
    "capture_installation_token", "create_snapshot", "fsync_directory",
    "scan_environment", "verify_installation_token",
    "validate_protected_provenance",
]


def installation_external_links(installation):
    """Allow only interpreter links proven to use the retained base interpreter."""
    import os
    from pathlib import Path
    names = {"python", "python3", f"python{installation.running_python[0]}.{installation.running_python[1]}"}
    result = {}
    for name in sorted(names):
        path = installation.venv / "bin" / name
        if path.is_symlink():
            target = os.readlink(path)
            if path.resolve(strict=True) != installation.base_interpreter:
                raise SnapshotError("interpreter link differs from retained interpreter")
            # Internal relative links need no escape allowance, but supplying
            # their exact bytes is harmless and avoids normalizing their target.
            result[f"bin/{name}"] = target
    return result
