from __future__ import annotations

import json

import pytest

from arxiv_digest.update_runtime import protocol


def base_journal(state="prepared"):
    return {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1,
            "application_data_generation": 2, "attempt_id": "a" * 64,
            "plan_sha256": "b" * 64, "state": state}


def test_canonical_round_trip_and_closed_state_fields():
    record = base_journal()
    payload = protocol.encode_journal(record)
    assert payload == json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert protocol.decode_journal(payload) == record
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_journal({**record, "receipt": None})
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_journal({**record, "state": "complete", "proposal": {}})


@pytest.mark.parametrize("change", [
    lambda value: value.rstrip(), lambda value: b" " + value,
    lambda value: value.replace(b'"schema_version":1', b'"schema_version":true'),
    lambda value: value.replace(b'"schema_version":1', b'"schema_version":2'),
    lambda value: value.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
    lambda value: value.replace(b'"schema_version":1', b'"schema_version":NaN'),
])
def test_reject_noncanonical_ambiguous_or_forward_records(change):
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_journal(change(protocol.encode_journal(base_journal())))


def test_inventory_paths_are_lexical_and_closed(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("codec accessed filesystem")
    monkeypatch.setattr(protocol.os, "stat", forbidden)
    inventory = {"root_mode": 0o700, "entries": [
        {"kind": "directory", "path": "bin", "mode": 0o755},
        {"kind": "symlink", "path": "bin/python", "mode": 0o755, "target": "../../base/python"},
    ]}
    assert protocol.decode_inventory(protocol.encode_inventory(inventory)) == inventory
    inventory["entries"][1]["path"] = "../python"
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_inventory(inventory)


def test_enclosing_limits_are_checked_before_decode(monkeypatch):
    monkeypatch.setattr(protocol, "RECORD_BYTE_LIMIT", 8)
    monkeypatch.setattr(protocol.json, "loads", lambda *a, **k: pytest.fail("parsed oversized record"))
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_journal(b"x" * 9)


@pytest.mark.parametrize("mode,locks,launch,outcome", [
    ("relaunch", {"instance": 9}, "c" * 64, "updated"),
    ("self-check", {"transition": 9}, None, None),
    ("recover-data", {"transition": 9, "launcher": 10, "instance": 11}, None, None),
    ("postterminal-relaunch", {}, None, None),
])
def test_boot_descriptor_allowlists_are_specific_to_each_internal_mode(mode, locks, launch, outcome):
    boot = {"schema_version": 1, "attempt_id": "a" * 64, "nonce": "b" * 64, "kind": "BOOT",
        "mode": mode, "plan_sha256": "d" * 64, "launch_id": launch, "outcome": outcome, "lock_fds": locks}
    assert protocol.decode_control(protocol.encode_control(boot)) == boot
    for extra in ("transition", "launcher", "instance", "unrelated"):
        if extra not in locks:
            with pytest.raises(protocol.ProtocolError):
                protocol.encode_control({**boot, "lock_fds": {**locks, extra: 12}})
    for key in locks:
        with pytest.raises(protocol.ProtocolError):
            protocol.encode_control({**boot, "lock_fds": {name: fd for name, fd in locks.items() if name != key}})


def test_control_channel_rejects_oversized_unterminated_input_before_json(monkeypatch):
    import socket
    import time
    left, right = socket.socketpair()
    channel = protocol.ControlChannel(right)
    monkeypatch.setattr(protocol, "CONTROL_BYTE_LIMIT", 32)
    try:
        left.sendall(b"x" * 33)
        with pytest.raises(protocol.ProtocolError):
            channel.receive(time.monotonic() + 1)
    finally:
        left.close()
        channel.close()


def test_guard_result_binds_the_exact_bounded_private_log():
    from tests.update_protocol_factory import identity
    log = {"identity": identity(size=3), "sha256": "a" * 64}
    value = {"schema_version": 1, "attempt_id": "a" * 64, "nonce": "b" * 64, "kind": "GUARD_RESULT",
        "returncode": 0, "process_group_id": 12, "processes_dead": True, "timed_out": False, "log": log}
    assert protocol.decode_control(protocol.encode_control(value)) == value
    value["log"]["identity"]["size"] = protocol.PRIVATE_LOG_BYTE_LIMIT + 1
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_control(value)
