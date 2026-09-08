from __future__ import annotations

import os
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from arxiv_digest.update_runtime import helper, protocol, recovery
from tests.update_protocol_factory import next_record, proposal
from tests.update_runtime_factory import prepared_runtime


@pytest.fixture
def completed(tmp_path):
    plan, store, current, locks = prepared_runtime(tmp_path / "recovery")
    current = finish_restored(plan, store, current)
    try:
        yield plan, store, current, locks
    finally:
        for lock in locks.values():
            lock.close()


def finish_restored(plan, store, current):
    current = store.transition(current, next_record(current, "committed"))
    value = plan.record
    core = value["old_token"]["core"]
    replay = {"snapshot_path": value["paths"]["snapshot"], "live_path": value["paths"]["environment"],
        "forensic_path": value["paths"]["forensic"], "old_token": core,
        "partial_token": recovery.capture_partial_environment(value["paths"]["environment"]),
        "partial_exposed": core["exposed_link"], "partial_provenance": None, "prior_provenance": None,
        "target_started": False, "process_group_ids": [], "processes_dead": True}
    authorization = protocol.RecoveryAuthorization(current.sha256, value["attempt_id"], replay)
    current = store.transition(current, next_record(current, "rolling_back", subphase="package_restore_pending", replay=replay), authorization=authorization)
    receipt = proposal(outcome="restored", attempt_id=value["attempt_id"])
    current = store.transition(current, next_record(current, "healthy_pending_commit", proposal=receipt))
    current = store.transition(current, next_record(current, "complete", receipt={**receipt, "unacknowledged": True}))
    return current


def clean(completed):
    plan, store, current, locks = completed
    return recovery.cleanup_terminal_snapshot(plan_snapshot=plan, journal_store=store,
        journal_snapshot=current, transition_lock=locks["transition"], launcher_lock=locks["launcher"])


def test_terminal_cleanup_removes_only_snapshot_and_is_idempotent(completed):
    plan, store, current, _ = completed
    paths = plan.record["paths"]
    live_before = recovery.scan_environment(paths["environment"])
    forensic = Path(paths["forensic"])
    forensic.mkdir(mode=0o700)
    (forensic / "preserve").write_bytes(b"failed target evidence")
    assert clean(completed) == "cleaned"
    assert not Path(paths["snapshot"]).exists()
    assert clean(completed) == "absent"
    assert recovery.scan_environment(paths["environment"]) == live_before
    assert (forensic / "preserve").read_bytes() == b"failed target evidence"
    assert store.read_snapshot() == current


@pytest.mark.parametrize("operation,when", [("unlink", "before"), ("unlink", "after"), ("rmdir", "before"), ("rmdir", "after")])
def test_cleanup_resumes_after_native_deletion_crash(completed, monkeypatch, operation, when):
    original = getattr(os, operation)
    fired = False

    def crash(path, *args, **kwargs):
        nonlocal fired
        # Record-store scratch files are outside this descriptor-bound tree.
        relevant = str(path) in {"arxiv-digest", "bin"}
        if relevant and not fired and when == "before":
            fired = True
            raise RuntimeError("synthetic cleanup crash")
        result = original(path, *args, **kwargs)
        if relevant and not fired and when == "after":
            fired = True
            raise RuntimeError("synthetic cleanup crash")
        return result

    monkeypatch.setattr(os, operation, crash)
    with pytest.raises(RuntimeError, match="synthetic cleanup crash"):
        clean(completed)
    assert fired
    assert clean(completed) == "cleaned"
    assert clean(completed) == "absent"


@pytest.mark.parametrize("kind", ["unknown", "bytecode", "modified", "symlink", "missing"])
def test_initial_cleanup_refuses_nonexact_snapshot_before_any_deletion(completed, kind):
    snapshot = Path(completed[0].record["paths"]["snapshot"])
    member = snapshot / "bin/arxiv-digest"
    if kind == "unknown":
        (snapshot / "preserve").write_bytes(b"unknown")
    elif kind == "bytecode":
        (snapshot / "__pycache__").mkdir()
        (snapshot / "__pycache__/foreign.pyc").write_bytes(b"unknown")
    elif kind == "modified":
        member.write_bytes(b"unexpected changes")
    elif kind == "symlink":
        member.unlink()
        member.symlink_to(completed[0].record["paths"]["environment"])
    else:
        member.unlink()
    before = {str(path.relative_to(snapshot)): recovery._stable(path.lstat()) for path in snapshot.rglob("*")}
    with pytest.raises((recovery.SnapshotError, protocol.StoreError)):
        clean(completed)
    assert {str(path.relative_to(snapshot)): recovery._stable(path.lstat()) for path in snapshot.rglob("*")} == before


def test_interrupted_cleanup_refuses_changed_identity_or_unknown_additions(completed, monkeypatch):
    snapshot = Path(completed[0].record["paths"]["snapshot"])
    original = os.unlink
    fired = False

    def crash(path, *args, **kwargs):
        nonlocal fired
        if path == "arxiv-digest" and not fired:
            fired = True
            raise RuntimeError("synthetic cleanup crash")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", crash)
    with pytest.raises(RuntimeError, match="synthetic cleanup crash"):
        clean(completed)
    member = snapshot / "bin/arxiv-digest"
    payload = member.read_bytes()
    member.rename(snapshot.parent / "original-preserved")
    member.write_bytes(payload)
    member.chmod(0o700)
    before = recovery.scan_environment(snapshot)
    with pytest.raises(recovery.SnapshotError):
        clean(completed)
    assert recovery.scan_environment(snapshot) == before


