from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_protected_store_rejects_symlinked_ancestor_before_creating_outside_root(tmp_path):
    from arxiv_digest.update_runtime import protocol
    from tests.update_protocol_factory import plan_record
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "recovery").mkdir(mode=0o700)
    link = tmp_path / "alias"
    link.symlink_to(outside, target_is_directory=True)
    root = link / "recovery"
    with pytest.raises((OSError, protocol.StoreError)):
        protocol.ProtectedPlanStore(root).publish(plan_record(root))
    assert list((outside / "recovery").iterdir()) == []


def test_protected_store_verification_detects_replaced_ancestor(tmp_path):
    from arxiv_digest.update_runtime import protocol
    parent = tmp_path / "parent"
    root = parent / "recovery"
    root.mkdir(mode=0o700, parents=True)
    protected = protocol._ProtectedDirectory(root)
    try:
        parent.rename(tmp_path / "retained")
        parent.symlink_to(tmp_path / "retained", target_is_directory=True)
        with pytest.raises(protocol.StoreError):
            protected.verify()
    finally:
        protected.close()

from arxiv_digest.update_runtime import protocol
from tests.update_protocol_factory import (
    admit_journal, completed_journal, next_record, plan_record, proposal, publish_plan,
)


def test_closed_success_graph_cas_and_unexposed_proposal(tmp_path):
    root = tmp_path / "private"
    store, current = admit_journal(root)
    original = current
    for state in ("committed", "installing", "target_installed", "launching_target"):
        assert store.receipt("0.3.0") is None
        target = next_record(current, state)
        auth = protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], target["installer"]) if state == "installing" else None
        current = store.transition(current, target, authorization=auth)
    with pytest.raises(protocol.StaleSnapshotError):
        store.transition(original, next_record(original, "committed"))
    current = store.transition(current, next_record(current, "healthy_pending_commit", proposal=proposal()))
    assert store.receipt("0.3.1") is None
    with pytest.raises(protocol.StoreError):
        store.transition(current, next_record(current, "complete"))
    current = store.transition(current, next_record(current, "complete", receipt={**proposal(), "unacknowledged": True}))
    assert set(store.receipt("0.3.1")) == {"receipt_id", "outcome", "installed_version", "attempted_version", "message_code"}
    with pytest.raises(protocol.StoreError):
        store.receipt("0.3.0")
    assert store.acknowledge("0" * 64) is False
    assert store.acknowledge("f" * 64) is True
    assert store.acknowledge("f" * 64) is False
    assert store.read_snapshot().record["state"] == "complete"
    assert store.receipt("0.3.1") is None


@pytest.mark.parametrize("source,target", [
    ("prepared", "installing"), ("prepared", "complete"), ("prepared", "recovery_failed"),
    ("committed", "canceling_no_install"), ("committed", "aborted_no_mutation"),
    ("committed", "recovery_failed"), ("prepared", "external_change_detected"),
])
def test_unlisted_or_untyped_edges_preserve_exact_record(tmp_path, source, target):
    store, current = admit_journal(tmp_path / "private")
    if source == "committed":
        current = store.transition(current, next_record(current, "committed"))
    with pytest.raises(protocol.StoreError):
        store.transition(current, next_record(current, target))
    assert store.read_snapshot() == current


def test_admission_rereads_fixed_plan_identity_and_bytes(tmp_path):
    root = tmp_path / "private"
    snapshot = publish_plan(root)
    forged = protocol.JournalSnapshot(snapshot.record, snapshot.canonical_bytes, "0" * 64, snapshot.identity)
    with pytest.raises(protocol.StaleSnapshotError):
        protocol.JournalStore(root).admit(forged)
    path = root / protocol.PLAN_FILENAME
    payload = path.read_bytes()
    path.unlink()
    path.write_bytes(payload)
    path.chmod(0o600)
    with pytest.raises(protocol.StaleSnapshotError):
        protocol.JournalStore(root).admit(snapshot)
    assert not (root / protocol.JOURNAL_FILENAME).exists()


