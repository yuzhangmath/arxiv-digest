from __future__ import annotations

import os
import math
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def test_closing_a_borrowed_duplicate_does_not_unlock_the_owner(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive, adopt_borrowed

    owner = acquire_exclusive(tmp_path / "transition.lock", timeout=0.1)
    borrowed = adopt_borrowed(
        os.dup(owner.fileno()),
        expected_identity=owner.identity,
        mode=owner.mode,
    )
    borrowed.close()

    with pytest.raises(TimeoutError):
        acquire_exclusive(tmp_path / "transition.lock", timeout=0.02)
    owner.release()


def test_transfer_close_only_permanently_invalidates_owned_release(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    owner = acquire_exclusive(path, timeout=0.1)

    borrowed = owner.transfer_close_only()
    owner.release()

    with pytest.raises(ValueError, match="closed"):
        owner.fileno()
    with pytest.raises(TimeoutError):
        acquire_exclusive(path, timeout=0.02)

    borrowed.close()
    replacement = acquire_exclusive(path, timeout=0.1)
    replacement.release()


def test_shared_locks_coexist_and_exclude_an_exclusive_lock(tmp_path: Path) -> None:
    from arxiv_digest.update_locks import acquire_exclusive, acquire_shared

    path = tmp_path / "transition.lock"
    first = acquire_shared(path, timeout=0.1)
    second = acquire_shared(path, timeout=0.1)

    with pytest.raises(TimeoutError):
        acquire_exclusive(path, timeout=0.02)

    first.release()
    second.release()
    exclusive = acquire_exclusive(path, timeout=0.1)
    exclusive.release()


def test_waiting_for_a_lock_can_be_canceled(tmp_path: Path) -> None:
    from arxiv_digest.update_locks import (
        LockCancelledError,
        acquire_exclusive,
    )

    path = tmp_path / "transition.lock"
    owner = acquire_exclusive(path, timeout=0.1)
    try:
        with pytest.raises(LockCancelledError):
            acquire_exclusive(path, timeout=1.0, cancelled=lambda: True)
    finally:
        owner.release()


def test_lock_file_with_an_additional_hard_link_is_rejected(tmp_path: Path) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    path.write_bytes(b"")
    path.chmod(0o600)
    os.link(path, tmp_path / "alias.lock")

    with pytest.raises(PermissionError, match="private"):
        acquire_exclusive(path, timeout=0.1)


def test_invalid_borrowed_descriptor_is_closed_on_rejection(tmp_path: Path) -> None:
    from arxiv_digest.update_locks import (
        LockIdentity,
        LockMode,
        adopt_borrowed,
    )

    path = tmp_path / "borrowed.lock"
    path.write_bytes(b"")
    path.chmod(0o600)
    os.link(path, tmp_path / "borrowed-alias.lock")
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    metadata = os.fstat(descriptor)
    identity = LockIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        0o600,
    )

    with pytest.raises(PermissionError, match="private"):
        adopt_borrowed(
            descriptor,
            expected_identity=identity,
            mode=LockMode.EXCLUSIVE,
        )
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_lock_path_substitution_during_acquisition_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as update_locks

    path = tmp_path / "transition.lock"
    original_flock = update_locks.fcntl.flock
    swapped = False

    def substitute(descriptor: int, operation: int) -> None:
        nonlocal swapped
        if operation & update_locks.fcntl.LOCK_NB and not swapped:
            swapped = True
            path.rename(tmp_path / "original.lock")
            path.write_bytes(b"")
            path.chmod(0o600)
        original_flock(descriptor, operation)

    monkeypatch.setattr(update_locks.fcntl, "flock", substitute)

    with pytest.raises(PermissionError, match="identity"):
        update_locks.acquire_exclusive(path, timeout=0.1)


def test_interrupted_nonblocking_lock_attempt_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as update_locks

    original_flock = update_locks.fcntl.flock
    attempts = 0

    def interrupt_once(descriptor: int, operation: int) -> None:
        nonlocal attempts
        if operation & update_locks.fcntl.LOCK_NB and attempts == 0:
            attempts += 1
            raise InterruptedError
        original_flock(descriptor, operation)

    monkeypatch.setattr(update_locks.fcntl, "flock", interrupt_once)

    owner = update_locks.acquire_exclusive(
        tmp_path / "transition.lock",
        timeout=0.1,
    )
    owner.release()
    assert attempts == 1


def test_atomic_rename_noreplace_preserves_an_existing_destination(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import atomic_rename_noreplace

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        atomic_rename_noreplace(source, destination)

    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"existing"


def test_exec_inherited_borrowed_fd_retains_the_lock_after_parent_close(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    owner = acquire_exclusive(path, timeout=0.1)
    borrowed = owner.transfer_close_only()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "os.fstat(int(sys.argv[1])); "
                "print('READY', flush=True); "
                "sys.stdin.buffer.read(1)"
            ),
            str(borrowed.fileno()),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(borrowed.fileno(),),
        text=False,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"READY\n"
        borrowed.close()
        with pytest.raises(TimeoutError):
            acquire_exclusive(path, timeout=0.02)
        assert child.stdin is not None
        child.stdin.write(b"x")
        child.stdin.flush()
        assert child.wait(timeout=2) == 0
        replacement = acquire_exclusive(path, timeout=0.1)
        replacement.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_negative_lock_timeout_is_rejected_before_creating_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    with pytest.raises(ValueError, match="nonnegative"):
        acquire_exclusive(path, timeout=-0.1)
    assert not path.exists()


def test_lock_acquisition_refuses_to_repair_an_unsafe_parent(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)

    with pytest.raises(PermissionError, match="0700"):
        acquire_exclusive(parent / "transition.lock", timeout=0.1)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf])
