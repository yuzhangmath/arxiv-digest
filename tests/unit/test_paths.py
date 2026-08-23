import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
