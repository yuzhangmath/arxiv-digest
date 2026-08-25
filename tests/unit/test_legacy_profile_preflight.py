from __future__ import annotations

import hashlib
import json

import pytest

from arxiv_digest.application import _DefaultRuntime
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import (
    LegacyProfileGenerationError,
    ProfileRepository,
)
from arxiv_digest.storage.database import open_database
from arxiv_digest.web.lifecycle import LifecycleController


@pytest.mark.parametrize("existing_database", (False, True))
def test_legacy_profile_is_rejected_before_database_creation_or_open(
    tmp_path,
    existing_database: bool,
) -> None:
    paths = resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "state"),
        },
    )
    paths.ensure()
    if existing_database:
        open_database(paths.database_path).close()
    database_before = (
        hashlib.sha256(paths.database_path.read_bytes()).digest()
        if existing_database
        else None
    )
    legacy_payload = (
        json.dumps(
            {
                "schema_version": 1,
                "revision": 7,
                "categories": ["cs.CL"],
                "keywords": [],
                "phrases": [],
                "authors": [],
                "seed_papers": [],
                "pdf_destination": {
                    "kind": "downloads",
                    "path": str(tmp_path / "papers"),
                },
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    paths.profile_path.write_bytes(legacy_payload)
    profile_before = hashlib.sha256(paths.profile_path.read_bytes()).digest()
    maintenance = MaintenanceBarrier()
    runtime = _DefaultRuntime(
        paths,
        ProfileRepository(
            paths.profile_path,
            paths.profile_lock_path,
            maintenance=maintenance,
        ),
        maintenance,
        LifecycleController(),
        output=lambda _message: None,
    )

    with pytest.raises(
        LegacyProfileGenerationError,
        match="Clean reset with recovery copy",
    ):
        runtime.open_database()

    assert hashlib.sha256(paths.profile_path.read_bytes()).digest() == profile_before
    if existing_database:
        assert hashlib.sha256(paths.database_path.read_bytes()).digest() == database_before
    else:
        assert not paths.database_path.exists()