def test_nonfinite_timeout_is_rejected_before_creating_state(
    tmp_path: Path, timeout: float,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    with pytest.raises(ValueError, match="finite"):
        acquire_exclusive(path, timeout=timeout)
    assert not path.exists()


def test_repeated_eintr_obeys_the_absolute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as locks

    now = 0.0
    attempts = 0
    original_flock = locks.fcntl.flock

    def interrupt(descriptor: int, operation: int) -> None:
        nonlocal now, attempts
        attempts += 1
        now += 0.02
        if attempts <= 10:
            raise InterruptedError
        original_flock(descriptor, operation)

    monkeypatch.setattr(locks.time, "monotonic", lambda: now)
    monkeypatch.setattr(locks.fcntl, "flock", interrupt)
    with pytest.raises(locks.LockTimeoutError):
        locks.acquire_exclusive(tmp_path / "transition.lock", timeout=0.03)
    assert attempts == 2


def test_path_substitution_before_flock_is_rejected_without_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as locks

    path = tmp_path / "transition.lock"
    original_open = os.open
    calls = []

    def substitute(target, flags, *args, **kwargs):
        descriptor = original_open(target, flags, *args, **kwargs)
        if Path(target) == path:
            path.rename(tmp_path / "original.lock")
            os.close(original_open(path, os.O_CREAT | os.O_RDWR, 0o600))
        return descriptor

    monkeypatch.setattr(locks.os, "open", substitute)
    monkeypatch.setattr(locks.fcntl, "flock", lambda *args: calls.append(args))
    with pytest.raises(PermissionError, match="identity"):
        locks.acquire_exclusive(path, timeout=0.1)
    assert calls == []


@pytest.mark.parametrize("mode", [0o400, 0o644, 0o660, 0o1600])
def test_lock_rejects_nonprivate_modes_without_repair(
    tmp_path: Path, mode: int,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    path.touch(mode=0o600)
    path.chmod(mode)
    with pytest.raises(PermissionError):
        acquire_exclusive(path, timeout=0.1)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_lock_rejects_symlink_and_foreign_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    target = tmp_path / "target.lock"
    target.touch(mode=0o600)
    path = tmp_path / "transition.lock"
    path.symlink_to(target)
    with pytest.raises(OSError):
        acquire_exclusive(path, timeout=0.1)
    path.unlink()
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: foreign_uid)
    with pytest.raises(PermissionError):
        acquire_exclusive(target, timeout=0.1)


def test_lock_without_required_nofollow_fails_before_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "new" / "transition.lock"
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(OSError, match="unsupported"):
        acquire_exclusive(path, timeout=0.1)
    assert not path.parent.exists()


def test_owned_descriptor_is_noninheritable_and_unrelated_exec_cannot_retain_it(
    tmp_path: Path,
) -> None:
    from arxiv_digest.update_locks import acquire_exclusive

    path = tmp_path / "transition.lock"
    owner = acquire_exclusive(path, timeout=0.1)
    assert not os.get_inheritable(owner.fileno())
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('READY', flush=True); sys.stdin.read(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=False,
    )
    try:
        assert child.stdout.readline() == b"READY\n"
        owner.transfer_close_only().close()
        replacement = acquire_exclusive(path, timeout=0.1)
        replacement.release()
        assert child.poll() is None
        assert child.communicate(b"x", timeout=2) == (b"", b"")
    finally:
        owner.release()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=2)


