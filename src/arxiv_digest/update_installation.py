"""Read-only proof that the running application is the supported pipx install."""

from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from arxiv_digest import __version__
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.paths import AppPaths
from arxiv_digest.update_contract import (
    APPLICATION_DATA_GENERATION, DISCOVERY_DEADLINE_SECONDS,
    METADATA_MEMBER_BYTE_LIMIT, REPOSITORY, UPDATE_SNAPSHOT_DIRNAME,
    canonical_version,
)
from arxiv_digest.update_manifest import runtime_requirements_sha256

if TYPE_CHECKING:
    from arxiv_digest.backup import PortableBackupSourceInspection
    from arxiv_digest.update_pipx import CommandRunner


class InstallationUnavailableReason(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNSUPPORTED_VERSION = "unsupported_version"
    PIPX_NOT_FOUND = "pipx_not_found"
    UNSAFE_EXECUTABLE = "unsafe_executable"
    UNSUPPORTED_LAYOUT = "unsupported_layout"
    COMMAND_MISMATCH = "command_mismatch"
    INTERPRETER_MISMATCH = "interpreter_mismatch"
    DISTRIBUTION_MISMATCH = "distribution_mismatch"
    UNSUPPORTED_SOURCE = "unsupported_source"
    PROTECTED_PROVENANCE_REQUIRED = "protected_provenance_required"
    PIPX_UNAVAILABLE = "pipx_unavailable"
    BACKUP_UNAVAILABLE = "backup_unavailable"
    EXTERNAL_CHANGE_DETECTED = "external_change_detected"
    DEADLINE_EXPIRED = "deadline_expired"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    link_target: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DistributionIdentity:
    name: str
    version: str
    path: Path
    requirements: tuple[str, ...]
    requires_python: str
    module_path: Path
    metadata_sha256: str
    direct_url_sha256: str


@dataclass(frozen=True, slots=True)
class PipxInstallation:
    version: str
    source_kind: Literal["canonical_tag", "updater_provenance"]
    platform: Literal["darwin", "linux"]
    running_python: tuple[int, int]
    pipx_executable: Path
    pipx_home: Path
    venv: Path
    exposed_command: Path
    entry_point: Path
    interpreter: Path
    base_interpreter: Path
    snapshot_root: Path
    distribution: DistributionIdentity
    runtime_requirements_sha256: str
    source_commit: str | None
    identities: tuple[tuple[str, FileIdentity], ...]
    pipx_metadata_sha256: str
    backup_inspection: PortableBackupSourceInspection
    home: Path
    pipx_shared_libs: Path
    pipx_man_dir: Path
    pipx_completion_dir: Path


@dataclass(frozen=True, slots=True)
class InstallationDetection:
    installation: PipxInstallation | None = None
    reason: InstallationUnavailableReason | None = None

    def __post_init__(self) -> None:
        if (self.installation is None) == (self.reason is None):
            raise ValueError("installation detection requires exactly one result")
        if self.installation is not None and not isinstance(self.installation, PipxInstallation):
            raise TypeError("installation must be a validated installation record")
        if self.reason is not None and not isinstance(self.reason, InstallationUnavailableReason):
            raise TypeError("installation reason must be a closed reason code")


class BackupSourceInspector(Protocol):
    def __call__(
        self, paths: AppPaths, *, maintenance: MaintenanceBarrier,
        timeout: float, monotonic: Callable[[], float],
    ) -> PortableBackupSourceInspection: ...


class ProtectedProvenanceValidator(Protocol):
    """Validate the sole protected provenance store and complete live token."""

    def __call__(
        self, *, paths: AppPaths, distribution: DistributionIdentity,
        direct_url: bytes, package_or_url: str,
    ) -> bool: ...


class _Unavailable(Exception):
    def __init__(self, reason: InstallationUnavailableReason) -> None:
        self.reason = reason


def _require(condition: bool, reason: InstallationUnavailableReason) -> None:
    if not condition:
        raise _Unavailable(reason)


def _absolute(raw: str | Path) -> Path:
    text = str(raw)
    path = Path(text)
    _require(
        path.is_absolute() and str(path) == text and "\0" not in text
        and ".." not in path.parts and "\\" not in text,
        InstallationUnavailableReason.UNSUPPORTED_LAYOUT,
    )
    return path


def _identity(metadata: os.stat_result, **values) -> FileIdentity:
    return FileIdentity(
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
        metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
        metadata.st_nlink, **values,
    )


def _capture(path: Path, *, kind: str, allow_root: bool = False) -> FileIdentity:
    before = path.lstat()
    owners = {os.getuid(), 0} if allow_root else {os.getuid()}
    _require(before.st_uid in owners, InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
    if kind == "link":
        _require(stat.S_ISLNK(before.st_mode) and before.st_nlink == 1,
                 InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        target = os.readlink(path)
        _require(_identity(path.lstat()) == _identity(before),
                 InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED)
        return _identity(before, link_target=target)
    _require(not before.st_mode & 0o022, InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
    if kind == "directory":
        _require(stat.S_ISDIR(before.st_mode), InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        return _identity(before)
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
             InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
    if kind == "executable":
        _require(os.access(path, os.X_OK), InstallationUnavailableReason.UNSAFE_EXECUTABLE)
        return _identity(before)
    _, identity = _read_file(path)
    return identity


def _read_file(path: Path, *, limit: int = METADATA_MEMBER_BYTE_LIMIT) -> tuple[bytes, FileIdentity]:
    _require(all(getattr(os, name, 0) for name in ("O_NOFOLLOW", "O_CLOEXEC")),
             InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
    before = path.lstat()
    _require(
        stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
        and before.st_nlink == 1 and not before.st_mode & 0o022
        and 0 <= before.st_size <= limit,
        InstallationUnavailableReason.UNSUPPORTED_LAYOUT,
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _require(_identity(os.fstat(fd)) == _identity(before),
                 InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED)
        payload = bytearray()
        while len(payload) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        _require(len(payload) <= limit, InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        _require(
            _identity(os.fstat(fd)) == _identity(before)
            and _identity(path.lstat()) == _identity(before),
            InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED,
        )
        return bytes(payload), _identity(before, sha256=hashlib.sha256(payload).hexdigest())
    finally:
        os.close(fd)


def _json(payload: bytes) -> object:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata field")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("nonfinite metadata value")

    return json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=nonfinite)


def _pipx_executable(environ: Mapping[str, str], home: Path, platform: str) -> tuple[Path, Path]:
    candidates: list[Path] = []
    for value in environ.get("PATH", "").split(os.pathsep):
        if value and Path(value).is_absolute():
            candidates.append(_absolute(value) / "pipx")
    candidates.extend((home / ".local/bin/pipx", Path("/usr/local/bin/pipx"), Path("/usr/bin/pipx")))
    if platform == "darwin":
        candidates.append(Path("/opt/homebrew/bin/pipx"))
    for candidate in dict.fromkeys(candidates):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        try:
            _capture(candidate.parent, kind="directory", allow_root=True)
            resolved = candidate.resolve(strict=True)
            _capture(resolved, kind="executable", allow_root=True)
        except (OSError, _Unavailable, RuntimeError) as error:
            raise _Unavailable(InstallationUnavailableReason.UNSAFE_EXECUTABLE) from error
        return candidate, resolved
    raise _Unavailable(InstallationUnavailableReason.PIPX_NOT_FOUND)


def _distribution(
    site: Path, version: str, running_module: Path,
    track: Callable[..., FileIdentity],
) -> tuple[DistributionIdentity, bytes]:
    dist = site / f"arxiv_digest-{version}.dist-info"
    track(dist, kind="directory")
    track(site, kind="directory")
    matches = []
    with os.scandir(site) as entries:
        for count, entry in enumerate(entries):
            _require(count < 4096, InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
            normalized = entry.name.lower().replace("-", "_").replace(".", "_")
            if normalized.startswith("arxiv_digest") and entry.name.endswith((".dist-info", ".egg-info", ".egg-link")):
                matches.append(entry.name)
    _require(matches == [dist.name], InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    module = site / "arxiv_digest" / "__init__.py"
    _require(running_module == module and module.resolve(strict=True) == module,
             InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    track(module.parent, kind="directory")
    track(module, kind="file")
    metadata_path, direct_path = dist / "METADATA", dist / "direct_url.json"
    metadata_identity = track(metadata_path, kind="file")
    direct_identity = track(direct_path, kind="file")
    metadata_bytes, _ = _read_file(metadata_path)
    metadata = BytesParser(policy=policy.default).parsebytes(metadata_bytes)
    _require(not metadata.defects, InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    for key in ("Metadata-Version", "Name", "Version", "Requires-Python"):
        _require(len(metadata.get_all(key, [])) == 1, InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    _require(str(metadata["Name"]) == "arxiv-digest" and str(metadata["Version"]) == version,
             InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    entry_path = dist / "entry_points.txt"
    track(entry_path, kind="file")
    entry_bytes, _ = _read_file(entry_path)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(entry_bytes.decode("utf-8"))
    _require(parser.sections() == ["console_scripts"]
             and dict(parser["console_scripts"]) == {"arxiv-digest": "arxiv_digest.cli:main"}
             and not parser.defaults(), InstallationUnavailableReason.DISTRIBUTION_MISMATCH)
    requirements = tuple(str(value) for value in metadata.get_all("Requires-Dist", []))
    runtime_requirements_sha256(requirements)
    direct, _ = _read_file(direct_path)
    return DistributionIdentity(
        "arxiv-digest", version, dist, requirements, str(metadata["Requires-Python"]),
        module, metadata_identity.sha256, direct_identity.sha256,
    ), direct


def detect_pipx_installation(
    *, current_version: str = __version__,
    running_command: Path | None = None,
    running_interpreter: Path | None = None,
    running_module: Path | None = None,
    running_python: tuple[int, int] | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    paths: AppPaths,
    maintenance: MaintenanceBarrier,
    run_command: CommandRunner | None = None,
    inspect_backup_source: BackupSourceInspector | None = None,
    validate_protected_provenance: ProtectedProvenanceValidator | None = None,
    deadline_at: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> InstallationDetection:
    """Return a private proof or a closed reason, without repairing installation state."""

    from arxiv_digest.backup import inspect_portable_backup_source
    from arxiv_digest.update_pipx import parse_pipx_metadata, probe_pipx, run_bounded_command

    reason = InstallationUnavailableReason.UNSUPPORTED_LAYOUT
    try:
        platform = sys.platform if platform is None else platform
        _require(platform in {"darwin", "linux"}, InstallationUnavailableReason.UNSUPPORTED_PLATFORM)
        _require(canonical_version(current_version) >= (0, 3, 0), InstallationUnavailableReason.UNSUPPORTED_VERSION)
        environ = dict(os.environ if environ is None else environ)
        deadline_at = monotonic() + DISCOVERY_DEADLINE_SECONDS if deadline_at is None else deadline_at
        _require(math.isfinite(deadline_at) and monotonic() < deadline_at,
                 InstallationUnavailableReason.DEADLINE_EXPIRED)
        running_python = (sys.version_info.major, sys.version_info.minor) if running_python is None else running_python
        home = _absolute(environ["HOME"])
        interpreter = _absolute(sys.executable if running_interpreter is None else running_interpreter)
        command = _absolute(sys.argv[0] if running_command is None else running_command)
        if running_module is None:
            import arxiv_digest
            running_module = Path(arxiv_digest.__file__)
        module = _absolute(running_module)
        venv = interpreter.parent.parent
        _require(interpreter.parent.name == "bin" and venv.name == "arxiv-digest"
                 and venv.parent.name == "venvs", InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        pipx_home = venv.parent.parent
        configured_home = environ.get("PIPX_HOME")
        if configured_home:
            allowed_home = _absolute(configured_home)
        else:
            legacy = home / ".local/pipx"
            if legacy.exists():
                allowed_home = legacy
            elif platform == "darwin":
                allowed_home = home / "Library/Application Support/pipx"
            else:
                xdg = environ.get("XDG_DATA_HOME", str(home / ".local/share"))
                allowed_home = _absolute(xdg) / "pipx"
        _require(pipx_home == allowed_home and pipx_home not in {Path("/opt/pipx"), Path("/usr/local/pipx")},
                 InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        bin_dir = _absolute(environ.get("PIPX_BIN_DIR", str(home / ".local/bin")))
        shared_libs = _absolute(environ.get("PIPX_SHARED_LIBS", str(pipx_home / "shared")))
        man_dir = _absolute(environ.get("PIPX_MAN_DIR", str(home / ".local/share/man")))
        completion_dir = _absolute(environ.get("PIPX_COMPLETION_DIR", str(home / ".local/share/bash-completion/completions")))
        exposed = bin_dir / "arxiv-digest"
        entry = venv / "bin/arxiv-digest"
        pipx_selector, pipx = _pipx_executable(environ, home, platform)
        tracked: dict[Path, tuple[str, bool, FileIdentity]] = {}

        def track(path: Path, *, kind: str, allow_root: bool = False) -> FileIdentity:
            identity = _capture(path, kind=kind, allow_root=allow_root)
            if path in tracked:
                _require(identity == tracked[path][2], InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED)
            tracked[path] = kind, allow_root, identity
            return identity

        for directory in (home, pipx_home, venv.parent, venv, entry.parent, bin_dir):
            track(directory, kind="directory")
        _require(len({tracked[path][2].device for path in (pipx_home, venv.parent, venv)}) == 1,
                 InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        track(pipx_selector.parent, kind="directory", allow_root=True)
        track(pipx_selector, kind="link" if pipx_selector.is_symlink() else "executable", allow_root=True)
        track(pipx, kind="executable", allow_root=True)
        track(exposed, kind="link")
        _require(exposed.resolve(strict=True) == entry and command in {entry, exposed},
                 InstallationUnavailableReason.COMMAND_MISMATCH)
        track(entry, kind="executable")
        interpreter_kind = "link" if interpreter.is_symlink() else "executable"
        track(interpreter, kind=interpreter_kind)
        base = interpreter.resolve(strict=True)
        track(base, kind="executable", allow_root=True)
        site = venv / "lib" / f"python{running_python[0]}.{running_python[1]}" / "site-packages"
        for directory in (site.parent.parent, site.parent):
            track(directory, kind="directory")
        distribution, direct_bytes = _distribution(site, current_version, module, track)
        metadata_path = venv / "pipx_metadata.json"
        track(metadata_path, kind="file")
        raw_metadata, _ = _read_file(metadata_path)
        direct_value = _json(direct_bytes)
        updater_source = type(direct_value) is dict and "archive_info" in direct_value
        authenticated_provenance = False
        provenance_validator = validate_protected_provenance
        if updater_source:
            if provenance_validator is None:
                from arxiv_digest.update_snapshot import validate_protected_provenance as provenance_validator
            raw_native = _json(raw_metadata)
            _require(type(raw_native) is dict and type(raw_native.get("main_package")) is dict,
                     InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED)
            source_candidate = raw_native["main_package"].get("package_or_url")
            _require(type(source_candidate) is str, InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED)
            authenticated_provenance = provenance_validator(
                paths=paths, distribution=distribution, direct_url=direct_bytes,
                package_or_url=source_candidate,
            ) is True
            _require(authenticated_provenance, InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED)
        native_metadata = parse_pipx_metadata(raw_metadata, updater_provenance=authenticated_provenance)
        source_interpreter = native_metadata.source_interpreter
        track(source_interpreter.parent, kind="directory", allow_root=True)
        track(source_interpreter, kind="link" if source_interpreter.is_symlink() else "executable", allow_root=True)
        _require(source_interpreter.resolve(strict=True) == base,
                 InstallationUnavailableReason.INTERPRETER_MISMATCH)
        config_path = venv / "pyvenv.cfg"
        track(config_path, kind="file")
        config_bytes, _ = _read_file(config_path)
        configuration: dict[str, str] = {}
        for line in config_bytes.decode("utf-8").splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition("=")
            _require(bool(separator) and key.strip() not in configuration,
                     InstallationUnavailableReason.INTERPRETER_MISMATCH)
            configuration[key.strip()] = value.strip()
        configured_base = _absolute(configuration.get("executable", ""))
        configured_home = _absolute(configuration.get("home", ""))
        home_interpreter = configured_home / source_interpreter.name
        track(configured_home, kind="directory", allow_root=True)
        for candidate in (configured_base, home_interpreter):
            track(candidate, kind="link" if candidate.is_symlink() else "executable", allow_root=True)
        _require(configuration.get("include-system-site-packages") == "false"
                 and home_interpreter.resolve(strict=True) == base
                 and configured_base.resolve(strict=True) == base,
                 InstallationUnavailableReason.INTERPRETER_MISMATCH)
        _require(configuration.get("version", "").startswith(f"{running_python[0]}.{running_python[1]}."),
                 InstallationUnavailableReason.INTERPRETER_MISMATCH)
        entry_bytes, _ = _read_file(entry)
        raw_interpreter = str(interpreter)
        quoted_interpreter = f'"{raw_interpreter}"' if " " in raw_interpreter else raw_interpreter
        wrappers = tuple((
            "#!/bin/sh\n'''exec' " + quoted_interpreter + flags + ' "$0" "$@"\n' + "' '''\n"
        ).encode() for flags in ("", " -E"))
        _require(bool(entry_bytes) and (
            entry_bytes.splitlines()[0] in {f"#!{interpreter}".encode(), f"#!{interpreter} -E".encode()}
            or (not any(char in raw_interpreter for char in '\"$`\\\r\n\t') and entry_bytes.startswith(wrappers))
        ),
                 InstallationUnavailableReason.COMMAND_MISMATCH)
        snapshot_root = pipx_home / UPDATE_SNAPSHOT_DIRNAME
        if snapshot_root.exists() or snapshot_root.is_symlink():
            snapshot_identity = track(snapshot_root, kind="directory")
            _require(stat.S_IMODE(snapshot_identity.mode) == 0o700
                     and snapshot_identity.device == venv.stat().st_dev,
                     InstallationUnavailableReason.UNSUPPORTED_LAYOUT)
        reason = InstallationUnavailableReason.PIPX_UNAVAILABLE
        probe = probe_pipx(
            pipx_executable=pipx, venv=venv, pipx_home=pipx_home,
            exposed_command=exposed, environ=environ, deadline_at=deadline_at,
            run_command=run_bounded_command if run_command is None else run_command,
            updater_provenance=authenticated_provenance,
        )
        metadata = probe.metadata
        _require(metadata.main_package.package_version == current_version
                 and metadata.main_package.app_paths == (entry,)
                 and metadata.main_package.apps == ("arxiv-digest",),
                 InstallationUnavailableReason.COMMAND_MISMATCH)
        _require(metadata.source_interpreter.resolve(strict=True) == base
                 and metadata.python_version == f"Python {configuration['version']}",
                 InstallationUnavailableReason.INTERPRETER_MISMATCH)
        source = metadata.main_package.package_or_url
        direct = _json(direct_bytes)
        source_kind: Literal["canonical_tag", "updater_provenance"] = "canonical_tag"
        commit = None
        if source == f"git+{REPOSITORY}.git@v{current_version}":
            _require(type(direct) is dict and set(direct) == {"url", "vcs_info"}
                     and direct["url"] == f"{REPOSITORY}.git",
                     InstallationUnavailableReason.UNSUPPORTED_SOURCE)
            vcs = direct["vcs_info"]
            _require(type(vcs) is dict and set(vcs) == {"vcs", "requested_revision", "commit_id"}
                     and vcs["vcs"] == "git" and vcs["requested_revision"] == f"v{current_version}"
                     and type(vcs["commit_id"]) is str and re.fullmatch(r"[0-9a-f]{40}", vcs["commit_id"]) is not None,
                     InstallationUnavailableReason.UNSUPPORTED_SOURCE)
            commit = vcs["commit_id"]
        elif type(direct) is dict and "archive_info" in direct:
            _require(provenance_validator is not None,
                     InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED)
            _require(provenance_validator(
                paths=paths, distribution=distribution, direct_url=direct_bytes,
                package_or_url=source,
            ) is True, InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED)
            source_kind = "updater_provenance"
        else:
            raise _Unavailable(InstallationUnavailableReason.UNSUPPORTED_SOURCE)
        reason = InstallationUnavailableReason.BACKUP_UNAVAILABLE
        inspector = inspect_portable_backup_source if inspect_backup_source is None else inspect_backup_source
        backup = inspector(paths, maintenance=maintenance,
                           timeout=max(0.0, deadline_at - monotonic()), monotonic=monotonic)
        _require(backup.application_generation == APPLICATION_DATA_GENERATION,
                 InstallationUnavailableReason.BACKUP_UNAVAILABLE)
        reason = InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED
        for path, (kind, allow_root, prior) in tracked.items():
            _require(_capture(path, kind=kind, allow_root=allow_root) == prior,
                     InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED)
        _require(monotonic() < deadline_at, InstallationUnavailableReason.DEADLINE_EXPIRED)
        return InstallationDetection(installation=PipxInstallation(
            current_version, source_kind, platform, running_python, pipx, pipx_home,
            venv, exposed, entry, interpreter, base, snapshot_root, distribution,
            runtime_requirements_sha256(distribution.requirements), commit,
            tuple((str(path), item[2]) for path, item in sorted(tracked.items())),
            probe.metadata_sha256, backup, home, shared_libs, man_dir, completion_dir,
        ))
    except _Unavailable as error:
        return InstallationDetection(reason=error.reason)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, configparser.Error):
        return InstallationDetection(reason=reason)


def revalidate_installation_identity(installation: PipxInstallation) -> None:
    """Recheck the detector's exact evidence before recording a new full token."""
    if not isinstance(installation, PipxInstallation) or not installation.identities:
        raise ValueError("installation has no detector identity proof")
    for raw_path, expected in installation.identities:
        path = Path(raw_path)
        if stat.S_ISLNK(expected.mode):
            kind = "link"
        elif stat.S_ISDIR(expected.mode):
            kind = "directory"
        else:
            kind = "file" if expected.sha256 is not None else "executable"
        if _capture(path, kind=kind, allow_root=expected.uid == 0) != expected:
            raise ValueError("installation changed since discovery")
