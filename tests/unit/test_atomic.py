from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import arxiv_digest.atomic as atomic



@pytest.mark.parametrize("kind", ["directory", "lock"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow descriptor checks")
def test_strict_private_path_rejects_substitution_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    path = tmp_path / "private"
    if kind == "directory":
        path.mkdir(mode=0o700)
        ensure = atomic.ensure_private_directory_strict
    else:
        path.touch(mode=0o600)
        ensure = atomic.open_private_lock_file
    original_open = os.open
    opened = []

    def substitute(target, flags, *args, **kwargs):
        descriptor = original_open(target, flags, *args, **kwargs)
        if Path(target) == path:
            opened.append(descriptor)
            path.rename(tmp_path / "original")
            if kind == "directory":
                path.mkdir(mode=0o700)
            else:
                os.close(original_open(path, os.O_CREAT | os.O_RDWR, 0o600))
        return descriptor

    monkeypatch.setattr(atomic.os, "open", substitute)
    with pytest.raises(PermissionError, match="identity"):
        ensure(path)
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)



@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX open flags")
def test_strict_directory_requires_supported_open_flags_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    path = tmp_path / "private"
    monkeypatch.delattr(os, flag)
    with pytest.raises(OSError, match="unsupported"):
        atomic.ensure_private_directory_strict(path)
    assert not path.exists()



@pytest.mark.skipif(os.name == "nt", reason="POSIX open flags")
def test_private_lock_requires_nofollow_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private.lock"
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(OSError, match="unsupported"):
        atomic.open_private_lock_file(path)
    assert not path.exists()


def test_private_file_permissions_and_read_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        atomic.set_private_file_permissions(descriptor)
        atomic.validate_private_file(descriptor)
        os.write(descriptor, b"private data")
    finally:
        os.close(descriptor)
    descriptor = atomic.open_private_read_file(path)
    try:
        assert not os.get_inheritable(descriptor)
        assert os.read(descriptor, 100) == b"private data"
    finally:
        os.close(descriptor)


def test_atomic_write_replaces_payload_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "private" / "state.json"
    atomic.atomic_write(path, b"first")
    atomic.atomic_write(path, b"second")
    assert path.read_bytes() == b"second"
    assert list(path.parent.iterdir()) == [path]
    descriptor = atomic.open_private_read_file(path)
    os.close(descriptor)


def test_atomic_write_permission_failure_closes_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = []

    def fail(descriptor: int, mode: int) -> None:
        opened.append(descriptor)
        raise PermissionError("private permissions unavailable")

    monkeypatch.setattr(atomic, "set_private_file_permissions", fail)
    with pytest.raises(PermissionError, match="unavailable"):
        atomic.atomic_write(tmp_path / "private.json", b"private data")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_private_lock_rejects_multiple_hard_links(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    atomic.ensure_private_directory(directory)
    path = directory / "process.lock"
    descriptor = atomic.open_private_lock_file(path)
    os.close(descriptor)
    os.link(path, directory / "other.lock")
    with pytest.raises(PermissionError):
        atomic.open_private_lock_file(path)


def test_process_lock_times_out_and_becomes_available_after_release(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    atomic.ensure_private_directory(directory)
    path = directory / "process.lock"
    code = """
import sys
from pathlib import Path
from arxiv_digest.atomic import acquire_exclusive
try:
    lock = acquire_exclusive(Path(sys.argv[1]), timeout=0.05)
except TimeoutError:
    print("busy")
else:
    lock.release()
    print("acquired")
"""

    def attempt() -> str:
        return subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True,
            text=True, check=True, timeout=10,
        ).stdout.strip()

    lock = atomic.acquire_exclusive(path)
    try:
        assert attempt() == "busy"
    finally:
        lock.release()
    lock.release()
    assert attempt() == "acquired"


def test_process_lock_is_released_when_owner_exits(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    atomic.ensure_private_directory(directory)
    path = directory / "process.lock"
    code = """
import sys
from pathlib import Path
from arxiv_digest.atomic import acquire_exclusive
lock = acquire_exclusive(Path(sys.argv[1]))
print("acquired", flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "acquired"
        with pytest.raises(TimeoutError):
            atomic.acquire_exclusive(path)
    finally:
        process.terminate()
        process.communicate(timeout=10)
    atomic.acquire_exclusive(path, timeout=1).release()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_python_created_directory_is_made_private(tmp_path: Path) -> None:
    import win32api
    import win32con
    import win32security

    path = tmp_path / "python-created"
    path.mkdir()
    atomic.ensure_private_directory(path)
    atomic.ensure_private_directory_strict(path)
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    security = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
    assert security.GetSecurityDescriptorOwner() == user


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")
def test_windows_strict_directory_rejects_public_acl_without_repair(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    path = tmp_path / "private"
    atomic.ensure_private_directory(path)
    information = win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetFileSecurity(str(path), information)
    dacl = descriptor.GetSecurityDescriptorDacl()
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    win32security.SetFileSecurity(str(path), information, descriptor)

    with pytest.raises(PermissionError, match="private"):
        atomic.ensure_private_directory_strict(path)
    unchanged = win32security.GetFileSecurity(str(path), information).GetSecurityDescriptorDacl()
    assert unchanged.GetAceCount() == dacl.GetAceCount()
    atomic.ensure_private_directory(path)
    atomic.ensure_private_directory_strict(path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows sharing semantics")
def test_windows_lock_prevents_path_replacement(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    atomic.ensure_private_directory(directory)
    path = directory / "process.lock"
    lock = atomic.acquire_exclusive(path)
    try:
        with pytest.raises(OSError):
            path.rename(directory / "moved.lock")
    finally:
        lock.release()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junctions")
def test_windows_private_directory_rejects_junction(tmp_path: Path) -> None:
    import _winapi

    target = tmp_path / "target"
    atomic.ensure_private_directory(target)
    link = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(link))
    try:
        for ensure in (atomic.ensure_private_directory, atomic.ensure_private_directory_strict):
            with pytest.raises(PermissionError, match="reparse"):
                ensure(link)
    finally:
        link.rmdir()
