from __future__ import annotations

from copy import deepcopy

import pytest

from arxiv_digest.update_runtime import protocol
from tests.update_protocol_factory import completed_journal, directory_identity


def intent(root):
    journal_store, journal = completed_journal(root)
    plan = protocol.ProtectedPlanStore(root).read_snapshot()
    record = {**{key: plan.record[key] for key in protocol.COMMON},
        "attempt_id": plan.record["attempt_id"], "plan_sha256": plan.sha256,
        "snapshot_path": plan.record["paths"]["snapshot"], "root_identity": directory_identity(inode=2),
        "entries": [], "receipt_id": journal.record["receipt"]["receipt_id"], "provenance": None}
    return protocol.SnapshotCleanupStore(root), record, plan, journal_store, journal


@pytest.mark.parametrize("mutation", ["plan", "path", "live_identity", "inventory", "receipt", "provenance"])
def test_cleanup_intent_rejects_authority_different_from_healthy_records(tmp_path, mutation):
    store, record, plan, journals, journal = intent(tmp_path / "recovery")
    if mutation == "plan": record["plan_sha256"] = "f" * 64
    if mutation == "path": record["snapshot_path"] = record["snapshot_path"].replace("pipx", "other-pipx")
    if mutation == "live_identity": record["root_identity"] = plan.record["old_token"]["core"]["environment_identity"]
    if mutation == "inventory": record["entries"] = [{"entry": {"kind": "directory", "path": "foreign", "mode": 0o700}, "identity": directory_identity()}]
    if mutation == "receipt": record["receipt_id"] = "1" * 64
    if mutation == "provenance":
        from tests.update_protocol_factory import identity
        record["provenance"] = {"identity": identity(), "sha256": "f" * 64}
    with pytest.raises(protocol.StoreError):
        store.publish(record, expected_plan=plan, expected_journal=journal, expected_provenance=None)
    assert store.read_snapshot(plan.record["attempt_id"]) is None


def test_cleanup_intent_is_immutable_and_acknowledgement_does_not_change_it(tmp_path):
    store, record, plan, journals, journal = intent(tmp_path / "recovery")
    saved = store.publish(record, expected_plan=plan, expected_journal=journal, expected_provenance=None)
    assert protocol.decode_snapshot_cleanup(saved.canonical_bytes) == record
    assert journals.acknowledge(journal.record["receipt"]["receipt_id"])
    assert store.read_snapshot(record["attempt_id"]) == saved
    acknowledged = journals.read_snapshot()
    replacement = {**record, "receipt_id": None}
    with pytest.raises(protocol.StaleSnapshotError):
        store.publish(replacement, expected_plan=plan, expected_journal=acknowledged, expected_provenance=None)
    store.remove(saved)
    assert store.read_snapshot(record["attempt_id"]) is None


def test_acknowledged_healthy_terminal_can_begin_deferred_cleanup(tmp_path):
    store, record, plan, journals, journal = intent(tmp_path / "recovery")
    assert journals.acknowledge(journal.record["receipt"]["receipt_id"])
    record["receipt_id"] = None
    saved = store.publish(record, expected_plan=plan, expected_journal=journals.read_snapshot(), expected_provenance=None)
    assert saved.record == record


def test_cleanup_codec_rejects_unbounded_unknown_and_wrong_symlink_evidence(tmp_path):
    _, record, *_ = intent(tmp_path / "recovery")
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_snapshot_cleanup({**record, "future": True})
    from tests.update_protocol_factory import identity
    bad = deepcopy(record)
    bad["entries"] = [{"entry": {"kind": "symlink", "path": "link", "mode": 0o777, "target": "target"},
                       "identity": identity(mode=0o777, size=1)}]
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_snapshot_cleanup(bad)


def test_cleanup_enumeration_retains_only_closed_fixed_name_authority(tmp_path):
    root = tmp_path / "recovery"
    store, record, plan, journals, journal = intent(root)
    saved = store.publish(record, expected_plan=plan, expected_journal=journal, expected_provenance=None)
    foreign = root / (protocol.SNAPSHOT_CLEANUP_PREFIX + "b" * 64 + ".json")
    foreign.write_bytes(b"{}"); foreign.chmod(0o600)
    wrong_name = root / (protocol.SNAPSHOT_CLEANUP_PREFIX + "c" * 64 + ".json")
    wrong_name.write_bytes(saved.canonical_bytes); wrong_name.chmod(0o600)
    (root / "cleanup-other.json").write_bytes(saved.canonical_bytes)
    assert store.list_snapshots() == (saved,)
    assert foreign.read_bytes() == b"{}"
    assert store.list_snapshots(deadline_at=0) == ()


@pytest.mark.parametrize("state", ["committed", "external_change_detected"])
def test_active_and_blocking_journals_never_authorize_cleanup_intents(tmp_path, state):
    root = tmp_path / "recovery"
    store, record, plan, journals, journal = intent(root)
    current = {key: value for key, value in journal.record.items() if key != "receipt"}
    current["state"] = state
    (root / protocol.JOURNAL_FILENAME).write_bytes(protocol.encode_journal(current))
    changed = journals.read_snapshot()
    with pytest.raises(protocol.StoreError):
        store.publish(record, expected_plan=plan, expected_journal=changed, expected_provenance=None)
    assert store.list_snapshots() == ()


def test_retired_cleanup_intent_survives_plan_replacement_without_reauthorizing_it(tmp_path):
    from tests.update_protocol_factory import plan_record
    root = tmp_path / "recovery"
    store, record, plan, journals, journal = intent(root)
    saved = store.publish(record, expected_plan=plan, expected_journal=journal, expected_provenance=None)
    journals.acknowledge(journal.record["receipt"]["receipt_id"])
    replacement = protocol.ProtectedPlanStore(root).publish(plan_record(root, attempt_id="b" * 64))
    journals.admit(replacement)
    assert store.list_snapshots() == (saved,)
    assert saved.record["plan_sha256"] != replacement.sha256
    with pytest.raises(protocol.StoreError):
        store.remove(saved)
    assert store.read_snapshot(record["attempt_id"]) == saved