@pytest.mark.parametrize("invalid", ["identity", "mode", "inheritable", "directory"])
def test_invalid_borrowed_adoption_closes_fd_without_probing_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    import arxiv_digest.update_locks as locks

    path = tmp_path / "transition.lock"
    owner = locks.acquire_exclusive(path, timeout=0.1)
    descriptor = os.dup(owner.fileno())
    identity = owner.identity
    if invalid == "identity":
        identity = locks.LockIdentity(identity.device, identity.inode + 1, identity.uid, identity.mode)
    elif invalid == "mode":
        os.fchmod(descriptor, 0o644)
    elif invalid == "inheritable":
        os.set_inheritable(descriptor, True)
    else:
        os.close(descriptor)
        descriptor = os.open(tmp_path, os.O_RDONLY)
    calls = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(locks.fcntl, "flock", lambda *args: calls.append(args))
            with pytest.raises(PermissionError):
                locks.adopt_borrowed(descriptor, expected_identity=identity, mode=owner.mode)
        assert calls == []
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        owner.release()


def test_transfer_and_borrowed_cleanup_never_emit_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as locks

    owner = locks.acquire_exclusive(tmp_path / "transition.lock", timeout=0.1)
    original_fd = owner.fileno()
    calls = []
    monkeypatch.setattr(locks.fcntl, "flock", lambda *args: calls.append(args))
    borrowed = owner.transfer_close_only()
    assert borrowed.fileno() == original_fd
    owner.release()
    borrowed.close()
    borrowed.close()
    owner.release()
    assert calls == []
    with pytest.raises(OSError):
        os.fstat(original_fd)


@pytest.mark.parametrize("failure", ["timeout", "cancelled", "path_changed"])
def test_failed_lock_acquisition_closes_every_opened_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import arxiv_digest.update_locks as locks

    path = tmp_path / "transition.lock"
    original_open, original_flock = os.open, locks.fcntl.flock
    opened = []
    attempted = False

    def track_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_flock(descriptor, operation):
        nonlocal attempted
        attempted = True
        if failure == "path_changed":
            original_flock(descriptor, operation)
            path.chmod(0o644)
        else:
            raise BlockingIOError

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(locks.fcntl, "flock", fail_flock)
    expected = {
        "timeout": locks.LockTimeoutError,
        "cancelled": locks.LockCancelledError,
        "path_changed": PermissionError,
    }[failure]
    with pytest.raises(expected):
        locks.acquire_exclusive(
            path, timeout=0.01,
            cancelled=lambda: attempted and failure == "cancelled",
        )
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_zero_timeout_allows_one_immediate_attempt(tmp_path: Path) -> None:
    from arxiv_digest.update_locks import acquire_exclusive, LockTimeoutError

    path = tmp_path / "transition.lock"
    owner = acquire_exclusive(path, timeout=0)
    try:
        with pytest.raises(LockTimeoutError):
            acquire_exclusive(path, timeout=0)
    finally:
        owner.release()


def test_time_spent_validating_identity_cannot_start_a_late_flock_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_locks as locks

    now = 0.0
    original_validate = locks._validate_lock_path
    attempts = []

    def slow_validation(*args):
        nonlocal now
        original_validate(*args)
        now += 0.02

    monkeypatch.setattr(locks.time, "monotonic", lambda: now)
    monkeypatch.setattr(locks, "_validate_lock_path", slow_validation)
    monkeypatch.setattr(locks.fcntl, "flock", lambda *args: attempts.append(args))
    with pytest.raises(locks.LockTimeoutError):
        locks.acquire_exclusive(tmp_path / "transition.lock", timeout=0.01)
    assert attempts == []
