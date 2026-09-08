"""Standard-library installation inventory, durable copy and exact snapshot replay.

This file is also copied beside protocol.py, guard.py and helper.py. It must
never import the changing application environment.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import secrets
import stat
import sys
import time
import unicodedata
from pathlib import Path

if __package__:
    from . import protocol
elif __name__ != "__main__":
    import protocol
else:
    protocol = None  # Populated only by the authenticated copied-entry loader.


class SnapshotError(RuntimeError):
    """An installation or recovery object cannot safely be proven."""


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino,
            "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def _stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _absolute(path):
    raw = str(path)
    if (not raw.startswith("/") or raw != posixpath.normpath(raw)
            or "\\" in raw or "\0" in raw or len(raw.encode()) > protocol.PATH_BYTE_LIMIT):
        raise SnapshotError("unsafe absolute path")
    return Path(raw)


def _flags(directory=False):
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY"):
        if not getattr(os, name, 0):
            raise SnapshotError("no-follow filesystem operations unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)


def _check(info, *, kind, device=None, allow_root=False):
    owners = {os.getuid(), 0} if allow_root else {os.getuid()}
    matches = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "symlink": stat.S_ISLNK}
    if (not matches[kind](info.st_mode) or info.st_uid not in owners
            or (device is not None and info.st_dev != device)
            or (kind != "symlink" and info.st_mode & 0o7022)
            or (kind != "directory" and info.st_nlink != 1)):
        raise SnapshotError("unsafe installation object")


def _open_directory(path, *, private=False):
    path = _absolute(path)
    # Verify each ancestor lexically, so O_NOFOLLOW on the leaf cannot conceal
    # a symlinked parent. Root-owned system ancestors are permitted.
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
            raise SnapshotError("unsafe directory ancestor")
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise SnapshotError("unsafe directory ancestor permissions")
    before = path.lstat()
    _check(before, kind="directory")
    if private and stat.S_IMODE(before.st_mode) != 0o700:
        raise SnapshotError("recovery directory must have mode 0700")
    fd = os.open(path, _flags(True))
    if _stable(os.fstat(fd)) != _stable(before):
        os.close(fd)
        raise SnapshotError("directory changed during open")
    return fd


def fsync_directory(path):
    fd = _open_directory(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_noreplace_anchored(source, destination, *, source_fd=None, destination_fd=None):
    """Use validated parent descriptors for the sole native no-replace adapter."""
    source, destination = _absolute(source), _absolute(destination)
    owned = []
    try:
        if source_fd is None:
            source_fd = _open_directory(source.parent)
            owned.append(source_fd)
        if destination_fd is None:
            destination_fd = _open_directory(destination.parent)
            owned.append(destination_fd)
        for path, descriptor in ((source.parent, source_fd), (destination.parent, destination_fd)):
            if _identity(path.lstat()) != _identity(os.fstat(descriptor)):
                raise SnapshotError("rename parent changed")
        protocol.atomic_rename_noreplace(Path(source.name), Path(destination.name),
            source_directory_fd=source_fd, destination_directory_fd=destination_fd)
        os.fsync(source_fd)
        os.fsync(destination_fd)
        for path, descriptor in ((source.parent, source_fd), (destination.parent, destination_fd)):
            if _identity(path.lstat()) != _identity(os.fstat(descriptor)):
                raise SnapshotError("rename parent changed")
    finally:
        for descriptor in owned:
            os.close(descriptor)


def _budget(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise SnapshotError("snapshot cleanup budget expired")


def _read_file_at(parent_fd, name, *, device, allow_root=False, copy_fd=None, deadline=None):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _check(before, kind="file", device=device, allow_root=allow_root)
    fd = os.open(name, _flags(), dir_fd=parent_fd)
    digest = hashlib.sha256()
    size = 0
    try:
        if _stable(os.fstat(fd)) != _stable(before):
            raise SnapshotError("file changed during open")
        while chunk := os.read(fd, 1024 * 1024):
            _budget(deadline)
            size += len(chunk)
            if size > before.st_size:
                raise SnapshotError("file grew during scan")
            digest.update(chunk)
            if copy_fd is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(copy_fd, view)
                    if written <= 0:
                        raise SnapshotError("short snapshot write")
                    view = view[written:]
        if (size != before.st_size or _stable(os.fstat(fd)) != _stable(before)
                or _stable(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _stable(before)):
            raise SnapshotError("file changed during scan")
        if copy_fd is not None:
            os.fchmod(copy_fd, stat.S_IMODE(before.st_mode))
            os.fsync(copy_fd)
        return before, digest.hexdigest()
    finally:
        os.close(fd)


def _link_allowed(relative, target, external):
    if (type(target) is not str or "\0" in target or "\\" in target
            or len(target.encode()) > protocol.PATH_BYTE_LIMIT):
        raise SnapshotError("unsafe symlink target")
    if external.get(relative) == target:
        return
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise SnapshotError("symlink escapes environment")


def scan_environment(path, *, allowed_external_symlinks=None, _copy_to=None,
                     _include_bytecode=False, _identities=None, _expected_entries=None, _deadline=None):
    """Inventory every durable member without following a directory symlink.

    Only actual __pycache__ directories and regular .pyc files are omitted.
    External symlinks require exact detector-authorized relative-path/target pairs.
    """
    path = _absolute(path)
    external = {} if allowed_external_symlinks is None else dict(allowed_external_symlinks)
    root_fd = _open_directory(path)
    destination_fd = None
    entries = []
    try:
        root_before = os.fstat(root_fd)
        if _identities is not None:
            _identities[""] = _identity(root_before)
        if _copy_to is not None:
            destination_fd = _open_directory(_copy_to)
        device = root_before.st_dev

        def visit(parent_fd, prefix, copy_fd):
            before = os.fstat(parent_fd)
            names = []
            with os.scandir(parent_fd) as iterator:
                for entry in iterator:
                    _budget(_deadline)
                    if len(entries) + len(names) >= protocol.COLLECTION_LIMIT:
                        raise SnapshotError("inventory entry limit exceeded")
                    names.append(entry.name)
            folded = set()
            for name in sorted(names):
                _budget(_deadline)
                relative = f"{prefix}/{name}" if prefix else name
                if ("\\" in relative or len(relative.encode()) > protocol.PATH_BYTE_LIMIT
                        or any(ord(c) < 32 for c in relative)):
                    raise SnapshotError("unsafe inventory path")
                key = unicodedata.normalize("NFC", name).casefold()
                if key in folded:
                    raise SnapshotError("case-conflicting installation paths")
                folded.add(key)
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if _expected_entries is not None:
                    expected = _expected_entries.get(relative)
                    matches = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "symlink": stat.S_ISLNK}
                    if (expected is None or not matches[expected["kind"]](info.st_mode)
                            or mode != expected["mode"]
                            or (expected["kind"] == "file" and info.st_size != expected["size"])):
                        raise SnapshotError("snapshot cleanup contains an unexpected member")
                if stat.S_ISDIR(info.st_mode):
                    _check(info, kind="directory", device=device)
                    if name == "__pycache__" and not _include_bytecode:
                        continue
                    entries.append({"kind": "directory", "path": relative, "mode": mode})
                    child = os.open(name, _flags(True), dir_fd=parent_fd)
                    copied = None
                    try:
                        if _stable(os.fstat(child)) != _stable(info):
                            raise SnapshotError("directory changed during scan")
                        if copy_fd is not None:
                            os.mkdir(name, 0o700, dir_fd=copy_fd)
                            copied = os.open(name, _flags(True), dir_fd=copy_fd)
                        visit(child, relative, copied)
                        if copied is not None:
                            os.fchmod(copied, mode)
                            os.fsync(copied)
                    finally:
                        os.close(child)
                        if copied is not None:
                            os.close(copied)
                    if _stable(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _stable(info):
                        raise SnapshotError("directory changed during scan")
                elif stat.S_ISREG(info.st_mode):
                    _check(info, kind="file", device=device)
                    if name.endswith(".pyc") and not _include_bytecode:
                        continue
                    copied = None
                    try:
                        if copy_fd is not None:
                            copied = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=copy_fd)
                        info, digest = _read_file_at(parent_fd, name, device=device, copy_fd=copied, deadline=_deadline)
                        entries.append({"kind": "file", "path": relative, "mode": mode,
                                        "size": info.st_size, "sha256": digest})
                    finally:
                        if copied is not None:
                            os.close(copied)
                elif stat.S_ISLNK(info.st_mode):
                    _check(info, kind="symlink", device=device)
                    target = os.readlink(name, dir_fd=parent_fd)
                    _link_allowed(relative, target, external)
                    if _stable(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _stable(info):
                        raise SnapshotError("symlink changed during scan")
                    entries.append({"kind": "symlink", "path": relative, "mode": mode, "target": target})
                    if copy_fd is not None:
                        os.symlink(target, name, dir_fd=copy_fd)
                        # macOS permits preserving a raw symlink's mode; Linux
                        # has immutable 0777 symlink permissions.
                        if os.chmod in os.supports_follow_symlinks:
                            os.chmod(name, mode, dir_fd=copy_fd, follow_symlinks=False)
                else:
                    raise SnapshotError("special installation file rejected")
                if _identities is not None:
                    _identities[relative] = _identity(info) if stat.S_ISDIR(info.st_mode) else _file_identity(info)
                if len(entries) > protocol.COLLECTION_LIMIT:
                    raise SnapshotError("inventory entry limit exceeded")
            if _stable(os.fstat(parent_fd)) != _stable(before):
                raise SnapshotError("directory changed during scan")
            if copy_fd is not None:
                os.fsync(copy_fd)

        visit(root_fd, "", destination_fd)
        if _stable(path.lstat()) != _stable(root_before):
            raise SnapshotError("environment changed during scan")
        if destination_fd is not None:
            os.fchmod(destination_fd, stat.S_IMODE(root_before.st_mode))
            os.fsync(destination_fd)
        result = {"root_mode": stat.S_IMODE(root_before.st_mode),
                  "entries": sorted(entries, key=lambda item: item["path"])}
        protocol.validate_inventory(result)
        return result
    except (OSError, ValueError) as error:
        raise SnapshotError("installation scan failed") from error
    finally:
        os.close(root_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _cleanup_guards(plan, transition, launcher):
    if __package__:
        from . import guard
    else:
        import guard
    root = Path(plan["paths"]["recovery_root"])
    for name, lock, filename in (("transition", transition, "update-transition.lock"),
                                  ("launcher", launcher, "launcher-operation.lock")):
        if lock.mode != "exclusive":
            raise SnapshotError("snapshot cleanup requires exclusive guards")
        guard.verify_exclusive_reference(lock.fileno(), root / filename, plan["lock_identities"][name])


def _cleanup_terminal_authority(plan_snapshot, journal_store, journal_snapshot, transition, launcher):
    plan = plan_snapshot.record
    root = Path(plan["paths"]["recovery_root"])
    _cleanup_guards(plan, transition, launcher)
    current = journal_store.validate_snapshot(journal_snapshot)
    if current.record["state"] != "complete":
        raise SnapshotError("snapshot cleanup requires durable healthy completion")
    protocol._check_expected(protocol.ProtectedPlanStore(root).read_snapshot(), plan_snapshot)
    provenance = protocol.ProtectedProvenanceStore(root).read_snapshot()
    value = None if provenance is None else provenance.record
    receipt = current.record.get("receipt")
    live = Path(plan["paths"]["environment"]).lstat()
    _check(live, kind="directory")
    if value is not None and value["attempt_id"] == plan["attempt_id"]:
        if (value["version"] != plan["target_version"] or value["wheel"] != plan["target_wheel"]
                or value["core_token"]["environment_path"] != plan["paths"]["environment"]
                or value["core_token"]["environment_identity"] != _identity(live)
                or (receipt is not None and (receipt["outcome"] != "updated" or receipt["launch_id"] != value["launch_id"]))):
            raise SnapshotError("snapshot cleanup healthy provenance differs")
    elif value != plan["prior_provenance"] or (receipt is not None and receipt["outcome"] != "restored"):
        raise SnapshotError("snapshot cleanup prior provenance differs")
    return provenance


def _cleanup_scan(path, inventory, deadline):
    expected = {entry["path"]: entry for entry in inventory["entries"]}
    identities = {}
    external = {entry["path"]: entry["target"] for entry in inventory["entries"] if entry["kind"] == "symlink"}
    scanned = scan_environment(path, allowed_external_symlinks=external,
        _include_bytecode=True, _identities=identities, _expected_entries=expected, _deadline=deadline)
    if scanned["root_mode"] != inventory["root_mode"] or any(expected.get(entry["path"]) != entry for entry in scanned["entries"]):
        raise SnapshotError("snapshot cleanup inventory differs")
    return scanned, identities


def _cleanup_names(descriptor, expected, deadline):
    found = set()
    with os.scandir(descriptor) as names:
        for item in names:
            _budget(deadline)
            if item.name not in expected or len(found) >= len(expected):
                raise SnapshotError("snapshot cleanup directory contents changed")
            found.add(item.name)
    if found != expected:
        raise SnapshotError("snapshot cleanup directory contents changed")


def _cleanup_one(saved, *, plan, cleanup_store, transition, launcher, deadline):
    """Delete a monotonic subset of the fsynced immutable ownership inventory."""
    record = saved.record
    path = Path(record["snapshot_path"])
    expected = {item["entry"]["path"]: item for item in record["entries"]}
    inventory = {"root_mode": record["root_identity"]["mode"], "entries": [item["entry"] for item in record["entries"]]}
    _budget(deadline)
    with protocol._directory(path.parent) as parent:
        try:
            root_info = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            cleanup_store.remove(saved)
            return
        _check(root_info, kind="directory")
        if _identity(root_info) != record["root_identity"]:
            raise SnapshotError("snapshot cleanup root identity changed")
        root_fd = os.open(path.name, _flags(True), dir_fd=parent.fd)
        try:
            def verify():
                _budget(deadline)
                parent.verify()
                if (_identity(os.fstat(root_fd)) != record["root_identity"]
                        or _identity(os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)) != record["root_identity"]):
                    raise SnapshotError("snapshot cleanup root changed")
                for key in ("environment", "forensic"):
                    try:
                        protected = Path(plan["paths"][key]).lstat()
                    except FileNotFoundError:
                        continue
                    if (protected.st_dev, protected.st_ino) == (root_info.st_dev, root_info.st_ino):
                        raise SnapshotError("snapshot cleanup would delete a protected environment")

            verify()
            scanned, identities = _cleanup_scan(path, inventory, deadline)
            verify()
            if identities[""] != record["root_identity"] or any(
                    identities[entry["path"]] != expected[entry["path"]]["identity"] for entry in scanned["entries"]):
                raise SnapshotError("snapshot cleanup member identity changed")
            remaining_names = {"": set()}
            for entry in scanned["entries"]:
                prefix, _, name = entry["path"].rpartition("/")
                remaining_names.setdefault(prefix, set()).add(name)
                if entry["kind"] == "directory":
                    remaining_names.setdefault(entry["path"], set())
            _cleanup_guards(plan, transition, launcher)
            for entry in sorted(scanned["entries"], key=lambda item: (item["path"].count("/"), item["path"]), reverse=True):
                verify()
                parts = entry["path"].split("/")
                descriptors = [os.dup(root_fd)]
                try:
                    prefix = []
                    for name in parts[:-1]:
                        prefix.append(name)
                        info = os.stat(name, dir_fd=descriptors[-1], follow_symlinks=False)
                        wanted = expected["/".join(prefix)]["identity"]
                        if not stat.S_ISDIR(info.st_mode) or _identity(info) != wanted:
                            raise SnapshotError("snapshot cleanup parent changed")
                        child = os.open(name, _flags(True), dir_fd=descriptors[-1])
                        descriptors.append(child)
                        if _identity(os.fstat(child)) != wanted:
                            raise SnapshotError("snapshot cleanup parent changed")
                    descriptor = descriptors[-1]
                    name = parts[-1]
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    actual = _identity(info) if entry["kind"] == "directory" else _file_identity(info)
                    if actual != expected[entry["path"]]["identity"]:
                        raise SnapshotError("snapshot cleanup member changed before deletion")
                    verify()
                    for index, ancestor in enumerate(parts[:-1]):
                        wanted = expected["/".join(parts[:index + 1])]["identity"]
                        if (_identity(os.fstat(descriptors[index + 1])) != wanted
                                or _identity(os.stat(ancestor, dir_fd=descriptors[index], follow_symlinks=False)) != wanted):
                            raise SnapshotError("snapshot cleanup parent moved before deletion")
                    for index, opened in enumerate(descriptors):
                        _cleanup_names(opened, remaining_names["/".join(parts[:index])], deadline)
                    if entry["kind"] == "directory":
                        os.rmdir(name, dir_fd=descriptor)
                    else:
                        os.unlink(name, dir_fd=descriptor)
                    remaining_names["/".join(parts[:-1])].remove(name)
                    os.fsync(descriptor)
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
            verify()
            _cleanup_guards(plan, transition, launcher)
            os.rmdir(path.name, dir_fd=parent.fd)
            os.fsync(parent.fd)
            parent.verify()
        finally:
            os.close(root_fd)
    cleanup_store.remove(saved)


def cleanup_terminal_snapshot(*, plan_snapshot, journal_store, journal_snapshot, transition_lock, launcher_lock):
    """Bounded terminal maintenance; callers preserve success on any refusal.

    Initial ownership records require an exact complete snapshot. Their immutable
    member identities make absent entries unambiguous deletion progress on retry,
    including retries after a later protected plan has replaced the original.
    """
    deadline = time.monotonic() + protocol.SNAPSHOT_CLEANUP_TIMEOUT_SECONDS
    _budget(deadline)
    plan = plan_snapshot.record
    provenance = _cleanup_terminal_authority(plan_snapshot, journal_store, journal_snapshot, transition_lock, launcher_lock)
    cleanup_store = protocol.SnapshotCleanupStore(plan["paths"]["recovery_root"])
    saved = cleanup_store.read_snapshot(plan["attempt_id"])
    path = Path(plan["paths"]["snapshot"])
    existed = False
    try:
        path.lstat()
        existed = True
    except FileNotFoundError:
        pass
    if saved is None and existed:
        inventory = plan["old_token"]["core"]["inventory"]
        scanned, identities = _cleanup_scan(path, inventory, deadline)
        if scanned != inventory:
            raise SnapshotError("initial snapshot cleanup inventory is incomplete")
        record = {**{key: plan[key] for key in protocol.COMMON}, "attempt_id": plan["attempt_id"],
            "plan_sha256": plan_snapshot.sha256, "snapshot_path": str(path), "root_identity": identities[""],
            "entries": [{"entry": entry, "identity": identities[entry["path"]]} for entry in scanned["entries"]],
            "receipt_id": journal_snapshot.record.get("receipt", {}).get("receipt_id"),
            "provenance": None if provenance is None else {"identity": provenance.identity, "sha256": provenance.sha256}}
        _budget(deadline)
        saved = cleanup_store.publish(record, expected_plan=plan_snapshot,
            expected_journal=journal_snapshot, expected_provenance=provenance)
    failure = None
    for pending in cleanup_store.list_snapshots(deadline_at=deadline):
        try:
            _cleanup_terminal_authority(plan_snapshot, journal_store, journal_snapshot, transition_lock, launcher_lock)
            _cleanup_one(pending, plan=plan, cleanup_store=cleanup_store,
                transition=transition_lock, launcher=launcher_lock, deadline=deadline)
        except (OSError, ValueError, RuntimeError) as error:
            failure = error
            if time.monotonic() >= deadline:
                break
    if failure is not None:
        raise failure
    return "cleaned" if existed else "absent"


def capture_exposed_link(path, *, allow_missing=False):
    path = _absolute(path)
    parent_fd = _open_directory(path.parent)
    try:
        parent = os.fstat(parent_fd)
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        _check(before, kind="symlink")
        target = os.readlink(path.name, dir_fd=parent_fd)
        if _stable(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)) != _stable(before):
            raise SnapshotError("exposed symlink changed")
        if _identity(path.parent.lstat()) != _identity(parent):
            raise SnapshotError("exposed symlink parent changed")
        return {"path": str(path), "target": target, "uid": before.st_uid,
                "parent": _identity(parent)}
    except OSError as error:
        raise SnapshotError("exposed command is unsafe") from error
    finally:
        os.close(parent_fd)


def _metadata_digest(environment):
    parent = _open_directory(environment)
    try:
        if os.stat("pipx_metadata.json", dir_fd=parent, follow_symlinks=False).st_size > protocol.RECORD_BYTE_LIMIT:
            raise SnapshotError("pipx metadata too large")
        info, digest = _read_file_at(parent, "pipx_metadata.json", device=os.fstat(parent).st_dev)
        if info.st_size > protocol.RECORD_BYTE_LIMIT:
            raise SnapshotError("pipx metadata too large")
        descriptor = os.open("pipx_metadata.json", _flags(), dir_fd=parent)
        try:
            payload = os.read(descriptor, protocol.RECORD_BYTE_LIMIT + 1)
            if len(payload) != info.st_size or hashlib.sha256(payload).hexdigest() != digest:
                raise SnapshotError("pipx metadata changed")
        finally:
            os.close(descriptor)
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise SnapshotError("duplicate pipx metadata key")
                result[key] = value
            return result
        def nonfinite(value):
            raise SnapshotError("nonfinite pipx metadata value")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    finally:
        os.close(parent)


def capture_core_installation_token(environment, exposed, interpreter, *, allowed_external_symlinks=None):
    environment, interpreter = _absolute(environment), _absolute(interpreter)
    before = environment.lstat()
    inventory = scan_environment(environment, allowed_external_symlinks=allowed_external_symlinks)
    parent = os.open(interpreter.parent, _flags(True))
    try:
        info, digest = _read_file_at(parent, interpreter.name, device=None, allow_root=True)
    finally:
        os.close(parent)
    token = {"environment_path": str(environment), "environment_identity": _identity(before),
             "inventory": inventory, "exposed_link": capture_exposed_link(exposed),
             "pipx_metadata_sha256": _metadata_digest(environment),
             "interpreter": {"path": str(interpreter), **_identity(info), "size": info.st_size, "sha256": digest}}
    if _identity(environment.lstat()) != _identity(before):
        raise SnapshotError("environment changed during capture")
    protocol.validate_core_installation_token(token)
    return token


def capture_executable_identity(path):
    path = _absolute(path)
    before = path.lstat()
    _check(before, kind="file", allow_root=True)
    if not before.st_mode & 0o111:
        raise SnapshotError("validated executable is not executable")
    parent = os.open(path.parent, _flags(True))
    try:
        info, digest = _read_file_at(parent, path.name, device=None, allow_root=True)
        return {"path": str(path), **_identity(info), "size": info.st_size, "sha256": digest}
    finally:
        os.close(parent)


def capture_installation_token(environment, exposed, interpreter, *, allowed_external_symlinks=None, provenance=None):
    token = {"core": capture_core_installation_token(environment, exposed, interpreter,
             allowed_external_symlinks=allowed_external_symlinks), "provenance": provenance}
    protocol.validate_installation_token(token)
    return token


def _external_links(core):
    # These pairs were accepted only when the original scanner had explicit
    # detector authorization. Rereading a protected token cannot broaden them.
    result = {}
    for item in core["inventory"]["entries"]:
        if item["kind"] == "symlink":
            try:
                _link_allowed(item["path"], item["target"], {})
            except SnapshotError:
                result[item["path"]] = item["target"]
    return result


def verify_installation_token(token, *, provenance=None):
    protocol.validate_installation_token(token)
    old = token["core"]
    current = capture_installation_token(old["environment_path"], old["exposed_link"]["path"],
            old["interpreter"]["path"], allowed_external_symlinks=_external_links(old), provenance=provenance)
    if current != token:
        raise SnapshotError("installation changed since snapshot")
    return current


def validate_target_installation(plan):
    """Prove retained interpreter and all files outside the application update."""
    old = plan["old_token"]["core"]
    current = capture_core_installation_token(plan["paths"]["environment"], plan["paths"]["exposed_command"],
                plan["paths"]["base_interpreter"], allowed_external_symlinks=_external_links(old))
    if current["interpreter"] != old["interpreter"] or current["exposed_link"] != old["exposed_link"]:
        raise SnapshotError("target interpreter or exposed command differs")
    if current["environment_identity"] != old["environment_identity"]:
        raise SnapshotError("target environment identity differs")
    def preserved(inventory):
        result = []
        app_roots = ("arxiv_digest", f"arxiv_digest-{plan['old_version']}.dist-info", f"arxiv_digest-{plan['target_version']}.dist-info")
        for item in inventory["entries"]:
            path = item["path"]
            if path in {"pipx_metadata.json", "bin/arxiv-digest"}:
                continue
            parts = path.split("/")
            # The application roots occur only in the one real site-packages
            # directory; similarly named modules elsewhere remain protected.
            if "site-packages" in parts:
                index = parts.index("site-packages")
                if len(parts) > index + 1 and parts[index + 1] in app_roots:
                    continue
            result.append(item)
        return result
    if current["inventory"]["root_mode"] != old["inventory"]["root_mode"] or preserved(current["inventory"]) != preserved(old["inventory"]):
        raise SnapshotError("target changed nonapplication installation files")
    return current


def validate_old_installation(plan):
    old = plan["old_token"]["core"]
    current = capture_core_installation_token(plan["paths"]["environment"], plan["paths"]["exposed_command"],
                plan["paths"]["base_interpreter"], allowed_external_symlinks=_external_links(old))
    expected = {**old, "environment_identity": {**old["environment_identity"], "inode": current["environment_identity"]["inode"]}}
    if current != expected:
        raise SnapshotError("restored installation differs from old inventory")
    return current


def create_snapshot(environment, destination, exposed, interpreter, *, allowed_external_symlinks=None, provenance=None):
    environment, destination = _absolute(environment), _absolute(destination)
    if environment == destination or environment in destination.parents or destination in environment.parents:
        raise SnapshotError("nested snapshot path")
    parent = _open_directory(destination.parent, private=True)
    try:
        if os.fstat(parent).st_dev != environment.lstat().st_dev:
            raise SnapshotError("snapshot must share installation filesystem")
        before = capture_installation_token(environment, exposed, interpreter,
                allowed_external_symlinks=allowed_external_symlinks, provenance=provenance)
        try:
            os.mkdir(destination.name, 0o700, dir_fd=parent)
        except FileExistsError as error:
            raise SnapshotError("snapshot destination exists") from error
        copied = scan_environment(environment, allowed_external_symlinks=allowed_external_symlinks, _copy_to=destination)
        if copied != before["core"]["inventory"]:
            raise SnapshotError("installation changed during snapshot")
        if scan_environment(destination, allowed_external_symlinks=allowed_external_symlinks) != copied:
            raise SnapshotError("snapshot reread mismatch")
        os.fsync(parent)
        verify_installation_token(before, provenance=provenance)
        return before
    finally:
        os.close(parent)


def capture_partial_environment(path, *, allowed_external_symlinks=None):
    path = _absolute(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    inventory = scan_environment(path, allowed_external_symlinks=allowed_external_symlinks)
    if _identity(path.lstat()) != _identity(before):
        raise SnapshotError("partial environment changed")
    return {"identity": _identity(before), "inventory": inventory}


def _restore_exposed_link(old, partial):
    path = _absolute(old["path"])
    current = capture_exposed_link(path, allow_missing=True)
    if current == old:
        return
    if current is not None and current != partial:
        raise SnapshotError("external exposed command change")
    parent = _open_directory(path.parent)
    try:
        if _identity(os.fstat(parent)) != old["parent"]:
            raise SnapshotError("exposed command parent changed")
        if current is not None:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if capture_exposed_link(path) != current:
                raise SnapshotError("exposed command changed before restore")
            if _stable(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _stable(before):
                raise SnapshotError("exposed command changed before unlink")
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)
        # symlink itself has exclusive-create semantics; no os.replace fallback.
        os.symlink(old["target"], path.name, dir_fd=parent)
        os.fsync(parent)
        if capture_exposed_link(path) != old:
            raise SnapshotError("exposed command restoration mismatch")
    finally:
        os.close(parent)


def _live_matches_old(partial, old):
    return (partial is not None and partial["inventory"] == old["inventory"]
            and partial["identity"]["device"] == old["environment_identity"]["device"]
            and partial["identity"]["uid"] == old["environment_identity"]["uid"]
            and partial["identity"]["mode"] == old["environment_identity"]["mode"])


def replay_snapshot(*, journal_store, journal_snapshot, process_death, provenance_store=None):
    """Replay only the exact committed journal under caller-owned update locks.

    ``process_death`` must positively verify/reap every recorded process group.
    Boolean durable facts alone never prove a process is dead. The same routine
    is used by a running helper and the copied next-launch recovery entry point.
    """
    current_journal = journal_store.validate_snapshot(journal_snapshot)
    record = current_journal.record
    if record["state"] != "rolling_back" or record.get("subphase") != "package_restore_pending":
        raise SnapshotError("snapshot replay requires committed rollback authorization")
    replay = record["replay"]
    if replay is None or not replay["processes_dead"]:
        raise SnapshotError("snapshot replay lacks process death authorization")
    groups = tuple(replay["process_group_ids"])
    if process_death(groups) is not True:
        raise SnapshotError("installer process death is not proven")
    old = replay["old_token"]
    live, snapshot, forensic = map(_absolute, (replay["live_path"], replay["snapshot_path"], replay["forensic_path"]))
    if (str(live) != old["environment_path"] or live == snapshot or live == forensic
            or snapshot.parent != forensic.parent or snapshot == forensic
            or record["attempt_id"] not in snapshot.name
            or record["attempt_id"] not in forensic.name
            or any(a in b.parents for a, b in ((live, snapshot), (snapshot, live), (live, forensic), (forensic, live)))):
        raise SnapshotError("replay paths do not match the exact attempt")
    snapshot_parent = _open_directory(snapshot.parent, private=True)
    live_parent = _open_directory(live.parent)
    try:
        if os.fstat(snapshot_parent).st_dev != os.fstat(live_parent).st_dev:
            raise SnapshotError("replay filesystem mismatch")
        external = _external_links(old)
        saved = capture_partial_environment(snapshot, allowed_external_symlinks=external)
        actual = capture_partial_environment(live, allowed_external_symlinks=external)
        retained = capture_partial_environment(forensic, allowed_external_symlinks=external)
        partial = replay["partial_token"]
        if retained is not None and retained != partial:
            raise SnapshotError("unknown forensic environment")
        # Decide the complete replay shape before any package/provenance/link write.
        if saved is None:
            if not _live_matches_old(actual, old):
                raise SnapshotError("consumed snapshot lacks exact old live environment")
            if capture_exposed_link(old["exposed_link"]["path"], allow_missing=True) != old["exposed_link"]:
                raise SnapshotError("consumed snapshot has conflicting exposed command")
            _restore_provenance(replay, provenance_store, verify_only=True)
            return "already_restored"
        if saved["inventory"] != old["inventory"]:
            raise SnapshotError("snapshot inventory changed")
        if actual is not None and not _live_matches_old(actual, old) and actual != partial:
            raise SnapshotError("unauthorized partial installation")
        if retained is not None and actual is not None and not _live_matches_old(actual, old):
            raise SnapshotError("conflicting live and forensic environments")
        link = capture_exposed_link(old["exposed_link"]["path"], allow_missing=True)
        if link is not None and link not in (old["exposed_link"], replay["partial_exposed"]):
            raise SnapshotError("external exposed command change")
        _restore_provenance(replay, provenance_store, verify_only=True, allow_partial=True)
        # No journal lock spans filesystem mutation. Under the outer locks, an
        # exact reread still rejects stale acknowledgement or recovery attempts.
        journal_store.validate_snapshot(current_journal)
        if actual is not None and not _live_matches_old(actual, old):
            if capture_partial_environment(live, allowed_external_symlinks=external) != actual:
                raise SnapshotError("live installation changed before quarantine")
            rename_noreplace_anchored(live, forensic, source_fd=live_parent, destination_fd=snapshot_parent)
            os.fsync(live_parent)
            os.fsync(snapshot_parent)
            actual = None
        _restore_provenance(replay, provenance_store)
        _restore_exposed_link(old["exposed_link"], replay["partial_exposed"])
        if actual is None:
            if capture_partial_environment(snapshot, allowed_external_symlinks=external) != saved:
                raise SnapshotError("snapshot changed before restoration")
            rename_noreplace_anchored(snapshot, live, source_fd=snapshot_parent, destination_fd=live_parent)
            os.fsync(snapshot_parent)
            os.fsync(live_parent)
        restored = capture_partial_environment(live, allowed_external_symlinks=external)
        if not _live_matches_old(restored, old):
            raise SnapshotError("restored installation inventory mismatch")
        _restore_provenance(replay, provenance_store, verify_only=True)
        if capture_exposed_link(old["exposed_link"]["path"]) != old["exposed_link"]:
            raise SnapshotError("restored exposed command mismatch")
        return "preserved_old" if actual is not None else "restored"
    except OSError as error:
        raise SnapshotError("snapshot replay filesystem conflict") from error
    finally:
        os.close(snapshot_parent)
        os.close(live_parent)


def _restore_provenance(replay, store, *, verify_only=False, allow_partial=False):
    prior = replay["prior_provenance"]
    if store is None:
        if prior is not None or replay["partial_provenance"] is not None:
            raise SnapshotError("protected provenance store required")
        return
    current = store.read_snapshot()
    if (current is None and prior is None) or (current is not None and current.record == prior):
        return
    partial = replay["partial_provenance"]
    actual = None if current is None else {"identity": current.identity, "sha256": current.sha256}
    if actual != partial:
        raise SnapshotError("external protected provenance change")
    if verify_only:
        if not allow_partial:
            raise SnapshotError("prior protected provenance not restored")
        return
    store.compare_and_swap(current, prior)


def _file_identity(info):
    return {**_identity(info), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "nlink": info.st_nlink}


def read_owned_bytes(path, *, limit=None):
    path = _absolute(path)
    limit = protocol.RECORD_BYTE_LIMIT if limit is None else limit
    parent = _open_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _check(before, kind="file")
        if before.st_size > limit:
            raise SnapshotError("owned file exceeds byte limit")
        fd = os.open(path.name, _flags(), dir_fd=parent)
        try:
            chunks = []
            size = 0
            while chunk := os.read(fd, min(1024 * 1024, limit + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise SnapshotError("owned file exceeds byte limit")
            if (size != before.st_size or _stable(os.fstat(fd)) != _stable(before)
                    or _stable(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _stable(before)):
                raise SnapshotError("owned file changed while reading")
            return b"".join(chunks), _file_identity(before)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _write_new(path, payload, mode, *, private_parent=True, temporary_parent=None):
    parent = _open_directory(path.parent, private=private_parent)
    temporary_parent = path.parent if temporary_parent is None else Path(temporary_parent)
    temporary_directory = parent if temporary_parent == path.parent else _open_directory(temporary_parent, private=True)
    fd = None
    temporary = temporary_parent / (".arxiv-update-" + secrets.token_hex(32) + ".tmp")
    created = None
    try:
        fd = os.open(temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=temporary_directory)
        created = _identity(os.fstat(fd))
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise SnapshotError("short protected-file write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.fsync(temporary_directory)
        reread, identity = read_owned_bytes(temporary)
        if reread != payload or identity["mode"] != mode:
            raise SnapshotError("protected-file reread differs")
        rename_noreplace_anchored(temporary, path, source_fd=temporary_directory, destination_fd=parent)
        os.fsync(temporary_directory)
        os.fsync(parent)
        reread, identity = read_owned_bytes(path)
        if reread != payload:
            raise SnapshotError("published protected file differs")
        return identity
    finally:
        if fd is not None:
            os.close(fd)
        try:
            info = os.stat(temporary.name, dir_fd=temporary_directory, follow_symlinks=False)
            if created is not None and _identity(info) == created and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                os.unlink(temporary.name, dir_fd=temporary_directory)
                os.fsync(temporary_directory)
        except FileNotFoundError:
            pass
        if temporary_directory != parent:
            os.close(temporary_directory)
        os.close(parent)


def materialize_runtime(recovery_root, base_interpreter, sources, *, prior_journal_store=None, prior_journal_snapshot=None):
    """Fsync and reread an exact four-file runtime before any plan publication.

    Source bytes are injected for slice tests; production supplies the exact
    packaged inventory. Existing unequal runtime files refuse replacement.
    """
    recovery_root, base_interpreter = _absolute(recovery_root), _absolute(base_interpreter)
    prior_runtime = {}
    prior_wrapper = None
    prior_attempt = None
    if prior_journal_store is not None or prior_journal_snapshot is not None:
        if prior_journal_store is None or prior_journal_snapshot is None:
            raise SnapshotError("runtime replacement requires exact prior journal")
        previous = prior_journal_store.validate_snapshot(prior_journal_snapshot)
        if previous.record["state"] not in {"complete", "aborted_no_mutation"} or previous.record.get("receipt") is not None:
            raise SnapshotError("runtime replacement requires an acknowledged healthy terminal")
        prior_plan = protocol.ProtectedPlanStore(recovery_root).read_snapshot()
        if prior_plan is None or prior_plan.sha256 != previous.record["plan_sha256"]:
            raise SnapshotError("runtime replacement lacks prior protected plan")
        prior_runtime = {item["name"]: item for item in prior_plan.record["runtime"]}
        prior_wrapper = prior_plan.record["wrapper"]
        prior_attempt = previous.record["attempt_id"]
    expected = {"protocol.py", "guard.py", "helper.py", "recovery.py"}
    if type(sources) is not dict or set(sources) != expected:
        raise SnapshotError("copied runtime inventory must contain exactly four files")
    for name, payload in sources.items():
        if type(payload) is not bytes or not payload or len(payload) > protocol.RECORD_BYTE_LIMIT:
            raise SnapshotError("invalid runtime source bytes")
        try:
            tree = ast.parse(payload.decode("utf-8"), filename=name)
        except (SyntaxError, UnicodeError) as error:
            raise SnapshotError("runtime source is not executable Python") from error
        for node in ast.walk(tree):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for module in modules:
                root = module.split(".", 1)[0]
                if root and root not in sys.stdlib_module_names and root + ".py" not in expected:
                    raise SnapshotError("runtime imports outside stdlib and fixed siblings")
    parent = _open_directory(recovery_root, private=True)
    runtime = recovery_root / "runtime"
    try:
        try:
            os.mkdir("runtime", 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        runtime_fd = _open_directory(runtime, private=True)
        try:
            if set(os.listdir(runtime_fd)) - expected:
                raise SnapshotError("unlisted copied runtime object")
        finally:
            os.close(runtime_fd)
        records = []
        for name in sorted(expected):
            path, payload = runtime / name, sources[name]
            try:
                existing, identity = read_owned_bytes(path)
                if existing != payload or identity["mode"] != 0o600:
                    old = prior_runtime.get(name)
                    if old is None or old["identity"] != identity or old["sha256"] != hashlib.sha256(existing).hexdigest():
                        raise SnapshotError("existing runtime differs")
                    prior_journal_store.validate_snapshot(prior_journal_snapshot)
                    retained = recovery_root / ("runtime-" + prior_attempt + "-" + name)
                    staged = recovery_root / ("runtime-new-" + hashlib.sha256(payload).hexdigest() + "-" + name)
                    try:
                        staged_bytes, _ = read_owned_bytes(staged)
                        if staged_bytes != payload:
                            raise SnapshotError("conflicting staged runtime")
                    except FileNotFoundError:
                        _write_new(staged, payload, 0o600)
                    if read_owned_bytes(path) != (existing, identity):
                        raise SnapshotError("runtime changed before replacement")
                    rename_noreplace_anchored(path, retained)
                    fsync_directory(runtime)
                    fsync_directory(recovery_root)
                    rename_noreplace_anchored(staged, path)
                    fsync_directory(runtime)
                    fsync_directory(recovery_root)
                    reread, identity = read_owned_bytes(path)
                    if reread != payload:
                        raise SnapshotError("replacement runtime reread differs")
            except FileNotFoundError:
                identity = _write_new(path, payload, 0o600, temporary_parent=recovery_root)
            records.append({"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                            "mode": 0o600, "identity": identity})
        # A fixed Python wrapper avoids shell expansion entirely. Its only
        # executable value is the already validated absolute base interpreter.
        wrapper = recovery_root / "recover-arxiv-digest"
        if any(char in str(base_interpreter) for char in "\r\n\0"):
            raise SnapshotError("unsafe recovery interpreter")
        import shlex
        command = ("exec " + shlex.quote(str(base_interpreter)) + " -I -B "
                   + shlex.quote(str(runtime / "recovery.py")) + " --recovery-root "
                   + shlex.quote(str(recovery_root)))
        payload = ("#!/bin/sh\ncase \"$#\" in\n  0) " + command + ";;\n"
                   "  1) [ \"$1\" = '--explicit-recovery' ] || exit 64\n    " + command + " --explicit-recovery;;\n"
                   "  *) exit 64;;\nesac\n").encode()
        try:
            existing, identity = read_owned_bytes(wrapper)
            if existing != payload or identity["mode"] != 0o700:
                if (prior_wrapper is None or identity != prior_wrapper["identity"]
                        or hashlib.sha256(existing).hexdigest() != prior_wrapper["sha256"]):
                    raise SnapshotError("existing recovery wrapper differs")
                prior_journal_store.validate_snapshot(prior_journal_snapshot)
                rename_noreplace_anchored(wrapper, recovery_root / ("wrapper-" + prior_attempt))
                fsync_directory(recovery_root)
                identity = _write_new(wrapper, payload, 0o700)
        except FileNotFoundError:
            identity = _write_new(wrapper, payload, 0o700)
        os.fsync(parent)
        return {"runtime": records, "wrapper": {"path": str(wrapper), "size": len(payload), "mode": 0o700,
                "sha256": hashlib.sha256(payload).hexdigest(), "identity": identity}}
    finally:
        os.close(parent)


def capture_launcher_state(path):
    """Capture exact bytes/modes, including an optional launcher's absence."""
    path = _absolute(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent", "path": str(path)}
    if stat.S_ISREG(before.st_mode):
        payload, identity = read_owned_bytes(path)
        result = {"kind": "file", "path": str(path), "size": len(payload),
                  "sha256": hashlib.sha256(payload).hexdigest(), "mode": identity["mode"],
                  "content_hex": payload.hex()}
    elif stat.S_ISDIR(before.st_mode):
        inventory = scan_environment(path)
        files = []
        total = 0
        for item in inventory["entries"]:
            if item["kind"] == "symlink":
                raise SnapshotError("managed launcher contains symlinks")
            if item["kind"] == "file":
                payload, identity = read_owned_bytes(path / item["path"])
                total += len(payload) * 2
                if total > protocol.RECORD_BYTE_LIMIT // 2:
                    raise SnapshotError("launcher capture exceeds plan budget")
                if hashlib.sha256(payload).hexdigest() != item["sha256"] or identity["mode"] != item["mode"]:
                    raise SnapshotError("launcher changed during capture")
                files.append({"path": item["path"], "bytes_hex": payload.hex()})
        if scan_environment(path) != inventory:
            raise SnapshotError("launcher changed during capture")
        result = {"kind": "directory", "path": str(path), "inventory": inventory, "files": files}
    else:
        raise SnapshotError("unsafe managed launcher")
    protocol.validate_launcher_state(result)
    return result


def _at_launcher_path(state, path):
    return {**state, "path": str(path)}


def _write_launcher_state(state, path):
    if state["kind"] == "file":
        _write_new(path, bytes.fromhex(state["content_hex"]), state["mode"], private_parent=False)
        return
    if state["kind"] != "directory":
        raise SnapshotError("cannot materialize absent launcher")
    os.mkdir(path, 0o700)
    files = {item["path"]: bytes.fromhex(item["bytes_hex"]) for item in state["files"]}
    directories = []
    for item in state["inventory"]["entries"]:
        target = path / item["path"]
        if item["kind"] == "directory":
            os.mkdir(target, 0o700)
            directories.append((target, item["mode"]))
        elif item["kind"] == "file":
            _write_new(target, files[item["path"]], item["mode"])
        else:
            raise SnapshotError("launcher symlinks are unsupported")
    for directory, mode in reversed(directories):
        os.chmod(directory, mode)
        fsync_directory(directory)
    os.chmod(path, state["inventory"]["root_mode"])
    fsync_directory(path)
    fsync_directory(path.parent)


def replace_launcher_state(expected, replacement, *, attempt_id, transition_lock, launcher_lock):
    """Replace only the exact owned launcher state under both exclusive locks.

    The journal must publish prior/intended states before this operation. An
    absent shortcut stays absent. The bounded old object permits replay if a
    process stops between the two native no-replace renames.
    """
    protocol.validate_launcher_state(expected)
    protocol.validate_launcher_state(replacement)
    if expected["path"] != replacement["path"] or len(attempt_id) != 64 or any(c not in "0123456789abcdef" for c in attempt_id):
        raise SnapshotError("launcher attempt or paths differ")
    for lock in (transition_lock, launcher_lock):
        if lock.mode != "exclusive":
            raise SnapshotError("launcher replacement requires exclusive ownership")
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise SnapshotError("invalid launcher ownership descriptor")
    path = _absolute(expected["path"])
    current = capture_launcher_state(path)
    if current == replacement:
        return
    if expected["kind"] == "absent" or replacement["kind"] == "absent":
        raise SnapshotError("update cannot create or remove an optional launcher")
    fingerprint = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prefix = path.name + ".update-" + attempt_id + "." + fingerprint
    staged = path.with_name(prefix + ".new")
    retained = path.with_name(prefix + ".old")
    saved = capture_launcher_state(retained)
    if current != expected:
        if current["kind"] != "absent" or saved != _at_launcher_path(expected, retained):
            raise SnapshotError("launcher changed outside update")
    elif saved["kind"] != "absent":
        raise SnapshotError("conflicting retained launcher")
    pending = capture_launcher_state(staged)
    if pending["kind"] == "absent":
        temporary = path.with_name(".arxiv-launcher-" + secrets.token_hex(32))
        _write_launcher_state(replacement, temporary)
        if capture_launcher_state(temporary) != _at_launcher_path(replacement, temporary):
            raise SnapshotError("temporary launcher verification failed")
        rename_noreplace_anchored(temporary, staged)
        fsync_directory(path.parent)
    elif pending != _at_launcher_path(replacement, staged):
        raise SnapshotError("conflicting staged launcher")
    if capture_launcher_state(staged) != _at_launcher_path(replacement, staged):
        raise SnapshotError("staged launcher verification failed")
    if current["kind"] != "absent":
        if capture_launcher_state(path) != expected:
            raise SnapshotError("launcher changed before replacement")
        rename_noreplace_anchored(path, retained)
        fsync_directory(path.parent)
    rename_noreplace_anchored(staged, path)
    fsync_directory(path.parent)
    if capture_launcher_state(path) != replacement:
        raise SnapshotError("launcher replacement reread differs")


def _bootstrap_copied_runtime(root, attempt_id=None):
    """Authenticate all four already-read source files before sibling execution.

    This initial entry has only stdlib imports and cannot trust a codec until
    its own protected plan has authenticated protocol.py's bytes. The fixed
    bootstrap cap is checked against the authoritative codec after loading.
    """
    import types

    global protocol
    root = Path(root)
    if not root.is_absolute() or str(root) != posixpath.normpath(str(root)) or root.resolve() != root:
        raise SnapshotError("unsafe copied runtime root")
    for path in (root, root / "runtime"):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SnapshotError("unsafe copied runtime directory")
    if set(os.listdir(root / "runtime")) != {"protocol.py", "guard.py", "helper.py", "recovery.py"}:
        raise SnapshotError("unlisted copied runtime file")
    bootstrap_limit = 32 * 1024 * 1024

    def read(path):
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > bootstrap_limit):
            raise SnapshotError("unsafe copied runtime file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            data = bytearray()
            while chunk := os.read(fd, min(1024 * 1024, bootstrap_limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > bootstrap_limit:
                    raise SnapshotError("copied runtime file exceeds limit")
            if _stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(path.lstat()):
                raise SnapshotError("copied runtime file changed")
            return bytes(data), _file_identity(before)
        finally:
            os.close(fd)

    plan_bytes, _ = read(root / "update-plan.json")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SnapshotError("duplicate bootstrap field")
            result[key] = value
        return result
    def invalid(value):
        raise SnapshotError("nonfinite bootstrap number")
    plan = json.loads(plan_bytes, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(plan, dict) or plan.get("paths", {}).get("recovery_root") != str(root):
        raise SnapshotError("copied runtime plan root differs")
    if attempt_id is not None and plan.get("attempt_id") != attempt_id:
        raise SnapshotError("copied runtime attempt differs")
    runtime = plan.get("runtime")
    if not isinstance(runtime, list) or len(runtime) != 4:
        raise SnapshotError("copied runtime inventory differs")
    sources = {}
    for item in runtime:
        if type(item) is not dict or item.get("name") not in {"protocol.py", "guard.py", "helper.py", "recovery.py"} or item["name"] in sources:
            raise SnapshotError("copied runtime inventory differs")
        payload, identity = read(root / "runtime" / item["name"])
        if identity != item.get("identity") or len(payload) != item.get("size") or hashlib.sha256(payload).hexdigest() != item.get("sha256"):
            raise SnapshotError("copied runtime source differs")
        sources[item["name"]] = payload
    if Path(__file__) != root / "runtime/recovery.py":
        raise SnapshotError("copied entry path differs")
    module = types.ModuleType("protocol")
    module.__file__ = str(root / "runtime/protocol.py")
    sys.modules["protocol"] = module
    exec(compile(sources["protocol.py"], module.__file__, "exec"), module.__dict__)
    protocol = module
    if protocol.RECORD_BYTE_LIMIT != bootstrap_limit:
        raise SnapshotError("copied runtime bootstrap limit differs")
    saved = protocol.ProtectedPlanStore(root).read_snapshot()
    if saved is None or saved.canonical_bytes != plan_bytes:
        raise SnapshotError("copied plan changed during bootstrap")
    wrapper, wrapper_identity = read_owned_bytes(root / "recover-arxiv-digest")
    if (wrapper_identity != saved.record["wrapper"]["identity"]
            or hashlib.sha256(wrapper).hexdigest() != saved.record["wrapper"]["sha256"]):
        raise SnapshotError("copied recovery wrapper differs")
    if (Path(sys.executable).resolve() != Path(saved.record["paths"]["base_interpreter"])
            or capture_executable_identity(saved.record["paths"]["base_interpreter"]) != saved.record["old_token"]["core"]["interpreter"]):
        raise SnapshotError("copied base interpreter differs")
    sys.modules["recovery"] = sys.modules[__name__]
    for name in ("guard", "helper"):
        sibling = types.ModuleType(name)
        sibling.__file__ = str(root / "runtime" / (name + ".py"))
        sys.modules[name] = sibling
        exec(compile(sources[name + ".py"], sibling.__file__, "exec"), sibling.__dict__)
    return saved, sys.modules["helper"], sys.modules["guard"]


def copied_main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--helper", action="store_true")
    modes.add_argument("--guard", action="store_true")
    modes.add_argument("--installer-child", action="store_true")
    parser.add_argument("--recovery-root", required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument("--nonce")
    parser.add_argument("--control-fd", type=int)
    parser.add_argument("--transition-fd", type=int)
    parser.add_argument("--launcher-fd", type=int)
    parser.add_argument("--explicit-recovery", action="store_true")
    options = parser.parse_args(argv)
    try:
        saved, helper, guard = _bootstrap_copied_runtime(options.recovery_root, options.attempt_id)
        if options.helper or options.guard or options.installer_child:
            if (options.attempt_id is None or options.nonce is None or options.control_fd is None
                    or options.control_fd < 3 or len(options.nonce) != 64
                    or any(c not in "0123456789abcdef" for c in options.nonce)):
                raise SnapshotError("copied control arguments differ")
        if options.helper:
            return helper.run_helper(options, saved)
        if options.guard:
            return guard.run_guard(options, saved)
        if options.installer_child:
            return guard.installer_child(options, saved.record)
        return helper.run_recovery(options, saved)
    except (OSError, ValueError, RuntimeError, EOFError, KeyError, TypeError):
        print("arXiv Digest recovery is blocked. Fully quit the application and review the documented recovery guidance.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(copied_main())
