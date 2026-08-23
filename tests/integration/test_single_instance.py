from __future__ import annotations

import stat

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
