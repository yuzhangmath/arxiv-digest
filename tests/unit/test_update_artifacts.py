from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone

import pytest

from arxiv_digest.update_artifacts import prune_update_artifacts, register_update_artifact
from arxiv_digest.update_runtime import protocol
from tests.update_protocol_factory import completed_journal, core_token, wheel
from tests.unit.test_backup import initialized_paths


def healthy(root):
    store, terminal = completed_journal(root)
    record = {key: terminal.record[key] for key in protocol.COMMON}
    record.update(version="0.3.1", attempt_id="a" * 64, launch_id="e" * 64,
                  wheel=wheel(root), core_token=core_token(root.parent),
                  direct_url_sha256="b" * 64, runtime_requirements_sha256="c" * 64)
    protocol.ProtectedProvenanceStore(root).compare_and_swap(None, record)
    return store, record


def test_only_two_valid_registered_updater_backups_are_kept(tmp_path):
    from arxiv_digest.backup import export_update_backup_under_lease
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    healthy(paths.update_recovery_dir)
    outputs = []
    for number in range(3):
        attempt = str(number) * 64
        item = export_update_backup_under_lease(paths, attempt, source_version="0.3.0",
            clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc))
        register_update_artifact(paths.update_recovery_dir, "backup", attempt, "0.3.0", item.path, created_at_ns=number)
        outputs.append(item.path)
    ordinary = paths.backup_dir / "ordinary-user-backup.zip"
    ordinary.write_bytes(b"user-owned")
    fake = outputs[0].parent / "update-backup-20000101T000000Z-".replace("-", "_", 1)
    fake.write_bytes(b"not registered")
    assert prune_update_artifacts(paths.update_recovery_dir) == outputs[:1]
    assert not outputs[0].exists()
    assert all(path.exists() for path in outputs[1:])
    assert ordinary.read_bytes() == b"user-owned" and fake.exists()
    assert prune_update_artifacts(paths.update_recovery_dir) == []


def test_invalid_newest_backup_does_not_evict_valid_older_backup(tmp_path):
    from arxiv_digest.backup import export_update_backup_under_lease
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    healthy(paths.update_recovery_dir)
    outputs = []
    for number in range(3):
        attempt = str(number) * 64
        item = export_update_backup_under_lease(paths, attempt, source_version="0.3.0")
        register_update_artifact(paths.update_recovery_dir, "backup", attempt, "0.3.0", item.path, created_at_ns=number)
        outputs.append(item.path)
    outputs[-1].write_bytes(b"corrupt")
    assert prune_update_artifacts(paths.update_recovery_dir) == []
    assert all(path.exists() for path in outputs)


def test_active_attempt_protects_all_registered_wheels_even_after_ack(tmp_path):
    from tests.update_protocol_factory import plan_record
    root = tmp_path / "private"
    store, _ = healthy(root)
    store.acknowledge("f" * 64)
    plan = protocol.ProtectedPlanStore(root).publish(plan_record(root, attempt_id="1" * 64))
    store.admit(plan)
    path = root / "attempts" / ("a" * 64) / "arxiv_digest-0.3.1-py3-none-any.whl"
    path.parent.mkdir(mode=0o700, parents=True)
    path.parent.parent.chmod(0o700)
    path.write_bytes(b"retained")
    path.chmod(0o600)
    register_update_artifact(root, "wheel", "a" * 64, "0.3.1", path)
    assert prune_update_artifacts(root) == [] and path.exists()


def test_receipt_ack_does_not_allow_pruning_a_blocking_journal(tmp_path):
    from tests.update_protocol_factory import admit_journal, next_record, proposal
    root = tmp_path / "private"
    store, current = admit_journal(root)
    auth = protocol.ExternalChangeAuthorization(current.sha256, current.record["attempt_id"], "inventory")
    receipt = {**proposal(outcome="external_change_detected"), "unacknowledged": True}
    store.transition(current, next_record(current, "external_change_detected", receipt=receipt), authorization=auth)
    store.acknowledge(receipt["receipt_id"])
    assert prune_update_artifacts(root) == []


def test_catalog_refuses_nonowned_paths_and_identity_replacement(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    foreign = root / "user.zip"
    foreign.write_bytes(b"user")
    foreign.chmod(0o600)
    with pytest.raises(protocol.StoreError, match="not updater-owned"):
        register_update_artifact(root, "backup", "a" * 64, "0.3.0", foreign)
    path = root / "attempts" / ("a" * 64) / "arxiv_digest-0.3.1-py3-none-any.whl"
    path.parent.mkdir(mode=0o700, parents=True)
    path.parent.parent.chmod(0o700)
    path.write_bytes(b"wheel")
    path.chmod(0o600)
    entry = register_update_artifact(root, "wheel", "a" * 64, "0.3.1", path)
    assert register_update_artifact(root, "wheel", "a" * 64, "0.3.1", path) == entry
    path.unlink()
    path.write_bytes(b"wheel")
    path.chmod(0o600)
    with pytest.raises(protocol.StoreError, match="identity changed"):
        register_update_artifact(root, "wheel", "a" * 64, "0.3.1", path)


@pytest.mark.parametrize("change", [lambda x: x.update(unknown=1), lambda x: x.update(schema_version=True),
    lambda x: x["artifacts"][0].update(created_at_ns=True), lambda x: x["artifacts"][0].update(kind="user-backup")])
def test_catalog_codec_is_closed_and_rejects_boolean_integers(tmp_path, change):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "attempts" / ("a" * 64) / "arxiv_digest-0.3.1-py3-none-any.whl"
    path.parent.mkdir(mode=0o700, parents=True)
    path.parent.parent.chmod(0o700)
    path.write_bytes(b"wheel")
    path.chmod(0o600)
    register_update_artifact(root, "wheel", "a" * 64, "0.3.1", path)
    record = copy.deepcopy(protocol.ArtifactCatalogStore(root).read_snapshot().record)
    change(record)
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_artifact_catalog(record)
