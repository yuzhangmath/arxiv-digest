import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from arxiv_digest.paths import resolve_paths


class ResolvePathsTest(unittest.TestCase):
    def test_macos_paths_use_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = resolve_paths(platform="darwin", home=home, environ={})

            self.assertEqual(
                paths.data_dir,
                home / "Library/Application Support/arxiv-digest",
            )
            self.assertEqual(paths.profile_path, paths.data_dir / "profile.json")
            self.assertEqual(paths.database_path, paths.data_dir / "state.sqlite3")
            self.assertEqual(
                paths.cache_dir,
                home / "Library/Caches/arxiv-digest",
            )

    def test_linux_paths_honor_absolute_xdg_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = resolve_paths(
                platform="linux",
                home=home,
                environ={
                    "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"),
                    "XDG_CACHE_HOME": str(home / "cache"),
                },
            )

            self.assertEqual(
                paths.profile_path,
                home / "config/arxiv-digest/profile.json",
            )
            self.assertEqual(
                paths.database_path,
                home / "data/arxiv-digest/state.sqlite3",
            )
            self.assertEqual(paths.cache_dir, home / "cache/arxiv-digest")

    def test_linux_paths_ignore_relative_xdg_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = resolve_paths(
                platform="linux",
                home=home,
                environ={
                    "XDG_CONFIG_HOME": "relative-config",
                    "XDG_DATA_HOME": "relative-data",
                    "XDG_CACHE_HOME": "relative-cache",
                },
            )

            self.assertEqual(paths.config_dir, home / ".config/arxiv-digest")
            self.assertEqual(paths.data_dir, home / ".local/share/arxiv-digest")
            self.assertEqual(paths.cache_dir, home / ".cache/arxiv-digest")

    def test_explicit_test_root_isolated_from_platform_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            isolated = root / "isolated"
            paths = resolve_paths(
                platform="darwin",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(isolated),
                },
            )

            self.assertEqual(paths.profile_path, isolated / "config/profile.json")
            self.assertEqual(
                paths.database_path,
                isolated / "data/state.sqlite3",
            )

    def test_update_coordination_paths_use_the_fixed_private_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "isolated"
            paths = resolve_paths(
                platform="darwin",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(root),
                },
            )

            recovery = root / "data/update-recovery"
            self.assertEqual(paths.update_recovery_dir, recovery)
            self.assertEqual(
                paths.update_transition_lock_path,
                recovery / "update-transition.lock",
            )
            self.assertEqual(
                paths.launcher_operation_lock_path,
                recovery / "launcher-operation.lock",
            )
            self.assertEqual(
                paths.update_journal_lock_path,
                recovery / "update-journal.lock",
            )
            self.assertEqual(paths.update_plan_path, recovery / "update-plan.json")
            self.assertEqual(
                paths.update_journal_path,
                recovery / "update-journal.json",
            )
            self.assertEqual(
                paths.update_provenance_path,
                recovery / "update-provenance.json",
            )
            self.assertEqual(
                paths.update_diagnostic_log_path,
                recovery / "update-diagnostic.log",
            )
            self.assertEqual(
                paths.recovery_wrapper_path,
                recovery / "recover-arxiv-digest",
            )
            self.assertEqual(paths.update_runtime_dir, recovery / "runtime")

    def test_update_coordination_scaffolding_is_created_with_private_modes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "isolated"
            paths = resolve_paths(
                platform="linux",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(root),
                },
            )

            paths.ensure_update_coordination()

            for path in (
                paths.data_dir,
                paths.update_recovery_dir,
                paths.update_runtime_dir,
            ):
                with self.subTest(path=path):
                    self.assertTrue(path.is_dir())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in (
                paths.update_transition_lock_path,
                paths.launcher_operation_lock_path,
                paths.update_journal_lock_path,
            ):
                with self.subTest(path=path):
                    metadata = path.stat()
                    self.assertTrue(stat.S_ISREG(metadata.st_mode))
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                    self.assertEqual(metadata.st_nlink, 1)

    def test_default_environment_honors_explicit_test_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            isolated = home / "isolated"
            with mock.patch.dict(
                os.environ,
                {
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(isolated),
                },
                clear=True,
            ):
                paths = resolve_paths(platform="darwin", home=home)

            self.assertEqual(paths.profile_path, isolated / "config/profile.json")

    def test_ensure_never_creates_relative_xdg_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            working_directory = root / "working"
            working_directory.mkdir()
            paths = resolve_paths(
                platform="linux",
                home=root / "home",
                environ={
                    "XDG_CONFIG_HOME": "relative-config",
                    "XDG_DATA_HOME": "relative-data",
                    "XDG_CACHE_HOME": "relative-cache",
                },
            )
            previous = Path.cwd()
            try:
                os.chdir(working_directory)
                paths.ensure()
            finally:
                os.chdir(previous)

            self.assertFalse((working_directory / "relative-config").exists())
            self.assertFalse((working_directory / "relative-data").exists())
            self.assertFalse((working_directory / "relative-cache").exists())
            self.assertTrue(paths.config_dir.is_dir())
            self.assertTrue(paths.data_dir.is_dir())
            self.assertTrue(paths.cache_dir.is_dir())

    def test_ensure_rejects_symlinked_app_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "isolated"
            root.mkdir()
            redirect = root / "redirect"
            redirect.mkdir()
            (root / "config").symlink_to(redirect, target_is_directory=True)
            paths = resolve_paths(
                platform="linux",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(root),
                },
            )

            with self.assertRaises(OSError):
                paths.ensure()

    def test_ensure_repairs_owner_owned_app_directory_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "isolated"
            for name in ("config", "data", "cache"):
                path = root / name
                path.mkdir(parents=True)
                path.chmod(0o755)
            paths = resolve_paths(
                platform="linux",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(root),
                },
            )

            paths.ensure()

            for path in (
                paths.config_dir,
                paths.data_dir,
                paths.cache_dir,
                paths.backup_dir,
            ):
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_ensure_rejects_foreign_owned_app_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "isolated"
            (root / "config").mkdir(parents=True, mode=0o700)
            paths = resolve_paths(
                platform="linux",
                home=root / "unused-home",
                environ={
                    "ARXIV_DIGEST_TESTING": "1",
                    "ARXIV_DIGEST_TEST_ROOT": str(root),
                },
            )
            foreign_uid = os.getuid() + 1

            with mock.patch(
                "arxiv_digest.atomic.os.getuid",
                return_value=foreign_uid,
            ):
                with self.assertRaises(PermissionError):
                    paths.ensure()


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_platform_updater_paths_are_all_fixed_children(tmp_path: Path, platform: str) -> None:
    paths = resolve_paths(platform=platform, home=tmp_path, environ={})
    recovery = paths.data_dir / "update-recovery"
    assert paths.update_recovery_dir == recovery
    for field, filename in {
        "update_transition_lock_path": "update-transition.lock",
        "launcher_operation_lock_path": "launcher-operation.lock",
        "update_journal_lock_path": "update-journal.lock",
        "update_plan_path": "update-plan.json",
        "update_journal_path": "update-journal.json",
        "update_provenance_path": "update-provenance.json",
        "update_diagnostic_log_path": "update-diagnostic.log",
        "recovery_wrapper_path": "recover-arxiv-digest",
        "update_runtime_dir": "runtime",
    }.items():
        assert getattr(paths, field) == recovery / filename
    paths.ensure_update_coordination()
    for directory in (paths.data_dir, recovery, paths.update_runtime_dir):
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    for lock in (
        paths.update_transition_lock_path,
        paths.launcher_operation_lock_path,
        paths.update_journal_lock_path,
    ):
        metadata = lock.lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_uid == os.getuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_nlink == 1


