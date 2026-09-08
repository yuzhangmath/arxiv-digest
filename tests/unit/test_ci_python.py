from __future__ import annotations

import os
import stat
import sys
import venv
from pathlib import Path

import pytest

from scripts import ci_python


def _synthetic_venv_templates(cache: Path, monkeypatch: pytest.MonkeyPatch, *, fish: str = "common") -> Path:
    module = cache / "Python/3.11/x64/lib/python3.11/venv/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("# Synthetic stdlib location.\n")
    scripts = module.parent / "scripts"
    for folder in ("common", "posix", "nt"):
        (scripts / folder).mkdir(parents=True)
    for relative, mode in (
        ("common/activate", 0o777), ("common/Activate.ps1", 0o777),
        (f"{fish}/activate.fish", 0o666), ("posix/activate.csh", 0o644),
        ("nt/unrelated", 0o777),
    ):
        template = scripts / relative
        template.write_text("# Synthetic activation template for __VENV_DIR__.\n")
        template.chmod(mode)
    monkeypatch.setattr(venv, "__file__", str(module))
    return scripts


def _synthetic_chmod(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls = []

    def chmod(command, *, check):
        assert command[:3] == ("sudo", "chmod", "go-w")
        assert check is True
        path = Path(command[3])
        calls.append(path)
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)

    monkeypatch.setattr(ci_python.subprocess, "run", chmod)
    return calls


@pytest.mark.parametrize("fish", ["common", "posix"])
def test_hosted_activation_templates_produce_environments_accepted_by_strict_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fish: str,
) -> None:
    from arxiv_digest.update_runtime import recovery

    cache = tmp_path / "toolcache"
    base = cache / "Python/3.11/x64/bin/python3.11"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"synthetic interpreter")
    base.chmod(0o755)
    scripts = _synthetic_venv_templates(cache, monkeypatch, fish=fish)
    calls = _synthetic_chmod(monkeypatch)

    def install_scripts(destination):
        destination.mkdir(mode=0o700)
        previous_umask = os.umask(0o022)
        try:
            builder = venv.EnvBuilder(with_pip=False)
            context = builder.ensure_directories(str(destination))
            builder.setup_scripts(context)
        finally:
            os.umask(previous_umask)
        return destination

    before = install_scripts(tmp_path / "before")
    assert stat.S_IMODE((before / "bin/Activate.ps1").stat().st_mode) == 0o777
    with pytest.raises(recovery.SnapshotError, match="unsafe installation object"):
        recovery.scan_environment(before)

    ci_python.secure_interpreter(base, cache, platform="linux")
    after = install_scripts(tmp_path / "after")
    inventory = recovery.scan_environment(after)
    assert {entry["path"] for entry in inventory["entries"] if entry["kind"] == "file"} == {
        "bin/activate", "bin/Activate.ps1", "bin/activate.fish", "bin/activate.csh",
    }
    assert stat.S_IMODE((after / "bin/activate.fish").stat().st_mode) == 0o644
    assert set(calls) == {
        scripts / "common/activate", scripts / "common/Activate.ps1", scripts / f"{fish}/activate.fish",
    }
    assert stat.S_IMODE((scripts / "nt/unrelated").stat().st_mode) == 0o777


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
    scripts = _synthetic_venv_templates(cache, monkeypatch)
    for folder in ("common", "posix"):
        for template in (scripts / folder).iterdir():
            template.chmod(0o644)
    calls = _synthetic_chmod(monkeypatch)
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
    base = (tmp_path if unsafe == "outside-cache" else cache) / "selected-python"
    base.write_bytes(b"synthetic interpreter")
    base.chmod(0o777)
    _synthetic_venv_templates(cache, monkeypatch)
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


@pytest.mark.parametrize("unsafe", [
    "outside-cache", "unknown-file", "nested-directory", "symlink", "directory",
    "hardlink", "special-mode", "ownership", "missing-template",
])
def test_hosted_toolchain_refuses_unsafe_templates_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str,
) -> None:
    cache = tmp_path / "toolcache"
    base = cache / "bin/python"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"synthetic interpreter")
    base.chmod(0o777)
    scripts = _synthetic_venv_templates(
        tmp_path / "outside" if unsafe == "outside-cache" else cache, monkeypatch,
    )
    template = scripts / "posix/activate.csh"
    if unsafe == "unknown-file":
        unexpected = template.with_name("unrelated-tool")
        unexpected.write_bytes(b"unrelated unsafe file")
        unexpected.chmod(0o777)
    if unsafe == "nested-directory":
        template.with_name("unexpected-directory").mkdir()
    if unsafe in {"symlink", "directory", "missing-template"}:
        template.unlink()
        if unsafe == "symlink":
            template.symlink_to(base)
        if unsafe == "directory":
            template.mkdir()
    if unsafe == "hardlink":
        os.link(template, tmp_path / "linked-template")
    if unsafe == "special-mode":
        template.chmod(0o1644)
    if unsafe == "ownership":
        original_lstat = Path.lstat

        def wrong_owner(path):
            info = original_lstat(path)
            if path == template:
                fields = list(info)
                fields[4] = 123456
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    monkeypatch.setattr(ci_python.subprocess, "run", lambda *a, **k: pytest.fail("unsafe toolchain was mutated"))
    with pytest.raises(ci_python.CiPythonError):
        ci_python.secure_interpreter(base, cache, platform="linux")
    assert stat.S_IMODE(base.stat().st_mode) == 0o777


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
