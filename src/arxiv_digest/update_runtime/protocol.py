"""Authoritative bounded codecs and protected updater stores.

This module is copied verbatim outside the changing environment. It imports only
Python's standard library. Codec validation is lexical; protected store methods
own filesystem identity checks and durability. Journal locking is innermost.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

class AtomicRenameUnsupportedError(RuntimeError):
    """The host cannot provide an atomic no-replace rename."""


def atomic_rename_noreplace(
    source: Path, destination: Path, *, source_directory_fd: int | None = None,
    destination_directory_fd: int | None = None,
) -> None:
    """Atomically rename ``source`` while refusing an existing destination."""

    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if b"\x00" in source_bytes or b"\x00" in destination_bytes:
        raise ValueError("embedded null byte in rename path")
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as error:
            raise AtomicRenameUnsupportedError(
                "atomic no-replace rename is unsupported"
            ) from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100 if source_directory_fd is None else source_directory_fd, source_bytes,
                        -100 if destination_directory_fd is None else destination_directory_fd, destination_bytes, 1)
    elif sys.platform == "darwin":
        try:
            rename = library.renamex_np if source_directory_fd is None and destination_directory_fd is None else library.renameatx_np
        except AttributeError as error:
            raise AtomicRenameUnsupportedError(
                "atomic no-replace rename is unsupported"
            ) from error
        rename.restype = ctypes.c_int
        if source_directory_fd is None and destination_directory_fd is None:
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            result = rename(source_bytes, destination_bytes, 0x00000004)
        else:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = rename(-2 if source_directory_fd is None else source_directory_fd, source_bytes,
                            -2 if destination_directory_fd is None else destination_directory_fd, destination_bytes, 0x00000004)
    else:
        raise AtomicRenameUnsupportedError(
            "atomic no-replace rename is unsupported"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    if error_number in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.ENOTSUP,
    }:
        raise AtomicRenameUnsupportedError(
            "atomic no-replace rename is unsupported"
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("private path must be a directory")
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                "private directory must be owned by the current user"
            )
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def ensure_private_directory_strict(path: Path) -> None:
    """Create or validate an exact private directory without repairing it."""

    flags = (
        os.O_RDONLY
        | _required_open_flag("O_DIRECTORY")
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = path.lstat()
    _validate_private_directory(before)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        after = path.lstat()
        for entry in (metadata, after):
            _validate_private_directory(entry)
            if _private_identity(entry) != _private_identity(before):
                raise PermissionError("private directory path identity changed")
        if os.get_inheritable(descriptor):
            raise PermissionError("private directory descriptor must be close-on-exec")
    finally:
        os.close(descriptor)


def _required_open_flag(name: str) -> int:
    flag = getattr(os, name, 0)
    if not flag:
        raise OSError(errno.ENOTSUP, f"private no-follow open is unsupported: {name}")
    return flag


def _private_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode


def _validate_private_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PermissionError("private directory must be owned mode 0700")


def _validate_private_lock_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise PermissionError("private lock file must be owned mode 0600")


def open_private_lock_file(path: Path) -> int:
    """Return a stable no-follow coordination descriptor, owned by the caller."""

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_NONBLOCK
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None:
        _validate_private_lock_file(before)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        after = path.lstat()
        _validate_private_lock_file(metadata)
        _validate_private_lock_file(after)
        if (
            _private_identity(metadata) != _private_identity(after)
            or (before is not None and _private_identity(before) != _private_identity(metadata))
        ):
            raise PermissionError("private lock path identity changed")
        if os.get_inheritable(descriptor):
            raise PermissionError("private lock descriptor must be close-on-exec")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def ensure_private_lock_file(path: Path) -> None:
    """Create or validate a private coordination lock without changing content."""

    os.close(open_private_lock_file(path))


RECORD_BYTE_LIMIT = 32 * 1024 * 1024
CONTROL_BYTE_LIMIT = 64 * 1024
COLLECTION_LIMIT = 200_000
PATH_BYTE_LIMIT = 4096
STRING_BYTE_LIMIT = 16 * 1024
WHEEL_BYTE_LIMIT = 128 * 1024 * 1024
PRIVATE_LOG_BYTE_LIMIT = 4 * 1024 * 1024
LOCK_WAIT_TIMEOUT_SECONDS = 45.0
READY_DEADLINE_SECONDS = 120.0
PARENT_EXIT_TIMEOUT_SECONDS = 60.0
PIPX_COMMAND_TIMEOUT_SECONDS = 15.0 * 60.0
SELF_CHECK_TIMEOUT_SECONDS = 60.0
DATA_RECOVERY_TIMEOUT_SECONDS = 60.0
HEALTH_HANDSHAKE_TIMEOUT_SECONDS = 90.0
TERM_GRACE_SECONDS = 10.0
PRODUCT = "arxiv-digest"
SCHEMA_VERSION = UPDATER_PROTOCOL = 1
APPLICATION_DATA_GENERATION = 2
PLAN_FILENAME = "update-plan.json"
JOURNAL_FILENAME = "update-journal.json"
PROVENANCE_FILENAME = "update-provenance.json"
JOURNAL_LOCK_FILENAME = "update-journal.lock"
RUNTIME_FILENAMES = ("protocol.py", "guard.py", "helper.py", "recovery.py")
COMMON = {"schema_version", "product", "updater_protocol", "application_data_generation"}
IDENTITY_KEYS = {"device", "inode", "uid", "mode"}
FILE_IDENTITY_KEYS = IDENTITY_KEYS | {"size", "mtime_ns", "ctime_ns", "nlink"}
STATES = frozenset({"prepared", "committed", "installing", "target_installed",
    "launching_target", "healthy_pending_commit", "rolling_back",
    "canceling_no_install", "complete", "aborted_no_mutation", "recovery_failed",
    "external_change_detected"})
TERMINAL_STATES = frozenset({"complete", "aborted_no_mutation", "recovery_failed", "external_change_detected"})
BLOCKING_STATES = frozenset({"recovery_failed", "external_change_detected"})
OUTCOME_MESSAGES = {"updated": "update_succeeded", "restored": "update_failed_restored",
    "handoff_failed": "handoff_failed", "recovery_failed": "recovery_failed",
    "external_change_detected": "external_change_detected"}


class ProtocolError(ValueError):
    """A control record violates the closed updater protocol."""


class StoreError(ProtocolError):
    """A protected record could not be read or published unambiguously."""


class StaleSnapshotError(StoreError):
    pass


class PendingReceiptError(StoreError):
    def __init__(self):
        super().__init__("pending_update_receipt")


def _require(condition, message="invalid updater record"):
    if not condition:
        raise ProtocolError(message)


def _object(value, keys, optional=()):
    _require(type(value) is dict and len(value) <= COLLECTION_LIMIT,
             "invalid updater object")
    _require(set(keys) <= set(value) <= set(keys) | set(optional),
             "unknown or missing updater fields")
    return value


def _string(value, *, limit=STRING_BYTE_LIMIT):
    _require(type(value) is str, "updater text must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise ProtocolError("invalid UTF-8 updater text") from error
    _require(size <= limit and "\0" not in value, "updater text exceeds its bound")
    return value


def _integer(value, *, minimum=0, maximum=(1 << 63) - 1):
    _require(type(value) is int and minimum <= value <= maximum, "invalid updater integer")
    return value


def _boolean(value):
    _require(type(value) is bool, "invalid updater boolean")
    return value


def _digest(value):
    _string(value, limit=64)
    _require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, "invalid updater digest or nonce")
    return value


def _version(value):
    _string(value)
    _require(re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value) is not None,
             "invalid updater version")
    return value


def validate_path(value, *, absolute=True):
    _string(value, limit=PATH_BYTE_LIMIT)
    _require(bool(value) and "\\" not in value and not any(ord(c) < 32 for c in value),
             "invalid updater path")
    path = PurePosixPath(value)
    _require(path.is_absolute() is absolute and str(path) == value
             and ".." not in path.parts and "." not in path.parts and value not in {"/", "."},
             "updater path must be canonical")
    return value


def _target(value):
    # Symlink targets are recorded as raw text. Resolution/escape rules belong
    # to the scanner; normalizing here would destroy their exact identity.
    _string(value, limit=PATH_BYTE_LIMIT)
    _require(bool(value) and "\\" not in value and not any(ord(c) < 32 for c in value),
             "invalid symlink target")


def _list(value, *, limit=None):
    _require(type(value) is list and len(value) <= (COLLECTION_LIMIT if limit is None else limit),
             "updater collection exceeds its bound")
    return value


def _common(value):
    _require(type(value["schema_version"]) is int and value["schema_version"] == SCHEMA_VERSION,
             "unsupported updater schema")
    _require(type(value["updater_protocol"]) is int and value["updater_protocol"] == UPDATER_PROTOCOL,
             "unsupported updater protocol")
    _require(type(value["application_data_generation"]) is int
             and value["application_data_generation"] == APPLICATION_DATA_GENERATION,
             "unsupported updater data generation")
    _require(value["product"] == PRODUCT, "wrong updater product")


def validate_identity(value, *, file=False):
    _object(value, FILE_IDENTITY_KEYS if file else IDENTITY_KEYS)
    for key in value:
        _integer(value[key])
    _integer(value["mode"], maximum=0o7777)
    if file:
        _require(value["nlink"] == 1, "ambiguous file link count")
    return value


def validate_inventory(value):
    _object(value, {"root_mode", "entries"})
    _integer(value["root_mode"], maximum=0o7777)
    _require(not value["root_mode"] & 0o7022, "unsafe inventory root mode")
    entries = _list(value["entries"])
    paths = set()
    directories = set()
    folded = set()
    previous = None
    for entry in entries:
        _require(type(entry) is dict and entry.get("kind") in {"directory", "file", "symlink"},
                 "invalid inventory entry kind")
        kind = entry["kind"]
        keys = {"kind", "path", "mode"}
        keys |= {"size", "sha256"} if kind == "file" else {"target"} if kind == "symlink" else set()
        _object(entry, keys)
        path = validate_path(entry["path"], absolute=False)
        _integer(entry["mode"], maximum=0o7777)
        _require(path not in paths and path.casefold() not in folded
                 and (previous is None or previous < path), "ambiguous or unordered inventory paths")
        for parent in PurePosixPath(path).parents:
            if str(parent) != ".":
                _require(str(parent) in directories, "inventory omits parent directory")
        if kind != "symlink":
            _require(not entry["mode"] & 0o7022, "unsafe inventory mode")
        if kind == "file":
            _integer(entry["size"])
            _digest(entry["sha256"])
        elif kind == "symlink":
            _target(entry["target"])
        paths.add(path)
        if kind == "directory":
            directories.add(path)
        folded.add(path.casefold())
        previous = path
    return value


def validate_exposed_link(value):
    _object(value, {"path", "target", "uid", "parent"})
    validate_path(value["path"])
    _target(value["target"])
    _integer(value["uid"])
    validate_identity(value["parent"])
    _require(not value["parent"]["mode"] & 0o7022, "unsafe exposed-link parent")
    return value


def validate_core_installation_token(value):
    _object(value, {"environment_path", "environment_identity", "inventory", "exposed_link",
                    "pipx_metadata_sha256", "interpreter"})
    validate_path(value["environment_path"])
    validate_identity(value["environment_identity"])
    validate_inventory(value["inventory"])
    _require(value["environment_identity"]["mode"] == value["inventory"]["root_mode"],
             "environment inventory mode differs")
    validate_exposed_link(value["exposed_link"])
    _digest(value["pipx_metadata_sha256"])
    interpreter = _object(value["interpreter"], {"path", "device", "inode", "uid", "mode", "size", "sha256"})
    validate_path(interpreter["path"])
    for key in ("device", "inode", "uid", "size"):
        _integer(interpreter[key])
    _integer(interpreter["mode"], maximum=0o7777)
    _require(interpreter["mode"] & 0o111 and not interpreter["mode"] & 0o7022,
             "unsafe base interpreter mode")
    _digest(interpreter["sha256"])
    return value


def validate_record_reference(value):
    _object(value, {"identity", "sha256"})
    validate_identity(value["identity"], file=True)
    _require(value["identity"]["mode"] == 0o600, "protected record mode differs")
    _digest(value["sha256"])
    return value


def validate_installation_token(value):
    _object(value, {"core", "provenance"})
    validate_core_installation_token(value["core"])
    if value["provenance"] is not None:
        validate_record_reference(value["provenance"])
    return value


def validate_artifact(value, *, extra=()):
    _object(value, {"path", "size", "sha256", "identity"} | set(extra))
    validate_path(value["path"])
    _integer(value["size"])
    _digest(value["sha256"])
    validate_identity(value["identity"], file=True)
    _require(value["identity"]["size"] == value["size"] and value["identity"]["mode"] == 0o600,
             "protected artifact identity differs")
    return value


def validate_wheel(value):
    validate_artifact(value, extra={"version", "manifest_sha256", "source_url"})
    _version(value["version"])
    _integer(value["size"], minimum=1, maximum=WHEEL_BYTE_LIMIT)
    _digest(value["manifest_sha256"])
    source = _string(value["source_url"])
    name = f"arxiv_digest-{value['version']}-py3-none-any.whl"
    _require(source == f"https://github.com/yuzhangmath/arxiv-digest/releases/download/v{value['version']}/{name}",
             "wheel source is not canonical")
    _require(PurePosixPath(value["path"]).name == name, "retained wheel name differs")
    return value


def validate_provenance(value):
    _object(value, COMMON | {"version", "attempt_id", "launch_id", "wheel", "core_token",
                            "direct_url_sha256", "runtime_requirements_sha256"})
    _common(value)
    _version(value["version"])
    _digest(value["attempt_id"])
    _digest(value["launch_id"])
    validate_wheel(value["wheel"])
    _require(value["wheel"]["version"] == value["version"], "provenance wheel version differs")
    validate_core_installation_token(value["core_token"])
    _digest(value["direct_url_sha256"])
    _digest(value["runtime_requirements_sha256"])
    return value


def _launcher_state(value):
    _require(type(value) is dict and value.get("kind") in {"absent", "file", "directory"}, "invalid launcher kind")
    kind = value["kind"]
    extra = {"size", "sha256", "mode", "content_hex"} if kind == "file" else {"inventory", "files"} if kind == "directory" else set()
    _object(value, {"kind", "path"} | extra)
    validate_path(value["path"])
    if kind == "file":
        _integer(value["size"], maximum=RECORD_BYTE_LIMIT // 2)
        _digest(value["sha256"])
        _integer(value["mode"], maximum=0o777)
        _require(not value["mode"] & 0o022, "unsafe launcher mode")
        raw = _string(value["content_hex"], limit=RECORD_BYTE_LIMIT)
        _require(re.fullmatch(r"(?:[0-9a-f]{2})*", raw) is not None and len(raw) == value["size"] * 2,
                 "invalid launcher content")
        _require(hashlib.sha256(bytes.fromhex(raw)).hexdigest() == value["sha256"], "launcher digest differs")
    elif kind == "directory":
        inventory = validate_inventory(value["inventory"])
        expected = {item["path"]: item for item in inventory["entries"] if item["kind"] == "file"}
        files = _list(value["files"])
        seen = []
        for item in files:
            _object(item, {"path", "bytes_hex"})
            path = validate_path(item["path"], absolute=False)
            raw = _string(item["bytes_hex"], limit=RECORD_BYTE_LIMIT)
            _require(path in expected and re.fullmatch(r"(?:[0-9a-f]{2})*", raw) is not None,
                     "invalid launcher file contents")
            entry = expected[path]
            _require(len(raw) == entry["size"] * 2 and hashlib.sha256(bytes.fromhex(raw)).hexdigest() == entry["sha256"],
                     "launcher file differs from inventory")
            seen.append(path)
        _require(seen == sorted(expected), "launcher file contents are incomplete")
    return value


validate_launcher_state = _launcher_state

PLAN_PATH_KEYS = {"recovery_root", "environment", "snapshot", "forensic", "pipx", "base_interpreter",
    "exposed_command", "instance_lock", "diagnostic_log", "config_dir", "data_dir", "cache_dir",
    "user_home", "pipx_home", "pipx_shared_libs", "pipx_bin_dir", "pipx_man_dir", "pipx_completion_dir"}


def validate_plan(value):
    _object(value, COMMON | {"attempt_id", "old_version", "target_version", "parent_pid", "paths",
        "old_token", "target_wheel", "prior_provenance", "backup", "runtime", "wrapper", "launcher", "lock_identities", "pipx_identity"})
    _common(value)
    _digest(value["attempt_id"])
    _version(value["old_version"])
    _version(value["target_version"])
    _require(tuple(map(int, value["target_version"].split("."))) > tuple(map(int, value["old_version"].split("."))),
             "target version is not newer")
    _integer(value["parent_pid"], minimum=1)
    paths = _object(value["paths"], PLAN_PATH_KEYS)
    for path in paths.values():
        validate_path(path)
    root = PurePosixPath(paths["recovery_root"])
    _require(PurePosixPath(paths["environment"]) == PurePosixPath(paths["pipx_home"]) / "venvs/arxiv-digest",
             "plan environment layout differs")
    _require(PurePosixPath(paths["snapshot"]).parent == PurePosixPath(paths["pipx_home"]) / "arxiv-digest-update-snapshots"
             and PurePosixPath(paths["snapshot"]).name == value["attempt_id"]
             and PurePosixPath(paths["forensic"]) == PurePosixPath(paths["snapshot"]).with_name(value["attempt_id"] + ".failed"),
             "plan snapshot ownership differs")
    _require(PurePosixPath(paths["exposed_command"]) == PurePosixPath(paths["pipx_bin_dir"]) / "arxiv-digest",
             "plan exposed command differs")
    _require(PurePosixPath(paths["diagnostic_log"]) == root / "update-diagnostic.log",
             "plan log path differs")
    executable = _object(value["pipx_identity"], {"path", "device", "inode", "uid", "mode", "size", "sha256"})
    _require(executable["path"] == paths["pipx"], "pipx executable path differs")
    for key in ("device", "inode", "uid", "size"):
        _integer(executable[key])
    _integer(executable["mode"], maximum=0o7777)
    _require(executable["mode"] & 0o111 and not executable["mode"] & 0o7022, "unsafe pipx executable mode")
    _digest(executable["sha256"])
    validate_installation_token(value["old_token"])
    core = value["old_token"]["core"]
    _require(core["environment_path"] == paths["environment"]
             and core["exposed_link"]["path"] == paths["exposed_command"]
             and core["interpreter"]["path"] == paths["base_interpreter"], "plan old token paths differ")
    validate_wheel(value["target_wheel"])
    _require(value["target_wheel"]["version"] == value["target_version"], "target wheel version differs")
    _require(PurePosixPath(value["target_wheel"]["path"]).is_relative_to(root), "target wheel is outside recovery root")
    prior = value["prior_provenance"]
    _require((prior is None) == (value["old_token"]["provenance"] is None), "old provenance reference differs")
    if prior is not None:
        validate_provenance(prior)
        _require(prior["version"] == value["old_version"] and {**prior["core_token"],
                 "environment_identity": {**prior["core_token"]["environment_identity"], "inode": core["environment_identity"]["inode"]}} == core,
                 "prior provenance does not describe old installation")
        _require(hashlib.sha256(encode_provenance(prior)).hexdigest() == value["old_token"]["provenance"]["sha256"],
                 "prior provenance digest differs")
        _require(PurePosixPath(prior["wheel"]["path"]).is_relative_to(root), "prior wheel is outside recovery root")
    backup = validate_artifact(value["backup"], extra={"source_version", "data_generation", "pdf_destination"})
    _require(backup["source_version"] == value["old_version"]
             and type(backup["data_generation"]) is int and backup["data_generation"] == APPLICATION_DATA_GENERATION,
             "backup source differs")
    _require(PurePosixPath(backup["path"]).is_relative_to(root), "backup is outside recovery root")
    pdf = _object(backup["pdf_destination"], {"kind", "path"})
    _require(pdf["kind"] in {"downloads", "documents", "custom"}, "invalid PDF destination kind")
    validate_path(pdf["path"])
    runtime = _list(value["runtime"], limit=4)
    _require(len(runtime) == 4, "runtime inventory is incomplete")
    names = []
    for item in runtime:
        _object(item, {"name", "size", "sha256", "mode", "identity"})
        _require(item["name"] in RUNTIME_FILENAMES, "unknown runtime member")
        _integer(item["size"], minimum=1, maximum=RECORD_BYTE_LIMIT)
        _digest(item["sha256"])
        _require(item["mode"] == 0o600, "unsafe runtime member mode")
        validate_identity(item["identity"], file=True)
        _require(item["identity"]["mode"] == item["mode"] and item["identity"]["size"] == item["size"],
                 "runtime identity differs")
        names.append(item["name"])
    _require(names == sorted(RUNTIME_FILENAMES), "runtime inventory must have exactly the sorted fixed members")
    wrapper = _object(value["wrapper"], {"path", "size", "sha256", "mode", "identity"})
    _require(wrapper["path"] == str(root / "recover-arxiv-digest") and wrapper["mode"] == 0o700,
             "recovery wrapper path or mode differs")
    _integer(wrapper["size"], minimum=1, maximum=STRING_BYTE_LIMIT)
    _digest(wrapper["sha256"])
    validate_identity(wrapper["identity"], file=True)
    _require(wrapper["identity"]["mode"] == 0o700 and wrapper["identity"]["size"] == wrapper["size"],
             "recovery wrapper identity differs")
    launcher = _object(value["launcher"], {"prior", "intended"})
    _launcher_state(launcher["prior"])
    _launcher_state(launcher["intended"])
    _require(launcher["prior"]["path"] == launcher["intended"]["path"]
             and (launcher["prior"]["kind"] != "absent" or launcher["intended"]["kind"] == "absent"),
             "launcher ownership differs")
    locks = _object(value["lock_identities"], {"transition", "launcher", "instance"})
    for identity in locks.values():
        validate_identity(identity)
        _require(identity["mode"] == 0o600, "unsafe lock identity mode")
    return value


def validate_receipt(value, *, proposal=False):
    keys = {"receipt_id", "outcome", "installed_version", "attempted_version", "message_code", "attempt_id", "launch_id"}
    _object(value, keys if proposal else keys | {"unacknowledged"})
    for key in ("receipt_id", "attempt_id"):
        _digest(value[key])
    for key in ("installed_version", "attempted_version"):
        _version(value[key])
    outcome = value["outcome"]
    _require(type(outcome) is str and outcome in OUTCOME_MESSAGES, "unknown receipt outcome")
    _require(value["message_code"] == OUTCOME_MESSAGES[outcome], "receipt message differs")
    if outcome in {"updated", "restored"}:
        _digest(value["launch_id"])
    else:
        _require(value["launch_id"] is None, "failure receipt has unexpected launch nonce")
    if proposal:
        _require(outcome in {"updated", "restored"}, "invalid healthy proposal")
    else:
        _require(value["unacknowledged"] is True, "invalid pending receipt flag")
    return value


def validate_replay(value):
    _object(value, {"snapshot_path", "live_path", "forensic_path", "old_token", "partial_token",
        "prior_provenance", "target_started", "process_group_ids", "processes_dead", "partial_exposed", "partial_provenance"})
    for key in ("snapshot_path", "live_path", "forensic_path"):
        validate_path(value[key])
    _require(len({value[k] for k in ("snapshot_path", "live_path", "forensic_path")}) == 3,
             "replay paths overlap")
    validate_core_installation_token(value["old_token"])
    _require(value["old_token"]["environment_path"] == value["live_path"], "replay old environment differs")
    partial = value["partial_token"]
    if partial is not None:
        _object(partial, {"identity", "inventory"})
        validate_identity(partial["identity"])
        validate_inventory(partial["inventory"])
    if value["prior_provenance"] is not None:
        validate_provenance(value["prior_provenance"])
    if value["partial_exposed"] is not None:
        validate_exposed_link(value["partial_exposed"])
    if value["partial_provenance"] is not None:
        validate_record_reference(value["partial_provenance"])
    _boolean(value["target_started"])
    groups = _list(value["process_group_ids"], limit=16)
    for pid in groups:
        _integer(pid, minimum=1)
    _require(groups == sorted(set(groups)), "ambiguous process group set")
    _require(value["processes_dead"] is True, "replay lacks positive process teardown proof")
    return value


def validate_journal(value):
    _require(type(value) is dict and type(value.get("state")) is str and value["state"] in STATES,
             "unknown journal state")
    state = value["state"]
    keys = COMMON | {"attempt_id", "plan_sha256", "state"}
    optional = {"receipt"} if state in TERMINAL_STATES else set()
    if state == "installing":
        keys |= {"installer"}
    if state == "rolling_back":
        keys |= {"subphase", "replay"}
    if state == "healthy_pending_commit":
        keys |= {"proposal"}
    _object(value, keys, optional)
    _common(value)
    _digest(value["attempt_id"])
    _digest(value["plan_sha256"])
    if state == "installing":
        _object(value["installer"], {"guard_pid", "process_group_id"})
        for pid in value["installer"].values():
            _integer(pid, minimum=1)
    if state == "rolling_back":
        _require(value["subphase"] == "package_restore_pending", "unknown replay subphase")
        validate_replay(value["replay"])
    if state == "healthy_pending_commit":
        validate_receipt(value["proposal"], proposal=True)
        _require(value["proposal"]["attempt_id"] == value["attempt_id"], "proposal attempt differs")
    if "receipt" in value:
        receipt = validate_receipt(value["receipt"])
        allowed = {"updated", "restored"} if state == "complete" else {"handoff_failed"} if state == "aborted_no_mutation" else {state}
        _require(receipt["outcome"] in allowed and receipt["attempt_id"] == value["attempt_id"],
                 "receipt is incompatible with terminal state")
    return value


def _bounded_tree(value, depth=0, field=None):
    _require(depth <= 40, "updater record nesting exceeds bound")
    if type(value) is dict:
        _require(len(value) <= COLLECTION_LIMIT, "updater collection exceeds its bound")
        for key, item in value.items():
            _string(key)
            _bounded_tree(item, depth + 1, key)
    elif type(value) is list:
        _list(value)
        for item in value:
            _bounded_tree(item, depth + 1)
    elif type(value) is str:
        _string(value, limit=RECORD_BYTE_LIMIT if field in {"bytes_hex", "content_hex"} else STRING_BYTE_LIMIT)
    else:
        _require(value is None or type(value) in {bool, int}, "unsupported updater JSON value")
        if type(value) is int:
            _integer(value, minimum=-(1 << 63))


def _encode(value, validator, *, limit=None):
    _bounded_tree(value)
    validator(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("invalid updater JSON") from error
    _require(len(payload) <= (RECORD_BYTE_LIMIT if limit is None else limit), "updater record exceeds its byte limit")
    return payload


def _decode(payload, validator, *, limit=None):
    limit = RECORD_BYTE_LIMIT if limit is None else limit
    _require(type(payload) is bytes and 0 < len(payload) <= limit, "updater record exceeds its byte limit")
    def pairs(items):
        _require(len(items) <= COLLECTION_LIMIT, "updater collection exceeds its bound")
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate updater JSON key")
            result[key] = value
        return result
    def constant(_value):
        raise ProtocolError("nonfinite updater JSON value")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ProtocolError("invalid updater JSON") from error
    _require(_encode(value, validator, limit=limit) == payload, "updater JSON is not canonical")
    return value


def encode_inventory(value): return _encode(value, validate_inventory)
def decode_inventory(payload): return _decode(payload, validate_inventory)
def encode_core_installation_token(value): return _encode(value, validate_core_installation_token)
def decode_core_installation_token(payload): return _decode(payload, validate_core_installation_token)
def encode_installation_token(value): return _encode(value, validate_installation_token)
def decode_installation_token(payload): return _decode(payload, validate_installation_token)
def encode_plan(value): return _encode(value, validate_plan)
def decode_plan(payload): return _decode(payload, validate_plan)
def encode_journal(value): return _encode(value, validate_journal)
def decode_journal(payload): return _decode(payload, validate_journal)
def encode_provenance(value): return _encode(value, validate_provenance)
def decode_provenance(payload): return _decode(payload, validate_provenance)
def encode_receipt(value): return _encode(value, validate_receipt, limit=CONTROL_BYTE_LIMIT)
def decode_receipt(payload): return _decode(payload, validate_receipt, limit=CONTROL_BYTE_LIMIT)


@dataclass(frozen=True, slots=True)
class JournalSnapshot:
    record: dict
    canonical_bytes: bytes
    sha256: str
    identity: dict


# Plans and provenance deliberately use the same exact snapshot semantics.
PlanSnapshot = ProvenanceSnapshot = JournalSnapshot


def file_identity(metadata):
    return {"device": metadata.st_dev, "inode": metadata.st_ino,
            "uid": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns,
            "ctime_ns": metadata.st_ctime_ns, "nlink": metadata.st_nlink}


def _directory_identity(metadata):
    return {"device": metadata.st_dev, "inode": metadata.st_ino,
            "uid": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode)}


def _owned_record(metadata):
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= RECORD_BYTE_LIMIT):
        raise StoreError("protected updater record is unsafe")


class _ProtectedDirectory:
    def __init__(self, root, *, create=False):
        self.root = Path(validate_path(str(root)))
        flags = os.O_RDONLY | _required_open_flag("O_DIRECTORY") | _required_open_flag("O_NOFOLLOW") | getattr(os, "O_CLOEXEC", 0)
        self._chain = []
        try:
            descriptor = os.open("/", flags)
            self._chain.append((descriptor, "/", _directory_identity(os.fstat(descriptor))))
            for index, part in enumerate(self.root.parts[1:], start=1):
                parent = self._chain[-1][0]
                final = index == len(self.root.parts) - 1
                if create and final:
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                before = os.stat(part, dir_fd=parent, follow_symlinks=False)
                self._check_ancestor(before, private=final)
                descriptor = os.open(part, flags, dir_fd=parent)
                self._chain.append((descriptor, part, _directory_identity(before)))
                if _directory_identity(os.fstat(descriptor)) != _directory_identity(before):
                    raise StoreError("protected updater ancestor changed during open")
            self.fd = self._chain[-1][0]
            self.identity = self._chain[-1][2]
            self.verify()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check_ancestor(metadata, *, private=False):
        if private:
            _validate_private_directory(metadata)
        elif (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.getuid()}
                or metadata.st_mode & 0o022 and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)):
            raise StoreError("protected updater directory ancestor is unsafe")

    def verify(self):
        for index, (descriptor, name, expected) in enumerate(self._chain):
            named = os.stat("/", follow_symlinks=False) if index == 0 else os.stat(
                name, dir_fd=self._chain[index - 1][0], follow_symlinks=False)
            for metadata in (os.fstat(descriptor), named):
                self._check_ancestor(metadata, private=index == len(self._chain) - 1)
                if _directory_identity(metadata) != expected:
                    raise StoreError("protected updater parent identity changed")

    def close(self):
        for descriptor, _name, _identity in reversed(self._chain):
            os.close(descriptor)
        self._chain.clear()

    def read(self, name, decoder):
        self.verify()
        try:
            before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _owned_record(before)
        descriptor = os.open(name, os.O_RDONLY | _required_open_flag("O_NOFOLLOW")
                             | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=self.fd)
        try:
            expected = file_identity(before)
            if file_identity(os.fstat(descriptor)) != expected:
                raise StoreError("protected updater record identity changed")
            chunks = bytearray()
            while len(chunks) <= before.st_size:
                chunk = os.read(descriptor, min(65536, before.st_size + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            for metadata in (os.fstat(descriptor), os.stat(name, dir_fd=self.fd, follow_symlinks=False)):
                _owned_record(metadata)
                if file_identity(metadata) != expected:
                    raise StoreError("protected updater record changed during reading")
            self.verify()
            if len(chunks) != before.st_size:
                raise StoreError("protected updater record length changed")
            payload = bytes(chunks)
            record = decoder(payload)
            return JournalSnapshot(record, payload, hashlib.sha256(payload).hexdigest(), expected)
        finally:
            os.close(descriptor)

    def publish(self, name, payload, decoder, expected):
        self.verify()
        _check_expected(self.read(name, decoder), expected)
        temporary = "." + name + "." + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL
                             | _required_open_flag("O_NOFOLLOW") | getattr(os, "O_CLOEXEC", 0),
                             0o600, dir_fd=self.fd)
        created = None
        try:
            os.fchmod(descriptor, 0o600)
            created = file_identity(os.fstat(descriptor))
            cursor = 0
            while cursor < len(payload):
                written = os.write(descriptor, payload[cursor:])
                if written <= 0:
                    raise StoreError("protected updater write did not complete")
                cursor += written
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            reread = bytearray()
            while len(reread) <= len(payload):
                chunk = os.read(descriptor, min(65536, len(payload) + 1 - len(reread)))
                if not chunk:
                    break
                reread.extend(chunk)
            if bytes(reread) != payload:
                raise StoreError("protected updater write verification failed")
            decoder(bytes(reread))
            ready = file_identity(os.fstat(descriptor))
            _owned_record(os.fstat(descriptor))
            if file_identity(os.stat(temporary, dir_fd=self.fd, follow_symlinks=False)) != ready:
                raise StoreError("protected updater temporary identity changed")
            self.verify()
            _check_expected(self.read(name, decoder), expected)
            if expected is None:
                # Native no-replace rename is the only creation publication path.
                atomic_rename_noreplace(Path(temporary), Path(name), source_directory_fd=self.fd, destination_directory_fd=self.fd)
            else:
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
            self.verify()
            published = self.read(name, decoder)
            if (published is None or published.canonical_bytes != payload
                    or any(published.identity[k] != ready[k] for k in ("device", "inode", "uid", "mode", "size", "mtime_ns", "nlink"))):
                raise StoreError("protected updater destination verification failed")
            return published
        finally:
            os.close(descriptor)
            try:
                found = os.stat(temporary, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if created is not None and (found.st_dev, found.st_ino) == (created["device"], created["inode"]):
                    os.unlink(temporary, dir_fd=self.fd)
                    os.fsync(self.fd)

    def remove(self, name, decoder, expected):
        self.verify()
        current = self.read(name, decoder)
        _check_expected(current, expected)
        if current is not None:
            os.unlink(name, dir_fd=self.fd)
            os.fsync(self.fd)
            self.verify()
            if self.read(name, decoder) is not None:
                raise StoreError("protected updater record removal changed")


@contextmanager
def _directory(root, *, create=False):
    parent = _ProtectedDirectory(root, create=create)
    try:
        yield parent
    finally:
        parent.close()


@contextmanager
def _journal_lock(root, *, timeout=45.0):
    # No other application lock may be acquired while this scope is held.
    # Close-only release also keeps borrowed descriptors from unlocking a peer.
    with _directory(root) as parent:
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | _required_open_flag("O_NOFOLLOW") | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(JOURNAL_LOCK_FILENAME, flags, 0o600, dir_fd=parent.fd)
        try:
            metadata = os.fstat(descriptor)
            _validate_private_lock_file(metadata)
            identity = _private_identity(metadata)
            deadline = time.monotonic() + timeout
            while True:
                parent.verify()
                current = os.stat(JOURNAL_LOCK_FILENAME, dir_fd=parent.fd, follow_symlinks=False)
                _validate_private_lock_file(current)
                if _private_identity(current) != identity:
                    raise StoreError("journal lock identity changed")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreError("journal lock timed out")
                    time.sleep(min(0.005, remaining))
                except InterruptedError:
                    continue
            parent.verify()
            current = os.stat(JOURNAL_LOCK_FILENAME, dir_fd=parent.fd, follow_symlinks=False)
            if _private_identity(current) != identity:
                raise StoreError("journal lock identity changed")
            yield parent
        finally:
            os.close(descriptor)


def _check_expected(current, expected):
    if expected is not None:
        if type(expected) is not JournalSnapshot:
            raise StaleSnapshotError("expected an exact updater snapshot")
        if (hashlib.sha256(expected.canonical_bytes).hexdigest() != expected.sha256
                or json.loads(expected.canonical_bytes) != expected.record):
            raise StaleSnapshotError("supplied updater snapshot was modified")
    if current != expected:
        raise StaleSnapshotError("updater snapshot changed")


def _admission_allowed(journal):
    if journal is None:
        return
    if "receipt" in journal.record:
        raise PendingReceiptError()
    if journal.record["state"] not in {"complete", "aborted_no_mutation"}:
        raise StoreError("update journal does not permit a new attempt")


class ProtectedPlanStore:
    def __init__(self, root):
        self.root = Path(root)

    def read_snapshot(self):
        with _directory(self.root) as parent:
            return parent.read(PLAN_FILENAME, decode_plan)

    def publish(self, record):
        payload = encode_plan(record)
        if Path(record["paths"]["recovery_root"]) != self.root:
            raise StoreError("plan does not belong to fixed recovery root")
        with _journal_lock(self.root) as parent:
            journal = parent.read(JOURNAL_FILENAME, decode_journal)
            _admission_allowed(journal)
            prior = parent.read(PLAN_FILENAME, decode_plan)
            if journal is not None:
                _bind_plan(journal, prior)
            return parent.publish(PLAN_FILENAME, payload, decode_plan, prior)


class ProtectedProvenanceStore:
    def __init__(self, root):
        self.root = Path(root)

    def read_snapshot(self):
        with _directory(self.root) as parent:
            result = parent.read(PROVENANCE_FILENAME, decode_provenance)
            if result is not None:
                self._check_root(result.record)
            return result

    def _check_root(self, record):
        if not PurePosixPath(record["wheel"]["path"]).is_relative_to(PurePosixPath(str(self.root))):
            raise StoreError("provenance wheel is outside fixed recovery root")

    def compare_and_swap(self, expected, record):
        if record is not None:
            self._check_root(record)
            payload = encode_provenance(record)
        with _journal_lock(self.root) as parent:
            if record is None:
                parent.remove(PROVENANCE_FILENAME, decode_provenance, expected)
                return None
            return parent.publish(PROVENANCE_FILENAME, payload, decode_provenance, expected)


ProvenanceStore = ProtectedProvenanceStore


SNAPSHOT_CLEANUP_PREFIX = "snapshot-cleanup-"
SNAPSHOT_CLEANUP_TIMEOUT_SECONDS = 5.0
SNAPSHOT_CLEANUP_RECORD_LIMIT = 128


def validate_snapshot_cleanup(value):
    _object(value, COMMON | {"attempt_id", "plan_sha256", "snapshot_path", "root_identity",
                            "entries", "receipt_id", "provenance"})
    _common(value)
    _digest(value["attempt_id"])
    _digest(value["plan_sha256"])
    snapshot = PurePosixPath(validate_path(value["snapshot_path"]))
    _require(snapshot.name == value["attempt_id"] and snapshot.parent.name == "arxiv-digest-update-snapshots",
             "snapshot cleanup path differs from attempt")
    validate_identity(value["root_identity"])
    if value["receipt_id"] is not None:
        _digest(value["receipt_id"])
    if value["provenance"] is not None:
        validate_record_reference(value["provenance"])
    entries = _list(value["entries"])
    inventory = {"root_mode": value["root_identity"]["mode"], "entries": []}
    for item in entries:
        _object(item, {"entry", "identity"})
        entry = item["entry"]
        _require(type(entry) is dict and entry.get("kind") in {"directory", "file", "symlink"},
                 "unknown snapshot cleanup object")
        validate_identity(item["identity"], file=entry["kind"] != "directory")
        inventory["entries"].append(entry)
    validate_inventory(inventory)
    for item in entries:
        entry, identity = item["entry"], item["identity"]
        _require(identity["mode"] == entry["mode"], "snapshot cleanup mode differs")
        if entry["kind"] == "file":
            _require(identity["size"] == entry["size"], "snapshot cleanup file size differs")
        if entry["kind"] == "symlink":
            _require(identity["size"] == len(entry["target"].encode("utf-8")), "snapshot cleanup symlink size differs")
    return value


def encode_snapshot_cleanup(value):
    return _encode(value, validate_snapshot_cleanup)


def decode_snapshot_cleanup(payload):
    return _decode(payload, validate_snapshot_cleanup)


class SnapshotCleanupStore:
    """Immutable deletion intent admitted only against exact healthy records."""
    def __init__(self, root):
        self.root = Path(validate_path(str(root)))

    @staticmethod
    def _name(attempt_id):
        _digest(attempt_id)
        return SNAPSHOT_CLEANUP_PREFIX + attempt_id + ".json"

    def read_snapshot(self, attempt_id):
        with _directory(self.root) as parent:
            saved = parent.read(self._name(attempt_id), decode_snapshot_cleanup)
            if saved is not None and saved.record["attempt_id"] != attempt_id:
                raise StoreError("snapshot cleanup attempt differs")
            return saved

    def list_snapshots(self, *, deadline_at=None):
        deadline_at = time.monotonic() + SNAPSHOT_CLEANUP_TIMEOUT_SECONDS if deadline_at is None else deadline_at
        found = []
        with _directory(self.root) as parent:
            parent.verify()
            with os.scandir(parent.fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= COLLECTION_LIMIT or len(found) >= SNAPSHOT_CLEANUP_RECORD_LIMIT or time.monotonic() >= deadline_at:
                        break
                    match = re.fullmatch(SNAPSHOT_CLEANUP_PREFIX + r"([0-9a-f]{64})\.json", entry.name)
                    if match is None:
                        continue
                    try:
                        saved = parent.read(entry.name, decode_snapshot_cleanup)
                        if saved is not None and saved.record["attempt_id"] == match.group(1):
                            found.append(saved)
                    except (OSError, ValueError, RuntimeError):
                        # Unknown or changed records grant no deletion authority.
                        continue
            parent.verify()
        return tuple(sorted(found, key=lambda item: item.record["attempt_id"]))

    def publish(self, record, *, expected_plan, expected_journal, expected_provenance):
        payload = encode_snapshot_cleanup(record)
        record = decode_snapshot_cleanup(payload)
        with _journal_lock(self.root) as parent:
            plan = parent.read(PLAN_FILENAME, decode_plan)
            journal = JournalStore(self.root)._read(parent)
            provenance = parent.read(PROVENANCE_FILENAME, decode_provenance)
            _check_expected(plan, expected_plan)
            _check_expected(journal, expected_journal)
            _check_expected(provenance, expected_provenance)
            if (plan is None or journal is None or journal.record["state"] != "complete"
                    or record["attempt_id"] != plan.record["attempt_id"] or record["plan_sha256"] != plan.sha256
                    or record["snapshot_path"] != plan.record["paths"]["snapshot"]):
                raise StoreError("snapshot cleanup lacks terminal plan authority")
            reference = None if provenance is None else {"identity": provenance.identity, "sha256": provenance.sha256}
            inventory = plan.record["old_token"]["core"]["inventory"]
            if (record["receipt_id"] != journal.record.get("receipt", {}).get("receipt_id")
                    or record["provenance"] != reference
                    or [item["entry"] for item in record["entries"]] != inventory["entries"]
                    or record["root_identity"]["mode"] != inventory["root_mode"]
                    or record["root_identity"] == plan.record["old_token"]["core"]["environment_identity"]
                    or record["snapshot_path"] in {plan.record["paths"]["environment"], plan.record["paths"]["forensic"]}):
                raise StoreError("snapshot cleanup inventory authority differs")
            return parent.publish(self._name(record["attempt_id"]), payload, decode_snapshot_cleanup, None)

    def remove(self, expected):
        if not isinstance(expected, JournalSnapshot):
            raise StoreError("snapshot cleanup expected record is required")
        with _journal_lock(self.root) as parent:
            journal = JournalStore(self.root)._read(parent)
            if journal is None or journal.record["state"] != "complete":
                raise StoreError("snapshot cleanup cannot finish during an active or blocking update")
            name = self._name(expected.record["attempt_id"])
            _check_expected(parent.read(name, decode_snapshot_cleanup), expected)
            try:
                Path(expected.record["snapshot_path"]).lstat()
            except FileNotFoundError:
                pass
            else:
                raise StoreError("snapshot cleanup directory still exists")
            parent.remove(name, decode_snapshot_cleanup, expected)


def _bind_plan(journal, plan):
    if plan is None:
        raise StoreError("journal protected plan is absent")
    record = journal.record
    if record["plan_sha256"] != plan.sha256 or record["attempt_id"] != plan.record["attempt_id"]:
        raise StoreError("journal does not match its fixed protected plan")
    for field in COMMON:
        if record[field] != plan.record[field]:
            raise StoreError("journal protocol differs from its protected plan")
    envelope = record.get("proposal", record.get("receipt"))
    if envelope is not None:
        wanted = plan.record["target_version"] if envelope["outcome"] == "updated" else plan.record["old_version"]
        if (envelope["installed_version"] != wanted
                or envelope["attempted_version"] != plan.record["target_version"]):
            raise StoreError("journal receipt version differs from protected plan")
    if record["state"] == "rolling_back":
        replay = record["replay"]
        paths = plan.record["paths"]
        if (replay["snapshot_path"] != paths["snapshot"] or replay["live_path"] != paths["environment"]
                or replay["forensic_path"] != paths["forensic"]
                or replay["old_token"] != plan.record["old_token"]["core"]
                or replay["prior_provenance"] != plan.record["prior_provenance"]):
            raise StoreError("journal replay differs from its protected plan")


@dataclass(frozen=True, slots=True)
class InstallerAuthorization:
    snapshot_sha256: str
    attempt_id: str
    installer: dict


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    snapshot_sha256: str
    attempt_id: str
    replay: dict
    explicit: bool = False


@dataclass(frozen=True, slots=True)
class NoInstallAuthorization:
    snapshot_sha256: str
    attempt_id: str
    no_installer_began: bool
    exact_old_installation: bool
    launcher_restored: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ExternalChangeAuthorization:
    snapshot_sha256: str
    attempt_id: str
    conflict: str


@dataclass(frozen=True, slots=True)
class FailureAuthorization:
    snapshot_sha256: str
    attempt_id: str
    processes_dead: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ReplayAuthorization:
    snapshot_sha256: str
    attempt_id: str
    replay: dict


def _authorize(authorization, expected, kind):
    if (type(authorization) is not kind
            or authorization.snapshot_sha256 != expected.sha256
            or authorization.attempt_id != expected.record["attempt_id"]):
        raise StoreError("journal exceptional edge lacks exact typed authorization")


def _edge(expected, target, authorization):
    source = expected.record
    before, after = source["state"], target["state"]
    common = COMMON | {"attempt_id", "plan_sha256"}
    if any(source[k] != target[k] for k in common):
        raise StoreError("journal transition changed attempt identity")
    ordinary = {("prepared", "committed"), ("prepared", "canceling_no_install"),
        ("canceling_no_install", "aborted_no_mutation"),
        ("installing", "target_installed"), ("target_installed", "launching_target")}
    if (before, after) == ("committed", "installing"):
        _authorize(authorization, expected, InstallerAuthorization)
        if target["installer"] != authorization.installer:
            raise StoreError("installer process identity differs from authorization")
        return
    if (before, after) in ordinary:
        if authorization is not None:
            raise StoreError("ordinary journal edge has unexpected authorization")
        return
    if after == "healthy_pending_commit" and before in {"launching_target", "rolling_back"}:
        wanted = "updated" if before == "launching_target" else "restored"
        if authorization is not None or target["proposal"]["outcome"] != wanted:
            raise StoreError("healthy proposal differs from incoming edge")
        return
    if (before, after) == ("healthy_pending_commit", "complete"):
        if authorization is not None or target.get("receipt") != {**source["proposal"], "unacknowledged": True}:
            raise StoreError("terminal promotion must preserve exact receipt proposal")
        return
    if after == "rolling_back" and before in {"committed", "installing", "target_installed", "launching_target", "healthy_pending_commit", *BLOCKING_STATES}:
        _authorize(authorization, expected, RecoveryAuthorization)
        validate_replay(authorization.replay)
        if target["replay"] != authorization.replay or (before in BLOCKING_STATES and authorization.explicit is not True):
            raise StoreError("recovery admission lacks protected replay authorization")
        return
    if (before, after) == ("committed", "aborted_no_mutation"):
        _authorize(authorization, expected, NoInstallAuthorization)
        if (authorization.no_installer_began is not True or authorization.exact_old_installation is not True
                or authorization.launcher_restored is not True
                or authorization.reason not in {"parent_exit_timeout", "instance_lock_timeout", "helper_commit_aborted"}):
            raise StoreError("no-install abort is not proven")
        return
    if after == "external_change_detected" and before not in TERMINAL_STATES:
        _authorize(authorization, expected, ExternalChangeAuthorization)
        if authorization.conflict not in {"ownership", "inventory", "launcher", "provenance", "exposed_link"}:
            raise StoreError("unknown external installation conflict")
        return
    if after == "recovery_failed" and before in {"committed", "installing", "target_installed", "launching_target", "healthy_pending_commit", "rolling_back"}:
        _authorize(authorization, expected, FailureAuthorization)
        if authorization.processes_dead is not True or authorization.reason not in {"package_restore", "data_restore", "self_check", "relaunch", "post_install"}:
            raise StoreError("recovery failure lacks safe teardown proof")
        return
    if before == after == "rolling_back":
        _authorize(authorization, expected, ReplayAuthorization)
        if target["replay"] != authorization.replay:
            raise StoreError("replay rewrite differs from authorization")
        # Protected old inputs are invariant even when captured partial facts advance.
        for key in ("snapshot_path", "live_path", "forensic_path", "old_token", "prior_provenance", "target_started", "process_group_ids"):
            if source["replay"][key] != target["replay"][key]:
                raise StoreError("replay rewrite changed protected recovery facts")
        return
    raise StoreError("journal transition is not permitted")


class JournalStore:
    def __init__(self, root):
        self.root = Path(root)

    def _read(self, parent):
        journal = parent.read(JOURNAL_FILENAME, decode_journal)
        if journal is not None:
            plan = parent.read(PLAN_FILENAME, decode_plan)
            if plan is not None and Path(plan.record["paths"]["recovery_root"]) != self.root:
                raise StoreError("journal plan root differs")
            _bind_plan(journal, plan)
        return journal

    def read_snapshot(self):
        with _journal_lock(self.root) as parent:
            return self._read(parent)

    def validate_snapshot(self, expected):
        with _journal_lock(self.root) as parent:
            current = self._read(parent)
            _check_expected(current, expected)
            return current

    def admit(self, plan_snapshot):
        with _journal_lock(self.root) as parent:
            # The fixed path is authoritative, never a caller-selected path or
            # record object; compare its full identity and reread canonical bytes.
            plan = parent.read(PLAN_FILENAME, decode_plan)
            _check_expected(plan, plan_snapshot)
            if plan is None or Path(plan.record["paths"]["recovery_root"]) != self.root:
                raise StoreError("protected plan is unavailable")
            prior = parent.read(JOURNAL_FILENAME, decode_journal)
            _admission_allowed(prior)
            record = {key: plan.record[key] for key in COMMON}
            record.update(attempt_id=plan.record["attempt_id"], plan_sha256=plan.sha256, state="prepared")
            return parent.publish(JOURNAL_FILENAME, encode_journal(record), decode_journal, prior)

    def transition(self, expected, record, *, authorization=None):
        payload = encode_journal(record)
        with _journal_lock(self.root) as parent:
            current = self._read(parent)
            _check_expected(current, expected)
            if current is None:
                raise StoreError("journal is absent")
            _edge(current, record, authorization)
            candidate = JournalSnapshot(record, payload, hashlib.sha256(payload).hexdigest(), {})
            _bind_plan(candidate, parent.read(PLAN_FILENAME, decode_plan))
            return parent.publish(JOURNAL_FILENAME, payload, decode_journal, current)

    def receipt(self, installed_version):
        _version(installed_version)
        snapshot = self.read_snapshot()
        if snapshot is None or "receipt" not in snapshot.record:
            return None
        receipt = snapshot.record["receipt"]
        if receipt["installed_version"] != installed_version:
            raise StoreError("receipt does not match the serving installed version")
        return {key: receipt[key] for key in ("receipt_id", "outcome", "installed_version", "attempted_version", "message_code")}

    def acknowledge(self, receipt_id):
        _digest(receipt_id)
        with _journal_lock(self.root) as parent:
            current = self._read(parent)
            if current is None or "receipt" not in current.record:
                return False
            if current.record["receipt"]["receipt_id"] != receipt_id:
                return False
            record = {key: value for key, value in current.record.items() if key != "receipt"}
            parent.publish(JOURNAL_FILENAME, encode_journal(record), decode_journal, current)
            return True


def classify_journal(root):
    """Return allow/recover/block; only ENOENT establishes absence.

    This classifies durable state only. The application must independently prove
    a fixed recovery runtime runnable before turning recover into an exec.
    """
    root = Path(root)
    try:
        with _directory(root) as parent:
            journal = parent.read(JOURNAL_FILENAME, decode_journal)
            if journal is None:
                return "allow"
            plan = parent.read(PLAN_FILENAME, decode_plan)
            _bind_plan(journal, plan)
            if plan is None or Path(plan.record["paths"]["recovery_root"]) != root:
                return "block"
            state = journal.record["state"]
            return "allow" if state in {"complete", "aborted_no_mutation"} else "block" if state in BLOCKING_STATES else "recover"
    except FileNotFoundError:
        # An absent root is ordinary pristine startup. Missing records inside an
        # otherwise present journal are caught by binding and cannot be guessed.
        try:
            root.lstat()
        except FileNotFoundError:
            return "allow"
        except OSError:
            return "block"
        return "block"
    except (OSError, ValueError, TypeError, KeyError):
        return "block"


RAW_MEMBER_NAMES = ("database", "profile", "restore_journal", "shm", "wal")
RAW_PHASES = {"preserved", "clearing", "publishing", "complete", "reverting", "reverted"}
RAW_FILENAME = "raw-recovery.json"
CATALOG_FILENAME = "update-artifacts.json"


def validate_raw_recovery(value):
    _object(value, COMMON | {"attempt_id", "plan_sha256", "created_at_ns", "phase", "members", "replacement"})
    _common(value)
    _digest(value["attempt_id"])
    _digest(value["plan_sha256"])
    _integer(value["created_at_ns"])
    _require(value["phase"] in RAW_PHASES, "unknown raw recovery phase")
    _object(value["members"], set(RAW_MEMBER_NAMES))
    for member in value["members"].values():
        _require(type(member) is dict and member.get("kind") in {"absent", "file"}, "invalid raw member")
        if member["kind"] == "absent":
            _object(member, {"kind"})
        else:
            _object(member, {"kind", "size", "sha256", "original_identity", "saved_identity"})
            _integer(member["size"])
            _digest(member["sha256"])
            for key in ("original_identity", "saved_identity"):
                validate_identity(member[key], file=True)
                _require(member[key]["mode"] == 0o600 and member[key]["size"] == member["size"], "unsafe raw member identity")
    _object(value["replacement"], {"database", "profile"})
    for digest in value["replacement"].values():
        _digest(digest)
    return value


def encode_raw_recovery(value):
    return _encode(value, validate_raw_recovery)


def decode_raw_recovery(payload):
    return _decode(payload, validate_raw_recovery)


class RawRecoveryStore:
    """An attempt's opaque inventory, serialized under the one journal lock."""

    def __init__(self, root, attempt_id):
        _digest(attempt_id)
        self.root = Path(root)
        self.path = self.root / "raw" / attempt_id

    def read_snapshot(self):
        with _journal_lock(self.root):
            with _directory(self.path) as parent:
                return parent.read(RAW_FILENAME, decode_raw_recovery)

    def compare_and_swap(self, expected, record):
        _require(record["attempt_id"] == self.path.name, "raw recovery attempt differs")
        payload = encode_raw_recovery(record)
        with _journal_lock(self.root):
            with _directory(self.path) as parent:
                return parent.publish(RAW_FILENAME, payload, decode_raw_recovery, expected)


