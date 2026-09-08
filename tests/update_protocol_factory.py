"""Synthetic closed updater records shared by protocol/recovery integration tests."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from arxiv_digest.update_runtime import protocol


ATTEMPT = "a" * 64


def identity(*, mode=0o600, size=1, inode=1):
    return {"device": 1, "inode": inode, "uid": os.getuid(), "mode": mode,
            "size": size, "mtime_ns": 1, "ctime_ns": 1, "nlink": 1}


def directory_identity(*, mode=0o700, inode=1):
    return {key: value for key, value in identity(mode=mode, inode=inode).items()
            if key in {"device", "inode", "uid", "mode"}}


def core_token(root: Path):
    return {"environment_path": str(root / "pipx/venvs/arxiv-digest"),
            "environment_identity": directory_identity(),
            "inventory": {"root_mode": 0o700, "entries": []},
            "exposed_link": {"path": str(root / "bin/arxiv-digest"),
                "target": str(root / "pipx/venvs/arxiv-digest/bin/arxiv-digest"),
                "uid": os.getuid(), "parent": directory_identity()},
            "pipx_metadata_sha256": "b" * 64,
            "interpreter": {"path": str(root / "base/python"), "device": 1, "inode": 1,
                "uid": os.getuid(), "mode": 0o755, "size": 1, "sha256": "c" * 64}}


def artifact(path: Path, *, payload=b"x"):
    return {"path": str(path), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "identity": identity(size=len(payload))}


def wheel(root: Path, *, version="0.3.1"):
    value = artifact(root / ATTEMPT / f"arxiv_digest-{version}-py3-none-any.whl")
    value.update(version=version, manifest_sha256="d" * 64,
                 source_url=f"https://github.com/yuzhangmath/arxiv-digest/releases/download/v{version}/arxiv_digest-{version}-py3-none-any.whl")
    return value


def plan_record(recovery_root: Path, *, attempt_id=ATTEMPT, old_token=None):
    base = recovery_root.parent
    core = core_token(base) if old_token is None else old_token["core"]
    paths = {
        "recovery_root": str(recovery_root), "environment": core["environment_path"],
        "snapshot": str(base / "pipx/arxiv-digest-update-snapshots" / attempt_id),
        "forensic": str(base / "pipx/arxiv-digest-update-snapshots" / (attempt_id + ".failed")),
        "pipx": str(base / "pipx-command"), "base_interpreter": core["interpreter"]["path"],
        "exposed_command": core["exposed_link"]["path"], "instance_lock": str(base / "runtime.lock"),
        "diagnostic_log": str(recovery_root / "update-diagnostic.log"),
        "config_dir": str(base / "config"), "data_dir": str(base / "data"), "cache_dir": str(base / "cache"),
        "user_home": str(base / "home"), "pipx_home": str(Path(core["environment_path"]).parent.parent),
        "pipx_shared_libs": str(base / "pipx/shared"), "pipx_bin_dir": str(Path(core["exposed_link"]["path"]).parent),
        "pipx_man_dir": str(base / "man"), "pipx_completion_dir": str(base / "completions"),
    }
    paths["snapshot"] = str(Path(paths["pipx_home"]) / "arxiv-digest-update-snapshots" / attempt_id)
    paths["forensic"] = paths["snapshot"] + ".failed"
    backup = artifact(recovery_root / "backup.zip")
    backup.update(source_version="0.3.0", data_generation=2,
                  pdf_destination={"kind": "custom", "path": str(base / "pdfs")})
    return {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1,
        "application_data_generation": 2, "attempt_id": attempt_id, "old_version": "0.3.0", "target_version": "0.3.1",
        "parent_pid": os.getpid(), "paths": paths, "old_token": old_token or {"core": core, "provenance": None},
        "pipx_identity": {**core["interpreter"], "path": paths["pipx"]},
        "target_wheel": wheel(recovery_root), "prior_provenance": None, "backup": backup,
        "runtime": [{"name": name, "size": 1, "sha256": "e" * 64, "mode": 0o600, "identity": identity()}
                    for name in sorted(protocol.RUNTIME_FILENAMES)],
        "wrapper": {"path": str(recovery_root / "recover-arxiv-digest"), "size": 1, "sha256": "e" * 64, "mode": 0o700, "identity": identity(mode=0o700)},
        "launcher": {which: {"kind": "absent", "path": str(base / "launcher")} for which in ("prior", "intended")},
        "lock_identities": {name: directory_identity(mode=0o600) for name in ("transition", "launcher", "instance")}}


def publish_plan(recovery_root: Path, **kwargs):
    recovery_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return protocol.ProtectedPlanStore(recovery_root).publish(plan_record(recovery_root, **kwargs))


def admit_journal(recovery_root: Path, **kwargs):
    plan = publish_plan(recovery_root, **kwargs)
    store = protocol.JournalStore(recovery_root)
    return store, store.admit(plan)


def next_record(snapshot, state, **fields):
    record = {key: snapshot.record[key] for key in protocol.COMMON | {"attempt_id", "plan_sha256"}}
    if state == "installing" and "installer" not in fields:
        fields["installer"] = {"guard_pid": os.getpid(), "process_group_id": os.getpid()}
    record.update(state=state, **fields)
    return record


def proposal(*, outcome="updated", attempt_id=ATTEMPT):
    return {"receipt_id": "f" * 64, "outcome": outcome, "installed_version": "0.3.1" if outcome == "updated" else "0.3.0",
        "attempted_version": "0.3.1", "message_code": protocol.OUTCOME_MESSAGES[outcome],
        "attempt_id": attempt_id, "launch_id": "e" * 64 if outcome in {"updated", "restored"} else None}


def completed_journal(recovery_root: Path):
    store, current = admit_journal(recovery_root)
    for state in ("committed", "installing", "target_installed", "launching_target"):
        target = next_record(current, state)
        auth = protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], target["installer"]) if state == "installing" else None
        current = store.transition(current, target, authorization=auth)
    current = store.transition(current, next_record(current, "healthy_pending_commit", proposal=proposal()))
    current = store.transition(current, next_record(current, "complete", receipt={**proposal(), "unacknowledged": True}))
    return store, current
