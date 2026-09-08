from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from arxiv_digest.update_runtime import guard, helper, protocol, recovery
from tests.update_runtime_factory import prepared_runtime


def spawn(saved, locks):
    plan = saved.record
    parent, child = socket.socketpair()
    nonce = "b" * 64
    command = (*helper._runtime_command(plan, "--helper", nonce, child.fileno()),
               "--transition-fd", str(locks["transition"].fileno()), "--launcher-fd", str(locks["launcher"].fileno()))
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, pass_fds=(child.fileno(), locks["transition"].fileno(), locks["launcher"].fileno()))
    child.close()
    return process, protocol.ControlChannel(parent), nonce


@pytest.mark.parametrize("action", ["cancel", "eof", "commit"])
def test_real_copied_helper_controls_are_durable_and_disarmed_until_parent_exit(tmp_path, action):
    saved, store, _, locks = prepared_runtime(tmp_path / "recovery")
    process, channel, nonce = spawn(saved, locks)
    try:
        ready = channel.receive(time.monotonic() + 10)
        assert ready["kind"] == "READY"
        if action == "eof":
            channel.close()
        else:
            channel.send(helper._message(saved.record["attempt_id"], nonce, "CANCEL" if action == "cancel" else "COMMIT"), time.monotonic() + 5)
            reply = channel.receive(time.monotonic() + 5)
            assert reply["kind"] == ("CANCELED" if action == "cancel" else "COMMITTED")
        if action == "commit":
            assert store.read_snapshot().record["state"] == "committed"
            committed = store.read_snapshot()
            channel.send(helper._message(saved.record["attempt_id"], nonce, "COMMIT"), time.monotonic() + 5)
            assert channel.receive(time.monotonic() + 5)["kind"] == "COMMITTED"
            assert store.read_snapshot() == committed
            time.sleep(0.2)
            assert process.poll() is None
        else:
            assert process.wait(timeout=10) == 0, process.stderr.read().decode()
            assert store.read_snapshot().record["state"] == "canceling_no_install"
        assert Path(saved.record["paths"]["environment"], "bin/arxiv-digest").read_bytes() == b"synthetic old application"
    finally:
        channel.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        for lock in locks.values():
            lock.close()


def test_copied_bootstrap_rejects_tampered_sibling_before_ready(tmp_path):
    saved, store, _, locks = prepared_runtime(tmp_path / "recovery")
    (tmp_path / "recovery/runtime/protocol.py").write_bytes(b"raise RuntimeError('must not execute')\n")
    process, channel, _ = spawn(saved, locks)
    try:
        with pytest.raises(EOFError):
            channel.receive(time.monotonic() + 10)
        assert process.wait(timeout=10) == 1
        error = process.stderr.read()
        assert b"must not execute" not in error
        assert store.read_snapshot().record["state"] == "prepared"
    finally:
        channel.close()
        for lock in locks.values():
            lock.close()


def test_guard_persists_group_identity_before_pipx_and_returns_proven_death(tmp_path):
    import sys
    root = tmp_path / "recovery"
    code = (f"#!{Path(sys.executable).resolve()}\n"
            "import json, os, pathlib\n"
            f"root = pathlib.Path({str(root)!r})\n"
            "record = json.loads((root / 'update-journal.json').read_bytes())\n"
            "assert record['state'] == 'installing'\n"
            "assert record['installer']['process_group_id'] == os.getpgrp()\n"
            "(root / 'observed-durable-installing').write_text('yes')\n"
            "raise SystemExit(17)\n").encode()
    saved, store, current, locks = prepared_runtime(root, pipx_payload=code)
    try:
        store.transition(current, helper._record(current, "committed"))
        result = helper.run_installer(saved, locks["transition"])
        assert result["returncode"] == 17 and result["processes_dead"]
        assert guard.process_group_dead(result["process_group_id"])
        assert (root / "observed-durable-installing").read_text() == "yes"
        assert store.read_snapshot().record["installer"]["process_group_id"] == result["process_group_id"]
    finally:
        for lock in locks.values():
            lock.close()


def test_guard_limits_output_and_escalates_term_ignoring_installer(tmp_path):
    import sys
    root = tmp_path / "recovery"
    source = Path(protocol.__file__).read_bytes()
    source = source.replace(b"PIPX_COMMAND_TIMEOUT_SECONDS = 15.0 * 60.0", b"PIPX_COMMAND_TIMEOUT_SECONDS = 1.5")
    source = source.replace(b"TERM_GRACE_SECONDS = 10.0", b"TERM_GRACE_SECONDS = 0.1")
    code = (f"#!{Path(sys.executable).resolve()}\n"
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "os.write(1, b'x' * (5 * 1024 * 1024))\n"
            "time.sleep(30)\n").encode()
    saved, store, current, locks = prepared_runtime(root, source_overrides={"protocol.py": source}, pipx_payload=code)
    try:
        store.transition(current, helper._record(current, "committed"))
        result = helper.run_installer(saved, locks["transition"])
        assert result["timed_out"] and result["returncode"] == -9
        assert guard.process_group_dead(result["process_group_id"])
        assert (root / "update-diagnostic.log").stat().st_size <= protocol.PRIVATE_LOG_BYTE_LIMIT
    finally:
        for lock in locks.values():
            lock.close()