def test_pending_receipt_prevents_plan_and_attempt_replacement(tmp_path):
    root = tmp_path / "private"
    store, terminal = completed_journal(root)
    old_plan = protocol.ProtectedPlanStore(root).read_snapshot()
    with pytest.raises(protocol.PendingReceiptError):
        protocol.ProtectedPlanStore(root).publish(plan_record(root, attempt_id="1" * 64))
    with pytest.raises(protocol.PendingReceiptError):
        store.admit(old_plan)
    assert store.read_snapshot() == terminal
    assert protocol.ProtectedPlanStore(root).read_snapshot() == old_plan


def test_acknowledgement_and_admission_serialize(tmp_path):
    root = tmp_path / "private"
    store, _ = completed_journal(root)
    old_plan = protocol.ProtectedPlanStore(root).read_snapshot()
    def admit():
        try:
            return store.admit(old_plan).record["state"]
        except protocol.PendingReceiptError:
            return "pending"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(admit)
        ack = executor.submit(store.acknowledge, "f" * 64)
        result, acknowledged = first.result(), ack.result()
    assert acknowledged is True
    assert result in {"pending", "prepared"}
    final = store.read_snapshot().record
    assert "receipt" not in final
    assert final["state"] == ("complete" if result == "pending" else "prepared")


def test_acknowledged_blocking_terminal_remains_blocking(tmp_path):
    root = tmp_path / "private"
    store, current = admit_journal(root)
    receipt = {**proposal(outcome="external_change_detected"), "unacknowledged": True}
    auth = protocol.ExternalChangeAuthorization(current.sha256, current.record["attempt_id"], "inventory")
    current = store.transition(current, next_record(current, "external_change_detected", receipt=receipt), authorization=auth)
    assert store.acknowledge(receipt["receipt_id"])
    assert protocol.classify_journal(root) == "block"
    with pytest.raises(protocol.StoreError):
        store.admit(protocol.ProtectedPlanStore(root).read_snapshot())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "mode", "oversized", "empty"])
def test_unsafe_record_refused_without_opening_special_file(tmp_path, kind):
    root = tmp_path / "private"
    store, _ = admit_journal(root)
    path = root / protocol.JOURNAL_FILENAME
    if kind == "mode": path.chmod(0o644)
    elif kind == "oversized":
        with path.open("r+b") as handle: handle.truncate(protocol.RECORD_BYTE_LIMIT + 1)
    elif kind == "empty": path.write_bytes(b"")
    else:
        path.unlink()
        if kind == "symlink": path.symlink_to(root / "missing")
        elif kind == "hardlink":
            other = root / "other"
            other.write_bytes(b"{}\n")
            other.chmod(0o600)
            os.link(other, path)
        elif kind == "directory": path.mkdir()
        else: os.mkfifo(path, 0o600)
    with pytest.raises((protocol.ProtocolError, OSError)):
        store.read_snapshot()
    assert protocol.classify_journal(root) == "block"


def test_fault_before_publication_preserves_old_journal(tmp_path, monkeypatch):
    root = tmp_path / "private"
    store, current = admit_journal(root)
    original = os.replace
    def fail(*args, **kwargs):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.transition(current, next_record(current, "committed"))
    monkeypatch.setattr(os, "replace", original)
    assert store.read_snapshot() == current
    assert sorted(path.name for path in root.iterdir()) == sorted([protocol.JOURNAL_LOCK_FILENAME, protocol.JOURNAL_FILENAME, protocol.PLAN_FILENAME])


def test_fault_after_publication_is_readable_but_never_reported_committed(tmp_path, monkeypatch):
    root = tmp_path / "private"
    store, current = admit_journal(root)
    original = os.fsync
    count = 0
    def fail(fd):
        nonlocal count
        count += 1
        if count == 2: raise OSError("synthetic directory fsync failure")
        return original(fd)
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        store.transition(current, next_record(current, "committed"))
    monkeypatch.setattr(os, "fsync", original)
    assert store.read_snapshot().record["state"] == "committed"
    with pytest.raises(protocol.StaleSnapshotError):
        store.transition(current, next_record(current, "committed"))


def test_snapshot_objects_cannot_mutate_their_canonical_authority(tmp_path):
    store, current = admit_journal(tmp_path / "private")
    current.record["state"] = "committed"
    with pytest.raises(protocol.StaleSnapshotError):
        store.transition(current, next_record(current, "installing"))
    assert store.read_snapshot().record["state"] == "prepared"
