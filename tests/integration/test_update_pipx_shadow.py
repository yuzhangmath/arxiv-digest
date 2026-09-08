from __future__ import annotations

import hashlib
import os
import sys
import time
from importlib import metadata
from pathlib import Path

from tests.update_installation_factory import synthetic_pipx_installation


def _inventory(root: Path) -> tuple:
    values = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            values.append((relative, "link", os.readlink(path)))
        elif path.is_file():
            values.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            values.append((relative, "directory"))
    return tuple(values)


def test_real_pipx_1_16_7_lists_only_shadow_and_preserves_live_trash(tmp_path: Path) -> None:
    from arxiv_digest.update_pipx import probe_pipx, run_bounded_command

    assert metadata.version("pipx") == "1.16.7"
    pipx_executable = Path(sys.executable).with_name("pipx")
    assert pipx_executable.is_file()
    installation = synthetic_pipx_installation(tmp_path / "synthetic")
    trash = installation.pipx_home / ".trash"
    trash.mkdir()
    (trash / "sentinel").write_bytes(b"preserve synthetic live trash")
    before = _inventory(installation.root)
    shadows = []
    audited = []

    def command(request):
        shadows.append(request.cwd)
        result = run_bounded_command(request)
        audited.append(_inventory(request.cwd))
        assert _inventory(installation.root) == before
        return result

    result = probe_pipx(
        pipx_executable=pipx_executable,
        venv=installation.venv, pipx_home=installation.pipx_home,
        exposed_command=installation.exposed_command,
        environ=installation.environ, deadline_at=time.monotonic() + 10,
        run_command=command,
    )

    assert result.metadata.main_package.package_version == "0.3.0"
    assert len(shadows) == 4
    assert all(not shadow.exists() for shadow in shadows)
    assert _inventory(installation.root) == before
    final = {entry[0]: entry[1] for entry in audited[-1]}
    assert final["pipx/venvs/arxiv-digest"] == "link"
    assert final["pipx/venvs/.arxiv-digest.lock"] == "file"
    assert final["pipx/.cache/CACHEDIR.TAG"] == "file"
    assert final["pipx/py/CACHEDIR.TAG"] == "file"
    assert any(path.startswith("pipx/logs/cmd_") for path in final)


def test_detector_accepts_synthetic_installation_with_real_pipx_probe(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import detect_pipx_installation

    installation = synthetic_pipx_installation(tmp_path / "synthetic")
    installation.environ["PATH"] = str(Path(sys.executable).parent)
    before = _inventory(installation.root)
    result = detect_pipx_installation(
        current_version="0.3.0",
        running_command=installation.exposed_command,
        running_interpreter=installation.interpreter,
        running_module=installation.module_path,
        paths=installation.paths,
        maintenance=installation.maintenance,
        environ=installation.environ,
        inspect_backup_source=installation.inspect_backup_source,
    )
    assert result.reason is None
    assert result.installation is not None
    assert result.installation.source_kind == "canonical_tag"
    assert _inventory(installation.root) == before
