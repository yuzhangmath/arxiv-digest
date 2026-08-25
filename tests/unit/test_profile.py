import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from arxiv_digest.atomic import atomic_write, exclusive_flock
from arxiv_digest.profile import (
    LegacyProfileGenerationError,
    PdfDestination,
    Profile,
    ProfileCategory,
    ProfileRepository,
    ProfileRevisionError,
    decode_profile,
    encode_profile,
)


def sample_profile(destination: Path, revision: int = 1) -> Profile:
    return Profile(
        schema_version=2,
        revision=revision,
        category_coverage=(
            ProfileCategory("cs.CL", date(2026, 7, 1)),
            ProfileCategory("stat.ML", date(2026, 7, 8)),
        ),
        keywords=("verification",),
        phrases=("causal representation",),
        authors=("Alex Example",),
        seed_papers=("2607.00001",),
        pdf_destination=PdfDestination("custom", destination),
    )


def repository_at(root: Path) -> ProfileRepository:
    return ProfileRepository(root / "profile.json", root / "profile.lock")


class ProfileRepositoryTest(unittest.TestCase):
    def test_categories_projects_category_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = sample_profile(Path(directory) / "papers")

            self.assertEqual(profile.categories, ("cs.CL", "stat.ML"))

    def test_schema_v2_round_trips_category_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            expected = Profile(
                schema_version=2,
                revision=1,
                category_coverage=(
                    ProfileCategory("cs.CL", date(2026, 7, 1)),
                    ProfileCategory("stat.ML", date(2026, 7, 8)),
                ),
                keywords=("verification",),
                phrases=("causal representation",),
                authors=("Alex Example",),
                seed_papers=("2607.00001",),
                pdf_destination=PdfDestination("custom", root / "papers"),
            )

            decoded = decode_profile(encode_profile(expected))

            self.assertEqual(decoded, expected)

    def test_encoded_category_coverage_has_exact_pair_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            encoded = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )

            self.assertNotIn("categories", encoded)
            self.assertEqual(
                encoded["category_coverage"],
                [
                    {"category": "cs.CL", "coverage_start": "2026-07-01"},
                    {"category": "stat.ML", "coverage_start": "2026-07-08"},
                ],
            )

    def test_category_coverage_requires_canonical_iso_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["category_coverage"][0]["coverage_start"] = "20260701"

            with self.assertRaisesRegex(ValueError, "ISO date"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_legacy_profile_is_rejected_without_modifying_its_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)
            legacy_payload = json.dumps(
                {
                    "schema_version": 1,
                    "revision": 7,
                    "categories": ["cs.CL"],
                    "keywords": ["verification"],
                    "phrases": [],
                    "authors": [],
                    "seed_papers": [],
                    "pdf_destination": {
                        "kind": "custom",
                        "path": str(root / "papers"),
                    },
                },
                sort_keys=True,
            ).encode("utf-8")
            repository.path.write_bytes(legacy_payload)
            before = hashlib.sha256(repository.path.read_bytes()).digest()

            with self.assertRaisesRegex(
                LegacyProfileGenerationError,
                "Clean reset with recovery copy",
            ):
                repository.load()

            after = hashlib.sha256(repository.path.read_bytes()).digest()
            self.assertEqual(after, before)

    def test_missing_category_coverage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            del value["category_coverage"]

            with self.assertRaisesRegex(ValueError, "missing profile keys"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_category_coverage_entry_keys_must_be_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            for replacement in (
                {"category": "cs.CL"},
                {
                    "category": "cs.CL",
                    "coverage_start": "2026-07-01",
                    "unexpected": True,
                },
            ):
                with self.subTest(replacement=replacement):
                    value = json.loads(json.dumps(baseline))
                    value["category_coverage"][0] = replacement
                    with self.assertRaisesRegex(ValueError, "coverage keys"):
                        decode_profile(json.dumps(value).encode("utf-8"))

    def test_duplicate_profile_categories_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["category_coverage"][1]["category"] = "CS.cl"

            with self.assertRaisesRegex(ValueError, "duplicate category_coverage"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_legacy_categories_cannot_be_aligned_with_category_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["categories"] = ["cs.CL", "math.AT"]

            with self.assertRaisesRegex(ValueError, "unknown profile keys"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_atomic_profile_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)
            expected = sample_profile(root / "papers")

            repository.save_atomic(expected, expected_revision=None)

            self.assertEqual(repository.load(), expected)

    def test_missing_profile_has_no_default_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = repository_at(Path(directory).resolve())

            self.assertIsNone(repository.load())

    def test_failed_replace_preserves_previous_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)
            original = sample_profile(root / "first")
            repository.save_atomic(original, expected_revision=None)

            with mock.patch("os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    repository.save_atomic(
                        sample_profile(root / "second", revision=2),
                        expected_revision=1,
                    )

            self.assertEqual(repository.load(), original)

    def test_successful_profile_write_fsyncs_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)

            with mock.patch(
                "arxiv_digest.atomic.os.fsync",
                wraps=os.fsync,
            ) as fsync:
                repository.save_atomic(
                    sample_profile(root / "papers"),
                    expected_revision=None,
                )

            self.assertEqual(fsync.call_count, 2)

    def test_profile_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)

            repository.save_atomic(
                sample_profile(root / "papers"),
                expected_revision=None,
            )

            self.assertEqual(stat.S_IMODE(repository.path.stat().st_mode), 0o600)

    def test_atomic_write_applies_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "value.bin"

            atomic_write(path, b"value", mode=0o640)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_exclusive_flock_rejects_symlink_without_mutating_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target.lock"
            target.write_bytes(b"")
            target.chmod(0o640)
            link = root / "profile.lock"
            link.symlink_to(target)

            with self.assertRaises(OSError):
                with exclusive_flock(link):
                    self.fail("a symlink lock target must not be acquired")

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_exclusive_flock_rejects_existing_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "profile.lock"
            lock_path.write_bytes(b"")
            lock_path.chmod(0o640)

            with self.assertRaises(PermissionError):
                with exclusive_flock(lock_path):
                    self.fail("an insecure lock file must not be acquired")

            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o640)

    def test_exclusive_flock_rejects_non_regular_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "profile.lock"
            os.mkfifo(lock_path, mode=0o600)

            with self.assertRaises(PermissionError):
                with exclusive_flock(lock_path):
                    self.fail("a FIFO must not be acquired as a lock file")

    def test_exclusive_flock_rejects_foreign_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "profile.lock"
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)

            with mock.patch(
                "arxiv_digest.atomic.os.getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaises(PermissionError):
                    with exclusive_flock(lock_path):
                        self.fail("a foreign-owned lock file must not be acquired")

    def test_exclusive_flock_rejects_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            redirect = root / "redirect"
            redirect.mkdir()
            lock_parent = root / "locks"
            lock_parent.symlink_to(redirect, target_is_directory=True)

            with self.assertRaises(OSError):
                with exclusive_flock(lock_parent / "profile.lock"):
                    self.fail("a lock must not be created through a symlink")

            self.assertFalse((redirect / "profile.lock").exists())

    def test_stale_profile_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = repository_at(root)
            repository.save_atomic(
                sample_profile(root / "first"),
                expected_revision=None,
            )

            with self.assertRaises(ProfileRevisionError) as raised:
                repository.save_atomic(
                    sample_profile(root / "second", revision=2),
                    expected_revision=0,
                )

            self.assertEqual(raised.exception.expected, 0)
            self.assertEqual(raised.exception.actual, 1)

    def test_unknown_profile_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["unexpected"] = True

            with self.assertRaisesRegex(ValueError, "unknown profile keys"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_unknown_pdf_destination_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["pdf_destination"]["unexpected"] = True

            with self.assertRaisesRegex(ValueError, "PDF destination keys"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_duplicate_json_object_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = encode_profile(sample_profile(Path(directory) / "papers"))
            duplicated = payload.replace(
                b'"revision": 1,',
                b'"revision": 1,\n  "revision": 2,',
                1,
            )

            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                decode_profile(duplicated)

    def test_missing_profile_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            del value["authors"]

            with self.assertRaisesRegex(ValueError, "profile keys"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_unsupported_profile_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["schema_version"] = 1

            with self.assertRaisesRegex(ValueError, "schema version"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_revision_must_be_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            for invalid in (0, -1, True, 1.0, "1"):
                with self.subTest(invalid=invalid):
                    value = dict(baseline)
                    value["revision"] = invalid
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        decode_profile(json.dumps(value).encode("utf-8"))

    def test_unsupported_pdf_destination_kind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["pdf_destination"]["kind"] = "network-share"

            with self.assertRaisesRegex(ValueError, "destination kind"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_profile_list_values_are_whitespace_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["category_coverage"][0]["category"] = "  cs.CL  "
            value["keywords"] = ["  proof   search "]
            value["phrases"] = [" causal\n representation "]
            value["authors"] = ["  Alex   Example "]
            value["seed_papers"] = [" 2607.00001 "]

            decoded = decode_profile(json.dumps(value).encode("utf-8"))

            self.assertEqual(decoded.categories, ("cs.CL", "stat.ML"))
            self.assertEqual(decoded.keywords, ("proof search",))
            self.assertEqual(decoded.phrases, ("causal representation",))
            self.assertEqual(decoded.authors, ("Alex Example",))
            self.assertEqual(decoded.seed_papers, ("2607.00001",))

    def test_blank_profile_list_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            for field in (
                "keywords",
                "phrases",
                "authors",
                "seed_papers",
            ):
                with self.subTest(field=field):
                    value = json.loads(json.dumps(baseline))
                    value[field] = [" \t\n "]
                    with self.assertRaisesRegex(ValueError, f"blank {field}"):
                        decode_profile(json.dumps(value).encode("utf-8"))

    def test_casefold_duplicate_profile_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["keywords"] = ["Straße", "STRASSE"]

            with self.assertRaisesRegex(ValueError, "duplicate keywords"):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_two_writers_publish_only_one_next_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository_at(root).save_atomic(
                sample_profile(root / "first"),
                expected_revision=None,
            )
            barrier = threading.Barrier(2)

            class CoordinatedRepository(ProfileRepository):
                def load(self) -> Profile | None:
                    value = super().load()
                    try:
                        barrier.wait(timeout=0.25)
                    except threading.BrokenBarrierError:
                        pass
                    return value

            outcomes: list[str] = []

            def write(destination: str) -> None:
                repository = CoordinatedRepository(
                    root / "profile.json",
                    root / "profile.lock",
                )
                try:
                    repository.save_atomic(
                        sample_profile(root / destination, revision=2),
                        expected_revision=1,
                    )
                except ProfileRevisionError:
                    outcomes.append("stale")
                else:
                    outcomes.append("saved")

            writers = [
                threading.Thread(target=write, args=("second-a",)),
                threading.Thread(target=write, args=("second-b",)),
            ]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join(timeout=2)

            self.assertEqual(sorted(outcomes), ["saved", "stale"])

    def test_profile_constructor_normalizes_tuple_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile(
                schema_version=2,
                revision=1,
                category_coverage=(
                    ProfileCategory(" cs.CL ", date(2026, 7, 1)),
                ),
                keywords=(" proof   search ",),
                phrases=(),
                authors=(),
                seed_papers=(),
                pdf_destination=PdfDestination(
                    "custom",
                    Path(directory).resolve(),
                ),
            )

            self.assertEqual(profile.categories, ("cs.CL",))
            self.assertEqual(profile.keywords, ("proof search",))

    def test_profile_constructor_rejects_nonpositive_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "positive integer"):
                sample_profile(Path(directory).resolve(), revision=0)

    def test_profile_constructor_rejects_unsupported_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, "schema version"):
                Profile(
                    schema_version=1,
                    revision=1,
                    category_coverage=(
                        ProfileCategory("cs.CL", date(2026, 7, 1)),
                    ),
                    keywords=(),
                    phrases=(),
                    authors=(),
                    seed_papers=(),
                    pdf_destination=PdfDestination("custom", root),
                )

    def test_pdf_destination_constructor_rejects_unknown_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "destination kind"):
                PdfDestination("network-share", Path(directory).resolve())

    def test_profile_json_lists_require_string_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            for invalid in ("verification", [1]):
                with self.subTest(invalid=invalid):
                    value = dict(baseline)
                    value["keywords"] = invalid
                    with self.assertRaisesRegex(ValueError, "list of strings"):
                        decode_profile(json.dumps(value).encode("utf-8"))

    def test_profile_requires_at_least_one_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, "at least one category"):
                Profile(
                    schema_version=2,
                    revision=1,
                    category_coverage=(),
                    keywords=(),
                    phrases=(),
                    authors=(),
                    seed_papers=(),
                    pdf_destination=PdfDestination("custom", root),
                )

    def test_profile_json_requires_object_containers(self) -> None:
        with self.assertRaisesRegex(ValueError, "profile must be a JSON object"):
            decode_profile(b"3")

        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            value["pdf_destination"] = 3
            with self.assertRaisesRegex(
                ValueError,
                "PDF destination must be a JSON object",
            ):
                decode_profile(json.dumps(value).encode("utf-8"))

    def test_pdf_destination_path_must_be_nonblank_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = json.loads(
                encode_profile(sample_profile(Path(directory) / "papers"))
            )
            for invalid in (3, ""):
                with self.subTest(invalid=invalid):
                    value = json.loads(json.dumps(baseline))
                    value["pdf_destination"]["path"] = invalid
                    with self.assertRaisesRegex(ValueError, "destination path"):
                        decode_profile(json.dumps(value).encode("utf-8"))

    def test_pdf_destination_constructor_requires_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "destination path"):
            PdfDestination("custom", "papers")


if __name__ == "__main__":
    unittest.main()
