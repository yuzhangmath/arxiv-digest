from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from arxiv_digest.atomic import ensure_private_directory


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

    def ensure(self) -> None:
        for path in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.backup_dir,
        ):
            ensure_private_directory(path)


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
        return AppPaths(
            config_dir=root / "config",
            data_dir=root / "data",
            cache_dir=root / "cache",
            backup_dir=root / "data/backups",
            profile_path=root / "config/profile.json",
            database_path=root / "data/state.sqlite3",
            runtime_descriptor_path=root / "data/runtime.json",
            process_lock_path=root / "data/runtime.lock",
            profile_lock_path=root / "config/profile.lock",
            restore_journal_path=root / "config/restore-journal.json",
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
    )
