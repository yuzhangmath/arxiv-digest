from __future__ import annotations

import builtins
import os
import socket

import pytest

from arxiv_digest.update_internal import dispatch_internal


@pytest.mark.parametrize("argv", [
    ["--arxiv-digest-internal-self-check"],
    ["--arxiv-digest-internal-unknown", "3", "/private/tmp/unused", "a" * 64],
    ["--arxiv-digest-internal-relaunch", "-1", "/private/tmp/unused", "a" * 64],
])
def test_unauthenticated_internal_mode_rejects_before_mutable_imports(argv, monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith(("arxiv_digest.application", "arxiv_digest.storage", "arxiv_digest.profile")):
            pytest.fail("unauthenticated mode imported mutable application components")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit, match="authenticated"):
        dispatch_internal(argv)


def test_public_cli_does_not_enter_internal_mode():
    assert dispatch_internal(["doctor"]) is None
    assert dispatch_internal([]) is None


def test_plain_file_is_not_an_authenticated_bootstrap_channel(tmp_path):
    path = tmp_path / "private"
    path.write_bytes(b"{}"); path.chmod(0o600)
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(SystemExit, match="authenticated"):
            dispatch_internal(["--arxiv-digest-internal-self-check", str(fd), str(tmp_path), "a" * 64])
    finally:
        try: os.close(fd)
        except OSError: pass


def test_restored_launch_preserves_prior_provenance_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from arxiv_digest import update_internal as internal
    from arxiv_digest.update_runtime import protocol
    from tests.update_protocol_factory import plan_record
    plan = plan_record(tmp_path / "recovery")
    prior = {"retained": "exact previous provenance"}
    canonical = b"retained prior canonical bytes"
    snapshot = SimpleNamespace(canonical_bytes=canonical)
    plan["prior_provenance"] = prior
    plan["old_token"]["provenance"] = {"sha256": "a" * 64}
    monkeypatch.setattr(protocol, "encode_provenance", lambda record: canonical if record is prior else pytest.fail("rewrote provenance"))
    monkeypatch.setattr(protocol, "ProtectedProvenanceStore", lambda root: SimpleNamespace(read_snapshot=lambda: snapshot,
        compare_and_swap=lambda *args: pytest.fail("restored launch rewrote prior provenance")))
    context = SimpleNamespace(plan=plan, boot={"outcome": "restored"})
    assert internal._provisional_provenance(context, plan["old_token"]["core"]) is snapshot


def test_authenticated_relaunch_adopts_only_ordinary_lock_and_close_preserves_parent(tmp_path):
    import time
    from arxiv_digest.update_internal import _authenticate
    from arxiv_digest.update_locks import acquire_exclusive, LockTimeoutError
    from arxiv_digest.update_runtime import helper, protocol
    from tests.update_runtime_factory import prepared_runtime
    from tests.update_protocol_factory import next_record
    saved, store, current, locks = prepared_runtime(tmp_path / "recovery")
    for state in ("committed", "installing", "target_installed", "launching_target"):
        record = next_record(current, state)
        auth = protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], record["installer"]) if state == "installing" else None
        current = store.transition(current, record, authorization=auth)
    left, right = socket.socketpair()
    channel = protocol.ControlChannel(left)
    ordinary_fd = os.dup(locks["instance"].fileno())
    boot = {"schema_version": 1, "attempt_id": saved.record["attempt_id"], "nonce": "b" * 64, "kind": "BOOT",
        "mode": "relaunch", "plan_sha256": saved.sha256, "launch_id": "c" * 64, "outcome": "updated",
        "lock_fds": {"instance": ordinary_fd}}
    try:
        channel.send(boot, time.monotonic() + 2)
        context = _authenticate(["--arxiv-digest-internal-relaunch", str(right.detach()),
                                 str(tmp_path / "recovery"), saved.record["attempt_id"]])
        assert set(context.locks) == {"instance"}
        assert os.get_inheritable(context.locks["instance"].fileno()) is False
        context.close()
        with pytest.raises(LockTimeoutError):
            acquire_exclusive(saved.record["paths"]["instance_lock"], timeout=0)
    finally:
        channel.close()
        right.close()
        for lock in locks.values():
            lock.close()