def test_cleanup_rejects_inactive_guard(completed):
    plan, store, current, locks = completed
    locks["launcher"].close()
    descriptor = protocol.open_private_lock_file(Path(plan.record["paths"]["recovery_root"]) / "launcher-operation.lock")
    locks["launcher"] = helper.LockReference(descriptor)
    with pytest.raises(RuntimeError):
        clean(completed)
    assert Path(plan.record["paths"]["snapshot"]).is_dir()


def test_cleanup_rejects_active_journal(tmp_path):
    prepared = prepared_runtime(tmp_path / "recovery")
    try:
        with pytest.raises(recovery.SnapshotError, match="healthy completion"):
            clean(prepared)
        assert Path(prepared[0].record["paths"]["snapshot"]).is_dir()
    finally:
        for lock in prepared[3].values():
            lock.close()


def test_cleanup_timeout_preserves_terminal_receipt_and_retries(completed, monkeypatch):
    monkeypatch.setattr(protocol, "SNAPSHOT_CLEANUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(recovery.SnapshotError, match="budget"):
        clean(completed)
    assert completed[1].read_snapshot() == completed[2]
    monkeypatch.setattr(protocol, "SNAPSHOT_CLEANUP_TIMEOUT_SECONDS", 5)
    assert clean(completed) == "cleaned"


def interrupt_after_one_member(completed, monkeypatch):
    original = os.unlink
    fired = False

    def crash(path, *args, **kwargs):
        nonlocal fired
        result = original(path, *args, **kwargs)
        if path == "arxiv-digest" and not fired:
            fired = True
            raise RuntimeError("synthetic cleanup crash")
        return result

    monkeypatch.setattr(os, "unlink", crash)
    with pytest.raises(RuntimeError, match="synthetic cleanup crash"):
        clean(completed)
    assert fired


def test_retired_cleanup_intent_retries_after_next_plan_completes(completed, monkeypatch):
    interrupt_after_one_member(completed, monkeypatch)
    plan, store, current, locks = completed
    root = Path(plan.record["paths"]["recovery_root"])
    old_snapshot = Path(plan.record["paths"]["snapshot"])
    store.acknowledge(current.record["receipt"]["receipt_id"])
    record = copy.deepcopy(plan.record)
    record["attempt_id"] = "b" * 64
    record["paths"]["snapshot"] = str(old_snapshot.with_name("b" * 64))
    record["paths"]["forensic"] = record["paths"]["snapshot"] + ".failed"
    recovery.create_snapshot(record["paths"]["environment"], record["paths"]["snapshot"],
        record["paths"]["exposed_command"], record["paths"]["base_interpreter"])
    new_plan = protocol.ProtectedPlanStore(root).publish(record)
    new_current = store.admit(new_plan)
    with pytest.raises(recovery.SnapshotError, match="healthy completion"):
        clean((new_plan, store, new_current, locks))
    assert old_snapshot.exists()
    new_current = finish_restored(new_plan, store, new_current)
    assert clean((new_plan, store, new_current, locks)) == "cleaned"
    assert not old_snapshot.exists()
    assert not Path(record["paths"]["snapshot"]).exists()
    assert protocol.SnapshotCleanupStore(root).list_snapshots() == ()


def test_terminal_wrapper_retries_without_needing_ordinary_lock(completed, monkeypatch):
    interrupt_after_one_member(completed, monkeypatch)
    plan, _store, _current, locks = completed
    locks["transition"].close()
    locks["launcher"].close()
    # The healthy application keeps its ordinary lock throughout this retry.
    assert helper.run_recovery(SimpleNamespace(explicit_recovery=False), plan) == 0
    assert not Path(plan.record["paths"]["snapshot"]).exists()


@pytest.mark.parametrize("change", ["unknown", "moved_parent"])
def test_cleanup_stops_on_changes_after_scan_before_deletion(completed, monkeypatch, change):
    snapshot = Path(completed[0].record["paths"]["snapshot"])
    original_scan = recovery._cleanup_scan
    original_open = os.open
    scanned = 0
    moved = False

    def scan(*args, **kwargs):
        nonlocal scanned
        result = original_scan(*args, **kwargs)
        scanned += 1
        if scanned == 2 and change == "unknown":
            (snapshot / "preserve").write_bytes(b"unknown arrival")
        return result

    def opened(path, *args, **kwargs):
        nonlocal moved
        result = original_open(path, *args, **kwargs)
        if scanned == 2 and change == "moved_parent" and path == "bin" and not moved:
            moved = True
            (snapshot / "bin").rename(snapshot.parent / "moved-bin")
            (snapshot / "bin").mkdir(mode=0o700)
        return result

    monkeypatch.setattr(recovery, "_cleanup_scan", scan)
    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(recovery.SnapshotError):
        clean(completed)
    member = snapshot.parent / "moved-bin/arxiv-digest" if moved else snapshot / "bin/arxiv-digest"
    assert member.read_bytes() == b"synthetic old application"
    assert (snapshot / "pipx_metadata.json").exists()
