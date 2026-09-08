from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from arxiv_digest.maintenance import MaintenanceBarrier
from tests.unit.test_backup import initialized_paths


def recovery_fixture(tmp_path):
    import os
    from arxiv_digest.backup import export_update_backup_under_lease
    from tests.update_protocol_factory import plan_record
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    prepared = export_update_backup_under_lease(paths, "a" * 64, source_version="0.3.0")
    plan = plan_record(paths.update_recovery_dir)
    plan["paths"].update({key: str(getattr(paths, key)) for key in ("config_dir", "data_dir", "cache_dir")})
    plan["paths"]["instance_lock"] = str(paths.process_lock_path)
    plan["backup"] = {"path": str(prepared.path), "size": prepared.identity["size"],
        "sha256": prepared.inspection.archive_sha256, "identity": prepared.identity,
        "source_version": "0.3.0", "data_generation": 2,
        "pdf_destination": {"kind": prepared.pdf_destination.kind, "path": str(prepared.pdf_destination.path)}}
    raw = {"profile": b"newer profile cannot be decoded\x00", "database": b"not an older SQLite database\x00",
           "restore_journal": b"newer incompatible journal\x00", "wal": b"future WAL", "shm": b"future SHM"}
    from arxiv_digest.update_data_recovery import _members
    for name, path in _members(paths).items():
        path.write_bytes(raw[name])
        os.chmod(path, 0o600)
    return paths, plan, raw


def test_update_backup_uses_the_existing_outer_exclusive_lease(tmp_path, monkeypatch):
    import arxiv_digest.backup as backup
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    barrier = MaintenanceBarrier()
    real_export = backup.export_backup
    calls = []

    def export(*args, **kwargs):
        assert kwargs["maintenance"] is None
        calls.append(True)
        return real_export(*args, **kwargs)

    with barrier.exclusive():
        monkeypatch.setattr(backup, "export_backup", export)
        monkeypatch.setattr(barrier, "exclusive", lambda **kw: pytest.fail("nested maintenance lease"))
        result = backup.export_update_backup_under_lease(
            paths, "a" * 64, source_version="0.3.0",
            clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc),
        )
    assert calls == [True]
    assert result.inspection.manifest.application_version == "0.3.0"
    assert result.path.parent == paths.update_recovery_dir / "backups"
    assert result.path.stat().st_mode & 0o777 == 0o600
    assert result.path.stat().st_nlink == 1
    assert result.pdf_destination.path == tmp_path / "Private PDF Destination"


def test_update_backup_refuses_ambiguous_names_and_existing_files(tmp_path):
    from arxiv_digest.backup import BackupError, export_update_backup_under_lease
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    for value in ("../escape", "a"*63, "A"*64, True):
        with pytest.raises((ValueError, BackupError)):
            export_update_backup_under_lease(paths, value)
    first = export_update_backup_under_lease(paths, "b"*64)
    before = first.path.read_bytes()
    with pytest.raises(BackupError):
        export_update_backup_under_lease(paths, "b"*64, clock=lambda: first.inspection.manifest.created_at)
    assert first.path.read_bytes() == before


def test_opaque_restored_state_build_never_reads_pdf_files(tmp_path, monkeypatch):
    import arxiv_digest.backup as backup
    paths = initialized_paths(tmp_path)
    destination = backup.decode_profile(paths.profile_path.read_bytes()).pdf_destination
    output = tmp_path / "source.zip"
    backup.export_backup(paths, output, application_version="0.3.0")
    inspection = backup.inspect_backup(output)
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(backup, "_recompute_local_downloads", lambda *a, **k: pytest.fail("updater must not hash PDF files"))
    database, profile = backup._build_restored_state(
        build, inspection, destination, inspection.profile.revision,
        datetime.now(timezone.utc), preserve_download_records=True,
    )
    assert database.is_file()
    assert backup.decode_profile(profile.read_bytes()).pdf_destination == destination


