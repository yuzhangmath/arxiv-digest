"""Isolated copied-runtime records; never an installed application's state."""
from __future__ import annotations

import hashlib
import json
import os
import venv
from pathlib import Path

from arxiv_digest.update_runtime import helper, protocol, recovery
from tests.update_protocol_factory import plan_record


def private_test_interpreter(root: Path) -> Path:
    """Copy Python into a synthetic venv without trusting host file metadata."""
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True)
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    interpreter = root / "bin/python"
    assert not interpreter.is_symlink()
    recovery.capture_executable_identity(interpreter)
    return interpreter


def prepared_runtime(root: Path, *, source_overrides=None, pipx_payload=None):
    root.mkdir(mode=0o700, parents=True)
    env = root.parent / "pipx/venvs/arxiv-digest"
    env.mkdir(parents=True, mode=0o700)
    (env / "bin").mkdir(mode=0o700)
    (env / "bin/arxiv-digest").write_bytes(b"synthetic old application")
    (env / "bin/arxiv-digest").chmod(0o700)
    exposed = root.parent / "bin/arxiv-digest"
    exposed.parent.mkdir(mode=0o700)
    exposed.symlink_to(env / "bin/arxiv-digest")
    pipx = root.parent / "pipx-command"
    pipx.write_bytes(pipx_payload or b"#!/bin/sh\nexit 17\n")
    pipx.chmod(0o700)
    interpreter = private_test_interpreter(root.parent / "fixture-interpreter")
    (env / "pipx_metadata.json").write_text(json.dumps({
        "synthetic": True, "source_interpreter": {"__type__": "Path", "__Path__": str(interpreter)},
    }))
    snapshot = root.parent / "pipx/arxiv-digest-update-snapshots" / ("a" * 64)
    snapshot.parent.mkdir(mode=0o700)
    token = recovery.create_snapshot(env, snapshot, exposed, interpreter)
    plan = plan_record(root, old_token=token)
    sources = {name: (Path(recovery.__file__).parent / name).read_bytes() for name in protocol.RUNTIME_FILENAMES}
    sources.update(source_overrides or {})
    materialized = recovery.materialize_runtime(root, interpreter, sources)
    plan.update(materialized)
    plan["pipx_identity"] = recovery.capture_executable_identity(pipx)
    plan["paths"]["pipx"] = str(pipx)
    for name in ("target_wheel", "backup"):
        artifact = plan[name]
        path = Path(artifact["path"])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(b"synthetic protected artifact")
        path.chmod(0o600)
        artifact.update(size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), identity=recovery._file_identity(path.lstat()))
    locks = {}
    for name, path in (
        ("transition", root / "update-transition.lock"), ("launcher", root / "launcher-operation.lock"),
        ("instance", Path(plan["paths"]["instance_lock"])),
    ):
        fd = protocol.open_private_lock_file(path)
        plan["lock_identities"][name] = recovery._identity(os.fstat(fd))
        os.close(fd)
        locks[name] = helper._acquire(path, plan["lock_identities"][name], timeout=0)
    saved = protocol.ProtectedPlanStore(root).publish(plan)
    store = protocol.JournalStore(root)
    current = store.admit(saved)
    return saved, store, current, locks
