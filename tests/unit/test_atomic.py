from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import arxiv_digest.atomic as atomic


@pytest.mark.parametrize("kind", ["directory", "lock"])
def test_strict_private_path_rejects_substitution_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    path = tmp_path / "private"
    if kind == "directory":
        path.mkdir(mode=0o700)
        ensure = atomic.ensure_private_directory_strict
    else:
        path.touch(mode=0o600)
        ensure = atomic.ensure_private_lock_file
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
def test_strict_directory_requires_supported_open_flags_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    path = tmp_path / "private"
    monkeypatch.delattr(os, flag)
    with pytest.raises(OSError, match="unsupported"):
        atomic.ensure_private_directory_strict(path)
    assert not path.exists()


def test_private_lock_requires_nofollow_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private.lock"
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(OSError, match="unsupported"):
        atomic.ensure_private_lock_file(path)
    assert not path.exists()


@pytest.mark.parametrize("platform,symbol,expected", [
    ("linux", "renameat2", (-100, b"source-\xc3\xa9", -100, b"target-\xc3\xa9", 1)),
    ("darwin", "renamex_np", (b"source-\xc3\xa9", b"target-\xc3\xa9", 4)),
])
def test_noreplace_calls_platform_syscall_with_fsencoded_paths(
    monkeypatch: pytest.MonkeyPatch, platform: str, symbol: str, expected: tuple,
) -> None:
    calls = []

    def rename(*args):
        calls.append(args)
        return 0

    def library(name, *, use_errno):
        assert name is None and use_errno is True
        return SimpleNamespace(**{symbol: rename})

    monkeypatch.setattr(atomic.sys, "platform", platform)
    monkeypatch.setattr(atomic.ctypes, "CDLL", library)
    atomic.atomic_rename_noreplace(Path("source-é"), Path("target-é"))
    assert calls == [expected]
    assert rename.restype == ctypes.c_int


@pytest.mark.parametrize("platform,symbol", [("linux", "renameat2"), ("darwin", "renamex_np")])
@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV, errno.EEXIST])
def test_noreplace_failure_never_falls_back_to_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, symbol: str, error: int,
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    def rename(*args):
        ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(atomic.sys, "platform", platform)
    monkeypatch.setattr(atomic.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(**{symbol: rename}))
    expected = FileExistsError if error == errno.EEXIST else OSError if error == errno.EXDEV else atomic.AtomicRenameUnsupportedError
    with pytest.raises(expected) as caught:
        atomic.atomic_rename_noreplace(source, destination)
    if error == errno.EXDEV:
        assert caught.value.errno == errno.EXDEV
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


@pytest.mark.parametrize("platform", ["linux", "darwin", "unsupported"])
def test_noreplace_unsupported_platform_or_missing_symbol_fails_closed(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    monkeypatch.setattr(atomic.sys, "platform", platform)
    monkeypatch.setattr(atomic.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(atomic.AtomicRenameUnsupportedError):
        atomic.atomic_rename_noreplace(Path("source"), Path("destination"))


def test_noreplace_real_platform_renames_a_new_destination(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    atomic.atomic_rename_noreplace(source, destination)
    assert not source.exists()
    assert destination.read_bytes() == b"payload"


@pytest.mark.parametrize("invalid", ["source", "destination"])
def test_noreplace_rejects_embedded_nul_before_loading_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    calls = []
    original_cdll = atomic.ctypes.CDLL

    def track_cdll(*args, **kwargs):
        calls.append(args)
        return original_cdll(*args, **kwargs)

    monkeypatch.setattr(atomic.ctypes, "CDLL", track_cdll)
    input_source = Path(str(source) + "\x00ignored") if invalid == "source" else source
    input_destination = Path(str(destination) + "\x00ignored") if invalid == "destination" else destination
    with pytest.raises(ValueError, match="null"):
        atomic.atomic_rename_noreplace(input_source, input_destination)
    assert calls == []
    assert source.read_bytes() == b"payload"
    assert not destination.exists()
