"""Freeze real pipx 1.16.7 install/recovery behavior before helper development."""

from __future__ import annotations

import json
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from arxiv_digest.update_pipx import CommandTimeoutError, run_bounded_command
from tests.update_pipx_factory import (
    RealPipx, InstallationSnapshot, assert_process_group_dead, inventory,
    real_pipx_installation, require_empty_safe_trash,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="automatic pipx support is macOS/Linux")


@pytest.fixture
def installation(tmp_path: Path) -> RealPipx:
    return real_pipx_installation(tmp_path / "real-pipx")


def _nonapplication_facts(facts: dict) -> dict:
    return facts | {"distributions": [item for item in facts["distributions"] if item[0] != "arxiv-digest"]}


def test_exact_install_argv_preserves_interpreter_and_every_other_distribution(installation: RealPipx) -> None:
    from arxiv_digest.update_runtime import recovery

    fixture = installation
    before = fixture.facts()
    files_before = fixture.nonapplication_files()
    sentinels = fixture.sentinels()
    config_before = (fixture.venv / "pyvenv.cfg").read_bytes()
    python_target = os.readlink(fixture.venv / "bin/python")
    python_identity = (fixture.venv / "bin/python").stat()
    external = {path.relative_to(fixture.venv).as_posix(): os.readlink(path)
                for path in (fixture.venv / "bin").iterdir()
                if path.is_symlink() and path.name.startswith("python") and path.resolve() == fixture.source_python.resolve()}
    core = recovery.capture_core_installation_token(fixture.venv, fixture.exposed, fixture.source_python.resolve(), allowed_external_symlinks=external)
    argv = fixture.install_argv(fixture.target)
    assert type(argv) is tuple
    assert argv[1:] == (
        "install", "--force", "--app", "arxiv-digest", "--python", str(fixture.source_python),
        "--fetch-python=never", "--skip-maintenance", "--backend=pip",
        "--pip-args=--no-deps --no-index", str(fixture.target),
    )
    result = fixture.install(fixture.target)
    assert result.returncode == 0, result.stderr.decode()
    recovery.validate_target_installation({"old_version": "0.3.0", "target_version": "0.3.1", "old_token": {"core": core, "provenance": None},
        "paths": {"environment": str(fixture.venv), "exposed_command": str(fixture.exposed), "base_interpreter": str(fixture.source_python.resolve())}})
    assert b"--python is ignored when --force is passed" in result.stdout + result.stderr
    after = fixture.facts()
    assert ["arxiv-digest", "0.3.1"] in after["distributions"]
    assert _nonapplication_facts(after) == _nonapplication_facts(before)
    assert ["synthetic-transitive", "1.0"] in after["distributions"]
    assert fixture.nonapplication_files() == files_before
    assert (fixture.venv / "pyvenv.cfg").read_bytes() == config_before
    assert os.readlink(fixture.venv / "bin/python") == python_target
    actual_python = (fixture.venv / "bin/python").stat()
    assert (actual_python.st_dev, actual_python.st_ino) == (python_identity.st_dev, python_identity.st_ino)
    assert fixture.sentinels() == sentinels
    native = json.loads((fixture.venv / "pipx_metadata.json").read_text())
    assert native["backend"] == "pip"
    assert native["main_package"]["expected_apps"] == ["arxiv-digest"]
    assert native["main_package"]["pip_args"] == ["--no-deps", "--no-index"]
    assert native["main_package"]["package_version"] == "0.3.1"
    assert json.loads(fixture.run((str(fixture.exposed),)).stdout)["version"] == "0.3.1"
    assert not list(fixture.trash.iterdir())
    hooked = [json.loads(path.read_text()) for path in (fixture.root / "hook-processes").iterdir()]
    assert any(
        item["executable"] == str(fixture.venv / "bin/python")
        and "pip" in item["argv"] and str(fixture.target) in item["argv"]
        for item in hooked
    ), "the actual pip installation subprocess must have active audit instrumentation"
    fixture.assert_offline()


def test_child_network_guard_survives_pipx_removing_pythonpath(installation: RealPipx) -> None:
    fixture = installation
    from dataclasses import replace

    request = fixture.request((str(fixture.venv / "bin/python"), "-c", (
        "import socket; socket.getaddrinfo('example.invalid', 443)"
    )))
    request = replace(request, environ={key: value for key, value in request.environ.items() if key != "PYTHONPATH"})
    result = run_bounded_command(request)
    assert result.returncode != 0
    assert b"network disabled in pipx capability experiment" in result.stderr
    assert (fixture.root / "network-attempts").read_text() == "socket.getaddrinfo\n"


