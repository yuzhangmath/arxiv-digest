"""Closed pipx metadata and isolated, bounded installation probes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from arxiv_digest.update_contract import TERM_GRACE_SECONDS


METADATA_BYTE_LIMIT = 128 * 1024
COMMAND_OUTPUT_BYTE_LIMIT = 256 * 1024
COMMAND_ERROR_BYTE_LIMIT = 32 * 1024


class PipxProbeError(ValueError):
    """The installation cannot be proven eligible by an isolated pipx probe."""


class CommandTimeoutError(TimeoutError):
    pass


class CommandOutputLimitError(ValueError):
    pass


class CommandProcessGroupError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CommandRequest:
    argv: tuple[str, ...]
    environ: Mapping[str, str]
    cwd: Path
    deadline_at: float
    stdout_limit: int
    stderr_limit: int

    def __post_init__(self) -> None:
        if (
            type(self.argv) is not tuple or not self.argv
            or any(type(arg) is not str or "\0" in arg for arg in self.argv)
            or not Path(self.argv[0]).is_absolute()
            or not self.cwd.is_absolute()
            or not math.isfinite(self.deadline_at)
            or any(type(limit) is not int or limit < 0 for limit in (self.stdout_limit, self.stderr_limit))
            or any(type(k) is not str or type(v) is not str or not k or "=" in k or "\0" in k + v for k, v in self.environ.items())
        ):
            raise ValueError("invalid bounded command request")
        object.__setattr__(self, "environ", MappingProxyType(dict(self.environ)))


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def __call__(self, request: CommandRequest) -> CommandResult: ...


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_bounded_command(request: CommandRequest) -> CommandResult:
    """Capture finite output while supervising the complete process group."""

    if time.monotonic() >= request.deadline_at:
        raise CommandTimeoutError("command deadline expired")
    process = subprocess.Popen(
        request.argv, env=dict(request.environ), cwd=request.cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": request.stdout_limit, "stderr": request.stderr_limit}
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                remaining = request.deadline_at - time.monotonic()
                if remaining <= 0:
                    raise CommandTimeoutError("command deadline expired")
                for key, _ in selector.select(min(remaining, 0.05)):
                    name = key.data
                    chunk = os.read(key.fd, min(64 * 1024, limits[name] - len(output[name]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[name].extend(chunk)
                        if len(output[name]) > limits[name]:
                            raise CommandOutputLimitError("command output limit exceeded")
            returncode = process.wait()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise CommandProcessGroupError("command left a running process group")
            return CommandResult(returncode, bytes(output["stdout"]), bytes(output["stderr"]))
    except BaseException:
        _terminate_group(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


@dataclass(frozen=True, slots=True)
class PipxPackageInfo:
    package: str
    package_or_url: str
    pip_args: tuple[str, ...]
    include_dependencies: bool
    include_apps: bool
    apps: tuple[str, ...]
    app_paths: tuple[Path, ...]
    apps_of_dependencies: tuple[str, ...]
    app_paths_of_dependencies: Mapping[str, tuple[Path, ...]]
    package_version: str
    expected_apps: tuple[str, ...]
    lock_file: Path | None
    include_resources_from: tuple[str, ...]
    cooldown_days: int | None
    man_pages: tuple[str, ...]
    man_paths: tuple[Path, ...]
    man_pages_of_dependencies: tuple[str, ...]
    man_paths_of_dependencies: Mapping[str, tuple[Path, ...]]
    completions: tuple[str, ...]
    completion_paths: tuple[Path, ...]
    completions_of_dependencies: tuple[str, ...]
    completion_paths_of_dependencies: Mapping[str, tuple[Path, ...]]
    suffix: str
    pinned: bool


@dataclass(frozen=True, slots=True)
class PipxMetadata:
    environment: str | None
    main_package: PipxPackageInfo
    python_version: str
    source_interpreter: Path
    venv_args: tuple[str, ...]
    injected_packages: Mapping[str, PipxPackageInfo]
    backend: str
    exposure_enabled: bool
    pipx_metadata_version: str


@dataclass(frozen=True, slots=True)
class PipxProbeResult:
    metadata: PipxMetadata
    metadata_sha256: str


def _strict_json(payload: bytes) -> object:
    if type(payload) is not bytes or len(payload) > METADATA_BYTE_LIMIT:
        raise PipxProbeError("pipx metadata exceeds its byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PipxProbeError("duplicate pipx metadata key")
            result[key] = value
        return result

    def constant(_value):
        raise PipxProbeError("nonfinite pipx metadata value")

    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise PipxProbeError("invalid pipx metadata JSON") from error


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise PipxProbeError("pipx metadata has an unsupported schema")
    return value


def _path_wire(value: object) -> Path:
    raw = _object(value, {"__type__", "__Path__"})
    text = raw["__Path__"]
    if raw["__type__"] != "Path" or type(text) is not str or "\0" in text or "\\" in text:
        raise PipxProbeError("invalid pipx path wire")
    path = Path(text)
    if not path.is_absolute() or str(path) != text or ".." in path.parts:
        raise PipxProbeError("pipx path must be canonical and absolute")
    return path


def _string_list(value: object) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or "\0" in item for item in value):
        raise PipxProbeError("invalid pipx string list")
    return tuple(value)


def _metadata_value(value: object, *, updater_provenance: bool = False) -> PipxMetadata:
    if type(updater_provenance) is not bool:
        raise PipxProbeError("invalid provenance admission")
    data = _object(value, {field.name for field in fields(PipxMetadata)})
    package = _object(data["main_package"], {field.name for field in fields(PipxPackageInfo)})
    parsed = dict(package)
    for name in ("package", "package_or_url", "package_version", "suffix"):
        if type(package[name]) is not str or "\0" in package[name]:
            raise PipxProbeError("invalid pipx package text")
    for name in ("include_dependencies", "include_apps", "pinned"):
        if type(package[name]) is not bool:
            raise PipxProbeError("invalid pipx package boolean")
    for name in ("pip_args", "apps", "apps_of_dependencies", "expected_apps", "include_resources_from", "man_pages", "man_pages_of_dependencies", "completions", "completions_of_dependencies"):
        parsed[name] = _string_list(package[name])
    for name in ("app_paths", "man_paths", "completion_paths"):
        if type(package[name]) is not list:
            raise PipxProbeError("invalid pipx path list")
        parsed[name] = tuple(_path_wire(item) for item in package[name])
    for name in ("app_paths_of_dependencies", "man_paths_of_dependencies", "completion_paths_of_dependencies"):
        if type(package[name]) is not dict or package[name]:
            raise PipxProbeError("pipx dependency resources are unsupported")
        parsed[name] = MappingProxyType({})
    if package["lock_file"] is not None or package["cooldown_days"] is not None:
        raise PipxProbeError("pipx install options are unsupported")
    info = PipxPackageInfo(**parsed)
    if (
        info.package != "arxiv-digest" or not info.package_or_url or not info.package_version
        or info.apps != ("arxiv-digest",) or len(info.app_paths) != 1
        or info.expected_apps not in ((), ("arxiv-digest",))
        or info.include_dependencies or not info.include_apps or info.pinned
        or info.pip_args != (("--no-deps", "--no-index") if updater_provenance else ())
        or (updater_provenance and info.expected_apps != ("arxiv-digest",))
        or any((info.apps_of_dependencies, info.include_resources_from, info.man_pages, info.man_paths, info.man_pages_of_dependencies, info.completions, info.completion_paths, info.completions_of_dependencies, info.suffix))
    ):
        raise PipxProbeError("pipx package options are unsupported")
    if (
        data["environment"] not in (None, "arxiv-digest")
        or type(data["python_version"]) is not str
        or re.fullmatch(r"Python [0-9]+\.[0-9]+\.[0-9]+", data["python_version"]) is None
        or data["backend"] != "pip" or data["pipx_metadata_version"] != "0.12"
        or data["exposure_enabled"] is not True
        or _string_list(data["venv_args"]) != ()
        or type(data["injected_packages"]) is not dict or data["injected_packages"]
    ):
        raise PipxProbeError("pipx environment options are unsupported")
    return PipxMetadata(
        environment=data["environment"], main_package=info,
        python_version=data["python_version"], source_interpreter=_path_wire(data["source_interpreter"]),
        venv_args=(), injected_packages=MappingProxyType({}), backend="pip",
        exposure_enabled=True, pipx_metadata_version="0.12",
    )


def parse_pipx_metadata(payload: bytes, *, updater_provenance: bool = False) -> PipxMetadata:
    return _metadata_value(_strict_json(payload), updater_provenance=updater_provenance)


def parse_pipx_list(payload: bytes, *, updater_provenance: bool = False) -> PipxMetadata:
    data = _object(_strict_json(payload), {"pipx_spec_version", "venvs"})
    if data["pipx_spec_version"] != "0.1":
        raise PipxProbeError("unsupported pipx list version")
    environments = _object(data["venvs"], {"arxiv-digest"})
    entry = _object(environments["arxiv-digest"], {"metadata"})
    return _metadata_value(entry["metadata"], updater_provenance=updater_provenance)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
        metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
    )


def _directory_identity(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise PipxProbeError("pipx directory is not owned and safe")
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode


def _read_metadata(path: Path) -> bytes:
    if not getattr(os, "O_NOFOLLOW", 0):
        raise PipxProbeError("no-follow metadata reads are unsupported")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
        or before.st_nlink != 1 or before.st_mode & 0o022
        or before.st_size > METADATA_BYTE_LIMIT
    ):
        raise PipxProbeError("pipx metadata file is unsafe")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise PipxProbeError("pipx metadata identity changed")
        chunks = bytearray()
        while len(chunks) <= METADATA_BYTE_LIMIT:
            chunk = os.read(descriptor, min(64 * 1024, METADATA_BYTE_LIMIT - len(chunks) + 1))
            if not chunk:
                break
            chunks.extend(chunk)
        if (
            len(chunks) > METADATA_BYTE_LIMIT
            or _identity(os.fstat(descriptor)) != _identity(before)
            or _identity(path.lstat()) != _identity(before)
        ):
            raise PipxProbeError("pipx metadata changed during reading")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _path_token(path: Path) -> tuple:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return _identity(metadata), os.readlink(path), _identity(path.stat())
    return (_identity(metadata),)


_SHADOW_DIRECTORIES = (
    "home", "bin", "man", "completion", "shared", "tmp",
    "xdg-data", "xdg-cache", "xdg-config", "xdg-state", "xdg-runtime",
    "pipx", "pipx/venvs", "pipx/logs", "pipx/.cache", "pipx/py",
)
_SHADOW_FILES = frozenset({
    "pipx/venvs/.arxiv-digest.lock", "pipx/.cache/CACHEDIR.TAG", "pipx/py/CACHEDIR.TAG",
})
_SHADOW_LOG = re.compile(r"pipx/logs/cmd_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}\.[0-9]{2}\.[0-9]{2}(?:_[1-9])?\.log")


def _shadow_environment(root: Path, environ: Mapping[str, str]) -> dict[str, str]:
    path = os.pathsep.join(dict.fromkeys(
        entry for entry in environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).is_absolute() and "\0" not in entry
    ))
    return {
        "PATH": path, "HOME": str(root / "home"),
        "PIPX_HOME": str(root / "pipx"), "PIPX_BIN_DIR": str(root / "bin"),
        "PIPX_MAN_DIR": str(root / "man"), "PIPX_COMPLETION_DIR": str(root / "completion"),
        "PIPX_SHARED_LIBS": str(root / "shared"),
        "PIPX_DEFAULT_BACKEND": "pip", "PIPX_FETCH_PYTHON": "never",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1",
        "PIPX_USE_EMOJI": "0", "PIPX_MAX_LOGS": "10",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": str(root / "xdg-cache"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "XDG_DATA_HOME": str(root / "xdg-data"), "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_RUNTIME_DIR": str(root / "xdg-runtime"), "TMPDIR": str(root / "tmp"),
        "LANG": "C", "LC_ALL": "C", "NO_COLOR": "1", "TERM": "dumb",
    }


def _audit_shadow(
    root: Path, identities: Mapping[str, tuple[int, ...]],
    link_identity: tuple, venv: Path,
) -> None:
    for relative, expected in identities.items():
        if _directory_identity(root / relative) != expected:
            raise PipxProbeError("pipx shadow directory identity changed")
    link = root / "pipx/venvs/arxiv-digest"
    if (
        _identity(link.lstat()) != link_identity
        or not link.is_symlink() or os.readlink(link) != str(venv)
    ):
        raise PipxProbeError("pipx shadow environment link changed")
    pending = [root]
    count = 0
    size = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 64:
                    raise PipxProbeError("pipx shadow inventory exceeds its limit")
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                metadata = entry.stat(follow_symlinks=False)
                if relative == "pipx/venvs/arxiv-digest":
                    if _identity(metadata) != link_identity:
                        raise PipxProbeError("pipx shadow environment link changed")
                    continue
                if relative in identities:
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode) != identities[relative]
                    ):
                        raise PipxProbeError("pipx shadow directory identity changed")
                    pending.append(path)
                    continue
                if (
                    relative not in _SHADOW_FILES and _SHADOW_LOG.fullmatch(relative) is None
                    or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1 or metadata.st_mode & 0o022
                ):
                    raise PipxProbeError("pipx shadow contains an unexpected write")
                size += metadata.st_size
                if size > COMMAND_OUTPUT_BYTE_LIMIT:
                    raise PipxProbeError("pipx shadow writes exceed their byte limit")


def probe_pipx(
    *, pipx_executable: Path, venv: Path, pipx_home: Path,
    exposed_command: Path, environ: Mapping[str, str], deadline_at: float,
    run_command: CommandRunner = run_bounded_command,
    updater_provenance: bool = False,
) -> PipxProbeResult:
    """Read native metadata first, then query pipx only inside an owned shadow."""

    try:
        return _probe_pipx(
            pipx_executable=pipx_executable, venv=venv, pipx_home=pipx_home,
            exposed_command=exposed_command, environ=environ,
            deadline_at=deadline_at, run_command=run_command,
            updater_provenance=updater_provenance,
        )
    except OSError as error:
        raise PipxProbeError("pipx probe could not verify filesystem state") from error


def _probe_pipx(
    *, pipx_executable: Path, venv: Path, pipx_home: Path,
    exposed_command: Path, environ: Mapping[str, str], deadline_at: float,
    run_command: CommandRunner,
    updater_provenance: bool = False,
) -> PipxProbeResult:

    if not math.isfinite(deadline_at) or time.monotonic() >= deadline_at:
        raise PipxProbeError("pipx probe deadline expired")
    if (
        any(not path.is_absolute() for path in (pipx_executable, venv, pipx_home, exposed_command))
        or venv != pipx_home / "venvs/arxiv-digest"
    ):
        raise PipxProbeError("pipx layout is unsupported")
    for directory in (pipx_home, venv.parent, venv, exposed_command.parent):
        _directory_identity(directory)
    executable_stat = pipx_executable.lstat()
    if (
        not stat.S_ISREG(executable_stat.st_mode) or executable_stat.st_uid not in {0, os.getuid()}
        or executable_stat.st_mode & 0o022 or not executable_stat.st_mode & 0o111
    ):
        raise PipxProbeError("pipx executable is unsafe")
    metadata_path = venv / "pipx_metadata.json"
    payload = _read_metadata(metadata_path)
    metadata = parse_pipx_metadata(payload, updater_provenance=updater_provenance)
    if metadata.main_package.app_paths != (venv / "bin/arxiv-digest",):
        raise PipxProbeError("pipx entry point differs from the environment")
    observed_paths = (
        pipx_executable, pipx_home, venv.parent, venv, exposed_command,
        venv / "bin", venv / "bin/python", metadata.source_interpreter,
        metadata.main_package.app_paths[0], metadata_path,
    )
    tokens = tuple(_path_token(path) for path in observed_paths)
    root = Path(tempfile.mkdtemp(prefix="arxiv-digest-pipx-probe-")).resolve()
    identities: dict[str, tuple[int, ...]] = {".": _directory_identity(root)}
    link: Path | None = None
    link_identity: tuple | None = None
    try:
        for relative in _SHADOW_DIRECTORIES:
            directory = root / relative
            directory.mkdir(mode=0o700)
            identities[relative] = _directory_identity(directory)
        link = root / "pipx/venvs/arxiv-digest"
        link.symlink_to(venv)
        link_identity = _identity(link.lstat())
        if tuple(path.name for path in link.parent.iterdir()) != ("arxiv-digest",):
            raise PipxProbeError("pipx shadow environment is not isolated")
        sanitized = _shadow_environment(root, environ)
        commands = (
            ("--version",), ("list", "--help"), ("install", "--help"),
            ("list", "--skip-maintenance", "--output", "json", "arxiv-digest"),
        )
        for arguments in commands:
            if time.monotonic() >= deadline_at:
                raise PipxProbeError("pipx probe deadline expired")
            _audit_shadow(root, identities, link_identity, venv)
            if tuple(_path_token(path) for path in observed_paths) != tokens or _read_metadata(metadata_path) != payload:
                raise PipxProbeError("pipx installation changed during probing")
            try:
                result = run_command(CommandRequest(
                    (str(pipx_executable), *arguments), sanitized, root, deadline_at,
                    COMMAND_OUTPUT_BYTE_LIMIT, COMMAND_ERROR_BYTE_LIMIT,
                ))
            except (OSError, ValueError) as error:
                raise PipxProbeError("pipx command could not be verified") from error
            _audit_shadow(root, identities, link_identity, venv)
            if tuple(_path_token(path) for path in observed_paths) != tokens or _read_metadata(metadata_path) != payload:
                raise PipxProbeError("pipx installation changed during probing")
            if (
                time.monotonic() >= deadline_at or type(result) is not CommandResult
                or type(result.returncode) is not int or result.returncode != 0
                or type(result.stdout) is not bytes or type(result.stderr) is not bytes
                or result.stderr or len(result.stdout) > COMMAND_OUTPUT_BYTE_LIMIT
            ):
                raise PipxProbeError("pipx command returned an unsupported result")
            if arguments == ("--version",) and result.stdout != b"1.16.7\n":
                raise PipxProbeError("unsupported pipx version")
            if arguments[-1] == "--help":
                options = (
                    ("--output", "--skip-maintenance") if arguments[0] == "list"
                    else ("--force", "--app", "--python", "--fetch-python", "--skip-maintenance", "--backend", "--pip-args")
                )
                if any(re.search(rb"(?<![a-z0-9-])" + option.encode() + rb"(?![a-z0-9-])", result.stdout) is None for option in options):
                    raise PipxProbeError("pipx help lacks required command options")
            if arguments[-1] == "arxiv-digest" and parse_pipx_list(result.stdout, updater_provenance=updater_provenance) != metadata:
                raise PipxProbeError("pipx list differs from native metadata")
        return PipxProbeResult(metadata, hashlib.sha256(payload).hexdigest())
    finally:
        if link is not None and link_identity is not None:
            _audit_shadow(root, identities, link_identity, venv)
            link.unlink()
        elif _directory_identity(root) != identities["."]:
            raise PipxProbeError("pipx shadow root changed before cleanup")
        shutil.rmtree(root)
