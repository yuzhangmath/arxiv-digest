"""Synthetic installed layouts; never invoke an installed user application."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.paths import AppPaths, resolve_paths


@dataclass
class SyntheticPipxInstallation:
    root: Path
    pipx_home: Path
    venv: Path
    exposed_command: Path
    entry_point: Path
    interpreter: Path
    base_interpreter: Path
    pipx_executable: Path
    distribution: Path
    module_path: Path
    paths: AppPaths
    maintenance: MaintenanceBarrier
    environ: dict[str, str]
    requests: list

    def run(self, request):
        from arxiv_digest.update_pipx import CommandResult

        self.requests.append(request)
        args = request.argv[1:]
        if args == ("--version",):
            output = b"1.16.7\n"
        elif args == ("list", "--help"):
            output = b"usage: pipx list --output --skip-maintenance\n"
        elif args == ("install", "--help"):
            output = b"usage: pipx install --force --app --python --fetch-python --skip-maintenance --backend --pip-args\n"
        elif args == ("list", "--skip-maintenance", "--output", "json", "arxiv-digest"):
            output = json.dumps({
                "pipx_spec_version": "0.1",
                "venvs": {"arxiv-digest": {
                    "metadata": json.loads((self.venv / "pipx_metadata.json").read_bytes()),
                }},
            }).encode()
        else:
            raise AssertionError(f"unexpected probe argv: {args}")
        return CommandResult(0, output, b"")

    def inspect_backup_source(self, paths, *, maintenance, **kwargs):
        from arxiv_digest.backup import PortableBackupSourceInspection

        assert paths == self.paths
        assert maintenance is self.maintenance
        return PortableBackupSourceInspection(2, 4, 2, 1)


def synthetic_pipx_installation(root: Path) -> SyntheticPipxInstallation:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = root / "home"
    pipx_home = home / ".local" / "share" / "pipx"
    venv = pipx_home / "venvs" / "arxiv-digest"
    python_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    site = venv / "lib" / f"python{python_minor}" / "site-packages"
    distribution = site / "arxiv_digest-0.3.0.dist-info"
    base_interpreter = root / "python" / "bin" / "python3"
    pipx_executable = home / ".local" / "bin" / "pipx"
    exposed_command = pipx_executable.with_name("arxiv-digest")
    entry_point = venv / "bin" / "arxiv-digest"
    interpreter = venv / "bin" / "python"
    module_path = site / "arxiv_digest" / "__init__.py"
    for path in (
        distribution, module_path.parent, base_interpreter.parent,
        pipx_executable.parent, entry_point.parent,
    ):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (home, pipx_home, venv.parent, venv, site, site.parent, site.parent.parent):
        path.chmod(0o700)
    for executable in (base_interpreter, pipx_executable):
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
    interpreter.symlink_to(base_interpreter)
    entry_point.write_text(
        f"#!{interpreter}\nfrom arxiv_digest.cli import main\n"
        "if __name__ == '__main__':\n    raise SystemExit(main())\n"
    )
    entry_point.chmod(0o700)
    exposed_command.symlink_to(entry_point)
    module_path.write_text('__version__ = "0.3.0"\n')
    (venv / "pyvenv.cfg").write_text(
        f"home = {base_interpreter.parent}\ninclude-system-site-packages = false\n"
        f"version = {sys.version.split()[0]}\nexecutable = {base_interpreter}\n"
    )
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: arxiv-digest\nVersion: 0.3.0\n"
        "Requires-Python: >=3.11\nRequires-Dist: beautifulsoup4<5,>=4.12\n"
        "Requires-Dist: packaging<27,>=24\n\n"
    )
    (distribution / "entry_points.txt").write_text(
        "[console_scripts]\narxiv-digest = arxiv_digest.cli:main\n"
    )
    (distribution / "direct_url.json").write_text(json.dumps({
        "url": "https://github.com/yuzhangmath/arxiv-digest.git",
        "vcs_info": {"vcs": "git", "requested_revision": "v0.3.0", "commit_id": "a" * 40},
    }))
    fixture = Path(__file__).parent / "fixtures" / "update" / "pipx-1.16.7-metadata.json"
    metadata = fixture.read_text().replace(
        "/synthetic/pipx/venvs/arxiv-digest", str(venv)
    ).replace("/synthetic/python/bin/python3", str(base_interpreter))
    raw = json.loads(metadata)
    raw["python_version"] = f"Python {sys.version.split()[0]}"
    (venv / "pipx_metadata.json").write_text(json.dumps(raw))
    paths = resolve_paths(home=home, environ={
        "ARXIV_DIGEST_TESTING": "1", "ARXIV_DIGEST_TEST_ROOT": str(root / "app"),
    })
    return SyntheticPipxInstallation(
        root, pipx_home, venv, exposed_command, entry_point, interpreter,
        base_interpreter, pipx_executable, distribution, module_path,
        paths, MaintenanceBarrier(),
        {"HOME": str(home), "PATH": str(pipx_executable.parent),
         "PIPX_HOME": str(pipx_home), "PIPX_BIN_DIR": str(exposed_command.parent)},
        [],
    )


def eligible_installation(
    root: Path, *, version: str = "0.3.0",
    requirements: tuple[str, ...] = ("beautifulsoup4<5,>=4.12", "packaging<27,>=24"),
    requires_python: str = ">=3.11",
):
    """Construct private proof data for pure release-policy tests, without probing."""
    from arxiv_digest.backup import PortableBackupSourceInspection
    from arxiv_digest.update_installation import DistributionIdentity, PipxInstallation
    from arxiv_digest.update_manifest import runtime_requirements_sha256

    fixture = synthetic_pipx_installation(root)
    distribution = DistributionIdentity(
        "arxiv-digest", version, fixture.distribution, requirements,
        requires_python, fixture.module_path, "a" * 64, "b" * 64,
    )
    return PipxInstallation(
        version, "canonical_tag", "linux", (sys.version_info.major, sys.version_info.minor),
        fixture.pipx_executable, fixture.pipx_home, fixture.venv, fixture.exposed_command,
        fixture.entry_point, fixture.interpreter, fixture.base_interpreter,
        fixture.pipx_home / "arxiv-digest-update-snapshots", distribution,
        runtime_requirements_sha256(requirements), "a" * 40, (), "c" * 64,
        PortableBackupSourceInspection(2, 4, 2, 1),
        Path(fixture.environ["HOME"]), fixture.pipx_home / "shared",
        Path(fixture.environ["HOME"]) / ".local/share/man",
        Path(fixture.environ["HOME"]) / ".local/share/bash-completion/completions",
    )