def test_raw_failed_target_is_preserved_without_parsing_or_pdf_access(tmp_path, monkeypatch):
    import hashlib
    import arxiv_digest.backup as backup
    from arxiv_digest.update_data_recovery import recover_update_backup_under_locks
    from arxiv_digest.update_runtime.protocol import RawRecoveryStore
    paths, plan, expected = recovery_fixture(tmp_path)
    destination = tmp_path / "Private PDF Destination"
    def pdf_inventory():
        return {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in destination.rglob("*") if path.is_file()}
    before_pdfs = pdf_inventory()
    monkeypatch.setattr(backup.FolderService, "validate", lambda *a, **k: pytest.fail("folder picker/validation is forbidden"))
    monkeypatch.setattr(backup, "_recompute_local_downloads", lambda *a, **k: pytest.fail("PDF hashing is forbidden"))
    raw = recover_update_backup_under_locks(paths, plan)
    assert {name: (raw / name).read_bytes() for name in expected} == expected
    assert backup.decode_profile(paths.profile_path.read_bytes()).pdf_destination.path == tmp_path / "Private PDF Destination"
    assert RawRecoveryStore(paths.update_recovery_dir, "a" * 64).read_snapshot().record["phase"] == "complete"
    assert recover_update_backup_under_locks(paths, plan) == raw
    assert pdf_inventory() == before_pdfs


class Crash(BaseException):
    pass


@pytest.mark.parametrize("point", ["raw_inventory_fsynced", "raw_auxiliary_cleared", "journal_fsynced", "database_published", "profile_published"])
def test_raw_restore_replays_after_process_death(tmp_path, point):
    from arxiv_digest.update_data_recovery import recover_update_backup_under_locks
    paths, plan, raw_bytes = recovery_fixture(tmp_path)
    def crash(name):
        if name == point:
            raise Crash()
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan, crash_injector=crash)
    raw = recover_update_backup_under_locks(paths, plan)
    assert {name: (raw / name).read_bytes() for name in raw_bytes} == raw_bytes


@pytest.mark.parametrize("point", ["journal_fsynced", "database_published", "profile_published"])
def test_failed_data_publication_restores_opaque_pair_and_auxiliary(tmp_path, point):
    from arxiv_digest.backup import BackupError
    from arxiv_digest.update_data_recovery import _members, recover_update_backup_under_locks
    from arxiv_digest.update_runtime.protocol import RawRecoveryStore
    paths, plan, expected = recovery_fixture(tmp_path)
    def fail(name):
        if name == point:
            raise OSError("synthetic disk failure")
    with pytest.raises(BackupError):
        recover_update_backup_under_locks(paths, plan, crash_injector=fail)
    assert {name: path.read_bytes() for name, path in _members(paths).items()} == expected
    assert all(path.stat().st_nlink == 1 for path in _members(paths).values())
    assert RawRecoveryStore(paths.update_recovery_dir, "a" * 64).read_snapshot().record["phase"] == "reverted"


@pytest.mark.parametrize("point", ["raw_database_fsynced", "raw_profile_fsynced", "raw_restore_journal_fsynced", "raw_wal_fsynced", "raw_shm_fsynced"])
def test_incomplete_raw_preservation_does_not_mutate_failed_target(tmp_path, point):
    from arxiv_digest.update_data_recovery import DataRecoveryError, _members, recover_update_backup_under_locks
    paths, plan, expected = recovery_fixture(tmp_path)
    def crash(name):
        if name == point:
            raise Crash()
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan, crash_injector=crash)
    assert {name: path.read_bytes() for name, path in _members(paths).items()} == expected
    with pytest.raises(DataRecoveryError, match="interrupted"):
        recover_update_backup_under_locks(paths, plan)


