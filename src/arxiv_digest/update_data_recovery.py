"""Authenticated updater data restore; failed target bytes remain opaque.

The internal entry point authenticates the protected plan and borrowed locks
before importing this module. No function here selects or validates a PDF folder.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
from pathlib import Path

from arxiv_digest import backup
from arxiv_digest.atomic import ensure_private_directory_strict
from arxiv_digest.profile import PdfDestination
from arxiv_digest.update_runtime import protocol


class DataRecoveryError(ValueError):
    pass


def _members(paths):
    return {"database": paths.database_path, "profile": paths.profile_path,
            "restore_journal": paths.restore_journal_path,
            "wal": Path(str(paths.database_path) + "-wal"),
            "shm": Path(str(paths.database_path) + "-shm")}


def _read_file(path, *, links=False):
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
            raise DataRecoveryError("unsafe recovery file")
        digest = hashlib.sha256()
        remaining = before.st_size + 1
        while remaining and (chunk := os.read(fd, min(64 * 1024, remaining))):
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining != 1:
            raise DataRecoveryError("recovery file length changed")
        identity = protocol.file_identity(before)
        if identity != protocol.file_identity(os.fstat(fd)) or identity != protocol.file_identity(path.lstat()):
            raise DataRecoveryError("recovery file changed")
        return {"identity": identity, "size": before.st_size, "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _copy(source, destination):
    before = _read_file(source)
    if before is None:
        raise DataRecoveryError("recovery source is absent")
    backup._copy_file_exclusive(source, destination)
    after = _read_file(source)
    saved = _read_file(destination)
    if before != after or saved is None or (saved["size"], saved["sha256"]) != (before["size"], before["sha256"]):
        raise DataRecoveryError("recovery copy changed")
    backup._fsync_directory(destination.parent)
    return before, saved


def _matches(path, digest, *, links=False):
    current = _read_file(path, links=links)
    return current is not None and current["sha256"] == digest


def _phase(store, snapshot, phase):
    return store.compare_and_swap(snapshot, {**snapshot.record, "phase": phase})


def _verify_saved(raw, record):
    for name, member in record["members"].items():
        saved = _read_file(raw / name)
        if member["kind"] == "absent":
            if saved is not None:
                raise DataRecoveryError("unexpected raw recovery member")
        elif saved is None or saved != {"identity": member["saved_identity"], "size": member["size"], "sha256": member["sha256"]}:
            raise DataRecoveryError("raw recovery inventory changed")
    for name, digest in record["replacement"].items():
        if not _matches(raw / ("replacement." + name), digest):
            raise DataRecoveryError("replacement recovery file changed")


def _preserve(paths, plan, raw, store, inspection, inject):
    # No active file is modified until every raw member and the inventory have
    # been fsynced and read back. An interrupted incomplete preservation blocks.
    raw.mkdir(mode=0o700)
    backup._fsync_directory(raw.parent)
    destination = PdfDestination(plan["backup"]["pdf_destination"]["kind"], Path(plan["backup"]["pdf_destination"]["path"]))
    with tempfile.TemporaryDirectory(prefix=".update-build-", dir=paths.data_dir) as temp:
        database, profile = backup._build_restored_state(
            Path(temp), inspection, destination, inspection.profile.revision,
            inspection.manifest.created_at, preserve_download_records=True,
        )
        replacement = {}
        for name, source in (("database", database), ("profile", profile)):
            _, saved = _copy(source, raw / ("replacement." + name))
            replacement[name] = saved["sha256"]
    members = {}
    for name, path in _members(paths).items():
        original = _read_file(path)
        if original is None:
            members[name] = {"kind": "absent"}
        else:
            before, saved = _copy(path, raw / name)
            if original != before:
                raise DataRecoveryError("failed target state changed")
            members[name] = {"kind": "file", "size": saved["size"], "sha256": saved["sha256"],
                             "original_identity": original["identity"], "saved_identity": saved["identity"]}
        inject("raw_" + name + "_fsynced")
    record = {key: plan[key] for key in protocol.COMMON}
    record.update(attempt_id=plan["attempt_id"], plan_sha256=hashlib.sha256(protocol.encode_plan(plan)).hexdigest(),
                  created_at_ns=time.time_ns(), phase="preserved", members=members, replacement=replacement)
    snapshot = store.compare_and_swap(None, record)
    _verify_saved(raw, snapshot.record)
    inject("raw_inventory_fsynced")
    return snapshot


def _clear_auxiliary(paths, record, *, first=False):
    for name in ("restore_journal", "wal", "shm"):
        path = _members(paths)[name]
        member = record["members"][name]
        current = _read_file(path)
        if current is None:
            if first and member["kind"] != "absent":
                raise DataRecoveryError("failed target state disappeared")
            continue
        if member["kind"] == "absent" or current["sha256"] != member["sha256"]:
            raise DataRecoveryError("failed target state changed")
        if first and current["identity"] != member["original_identity"]:
            raise DataRecoveryError("failed target identity changed")
        path.unlink()
        backup._fsync_directory(path.parent)


def _restore_auxiliary(paths, raw, record):
    for name in ("restore_journal", "wal", "shm"):
        path = _members(paths)[name]
        member = record["members"][name]
        current = _read_file(path)
        if member["kind"] == "absent":
            if current is not None:
                raise DataRecoveryError("unexpected recovery file prevents rollback")
        elif current is None:
            _copy(raw / name, path)
        elif current["sha256"] != member["sha256"]:
            raise DataRecoveryError("changed recovery file prevents rollback")


def _require_auxiliary_absent(paths, *, include_journal=False):
    # Publication uses standalone, checkpointed replacement bytes. A later WAL
    # or SHM belongs to an unexpected writer and must not accompany that pair.
    names = ("wal", "shm", "restore_journal") if include_journal else ("wal", "shm")
    for name in names:
        if _read_file(_members(paths)[name]) is not None:
            raise DataRecoveryError("unexpected auxiliary file prevents recovery publication")


def _pair_matches(paths, record, *, replacement):
    for name in ("database", "profile"):
        member = record["members"][name]
        digest = record["replacement"][name] if replacement else member.get("sha256")
        path = _members(paths)[name]
        if digest is None:
            if _read_file(path, links=True) is not None:
                return False
        elif not _matches(path, digest, links=True):
            return False
    return True


def _validate_publication_journal(paths, record):
    current = _read_file(paths.restore_journal_path)
    if current is None:
        return False
    if current["size"] > protocol.CONTROL_BYTE_LIMIT:
        raise DataRecoveryError("unexpected restore journal")
    journal = backup._parse_restore_journal(paths)
    suffix = record["attempt_id"][:16]
    for name in ("database", "profile"):
        path = _members(paths)[name]
        old = record["members"][name].get("sha256")
        if (journal["new_" + name + "_sha256"] != record["replacement"][name]
                or journal["old_" + name + "_sha256"] != old
                or journal["staged_" + name] != f".{path.name}.restore-{suffix}.new"
                or journal["rollback_" + name] != (None if old is None else f".{path.name}.restore-{suffix}.rollback")):
            raise DataRecoveryError("restore journal is not owned by this attempt")
        active = _read_file(path, links=True)
        if active is None:
            if old is not None:
                raise DataRecoveryError("active recovery file disappeared")
        elif active["sha256"] not in {old, record["replacement"][name]}:
            raise DataRecoveryError("active recovery file changed")
        for field, expected in (("rollback_" + name, old), ("staged_" + name, record["replacement"][name])):
            if journal[field] is not None:
                saved = _read_file(path.parent / journal[field], links=True)
                if saved is not None and saved["sha256"] != expected:
                    raise DataRecoveryError("restore publication file changed")
    return True


def _cleanup_prepublication(paths, record):
    # Only the four exact, recorded publication names are eligible. Never infer
    # ownership from a glob or from another attempt's timestamp.
    suffix = record["attempt_id"][:16]
    for name in ("database", "profile"):
        active = _members(paths)[name]
        for ending, digest in (("new", record["replacement"][name]), ("rollback", record["members"][name].get("sha256"))):
            path = active.with_name(f".{active.name}.restore-{suffix}.{ending}")
            current = _read_file(path, links=True)
            if current is not None:
                if digest is None or current["sha256"] != digest:
                    raise DataRecoveryError("restore staging conflict")
                path.unlink()
                backup._fsync_directory(path.parent)


def recover_update_backup_under_locks(paths, plan_record, *, crash_injector=None):
    """Restore under authenticated ordinary/transition/launcher lock ownership.

    The caller must authenticate inherited lock descriptors before this import.
    Only the profile lock is acquired here, with journal locking innermost.
    """
    plan = protocol.validate_plan(plan_record)
    for key in ("config_dir", "data_dir", "cache_dir"):
        if str(getattr(paths, key)) != plan["paths"][key]:
            raise DataRecoveryError("recovery paths do not match protected plan")
    if str(paths.update_recovery_dir) != plan["paths"]["recovery_root"]:
        raise DataRecoveryError("recovery root does not match protected plan")
    artifact = plan["backup"]
    current = _read_file(Path(artifact["path"]))
    if current != {key: artifact[key] for key in ("identity", "size", "sha256")}:
        raise DataRecoveryError("protected update backup changed")
    inspection = backup.inspect_backup(Path(artifact["path"]))
    if (inspection.archive_sha256 != artifact["sha256"] or inspection.manifest.application_version != plan["old_version"]
            or inspection.manifest.application_generation != artifact["data_generation"]):
        raise DataRecoveryError("protected update backup is incompatible")
    inject = crash_injector or (lambda _: None)
    raw_parent = paths.update_recovery_dir / "raw"
    ensure_private_directory_strict(raw_parent)
    store = protocol.RawRecoveryStore(paths.update_recovery_dir, plan["attempt_id"])
    raw = store.path
    with backup.exclusive_flock(paths.profile_lock_path):
        try:
            raw.lstat()
        except FileNotFoundError:
            snapshot = _preserve(paths, plan, raw, store, inspection, inject)
        else:
            snapshot = store.read_snapshot()
            if snapshot is None:
                raise DataRecoveryError("raw preservation was interrupted before publication")
        if snapshot.record["plan_sha256"] != hashlib.sha256(protocol.encode_plan(plan)).hexdigest():
            raise DataRecoveryError("raw recovery belongs to a different plan")
        _verify_saved(raw, snapshot.record)
        if snapshot.record["phase"] == "complete":
            _require_auxiliary_absent(paths, include_journal=True)
            if not _pair_matches(paths, snapshot.record, replacement=True):
                raise DataRecoveryError("completed recovery state changed")
            return raw
        if snapshot.record["phase"] in {"reverting", "reverted"}:
            if not _pair_matches(paths, snapshot.record, replacement=False):
                raise DataRecoveryError("failed restore rollback state changed")
            _restore_auxiliary(paths, raw, snapshot.record)
            if snapshot.record["phase"] != "reverted":
                _phase(store, snapshot, "reverted")
            raise DataRecoveryError("previous data recovery failed; raw target state was preserved")
        if snapshot.record["phase"] in {"preserved", "clearing"}:
            first = snapshot.record["phase"] == "preserved"
            if not _pair_matches(paths, snapshot.record, replacement=False):
                raise DataRecoveryError("failed target pair changed")
            if first:
                for name in ("database", "profile"):
                    member = snapshot.record["members"][name]
                    current = _read_file(_members(paths)[name])
                    if member["kind"] == "file" and current["identity"] != member["original_identity"]:
                        raise DataRecoveryError("failed target pair identity changed")
            snapshot = _phase(store, snapshot, "clearing")
            _clear_auxiliary(paths, snapshot.record, first=first)
            snapshot = _phase(store, snapshot, "publishing")
            inject("raw_auxiliary_cleared")
        _require_auxiliary_absent(paths)
        try:
            if _validate_publication_journal(paths, snapshot.record):
                backup._recover_restore_locked(paths, opaque_previous=True)
            if not _pair_matches(paths, snapshot.record, replacement=True):
                if not _pair_matches(paths, snapshot.record, replacement=False):
                    raise DataRecoveryError("recovery pair changed outside publication")
                _cleanup_prepublication(paths, snapshot.record)
                backup._publish_restored_state(
                    paths, raw / "replacement.database", raw / "replacement.profile",
                    suffix=plan["attempt_id"][:16], crash_injector=inject, opaque_previous=True,
                )
            backup._verify_published_pair(paths)
            _require_auxiliary_absent(paths, include_journal=True)
            _phase(store, snapshot, "complete")
            return raw
        except Exception:
            # Publication's own rollback has already restored the old raw pair.
            # Never parse it with the older application's schema.
            if _pair_matches(paths, snapshot.record, replacement=False):
                snapshot = _phase(store, snapshot, "reverting")
                _restore_auxiliary(paths, raw, snapshot.record)
                _phase(store, snapshot, "reverted")
            raise