def validate_artifact_catalog(value):
    _object(value, COMMON | {"artifacts"})
    _common(value)
    prior = None
    paths = set()
    for entry in _list(value["artifacts"]):
        validate_artifact(entry, extra={"kind", "attempt_id", "version", "created_at_ns"})
        _require(entry["kind"] in {"wheel", "backup", "raw"}, "unknown updater artifact")
        _digest(entry["attempt_id"])
        _version(entry["version"])
        _integer(entry["created_at_ns"])
        order = (entry["created_at_ns"], entry["path"])
        _require((prior is None or prior < order) and entry["path"] not in paths, "unordered or duplicate updater artifacts")
        paths.add(entry["path"])
        prior = order
    return value


def encode_artifact_catalog(value):
    return _encode(value, validate_artifact_catalog)


def decode_artifact_catalog(payload):
    return _decode(payload, validate_artifact_catalog)


class ArtifactCatalogStore:
    def __init__(self, root):
        self.root = Path(root)

    def read_snapshot(self):
        with _journal_lock(self.root) as parent:
            return parent.read(CATALOG_FILENAME, decode_artifact_catalog)

    def compare_and_swap(self, expected, record):
        with _journal_lock(self.root) as parent:
            return parent.publish(CATALOG_FILENAME, encode_artifact_catalog(record), decode_artifact_catalog, expected)

    def prune_terminal(self):
        return prune_update_artifacts(self.root)


