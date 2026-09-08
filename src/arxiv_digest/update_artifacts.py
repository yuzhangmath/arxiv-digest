"""Reference-aware retention of explicitly registered updater artifacts."""
from __future__ import annotations

import time
from pathlib import Path

from arxiv_digest import backup
from arxiv_digest.update_runtime.protocol import _artifact_read_file as _read_file
from arxiv_digest.update_runtime import protocol


def _inspect(root, entry):
    protocol._artifact_inspect(root, entry)
    if entry["kind"] == "backup":
        inspected = backup.inspect_backup(Path(entry["path"]))
        if inspected.archive_sha256 != entry["sha256"] or inspected.manifest.application_version != entry["version"]:
            raise protocol.StoreError("registered updater backup changed")


def register_update_artifact(root, kind, attempt_id, version, path, *, created_at_ns=None):
    """Register only a stable private artifact at its exact updater-owned name."""
    root = Path(root)
    current = _read_file(Path(path))
    if current is None:
        raise protocol.StoreError("updater artifact is absent")
    entry = {**current, "path": str(path), "kind": kind, "attempt_id": attempt_id,
             "version": version, "created_at_ns": time.time_ns() if created_at_ns is None else created_at_ns}
    record = {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1,
              "application_data_generation": 2, "artifacts": [entry]}
    protocol.validate_artifact_catalog(record)
    _inspect(root, entry)
    with protocol._journal_lock(root) as parent:
        prior = parent.read(protocol.CATALOG_FILENAME, protocol.decode_artifact_catalog)
        entries = [] if prior is None else prior.record["artifacts"]
        existing = next((item for item in entries if item["path"] == entry["path"]), None)
        if existing is not None:
            if {key: val for key, val in existing.items() if key != "created_at_ns"} != {key: val for key, val in entry.items() if key != "created_at_ns"}:
                raise protocol.StoreError("registered updater artifact identity changed")
            return existing
        record["artifacts"] = sorted([*entries, entry], key=lambda item: (item["created_at_ns"], item["path"]))
        parent.publish(protocol.CATALOG_FILENAME, protocol.encode_artifact_catalog(record), protocol.decode_artifact_catalog, prior)
    return entry


def prune_update_artifacts(root):
    """Application adapter to the copied runtime's sole retention engine."""
    return protocol.prune_update_artifacts(root)
