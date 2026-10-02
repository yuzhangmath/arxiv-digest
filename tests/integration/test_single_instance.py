from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest



def test_second_claim_verifies_private_descriptor_and_returns_existing(tmp_path) -> None:
    from arxiv_digest.web.lifecycle import ExistingInstance, SingleInstance

    lock_path = tmp_path / "runtime.lock"
    descriptor_path = tmp_path / "runtime.json"
    owner = SingleInstance(lock_path, descriptor_path)
    ownership = owner.acquire()
    ownership.publish(
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="A" * 43,
    )
    probed = []
    contender = SingleInstance(
        lock_path,
        descriptor_path,
        pid_alive=lambda pid: True,
        health_probe=lambda descriptor: probed.append(descriptor) or True,
    )

    try:
        existing = contender.acquire()

        assert isinstance(existing, ExistingInstance)
        assert existing.descriptor.port == 43123
        assert existing.descriptor.startup_nonce == "nonce_abcd12345678"
        assert probed == [existing.descriptor]
        assert stat.S_IMODE(descriptor_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    finally:
        owner.release()



def test_untrusted_lock_or_runtime_metadata_is_rejected(tmp_path) -> None:
    from arxiv_digest.web.lifecycle import InstanceSecurityError, SingleInstance

    target = tmp_path / "elsewhere"
    target.write_text("not a lock", encoding="utf-8")
    lock_path = tmp_path / "runtime.lock"
    lock_path.symlink_to(target)

    with pytest.raises(InstanceSecurityError, match="process lock"):
        SingleInstance(lock_path, tmp_path / "runtime.json").acquire()



def test_contender_rejects_world_readable_runtime_descriptor(tmp_path) -> None:
    from arxiv_digest.web.lifecycle import InstanceSecurityError, SingleInstance

    lock_path = tmp_path / "runtime.lock"
    descriptor_path = tmp_path / "runtime.json"
    owner = SingleInstance(lock_path, descriptor_path)
    owner.acquire().publish(
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="A" * 43,
    )
    descriptor_path.chmod(0o644)

    try:
        with pytest.raises(InstanceSecurityError, match="not private"):
            SingleInstance(
                lock_path,
                descriptor_path,
                pid_alive=lambda pid: True,
                health_probe=lambda descriptor: True,
            ).acquire()
    finally:
        descriptor_path.chmod(0o600)
        owner.release()



def test_owner_rejects_non_base64url_lifetime_tokens(tmp_path) -> None:
    from arxiv_digest.web.lifecycle import SingleInstance

    owner = SingleInstance(
        tmp_path / "runtime.lock", tmp_path / "runtime.json"
    )
    ownership = owner.acquire()
    try:
        with pytest.raises(ValueError, match="descriptor values"):
            ownership.publish(
                port=43123,
                startup_nonce="nonce_abcd12345678",
                token="!" * 43,
            )
    finally:
        owner.release()



def test_contender_rejects_runtime_descriptor_path_substitution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.web.lifecycle as lifecycle
    from arxiv_digest.web.lifecycle import InstanceSecurityError, SingleInstance

    lock_path = tmp_path / "runtime.lock"
    descriptor_path = tmp_path / "runtime.json"
    owner = SingleInstance(lock_path, descriptor_path)
    owner.acquire().publish(
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="A" * 43,
    )
    original_open = lifecycle.os.open
    replaced = False

    def replace_before_open(path, *args, **kwargs):
        nonlocal replaced
        if Path(path) == descriptor_path and not replaced:
            replaced = True
            payload = descriptor_path.read_bytes()
            descriptor_path.rename(tmp_path / "original-runtime.json")
            descriptor_path.write_bytes(payload)
            descriptor_path.chmod(0o600)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.os, "open", replace_before_open)
    try:
        with pytest.raises(InstanceSecurityError, match="changed"):
            SingleInstance(
                lock_path,
                descriptor_path,
                pid_alive=lambda pid: True,
                health_probe=lambda descriptor: True,
            ).acquire()
    finally:
        owner.release()



@pytest.mark.parametrize("change", ["permissions", "content"])
def test_runtime_descriptor_must_remain_private_and_stable_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    import arxiv_digest.web.lifecycle as lifecycle
    from arxiv_digest.web.lifecycle import InstanceSecurityError, SingleInstance

    lock_path = tmp_path / "runtime.lock"
    descriptor_path = tmp_path / "runtime.json"
    owner = SingleInstance(lock_path, descriptor_path)
    owner.acquire().publish(
        port=43123, startup_nonce="nonce_abcd12345678", token="A" * 43,
    )
    original_read = lifecycle.os.read
    changed = False

    def change_after_read(fd, count):
        nonlocal changed
        result = original_read(fd, count)
        if result and not changed:
            changed = True
            if change == "permissions":
                descriptor_path.chmod(0o644)
            else:
                descriptor_path.write_bytes(result.replace(b"43123", b"43124"))
        return result

    monkeypatch.setattr(lifecycle.os, "read", change_after_read)
    try:
        with pytest.raises(InstanceSecurityError, match="changed"):
            SingleInstance(
                lock_path, descriptor_path,
                pid_alive=lambda pid: True, health_probe=lambda descriptor: True,
            ).acquire()
    finally:
        descriptor_path.chmod(0o600)
        owner.release()



@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_release_preserves_a_replaced_runtime_descriptor(
    tmp_path: Path, replacement: str,
) -> None:
    from arxiv_digest.atomic import acquire_exclusive
    from arxiv_digest.web.lifecycle import SingleInstance

    lock_path = tmp_path / "runtime.lock"
    descriptor_path = tmp_path / "runtime.json"
    owner = SingleInstance(lock_path, descriptor_path)
    owner.acquire().publish(
        port=43123, startup_nonce="nonce_abcd12345678", token="A" * 43,
    )
    original_path = tmp_path / "original.json"
    descriptor_path.rename(original_path)
    if replacement == "file":
        descriptor_path.write_bytes(original_path.read_bytes())
        descriptor_path.chmod(0o600)
    else:
        descriptor_path.symlink_to(original_path)

    owner.release()

    assert descriptor_path.exists()
    replacement_owner = acquire_exclusive(lock_path, timeout=0.1)
    replacement_owner.release()



@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_CLOEXEC"])
def test_runtime_descriptor_read_requires_safe_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    import arxiv_digest.web.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.os, flag, 0)
    with pytest.raises(lifecycle.InstanceSecurityError, match="unsupported"):
        lifecycle._read_private_descriptor(tmp_path / "runtime.json")
