from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from arxiv_digest.update_runtime import protocol, recovery
from tests.update_protocol_factory import next_record, plan_record, proposal


def sources():
    return {name: b"import sys\n" for name in protocol.RUNTIME_FILENAMES}


def test_fixed_runtime_and_wrapper_are_fsynced_reread_and_idempotent(tmp_path):
    import subprocess
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    first = recovery.materialize_runtime(root, Path("/usr/bin/python3"), sources())
    assert first == recovery.materialize_runtime(root, Path("/usr/bin/python3"), sources())
    assert {item["name"] for item in first["runtime"]} == set(protocol.RUNTIME_FILENAMES)
    assert first["wrapper"]["mode"] == 0o700
    for item in first["runtime"]:
        assert hashlib.sha256((root / "runtime" / item["name"]).read_bytes()).hexdigest() == item["sha256"]
    assert subprocess.run((str(root / "recover-arxiv-digest"), "--unknown"), check=False).returncode == 64


@pytest.mark.parametrize("hostile", ["extra", "package_import", "changed", "symlink"])
def test_materializer_refuses_unlisted_or_unauthorized_runtime(tmp_path, hostile):
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    values = sources()
    if hostile == "extra":
        values["extra.py"] = b"pass\n"
    elif hostile == "package_import":
        values["helper.py"] = b"import arxiv_digest\n"
    else:
        recovery.materialize_runtime(root, Path("/usr/bin/python3"), values)
        if hostile == "changed":
            values["helper.py"] = b"print('new')\n"
        else:
            (root / "runtime/helper.py").unlink()
            (root / "runtime/helper.py").symlink_to(root / "runtime/protocol.py")
    with pytest.raises((recovery.SnapshotError, OSError)):
        recovery.materialize_runtime(root, Path("/usr/bin/python3"), values)


def test_next_runtime_requires_exact_acknowledged_previous_terminal(tmp_path):
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    first = recovery.materialize_runtime(root, Path("/usr/bin/python3"), sources())
    plan = plan_record(root)
    plan["runtime"], plan["wrapper"] = first["runtime"], first["wrapper"]
    saved = protocol.ProtectedPlanStore(root).publish(plan)
    store = protocol.JournalStore(root)
    current = store.admit(saved)
    for state in ("committed", "installing", "target_installed", "launching_target"):
        record = next_record(current, state)
        authorization = protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], record["installer"]) if state == "installing" else None
        current = store.transition(current, record, authorization=authorization)
    current = store.transition(current, next_record(current, "healthy_pending_commit", proposal=proposal()))
    current = store.transition(current, next_record(current, "complete", receipt={**proposal(), "unacknowledged": True}))
    changed = {**sources(), "helper.py": b"import os\n"}
    with pytest.raises(recovery.SnapshotError, match="acknowledged"):
        recovery.materialize_runtime(root, Path("/usr/bin/python3"), changed, prior_journal_store=store, prior_journal_snapshot=current)
    assert store.acknowledge("f" * 64)
    current = store.read_snapshot()
    second = recovery.materialize_runtime(root, Path("/usr/bin/python3"), changed, prior_journal_store=store, prior_journal_snapshot=current)
    assert second["runtime"] != first["runtime"]
    assert (root / "runtime/helper.py").read_bytes() == b"import os\n"


@pytest.mark.parametrize("directory", [False, True])
def test_launcher_capture_refresh_and_restore_preserve_later_changes(tmp_path, directory):
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / ("launcher.app" if directory else "launcher.desktop")
    if directory:
        path.mkdir(mode=0o700)
        (path / "Contents").mkdir(mode=0o700)
        file = path / "Contents/launcher"
    else:
        file = path
    file.write_bytes(b"old launcher\n")
    file.chmod(0o700)
    old = recovery.capture_launcher_state(path)
    file.write_bytes(b"recovery-aware launcher\n")
    intended = recovery.capture_launcher_state(path)
    file.write_bytes(b"old launcher\n")
    transition = acquire_exclusive(tmp_path / "transition.lock", timeout=0)
    launcher = acquire_exclusive(tmp_path / "launcher.lock", timeout=0)
    try:
        kwargs = {"attempt_id": "a" * 64, "transition_lock": transition, "launcher_lock": launcher}
        recovery.replace_launcher_state(old, intended, **kwargs)
        assert recovery.capture_launcher_state(path) == intended
        recovery.replace_launcher_state(intended, old, **kwargs)
        assert recovery.capture_launcher_state(path) == old
        file.write_bytes(b"later legitimate launcher\n")
        with pytest.raises(recovery.SnapshotError):
            recovery.replace_launcher_state(intended, old, **kwargs)
        assert file.read_bytes() == b"later legitimate launcher\n"
    finally:
        launcher.release()
        transition.release()


def test_launcher_absence_is_preserved(tmp_path):
    state = recovery.capture_launcher_state(tmp_path / "absent.desktop")
    assert state == {"kind": "absent", "path": str(tmp_path / "absent.desktop")}
