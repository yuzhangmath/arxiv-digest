from __future__ import annotations

import os
from pathlib import Path

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
        atomic.open_private_lock_file(path)
    assert not path.exists()
