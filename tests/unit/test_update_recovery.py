from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from arxiv_digest.update_runtime import protocol, recovery
from tests.unit.test_update_snapshot import layout


class JournalBoundary:
    """Filesystem fault tests isolate the independently tested journal store."""
    def __init__(self, record):
        protocol.validate_replay(record["replay"])
        self.current = SimpleNamespace(record=record)

    def validate_snapshot(self, expected):
        if expected is not self.current:
            raise recovery.SnapshotError("stale journal")
        return expected


def prepared_replay(layout):
    env, exposed, interpreter, snapshot = layout
    old = recovery.create_snapshot(env, snapshot, exposed, interpreter)["core"]
    (env / "bin/arxiv-digest").write_bytes(b"partial target")
    exposed.unlink()
    replay = {
        "snapshot_path": str(snapshot), "live_path": str(env),
        "forensic_path": str(snapshot.with_name(snapshot.name + ".failed")),
        "old_token": old, "partial_token": recovery.capture_partial_environment(env),
        "prior_provenance": None, "partial_provenance": None,
        "partial_exposed": None, "target_started": False,
        "process_group_ids": [100000001], "processes_dead": True,
    }
    store = JournalBoundary({"state": "rolling_back", "subphase": "package_restore_pending", "attempt_id": "a" * 64, "replay": replay})
    return store


def run(store, death=lambda groups: True):
    return recovery.replay_snapshot(journal_store=store, journal_snapshot=store.current, process_death=death)


def test_two_shape_replay_consumes_snapshot_then_verifies_exact_successor(layout):
    store = prepared_replay(layout)
    env, exposed, _, snapshot = layout
    assert run(store) == "restored"
    assert not snapshot.exists()
    assert run(store) == "already_restored"
    assert (env / "bin/arxiv-digest").read_bytes() == b"old application\n"
    assert os.readlink(exposed) == store.current.record["replay"]["old_token"]["exposed_link"]["target"]
    assert (snapshot.with_name(snapshot.name + ".failed") / "bin/arxiv-digest").read_bytes() == b"partial target"


def test_exact_old_live_preservation_retains_snapshot(layout):
    env, exposed, interpreter, snapshot = layout
    old = recovery.create_snapshot(env, snapshot, exposed, interpreter)["core"]
    replay = {
        "snapshot_path": str(snapshot), "live_path": str(env),
        "forensic_path": str(snapshot.with_name(snapshot.name + ".failed")),
        "old_token": old, "partial_token": recovery.capture_partial_environment(env),
        "prior_provenance": None, "partial_provenance": None, "partial_exposed": None,
        "target_started": False, "process_group_ids": [], "processes_dead": True,
    }
    store = JournalBoundary({"state": "rolling_back", "subphase": "package_restore_pending", "attempt_id": "a" * 64, "replay": replay})
    exposed.unlink()
    assert run(store) == "preserved_old"
    assert snapshot.is_dir()
    assert run(store) == "preserved_old"


@pytest.mark.parametrize("operation,when", [("quarantine", "before"), ("quarantine", "after"), ("restore", "before"), ("restore", "after"), ("link", "before"), ("link", "after")])
def test_replay_resumes_at_each_native_mutation_boundary(layout, monkeypatch, operation, when):
    store = prepared_replay(layout)
    rename = protocol.atomic_rename_noreplace
    restore_link = recovery._restore_exposed_link
    fired = False

    def faulting_rename(source, destination, **kwargs):
        nonlocal fired
        relevant = (operation == "quarantine" and source.name == layout[0].name) or (operation == "restore" and source.name == layout[3].name)
        if relevant and not fired and when == "before":
            fired = True
            raise RuntimeError("synthetic crash")
        rename(source, destination, **kwargs)
        if relevant and not fired and when == "after":
            fired = True
            raise RuntimeError("synthetic crash")

    def faulting_link(*args):
        nonlocal fired
        if operation == "link" and not fired and when == "before":
            fired = True
            raise RuntimeError("synthetic crash")
        restore_link(*args)
        if operation == "link" and not fired and when == "after":
            fired = True
            raise RuntimeError("synthetic crash")

    monkeypatch.setattr(protocol, "atomic_rename_noreplace", faulting_rename)
    monkeypatch.setattr(recovery, "_restore_exposed_link", faulting_link)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        run(store)
    assert fired
    assert run(store) in {"restored", "already_restored"}
    assert run(store) == "already_restored"


@pytest.mark.parametrize("conflict", ["live", "forensic", "snapshot", "link", "death", "absent_snapshot", "wrong_attempt"])
def test_replay_refuses_conflicts_without_package_mutation(layout, conflict):
    store = prepared_replay(layout)
    env, exposed, _, snapshot = layout
    if conflict == "live":
        (env / "external").write_bytes(b"preserve")
    elif conflict == "forensic":
        snapshot.with_name(snapshot.name + ".failed").mkdir()
    elif conflict == "snapshot":
        (snapshot / "external").write_bytes(b"preserve")
    elif conflict == "link":
        exposed.symlink_to("/unrelated/application")
    elif conflict == "absent_snapshot":
        snapshot.rename(snapshot.with_name("unrelated"))
    elif conflict == "wrong_attempt":
        store.current.record["attempt_id"] = "b" * 64
    before = recovery.scan_environment(env)
    with pytest.raises(recovery.SnapshotError):
        run(store, death=lambda groups: conflict != "death")
    assert recovery.scan_environment(env) == before


def test_prepared_or_unproven_rollback_cannot_authorize_restoration(layout):
    store = prepared_replay(layout)
    store.current.record["state"] = "prepared"
    with pytest.raises(recovery.SnapshotError, match="committed"):
        run(store)
    store.current.record["state"] = "rolling_back"
    store.current.record["replay"]["processes_dead"] = False
    with pytest.raises(recovery.SnapshotError, match="death"):
        run(store)