CONTROL_BASE = {"schema_version", "attempt_id", "nonce", "kind"}
CONTROL_FIELDS = {
    "GUARD_BOOT": {"plan_sha256", "transition_fd"},
    "GUARD_STARTED": {"pid", "pgid"},
    "GUARD_RESULT": {"returncode", "process_group_id", "processes_dead", "timed_out", "log"},
    "GUARD_CANCEL": set(),
    "READY": set(), "COMMIT": set(), "COMMITTED": set(), "CANCEL": set(), "CANCELED": set(),
    "HEALTH": {"launch_id", "pid", "port", "startup_nonce", "token"},
    "OPEN": {"launch_id", "outcome", "ownership"},
    "HEALTHY_READY": {"launch_id", "journal_sha256", "provenance_sha256"},
    "UNLOCKED": {"launch_id", "journal_sha256"},
    "BOOT": {"mode", "plan_sha256", "launch_id", "outcome", "lock_fds"},
    "SELF_CHECK": {"version", "updater_protocol", "application_data_generation", "ok"},
    "DATA_RECOVERED": {"version", "ok"},
}


def validate_control(value):
    _require(type(value) is dict and type(value.get("kind")) is str and value["kind"] in CONTROL_FIELDS,
             "unknown control message")
    kind = value["kind"]
    _object(value, CONTROL_BASE | CONTROL_FIELDS[kind])
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1, "unsupported control schema")
    _digest(value["attempt_id"])
    _digest(value["nonce"])
    if kind == "GUARD_BOOT":
        _digest(value["plan_sha256"])
        _integer(value["transition_fd"], minimum=3, maximum=1_000_000)
    if kind == "GUARD_STARTED":
        _integer(value["pid"], minimum=1)
        _integer(value["pgid"], minimum=1)
        _require(value["pid"] == value["pgid"], "guard process group differs")
    if kind == "GUARD_RESULT":
        _integer(value["returncode"], minimum=-255, maximum=255)
        _integer(value["process_group_id"], minimum=1)
        _require(value["processes_dead"] is True, "guard lacks teardown proof")
        _boolean(value["timed_out"])
        validate_record_reference(value["log"])
        _require(value["log"]["identity"]["size"] <= PRIVATE_LOG_BYTE_LIMIT,
                 "private diagnostic log exceeds bound")
    if kind in {"HEALTH", "OPEN", "HEALTHY_READY", "UNLOCKED"}:
        _digest(value["launch_id"])
    if kind in {"HEALTHY_READY", "UNLOCKED"}:
        _digest(value["journal_sha256"])
    if kind == "HEALTHY_READY" and value["provenance_sha256"] is not None:
        _digest(value["provenance_sha256"])
    if kind == "HEALTH":
        _integer(value["pid"], minimum=1)
        _integer(value["port"], minimum=1, maximum=65535)
        _require(type(value["startup_nonce"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value["startup_nonce"]) is not None,
                 "invalid startup nonce")
        _require(type(value["token"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{43}", value["token"]) is not None,
                 "invalid health token")
    if kind == "OPEN":
        _require(value["outcome"] in {"updated", "restored"} and value["ownership"] is True,
                 "invalid relaunch ownership decision")
    if kind == "BOOT":
        _require(value["mode"] in {"self-check", "recover-data", "relaunch", "postterminal-relaunch"}, "unknown internal mode")
        _digest(value["plan_sha256"])
        if value["mode"] == "relaunch":
            _digest(value["launch_id"])
            _require(value["outcome"] in {"updated", "restored"}, "invalid relaunch outcome")
            lock_names = {"instance"}
        else:
            _require(value["launch_id"] is None and value["outcome"] is None, "unexpected internal relaunch fields")
            lock_names = {} if value["mode"] == "postterminal-relaunch" else {"transition"} if value["mode"] == "self-check" else {"transition", "launcher", "instance"}
        _object(value["lock_fds"], lock_names)
        for fd in value["lock_fds"].values():
            _integer(fd, minimum=3, maximum=1_000_000)
        _require(len(set(value["lock_fds"].values())) == len(lock_names), "duplicate internal lock descriptor")
    if kind in {"SELF_CHECK", "DATA_RECOVERED"}:
        _version(value["version"])
        _require(value["ok"] is True, "internal success record must report true")
    if kind == "SELF_CHECK":
        _require(type(value["updater_protocol"]) is int and value["updater_protocol"] == 1
                 and type(value["application_data_generation"]) is int and value["application_data_generation"] == 2,
                 "self-check protocol differs")
    return value


def encode_control(value): return _encode(value, validate_control, limit=CONTROL_BYTE_LIMIT)
def decode_control(payload): return _decode(payload, validate_control, limit=CONTROL_BYTE_LIMIT)


class ControlChannel:
    """One-owner canonical LF-framed channel with bounded I/O and buffering."""
    def __init__(self, sock):
        import socket
        if not isinstance(sock, socket.socket) or sock.family != socket.AF_UNIX or sock.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
            raise ProtocolError("internal channel must be a Unix stream socket")
        sock.set_inheritable(False)
        self.socket = sock
        self._buffer = bytearray()

    def _remaining(self, deadline_at):
        import math
        if type(deadline_at) not in {int, float} or not math.isfinite(deadline_at):
            raise ProtocolError("control deadline is invalid")
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("control deadline expired")
        return remaining

    def send(self, message, deadline_at):
        payload = encode_control(message)
        self.socket.settimeout(self._remaining(deadline_at))
        self.socket.sendall(payload)

    def receive(self, deadline_at):
        while True:
            self._remaining(deadline_at)
            position = self._buffer.find(b"\n")
            if position >= 0:
                payload = bytes(self._buffer[:position + 1])
                del self._buffer[:position + 1]
                return decode_control(payload)
            if len(self._buffer) >= CONTROL_BYTE_LIMIT:
                raise ProtocolError("control message exceeds byte limit")
            self.socket.settimeout(self._remaining(deadline_at))
            chunk = self.socket.recv(min(4096, CONTROL_BYTE_LIMIT - len(self._buffer)))
            if not chunk:
                raise EOFError("internal control channel closed")
            self._buffer.extend(chunk)

    def close(self):
        self.socket.close()


# Copied retention engine: only registered, byte-verified updater artifacts.
from contextlib import ExitStack


def _artifact_fsync_directory(path):
    with _directory(path) as parent:
        os.fsync(parent.fd)


def _artifact_read_file(path, *, links=False):
    """Hash a stable no-follow file; absence is distinguished from every error."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink not in ({1, 2} if links else {1})):
            raise StoreError("unsafe recovery file")
        digest = hashlib.sha256()
        remaining = before.st_size + 1
        while remaining and (chunk := os.read(fd, min(64 * 1024, remaining))):
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining != 1:
            raise StoreError("recovery file length changed")
        identity = file_identity(before)
        if identity != file_identity(os.fstat(fd)) or identity != file_identity(path.lstat()):
            raise StoreError("recovery file changed")
        return {"identity": identity, "size": before.st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _artifact_verify_saved(raw, record):
    for name, member in record["members"].items():
        saved = _artifact_read_file(raw / name)
        if member["kind"] == "absent":
            if saved is not None:
                raise StoreError("unexpected raw recovery member")
        elif saved is None or saved != {"identity": member["saved_identity"], "size": member["size"], "sha256": member["sha256"]}:
            raise StoreError("raw recovery inventory changed")
    for name, digest in record["replacement"].items():
        if (_artifact_read_file(raw / ("replacement." + name)) or {}).get("sha256") != digest:
            raise StoreError("replacement recovery file changed")


def _artifact_owned_path(root, entry):
    path = Path(entry["path"])
    attempt = entry["attempt_id"]
    version = entry["version"]
    if entry["kind"] == "wheel":
        valid = path == root / "attempts" / attempt / f"arxiv_digest-{version}-py3-none-any.whl"
    elif entry["kind"] == "backup":
        valid = path.parent == root / "backups" and re.fullmatch(
            r"update-backup-[0-9]{8}T[0-9]{6}Z-" + attempt + r"\.zip", path.name) is not None
    else:
        valid = path == root / "raw" / attempt / RAW_FILENAME
    if not valid:
        raise StoreError("artifact path is not updater-owned")
    return path


@contextmanager
def _artifact_owned_parent(root, path):
    """Anchor each private ancestor; no symlink can redirect a deletion."""
    with ExitStack() as stack:
        parents = [stack.enter_context(_directory(root))]
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            parents.append(stack.enter_context(_directory(current)))
        yield parents[-1]
        for parent in reversed(parents):
            parent.verify()


def _artifact_inspect(root, entry):
    path = _artifact_owned_path(root, entry)
    with _artifact_owned_parent(root, path):
        return _artifact_inspect_anchored(path, entry)


def _artifact_inspect_anchored(path, entry):
    current = _artifact_read_file(path)
    if current != {key: entry[key] for key in ("identity", "size", "sha256")}:
        raise StoreError("registered updater artifact changed")
    # Registration inspected the portable backup; exact identity and hash above
    # prove those already-validated bytes still match without importing the app.
    if entry["kind"] == "raw":
        with _directory(path.parent) as parent:
            snapshot = parent.read(RAW_FILENAME, decode_raw_recovery)
        if snapshot is None or snapshot.record["attempt_id"] != entry["attempt_id"] or snapshot.record["phase"] not in {"complete", "reverted"}:
            raise StoreError("raw recovery is not finalized")
        _artifact_verify_saved(path.parent, snapshot.record)
        names = {RAW_FILENAME, "replacement.database", "replacement.profile"}
        names.update(name for name, member in snapshot.record["members"].items() if member["kind"] == "file")
        if set(os.listdir(path.parent)) != names:
            raise StoreError("raw recovery has unowned contents")
        return snapshot.record, names
    return None


def prune_update_artifacts(root):
    """Prune under the journal lock only after durable healthy provenance.

    Active, blocking, absent or invalid journals grant no deletion authority.
    Invalid artifacts are preserved and do not count toward the two valid sets.
    """
    root = Path(root)
    removed = []
    with _journal_lock(root) as parent:
        journal = JournalStore(root)._read(parent)
        if journal is None or journal.record["state"] != "complete":
            return removed
        provenance = parent.read(PROVENANCE_FILENAME, decode_provenance)
        if provenance is None:
            return removed
        # Re-fsync and reread the actual provenance file before any pruning.
        fd = os.open(PROVENANCE_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent.fd)
        try:
            if file_identity(os.fstat(fd)) != provenance.identity:
                raise StoreError("provenance changed before pruning")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent.fd)
        _check_expected(parent.read(PROVENANCE_FILENAME, decode_provenance), provenance)
        plan = parent.read(PLAN_FILENAME, decode_plan)
        if provenance.record["version"] not in {plan.record["old_version"], plan.record["target_version"]}:
            raise StoreError("healthy provenance differs from completed plan")
        catalog = parent.read(CATALOG_FILENAME, decode_artifact_catalog)
        if catalog is None:
            return removed
        valid = {}
        for entry in catalog.record["artifacts"]:
            try:
                valid[entry["path"]] = _artifact_inspect(root, entry)
            except (OSError, ValueError):
                continue
        keep = {provenance.record["wheel"]["path"]}
        for kind in ("backup", "raw"):
            candidates = [entry for entry in catalog.record["artifacts"] if entry["kind"] == kind and entry["path"] in valid]
            keep.update(entry["path"] for entry in candidates[-2:])
        retained = []
        for entry in catalog.record["artifacts"]:
            if entry["path"] in keep or entry["path"] not in valid:
                retained.append(entry)
                continue
            path = _artifact_owned_path(root, entry)
            _artifact_inspect(root, entry)
            if entry["kind"] == "raw":
                _, names = valid[entry["path"]]
                with _artifact_owned_parent(root, path) as raw_parent:
                    _artifact_inspect_anchored(path, entry)
                    for name in sorted(names - {RAW_FILENAME}):
                        os.unlink(name, dir_fd=raw_parent.fd)
                    os.fsync(raw_parent.fd)
                    os.unlink(path.name, dir_fd=raw_parent.fd)
                    os.fsync(raw_parent.fd)
                with _directory(path.parent.parent) as raw_collection:
                    os.rmdir(path.parent.name, dir_fd=raw_collection.fd)
                    os.fsync(raw_collection.fd)
                _artifact_fsync_directory(path.parent.parent)
            else:
                with _artifact_owned_parent(root, path) as artifact_parent:
                    if file_identity(os.stat(path.name, dir_fd=artifact_parent.fd, follow_symlinks=False)) != entry["identity"]:
                        raise StoreError("artifact changed before deletion")
                    os.unlink(path.name, dir_fd=artifact_parent.fd)
                    os.fsync(artifact_parent.fd)
            removed.append(path)
        if removed:
            record = {**catalog.record, "artifacts": retained}
            parent.publish(CATALOG_FILENAME, encode_artifact_catalog(record), decode_artifact_catalog, catalog)
    return removed