@pytest.mark.parametrize("field", ["data_dir", "update_recovery_dir", "update_runtime_dir"])
@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_updater_directories_are_never_followed_or_repaired(
    tmp_path: Path, field: str, unsafe: str,
) -> None:
    paths = resolve_paths(
        home=tmp_path, environ={
            "ARXIV_DIGEST_TESTING": "1", "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "app"),
        },
    )
    paths.ensure_update_coordination()
    target = getattr(paths, field)
    if unsafe == "mode":
        target.chmod(0o755)
    else:
        original = target.with_name("original-directory")
        target.rename(original)
        target.symlink_to(original, target_is_directory=True)
    with pytest.raises(OSError):
        paths.ensure_update_coordination()
    if unsafe == "mode":
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
    else:
        assert target.is_symlink()


@pytest.mark.parametrize("field", [
    "update_transition_lock_path", "launcher_operation_lock_path", "update_journal_lock_path",
])
@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink"])
def test_updater_lock_scaffolding_refuses_unsafe_existing_files(
    tmp_path: Path, field: str, unsafe: str,
) -> None:
    paths = resolve_paths(
        home=tmp_path, environ={
            "ARXIV_DIGEST_TESTING": "1", "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "app"),
        },
    )
    paths.ensure_update_coordination()
    target = getattr(paths, field)
    target.write_bytes(b"must be preserved")
    original = tmp_path / "original.lock"
    if unsafe == "mode":
        target.chmod(0o644)
    elif unsafe == "symlink":
        target.rename(original)
        target.symlink_to(original)
    else:
        os.link(target, original)
    with pytest.raises(OSError):
        paths.ensure_update_coordination()
    assert target.read_bytes() == b"must be preserved"
