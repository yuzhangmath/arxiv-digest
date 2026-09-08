from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from arxiv_digest.update_runtime import guard, recovery
from tests.update_protocol_factory import plan_record


def installation_plan(tmp_path, *, alias_directory=None, metadata_change=None):
    from tests.update_installation_factory import synthetic_pipx_installation

    fixture = synthetic_pipx_installation(tmp_path / "installation")
    selector = fixture.base_interpreter
    if alias_directory is not None:
        parent = fixture.base_interpreter.parent if alias_directory == "same" else tmp_path / "alias"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        selector = parent / "python-alias"
        selector.symlink_to(fixture.base_interpreter)
    path = fixture.venv / "pipx_metadata.json"
    metadata = json.loads(path.read_bytes())
    metadata["source_interpreter"]["__Path__"] = str(selector)
    if metadata_change is not None:
        metadata_change(metadata)
    path.write_text(json.dumps(metadata))
    token = recovery.capture_installation_token(fixture.venv, fixture.exposed_command,
        fixture.base_interpreter, allowed_external_symlinks={"bin/python": str(fixture.base_interpreter)})
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    return fixture, selector, plan_record(root, old_token=token)


def test_guard_uses_exact_offline_argv_and_sanitized_private_environment(tmp_path, monkeypatch):
    _, _, plan = installation_plan(tmp_path)
    root = Path(plan["paths"]["recovery_root"])
    monkeypatch.setenv("PIP_INDEX_URL", "https://untrusted.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    command = guard.install_argv(plan)
    assert command == (plan["paths"]["pipx"], "install", "--force", "--app", "arxiv-digest", "--python", plan["paths"]["base_interpreter"], "--fetch-python=never", "--skip-maintenance", "--backend=pip", "--pip-args=--no-deps --no-index", plan["target_wheel"]["path"])
    environment = guard.install_environment(plan)
    assert "PIP_INDEX_URL" not in environment and "PYTHONPATH" not in environment
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIPX_HOME"] == plan["paths"]["pipx_home"]
    assert Path(environment["HOME"]).is_relative_to(root)


@pytest.mark.parametrize("alias_directory", ["same", "different"])
def test_guard_retains_authenticated_original_interpreter_selector(tmp_path, alias_directory):
    fixture, selector, plan = installation_plan(tmp_path, alias_directory=alias_directory)
    argv = guard.install_argv(plan)
    assert argv[argv.index("--python") + 1] == str(selector)
    assert plan["paths"]["base_interpreter"] == str(fixture.base_interpreter)


@pytest.mark.parametrize("change", ["metadata", "mode", "wrong_target", "unsafe_parent", "base_bytes"])
def test_guard_refuses_changed_interpreter_selection_evidence(tmp_path, change):
    fixture, selector, plan = installation_plan(tmp_path, alias_directory="different")
    metadata = fixture.venv / "pipx_metadata.json"
    if change == "metadata":
        metadata.write_bytes(metadata.read_bytes() + b"\n")
    elif change == "mode":
        metadata.chmod(0o644 if metadata.stat().st_mode & 0o777 == 0o600 else 0o600)
    elif change == "wrong_target":
        different = selector.parent / "different-python"
        different.write_bytes(fixture.base_interpreter.read_bytes())
        different.chmod(0o700)
        selector.unlink()
        selector.symlink_to(different)
    elif change == "unsafe_parent":
        selector.parent.chmod(0o777)
    else:
        fixture.base_interpreter.write_bytes(b"#!/bin/sh\nexit 1\n")
    with pytest.raises((guard.GuardError, recovery.SnapshotError)):
        guard.install_argv(plan)


@pytest.mark.parametrize("wire", [
    {"__type__": "Unexpected", "__Path__": "/synthetic/python"},
    {"__type__": "Path", "__Path__": "/synthetic/../python"},
    {"__type__": "Path", "__Path__": "relative/python"},
])
def test_guard_refuses_invalid_authenticated_interpreter_wire(tmp_path, wire):
    _, _, plan = installation_plan(tmp_path,
        metadata_change=lambda metadata: metadata.update(source_interpreter=wire))
    with pytest.raises((guard.GuardError, recovery.SnapshotError, ValueError)):
        guard.install_argv(plan)


def test_guard_refuses_interpreter_link_replaced_during_resolution(tmp_path, monkeypatch):
    fixture, selector, plan = installation_plan(tmp_path, alias_directory="different")
    original = Path.resolve
    replaced = False

    def replace_link(path, *args, **kwargs):
        nonlocal replaced
        resolved = original(path, *args, **kwargs)
        if path == selector and not replaced:
            replaced = True
            selector.unlink()
            selector.symlink_to(fixture.base_interpreter)
        return resolved

    monkeypatch.setattr(Path, "resolve", replace_link)
    with pytest.raises((guard.GuardError, recovery.SnapshotError)):
        guard.install_argv(plan)


@pytest.mark.parametrize("kind", ["populated", "symlink", "writable", "file"])
def test_guard_refuses_unsafe_trash_without_changing_it(tmp_path, kind):
    trash = tmp_path / ".trash"
    trash.mkdir(mode=0o700)
    if kind == "populated":
        (trash / "sentinel").write_bytes(b"preserve")
    elif kind == "symlink":
        trash.rmdir()
        trash.symlink_to(tmp_path)
    elif kind == "writable":
        trash.chmod(0o777)
    else:
        trash.rmdir()
        trash.write_bytes(b"preserve")
    before = trash.lstat()
    with pytest.raises(guard.GuardError):
        guard.require_empty_safe_trash(trash)
    assert trash.lstat() == before


def test_guard_never_calls_absent_process_group_alive():
    assert guard.process_group_dead(1_000_000_001)
    assert not guard.process_group_dead(os.getpgrp())


def test_success_cleanup_removes_exact_log_and_preserves_changed_file(tmp_path):
    import hashlib
    from arxiv_digest.update_runtime import helper, recovery

    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    log = root / "update-diagnostic.log"
    log.write_bytes(b"guard output")
    log.chmod(0o600)
    reference = {"identity": recovery._file_identity(log.lstat()), "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
    plan = {"paths": {"diagnostic_log": str(log)}}
    helper.remove_success_log(plan, reference)
    assert not log.exists()
    helper.remove_success_log(plan, reference)
    log.write_bytes(b"different owned file")
    log.chmod(0o600)
    with pytest.raises(helper.HelperError):
        helper.remove_success_log(plan, reference)
    assert log.read_bytes() == b"different owned file"
