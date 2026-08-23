from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arxiv_digest.atomic import atomic_write, exclusive_flock
from arxiv_digest.maintenance import MaintenanceBarrier


@dataclass(frozen=True, slots=True)
class PdfDestination:
    kind: Literal["downloads", "documents", "custom"]
    path: Path

    def __post_init__(self) -> None:
        if self.kind not in {"downloads", "documents", "custom"}:
            raise ValueError("unsupported PDF destination kind")
        if not isinstance(self.path, Path):
            raise ValueError("PDF destination path must be a pathlib.Path")


@dataclass(frozen=True, slots=True)
class Profile:
    schema_version: int
    revision: int
    categories: tuple[str, ...]
    keywords: tuple[str, ...]
    phrases: tuple[str, ...]
    authors: tuple[str, ...]
    seed_papers: tuple[str, ...]
    pdf_destination: PdfDestination

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported profile schema version")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("profile revision must be a positive integer")
        for field in (
            "categories",
            "keywords",
            "phrases",
            "authors",
            "seed_papers",
        ):
            object.__setattr__(
                self,
                field,
                _normalized_values(getattr(self, field), field),
            )
        if not self.categories:
            raise ValueError("profile requires at least one category")


class ProfileRevisionError(RuntimeError):
    def __init__(self, expected: int | None, actual: int | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"profile revision mismatch: expected {expected}, actual {actual}"
        )


def encode_profile(profile: Profile) -> bytes:
    value = {
        "schema_version": profile.schema_version,
        "revision": profile.revision,
        "categories": list(profile.categories),
        "keywords": list(profile.keywords),
        "phrases": list(profile.phrases),
        "authors": list(profile.authors),
        "seed_papers": list(profile.seed_papers),
        "pdf_destination": {
            "kind": profile.pdf_destination.kind,
            "path": str(profile.pdf_destination.path),
        },
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _normalized_values(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or any(
        type(value) is not str for value in values
    ):
        raise ValueError(f"{field} must be a list of strings")
    normalized = tuple(" ".join(value.split()) for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"blank {field} entry")
    folded = tuple(value.casefold() for value in normalized)
    if len(folded) != len(set(folded)):
        raise ValueError(f"duplicate {field} entry")
    return normalized


def decode_profile(payload: bytes) -> Profile:
    value = json.loads(payload, object_pairs_hook=_object_without_duplicates)
    if not isinstance(value, dict):
        raise ValueError("profile must be a JSON object")
    expected_keys = {
        "schema_version",
        "revision",
        "categories",
        "keywords",
        "phrases",
        "authors",
        "seed_papers",
        "pdf_destination",
    }
    unknown = set(value) - expected_keys
    if unknown:
        raise ValueError(f"unknown profile keys: {sorted(unknown)}")
    missing = expected_keys - set(value)
    if missing:
        raise ValueError(f"missing profile keys: {sorted(missing)}")
    if value["schema_version"] != 1:
        raise ValueError("unsupported profile schema version")
    revision = value["revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("profile revision must be a positive integer")
    destination = value["pdf_destination"]
    if not isinstance(destination, dict):
        raise ValueError("PDF destination must be a JSON object")
    if set(destination) != {"kind", "path"}:
        raise ValueError("PDF destination keys must be exactly kind and path")
    if destination["kind"] not in {"downloads", "documents", "custom"}:
        raise ValueError("unsupported PDF destination kind")
    destination_path = destination["path"]
    if type(destination_path) is not str or not destination_path:
        raise ValueError("PDF destination path must be nonblank text")
    return Profile(
        schema_version=value["schema_version"],
        revision=value["revision"],
        categories=_normalized_values(value["categories"], "categories"),
        keywords=_normalized_values(value["keywords"], "keywords"),
        phrases=_normalized_values(value["phrases"], "phrases"),
        authors=_normalized_values(value["authors"], "authors"),
        seed_papers=_normalized_values(value["seed_papers"], "seed_papers"),
        pdf_destination=PdfDestination(
            destination["kind"],
            Path(destination_path),
        ),
    )


class ProfileRepository:
    def __init__(
        self,
        path: Path,
        lock_path: Path,
        *,
        maintenance: MaintenanceBarrier | None = None,
    ) -> None:
        self.path = path
        self.lock_path = lock_path
        self.maintenance = maintenance

    def _operation(self):
        return (
            nullcontext()
            if self.maintenance is None
            else self.maintenance.operation()
        )

    def load(self) -> Profile | None:
        with self._operation():
            if not self.path.exists():
                return None
            return decode_profile(self.path.read_bytes())

    def save_atomic(
        self,
        profile: Profile,
        *,
        expected_revision: int | None,
    ) -> None:
        with self._operation():
            with exclusive_flock(self.lock_path):
                current = self.load()
                actual = None if current is None else current.revision
                if actual != expected_revision:
                    raise ProfileRevisionError(expected_revision, actual)
                atomic_write(self.path, encode_profile(profile), mode=0o600)