def test_replaced_raw_member_blocks_without_overwriting_active_files(tmp_path):
    from arxiv_digest.update_data_recovery import DataRecoveryError, _members, recover_update_backup_under_locks
    paths, plan, expected = recovery_fixture(tmp_path)
    def crash(name):
        if name == "raw_inventory_fsynced":
            raise Crash()
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan, crash_injector=crash)
    saved = paths.update_recovery_dir / "raw" / ("a" * 64) / "profile"
    content = saved.read_bytes()
    saved.unlink()
    saved.write_bytes(content)
    saved.chmod(0o600)
    with pytest.raises(DataRecoveryError, match="inventory changed"):
        recover_update_backup_under_locks(paths, plan)
    assert {name: path.read_bytes() for name, path in _members(paths).items()} == expected


def test_absent_raw_auxiliary_files_are_explicit_and_remain_absent(tmp_path):
    from arxiv_digest.update_data_recovery import _members, recover_update_backup_under_locks
    from arxiv_digest.update_runtime.protocol import RawRecoveryStore
    paths, plan, _ = recovery_fixture(tmp_path)
    for name in ("restore_journal", "wal", "shm"):
        _members(paths)[name].unlink()
    raw = recover_update_backup_under_locks(paths, plan)
    record = RawRecoveryStore(paths.update_recovery_dir, "a" * 64).read_snapshot().record
    for name in ("restore_journal", "wal", "shm"):
        assert record["members"][name] == {"kind": "absent"}
        assert not (raw / name).exists()
        assert not _members(paths)[name].exists()


def test_unknown_active_bytes_after_crash_are_never_overwritten(tmp_path):
    from arxiv_digest.update_data_recovery import DataRecoveryError, recover_update_backup_under_locks
    paths, plan, _ = recovery_fixture(tmp_path)
    def crash(name):
        if name == "database_published":
            raise Crash()
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan, crash_injector=crash)
    paths.database_path.write_bytes(b"unrelated concurrent replacement")
    with pytest.raises(DataRecoveryError, match="active recovery file changed"):
        recover_update_backup_under_locks(paths, plan)
    assert paths.database_path.read_bytes() == b"unrelated concurrent replacement"


@pytest.mark.parametrize("member,point", [("database", "database_published"), ("profile", "profile_published")])
def test_failed_publication_rollback_never_overwrites_unexpected_active_bytes(tmp_path, member, point):
    from arxiv_digest.backup import BackupError
    from arxiv_digest.update_data_recovery import _members, recover_update_backup_under_locks
    paths, plan, expected = recovery_fixture(tmp_path)
    selected = _members(paths)[member]
    replacement = b"unrelated concurrent replacement must survive"
    def fail(name):
        if name == point:
            selected.write_bytes(replacement)
            raise OSError("publication discovered external change")
    with pytest.raises(BackupError):
        recover_update_backup_under_locks(paths, plan, crash_injector=fail)
    assert selected.read_bytes() == replacement
    raw = paths.update_recovery_dir / "raw" / plan["attempt_id"]
    assert (raw / member).read_bytes() == expected[member]


@pytest.mark.parametrize("member", ["wal", "shm"])
def test_unexpected_auxiliary_after_clearing_blocks_before_data_publication(tmp_path, member):
    from arxiv_digest.update_data_recovery import DataRecoveryError, _members, recover_update_backup_under_locks
    paths, plan, expected = recovery_fixture(tmp_path)
    def crash(name):
        if name == "raw_auxiliary_cleared":
            raise Crash()
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan, crash_injector=crash)
    selected = _members(paths)[member]
    selected.write_bytes(b"unexpected later auxiliary file")
    selected.chmod(0o600)
    with pytest.raises(DataRecoveryError):
        recover_update_backup_under_locks(paths, plan)
    assert selected.read_bytes() == b"unexpected later auxiliary file"
    assert paths.database_path.read_bytes() == expected["database"]
    assert paths.profile_path.read_bytes() == expected["profile"]