def test_parent_death_before_commit_durably_disarms_copied_helper(tmp_path):
    import sys
    root = tmp_path / "recovery"
    script = (
        "import json, os, pathlib, sys, time\n"
        "from tests.update_runtime_factory import prepared_runtime\n"
        "from tests.integration.test_update_helper import spawn\n"
        "saved, store, current, locks = prepared_runtime(pathlib.Path(sys.argv[1]))\n"
        "process, channel, nonce = spawn(saved, locks)\n"
        "assert channel.receive(time.monotonic() + 10)['kind'] == 'READY'\n"
        "print(process.pid, flush=True)\n"
        "os._exit(0)\n"
    )
    completed = subprocess.run((sys.executable, "-c", script, str(root)), capture_output=True, timeout=20,
                               env={**os.environ, "PYTHONPATH": os.pathsep.join((str(Path.cwd()), str(Path.cwd() / "src")))})
    assert completed.returncode == 0, completed.stderr.decode()
    store = protocol.JournalStore(root)
    deadline = time.monotonic() + 10
    while store.read_snapshot().record["state"] == "prepared" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert store.read_snapshot().record["state"] == "canceling_no_install"
    assert not (root / "update-diagnostic.log").exists()


def test_guard_cancels_and_reaps_installer_when_helper_channel_closes(tmp_path):
    import sys
    root = tmp_path / "recovery"
    code = (f"#!{Path(sys.executable).resolve()}\nimport time\ntime.sleep(30)\n").encode()
    saved, store, current, locks = prepared_runtime(root, pipx_payload=code)
    parent, child = socket.socketpair()
    nonce = "c" * 64
    process = None
    try:
        store.transition(current, helper._record(current, "committed"))
        process = subprocess.Popen(helper._runtime_command(saved.record, "--guard", nonce, child.fileno()),
                                   close_fds=True, pass_fds=(child.fileno(), locks["transition"].fileno()),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        child.close()
        channel = protocol.ControlChannel(parent)
        channel.send(helper._message(saved.record["attempt_id"], nonce, "GUARD_BOOT", plan_sha256=saved.sha256,
                     transition_fd=locks["transition"].fileno()), time.monotonic() + 5)
        started = channel.receive(time.monotonic() + 10)
        assert started["kind"] == "GUARD_STARTED"
        channel.close()
        process.wait(timeout=15)
        assert guard.process_group_dead(started["pgid"])
        assert store.read_snapshot().record["installer"]["process_group_id"] == started["pgid"]
    finally:
        parent.close()
        child.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        for lock in locks.values():
            lock.close()


def test_fixed_wrapper_cleans_prepared_attempt_and_repeated_launch_is_safe(tmp_path):
    root = tmp_path / "recovery"
    saved, store, _, locks = prepared_runtime(root)
    for lock in locks.values():
        lock.close()
    for _ in range(2):
        result = subprocess.run((str(root / "recover-arxiv-digest"),), capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode()
        assert store.read_snapshot().record["state"] == "aborted_no_mutation"
        assert Path(saved.record["paths"]["environment"], "bin/arxiv-digest").read_bytes() == b"synthetic old application"


def test_blocking_wrapper_requires_explicit_flag_and_failed_self_check_terminalizes(tmp_path):
    root = tmp_path / "recovery"
    saved, store, current, locks = prepared_runtime(root)
    for lock in locks.values():
        lock.close()
    authorization = protocol.ExternalChangeAuthorization(current.sha256, saved.record["attempt_id"], "inventory")
    blocked = store.transition(current, helper._record(current, "external_change_detected"), authorization=authorization)
    result = subprocess.run((str(root / "recover-arxiv-digest"),), capture_output=True, timeout=10)
    assert result.returncode == 1
    assert store.read_snapshot() == blocked
    # The tiny synthetic environment deliberately lacks a runnable old updater:
    # explicit exact-old admission succeeds, then meaningful self-check fails.
    result = subprocess.run((str(root / "recover-arxiv-digest"), "--explicit-recovery"), capture_output=True, timeout=10)
    assert result.returncode == 1
    failed = store.read_snapshot().record
    assert failed["state"] == "recovery_failed"
    assert failed["receipt"]["outcome"] == "recovery_failed"
    assert failed["receipt"]["installed_version"] == "0.3.0"
