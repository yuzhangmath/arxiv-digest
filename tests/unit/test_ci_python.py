from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from scripts import ci_python


def test_hosted_toolchain_normalizes_only_selected_executable_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "toolcache"
    base = cache / "Python/3.11/x64/bin/python3.11"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"synthetic interpreter")
    base.chmod(0o777)
    base.parent.chmod(0o777)
    sibling = base.with_name("other-tool")
    sibling.write_bytes(b"preserve unrelated tool")
    sibling.chmod(0o777)
    calls = []

    def chmod(command, *, check):
        assert command[:3] == ("sudo", "chmod", "go-w")
        assert check is True
        path = Path(command[3])
        calls.append(path)
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)

    monkeypatch.setattr(ci_python.subprocess, "run", chmod)
    assert ci_python.secure_interpreter(base, cache, platform="linux") == base
    assert set(calls) == {base, base.parent}
    assert stat.S_IMODE(base.stat().st_mode) == 0o755
    assert stat.S_IMODE(base.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(sibling.stat().st_mode) == 0o777


@pytest.mark.parametrize("unsafe", ["outside-cache", "hardlink", "special-mode", "ownership", "file-cache"])
def test_hosted_toolchain_refuses_unsafe_identity_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str,
) -> None:
    cache = tmp_path / "toolcache"
    cache.mkdir()
    base = (tmp_path if unsafe == "outside-cache" else cache) / "python"
    base.write_bytes(b"synthetic interpreter")
    base.chmod(0o777)
    if unsafe == "hardlink":
        os.link(base, base.with_name("linked-python"))
    if unsafe == "special-mode":
        base.chmod(0o1755)
    if unsafe == "ownership":
        original_lstat = Path.lstat

        def wrong_owner(path):
            info = original_lstat(path)
            if path == base:
                fields = list(info)
                fields[4] = 123456
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    if unsafe == "file-cache":
        cache = base
    monkeypatch.setattr(ci_python.subprocess, "run", lambda *a, **k: pytest.fail("unsafe identity was mutated"))
    with pytest.raises(ci_python.CiPythonError):
        ci_python.secure_interpreter(base, cache, platform="linux")


def test_ci_entry_requires_ephemeral_github_host_before_toolchain_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    monkeypatch.setattr(ci_python, "secure_interpreter", lambda *a, **k: pytest.fail("touched local interpreter"))
    with pytest.raises(ci_python.CiPythonError, match="GitHub-hosted"):
        ci_python.prepare()


def test_private_validation_venv_preserves_original_base_in_nested_venv(
    tmp_path: Path,
) -> None:
    base = Path(sys._base_executable).resolve()
    python = ci_python.create_validation_venv(base, tmp_path / "validation-python")
    assert python.is_file() and not python.is_symlink()
    assert stat.S_IMODE(python.parent.parent.stat().st_mode) == 0o700