@pytest.mark.parametrize("member", ["wal", "shm", "restore_journal"])
def test_completed_raw_recovery_refuses_later_auxiliary_files(tmp_path, member):
    from arxiv_digest.update_data_recovery import DataRecoveryError, _members, recover_update_backup_under_locks
    paths, plan, _ = recovery_fixture(tmp_path)
    recover_update_backup_under_locks(paths, plan)
    selected = _members(paths)[member]
    selected.write_bytes(b"preserve unexpected post-recovery state")
    selected.chmod(0o600)
    with pytest.raises(DataRecoveryError):
        recover_update_backup_under_locks(paths, plan)
    assert selected.read_bytes() == b"preserve unexpected post-recovery state"


@pytest.mark.parametrize("missing", [("database",), ("profile",), ("database", "profile")])
def test_absent_failed_target_pair_members_are_preserved_explicitly(tmp_path, missing):
    from arxiv_digest.update_data_recovery import _members, recover_update_backup_under_locks
    from arxiv_digest.update_runtime.protocol import RawRecoveryStore
    paths, plan, _ = recovery_fixture(tmp_path)
    for member in missing:
        _members(paths)[member].unlink()
    raw = recover_update_backup_under_locks(paths, plan)
    record = RawRecoveryStore(paths.update_recovery_dir, plan["attempt_id"]).read_snapshot().record
    for member in missing:
        assert record["members"][member] == {"kind": "absent"}
        assert not (raw / member).exists()
        assert _members(paths)[member].is_file()


def test_restore_replays_a_crash_after_staging_before_normal_journal(tmp_path, monkeypatch):
    import arxiv_digest.backup as backup
    from arxiv_digest.update_data_recovery import recover_update_backup_under_locks
    paths, plan, expected = recovery_fixture(tmp_path)
    original = backup._write_restore_journal
    def crash(*args):
        raise Crash()
    monkeypatch.setattr(backup, "_write_restore_journal", crash)
    with pytest.raises(Crash):
        recover_update_backup_under_locks(paths, plan)
    monkeypatch.setattr(backup, "_write_restore_journal", original)
    raw = recover_update_backup_under_locks(paths, plan)
    assert {name: (raw / name).read_bytes() for name in expected} == expected
    assert paths.profile_path.stat().st_nlink == 1
    assert paths.database_path.stat().st_nlink == 1


def test_raw_sets_are_pruned_only_by_exact_registered_inventory(tmp_path):
    import copy
    import os
    from arxiv_digest.backup import export_update_backup_under_lease
    from arxiv_digest.update_data_recovery import _members, recover_update_backup_under_locks
    from arxiv_digest.update_artifacts import prune_update_artifacts, register_update_artifact
    from tests.unit.test_update_artifacts import healthy
    paths, initial_plan, failed = recovery_fixture(tmp_path)
    raw_sets = []
    for index in range(3):
        plan = copy.deepcopy(initial_plan)
        attempt = str(index) * 64
        plan["attempt_id"] = attempt
        plan["paths"]["snapshot"] = str(Path(plan["paths"]["snapshot"]).with_name(attempt))
        plan["paths"]["forensic"] = plan["paths"]["snapshot"] + ".failed"
        if index:
            prepared = export_update_backup_under_lease(paths, attempt, source_version="0.3.0")
            plan["backup"].update(path=str(prepared.path), identity=prepared.identity,
                size=prepared.identity["size"], sha256=prepared.inspection.archive_sha256)
            for name, path in _members(paths).items():
                path.write_bytes(failed[name])
                os.chmod(path, 0o600)
        raw = recover_update_backup_under_locks(paths, plan)
        raw_sets.append(raw)
        register_update_artifact(paths.update_recovery_dir, "raw", attempt, "0.3.0", raw / "raw-recovery.json", created_at_ns=index)
    unowned = paths.update_recovery_dir / "raw" / ("f" * 64)
    unowned.mkdir(mode=0o700)
    (unowned / "user-file").write_bytes(b"preserve")
    healthy(paths.update_recovery_dir)
    assert prune_update_artifacts(paths.update_recovery_dir) == [raw_sets[0] / "raw-recovery.json"]
    assert not raw_sets[0].exists()
    assert all(path.exists() for path in raw_sets[1:])
    assert (unowned / "user-file").read_bytes() == b"preserve"
