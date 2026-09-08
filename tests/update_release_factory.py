"""Real canonical-source pipx installs with offline, actual application bytes.

Only fixture packaging, transport and browser seams differ from production.
Git and pip generate the source authorization metadata themselves. No installed
PEP 610 or pipx metadata is rewritten to grant eligibility.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from arxiv_digest.update_pipx import CommandRequest, run_bounded_command
from tests.update_wheel_factory import record_digest, regular_zip_info


PROJECT = Path(__file__).resolve().parents[1]
CANONICAL_GIT = "https://github.com/yuzhangmath/arxiv-digest.git"


def _application_source(version, *, source_ref=None, source_overrides=None):
    if source_ref is None:
        package = PROJECT / "src/arxiv_digest"
        files = {path.relative_to(PROJECT).as_posix(): path.read_bytes()
                 for path in sorted(package.rglob("*")) if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
        project = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]
    else:
        # Archive the real prior release source without checking out or changing
        # the shared working tree, its index, branches or tags.
        result = subprocess.run(("git", "archive", "--format=tar", source_ref, "src/arxiv_digest", "pyproject.toml"),
                                cwd=PROJECT, check=True, capture_output=True)
        files = {}
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            for item in archive:
                if item.isfile():
                    files[item.name] = archive.extractfile(item).read()
        project = tomllib.loads(files.pop("pyproject.toml").decode())["project"]
    files["src/arxiv_digest/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    for name, value in (source_overrides or {}).items():
        name = name if name.startswith("src/arxiv_digest/") else "src/arxiv_digest/" + name
        if Path(name).as_posix() != name or ".." in Path(name).parts or name not in files:
            raise ValueError("fixture override must select an existing application file")
        files[name] = value.encode() if isinstance(value, str) else value
    return files, {"version": version, "requires_python": project["requires-python"], "requires_dist": project["dependencies"]}


def _write_archive(path, members, dist_info):
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for name, value in sorted(members.items()):
        writer.writerow((name, record_digest(value), len(value)))
    writer.writerow((dist_info + "/RECORD", "", ""))
    members = {**members, dist_info + "/RECORD": rows.getvalue().encode()}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(members.items()):
            archive.writestr(regular_zip_info(name), value)
    return path


def application_wheel(destination, version, *, source_ref=None, source_overrides=None):
    files, build = _application_source(version, source_ref=source_ref, source_overrides=source_overrides)
    dist_info = f"arxiv_digest-{version}.dist-info"
    members = {name.removeprefix("src/"): value for name, value in files.items()}
    members[dist_info + "/METADATA"] = (f"Metadata-Version: 2.4\nName: arxiv-digest\nVersion: {version}\nRequires-Python: {build['requires_python']}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in build["requires_dist"]) + "\n").encode()
    members[dist_info + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: synthetic-release-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    members[dist_info + "/entry_points.txt"] = b"[console_scripts]\narxiv-digest = arxiv_digest.cli:main\n"
    return _write_archive(Path(destination) / f"arxiv_digest-{version}-py3-none-any.whl", members, dist_info)


def installed_dependency_wheel(name, destination):
    distribution = metadata.distribution(name)
    normalized = re.sub(r"[-_.]+", "_", distribution.metadata["Name"])
    dist_info = Path(distribution._path).name
    members = {}
    for entry in distribution.files or ():
        if ".." in entry.parts or "__pycache__" in entry.parts or entry.suffix == ".pyc":
            continue
        if entry.parts[0] == dist_info and entry.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}:
            continue
        path = Path(distribution.locate_file(entry))
        if path.is_file():
            members[entry.as_posix()] = path.read_bytes()
    assert dist_info + "/METADATA" in members
    members[dist_info + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: offline-installed-dependency-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    return _write_archive(Path(destination) / f"{normalized}-{distribution.version}-py3-none-any.whl", members, dist_info)


# This fixture PEP 517 backend uses no build dependency or network. Pip still
# clones the tagged repository, builds a real wheel, and writes real VCS origin
# metadata. The backend packages the same application bytes as application_wheel.
_BACKEND = '''import base64, csv, hashlib, io, json, pathlib, zipfile
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    root = pathlib.Path(__file__).parent
    config = json.loads((root / "_fixture_metadata.json").read_text())
    version = config["version"]
    info = f"arxiv_digest-{version}.dist-info"
    members = {path.relative_to(root / "src").as_posix(): path.read_bytes()
               for path in sorted((root / "src/arxiv_digest").rglob("*"))
               if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    members[info + "/METADATA"] = (f"Metadata-Version: 2.4\\nName: arxiv-digest\\nVersion: {version}\\nRequires-Python: {config['requires_python']}\\n"
        + "".join(f"Requires-Dist: {value}\\n" for value in config["requires_dist"]) + "\\n").encode()
    members[info + "/WHEEL"] = b"Wheel-Version: 1.0\\nGenerator: synthetic-release-fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    members[info + "/entry_points.txt"] = b"[console_scripts]\\narxiv-digest = arxiv_digest.cli:main\\n"
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\\n")
    for name, value in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip("=")
        writer.writerow((name, "sha256=" + digest, len(value)))
    writer.writerow((info + "/RECORD", "", ""))
    members[info + "/RECORD"] = rows.getvalue().encode()
    name = f"arxiv_digest-{version}-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, value in sorted(members.items()):
            item = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
            item.external_attr = 0o100644 << 16
            archive.writestr(item, value)
    return name
'''


@dataclass
class ReleaseInstallation:
    root: Path
    environ: dict[str, str]
    pipx: Path
    source_python: Path
    wheelhouse: Path
    repository: Path

    @property
    def venv(self): return self.root / "pipx/venvs/arxiv-digest"

    @property
    def exposed(self): return self.root / "bin/arxiv-digest"

    def run(self, argv, *, environ=None, timeout=120):
        environment = self.environ if environ is None else environ
        arguments = list(map(str, argv))
        if not Path(arguments[0]).is_absolute():
            arguments[0] = shutil.which(arguments[0], path=environment["PATH"])
            assert arguments[0] is not None
        result = run_bounded_command(CommandRequest(tuple(arguments), environment,
            self.root, time.monotonic() + timeout, 512 * 1024, 512 * 1024))
        assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
        return result

    def add_source_tag(self, version, *, source_ref=None, source_overrides=None):
        files, build = _application_source(version, source_ref=source_ref, source_overrides=source_overrides)
        if not (self.repository / ".git").exists():
            self.run(("git", "init", "-q", str(self.repository)))
        source = self.repository / "src"
        if source.exists(): shutil.rmtree(source)
        for name, payload in files.items():
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (self.repository / "pyproject.toml").write_text('[build-system]\nrequires = []\nbuild-backend = "_fixture_backend"\nbackend-path = ["."]\n')
        (self.repository / "_fixture_backend.py").write_text(_BACKEND)
        (self.repository / "_fixture_metadata.json").write_text(json.dumps(build))
        # Only this explicitly synthetic, temporary repository is staged or
        # committed. The user's checkout and index are never touched.
        self.run(("git", "-C", self.repository, "add", "--all"))
        self.run(("git", "-C", self.repository, "commit", "-q", "-m", "Synthetic release source"))
        self.run(("git", "-C", self.repository, "tag", "-a", f"v{version}", "-m", "Synthetic release tag"))
        return self.run(("git", "-C", self.repository, "rev-parse", "HEAD")).stdout.decode().strip()

    def bootstrap(self, version, *, force=False):
        arguments = [self.pipx, "install"]
        if force: arguments.append("--force")
        arguments.extend(("--python", self.source_python, "--fetch-python=never", "--skip-maintenance", "--backend=pip", f"git+{CANONICAL_GIT}@v{version}"))
        return self.run(arguments)

    def write_wheel(self, version, *, source_overrides=None):
        return application_wheel(self.wheelhouse, version, source_overrides=source_overrides)


def real_release_installation(root):
    assert metadata.version("pipx") == "1.16.7"
    root = Path(root).resolve()
    for name in ("home", "pipx", "bin", "shared", "man", "completions", "wheelhouse", "tmp", "config", "data", "cache", "state", "source"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    wheelhouse = root / "wheelhouse"
    for name in ("beautifulsoup4", "packaging", "soupsieve", "typing_extensions"):
        installed_dependency_wheel(name, wheelhouse)
    environment = {
        "PATH": os.pathsep.join((str(root / "bin"), str(Path(sys.executable).parent), str(Path(sys._base_executable).parent), "/usr/bin", "/bin")),
        "HOME": str(root / "home"), "PIPX_HOME": str(root / "pipx"), "PIPX_BIN_DIR": str(root / "bin"),
        "PIPX_SHARED_LIBS": str(root / "shared"), "PIPX_MAN_DIR": str(root / "man"), "PIPX_COMPLETION_DIR": str(root / "completions"),
        "PIPX_DEFAULT_PYTHON": str(Path(sys._base_executable)), "PIPX_DEFAULT_BACKEND": "pip", "PIPX_FETCH_PYTHON": "never",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1", "PIPX_USE_EMOJI": "0", "PIPX_MAX_LOGS": "20",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheelhouse), "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_COMPILE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Synthetic Fixture", "GIT_AUTHOR_EMAIL": "synthetic-fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Fixture", "GIT_COMMITTER_EMAIL": "synthetic-fixture@example.invalid",
    }
    repository = root / "source"
    (root / "gitconfig").write_text(f'[url "{repository.as_uri()}"]\n\tinsteadOf = {CANONICAL_GIT}\n')
    fixture = ReleaseInstallation(root, environment, Path(sys.executable).with_name("pipx"), Path(sys._base_executable), wheelhouse, repository)
    fixture.run((fixture.source_python, "-m", "venv", root / "shared"))
    return fixture
