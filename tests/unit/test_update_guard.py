from __future__ import annotations

import os
from pathlib import Path

import pytest

from arxiv_digest.update_runtime import guard
from tests.update_protocol_factory import plan_record


def test_guard_uses_exact_offline_argv_and_sanitized_private_environment(tmp_path, monkeypatch):
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    plan = plan_record(root)
    monkeypatch.setenv("PIP_INDEX_URL", "https://untrusted.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    command = guard.install_argv(plan)
    assert command == (plan["paths"]["pipx"], "install", "--force", "--app", "arxiv-digest", "--python", plan["paths"]["base_interpreter"], "--fetch-python=never", "--skip-maintenance", "--backend=pip", "--pip-args=--no-deps --no-index", plan["target_wheel"]["path"])
    environment = guard.install_environment(plan)
    assert "PIP_INDEX_URL" not in environment and "PYTHONPATH" not in environment
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIPX_HOME"] == plan["paths"]["pipx_home"]
    assert Path(environment["HOME"]).is_relative_to(root)


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
