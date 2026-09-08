from __future__ import annotations

import json
import os
import fcntl
import sqlite3
import stat
import warnings
import zipfile
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from arxiv_digest import __version__
from arxiv_digest.models import CategoryConfig, PaperMetadata, PaperVersion
from arxiv_digest.paths import AppPaths, resolve_paths
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
)
from arxiv_digest.setup import SetupService
from arxiv_digest.storage.database import open_database
from arxiv_digest.storage.store import DownloadFileRecord, Store


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def test_portable_backup_uses_format_and_record_schema_two() -> None:
    from arxiv_digest.backup import FORMAT_VERSION, RECORD_SCHEMA_VERSION

    assert FORMAT_VERSION == 2
    assert RECORD_SCHEMA_VERSION == 2


def initialized_paths(tmp_path: Path) -> AppPaths:
    paths = resolve_paths(
        platform="linux",
        home=tmp_path,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "isolated"),
        },
    )
    paths.ensure()
    destination = tmp_path / "Private PDF Destination"
    destination.mkdir(parents=True)
    open_database(paths.database_path).close()
    repository = ProfileRepository(paths.profile_path, paths.profile_lock_path)
    profile = Profile(
        schema_version=2,
        revision=1,
        category_coverage=(ProfileCategory("cs.SE", date(2026, 8, 1)),),
        keywords=("fictional keyword",),
        phrases=("synthetic phrase",),
        authors=("Ada Example",),
        seed_papers=("2608.41001",),
        pdf_destination=PdfDestination("custom", destination),
    )
    SetupService(paths.database_path, repository).publish_profile(
        profile,
        (CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1)),),
        expected_revision=None,
    )
    store = Store(paths.database_path)
    store.apply_article_snapshot(
        PaperMetadata(
            arxiv_id="2608.41001",
            title="Portable Fictional Lattices",
            authors=("Ada Example",),
            abstract="A wholly synthetic backup fixture.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        (PaperVersion(1, datetime(2026, 8, 1, tzinfo=timezone.utc)),),
    )
    store.save_paper("2608.41001", 1)
    local_pdf = destination / "2608.41001v1 - Portable Fictional Lattices.pdf"
    local_pdf.write_bytes(b"%PDF-1.7\nsynthetic private local file\n")
    store.record_download_file(
        DownloadFileRecord(
            arxiv_id="2608.41001",
            version=1,
            filename=local_pdf.name,
            byte_count=local_pdf.stat().st_size,
            sha256=sha256(local_pdf.read_bytes()).hexdigest(),
            last_verified_at=NOW,
        )
    )
    (paths.cache_dir / "raw-response.xml").write_text(
        "private cache payload", encoding="utf-8"
    )
    paths.runtime_descriptor_path.write_text(
        '{"token":"private-runtime-token"}', encoding="utf-8"
    )
    connection = open_database(paths.database_path)
    connection.execute(
        """UPDATE category_sync_state
           SET completed_through_utc = ?, last_success_at = ?,
               last_error_code = ?, last_error_message = ?
           WHERE category = ?""",
        (
            "2026-08-20",
            "2026-08-20T12:00:00Z",
            "synthetic_error_code",
            "private synchronization detail /private/source",
            "cs.SE",
        ),
    )
    connection.execute(
        """INSERT INTO oai_tombstones(
               oai_identifier, arxiv_id, oai_datestamp,
               set_specs_json, observed_at
           ) VALUES (?, ?, ?, ?, ?)""",
        (
            "oai:arXiv.org:2608.49998",
            None,
            "2026-08-19",
            '["cs:SE"]',
            "2026-08-20T12:00:00Z",
        ),
    )
    connection.execute(
        """INSERT INTO category_article_state(
               category, arxiv_id, last_oai_datestamp, category_set_hash,
               observed_categories_json, last_raw_sha256, last_seen_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "2608.41001",
            "2026-08-20",
            "1" * 64,
            '["cs.SE"]',
            "2" * 64,
            "2026-08-20T12:00:00Z",
        ),
    )
    connection.executemany(
        """INSERT INTO source_observations(
               observation_id, source_key, arxiv_id, source, category,
               announce_type, daily_list_date, announced_version,
               list_position, oai_datestamp, response_sha256, observed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            (
                51,
                "catchup:cs.SE:2026-08-20:0:2608.41001",
                "2608.41001",
                "catchup",
                "cs.SE",
                "new",
                "2026-08-20",
                None,
                0,
                None,
                "3" * 64,
                "2026-08-20T12:00:00Z",
            ),
            (
                52,
                "atom:cs.SE:2608.41001:v1",
                "2608.41001",
                "atom",
                "cs.SE",
                "new",
                None,
                1,
                0,
                None,
                "4" * 64,
                "2026-08-20T12:00:00Z",
            ),
        ),
    )
    connection.execute(
        """INSERT INTO catchup_days(
               category, daily_list_date, status, attempted_at,
               response_sha256, error_code
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "2026-08-20",
            "complete",
            "2026-08-20T12:00:00Z",
            "3" * 64,
            None,
        ),
    )
    connection.execute(
        """INSERT INTO canonical_events(
               event_id, arxiv_id, daily_list_date, announced_version,
               version_resolution, queue_revision, reviewed_at,
               recovered_after_finish, conflict_code
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            41,
            "2608.41001",
            "2026-08-20",
            1,
            "atom_confirmed",
            7,
            None,
            0,
            None,
        ),
    )
    connection.executemany(
        """INSERT INTO canonical_event_observations(event_id, observation_id)
           VALUES (?, ?)""",
        ((41, 51), (41, 52)),
    )
    connection.execute(
        """INSERT INTO review_date_state(
               daily_list_date, anchor_event_id, profile_revision,
               last_finished_at, last_finished_revision
           ) VALUES (?, ?, ?, ?, ?)""",
        ("2026-08-20", 41, 1, None, None),
    )
    connection.execute(
        """INSERT INTO sync_runs(
               category, run_kind, requested_from, started_at, status,
               error_code, error_message
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "cs.SE",
            "incremental",
            "2026-08-20",
            "2026-08-20T12:00:00Z",
            "failed",
            "fixture_network_error",
            "private transient run detail /private/source",
        ),
    )
    connection.execute(
        "UPDATE state_meta SET queue_revision = 7 WHERE singleton = 1"
    )
    connection.execute(
        "UPDATE application_settings SET launcher_operation = 'create_failed', "
        "launcher_last_error_code = 'private-launcher-detail' WHERE singleton = 1"
    )
    connection.commit()
    connection.close()
    return paths


def rewrite_archive_member(
    source: Path,
    destination: Path,
    name: str,
    payload: bytes,
    *,
    refresh_manifest: bool = True,
) -> None:
    with zipfile.ZipFile(source) as archive:
        members = {item: archive.read(item) for item in archive.namelist()}
    members[name] = payload
    if refresh_manifest and name in {"profile.json", "state.jsonl"}:
        manifest = json.loads(members["manifest.json"])
        for item in manifest["members"]:
            if item["name"] == name:
                item["byte_count"] = len(payload)
                item["sha256"] = sha256(payload).hexdigest()
        members["manifest.json"] = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    with zipfile.ZipFile(destination, "w") as archive:
        for member_name in ("manifest.json", "profile.json", "state.jsonl"):
            archive.writestr(member_name, members[member_name])


def archive_payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_member_list(
    path: Path,
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members:
            archive.writestr(name, payload)


def test_export_uses_generation_two_portable_record_allowlist(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "portable.zip"

    export_backup(paths, archive, clock=lambda: NOW)

    records = [
        json.loads(line)
        for line in archive_payloads(archive)["state.jsonl"].splitlines()
    ]
    record_types = {record["record_type"] for record in records}
    assert record_types == {
        "article",
        "oai_tombstone",
        "version",
        "author",
        "category",
        "category_sync",
        "category_article_state",
        "source_observation",
        "catchup_day",
        "canonical_event",
        "canonical_event_observation",
        "review_date_state",
        "saved_paper",
        "download_file",
    }
    assert {record["schema_version"] for record in records} == {2}


def test_export_manifest_declares_application_generation_two(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "portable.zip"

    manifest = export_backup(paths, archive, clock=lambda: NOW)
    manifest_payload = json.loads(archive_payloads(archive)["manifest.json"])

    assert manifest.application_generation == 2
    assert manifest_payload["application_generation"] == 2
    assert manifest_payload["format_version"] == 2


def test_export_rejects_a_database_from_another_application_generation(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup

    paths = initialized_paths(tmp_path)
    connection = sqlite3.connect(paths.database_path)
    connection.execute("UPDATE application_generation SET generation = 1")
    connection.commit()
    connection.close()
    archive = tmp_path / "portable.zip"

    with pytest.raises(BackupError) as raised:
        export_backup(paths, archive, clock=lambda: NOW)

    assert raised.value.code == "unsupported_schema"
    assert not archive.exists()


def test_export_rejects_a_database_without_a_generation_marker(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup

    paths = initialized_paths(tmp_path)
    connection = sqlite3.connect(paths.database_path)
    connection.execute("DROP TABLE application_generation")
    connection.commit()
    connection.close()
    archive = tmp_path / "portable.zip"

    with pytest.raises(BackupError) as raised:
        export_backup(paths, archive, clock=lambda: NOW)

    assert raised.value.code == "unsupported_schema"
    assert not archive.exists()


def test_portable_profile_round_trips_exact_category_coverage(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "portable.zip"

    export_backup(paths, archive, clock=lambda: NOW)
    portable_profile = json.loads(archive_payloads(archive)["profile.json"])
    inspection = inspect_backup(archive)

    assert portable_profile["schema_version"] == 2
    assert portable_profile["category_coverage"] == [
        {"category": "cs.SE", "coverage_start": "2026-08-01"}
    ]
    assert "categories" not in portable_profile
    assert inspection.profile.category_coverage == (
        ProfileCategory("cs.SE", date(2026, 8, 1)),
    )


def test_inspection_rejects_v1_without_modifying_archive_or_local_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    legacy = tmp_path / "legacy-v1.zip"
    export_backup(paths, current, clock=lambda: NOW)
    members = archive_payloads(current)
    manifest = json.loads(members["manifest.json"])
    manifest["format_version"] = 1
    rewrite_archive_member(
        current,
        legacy,
        "manifest.json",
        (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )
    before = {
        "archive": sha256(legacy.read_bytes()).digest(),
        "database": sha256(paths.database_path.read_bytes()).digest(),
        "profile": sha256(paths.profile_path.read_bytes()).digest(),
    }

    with pytest.raises(BackupError) as raised:
        inspect_backup(legacy)

    assert raised.value.code == "unsupported_schema"
    assert sha256(legacy.read_bytes()).digest() == before["archive"]
    assert sha256(paths.database_path.read_bytes()).digest() == before["database"]
    assert sha256(paths.profile_path.read_bytes()).digest() == before["profile"]


def test_inspection_rejects_another_application_generation(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    incompatible = tmp_path / "wrong-generation.zip"
    export_backup(paths, current, clock=lambda: NOW)
    manifest = json.loads(archive_payloads(current)["manifest.json"])
    manifest["application_generation"] = 1
    rewrite_archive_member(
        current,
        incompatible,
        "manifest.json",
        (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )
    before = sha256(incompatible.read_bytes()).digest()

    with pytest.raises(BackupError) as raised:
        inspect_backup(incompatible)

    assert raised.value.code == "unsupported_schema"
    assert sha256(incompatible.read_bytes()).digest() == before


def test_inspection_rejects_portable_profile_schema_one(tmp_path: Path) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    incompatible = tmp_path / "profile-v1.zip"
    export_backup(paths, current, clock=lambda: NOW)
    profile = json.loads(archive_payloads(current)["profile.json"])
    profile["schema_version"] = 1
    rewrite_archive_member(
        current,
        incompatible,
        "profile.json",
        (
            json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )

    with pytest.raises(BackupError) as raised:
        inspect_backup(incompatible)

    assert raised.value.code == "unsupported_schema"


def test_inspection_rejects_portable_record_schema_one(tmp_path: Path) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    incompatible = tmp_path / "records-v1.zip"
    export_backup(paths, current, clock=lambda: NOW)
    records = [
        json.loads(line)
        for line in archive_payloads(current)["state.jsonl"].splitlines()
    ]
    records[0]["schema_version"] = 1
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(
        current,
        incompatible,
        "state.jsonl",
        state_payload,
    )

    with pytest.raises(BackupError) as raised:
        inspect_backup(incompatible)

    assert raised.value.code == "unsupported_schema"


def test_inspection_rejects_unsafe_portable_download_filename(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    hostile = tmp_path / "unsafe-download.zip"
    export_backup(paths, current, clock=lambda: NOW)
    records = [
        json.loads(line)
        for line in archive_payloads(current)["state.jsonl"].splitlines()
    ]
    download = next(
        record for record in records if record["record_type"] == "download_file"
    )
    download["payload"]["filename"] = "../outside.pdf"
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(current, hostile, "state.jsonl", state_payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "unsupported_schema"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", 0),
        ("byte_count", 0),
        ("sha256", "not-a-sha256"),
        ("last_verified_at", "not-a-timestamp"),
    ),
)
def test_inspection_rejects_invalid_portable_download_metadata(
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    current = tmp_path / "current.zip"
    hostile = tmp_path / f"invalid-download-{field}.zip"
    export_backup(paths, current, clock=lambda: NOW)
    records = [
        json.loads(line)
        for line in archive_payloads(current)["state.jsonl"].splitlines()
    ]
    download = next(
        record for record in records if record["record_type"] == "download_file"
    )
    download["payload"][field] = value
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(current, hostile, "state.jsonl", state_payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "unsupported_schema"


def test_export_is_deterministic_portable_and_excludes_machine_local_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import export_backup

    paths = initialized_paths(tmp_path)
    first = tmp_path / "first.arxiv-digest-backup.zip"
    second = tmp_path / "second.arxiv-digest-backup.zip"

    export_backup(paths, first, clock=lambda: NOW)
    export_backup(paths, second, clock=lambda: NOW)

    assert first.read_bytes() == second.read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "profile.json",
            "state.jsonl",
        ]
        manifest_payload = archive.read("manifest.json")
        profile_payload = archive.read("profile.json")
        state_payload = archive.read("state.jsonl")
    manifest = json.loads(manifest_payload)
    assert manifest == {
        "application_generation": 2,
        "application_version": __version__,
        "created_at": "2026-08-22T12:00:00Z",
        "format_name": "arxiv-digest-backup",
        "format_version": 2,
        "members": [
            {
                "byte_count": len(profile_payload),
                "name": "profile.json",
                "sha256": sha256(profile_payload).hexdigest(),
            },
            {
                "byte_count": len(state_payload),
                "name": "state.jsonl",
                "sha256": sha256(state_payload).hexdigest(),
            },
        ],
    }
    portable_profile = json.loads(profile_payload)
    assert portable_profile == {
        "authors": ["Ada Example"],
        "category_coverage": [
            {"category": "cs.SE", "coverage_start": "2026-08-01"}
        ],
        "keywords": ["fictional keyword"],
        "phrases": ["synthetic phrase"],
        "revision": 1,
        "schema_version": 2,
        "seed_papers": ["2608.41001"],
    }
    records = [json.loads(line) for line in state_payload.splitlines()]
    assert any(record["record_type"] == "article" for record in records)
    assert any(record["record_type"] == "saved_paper" for record in records)
    assert {record["record_type"] for record in records} >= {
        "article",
        "oai_tombstone",
        "version",
        "author",
        "category",
        "category_sync",
        "category_article_state",
        "source_observation",
        "catchup_day",
        "canonical_event",
        "canonical_event_observation",
        "review_date_state",
        "saved_paper",
        "download_file",
    }
    archive_bytes = first.read_bytes()
    for excluded in (
        str(paths.profile_path.parent).encode(),
        str(tmp_path / "Private PDF Destination").encode(),
        b"private cache payload",
        b"private-runtime-token",
        b"private-launcher-detail",
        b"private synchronization detail",
        b"private enrichment detail",
        b"private transient run detail",
        b"sync_run",
        b"%PDF-1.7",
    ):
        assert excluded not in archive_bytes


def test_export_fsyncs_verified_archive_before_linking_and_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import arxiv_digest.backup as backup

    paths = initialized_paths(tmp_path)
    destination = tmp_path / "durable.zip"
    calls = []
    original_link = backup.os.link

    monkeypatch.setattr(
        backup,
        "_fsync_file",
        lambda path: calls.append(("file", Path(path).parent)),
    )

    def tracked_link(source, target):
        calls.append(("link", Path(target).parent))
        return original_link(source, target)

    monkeypatch.setattr(backup.os, "link", tracked_link)
    monkeypatch.setattr(
        backup,
        "_fsync_directory",
        lambda path: calls.append(("directory", Path(path))),
    )

    backup.export_backup(paths, destination, clock=lambda: NOW)

    assert calls == [
        ("file", destination.parent),
        ("link", destination.parent),
        ("directory", destination.parent),
    ]


def test_inspection_parses_a_valid_backup_without_changing_local_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import inspect_backup

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "portable.arxiv-digest-backup.zip"
    from arxiv_digest.backup import export_backup

    export_backup(paths, archive, clock=lambda: NOW)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    inspection = inspect_backup(archive)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert inspection.path == archive
    assert inspection.archive_sha256 == sha256(archive.read_bytes()).hexdigest()
    assert inspection.manifest.format_name == "arxiv-digest-backup"
    assert inspection.profile.categories == ("cs.SE",)
    assert inspection.profile.seed_papers == ("2608.41001",)
    assert {record.record_type for record in inspection.records} >= {
        "article",
        "version",
        "author",
        "category",
        "category_sync",
        "saved_paper",
    }


def test_inspection_rejects_an_active_category_without_sync_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    hostile = tmp_path / "hostile.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    with zipfile.ZipFile(valid) as archive:
        records = [
            json.loads(line)
            for line in archive.read("state.jsonl").splitlines()
        ]
    records = [
        record
        for record in records
        if record["record_type"] != "category_sync"
    ]
    payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(valid, hostile, "state.jsonl", payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "cross_record_invalid"


def test_inspection_rejects_profile_coverage_misaligned_with_sync_state(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    hostile = tmp_path / "misaligned-coverage.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    profile = json.loads(archive_payloads(valid)["profile.json"])
    profile["category_coverage"][0]["coverage_start"] = "2026-08-02"
    rewrite_archive_member(
        valid,
        hostile,
        "profile.json",
        (
            json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "cross_record_invalid"


def test_inspection_rejects_a_seed_without_durable_article_metadata(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    hostile = tmp_path / "hostile.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    with zipfile.ZipFile(valid) as archive:
        records = [
            json.loads(line)
            for line in archive.read("state.jsonl").splitlines()
        ]
    records = [
        record
        for record in records
        if record["record_type"]
        not in {"article", "version", "author", "category", "saved_paper"}
    ]
    payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(valid, hostile, "state.jsonl", payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "cross_record_invalid"


@pytest.mark.parametrize(
    ("start", "until"),
    [
        ("2026-07-01", None),
        (None, "2026-07-31"),
        ("2026-08-01", "2026-07-31"),
    ],
)
def test_inspection_rejects_invalid_pending_backfill_bounds(
    tmp_path: Path,
    start: str | None,
    until: str | None,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    hostile = tmp_path / "hostile.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    with zipfile.ZipFile(valid) as archive:
        records = [
            json.loads(line)
            for line in archive.read("state.jsonl").splitlines()
        ]
    sync = next(
        record for record in records if record["record_type"] == "category_sync"
    )
    sync["payload"]["pending_backfill_start"] = start
    sync["payload"]["pending_backfill_until"] = until
    payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(valid, hostile, "state.jsonl", payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == "cross_record_invalid"


def test_export_refuses_to_overwrite_an_existing_destination(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup

    paths = initialized_paths(tmp_path)
    destination = tmp_path / "existing.zip"
    original = b"preexisting private bytes"
    destination.write_bytes(original)

    with pytest.raises(BackupError) as raised:
        export_backup(paths, destination, clock=lambda: NOW)

    assert raised.value.code == "destination_exists"
    assert destination.read_bytes() == original


@pytest.mark.parametrize(
    ("hostile_kind", "expected_code"),
    [
        ("duplicate", "duplicate_member"),
        ("traversal", "path_traversal"),
        ("symlink", "symlink_member"),
        ("encrypted", "encrypted_member"),
        ("unknown", "unknown_member"),
        ("bad_crc", "archive_invalid"),
    ],
)
def test_inspection_rejects_hostile_zip_members(
    tmp_path: Path,
    hostile_kind: str,
    expected_code: str,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    hostile = tmp_path / f"{hostile_kind}.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    members = archive_payloads(valid)
    ordinary = [
        (name, members[name])
        for name in ("manifest.json", "profile.json", "state.jsonl")
    ]
    if hostile_kind == "duplicate":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            write_member_list(
                hostile,
                ordinary + [("profile.json", members["profile.json"])],
            )
    elif hostile_kind == "traversal":
        write_member_list(
            hostile,
            ordinary + [("../profile.json", b"unsafe")],
        )
    elif hostile_kind == "unknown":
        write_member_list(hostile, ordinary + [("cache/raw.xml", b"unsafe")])
    elif hostile_kind == "symlink":
        symlink = zipfile.ZipInfo("profile.json")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        write_member_list(
            hostile,
            [
                ("manifest.json", members["manifest.json"]),
                (symlink, b"state.jsonl"),
                ("state.jsonl", members["state.jsonl"]),
            ],
        )
    elif hostile_kind == "encrypted":
        payload = bytearray(valid.read_bytes())
        for signature, flag_offset in (
            (b"PK\x03\x04", 6),
            (b"PK\x01\x02", 8),
        ):
            position = 0
            while (position := payload.find(signature, position)) >= 0:
                offset = position + flag_offset
                flags = int.from_bytes(payload[offset : offset + 2], "little")
                payload[offset : offset + 2] = (flags | 1).to_bytes(2, "little")
                position += 4
        hostile.write_bytes(payload)
    else:
        payload = bytearray(valid.read_bytes())
        marker = members["state.jsonl"][:32]
        position = payload.find(marker)
        assert position >= 0
        payload[position] ^= 1
        hostile.write_bytes(payload)

    with pytest.raises(BackupError) as raised:
        inspect_backup(hostile)

    assert raised.value.code == expected_code


def test_inspection_enforces_member_and_expanded_size_limits(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        BackupLimits,
        export_backup,
        inspect_backup,
    )

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "valid.zip"
    export_backup(paths, archive, clock=lambda: NOW)

    with pytest.raises(BackupError) as count_error:
        inspect_backup(archive, limits=BackupLimits(max_members=2))
    with pytest.raises(BackupError) as size_error:
        inspect_backup(
            archive,
            limits=BackupLimits(
                max_members=16,
                max_member_bytes=1024 * 1024,
                max_expanded_bytes=100,
            ),
        )

    assert count_error.value.code == "archive_too_large"
    assert size_error.value.code == "archive_too_large"


def test_default_backup_limits_bound_each_semantic_member() -> None:
    from arxiv_digest.backup import BackupLimits

    limits = BackupLimits()

    assert limits.max_manifest_bytes == 256 * 1024
    assert limits.max_profile_bytes == 4 * 1024 * 1024
    assert limits.max_state_bytes == 128 * 1024 * 1024
    assert (
        limits.max_manifest_bytes
        + limits.max_profile_bytes
        + limits.max_state_bytes
        < limits.max_expanded_bytes
    )


@pytest.mark.parametrize(
    ("member_name", "limit_name"),
    [
        ("manifest.json", "max_manifest_bytes"),
        ("profile.json", "max_profile_bytes"),
        ("state.jsonl", "max_state_bytes"),
    ],
)
def test_inspection_enforces_semantic_member_size_limits(
    tmp_path: Path,
    member_name: str,
    limit_name: str,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        BackupLimits,
        export_backup,
        inspect_backup,
    )

    paths = initialized_paths(tmp_path)
    archive = tmp_path / "valid.zip"
    export_backup(paths, archive, clock=lambda: NOW)
    members = archive_payloads(archive)
    semantic_limits = {
        "max_manifest_bytes": 1024 * 1024,
        "max_profile_bytes": 1024 * 1024,
        "max_state_bytes": 1024 * 1024,
    }
    semantic_limits[limit_name] = len(members[member_name]) - 1

    with pytest.raises(BackupError) as raised:
        inspect_backup(
            archive,
            limits=BackupLimits(
                max_member_bytes=2 * 1024 * 1024,
                max_expanded_bytes=4 * 1024 * 1024,
                **semantic_limits,
            ),
        )

    assert raised.value.code == "archive_too_large"


def test_inspection_rejects_checksum_mismatch_and_unsupported_schemas(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_backup

    paths = initialized_paths(tmp_path)
    valid = tmp_path / "valid.zip"
    export_backup(paths, valid, clock=lambda: NOW)
    members = archive_payloads(valid)

    checksum = tmp_path / "checksum.zip"
    rewrite_archive_member(
        valid,
        checksum,
        "profile.json",
        members["profile.json"] + b" ",
        refresh_manifest=False,
    )
    with pytest.raises(BackupError) as mismatch:
        inspect_backup(checksum)
    assert mismatch.value.code == "checksum_mismatch"

    manifest_schema = tmp_path / "manifest-schema.zip"
    manifest = json.loads(members["manifest.json"])
    manifest["format_version"] = 3
    rewrite_archive_member(
        valid,
        manifest_schema,
        "manifest.json",
        (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )
    with pytest.raises(BackupError) as manifest_error:
        inspect_backup(manifest_schema)
    assert manifest_error.value.code == "unsupported_schema"

    record_schema = tmp_path / "record-schema.zip"
    records = [
        json.loads(line) for line in members["state.jsonl"].splitlines()
    ]
    records[0]["schema_version"] = 3
    state_payload = b"".join(
        (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        for record in records
    )
    rewrite_archive_member(valid, record_schema, "state.jsonl", state_payload)
    with pytest.raises(BackupError) as record_error:
        inspect_backup(record_schema)
    assert record_error.value.code == "unsupported_schema"


def test_restore_revalidates_the_archive_digest_after_inspection(
    tmp_path: Path,
) -> None:
    from arxiv_digest.backup import (
        BackupError,
        export_backup,
        inspect_backup,
        restore_backup,
    )

    source = initialized_paths(tmp_path / "source")
    archive = tmp_path / "portable.zip"
    export_backup(source, archive, clock=lambda: NOW)
    inspection = inspect_backup(archive)
    with archive.open("ab") as handle:
        handle.write(b"valid ZIP trailing bytes change the file identity")
    target = resolve_paths(
        platform="linux",
        home=tmp_path,
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "target"),
        },
    )
    destination = tmp_path / "Confirmed PDFs"
    destination.mkdir()

    with pytest.raises(BackupError) as raised:
        restore_backup(
            target,
            inspection,
            PdfDestination("custom", destination),
            clock=lambda: NOW,
        )

    assert raised.value.code == "archive_changed"
    assert not target.profile_path.exists()
    assert not target.database_path.exists()


def test_portable_source_inspection_returns_only_frozen_validated_metadata(tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError, fields
    from arxiv_digest.backup import inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    maintenance = MaintenanceBarrier()
    before = (paths.profile_path.read_bytes(), paths.database_path.read_bytes())
    result = inspect_portable_backup_source(paths, maintenance=maintenance)
    assert result.application_generation == 2
    assert result.database_schema_version == 4
    assert result.profile_schema_version == 2
    assert result.profile_revision == 1
    assert {field.name for field in fields(result)} == {
        "application_generation", "database_schema_version", "profile_schema_version", "profile_revision",
    }
    with pytest.raises(FrozenInstanceError):
        result.profile_revision = 2
    assert (paths.profile_path.read_bytes(), paths.database_path.read_bytes()) == before
    assert maintenance.active_operations == 0


@pytest.mark.parametrize("field", ["profile_lock_path", "profile_path", "database_path"])
@pytest.mark.parametrize("unsafe", ["missing", "mode", "symlink", "hardlink"])
def test_portable_source_inspection_does_not_create_or_repair_unsafe_sources(
    tmp_path: Path, field: str, unsafe: str,
) -> None:
    from arxiv_digest.backup import BackupError, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    target = getattr(paths, field)
    original = tmp_path / "original"
    if unsafe == "missing":
        target.unlink()
    elif unsafe == "mode":
        target.chmod(0o644)
    elif unsafe == "symlink":
        target.rename(original)
        target.symlink_to(original)
    else:
        os.link(target, original)
    before = {path: (path.lstat().st_mode, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(BackupError):
        inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    after = {path: (path.lstat().st_mode, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    if unsafe == "missing":
        assert not target.exists()


def test_portable_source_inspection_lock_wait_is_bounded_and_no_follow(tmp_path: Path) -> None:
    from arxiv_digest.backup import BackupError, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    descriptor = os.open(paths.profile_lock_path, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        with pytest.raises(BackupError) as caught:
            inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier(), timeout=0.01)
        assert caught.value.code == "source_busy"
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("statement,code", [
    ("UPDATE application_generation SET generation=1", "unsupported_schema"),
    ("DELETE FROM schema_migrations WHERE version=4", "unsupported_schema"),
    ("UPDATE profile_publication SET status='pending'", "publication_incomplete"),
    ("DELETE FROM category_sync_state", "cross_record_invalid"),
    ("DELETE FROM canonical_event_observations WHERE observation_id=51", "cross_record_invalid"),
    ("DELETE FROM article_versions", "cross_record_invalid"),
])
def test_portable_source_and_export_share_rejection_of_invalid_durable_data(
    tmp_path: Path, statement: str, code: str,
) -> None:
    from arxiv_digest.backup import BackupError, export_backup, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    connection = sqlite3.connect(paths.database_path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    before = (paths.profile_path.read_bytes(), paths.database_path.read_bytes())
    with pytest.raises(BackupError) as inspection_error:
        inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert inspection_error.value.code == code
    with pytest.raises(BackupError) as export_error:
        export_backup(paths, tmp_path / "invalid.zip", clock=lambda: NOW)
    assert export_error.value.code == code
    assert not (tmp_path / "invalid.zip").exists()
    assert (paths.profile_path.read_bytes(), paths.database_path.read_bytes()) == before


def test_portable_source_reads_committed_wal_rows_in_its_read_transaction(tmp_path: Path) -> None:
    from arxiv_digest.backup import BackupError, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    connection = sqlite3.connect(paths.database_path)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    try:
        connection.execute("DELETE FROM canonical_event_observations WHERE observation_id=51")
        connection.commit()
        assert Path(str(paths.database_path) + "-wal").stat().st_size > 0
        with pytest.raises(BackupError) as caught:
            inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
        assert caught.value.code == "cross_record_invalid"
    finally:
        connection.close()


def test_portable_source_uses_operation_lease_and_no_mutating_open_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    maintenance = MaintenanceBarrier()
    original_connect = sqlite3.connect

    def connect(database, **kwargs):
        assert maintenance.active_operations == 1
        assert "mode=ro" in database
        assert "immutable" not in database
        return original_connect(database, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("inspection used a mutating or unbounded source helper")

    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    monkeypatch.setattr(backup, "exclusive_flock", forbidden)
    monkeypatch.setattr(backup, "open_database", forbidden)
    backup.inspect_portable_backup_source(paths, maintenance=maintenance)


@pytest.mark.parametrize("payload", [
    b"not JSON", b'{"schema_version":1}', b'[]',
    b'{"schema_version":2,"schema_version":2}',
])
def test_portable_source_rejects_invalid_profiles_without_repair(
    tmp_path: Path, payload: bytes,
) -> None:
    from arxiv_digest.backup import BackupError, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    paths.profile_path.write_bytes(payload)
    with pytest.raises(BackupError) as caught:
        inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "unsupported_schema"
    assert paths.profile_path.read_bytes() == payload


def test_portable_source_deadline_is_checked_after_maintenance_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    import arxiv_digest.backup as backup

    paths = initialized_paths(tmp_path)
    now = 0.0

    class DelayedMaintenance:
        @contextmanager
        def operation(self):
            nonlocal now
            now = 2.0
            yield

    def forbidden(*args, **kwargs):
        pytest.fail("expired inspection touched source files")

    monkeypatch.setattr(backup.os, "open", forbidden)
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(
            paths, maintenance=DelayedMaintenance(), timeout=1.0, monotonic=lambda: now,
        )
    assert caught.value.code == "source_timeout"


def test_portable_source_repeated_eintr_stops_at_deadline_and_releases_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    maintenance = MaintenanceBarrier()
    now = 0.0
    attempted_fds = []

    def interrupt(fd, operation):
        nonlocal now
        attempted_fds.append(fd)
        now += 0.6
        raise InterruptedError

    monkeypatch.setattr(backup.fcntl, "flock", interrupt)
    monkeypatch.setattr(backup.time, "sleep", lambda duration: None)
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(
            paths, maintenance=maintenance, timeout=1.0, monotonic=lambda: now,
        )
    assert caught.value.code == "source_busy"
    assert len(attempted_fds) == 2
    assert maintenance.active_operations == 0
    with pytest.raises(OSError):
        os.fstat(attempted_fds[0])


@pytest.mark.parametrize("field", ["profile_lock_path", "profile_path", "database_path"])
def test_portable_source_rejects_path_substitution_at_open_and_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    target = getattr(paths, field)
    original_open = os.open
    opened = []

    def substitute(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if Path(path) == target:
            opened.append(descriptor)
            target.rename(tmp_path / "original")
            os.close(original_open(target, os.O_CREAT | os.O_RDWR, 0o600))
        return descriptor

    monkeypatch.setattr(backup.os, "open", substitute)
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "source_unsafe"
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_portable_source_requires_nofollow_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    monkeypatch.delattr(backup.os, "O_NOFOLLOW")
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "source_unsafe"


def test_portable_source_rejects_foreign_owned_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(backup.os, "getuid", lambda: foreign_uid)
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "source_unsafe"


def test_portable_source_rejects_profile_change_during_database_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.backup as backup
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    original_validate = backup._portable_source_payloads

    def substitute(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        with paths.profile_path.open("ab") as profile:
            profile.write(b" ")
        return result

    monkeypatch.setattr(backup, "_portable_source_payloads", substitute)
    with pytest.raises(backup.BackupError) as caught:
        backup.inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "source_unsafe"


def test_portable_source_malformed_profile_scalar_returns_a_backup_error(tmp_path: Path) -> None:
    from arxiv_digest.backup import BackupError, inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier

    paths = initialized_paths(tmp_path)
    payload = json.loads(paths.profile_path.read_bytes())
    payload["pdf_destination"]["kind"] = []
    paths.profile_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError) as caught:
        inspect_portable_backup_source(paths, maintenance=MaintenanceBarrier())
    assert caught.value.code == "unsupported_schema"
