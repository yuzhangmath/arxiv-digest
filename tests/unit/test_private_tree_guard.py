import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from scripts.private_tree_guard import create_snapshot, verify_snapshot
from scripts.private_tree_guard import _write_manifest_atomic


class PrivateTreeGuardTest(unittest.TestCase):
    def test_snapshot_detects_changed_and_added_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private"
            source.mkdir()
            (source / "saved.json").write_text("one", encoding="utf-8")
            before = create_snapshot(source, include_git=False)

            (source / "saved.json").write_text("two", encoding="utf-8")
            (source / "added.txt").write_text("new", encoding="utf-8")

            result = verify_snapshot(source, before, include_git=False)
            self.assertEqual(result.changed, ("saved.json",))
            self.assertEqual(result.added, ("added.txt",))
            self.assertEqual(result.removed, ())

    def test_snapshot_json_contains_hashes_not_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private"
            source.mkdir()
            (source / "personal.txt").write_text(
                "PRIVATE VALUE", encoding="utf-8"
            )
            snapshot = create_snapshot(source, include_git=False)
            encoded = json.dumps(snapshot.to_json())
            self.assertNotIn("PRIVATE VALUE", encoded)
            self.assertEqual(snapshot.files["personal.txt"].size, 13)

    def test_symlink_hashes_only_its_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private"
            source.mkdir()
            external = root / "secret.txt"
            external.write_text("FIRST PRIVATE VALUE", encoding="utf-8")
            link = source / "external-link"
            link.symlink_to(external)

            before = create_snapshot(source, include_git=False)
            external.write_text("SECOND PRIVATE VALUE", encoding="utf-8")
            after = create_snapshot(source, include_git=False)

            state = before.files["external-link"]
            target = os.fsencode(os.readlink(link))
            self.assertEqual(state.kind, "symlink")
            self.assertEqual(state.size, len(target))
            self.assertEqual(state.sha256, hashlib.sha256(target).hexdigest())
            self.assertEqual(after.files["external-link"], state)

    def test_snapshot_detects_chmod_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private"
            source.mkdir()
            saved = source / "saved.txt"
            saved.write_text("unchanged", encoding="utf-8")
            saved.chmod(0o600)
            before = create_snapshot(source, include_git=False)

            saved.chmod(0o644)

            result = verify_snapshot(source, before, include_git=False)
            self.assertEqual(result.changed, ("saved.txt",))

    def test_git_snapshot_detects_each_private_git_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private"
            source.mkdir()
            outputs = {
                ("rev-parse", "HEAD"): b"abc123\n",
                ("show-ref",): b"abc123 refs/heads/main\n",
                ("ls-files", "--stage", "-z"): b"100644 abc123 0\ttracked\0",
                ("status", "--porcelain=v1", "-z"): b"",
                ("config", "--local", "--null", "--list"): b"core.bare\nfalse\0",
            }

            def run(command, **kwargs):
                self.assertEqual(command[:2], ["git", "--no-optional-locks"])
                self.assertEqual(command[2:4], ["-C", str(source.resolve())])
                self.assertTrue(kwargs["check"])
                return CompletedProcess(command, 0, outputs[tuple(command[4:])], b"")

            with mock.patch("scripts.private_tree_guard.subprocess.run", side_effect=run):
                before = create_snapshot(source)
                for git_arguments in (
                    ("show-ref",),
                    ("ls-files", "--stage", "-z"),
                    ("status", "--porcelain=v1", "-z"),
                    ("config", "--local", "--null", "--list"),
                ):
                    with self.subTest(git_arguments=git_arguments):
                        original = outputs[git_arguments]
                        outputs[git_arguments] = original + b"changed"
                        self.assertTrue(verify_snapshot(source, before).git_changed)
                        outputs[git_arguments] = original

    def test_manifest_publication_is_private_atomic_and_never_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "snapshot.json"
            with mock.patch("scripts.private_tree_guard.os.link", wraps=os.link) as link:
                _write_manifest_atomic(manifest, b"first\n")

            self.assertEqual(manifest.read_bytes(), b"first\n")
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            link.assert_called_once()
            self.assertEqual(tuple(manifest.parent.glob(".snapshot.json.*")), ())

            with self.assertRaises(FileExistsError):
                _write_manifest_atomic(manifest, b"replacement\n")
            self.assertEqual(manifest.read_bytes(), b"first\n")

    def test_manifest_publication_loses_race_without_replacing_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "snapshot.json"
            real_link = os.link

            def racing_link(source, destination):
                Path(destination).write_bytes(b"race winner\n")
                real_link(source, destination)

            with mock.patch(
                "scripts.private_tree_guard.os.link", side_effect=racing_link
            ):
                with self.assertRaises(FileExistsError):
                    _write_manifest_atomic(manifest, b"ours\n")

            self.assertEqual(manifest.read_bytes(), b"race winner\n")
            self.assertEqual(tuple(manifest.parent.glob(".snapshot.json.*")), ())

    def test_manifest_publication_preserves_existing_broken_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "snapshot.json"
            original_target = "missing-private-target"
            manifest.symlink_to(original_target)

            with self.assertRaises(FileExistsError):
                _write_manifest_atomic(manifest, b"ours\n")

            self.assertTrue(manifest.is_symlink())
            self.assertEqual(os.readlink(manifest), original_target)
            self.assertEqual(tuple(manifest.parent.glob(".snapshot.json.*")), ())


if __name__ == "__main__":
    unittest.main()