def test_missing_required_app_restores_exact_old_environment_without_rollback_pipx(installation: RealPipx) -> None:
    fixture = installation
    before = inventory(fixture.venv)
    facts = fixture.facts()
    sentinels = fixture.sentinels()
    link = os.readlink(fixture.exposed)
    result = fixture.install(fixture.missing_app)
    assert result.returncode != 0
    assert b"arxiv-digest" in result.stderr + result.stdout
    # pip installs the target before --app validation rejects it. This checks
    # pipx's own transaction, including metadata and executable preservation.
    assert inventory(fixture.venv) == before
    assert fixture.facts() == facts
    assert os.readlink(fixture.exposed) == link
    assert json.loads(fixture.run((str(fixture.exposed),)).stdout)["version"] == "0.3.0"
    assert fixture.sentinels() == sentinels
    assert not list(fixture.trash.iterdir())
    fixture.assert_offline()


def test_skip_maintenance_still_deletes_same_home_trash(installation: RealPipx) -> None:
    fixture = installation
    fixture.trash.mkdir(exist_ok=True)
    sentinel = fixture.trash / "synthetic-existing-trash"
    sentinel.write_bytes(b"pipx must not be trusted to preserve this")
    untouched = fixture.sentinels()
    # Deliberately bypass admission only in this disposable fixture to pin
    # upstream behavior, which --skip-maintenance does not prevent.
    result = run_bounded_command(fixture.request(fixture.install_argv(fixture.target)))
    assert result.returncode == 0
    assert not sentinel.exists()
    assert fixture.sentinels() == untouched
    fixture.assert_offline()


@pytest.mark.parametrize("kind", ["nonempty", "symlink", "writable", "file"])
def test_unsafe_trash_refuses_before_real_pipx_invocation(installation: RealPipx, kind: str) -> None:
    fixture = installation
    fixture.trash.mkdir(exist_ok=True)
    if kind == "nonempty":
        (fixture.trash / "sentinel").write_bytes(b"preserve synthetic existing trash")
    elif kind == "symlink":
        fixture.trash.rmdir()
        fixture.trash.symlink_to(fixture.root / "unrelated-pipx/.trash")
    elif kind == "writable":
        fixture.trash.chmod(0o777)
    else:
        fixture.trash.rmdir()
        fixture.trash.write_bytes(b"preserve unexpected trash file")
    before = inventory(fixture.root)
    with pytest.raises(ValueError, match="trash is populated or unsafe"):
        fixture.install(fixture.target)
    assert inventory(fixture.root) == before  # Even pipx logs/lock stay untouched.


@pytest.mark.parametrize("phase", ["environment_renamed", "exposed_link_unlinked"])
@pytest.mark.parametrize("termination", ["kill", "timeout"])
def test_interrupted_pipx_requires_group_death_before_snapshot_recovery(
    installation: RealPipx, phase: str, termination: str,
) -> None:
    fixture = installation
    facts = fixture.facts()
    sentinels = fixture.sentinels()
    snapshot = InstallationSnapshot.capture(fixture)
    (fixture.root / "fault.json").write_text(json.dumps({"phase": phase}))
    wheel = fixture.missing_app if phase == "environment_renamed" else fixture.target
    require_empty_safe_trash(fixture.trash)
    request = fixture.request(fixture.install_argv(wheel), timeout=12)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_bounded_command, request)
        marker = fixture.root / "phase.json"
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and not future.done() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), "pipx did not reach the destructive fault phase"
            observed = json.loads(marker.read_text())
            assert observed["phase"] == phase
            assert observed["pgid"] == observed["pid"]
            if phase == "environment_renamed":
                assert not fixture.venv.exists()
                assert fixture.exposed.is_symlink() and not fixture.exposed.exists()
                assert any(path.name.endswith(".arxiv-digest") for path in fixture.trash.iterdir())
            else:
                assert fixture.venv.exists()
                assert not fixture.exposed.is_symlink()
            partial = inventory(fixture.root)
            with pytest.raises(AssertionError, match="still alive; recovery refused"):
                snapshot.restore(fixture, pgid=observed["pgid"])
            assert inventory(fixture.root) == partial
            if termination == "kill":
                os.killpg(observed["pgid"], signal.SIGKILL)
                assert future.result(timeout=5).returncode == -signal.SIGKILL
            else:
                # The hook ignores TERM, requiring the real bounded command
                # supervisor to escalate to KILL and reap the group leader.
                with pytest.raises(CommandTimeoutError):
                    future.result(timeout=25)
            assert_process_group_dead(observed["pgid"])
            snapshot.restore(fixture, pgid=observed["pgid"])
        finally:
            if marker.exists() and not future.done():
                try:
                    os.killpg(json.loads(marker.read_text())["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
    (fixture.root / "fault.json").write_text("{}")
    assert fixture.facts() == facts
    assert json.loads(fixture.run((str(fixture.exposed),)).stdout)["version"] == "0.3.0"
    assert fixture.sentinels() == sentinels
    # Preserve incomplete transactions as evidence; a second pipx invocation
    # would erase its trash. Recovery uses only the independent snapshot.
    assert any(fixture.trash.iterdir())
    fixture.assert_offline()
