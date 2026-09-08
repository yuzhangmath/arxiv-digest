from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from arxiv_digest.atomic import (
    ensure_private_directory,
    ensure_private_directory_strict,
    ensure_private_lock_file,
)
from arxiv_digest.update_contract import (
    LAUNCHER_OPERATION_LOCK_FILENAME,
    RECOVERY_WRAPPER_FILENAME,
    TRANSITION_LOCK_FILENAME,
    UPDATE_DIAGNOSTIC_LOG_FILENAME,
    UPDATE_JOURNAL_FILENAME,
    UPDATE_JOURNAL_LOCK_FILENAME,
    UPDATE_PLAN_FILENAME,
    UPDATE_PROVENANCE_FILENAME,
    UPDATE_RECOVERY_DIRNAME,
    UPDATE_RUNTIME_DIRNAME,
)


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    backup_dir: Path
    profile_path: Path
    database_path: Path
    runtime_descriptor_path: Path
    process_lock_path: Path
    profile_lock_path: Path
    restore_journal_path: Path
    update_recovery_dir: Path
    update_transition_lock_path: Path
    launcher_operation_lock_path: Path
    update_journal_lock_path: Path
    update_plan_path: Path
    update_journal_path: Path
    update_provenance_path: Path
    update_diagnostic_log_path: Path
    recovery_wrapper_path: Path
    update_runtime_dir: Path

    def ensure(self) -> None:
        for path in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.backup_dir,
        ):
            ensure_private_directory(path)

    def ensure_update_coordination(self) -> None:
        for path in (
            self.data_dir,
            self.update_recovery_dir,
            self.update_runtime_dir,
        ):
            ensure_private_directory_strict(path)
        for path in (
            self.update_transition_lock_path,
            self.launcher_operation_lock_path,
            self.update_journal_lock_path,
        ):
            ensure_private_lock_file(path)


def _resolved_app_paths(*, config: Path, data: Path, cache: Path) -> AppPaths:
    recovery = data / UPDATE_RECOVERY_DIRNAME
    return AppPaths(
        config_dir=config,
        data_dir=data,
        cache_dir=cache,
        backup_dir=data / "backups",
        profile_path=config / "profile.json",
        database_path=data / "state.sqlite3",
        runtime_descriptor_path=data / "runtime.json",
        process_lock_path=data / "runtime.lock",
        profile_lock_path=config / "profile.lock",
        restore_journal_path=config / "restore-journal.json",
        update_recovery_dir=recovery,
        update_transition_lock_path=recovery / TRANSITION_LOCK_FILENAME,
        launcher_operation_lock_path=recovery / LAUNCHER_OPERATION_LOCK_FILENAME,
        update_journal_lock_path=recovery / UPDATE_JOURNAL_LOCK_FILENAME,
        update_plan_path=recovery / UPDATE_PLAN_FILENAME,
        update_journal_path=recovery / UPDATE_JOURNAL_FILENAME,
        update_provenance_path=recovery / UPDATE_PROVENANCE_FILENAME,
        update_diagnostic_log_path=recovery / UPDATE_DIAGNOSTIC_LOG_FILENAME,
        recovery_wrapper_path=recovery / RECOVERY_WRAPPER_FILENAME,
        update_runtime_dir=recovery / UPDATE_RUNTIME_DIRNAME,
    )


def resolve_paths(
    *,
    platform: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppPaths:
    platform = platform or sys.platform
    home = (home or Path.home()).resolve()
    environ = dict(os.environ if environ is None else environ)
    if environ.get("ARXIV_DIGEST_TESTING") == "1":
        raw_test_root = environ.get("ARXIV_DIGEST_TEST_ROOT")
        if not raw_test_root:
            raise RuntimeError("testing mode requires an explicit test root")
        candidate = Path(raw_test_root)
        if not candidate.is_absolute():
            raise RuntimeError("test root must be absolute")
        root = candidate.resolve()
        if root in (Path("/"), home):
            raise RuntimeError("test root is too broad")
        return _resolved_app_paths(
            config=root / "config",
            data=root / "data",
            cache=root / "cache",
        )
    if platform == "darwin":
        data = home / "Library/Application Support/arxiv-digest"
        config = data
        cache = home / "Library/Caches/arxiv-digest"
    elif platform.startswith("linux"):
        def absolute_xdg(name: str, fallback: Path) -> Path:
            raw = environ.get(name)
            if raw is None:
                return fallback
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else fallback

        config = absolute_xdg("XDG_CONFIG_HOME", home / ".config") / "arxiv-digest"
        data = absolute_xdg("XDG_DATA_HOME", home / ".local/share") / "arxiv-digest"
        cache = absolute_xdg("XDG_CACHE_HOME", home / ".cache") / "arxiv-digest"
    else:
        raise RuntimeError("arxiv-digest 0.2 supports macOS and Linux")
    return _resolved_app_paths(config=config, data=data, cache=cache)
